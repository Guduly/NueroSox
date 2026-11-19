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
# REGIONS is only the fallback name list for a v1 stream (no "model" in the hello message).  The old static PI_SITE prior split is
# no longer shown anywhere: region values now come from the board (activity_model.py), per window.

lock = threading.Lock()
wins, chunks, status = [], [], {"lines": 0, "last": None, "board_note": "", "board_version": 1, "model": None, "ver": 0, "gaps": 0}

def ingest(line):
    i = line.find("CT1 ")
    if i < 0: return
    try: m = json.loads(line[i + 4:])
    except Exception: return                      # partial/garbled line on a serial console: skip it
    with lock:
        status["lines"] += 1; status["last"] = time.time()
        t = m.get("type")
        if t == "hello":
            status["board_note"] = m.get("note", ""); status["board_version"] = m.get("version", 1); status["model"] = m.get("model")
            wins.clear(); chunks.clear(); status.pop("hb", None)
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

def meta():
    """Display-only facts for the page: board software version, the board's active mapping, host clock and how old the last line is."""
    with lock: last, hb, ver, model = status["last"], status.get("hb"), status["board_version"], status["model"]
    now = time.time()
    return dict(board_version=ver, model=model, hb=hb, now=now, last_line_age=None if last is None else round(now - last, 1))

def build():
    with lock:
        W, C = list(wins), list(chunks)
    names = [r["name"] for r in status["model"]["regions"]] if status["model"] else REGIONS
    if not C: return dict(sessions=[], b=[-2.5, .6, .9, .6], regions=names, alpha=.1)
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
    return dict(sessions=[s], b=[-2.5, .6, .9, .6], regions=names, alpha=.1)

def _must_replace(h, old, new):
    """str.replace that insists on exactly one match: a template edit that changes an anchor must fail loudly, not silently drop a feature."""
    n = h.count(old)
    if n != 1: raise RuntimeError("dashboard template anchor matched %d times (expected 1): %r" % (n, old[:90]))
    return h.replace(old, new)

def _drop_lines(h, marker):
    lines = h.split("\n"); keep = [l for l in lines if marker not in l]
    if len(keep) == len(lines): raise RuntimeError("dashboard template line not found: %r" % marker)
    return "\n".join(keep)

def page():
    h = open(os.path.join(HERE, "dash_template.html"), encoding="utf-8").read()
    # the template polls /data.json itself; the host only seeds an empty data set (region names for a v1 stream)
    return _must_replace(h, "const D=/*DATA*/null;", 'const D={sessions:[],b:[-2.5,.6,.9,.6],regions:%s,alpha:.1};' % json.dumps(REGIONS))

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith("/status.json"):
            body = json.dumps(dict(dict(ver=status["ver"], lines=status["lines"]), **meta())).encode(); ct = "application/json"
        elif self.path.startswith("/data.json"):
            d = build(); d["ver"] = status["ver"]; d["lines"] = status["lines"]; d["gaps"] = status["gaps"]; d.update(meta())
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
