#!/usr/bin/env python3
"""Regression + observation checks on the real recordings (laptop; stdlib only).

  python3 -I tests/replay_check.py --orig ../src/neurosox_dev/board/cardiotwin_board.py --data data [--regression-only]

R1 regression : every v1 key of every window (and every wave message) must equal the ORIGINAL pipeline's, floats within 1e-9.
Sanity targets: behaviour checks on the new fields (NOT validation).  Observations: a per-window table written to
tests/out/observations.md.  Timing: ms per window, original vs new.
"""
import argparse, json, math, os, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
RECS = ["ecg_ppg1", "ecg_ppg2", "ecg_ppg3", "achilles1", "stress"]

def run_board(script, capture, extra=()):
    """Replay `capture` through a board script with a throw-away state/log; return (messages, seconds)."""
    with tempfile.TemporaryDirectory() as td:
        cmd = [sys.executable, "-I", script, "--replay", capture, "--reset", "--state", os.path.join(td, "st.json"),
               "--log", os.path.join(td, "log.jsonl")] + list(extra)
        t = time.perf_counter(); p = subprocess.run(cmd, capture_output=True, text=True); dt = time.perf_counter() - t
        if p.returncode != 0: raise RuntimeError("board failed (%s): %s" % (script, p.stderr[-800:]))
    msgs = [json.loads(l[4:]) for l in p.stdout.splitlines() if l.startswith("CT1 ")]
    return msgs, dt

def same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        if a is None or b is None: return a is b
        if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b): return True
        return abs(a - b) <= 1e-9
    if isinstance(a, dict) and isinstance(b, dict): return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list): return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b

def regression(orig, new, name):
    """Return list of problems (empty = identical on all v1 keys)."""
    bad = []
    for typ in ("window", "wave", "hb"):
        o = [m for m in orig if m.get("type") == typ]; n = [m for m in new if m.get("type") == typ]
        if len(o) != len(n): bad.append("%s: %d %s messages in original, %d in new" % (name, len(o), typ, len(n))); continue
        for i, (a, b) in enumerate(zip(o, n)):
            for k in a:
                if k not in b: bad.append("%s %s#%d: key %r missing in new" % (name, typ, i, k))
                elif not same(a[k], b[k]): bad.append("%s %s#%d: key %r differs: %r vs %r" % (name, typ, i, k, str(a[k])[:60], str(b[k])[:60]))
    return bad

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--orig", required=True); ap.add_argument("--data", required=True)
    ap.add_argument("--regression-only", action="store_true"); ap.add_argument("--out", default=os.path.join(HERE, "out"))
    a = ap.parse_args(); a.orig = os.path.abspath(a.orig); new = os.path.join(ROOT, "board", "cardiotwin_board.py")
    fails = []; res = {}
    for r in RECS:
        cap = os.path.abspath(os.path.join(a.data, r + ".jsonl"))
        mo, to = run_board(a.orig, cap); mn, tn = run_board(new, cap)
        res[r] = (mo, mn, to, tn)
        bad = regression(mo, mn, r); fails += bad
        nw = sum(1 for m in mo if m.get("type") == "window")
        print("%-10s regression %s  (%d windows; orig %.2fs, new %.2fs)" % (r, "OK" if not bad else "FAIL", nw, to, tn))
        for b in bad[:5]: print("    ", b)
    if fails: print("REGRESSION FAILED: %d differences" % len(fails)); return 1
    print("REGRESSION PASSED: all v1 keys identical on %d recordings" % len(RECS))
    if a.regression_only: return 0
    sys.path.insert(0, HERE); import sanity_report
    return sanity_report.run(res, a.out)

if __name__ == "__main__":
    sys.exit(main())
