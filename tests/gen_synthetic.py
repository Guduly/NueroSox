#!/usr/bin/env python3
"""Synthetic raw-packet generator for the CardioTwin board pipeline (software tests ONLY; stdlib only).

Writes the same .jsonl format as a real capture: {"fifo_pointer", "raw_words": [11 x (v & 0xFFFFFF)], "host_time"}, word 6 = PPG,
the other 10 = ECG; 49.87 packets/s, ECG 498.7 Hz.  A sidecar <out>.truth.json lists the scenario segments (the generator knows
the truth; recordings never get labels).

Scenario sit_walk_sit: sit 120 s, walk 120 s, sit 90 s, poor contact 40 s, sit 60 s.
  ECG   Gaussian QRS (~4e4 counts) + P/T waves + noise; RR follows the scenario HR with +-3 % jitter.
  PPG   DC 1.2e6 + pulse ~3e4 counts, trough at R + 0.35 s; poor contact = DC 5e4.
  walk  stride sinusoid (0.85-0.95 Hz, amplitude 2x pulse) + 2nd harmonic, ECG baseline wander at the stride frequency,
        HR rising 75 -> 105 bpm over 60 s; after the walk HR decays back toward 72 bpm (tau 30 s).
Nothing here is a model of a real person; it only checks that the code does what it was designed to do.
"""
import argparse, json, math, os, random

FB, FA = 49.87, 498.7
SCENARIO = [("sit", 120), ("walk", 120), ("sit", 90), ("poor_contact", 40), ("sit", 60)]

def segments(scn):
    t, out = 0.0, []
    for kind, d in scn: out.append({"kind": kind, "t0": t, "t1": t + d}); t += d
    return out, t

def hr_at(t, segs):
    """HR (bpm) at time t: resting 72, linear ramp 75->105 over 60 s in a walk segment, exponential recovery afterwards."""
    base, last_hr, last_end = 72.0, 72.0, 0.0
    for s in segs:
        if s["t0"] <= t < s["t1"]:
            if s["kind"] == "walk": return 75.0 + 30.0 * min(1.0, (t - s["t0"]) / 60.0)
            return base + (last_hr - base) * math.exp(-(t - last_end) / 30.0) + 0.8 * math.sin(2 * math.pi * t / 37.0)
        if s["kind"] == "walk": last_hr, last_end = 75.0 + 30.0 * min(1.0, (s["t1"] - s["t0"]) / 60.0), s["t1"]
    return base

def stride_phase(t): return 2 * math.pi * (0.9 * t + 0.03 * 45 / (2 * math.pi) * (1 - math.cos(2 * math.pi * t / 45)))

def generate(out, seed=1, scn=SCENARIO):
    rnd = random.Random(seed); segs, total = segments(scn)
    kind_at = lambda t: next((s["kind"] for s in segs if s["t0"] <= t < s["t1"]), segs[-1]["kind"])
    # beat times
    R, t = [], 0.4
    while t < total + 1:
        R.append(t); t += (60.0 / hr_at(t, segs)) * (1 + rnd.uniform(-0.03, 0.03))
    n_pkt = int((total + 0.5) * FB); n_a = n_pkt * 10      # 0.5 s of padding so the last 20 s window is complete
    ecg = [0.0] * n_a; ppg = [0.0] * n_pkt
    for r in R:                                                      # ECG beats (QRS, P and T waves)
        for j in range(max(0, int((r - 0.35) * FA)), min(n_a, int((r + 0.6) * FA))):
            d = j / FA - r
            ecg[j] += (40000 * math.exp(-(d / 0.011) ** 2) - 6000 * math.exp(-((d - 0.03) / 0.012) ** 2)
                       + 4000 * math.exp(-((d + 0.16) / 0.025) ** 2) + 7000 * math.exp(-((d - 0.28) / 0.045) ** 2))
    amp = 30000.0
    for r in R:                                                      # PPG pulse: dip (trough) at R + 0.35 s
        for i in range(max(0, int((r + 0.35 - 0.5) * FB)), min(n_pkt, int((r + 0.35 + 0.6) * FB))):
            d = i / FB - (r + 0.35)
            ppg[i] -= amp * (math.exp(-(d / 0.11) ** 2) + 0.25 * math.exp(-((d - 0.28) / 0.10) ** 2))
    for j in range(n_a):                                             # ECG: noise, respiration wander, walking wander
        tj = j / FA; k = kind_at(tj)
        ecg[j] += rnd.gauss(0, 1500) + 1500 * math.sin(2 * math.pi * 0.25 * tj)
        if k == "walk": ecg[j] += 8000 * math.sin(stride_phase(tj) + 0.7)
    for i in range(n_pkt):                                           # PPG: DC, respiration, stride + 2nd harmonic, noise
        ti = i / FB; k = kind_at(ti)
        if k == "poor_contact": ppg[i] = ppg[i] * 0.03 + 5e4 + rnd.gauss(0, 200)
        else:
            ppg[i] += 1.2e6 + 6000 * math.sin(2 * math.pi * 0.25 * ti) + rnd.gauss(0, 300)
            if k == "walk":
                ph = stride_phase(ti); ppg[i] += 2 * amp * math.sin(ph) + 0.5 * amp * math.sin(2 * ph + 1.1)
    with open(out, "w") as f:
        for i in range(n_pkt):
            vals = [int(round(v)) for v in ecg[i * 10:i * 10 + 10]]; p = int(round(ppg[i]))
            words = [v & 0xFFFFFF for v in vals[:6]] + [p & 0xFFFFFF] + [v & 0xFFFFFF for v in vals[6:]]
            f.write(json.dumps({"fifo_pointer": 10, "raw_words": words, "host_time": 1.8e9 + i / FB}) + "\n")
    with open(out + ".truth.json", "w") as f: json.dump({"seed": seed, "segments": segs, "duration": total, "note": "synthetic; generator truth"}, f, indent=1)
    return segs, total

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--out", required=True); ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(); os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    segs, total = generate(a.out, a.seed); print("wrote %s (%.0f s, %d segments) and %s.truth.json" % (a.out, total, len(segs), a.out))
