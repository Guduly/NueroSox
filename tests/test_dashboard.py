"""Headless-Chrome tests of laptop/host_dashboard.py + dash_template.html (stdlib unittest; skipped if Chrome is missing).
Uses synthetic CT1 logs (v2) and a v1 log made by the ORIGINAL board when ../src/neurosox_dev exists, else a v1 log derived by stripping the new keys."""
import json, os, re, shutil, socket, subprocess, sys, tempfile, time, unittest, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, HERE)
import gen_synthetic as gs
CHROME = shutil.which("google-chrome") or "/usr/bin/google-chrome"
BOARD = os.path.join(ROOT, "board", "cardiotwin_board.py"); HOST = os.path.join(ROOT, "laptop", "host_dashboard.py")
ORIG = os.path.join(os.path.dirname(ROOT), "src", "neurosox_dev", "board", "cardiotwin_board.py")
OUT = os.path.join(HERE, "out")
NEW_KEYS = ("act", "act_conf", "act_score", "act_reason", "act_basis", "mot_f", "mot_ratio", "p_card", "ecg_agree", "hr_elev", "hr_ref", "strain",
            "strain_conf", "strain_why", "strain_parts", "area", "area_q", "area_reason", "area_top", "area_elev", "area_h_s")

def free_port():
    with socket.socket() as s: s.bind(("127.0.0.1", 0)); return s.getsockname()[1]
def board_lines(script, cap, td):
    p = subprocess.run([sys.executable, "-I", script, "--replay", cap, "--reset", "--state", os.path.join(td, "st.json"), "--log", os.path.join(td, "l.jsonl")], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-400:]; return [l for l in p.stdout.splitlines() if l.startswith("CT1 ")]
def write(path, lines, mode="w"):
    with open(path, mode) as f: f.write("".join(l + "\n" for l in lines))

class Host:
    def __init__(s, logfile, td):
        s.port = free_port(); s.p = subprocess.Popen([sys.executable, "-I", HOST, "--file", logfile, "--port", str(s.port), "--save", os.path.join(td, "rx%d.jsonl" % s.port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try: urllib.request.urlopen(s.url + "status.json", timeout=1); break
            except Exception: time.sleep(0.2)
    url = property(lambda s: "http://127.0.0.1:%d/" % s.port)
    def data(s):
        with urllib.request.urlopen(s.url + "data.json") as r: return json.load(r)
    def wait_lines(s, n):
        for _ in range(60):
            if s.data()["lines"] >= n: return
            time.sleep(0.2)
    def dom(s, shot=None):   # NOTE: headless Chrome cannot be forced into prefers-color-scheme:dark here; the dark palette was checked by hand (see README)
        base = [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=6000", "--window-size=1440,900"]
        if shot: subprocess.run(base + ["--screenshot=" + shot, s.url], capture_output=True, timeout=90)
        return subprocess.run(base + ["--dump-dom", s.url], capture_output=True, text=True, timeout=90).stdout
    def stop(s): s.p.terminate(); s.p.wait(5)

def text_of(dom, eid):
    m = re.search(r'<[a-z0-9]+[^>]*id="%s"[^>]*>(.*?)</(?:span|div|pre)>' % eid, dom, re.S); return re.sub(r"<[^>]+>", "", m.group(1)) if m else None
def fill_of(dom, i):
    m = re.search(r'<path[^>]*id="reg-%d"[^>]*>' % i, dom); f = re.search(r'\sfill="([^"]*)"', m.group(0)) if m else None; return f.group(1) if f else None

@unittest.skipUnless(os.path.exists(CHROME), "headless Chrome not found")
class Dashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(OUT, exist_ok=True); cls.td = tempfile.TemporaryDirectory(); td = cls.td.name; cls.hosts = []
        cap = os.path.join(td, "s.jsonl"); gs.generate(cap, seed=1); cls.v2 = board_lines(BOARD, cap, td)
        if os.path.exists(ORIG):
            cls.v1 = board_lines(ORIG, os.path.join(ROOT, "data", "ecg_ppg2.jsonl"), td)
        else:
            cls.v1 = []
            for l in board_lines(BOARD, os.path.join(ROOT, "data", "ecg_ppg2.jsonl"), td):
                m = json.loads(l[4:]); [m.pop(k, None) for k in NEW_KEYS]
                if m["type"] == "hello": m = {"type": "hello", "version": 1, "note": "v1"}
                cls.v1.append("CT1 " + json.dumps(m, separators=(",", ":")))
    @classmethod
    def tearDownClass(cls):
        for h in cls.hosts: h.stop()
        cls.td.cleanup()
    def host(self, lines, name):
        f = os.path.join(self.td.name, name); write(f, lines); h = Host(f, self.td.name); self.hosts.append(h); h.wait_lines(len(lines)); return h, f

    def test_v1_log_still_renders(self):
        h, _ = self.host(self.v1, "v1.out"); d = h.data(); self.assertEqual(d["board_version"], 1); self.assertIsNone(d["model"])
        dom = h.dom(os.path.join(OUT, "dashboard_v1.png"))
        self.assertEqual((text_of(dom, "jserr") or "").strip(), ""); self.assertIn("board software v1", dom)
        self.assertEqual(text_of(dom, "actv").strip(), "—")
        self.assertRegex(dom, r'<svg[^>]*id="ecgsvg"[^>]*>\s*<[a-z]'); self.assertRegex(dom, r'<svg[^>]*id="ppgsvg"[^>]*>\s*<[a-z]')
        self.assertIn("<rect", re.search(r'<svg[^>]*id="tl"[^>]*>(.*?)</svg>', dom, re.S).group(1))
        self.assertNotIn("pi_site", json.dumps(d))
    def test_v2_foot_card_and_data_keys(self):
        h, _ = self.host(self.v2, "v2.out"); d = h.data()
        self.assertEqual(d["board_version"], 2); self.assertEqual(len(d["model"]["regions"]), 7)
        for k in ("hb", "now", "last_line_age", "model", "board_version"): self.assertIn(k, d)
        w = d["sessions"][0]["wins"][-1]; self.assertIn("area", w); self.assertIn("act", w)
        dom = h.dom(os.path.join(OUT, "dashboard_v2_light.png")); self.assertEqual((text_of(dom, "jserr") or "").strip(), "")
        for i in range(7): self.assertIsNotNone(fill_of(dom, i), i)
        self.assertEqual(text_of(dom, "fbadge").strip(), "Live"); self.assertIn("Sitting", text_of(dom, "actv"))
        self.assertIn("Inferred strain", dom); self.assertIn("next 2 min", text_of(dom, "fhor"))
    def test_live_update_changes_fills_and_shows_walking(self):
        i = next(n for n, l in enumerate(self.v2) if '"type":"window"' in l and '"k":10,' in l)      # stop after window k=10 (sitting)
        j = next(n for n, l in enumerate(self.v2) if '"type":"window"' in l and '"k":16,' in l)      # then add up to k=16 (walking)
        early = self.v2[:i + 1]; f = os.path.join(self.td.name, "live.out"); write(f, early); h = Host(f, self.td.name); self.hosts.append(h); h.wait_lines(len(early))
        a = h.dom(); self.assertIn("Sitting", text_of(a, "actv"))
        write(f, self.v2[i + 1:j + 1], "a"); h.wait_lines(j + 1); b = h.dom(os.path.join(OUT, "dashboard_v2_walking.png"))
        self.assertEqual((text_of(b, "jserr") or "").strip(), ""); self.assertIn("Walking", text_of(b, "actv"))
        self.assertNotEqual([fill_of(a, k) for k in range(7)], [fill_of(b, k) for k in range(7)])
    def test_stale_when_board_stops(self):
        k = next(n for n, l in enumerate(self.v2) if '"type":"window"' in l and '"k":16,' in l)
        lines = self.v2[:k + 1]; last = json.loads(lines[-1][4:]) if lines[-1].startswith("CT1 ") else None
        w_t1 = max(json.loads(l[4:])["t1"] for l in lines if '"type":"window"' in l)
        hb = {"type": "hb", "pkts": 99999, "rate": 49.87, "sec": w_t1 + 40.0, "k": 17}; lines.append("CT1 " + json.dumps(hb))
        h, _ = self.host(lines, "stale.out"); dom = h.dom(os.path.join(OUT, "dashboard_v2_stale.png"))
        self.assertEqual((text_of(dom, "jserr") or "").strip(), ""); self.assertIn("No new result for", text_of(dom, "fstate")); self.assertIn("last result at t =", text_of(dom, "fstate")); self.assertIn("40", text_of(dom, "fstate"))
        self.assertEqual(text_of(dom, "actv").strip(), "—"); self.assertIn('url(#footHatch)', fill_of(dom, 0))
    def test_poor_contact_text(self):
        pc = [l for l in self.v2 if '"type":"window"' in l]; idx = next(n for n, l in enumerate(self.v2) if '"type":"window"' in l and '"k":34,' in l)
        h, _ = self.host(self.v2[:idx + 1], "pc.out"); dom = h.dom(os.path.join(OUT, "dashboard_v2_poor_contact.png"))
        self.assertIn("PPG poor contact", dom.split('id="actr"', 1)[1][:300]); self.assertIn("Partial data", text_of(dom, "areaq"))

if __name__ == "__main__": unittest.main()
