#!/usr/bin/env python3
"""Digital-twin feature pipeline for AFE4950 ECG (stream A) + PPG (stream B) captures.

Research prototype - NOT diagnostic.  Signal roles (A=ECG, B=PPG), PAT polarity and units are inferred, not verified.
Reads afe4950 JSONL captures, computes per-window features, quality gates, a per-person EMA baseline, z-scores,
and the intensity I = sigmoid(b0 + b1*u1 + b2*u2 + b3*u3) from the project spec (fixed prior, no learning).
Usage: python3 twin.py out.json name1=file1.jsonl[:site] name2=file2.jsonl[:site] ...
"""
import importlib.util, json, math, os, sys
import numpy as np
from scipy import signal

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("ep", os.path.join(HERE, "afe_ecg_ppg.py"))
ep = importlib.util.module_from_spec(spec); spec.loader.exec_module(ep)

WIN_S, HOP_S = 20.0, 10.0
B_PRIOR = (-2.5, 0.6, 0.9, 0.6)          # spec v2: fixed, not learned
ALPHA = 0.1                              # EMA rate (assumption: spec gives no value in the handoff)
SD_FLOOR = {"pat": 15.0, "lnpi": 0.10, "lnlfhf": 0.30}   # ms / ln-units; prevents divide-by-~0 early on
REGIONS = ["heel", "sole/arch", "forefoot", "dorsum/midfoot", "medial", "lateral", "ankle/rearfoot"]
# PLACEHOLDER site prior for a lower-calf/Achilles sensor. NOT from spec v2 (not in the handoff): edit in the dashboard.
PI_SITE = [0.30, 0.10, 0.05, 0.05, 0.10, 0.10, 0.30]


def lfhf(tR):
    """LF/HF from trailing R-peak times via Lomb-Scargle on RR (no resampling). None if < 60 s of beats."""
    if len(tR) < 40 or tR[-1] - tR[0] < 55:
        return None
    rr = np.diff(tR); t = tR[1:]
    ok = (rr > .35) & (rr < 1.6)
    if ok.sum() < 35:
        return None
    t, rr = t[ok], rr[ok] - rr[ok].mean()
    f = np.linspace(0.04, 0.40, 200)
    p = signal.lombscargle(t, rr, 2 * np.pi * f, normalize=False)
    lf = p[(f >= .04) & (f < .15)].sum(); hf = p[(f >= .15) & (f <= .40)].sum()
    return float(lf / hf) if hf > 0 else None


def process(path, site):
    A, B, fa, fb = ep.load(path)
    disp, tR = ep.ecg_beats(A, fa)
    fB, tP = ep.ppg_peaks(B, fb)
    tT = ep.ppg_peaks(-B, fb)[1]                 # troughs = candidate systolic peaks if optical channel is inverted
    dur = len(B) / fb
    a_std_med = np.median([A[int(a * fa):int((a + 5) * fa)].std() for a in np.arange(0, dur - 5, 5)])
    wins = []
    for t0 in np.arange(0, dur - WIN_S + 1e-6, HOP_S):
        t1 = t0 + WIN_S
        r = tR[(tR >= t0) & (tR < t1)]
        rr = np.diff(r); okr = (rr > .35) & (rr < 1.6)
        w = dict(t0=float(t0), t1=float(t1), site=site)
        if len(r) < 8 or okr.mean() < 0.9:
            w.update(ecg_ok=False, ppg_ok=False, usable=False, reason="ECG beats not reliable")
            wins.append(w); continue
        hr_e = 60 / np.median(rr[okr]); rmssd = float(np.sqrt(np.mean(np.diff(rr[okr]) ** 2)) * 1e3)
        sat = float((np.abs(A[int(t0 * fa):int(t1 * fa)]) >= 2096000).mean())
        a_std = float(A[int(t0 * fa):int(t1 * fa)].std())
        ecg_ok = sat < 0.05 and a_std < 3 * a_std_med
        p = tP[(tP >= t0) & (tP < t1)]; ibi = np.diff(p)
        hr_p = 60 / np.median(ibi) if len(ibi) >= 5 else float("nan")
        # R -> next trough delay (ms) and fraction of R-beats that have one in the physiological 250-750 ms band
        d = []
        for tr in r:
            c = tT[(tT > tr + .15) & (tT < tr + 1.0)]
            if len(c): d.append(c[0] - tr)
        d = np.array(d); band = (d > .25) & (d < .75)
        match = float(band.sum() / len(r))
        pat = float(np.median(d[band]) * 1e3) if band.sum() >= 5 else None
        seg = fB[int(t0 * fb):int(t1 * fb)]; dc = float(B[int(t0 * fb):int(t1 * fb)].mean())
        pi = float(100 * (np.percentile(seg, 95) - np.percentile(seg, 5)) / abs(dc)) if abs(dc) > 3e5 else None
        ppg_ok = bool(pat is not None and pi is not None and match >= 0.8 and abs(hr_p - hr_e) <= 5 and abs(dc) > 3e5)
        why = []
        if not ecg_ok: why.append("ECG artifact/saturation")
        if abs(dc) <= 3e5: why.append("PPG signal level too low (poor contact)")
        elif match < 0.8: why.append("PPG beats do not match ECG beats")
        elif abs(hr_p - hr_e) > 5: why.append("PPG rate disagrees with ECG")
        w.update(hr_ecg=float(hr_e), hr_ppg=None if math.isnan(hr_p) else float(hr_p), rmssd_ms=rmssd, pat_ms=pat, pi_pct=pi,
                 beat_match=match, ppg_dc=dc, ecg_sat=sat, ecg_ok=bool(ecg_ok), ppg_ok=ppg_ok, usable=bool(ecg_ok and ppg_ok),
                 reason="; ".join(why) if why else "ok", lfhf=None)
        wins.append(w)
    dec = 2
    return dict(wins=wins, lfhf_session=lfhf(tR), dur=float(dur), fsA=float(fa / dec), fsB=float(fb),
                ecg=[int(round(v)) for v in disp[::dec]], ppg=[int(round(v)) for v in fB],
                R=[round(float(x), 3) for x in tR], Ppk=[round(float(x), 3) for x in tP], Ptr=[round(float(x), 3) for x in tT])


class EMA:
    def __init__(s, name): s.n, s.m, s.v, s.k, s.buf = name, 0.0, 0.0, 0, []
    def z(s, x):
        if s.k < 3: return None
        return float(np.clip((x - s.m) / max(math.sqrt(s.v), SD_FLOOR[s.n]), -4, 4))
    def update(s, x):
        if s.k < 3:
            s.buf.append(x); s.k += 1
            if s.k == 3: s.m = float(np.mean(s.buf)); s.v = float(np.var(s.buf))
            return
        d = x - s.m; s.m += ALPHA * d; s.v = (1 - ALPHA) * (s.v + ALPHA * d * d); s.k += 1


def sigmoid(x): return 1 / (1 + math.exp(-x))


def run_twin(sessions):
    emas = {k: EMA(k) for k in ("pat", "lnpi", "lnlfhf")}
    for s in sessions:
        for w in s["wins"]:
            w["I"] = None; w["u"] = None
            if not w.get("usable"):
                continue
            vals = {"pat": w["pat_ms"], "lnpi": math.log(w["pi_pct"]), "lnlfhf": math.log(w["lfhf"]) if w.get("lfhf") else None}
            z = {k: (emas[k].z(v) if v is not None else None) for k, v in vals.items()}
            if z["pat"] is None or z["lnpi"] is None:
                w["reason"] = "baseline warm-up (first 3 usable windows)"; w["warmup"] = True
            else:
                u1 = abs(z["pat"]); u2 = -z["lnpi"]; u3 = z["lnlfhf"] if z["lnlfhf"] is not None else 0.0
                eta = B_PRIOR[0] + B_PRIOR[1] * u1 + B_PRIOR[2] * u2 + B_PRIOR[3] * u3
                w["u"] = [u1, u2, u3]; w["z"] = z; w["I"] = sigmoid(eta); w["lfhf_used"] = z["lnlfhf"] is not None
            for k, v in vals.items():
                if v is not None: emas[k].update(v)    # baseline updated AFTER scoring so an anomaly does not normalise itself
    return sessions


if __name__ == "__main__":
    out = sys.argv[1]; sessions = []
    for arg in sys.argv[2:]:
        name, rest = arg.split("=", 1); path, _, site = rest.partition(":")
        s = process(path, site or "unspecified"); s["name"] = name; sessions.append(s)
    run_twin(sessions)
    json.dump(dict(sessions=sessions, b=B_PRIOR, regions=REGIONS, pi_site=PI_SITE, alpha=ALPHA), open(out, "w"))
    for s in sessions:
        u = [w for w in s["wins"] if w.get("usable")]
        print(f"{s['name']}: {len(s['wins'])} windows, usable {len(u)}, scored {sum(1 for w in s['wins'] if w.get('I') is not None)}")
        for w in s["wins"]:
            print(f"  {w['t0']:4.0f}s  HR {w.get('hr_ecg', float('nan')):5.1f}/{(w.get('hr_ppg') or float('nan')):5.1f}  PAT {w.get('pat_ms') or float('nan'):5.0f}  PI {w.get('pi_pct') or float('nan'):4.1f}%  match {w.get('beat_match', float('nan')):.2f}  usable {w.get('usable')}  I {w.get('I')}  {w.get('reason','')}")
