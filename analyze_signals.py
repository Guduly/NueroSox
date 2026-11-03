"""Track 1 - AFE4950 ECG/PPG analysis.

Reads data/AFE4950_CAPTURED_DATA.csv, filters both channels, detects ECG R-peaks
and optical peaks, pairs them, estimates heart rate, and writes
data/metrics.json and data/candidate_matches.csv for the dashboard.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.signal import butter, sosfiltfilt, find_peaks

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
CSV = DATA / "AFE4950_CAPTURED_DATA.csv"

ECG_BAND = (0.5, 40.0)    # Hz
PPG_BAND = (0.5, 5.0)     # Hz
MATCH_MIN_S, MATCH_MAX_S = 0.05, 0.60   # PPG peak must follow an R-peak by this much


def load_channels(csv_path=CSV):
    """Return (t_ecg, ecg, t_ppg, ppg) as clean numpy arrays."""
    # index_col=False: the export has a trailing delimiter on each data row
    df = pd.read_csv(csv_path, index_col=False)
    e = df[["ECG_Time", "ECG_Value"]].apply(pd.to_numeric, errors="coerce").dropna()
    p = df[["TIA1-3_Time", "TIA1-3_Value"]].apply(pd.to_numeric, errors="coerce").dropna()
    return (e["ECG_Time"].to_numpy(), e["ECG_Value"].to_numpy(),
            p["TIA1-3_Time"].to_numpy(), p["TIA1-3_Value"].to_numpy())


def sample_rate(t):
    return float(1.0 / np.median(np.diff(t)))


def bandpass(x, fs, lo, hi, order=3):
    hi = min(hi, 0.45 * fs)
    sos = butter(order, [lo, hi], btype="bandpass", fs=fs, output="sos")
    return sosfiltfilt(sos, x - np.mean(x))


def detect_r_peaks(ecg_filt, fs):
    """R-peaks: tall, positive, at least 0.3 s apart (max ~200 BPM)."""
    top = np.percentile(ecg_filt, 99.5)
    base = np.median(ecg_filt)
    idx, _ = find_peaks(ecg_filt, height=base + 0.5 * (top - base),
                        distance=int(0.3 * fs))
    return idx


def detect_ppg_peaks(ppg_filt, fs):
    idx, _ = find_peaks(ppg_filt, distance=int(0.3 * fs),
                        prominence=0.3 * np.std(ppg_filt))
    return idx


def match_peaks(t_r, t_p):
    """Pair each R-peak with the first unused PPG peak 0.05-0.60 s later."""
    rows, used = [], set()
    for tr in t_r:
        for j, tp in enumerate(t_p):
            if j in used:
                continue
            if MATCH_MIN_S <= tp - tr <= MATCH_MAX_S:
                rows.append({"ecg_peak_time_s": round(float(tr), 4),
                             "ppg_peak_time_s": round(float(tp), 4),
                             "delay_s": round(float(tp - tr), 4)})
                used.add(j)
                break
    return pd.DataFrame(rows)


def analyze(csv_path=CSV):
    t_e, ecg, t_p, ppg = load_channels(csv_path)
    fs_e, fs_p = sample_rate(t_e), sample_rate(t_p)
    ecg_f = bandpass(ecg, fs_e, *ECG_BAND)
    ppg_f = bandpass(ppg, fs_p, *PPG_BAND)

    r_idx = detect_r_peaks(ecg_f, fs_e)
    p_idx = detect_ppg_peaks(ppg_f, fs_p)
    t_r, t_pk = t_e[r_idx], t_p[p_idx]
    matches = match_peaks(t_r, t_pk)

    hr = float(60.0 / np.mean(np.diff(t_r))) if len(t_r) > 1 else float("nan")
    metrics = {
        "recording_length_s": round(float(max(t_e[-1], t_p[-1])), 2),
        "ecg_sample_rate_hz": round(fs_e),
        "ppg_sample_rate_hz": round(fs_p),
        "ecg_samples": int(len(t_e)),
        "ppg_samples": int(len(t_p)),
        "ecg_beats": int(len(r_idx)),
        "ppg_peaks": int(len(p_idx)),
        "ecg_ppg_matches": int(len(matches)),
        "heart_rate_bpm": round(hr, 2),
    }
    return metrics, matches


def main():
    if not CSV.exists():
        raise SystemExit(f"CSV not found: {CSV}\nCopy AFE4950_CAPTURED_DATA.csv into the data folder.")
    metrics, matches = analyze()
    DATA.mkdir(exist_ok=True)
    (DATA / "metrics.json").write_text(json.dumps(metrics, indent=2))
    matches.to_csv(DATA / "candidate_matches.csv", index=False)

    print("TRACK 1 - REAL AFE4950 ANALYSIS")
    print("-" * 31)
    print(f"ECG beats: {metrics['ecg_beats']}")
    print(f"Candidate PPG peaks: {metrics['ppg_peaks']}")
    print(f"ECG-PPG candidate matches: {metrics['ecg_ppg_matches']}")
    print(f"Heart rate: {metrics['heart_rate_bpm']:.2f} BPM")
    print(f"Saved to: {DATA}")


if __name__ == "__main__":
    main()