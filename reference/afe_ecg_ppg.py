#!/usr/bin/env python3
"""ECG (stream A, ~500 Hz) + PPG (stream B, ~50 Hz) analysis of an afe4950 JSONL capture.
Needs numpy, scipy, matplotlib.  Usage: python3 afe_ecg_ppg.py capture.jsonl [--ref-bpm 88] [--outdir out]
Outputs: printed summary, <name>_beats.csv (R times, RR, nearest PPG peak) and <name>_ecg_ppg.png.
Stream roles (A=ECG, B=PPG) are INFERRED from rates and behaviour, not confirmed against the TI GUI export.
PTT here is R-to-PPG-peak using the Linux packet clock; the two streams' hardware alignment and the PPG
polarity are NOT verified, so treat the delay as unvalidated.
"""
import argparse, json, os, sys
import numpy as np
from scipy import signal

def load(path):
    t, w = [], []
    for n, line in enumerate(open(path, encoding="utf-8"), 1):
        if not line.startswith("{"):
            continue
        p = json.loads(line)
        if len(p["raw_words"]) == 11:
            t.append(p["host_time"]); w.append(p["raw_words"])
    w = np.array(w, dtype=np.int64); t = np.array(t)
    s = np.where(w >= 2**23, w - 2**24, w)
    rate = 1 / np.polyfit(np.arange(len(t)), t, 1)[0]
    return np.delete(s, 6, axis=1).ravel().astype(float), s[:, 6].astype(float), rate * 10, rate

def ecg_beats(A, fs):
    x = A.copy()
    for f0 in (60, 120, 180):
        b, a = signal.iirnotch(f0, 30, fs=fs); x = signal.filtfilt(b, a, x)
    x -= np.median(x)
    disp = signal.sosfiltfilt(signal.butter(2, [0.5, 40], "band", fs=fs, output="sos"), x)
    y = signal.sosfiltfilt(signal.butter(2, [5, 20], "band", fs=fs, output="sos"), x)
    pk, _ = signal.find_peaks(y, height=np.percentile(y, 99) * 0.4, distance=int(0.3 * fs))
    R = []
    for k in pk:
        lo, hi = max(0, k - int(.04 * fs)), min(len(disp), k + int(.04 * fs))
        R.append(lo + np.argmax(np.abs(disp[lo:hi])))
    return disp, np.array(R) / fs

def ppg_peaks(B, fs):
    f = signal.sosfiltfilt(signal.butter(2, [0.7, 2.5], "band", fs=fs, output="sos"), signal.detrend(B))
    pk, _ = signal.find_peaks(f, distance=int(.4 * fs), prominence=.3 * np.std(f))
    return f, pk / fs

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("file"); ap.add_argument("--ref-bpm", type=float)
    ap.add_argument("--outdir", default="afe_out"); a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True); name = os.path.splitext(os.path.basename(a.file))[0]
    A, B, fsA, fsB = load(a.file)
    sat = float((np.abs(A) >= 2096000).mean())
    print(f"duration {len(B)/fsB:.1f}s  A sat {sat*100:.1f}%  (high saturation = ECG inputs not connected / mis-ranged)")
    disp, tR = ecg_beats(A, fsA); fB, tP = ppg_peaks(B, fsB)
    rr = np.diff(tR); ok = (rr > .35) & (rr < 1.6)
    ibi = np.diff(tP); okp = (ibi > .35) & (ibi < 1.6)
    hr_e, hr_p = 60 / np.median(rr), 60 / np.median(ibi)
    print(f"ECG: {len(tR)} beats  HR {hr_e:.1f} bpm  valid RR {ok.mean()*100:.0f}%  SDNN {rr[ok].std()*1e3:.0f} ms  RMSSD {np.sqrt(np.mean(np.diff(rr[ok])**2))*1e3:.0f} ms")
    print(f"PPG: {len(tP)} beats  HR {hr_p:.1f} bpm  valid IBI {okp.mean()*100:.0f}%   ECG-PPG HR difference {hr_p-hr_e:+.1f} bpm")
    if a.ref_bpm: print(f"reference {a.ref_bpm} bpm: ECG error {hr_e-a.ref_bpm:+.1f}, PPG error {hr_p-a.ref_bpm:+.1f}")
    # Timing of the PPG relative to each R-peak. Peaks sit near R (within ~+-0.1 s), so a fixed search window
    # would split them arbitrarily; report the SIGNED offset to the nearest peak, and the delay to the next trough
    # (the candidate systolic peak if the optical channel is inverted).
    tT = ppg_peaks(-B, fsB)[1]
    offs = np.array([(tP[np.argmin(np.abs(tP - tr))] - tr) for tr in tR])
    trough = []
    for tr in tR:
        c = tT[(tT > tr + .15) & (tT < tr + 1.0)]
        trough.append(c[0] - tr if len(c) else np.nan)
    trough = np.array(trough)
    print(f"PPG peak offset to nearest R: median {np.median(offs)*1e3:+.0f} ms (IQR {np.percentile(offs,25)*1e3:+.0f}..{np.percentile(offs,75)*1e3:+.0f}); "
          f"R->next PPG trough: median {np.nanmedian(trough)*1e3:.0f} ms (IQR {np.nanpercentile(trough,25)*1e3:.0f}..{np.nanpercentile(trough,75)*1e3:.0f})")
    print("NOTE: a wrist pulse peak normally arrives >= ~250 ms after R. Peaks near R+0 suggest inverted optical polarity or a stream offset; unresolved.")
    rows = []
    for i, tr in enumerate(tR):
        rows.append((tr, rr[i] if i < len(rr) else "", offs[i], trough[i] if not np.isnan(trough[i]) else ""))
    with open(os.path.join(a.outdir, f"{name}_beats.csv"), "w") as f:
        f.write("R_time_s,RR_next_s,signed_offset_to_nearest_PPG_peak_s,R_to_next_PPG_trough_s\n")
        for r in rows: f.write(",".join(f"{v:.4f}" if v != "" else "" for v in r) + "\n")
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    ta = np.arange(len(disp)) / fsA; tb = np.arange(len(fB)) / fsB
    fig, ax = plt.subplots(2, 1, figsize=(13, 7)); t0 = 20; m = (ta >= t0) & (ta <= t0 + 10); mb = (tb >= t0) & (tb <= t0 + 10)
    ax[0].plot(ta[m], disp[m], lw=.6); r = tR[(tR >= t0) & (tR <= t0 + 10)]; ax[0].plot(r, np.interp(r, ta, disp), "rv"); ax[0].set_title("ECG (A), R-peaks red")
    ax[1].plot(tb[mb], fB[mb]); p = tP[(tP >= t0) & (tP <= t0 + 10)]; ax[1].plot(p, np.interp(p, tb, fB), "gv")
    for k in r: ax[1].axvline(k, color="r", lw=.4)
    ax[1].set_title("PPG (B), peaks green, ECG R red lines"); plt.tight_layout(); plt.savefig(os.path.join(a.outdir, f"{name}_ecg_ppg.png"), dpi=80)

if __name__ == "__main__":
    main()
