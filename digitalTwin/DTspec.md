# Foot-Pain Digital Twin: Build Specification v2 (ECG + PPG at lower calf / Achilles, label-free)

Self-contained spec so that any engineer or LLM can build the digital twin from it. v2 updates the model for the actual sensor placement (**lower calf / Achilles tendon area**) and replaces the v1 scoring equations.

---

## 0. What changed from v1

1. **Sensor site is now fixed (posterior distal leg).** PPG and PTT are measured at the lower calf/Achilles, so they describe **local posterior-leg perfusion**, not the whole foot. A site-observability prior over regions is added (Section 7.4).
2. **New global equation** (logistic, 0-1 output), based on the inference form you found, with three corrections explained in Section 6.2:
   - PTT enters as a **two-sided deviation** `|z_PTT|`
   - perfusion enters as a **log inverse amplitude** `-z(ln PI)`
   - autonomic stress enters as **`ln(LF/HF)`** (equivalent to the inverse of HF/LF) computed on a **long enough RR window**
3. **β are fixed priors**, not learned. Learning β needs labelled outcomes, which this project does not have. The "calibration" step only fits the personal baseline (μ, sd), see Section 5.
4. Added posture/motion handling, which matters far more at the calf than at a fingertip.

---

## 1. Goal and constraints

- **Goal:** observe ECG + PPG (optional EMG) streams, maintain a self-updating per-person model, output (a) a 0-1 pain-likeness score `I(t)` and (b) a risk for each of 7 foot regions.
- **No labels or self-reports.**
- **Existing components:** AFE4950EVM (AFE4950, MSP430F5529 USB controller, TI `bio-sensing.exe`), TI SK-AM62B, host computer.
- **Input contract:** ADC counts in CSV (Section 3); same code for recorded files and live streams.
- **Regions (R = 7):** `heel`, `sole_arch`, `forefoot`, `dorsum_midfoot`, `medial`, `lateral`, `ankle_rearfoot`.

### Scope note
Calf/Achilles PPG tells you about perfusion of the posterior distal leg; ECG-derived HRV is systemic. Neither directly measures the plantar or forefoot structures. The region split (Section 7) is therefore a **hypothesis encoded in config (W and the site prior π)**, not something learned from data. Keep both as editable, versioned files, and label outputs as "research prototype, hypothesis-driven".

---

## 2. Architecture

```
[ECG electrodes + PPG LED/PD at lower calf / Achilles area (+ optional IMU, EMG)]
        |
   AFE4950 (22-bit ADC, 256-sample FIFO, I2C or SPI)
        |
   Phase 1: EVM MSP430 --USB--> PC: bio-sensing.exe --> CSV
   Phase 2: AM62B reads AFE over I2C/SPI (MSP430 held in reset: one bus master only)
        |
   AM62B: acquisition, de-interleave, filter, quality gate, features
        |  Ethernet (UDP/TCP/MQTT)
        v
   Host: Digital Twin (baseline, I(t), region risks, logging)
```

The twin core (Sections 5-8) consumes feature vectors only, so it is independent of how data arrived.

---

## 3. Data contract

CSV columns: `time_s` (float, monotonic), `ecg`, `ppg` (ADC counts; add `ppg2`, `ppg3` if more phases are used), optional `acc_x, acc_y, acc_z` (IMU), optional `emg`.
`fs = 1 / median(diff(time_s))`, require fs >= 100 Hz (recommend 250-500 Hz). Split recordings at gaps larger than 2 sample periods.

Live message: `{"t":..., "seq":..., "ecg":[...], "ppg":[...], "fs":250}`. A jump in `seq` invalidates the current window.

AFE decoding (Phase 2): 22-bit samples packed in 3 bytes (confirm in the datasheet), sign-extend from bit 21; FIFO holds phases in a fixed repeating order, so de-interleave by `index mod N_phases`; read on data-ready, check FIFO count, flag overflow (256 samples) and re-sync at the next cycle boundary. Verify against the GUI using the same input.

---

## 4. Signal processing

Short window `W_s = 30 s`, step `H_s = 10 s`. Long window `W_L = 180 s` (rolling) for spectral HRV.

1. **Filters** (zero-phase per window, or causal for live): ECG 0.5-40 Hz band-pass (+50/60 Hz notch); PPG 0.5-8 Hz; EMG 20-450 Hz then rectify/smooth.
2. **R peaks** (Pan-Tompkins or NeuroKit2), min spacing 0.3 s, positive polarity. `RR_i = (r_{i+1} - r_i)/fs`, keep 0.3 s < RR < 2.0 s.
3. **Per-beat PPG landmarks:** systolic peak `p = argmax PPG in [r+0.05 s, r+0.7 s]`; foot `f = argmin PPG in [r, p]`. At the calf the pulse arrives later than at the finger, so extend the upper search bound (up to r+0.9 s) if peaks are missed; confirm on real data.
4. **Spectral HRV (for LF/HF):** build the RR series from the last `W_L` seconds, resample to 4 Hz (cubic interpolation), detrend, Welch PSD. `LF = power in 0.04-0.15 Hz`, `HF = power in 0.15-0.40 Hz`. Require at least 120 s of valid data; LF is not meaningful on 30 s windows.
5. **Quality gate** (window rejected if any fails; rejected windows never update the baseline):
   - fewer than 10 R peaks in the window, or fewer than 80% of RR in range
   - fewer than 8 valid PPG beats, or PPG SNR below threshold (ratio of pulse-band power to out-of-band power)
   - flat-line, ADC clipping, sequence gap
   - **motion:** if an IMU is present, reject windows with motion energy above threshold; without an IMU, restrict scoring to controlled rest sessions
6. **Context label:** `rest` vs `active` (from IMU or protocol). Keep **separate baselines per context** (leg posture and muscle activity change calf PTT and amplitude substantially).

Sensor placement note: the Achilles tendon itself is poorly perfused. Put the PPG probe over soft tissue near the lower calf where a clean pulse is visible, and check SNR before recording.

---

## 5. Features and personal baseline

### 5.1 Raw features (per accepted window)

| name | definition | unit |
|---|---|---|
| `PTT` | median over beats of `(f - r)/fs x 1000` (R peak to calf PPG foot) | ms |
| `PI` | `A_ppg / abs(mean raw PPG)`, with `A_ppg = median(PPG[p] - PPG[f])` (peak-to-peak) | ratio |
| `LFHF` | LF power / HF power from Section 4.4 (updated every step using the rolling `W_L`) | ratio |
| `hr`, `rmssd`, `rise` | context / optional features | bpm, ms, ms |

Transforms: `y1 = PTT`, `y2 = ln(PI)`, `y3 = ln(LFHF)` (logs make the ratios roughly symmetric and Gaussian-like).

### 5.2 Baseline (this is the "patient calibration")
Warm-up: first `N_warm = 12` accepted windows **at rest, same posture** (about 10 minutes recommended): `mu_j = mean(y_j)`, `var_j = variance(y_j) + 1e-9`.

Standardize:
```
sd_j = max( sqrt(var_j), 0.02*|mu_j| + 1e-6 )
z_j  = clip( (y_j - mu_j) / sd_j , -5, +5 )
```
Online update every accepted window (EMA, slowed during deviations):
```
A_dev     = sqrt( mean_j z_j^2 )
alpha_eff = alpha          if A_dev < 2*A0
          = 0.1*alpha      otherwise
d    = y - mu
mu  <- mu + alpha_eff * d
var <- (1 - alpha_eff) * ( var + alpha_eff * d^2 )
```
Defaults: `alpha = 0.03`, `A0 = 1.5`. Optionally keep a fast and a slow baseline (minutes vs days).

---

## 6. Global equation: pain-likeness I(t)

### 6.1 Drivers (all oriented so that larger means more pain-like)
```
u1 =  | z_PTT |           two-sided deviation of pulse transit time from baseline
u2 = - z_lnPI            perfusion drop, i.e. a monotone form of 1/A_ppg (amplitude falls -> u2 rises)
u3 =  z_lnLFHF           sympathetic shift, equal to -z(ln(HF/LF))
```

### 6.2 Equation
```
eta(t) = b0 + b1*u1 + b2*u2 + b3*u3
I(t)   = sigmoid( eta ) = 1 / (1 + exp(-eta))            in [0, 1]
```
Fixed prior coefficients (starting values, in config, not learned):
```
b0 = -2.5     b1 = 0.6     b2 = 0.9     b3 = 0.6
```
Behaviour check: at baseline (E|z| about 0.8) `I` is about 0.12; all drivers at +1 sd gives about 0.40; all at +2 sd gives about 0.85.

Why this differs from the form you found:
- **ΔPTT sign is ambiguous.** Sympathetic vasoconstriction tends to shorten PTT, while ischemic/damped flow lengthens the arrival time at a distal site. Using `|z_PTT|` captures either without committing to one sign. (If you later establish the direction for your subjects, switch to a signed term.)
- **1/A_ppg** is unbounded as A approaches 0 and is scale-dependent. `-z(ln PI)` is the standardized, bounded-in-practice equivalent.
- **HF/LF falls under sympathetic stress**, so with a positive weight you need `LF/HF` (or a negative β). Also, LF needs windows of roughly 2 minutes or more.
- **Sigmoid:** the original is linear; the 0-1 range requires a logistic link.
- **β learned during calibration** requires labelled outcomes. Without them, β are priors; the calibration only fits μ and sd.
- The residual term ε is not part of inference; noise is handled by the quality gate and smoothing.

### 6.3 Smoothing and trend ("future tendency")
```
eta_f(t) = (1-bf)*eta_f(t-1) + bf*eta(t)        bf = 0.30
eta_s(t) = (1-bs)*eta_s(t-1) + bs*eta(t)        bs = 0.05
eta_hat  = eta_f + gamma*(eta_f - eta_s)        gamma = 1.0
I_hat(t) = sigmoid( eta_hat )
```
Report both `I` (now) and `I_hat` (trend-adjusted tendency).

---

## 7. Region allocation

### 7.1 Region evidence
```
S_r = sum_{j=1..3} W[r,j] * u_j          (R x 3 matrix times the driver vector)
```
Smooth with the same fast/slow EMAs and trend term as Section 6.3 to get `Score_r`.

### 7.2 Region distribution with site prior
```
P_r = exp( Score_r/tau + ln(pi_r) ) / sum_r' exp( Score_r'/tau + ln(pi_r') )       tau = 1.0
```
### 7.3 Final output
```
Risk_r(t) = I_hat(t) * P_r(t)
```
The overall level comes from `I_hat`; the region split only apportions it.

### 7.4 Placeholder W and site prior pi (editable hypotheses)

Rationale used for the placeholders: a posterior-leg sensor is physically closest to the Achilles/heel/rearfoot; the posterior tibial artery (passing behind the ankle) and the peroneal artery feed the heel and plantar surface and lateral side, so calf perfusion is a plausible indicator for those regions; the dorsum is supplied by the anterior tibial/dorsalis pedis route, which this sensor does not observe; forefoot is distal and is assumed to be reflected mostly through the systemic autonomic driver. These are assumptions to be verified, not facts established by data.

| region | W: u1 (PTT dev) | W: u2 (perfusion drop) | W: u3 (autonomic) | site prior pi |
|---|---|---|---|---|
| heel | 0.8 | 1.0 | 0.3 | 0.22 |
| ankle_rearfoot | 0.7 | 0.8 | 0.4 | 0.22 |
| sole_arch | 0.7 | 0.9 | 0.3 | 0.16 |
| lateral | 0.6 | 0.7 | 0.3 | 0.12 |
| medial | 0.6 | 0.6 | 0.3 | 0.12 |
| forefoot | 0.4 | 0.5 | 0.8 | 0.10 |
| dorsum_midfoot | 0.2 | 0.2 | 0.5 | 0.06 |

(pi sums to 1.0.) Store in `config/W.yaml` and `config/prior.yaml` with version numbers and per-row rationale.

Optional extra driver: pulse rise time `u4 = z_rise` with a small weight (default 0 until validated). If a calf EMG channel is added, `u5 = z(ln emg_rms)` can be appended as another column of W (muscle loading near the Achilles).

### 7.5 Hook if labels ever exist
Learn b (Section 6.2) by logistic regression on `(u, outcome)` pairs, and W by softmax regression `W <- W - lr * outer(P - y, u)`. Disabled by default; snapshot old values and keep a rollback.

---

## 8. Update policy and guardrails

| What | When | Rule |
|---|---|---|
| mu, var (per context) | every accepted window | Section 5.2 |
| eta_f, eta_s, Score EMAs | every accepted window | Section 6.3 / 7.1 |
| b, W, pi | never unless labels exist | Section 7.5, manual enable |
| rejected windows | n/a | update nothing |

Guardrails: daily drift check of mu (alert if any `mu_j` shifts more than 3 sd in 24 h; probe re-placement is the likely cause); restart warm-up when the probe is re-attached or after a long gap; stamp every output row with the config version; no random state, so the same input always gives the same output.

---

## 9. Software components

```
foot_twin/
  config/ params.yaml (fs, windows, bands, alpha, bf, bs, gamma, tau, A0, N_warm)
          beta.yaml (b0..b3)   W.yaml   prior.yaml
  io/    csv_source.py  stream_source.py  afe_reader.py (AM62B: I2C/SPI, FIFO, decode, de-interleave)
  dsp/   filters.py  beats.py  hrv_spectral.py  features.py  quality.py
  twin/  baseline.py  global_eq.py (u, eta, I)  regions.py (S, P, Risk)  twin.py
  out/   logger.py
  tests/ synth.py  test_*.py
  run.py
```

Interfaces: `features.extract(ecg, ppg, fs, acc=None) -> dict | None`; `Twin.step(features, context) -> dict | None`.
Output row: `t_end_s, context, PTT, PI, LFHF, u1, u2, u3, eta, I, I_hat, risk_<region> x7, top_region, config_version`.
Dependencies: Python 3.10+, numpy, scipy, pandas, pyyaml; optional neurokit2, smbus2/spidev/libgpiod.

---

## 10. Build phases

0. Offline: `dsp/` + `twin/` on synthetic data and public ECG+PPG data (for example PhysioNet BIDMC).
1. CSV exported from the EVM GUI at the calf site; confirm columns, fs, SNR.
2. AM62B acquisition over I2C (prove with `i2cdetect`, then register read, FIFO, decode, compare with GUI), stream to host.
3. Optional IMU (context and motion gate) and EMG.

---

## 11. Acceptance tests

1. Decoding: AM62B waveform matches the GUI on the same input (correlation > 0.99).
2. No silent loss over 10 minutes: zero `seq` gaps, zero FIFO overflows.
3. Features on synthetic data recover PTT within 10 ms, PI within 5%, LF/HF within 15%.
4. Baseline adaptation: after a step change, z returns toward 0 within about 3 time constants.
5. Episode test: synthetic episode (PTT shift, PI down 40%, LF/HF up) makes `I` cross 0.5 within 3 windows of onset and fall back afterwards; a pure PTT shift of either sign raises `I`.
6. Quality gate: noise bursts, motion, and gaps produce rejected windows and leave mu unchanged.
7. Context: switching rest/active uses separate baselines (no cross-contamination).
8. Determinism: identical input gives identical output.

---

## 12. Limitations and open items

- Region identity is assumed through W and pi; it is not learned. Validate against clinical assessment before relying on any region output.
- Calf/Achilles PPG cannot observe the plantar surface, forefoot, or dorsum directly; their scores come from the hypothesis in Section 7.4.
- LF/HF requires at least about 2 minutes of clean data per estimate and is sensitive to breathing rate and posture.
- Calf PPG is sensitive to posture, leg elevation and muscle activity; use rest-session baselines and the context split.
- Pending from TI (gated): AFE4950 register map, I2C address, FIFO format, EVM user guide pinout. Also confirm that the EVM's optical front end and ECG leads can physically reach the calf (cable length, connectors).
- Starting values (window sizes, alpha, beta, thresholds) are to be tuned on real recordings.
