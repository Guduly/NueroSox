#!/usr/bin/env python3
"""CardioTwin on-board pipeline for the SK-AM62B (Python standard library ONLY: no numpy/scipy).

Does everything on the board: read the AFE4950EVM FIFO packets, split ECG (stream A, ~500 Hz) / PPG (stream B, ~50 Hz),
filter, detect beats, compute per-window features, apply quality gates, update the per-person EMA baseline, and score the
twin intensity I.  Output is one JSON line per message ("CT1 {json}") on stdout and optionally appended to --log.
The host computer only displays these lines (see host_dashboard.py).

RESEARCH PROTOTYPE, NOT DIAGNOSTIC.  Stream roles (A=ECG, B=PPG), PPG polarity, the R-to-trough delay (PAT surrogate)
and the perfusion index are inferred and unvalidated.  Values are raw ADC counts / derived timings, not calibrated units.

Modes
  --replay capture.jsonl [--realtime]     process a saved capture (used for testing / offline demo)
  --live [--profile AFE4950_Tagged.cfg]   capture from the EVM on USB (uses afe4950_linux.py in the same folder;
                                           NOT yet run on real hardware)
"""
import argparse, importlib.util, json, math, os, queue, signal, sys, threading, time

# ----------------------------------------------------------------------------- parameters (from spec / assumptions)
WIN_S, HOP_S = 20.0, 10.0          # feature window and hop
LEAD_S, TAIL_S = 10.0, 5.0         # extra data before/after a window used only to settle the filters
B_PRIOR = (-2.5, 0.6, 0.9, 0.6)    # spec v2: fixed prior, not learned
ALPHA = 0.1                        # EMA rate (assumption)
SD_FLOOR = {"pat": 15.0, "lnpi": 0.10}
SAT_LEVEL = 2096000
DC_MIN = 3e5
ECG_DEC = 4                        # waveform decimation sent to the host (500 -> ~125 Hz)


# ----------------------------------------------------------------------------- tiny DSP library
def median(a):
    s = sorted(a); n = len(s)
    return float("nan") if n == 0 else (s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2]))

def percentile(a, q):
    s = sorted(a); n = len(s)
    if n == 0: return float("nan")
    k = (n - 1) * q / 100.0; f = int(math.floor(k)); c = min(f + 1, n - 1)
    return s[f] + (s[c] - s[f]) * (k - f)

def mean(a): return sum(a) / len(a) if a else float("nan")
def std(a):
    m = mean(a); return math.sqrt(sum((v - m) ** 2 for v in a) / len(a)) if a else float("nan")

def _norm(b0, b1, b2, a0, a1, a2): return (b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)

def notch(f0, fs, q=30.0):
    w = 2 * math.pi * f0 / fs; al = math.sin(w) / (2 * q); c = math.cos(w)
    return _norm(1, -2 * c, 1, 1 + al, -2 * c, 1 - al)

def lowpass(f0, fs):
    w = 2 * math.pi * f0 / fs; al = math.sin(w) / (2 * (1 / math.sqrt(2))); c = math.cos(w)
    return _norm((1 - c) / 2, 1 - c, (1 - c) / 2, 1 + al, -2 * c, 1 - al)

def highpass(f0, fs):
    w = 2 * math.pi * f0 / fs; al = math.sin(w) / (2 * (1 / math.sqrt(2))); c = math.cos(w)
    return _norm((1 + c) / 2, -(1 + c), (1 + c) / 2, 1 + al, -2 * c, 1 - al)

def _run(x, sec):
    b0, b1, b2, a1, a2 = sec; z1 = z2 = 0.0; y = [0.0] * len(x)
    for i, v in enumerate(x):                      # transposed direct form II
        o = b0 * v + z1; z1 = b1 * v - a1 * o + z2; z2 = b2 * v - a2 * o; y[i] = o
    return y

def sosfiltfilt(x, secs, pad):
    """Zero-phase filtering: odd-extend, forward, backward, trim."""
    n = len(x); pad = min(pad, n - 1)
    ext = [2 * x[0] - x[pad - k] for k in range(pad)] + list(x) + [2 * x[-1] - x[-2 - k] for k in range(pad)]
    for s in secs: ext = _run(ext, s)
    ext.reverse()
    for s in secs: ext = _run(ext, s)
    ext.reverse()
    return ext[pad:pad + n]

def local_maxima(y):
    return [i for i in range(1, len(y) - 1) if y[i] > y[i - 1] and y[i] >= y[i + 1]]

def prominence(y, i):
    lm = y[i]; j = i - 1
    while j >= 0 and y[j] <= y[i]:
        lm = min(lm, y[j]); j -= 1
    lmin = lm if j >= 0 else lm
    rm = y[i]; j = i + 1
    while j < len(y) and y[j] <= y[i]:
        rm = min(rm, y[j]); j += 1
    return y[i] - max(lmin, rm)

def pick_peaks(y, distance, min_height=None, min_prom=None):
    """Like scipy.find_peaks(distance, height, prominence): keep highest peaks, drop others within `distance`."""
    c = local_maxima(y)
    if min_height is not None: c = [i for i in c if y[i] >= min_height]
    if min_prom is not None: c = [i for i in c if prominence(y, i) >= min_prom]
    keep = []
    for i in sorted(c, key=lambda k: -y[k]):
        if all(abs(i - k) >= distance for k in keep): keep.append(i)
    return sorted(keep)

def detrend_linear(y):
    n = len(y); xm = (n - 1) / 2.0; ym = mean(y)
    sxx = sum((i - xm) ** 2 for i in range(n)); sxy = sum((i - xm) * (v - ym) for i, v in enumerate(y))
    s = sxy / sxx if sxx else 0.0
    return [v - (ym + s * (i - xm)) for i, v in enumerate(y)]


# ----------------------------------------------------------------------------- twin state (EMA baselines)
class EMA:
    def __init__(s, name): s.n, s.m, s.v, s.k, s.buf = name, 0.0, 0.0, 0, []
    def z(s, x):
        if s.k < 3: return None
        return max(-4.0, min(4.0, (x - s.m) / max(math.sqrt(s.v), SD_FLOOR[s.n])))
    def update(s, x):
        if s.k < 3:
            s.buf.append(x); s.k += 1
            if s.k == 3: s.m = mean(s.buf); s.v = std(s.buf) ** 2
            return
        d = x - s.m; s.m += ALPHA * d; s.v = (1 - ALPHA) * (s.v + ALPHA * d * d); s.k += 1
    def dump(s): return dict(m=s.m, v=s.v, k=s.k, buf=s.buf)
    def load(s, d): s.m, s.v, s.k, s.buf = d["m"], d["v"], d["k"], d["buf"]

class Twin:
    def __init__(s, state_path=None, reset=False):
        s.path = state_path; s.e = {"pat": EMA("pat"), "lnpi": EMA("lnpi")}
        if s.path and not reset and os.path.exists(s.path):
            try:
                d = json.load(open(s.path))
                for k in s.e: s.e[k].load(d[k])
            except Exception as ex:
                print("state file unreadable, starting fresh:", ex, file=sys.stderr)
    def save(s):
        if not s.path: return
        tmp = s.path + ".tmp"                               # atomic: a power cut leaves the old file intact
        with open(tmp, "w") as f:
            json.dump({k: v.dump() for k, v in s.e.items()}, f); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, s.path)
    def score(s, w):
        w["I"] = None; w["u"] = None
        if not w.get("usable"): return
        vals = {"pat": w["pat_ms"], "lnpi": math.log(w["pi_pct"])}
        z = {k: s.e[k].z(v) for k, v in vals.items()}
        if z["pat"] is None or z["lnpi"] is None:
            w["reason"] = "baseline warm-up (first 3 usable windows)"; w["warmup"] = True
        else:
            u1, u2, u3 = abs(z["pat"]), -z["lnpi"], 0.0      # LF/HF needs >= 60 s of beats: not available per window
            eta = B_PRIOR[0] + B_PRIOR[1] * u1 + B_PRIOR[2] * u2 + B_PRIOR[3] * u3
            w["u"] = [u1, u2, u3]; w["I"] = 1 / (1 + math.exp(-eta)); w["lfhf_used"] = False
        for k, v in vals.items(): s.e[k].update(v)           # baseline updated AFTER scoring
        s.save()


# ----------------------------------------------------------------------------- pipeline
class Pipeline:
    def __init__(s, emit, twin):
        s.emit, s.twin = emit, twin
        s.A, s.B = [], []                    # ECG / PPG samples still in memory (older ones are trimmed)
        s.offA = s.offB = 0                  # number of samples already trimmed from the front
        s.n_pkt, s.t_first, s.t_last = 0, None, None
        s.k = 0                               # next window index (t0 = k * HOP_S)
        s.chunk_std = {}                      # 5 s chunk index -> std of raw ECG (artifact reference)
        s.nextA = s.nextB = 0                 # absolute sample index where the next waveform chunk starts

    def rate(s):
        if s.n_pkt >= 100 and s.t_last > s.t_first: return (s.n_pkt - 1) / (s.t_last - s.t_first)
        return 49.85
    def getA(s, i0, i1): return s.A[max(0, i0 - s.offA):max(0, i1 - s.offA)]
    def getB(s, i0, i1): return s.B[max(0, i0 - s.offB):max(0, i1 - s.offB)]
    def add_packet(s, words, host_time):
        if len(words) != 11: return
        v = [w - (1 << 24) if w >= (1 << 23) else w for w in words]
        s.A.extend(v[:6] + v[7:]); s.B.append(v[6])
        s.n_pkt += 1; s.t_last = host_time
        if s.n_pkt % 250 == 0:                                   # heartbeat every ~5 s: proves the EVM is still streaming
            s.emit(dict(type="hb", pkts=s.n_pkt, rate=round(s.rate(), 3), sec=round((len(s.B) + s.offB) / s.rate(), 1), k=s.k))
        if s.t_first is None: s.t_first = host_time
        s.try_windows(final=False)

    def try_windows(s, final):
        pr = s.rate(); dur = (len(s.B) + s.offB) / pr
        while True:
            t0 = s.k * HOP_S
            need = t0 + WIN_S + (0 if final else TAIL_S)
            if dur < need - 1e-9 and not (final and dur >= t0 + WIN_S - 1e-9): return
            if final and dur < t0 + WIN_S - 1e-9: return
            s.process_window(t0, pr, dur); s.k += 1
            keep = max(0.0, s.k * HOP_S - LEAD_S - 6.0)        # bounded memory for continuous runs
            for name, off, rate in (("A", "offA", pr * 10), ("B", "offB", pr)):
                drop = int(keep * rate) - getattr(s, off)
                if drop > 0:
                    del getattr(s, name)[:drop]; setattr(s, off, getattr(s, off) + drop)
            while len(s.chunk_std) > 360: s.chunk_std.pop(min(s.chunk_std))

    def process_window(s, t0, pr, dur):
        fb, fa = pr, pr * 10
        t1 = t0 + WIN_S
        g0, g1 = max(0.0, t0 - LEAD_S), min(dur, t1 + TAIL_S)
        ia0, ia1 = int(g0 * fa), int(g1 * fa); ib0, ib1 = int(g0 * fb), int(g1 * fb)
        Araw, Braw = s.getA(ia0, ia1), s.getB(ib0, ib1)
        for c in range(int(g0 // 5), int(g1 // 5)):          # running artifact reference (median of 5 s ECG chunk std)
            if c not in s.chunk_std and (c + 1) * 5 <= dur:
                s.chunk_std[c] = std(s.getA(int(c * 5 * fa), int((c + 1) * 5 * fa)))
        a_med = median(list(s.chunk_std.values()))
        # ---- ECG
        m = median(Araw); x = [v - m for v in Araw]
        notches = [notch(f, fa) for f in (60, 120, 180)]
        disp = sosfiltfilt(x, notches + [highpass(0.5, fa), lowpass(40, fa)], int(fa))
        det = sosfiltfilt(x, notches + [highpass(5, fa), lowpass(20, fa)], int(fa))
        thr = 0.4 * percentile(det, 99)
        pk = pick_peaks(det, int(0.3 * fa), min_height=thr)
        R = []
        for k in pk:
            lo, hi = max(0, k - int(0.04 * fa)), min(len(disp), k + int(0.04 * fa))
            R.append(max(range(lo, hi), key=lambda i: abs(disp[i])))
        tR = [g0 + i / fa for i in R]
        # ---- PPG
        fB = sosfiltfilt(detrend_linear(Braw), [highpass(0.7, fb), lowpass(2.5, fb)], int(fb))
        sdB = std(fB)
        tP = [g0 + i / fb for i in pick_peaks(fB, int(0.4 * fb), min_prom=0.3 * sdB)]
        tT = [g0 + i / fb for i in pick_peaks([-v for v in fB], int(0.4 * fb), min_prom=0.3 * sdB)]
        # ---- window features
        w = dict(type="window", k=int(round(t0 / HOP_S)), t0=t0, t1=t1)
        r = [t for t in tR if t0 <= t < t1]; rr = [b - a for a, b in zip(r, r[1:])]; okr = [d for d in rr if 0.35 < d < 1.6]
        if len(r) < 8 or not rr or len(okr) / len(rr) < 0.9:
            w.update(ecg_ok=False, ppg_ok=False, usable=False, reason="ECG beats not reliable")
        else:
            hr_e = 60 / median(okr)
            rmssd = math.sqrt(mean([(b - a) ** 2 for a, b in zip(okr, okr[1:])])) * 1e3 if len(okr) > 1 else float("nan")
            seg = s.getA(int(t0 * fa), int(t1 * fa))
            sat = sum(1 for v in seg if abs(v) >= SAT_LEVEL) / len(seg); a_std = std(seg)
            ecg_ok = sat < 0.05 and a_std < 3 * a_med
            p = [t for t in tP if t0 <= t < t1]; ibi = [b - a for a, b in zip(p, p[1:])]
            hr_p = 60 / median(ibi) if len(ibi) >= 5 else None
            d = []
            for tr in r:
                c = [t for t in tT if tr + 0.15 < t < tr + 1.0]
                if c: d.append(c[0] - tr)
            band = [v for v in d if 0.25 < v < 0.75]
            match = len(band) / len(r); pat = median(band) * 1e3 if len(band) >= 5 else None
            sg = fB[int((t0 - g0) * fb):int((t1 - g0) * fb)]; dc = mean(s.getB(int(t0 * fb), int(t1 * fb)))
            pi = 100 * (percentile(sg, 95) - percentile(sg, 5)) / abs(dc) if abs(dc) > DC_MIN else None
            if pi is not None and pi <= 0: pi = None
            ppg_ok = bool(pat is not None and pi is not None and match >= 0.8 and hr_p is not None and abs(hr_p - hr_e) <= 5)
            why = []
            if not ecg_ok: why.append("ECG artifact/saturation")
            if abs(dc) <= DC_MIN: why.append("PPG signal level too low (poor contact)")
            elif match < 0.8: why.append("PPG beats do not match ECG beats")
            elif hr_p is None: why.append("too few PPG beats")
            elif abs(hr_p - hr_e) > 5: why.append("PPG rate disagrees with ECG")
            elif pi is None: why.append("PPG amplitude not measurable")
            if not why and not (ecg_ok and ppg_ok): why.append("PPG quality check failed")
            w.update(hr_ecg=hr_e, hr_ppg=hr_p, rmssd_ms=rmssd, pat_ms=pat, pi_pct=pi, beat_match=match, ppg_dc=dc, ecg_sat=sat,
                     ecg_ok=bool(ecg_ok), ppg_ok=ppg_ok, usable=bool(ecg_ok and ppg_ok), reason="; ".join(why) if why else "ok")
        s.twin.score(w)
        s.emit(w)
        # ---- waveform chunk for the host: interior 10 s (first window also sends the first 10 s)
        c0 = 0.0 if w["k"] == 0 else t0 + HOP_S
        c1 = t0 + 2 * HOP_S if w["k"] > 0 else HOP_S
        if w["k"] == 0:
            s.emit_wave(0.0, HOP_S, disp, fB, ia0, ib0, fa, fb, tR, tT, tP)
        s.emit_wave(t0 + HOP_S, t0 + 2 * HOP_S, disp, fB, ia0, ib0, fa, fb, tR, tT, tP)

    def emit_wave(s, c0, c1, disp, fB, ia0, ib0, fa, fb, tR, tT, tP):
        # each chunk starts exactly where the previous one ended (absolute sample index), so the host can
        # concatenate them without the slow drift that per-chunk rounding would cause
        A0, A1 = s.nextA, int(round(c1 * fa)); A1 = A0 + max(0, (A1 - A0) // ECG_DEC) * ECG_DEC
        B0, B1 = s.nextB, int(round(c1 * fb))
        a0, a1 = A0 - ia0, min(A1 - ia0, len(disp)); b0, b1 = B0 - ib0, min(B1 - ib0, len(fB))
        if a0 < 0 or b0 < 0 or a1 <= a0 or b1 <= b0: return
        ecg = disp[a0:a1:ECG_DEC]; ppg = fB[b0:b1]
        s.nextA = A0 + len(ecg) * ECG_DEC; s.nextB = B0 + len(ppg)
        s.emit(dict(type="wave", t0=round(B0 / fb, 4), fsA=fa / ECG_DEC, fsB=fb, ecg=[int(round(v)) for v in ecg],
                    ppg=[int(round(v)) for v in ppg],
                    R=[round(t, 3) for t in tR if c0 <= t < c1], Ptr=[round(t, 3) for t in tT if c0 <= t < c1],
                    Ppk=[round(t, 3) for t in tP if c0 <= t < c1]))

    def finish(s): s.try_windows(final=True)


# ----------------------------------------------------------------------------- IO
class Output:
    """Printing to a 115200-baud console is slow (a waveform line takes ~1 s).  Do it in its own thread so the
    capture loop never stops reading the EVM while the console is busy."""
    def __init__(s, log_path):
        s.q = queue.Queue(); s.log = open(log_path, "a", buffering=1) if log_path else None
        s.t = threading.Thread(target=s.run, daemon=True); s.t.start()
    def emit(s, obj): s.q.put(json.dumps(obj, separators=(",", ":")))
    def run(s):
        while True:
            line = s.q.get()
            if line is None: return
            try:
                sys.stdout.write("CT1 " + line + "\n"); sys.stdout.flush()
            except Exception: pass                      # a broken console must not stop the log or the capture
            if s.log and line.startswith('{"type":"window"'):
                s.log.write(line + "\n"); s.log.flush(); os.fsync(s.log.fileno())
    def close(s): s.q.put(None); s.t.join()

def replay(path, pipe, realtime, speed=1.0):
    last = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.startswith("{"): continue
            p = json.loads(line)
            if realtime and last is not None: time.sleep(max(0.0, (p["host_time"] - last) / speed))
            last = p["host_time"]; pipe.add_packet(p["raw_words"], p["host_time"])
    pipe.finish()

def live(args, pipe):
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("afe", os.path.join(here, "afe4950_linux.py"))
    afe = importlib.util.module_from_spec(spec); spec.loader.exec_module(afe)
    ports = [afe.SerialPort(p) for p in args.ports]
    try:
        roles = {}
        for pt in ports: roles[pt.query(b"C:CDC?")] = pt
        control, capture = roles.get(b"REG_EDIT"), roles.get(b"CAPTURE")
        if not control or not capture: raise RuntimeError("could not identify EVM ports (REG_EDIT / CAPTURE)")
        if args.profile: afe.load_profile(args.profile, control)
        capture.send(b"C:CAP_START" + b"00" + b"00000000")
        raw = open(args.raw_out, "a", buffering=1) if args.raw_out else None   # raw packets, replayable later with --replay
        buf = b""; end = time.monotonic() + args.seconds if args.seconds else None
        try:
            while end is None or time.monotonic() < end:
                try: chunk = capture.read(0.5)
                except OSError as ex:
                    print("EVM read failed (USB unplugged?):", ex, file=sys.stderr); break
                if not chunk: continue
                packets, buf = afe.parse_frames(buf + chunk)
                for p in packets:
                    if raw: raw.write(json.dumps({"fifo_pointer": p.get("fifo_pointer"), "raw_words": p["raw_words"], "host_time": time.time()}) + "\n")
                    pipe.add_packet(p["raw_words"], time.monotonic())   # monotonic: the board wall clock is not reliable
        finally:
            capture.send(b"C:CAP_STOP")
            if raw: raw.close()
        pipe.finish()
    finally:
        for pt in ports: pt.close()

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--replay"); g.add_argument("--live", action="store_true")
    ap.add_argument("--realtime", action="store_true"); ap.add_argument("--speed", type=float, default=1.0, help="with --realtime: playback speed factor (5 = five times faster)"); ap.add_argument("--profile")
    ap.add_argument("--ports", nargs=2, default=["/dev/ttyACM0", "/dev/ttyACM1"])
    ap.add_argument("--seconds", type=float, default=0, help="live: stop after N s (0 = run until Ctrl+C)")
    ap.add_argument("--state", default="cardiotwin_state.json"); ap.add_argument("--reset", action="store_true")
    ap.add_argument("--log", default="cardiotwin_windows.jsonl")
    ap.add_argument("--raw-out", help="live: also save the raw EVM packets to this .jsonl file (replayable with --replay)")
    ap.add_argument("--start-delay", type=float, default=0, help="wait N s before starting (time to close picocom and open the host display)")
    a = ap.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))   # kill/systemd stop -> stop capture cleanly
    time.sleep(a.start_delay)
    out = Output(a.log); emit = out.emit
    pipe = Pipeline(emit, Twin(a.state, a.reset))
    emit(dict(type="hello", version=1, note="research prototype, not diagnostic; stream roles/units inferred"))
    try:
        replay(a.replay, pipe, a.realtime, a.speed) if a.replay else live(a, pipe)
    except KeyboardInterrupt:
        pipe.finish()
    finally:
        out.close()

if __name__ == "__main__":
    main()
