#!/usr/bin/env python3
"""CardioTwin host display (Python standard library only).  NO signal processing happens here.

Reads the "CT1 {json}" lines that cardiotwin_board.py prints on the AM62B and serves a live dashboard at
http://127.0.0.1:8000 .  Sources (pick one):
  --serial /dev/ttyUSB0   the board's debug console (115200 baud) -- no network needed
  --file out.txt          a text file that is being appended to (or a finished one)
  --stdin                 lines piped in
All received lines are also appended to --save (default host_received.jsonl) so nothing is lost.
Research prototype, not diagnostic.
"""
import argparse, json, os, sys, threading, time, http.server

HERE = os.path.dirname(os.path.abspath(__file__))
KEEP_S = 900.0                      # rolling window kept (default 15 min; change with --keep SECONDS)
REGIONS = ['heel', 'sole/arch', 'forefoot', 'dorsum/midfoot', 'medial', 'lateral', 'ankle/rearfoot']
PI_SITE = [0.30, 0.10, 0.05, 0.05, 0.10, 0.10, 0.30]      # placeholders (not from the spec)

lock = threading.Lock()
wins, chunks, status = [], [], {"lines": 0, "last": None, "board_note": "", "ver": 0, "gaps": 0}

def ingest(line):
    i = line.find("CT1 ")
    if i < 0: return
    try: m = json.loads(line[i + 4:])
    except Exception: return                      # partial/garbled line on a serial console: skip it
    with lock:
        status["lines"] += 1; status["last"] = time.time()
        t = m.get("type")
        if t == "hello": status["board_note"] = m.get("note", ""); wins.clear(); chunks.clear(); status.pop("hb", None)
        elif t == "hb": status["hb"] = m
        elif t == "window": wins.append(m)
        elif t == "wave":
            if chunks:
                prev = chunks[-1]
                if abs(m["t0"] - (prev["t0"] + len(prev["ppg"]) / prev["fsB"])) > 0.5:   # lost/garbled line: resync
                    chunks.clear(); status["gaps"] += 1
            chunks.append(m)
        del wins[:-int(KEEP_S / 10 + 12)]; del chunks[:-int(KEEP_S / 10 + 6)]
        status["ver"] += 1

def build():
    with lock:
        W, C = list(wins), list(chunks)
    if not C: return dict(sessions=[], b=[-2.5, .6, .9, .6], regions=REGIONS, pi_site=PI_SITE, alpha=.1)
    end = C[-1]["t0"] + len(C[-1]["ppg"]) / C[-1]["fsB"]
    toff = max(0.0, end - KEEP_S); toff = (toff // 10) * 10
    C = [c for c in C if c["t0"] >= toff - 0.5]
    toff = C[0]["t0"]
    ecg, ppg, R, Pk, Tr = [], [], [], [], []
    for c in C:
        ecg += c["ecg"]; ppg += c["ppg"]
        R += [t - toff for t in c["R"]]; Pk += [t - toff for t in c["Ppk"]]; Tr += [t - toff for t in c["Ptr"]]
    fsA, fsB = C[0]["fsA"], C[0]["fsB"]
    out = []
    for w in W:
        if w["t0"] < toff: continue
        w = dict(w); w["t0"] -= toff; w["t1"] -= toff; w["site"] = "live (AM62B)"; w.setdefault("z", None); out.append(w)
    s = dict(name="Live from AM62B", base=toff, wins=out, lfhf_session=None, dur=len(ppg) / fsB, fsA=fsA, fsB=fsB,
             ecg=ecg, ppg=ppg, R=R, Ppk=Pk, Ptr=Tr)
    return dict(sessions=[s], b=[-2.5, .6, .9, .6], regions=REGIONS, pi_site=PI_SITE, alpha=.1)

POLL = """
let _ver=-1, paused=false;
function say(d,extra){$("livestat").textContent=(d.hb?"board: "+d.hb.pkts+" packets, "+d.hb.sec+" s captured, window "+d.hb.k+" · "+d.lines+" lines":"live · "+d.lines+" lines")+(extra||"")}
$("pausebtn").onclick=()=>{paused=!paused;$("pausebtn").textContent=paused?"Resume":"Pause";if(!paused)_ver=-1;poll()};
async function poll(){try{
  if(paused){const r=await fetch("/status.json?"+Date.now());const d=await r.json();say(d," · PAUSED (data still arriving)");return}
  const r=await fetch("/data.json?"+Date.now());const d=await r.json();
  if(d.ver!==_ver){_ver=d.ver;const lastSel=S.length?sel.w===S[0].wins.length-1:true;
   S.length=0;d.sessions.forEach(s=>S.push(s));si=0;
   if(S.length){const n=S[0].wins.length;sel={s:0,w:(lastSel||sel.w>=n)?Math.max(0,n-1):sel.w};tv=Math.max(0,Math.floor(S[0].dur-vlen))}
   say(d);
   if(S.length){agree();render()}}}catch(e){$("livestat").textContent="host server not reachable"}}
setInterval(poll,3000);poll();
"""

def page():
    h = open(os.path.join(HERE, "dash_template.html"), encoding="utf-8").read()
    h = h.replace("const D=/*DATA*/null;", 'const D={sessions:[],b:[-2.5,.6,.9,.6],regions:%s,pi_site:%s,alpha:.1};' % (json.dumps(REGIONS), json.dumps(PI_SITE)))
    h = "\n".join(l for l in h.split("\n") if "Watch readings noted" not in l and "Lower-calf recording:" not in l)
    h = h.replace('<div class="tip" id="tip"></div>', '<div class="tip" id="tip"></div><div id="livestat" style="position:fixed;right:10px;top:8px;font:12px sans-serif;color:#777">starting…</div>', 1)
    # the template renders immediately at load: guard for "no data yet"
    h = h.replace("wgrid();agree();render();\n</script>", "wgrid();\n" + POLL + "</script>")
    h = h.replace('<label>Length <select id="vlen">', '<button id="pausebtn" type="button" style="font:inherit;padding:3px 12px;margin-right:10px;border:1px solid #888;border-radius:6px;background:transparent;color:inherit;cursor:pointer">Pause</button> <label>Length <select id="vlen">', 1)
    # show true capture time (the host keeps a rolling window and re-bases it; S[i].base is the offset)
    h = h.replace('`${f1(w.t0,0)}–${f1(w.t1,0)} s`,"",S[sel.s].name', '`${f1(w.t0+(S[sel.s].base||0),0)}–${f1(w.t1+(S[sel.s].base||0),0)} s`,"",S[sel.s].name', 1)
    h = h.replace('for(let t=Math.ceil(t0/step)*step;t<=t1;t+=step){const tx=el("text",{x:X(t),y:H-6,"text-anchor":"middle"},ax);tx.textContent=t+" s"}',
                  'const bb=S[si].base||0;for(let ta=Math.ceil((t0+bb)/step)*step;ta<=t1+bb;ta+=step){const t=ta-bb;const tx=el("text",{x:X(t),y:H-6,"text-anchor":"middle"},ax);tx.textContent=ta+" s"}', 1)
    h = h.replace('`${f1(t0)}–${f1(t1)} s of ${f1(s.dur,0)} s`', '`${f1(t0+(s.base||0))}–${f1(t1+(s.base||0))} s of ${f1(s.dur+(s.base||0),0)} s`', 1)
    h = h.replace('`${s.name} · ${f1(w.t0,0)}–${f1(w.t1,0)} s · `', '`${s.name} · ${f1(w.t0+(s.base||0),0)}–${f1(w.t1+(s.base||0),0)} s · `', 1)
    h = h.replace('[s.name,f1(w.t0,0),f1(w.hr_ecg)', '[s.name,f1(w.t0+(s.base||0),0),f1(w.hr_ecg)', 1)
    # robust vertical scale: ignore the most extreme 0.5 % of samples so a single artifact cannot flatten the trace
    h = h.replace('let lo=Infinity,hi=-Infinity;for(let i=i0;i<=i1;i++){if(y[i]<lo)lo=y[i];if(y[i]>hi)hi=y[i]}',
                  'let lo=Infinity,hi=-Infinity;{const v=y.slice(i0,i1+1).sort((a,b)=>a-b);if(v.length){lo=v[Math.floor(v.length*.002)];hi=v[Math.ceil(v.length*.998)-1]}}', 1)
    h = h.replace('let d="";for(let i=i0;i<=i1;i++){d+=(i===i0?"M":"L")+X(i/fs).toFixed(1)+" "+Y(y[i]).toFixed(1)}',
                  'const Yc=v=>Y(Math.max(lo,Math.min(hi,v)));let d="";for(let i=i0;i<=i1;i++){d+=(i===i0?"M":"L")+X(i/fs).toFixed(1)+" "+Yc(y[i]).toFixed(1)}', 1)
    i = h.find("marks.forEach(")                       # keep beat markers inside the plot when a spike is clipped
    if i >= 0:
        j = h.find("\n", i); h = h[:i] + h[i:j].replace("Y(yy)", "Yc(yy)") + h[j:]
    for fn in ("render", "waves", "timeline"):
        h = h.replace("function %s(){" % fn, "function %s(){if(!S.length)return;" % fn, 1)
    return h

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith("/status.json"):
            body = json.dumps(dict(ver=status["ver"], lines=status["lines"], hb=status.get("hb"))).encode(); ct = "application/json"
        elif self.path.startswith("/data.json"):
            d = build(); d["ver"] = status["ver"]; d["lines"] = status["lines"]; d["gaps"] = status["gaps"]; d["hb"] = status.get("hb")
            body = json.dumps(d).encode(); ct = "application/json"
        else: body = page().encode(); ct = "text/html; charset=utf-8"
        self.send_response(200); self.send_header("Content-Type", ct); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

def reader(args):
    save = open(args.save, "a", buffering=1)
    def feed(line):
        if "CT1 " in line: save.write(line.rstrip("\r\n") + "\n")
        ingest(line)
    if args.serial:
        import termios, tty
        fd = os.open(args.serial, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)   # never block waiting for modem carrier
        tty.setraw(fd); a = termios.tcgetattr(fd)
        a[2] |= termios.CLOCAL | termios.CREAD
        a[4] = a[5] = termios.B115200; termios.tcsetattr(fd, termios.TCSANOW, a)
        import fcntl; fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
        buf = b""
        while True:
            d = os.read(fd, 4096)
            if not d: time.sleep(.05); continue
            buf += d
            while b"\n" in buf:
                l, buf = buf.split(b"\n", 1); feed(l.decode("utf-8", "replace"))
    elif args.file:
        # Follow a growing text file byte-exactly (picocom writes it in blocks, so lines arrive in pieces).
        # Survives: file not created yet, file emptied (: > pico.log), file replaced.
        pos, buf, ino = 0, b"", None
        while True:
            try: st = os.stat(args.file)
            except FileNotFoundError: time.sleep(.3); continue
            if ino is None: ino = st.st_ino
            if st.st_ino != ino or st.st_size < pos: pos, buf, ino = 0, b"", st.st_ino
            if st.st_size > pos:
                with open(args.file, "rb") as f:
                    f.seek(pos); chunk = f.read(1 << 20)
                pos += len(chunk); buf += chunk
                *lines, buf = buf.split(b"\n")
                for l in lines: feed(l.decode("utf-8", "replace").rstrip("\r") + "\n")
            else: time.sleep(.2)
    else:
        for l in sys.stdin: feed(l)

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--serial"); g.add_argument("--file"); g.add_argument("--stdin", action="store_true")
    ap.add_argument("--port", type=int, default=8000); ap.add_argument("--save", default="host_received.jsonl")
    ap.add_argument("--keep", type=float, default=900, help="seconds of history kept in the dashboard (default 900)")
    a = ap.parse_args()
    global KEEP_S; KEEP_S = a.keep
    threading.Thread(target=reader, args=(a,), daemon=True).start()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print(f"dashboard: http://127.0.0.1:{a.port}   (Ctrl+C to stop)", flush=True)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass

if __name__ == "__main__": main()
