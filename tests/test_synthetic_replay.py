"""Software tests on SYNTHETIC data (not observations of a real person).  Replays tests/gen_synthetic.py output through the board.
Run:  python3 -I -m unittest discover -s tests -p "test_*.py" -v      (from the repo root)"""
import json, math, os, subprocess, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import gen_synthetic as gs

BOARD = os.path.join(ROOT, "board", "cardiotwin_board.py")
ORIG = os.environ.get("ORIG_BOARD", "/tmp/orig_board.py")   # git show 340a124:board/cardiotwin_board.py > /tmp/orig_board.py
ALWAYS = {"type", "k", "t0", "t1", "ecg_ok", "ppg_ok", "usable", "reason", "I", "u"}
MEASURES = {"hr_ecg", "hr_ppg", "rmssd_ms", "pat_ms", "pi_pct", "beat_match", "ppg_dc", "ecg_sat"}

def jload(path):
    with open(path) as f: return json.load(f)
def jdump(obj, path):
    with open(path, "w") as f: json.dump(obj, f)
def jlines(path):
    with open(path) as f: return [json.loads(l) for l in f if l.strip()]

def replay(script, cap, td, extra=(), keep_state=False):
    cmd = [sys.executable, "-I", script, "--replay", cap, "--state", os.path.join(td, "st.json"), "--log", os.path.join(td, "log.jsonl")] + list(extra)
    if not keep_state: cmd.append("--reset")
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p, [json.loads(l[4:]) for l in p.stdout.splitlines() if l.startswith("CT1 ")]

class SyntheticReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory(); td = cls.td.name
        cls.cap = os.path.join(td, "sit_walk_sit.jsonl"); cls.segs, cls.total = gs.generate(cls.cap, seed=1)
        cls.proc, cls.msgs = replay(BOARD, cls.cap, td)
        cls.W = [m for m in cls.msgs if m["type"] == "window"]; cls.hello = [m for m in cls.msgs if m["type"] == "hello"]
    @classmethod
    def tearDownClass(cls): cls.td.cleanup()

    def seg(self, kind, nth=0): return [s for s in self.segs if s["kind"] == kind][nth]
    def inside(self, s, skip=0.0): return [w for w in self.W if w["t0"] >= s["t0"] + skip - 1e-9 and w["t1"] <= s["t1"] + 1e-9]

    def test_S6_exit_code_and_v1_keys(self):                  # T-S6
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr[-500:])
        self.assertGreaterEqual(len(self.W), 41)
        for w in self.W:
            self.assertTrue(ALWAYS <= w.keys(), (w["k"], ALWAYS - w.keys()))
            if w["reason"] != "ECG beats not reliable": self.assertTrue(MEASURES <= w.keys(), (w["k"], MEASURES - w.keys()))
        self.assertEqual(self.hello[0]["version"], 2); self.assertEqual(len(self.hello[0]["model"]["regions"]), 7)
    @unittest.skipUnless(os.path.exists(ORIG), "original board not found (set ORIG_BOARD, see README section 2)")
    def test_S6_v1_fields_identical_to_original(self):
        import replay_check as rc
        with tempfile.TemporaryDirectory() as td: _, mo = replay(ORIG, self.cap, td)
        self.assertEqual(rc.regression(mo, self.msgs, "synthetic"), [])
    def test_S1_sitting_segments(self):                       # T-S1
        sit = [w for n in range(3) for w in self.inside(self.seg("sit", n), 30)]
        self.assertGreaterEqual(len(sit), 12)
        self.assertEqual(sum(w["act"] == "walking" for w in sit), 0)
        self.assertGreaterEqual(sum(w["act"] == "sitting" for w in sit) / len(sit), 0.8)
    def test_S2_walking_detected(self):                       # T-S2
        s = self.seg("walk"); k_on = min(w["k"] for w in self.W if w["t1"] > s["t0"])
        first = min((w["k"] for w in self.W if w["k"] >= k_on and w["act"] == "walking"), default=None)
        self.assertIsNotNone(first); self.assertLessEqual(first - k_on, 3)
        after = [w for w in self.inside(s) if w["k"] >= first]; self.assertGreaterEqual(sum(w["act"] == "walking" for w in after) / len(after), 0.8)
    def test_S2_first_cadence_window_is_not_walking(self):    # F5: persistence required
        s = self.seg("walk"); k_on = min(w["k"] for w in self.W if w["t1"] > s["t0"])
        w = next(w for w in self.W if w["k"] == k_on); self.assertNotEqual(w["act"], "walking")
    def test_S3_leaves_walking(self):                         # T-S3
        s = self.seg("walk"); k_off = min(w["k"] for w in self.W if w["t1"] > s["t1"])
        first = min(w["k"] for w in self.W if w["k"] >= k_off and w["act"] != "walking"); self.assertLessEqual(first - k_off, 3)
        self.assertTrue(all(w["act"] != "walking" for w in self.W if w["t0"] >= s["t1"]))
    def test_S4_poor_contact(self):                           # T-S4
        pc = self.inside(self.seg("poor_contact")); self.assertGreaterEqual(len(pc), 3)
        for w in pc:
            self.assertEqual((w["act"], w["act_basis"], w["area_q"]), ("uncertain", "ecg_only", "partial"), w["k"])
            self.assertIn("contact", w["act_reason"]); self.assertIn("contact", w["area_reason"])
    def test_S5_area_rises_then_decays(self):                 # T-S5
        s = self.seg("walk"); ww = self.inside(s); mx = [max(w["area"]) for w in ww]
        self.assertTrue(any(mx[i] < mx[i + 1] < mx[i + 2] < mx[i + 3] for i in range(len(mx) - 3)), mx)   # rises over 3 consecutive steps
        self.assertTrue(any(w["area_elev"] for w in ww[-2:]), "no region elevated by the end of the walk")
        peak = max(mx); after = [w for w in self.W if w["t0"] >= s["t1"] + 30 and w["area"] is not None and w["area_q"] == "full"]
        self.assertLess(max(max(w["area"]) for w in after[-3:]), peak - 0.25)
    def test_no_elevated_region_while_sitting_first_segment(self):
        self.assertTrue(all(not w["area_elev"] for w in self.inside(self.seg("sit", 0), 0)))
    def test_new_field_types(self):
        for w in self.W:
            self.assertIn(w["act"], ("sitting", "walking", "uncertain")); self.assertIn(w["area_q"], ("full", "partial", "none"))
            self.assertLessEqual(len(w["act_reason"]), 160); self.assertLessEqual(len(w["strain_why"]), 160)
            self.assertEqual(w["area_h_s"], 120.0)
            if w["area"] is not None: self.assertEqual(len(w["area"]), 7); self.assertTrue(all(0 <= v <= 1 for v in w["area"]))
            self.assertTrue(w["strain"] is None or 0 <= w["strain"] <= 1)
    def test_log_has_new_fields(self):
        rows = jlines(os.path.join(self.td.name, "log.jsonl"))
        self.assertEqual(len(rows), len(self.W)); self.assertIn("act", rows[0]); self.assertIn("area_q", rows[0])
    def test_state_persists_ext_and_restart_loads(self):
        st = jload(os.path.join(self.td.name, "st.json"))
        self.assertIn("ext", st); self.assertEqual(len(st["ext"]["load"]), 7); self.assertIn("hr_ref", st["ext"])
        with tempfile.TemporaryDirectory() as td:                # old-format state (no ext) must still load
            jdump({k: st[k] for k in ("pat", "lnpi")}, os.path.join(td, "st.json"))
            p, m = replay(BOARD, self.cap, td, keep_state=True); self.assertEqual(p.returncode, 0, p.stderr[-300:]); self.assertNotIn("unreadable", p.stderr)
            self.assertIn("ext", jload(os.path.join(td, "st.json")))
        with tempfile.TemporaryDirectory() as td:                # restart with ext: the learned floor is reused
            jdump(st, os.path.join(td, "st.json")); p, m = replay(BOARD, self.cap, td, keep_state=True)
            self.assertEqual(p.returncode, 0); self.assertNotIn("unreadable", p.stderr)
    def test_rest_hr_option_and_calibration(self):
        with tempfile.TemporaryDirectory() as td:
            p, m = replay(BOARD, self.cap, td, extra=["--rest-hr", "72"]); self.assertEqual(p.returncode, 0, p.stderr[-300:])
            self.assertEqual(len([x for x in m if x["type"] == "window"]), len(self.W)); self.assertAlmostEqual([x for x in m if x["type"] == "window"][0]["hr_ref"], 72.0, delta=1.0)
            p, m = replay(BOARD, self.cap, td, extra=["--calibrate-rest", "60"]); self.assertEqual(p.returncode, 0, p.stderr[-300:])
            W = [x for x in m if x["type"] == "window"]
            self.assertTrue(all(w.get("calibrating") and w["strain"] is None for w in W[:3])); self.assertIsNotNone(W[6]["strain"])
            self.assertTrue(all(w["act"] != "walking" for w in W if w["t1"] <= 120))
            self.assertEqual(sum(w["act"] == "walking" for w in W if 130 <= w["t0"] <= 220) > 3, True)    # walking is still found after calibrating
            p, m = replay(BOARD, self.cap, td, extra=["--rest-hr", "5"]); self.assertNotEqual(p.returncode, 0); self.assertIn("option error", p.stderr)
    def test_bad_config_exits_before_capture(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = os.path.join(td, "bad.json"); jdump({"region_model": {"features": ["walk", "bogus"]}}, cfg)
            p, m = replay(BOARD, self.cap, td, extra=["--config", cfg]); self.assertNotEqual(p.returncode, 0)
            self.assertIn("unknown feature", p.stderr + p.stdout); self.assertEqual(m, [])

if __name__ == "__main__": unittest.main()
