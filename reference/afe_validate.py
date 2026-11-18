#!/usr/bin/env python3
"""AFE4950 capture validator / converter (laptop version: needs numpy + scipy).

JSONL (afe_capture.py format) -> CSV, plus signal-quality report.

What it does, in order:
  1. Decode 24-bit words to signed ints.
  2. Split each 11-word packet: position 6 = stream B (~50 Hz), the other ten = stream A (~500 Hz).
     (This split is by position; the tag bits are cross-checked, see tag_report.)
  3. Measure the real sample rate from host timestamps (linear fit over packet index).
  4. Mask saturated samples, 60 Hz notch (+120/180 Hz), 0.5-8 Hz band-pass.
  5. Per-window quality (saturation, band power fraction, spectral HR vs beat-count HR, beat regularity).
  6. Write CSVs and summary.

Usage:
  python3 afe_validate.py cap1.jsonl cap2.jsonl ... --outdir out
  python3 afe_validate.py --selftest
"""
import argparse
import csv
import json
import os
import sys

import numpy as np
from scipy import signal

SAT_LEVEL = 2096000      # |v| above this = at/near ADC rail (rails seen at +2096921 / -2096919)
WIN_S, HOP_S = 15.0, 5.0
HR_BAND = (0.6, 3.0)     # Hz = 36..180 bpm


# ---------------------------------------------------------------- loading / decoding
def load_jsonl(path):
    t, words = [], []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                p = json.loads(line)
            except json.JSONDecodeError:
                # e.g. a shell prompt captured by `picocom --logfile`; skip but tell the user
                print(f"note: {path}:{n}: skipped non-JSON line {line[:40]!r}", file=sys.stderr)
                continue
            if len(p["raw_words"]) != 11:
                raise ValueError(f"{path}:{n}: packet has {len(p['raw_words'])} words, expected 11")
            t.append(p["host_time"])
            words.append(p["raw_words"])
    return np.array(t), np.array(words, dtype=np.int64)


def to_signed24(w):
    return np.where(w >= 2 ** 23, w - 2 ** 24, w)


def tag_report(w):
    """Low 2 bits of each word. In the *tagged* config they look like a stream tag; in others they are noise."""
    low = w & 3
    pos6 = np.bincount(low[:, 6], minlength=4) / len(low)
    a = np.delete(low, 6, axis=1).ravel()
    pa = np.bincount(a, minlength=4) / a.size
    tagged = pos6[0] > 0.99 and (pa[0] < 0.01)          # B always tag 0, A never tag 0
    return {"tagged": bool(tagged), "B_low2": pos6.round(3).tolist(), "A_low2": pa.round(3).tolist()}


def split_streams(t, w):
    n = len(t)
    idx = np.arange(n)
    slope, _ = np.polyfit(idx, t - t[0], 1)
    pkt_rate = 1.0 / slope                               # packets per second (measured)
    resid = (t - t[0]) - (idx * slope + np.polyfit(idx, t - t[0], 1)[1])
    dt = np.diff(t)
    gaps = int((dt > 1.5 * np.median(dt)).sum())
    s = to_signed24(w)
    A = np.delete(s, 6, axis=1).ravel().astype(float)
    B = s[:, 6].astype(float)
    flagA = (np.delete(w, 6, axis=1).ravel() >> 1) & 1   # tag bit1 (meaning unknown, kept as column)
    fsA, fsB = pkt_rate * 10, pkt_rate
    return dict(A=A, B=B, flagA=flagA, fsA=fsA, fsB=fsB, pkt_rate=pkt_rate,
                jitter_ms=float(resid.std() * 1e3), gaps=gaps, n_pkt=n,
                dur=n / pkt_rate)


# ---------------------------------------------------------------- filtering
def clean(x, fs, notch=True, sat=None, band=(0.5, 8.0)):
    x = x.copy()
    if sat is None:
        sat = np.abs(x) >= SAT_LEVEL
    if sat.all():
        return np.zeros_like(x), sat
    if sat.any():
        good = np.flatnonzero(~sat)
        x[sat] = np.interp(np.flatnonzero(sat), good, x[good])
    x = x - np.median(x)
    if notch:
        for f0 in (60, 120, 180):
            if f0 < fs / 2 - 2:
                b, a = signal.iirnotch(f0, Q=30, fs=fs)
                x = signal.filtfilt(b, a, x)
    lo, hi = band[0], min(band[1], 0.4 * fs)
    sos = signal.butter(2, [lo, hi], btype="band", fs=fs, output="sos")
    return signal.sosfiltfilt(sos, x), sat


# ---------------------------------------------------------------- pulse analysis
def spectral_hr(x, fs):
    n = len(x)
    nfft = 1 << int(np.ceil(np.log2(n * 8)))
    X = np.abs(np.fft.rfft((x - x.mean()) * np.hanning(n), nfft)) ** 2
    f = np.fft.rfftfreq(nfft, 1 / fs)
    m = (f >= HR_BAND[0]) & (f <= HR_BAND[1])
    k = np.flatnonzero(m)[np.argmax(X[m])]
    f0 = f[k]
    tot = X[(f >= 0.5) & (f <= 4.0)].sum()
    near = ((np.abs(f - f0) < 0.15) | (np.abs(f - 2 * f0) < 0.15)) & (f >= 0.5) & (f <= 4.0)
    frac = X[near].sum() / tot if tot > 0 else 0.0
    return 60 * f0, float(frac)


def detect_beats(x, fs):
    if np.std(x) == 0:
        return np.array([], int)
    z = (x - x.mean()) / x.std()
    pk, _ = signal.find_peaks(z, distance=int(0.33 * fs), prominence=0.5)
    return pk


def window_metrics(xf, sat, fs):
    hr_s, frac = spectral_hr(xf, fs)
    pk = detect_beats(xf, fs)
    if len(pk) >= 4:
        ibi = np.diff(pk) / fs
        ibi = ibi[(ibi > 0.33) & (ibi < 1.7)]
    else:
        ibi = np.array([])
    if len(ibi) >= 3:
        hr_p = 60 / np.median(ibi)
        cv = float(ibi.std() / ibi.mean())
    else:
        hr_p, cv = np.nan, np.nan
    satf = float(sat.mean())
    agree = (not np.isnan(hr_p)) and abs(hr_p - hr_s) <= 5.0   # 15 s window => ~4 bpm FFT resolution
    good = bool(satf < 0.05 and frac >= 0.35 and agree and cv < 0.25)
    score = 0.0
    if not np.isnan(hr_p):
        score = (1 - min(1, satf / 0.2)) * min(1, frac / 0.5) * (1 - min(1, cv / 0.3)) * (1.0 if agree else 0.3)
    return dict(hr_spec=hr_s, hr_beats=hr_p, band_frac=frac, ibi_cv=cv, sat_frac=satf,
                agree=bool(agree), good=good, score=float(score))


def analyze_stream(x, fs, notch, mask_sat=True, band=(0.5, 8.0)):
    # Stream B is NOT a 22-bit signal (it reads ~2.3 M in untagged captures), so the A-stream rail level
    # must not be applied to it.
    xf, sat = clean(x, fs, notch=notch, sat=None if mask_sat else np.zeros(len(x), bool), band=band)
    wl, hl = int(WIN_S * fs), int(HOP_S * fs)
    rows = []
    for s0 in range(0, len(x) - wl + 1, hl):
        m = window_metrics(xf[s0:s0 + wl], sat[s0:s0 + wl], fs)
        m["t0"] = s0 / fs
        rows.append(m)
    return xf, sat, rows


# ---------------------------------------------------------------- per-file driver
def process(path, outdir):
    t, w = load_jsonl(path)
    name = os.path.splitext(os.path.basename(path))[0]
    sp = split_streams(t, w)
    tags = tag_report(w)
    A, B, fsA, fsB = sp["A"], sp["B"], sp["fsA"], sp["fsB"]
    # time axis from packet index (host_time has ~ms jitter; the fit is smoother)
    tA, tB = np.arange(len(A)) / fsA, np.arange(len(B)) / fsB
    os.makedirs(outdir, exist_ok=True)
    xf, satA, rowsA = analyze_stream(A, fsA, notch=True)
    yf, satB, rowsB = analyze_stream(B, fsB, notch=False, mask_sat=False, band=(0.7, 2.5))  # PPG band: 42-150 bpm; wider bands let the dicrotic notch confuse the beat counter
    with open(os.path.join(outdir, f"{name}_A.csv"), "w", newline="") as f:
        cw = csv.writer(f)
        cw.writerow(["time_s", "A_raw", "A_filtered", "saturated", "tag_bit1"])
        for i in range(len(A)):
            cw.writerow([f"{tA[i]:.5f}", int(A[i]), f"{xf[i]:.1f}", int(satA[i]), int(sp["flagA"][i])])
    with open(os.path.join(outdir, f"{name}_B.csv"), "w", newline="") as f:
        cw = csv.writer(f)
        cw.writerow(["time_s", "B_raw", "B_filtered", "saturated"])
        for i in range(len(B)):
            cw.writerow([f"{tB[i]:.5f}", int(B[i]), f"{yf[i]:.1f}", int(satB[i])])
    with open(os.path.join(outdir, f"{name}_windows.csv"), "w", newline="") as f:
        keys = ["t0", "hr_spec", "hr_beats", "band_frac", "ibi_cv", "sat_frac", "agree", "good", "score"]
        cw = csv.writer(f)
        cw.writerow(["stream"] + keys)
        for tag, rows in (("A", rowsA), ("B", rowsB)):
            for r in rows:
                cw.writerow([tag] + [round(r[k], 3) if isinstance(r[k], float) else r[k] for k in keys])
    goodA = [r for r in rowsA if r["good"]]
    goodB = [r for r in rowsB if r["good"]]
    hrs = [r["hr_beats"] for r in goodA]
    return dict(
        name=name, n_pkt=sp["n_pkt"], dur_s=round(sp["dur"], 1), fsA=round(fsA, 2), fsB=round(fsB, 3),
        gaps=sp["gaps"], jitter_ms=round(sp["jitter_ms"], 2), tagged=tags["tagged"],
        sat_A=round(float(satA.mean()), 3), sat_B=round(float(satB.mean()), 3),
        mains_frac=round(mains_fraction(A, fsA), 3),
        win_total=len(rowsA), win_good_A=len(goodA), win_good_B=len(goodB),
        mean_score_A=round(float(np.mean([r["score"] for r in rowsA])) if rowsA else 0, 3),
        hr_good_median=round(float(np.median(hrs)), 1) if hrs else None,
        hr_good_range=(round(min(hrs), 1), round(max(hrs), 1)) if hrs else None,
    )


def mains_fraction(x, fs):
    """Share of total variance sitting within +/-1 Hz of 60/120/180 Hz."""
    x = np.clip(x, -SAT_LEVEL, SAT_LEVEL)
    f, p = signal.welch(x - x.mean(), fs=fs, nperseg=min(4096, len(x)))
    tot = p.sum()
    m = np.zeros_like(f, bool)
    for f0 in (60, 120, 180, 240):
        m |= np.abs(f - f0) < 1.0
    return float(p[m].sum() / tot) if tot else 0.0


# ---------------------------------------------------------------- self-test on synthetic data
def selftest():
    rng = np.random.default_rng(1)
    fs = 498.4
    results = []
    for bpm in (50, 62, 75, 90, 110):
        n = int(60 * fs)
        t = np.arange(n) / fs
        f0 = bpm / 60
        # simple PPG-like pulse: fundamental + 2nd harmonic, small amplitude relative to ripple
        pulse = 90000 * (np.sin(2 * np.pi * f0 * t) + 0.4 * np.sin(4 * np.pi * f0 * t + 0.8))
        ripple = 600000 * np.sin(2 * np.pi * 60 * t + 0.3) + 200000 * np.sin(2 * np.pi * 120 * t)
        drift = 300000 * np.sin(2 * np.pi * 0.05 * t)
        x = pulse + ripple + drift + rng.normal(0, 30000, n)
        x = np.clip(x, -2096919, 2096921)
        xf, sat = clean(x, fs)
        wl = int(WIN_S * fs)
        est = [window_metrics(xf[s:s + wl], sat[s:s + wl], fs) for s in range(0, n - wl + 1, int(HOP_S * fs))]
        errs = [abs(e["hr_beats"] - bpm) for e in est if e["good"]]
        results.append((bpm, len(errs), len(est), max(errs) if errs else None))
        print(f"  synthetic {bpm:3d} bpm: good windows {len(errs)}/{len(est)}, max |error| {max(errs) if errs else 'n/a'}")
    ok = all(r[3] is not None and r[3] <= 3.0 for r in results)
    print("SELFTEST", "PASS (all errors <= 3 bpm)" if ok else "FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*")
    ap.add_argument("--outdir", default="afe_out")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--ref-bpm", type=float, help="reference heart rate (pulse-ox/chest strap) for the capture(s)")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if not a.files:
        ap.error("give at least one .jsonl file or --selftest")
    out = []
    for p in a.files:
        r = process(p, a.outdir)
        if a.ref_bpm and r["hr_good_median"] is not None:
            r["hr_error_vs_ref"] = round(r["hr_good_median"] - a.ref_bpm, 1)
        out.append(r)
    out.sort(key=lambda r: (-r["win_good_A"], -r["mean_score_A"]))
    keys = list(out[0].keys())
    with open(os.path.join(a.outdir, "summary.csv"), "w", newline="") as f:
        cw = csv.DictWriter(f, fieldnames=keys)
        cw.writeheader()
        cw.writerows(out)
    for r in out:
        print(json.dumps(r))


if __name__ == "__main__":
    main()
