# NeuroSox: project context and remaining tasks (for the final presentation)

This file briefs an LLM or teammate. It covers the following:

- what the team proposed in Round 1
- what has been built and verified
- what is still open
- the technical details needed to continue

Facts are labelled **verified**, **inferred** (plausible but not confirmed) or **not done**.

Status as of 2026-11-17. Scope decision: the TI Bio-Sensing GUI path is out of scope. All acquisition runs on Linux (`afe4950_linux.py`, picocom console). The final presentation is about one week after the prototype round (prototype demo: 2026-11-12, which went well).

---

## 1. Round 1 proposal (summary of the submitted document)

**Problem:**
- Chronic foot pain (heel, arch, general foot area; 3+ months) affects about 13–36% of adults. That figure is from an NIH-hosted consortium paper, "Prevalence of Foot Pain Across an International Consortium of Population-Based Cohorts".
- Clinicians diagnose the current state (gait, imaging), but not the habits and physiological changes that came before it.

**Vision:**
- A wearable at the region between the Achilles tendon and the calf.
- It collects ECG and PPG (EMG possible later) and compares them with the user's activity.
- The aim is to find patterns that precede discomfort or chronic foot pain.
- The data would go to clinicians and, anonymised, to shoe manufacturers.

**Proposed approach:**
1. ECG electrodes near the calf, plus PPG from an optical heart-rate sensor (blood flow). The document claims that higher heart rate and leg blood flow indicate leg stress.
2. Optional EMG for muscle and nerve activity.
3. The EVM acquires the signals. The Edge AI board timestamps them, extracts features and tracks trends against a baseline.
4. A digital twin combines these with **user-reported pain events** and simulates whether the user is likely to strain the leg.
5. A metrics dashboard for clinicians and shoe manufacturers.

---

## 2. Hardware and environment (verified)

**Sensor and board:**
- TI AFE4950EVM. ECG at about 498 Hz and the optical PPG channel at about 49.8 Hz. Rates are measured from packet timing, about 0.3% under nominal 500 and 50 Hz.
- PPG sensor board on cable J1. ECG uses 3 colour-coded leads with gel electrodes.

**Edge board:**
- TI SK-AM62B running Arago Linux from a 64 GB microSD, with Python 3.14 available.
- No numpy or scipy on the board, and no network. Debug console at 115200 baud over `/dev/ttyUSB0` on the laptop.

**Host:**
- Arch Linux laptop, user `dubious`. Working folders `~/afe_data` and `~/cardiotwin`.
- File transfer to the board is done by moving the SD card. Board files live in `/root/afe_data`, mounted on the laptop at `/mnt/root/afe_data` (root-only).

**EVM over USB:**
- Two CDC serial ports, `/dev/ttyACM0` and `/dev/ttyACM1`, with roles REG_EDIT and CAPTURE (queried with `C:CDC?`).
- Register profile `AFE4950_Tagged.cfg` (210 register writes, from the TI GUI).
- The protocol comes from TI firmware source (`pcComm.c`), per the teammate who wrote `afe4950_linux.py`.

---

## 3. What is built (the current system)

```
ECG electrodes (chest) ─┐
PPG sensor (lower calf) ─┴─► AFE4950EVM ──USB──► SK-AM62B: cardiotwin_board.py ──serial──► Laptop: host_dashboard.py + browser
                                                (all processing + digital twin)            (display only)
```

### Code (all Python)

- `afe4950_linux.py` (teammate's driver, standard library only)
  - Opens the EVM ports and loads the `.cfg` profile.
  - Starts and stops capture with `C:CAP_START`, `C:CAP_STOP`.
  - Parses frames into `raw_words`.

- `cardiotwin_board.py` (runs on the AM62B; standard library only, no numpy or scipy)
  - **Modes:** `--live` (from the EVM) or `--replay file.jsonl` (`--realtime`, `--speed N`). `--raw-out file.jsonl` saves the raw packets.
  - **Decode:** each packet is 11 signed 24-bit words. Position 6 is PPG (stream B); the other 10 are ECG (stream A).
  - **Filters**, written by hand (zero-phase biquads):
    - ECG: notch at 60/120/180 Hz, then 0.5–40 Hz for display and 5–20 Hz for detection.
    - PPG: detrend, then 0.7–2.5 Hz.
  - **Beats:**
    - ECG R-peaks: height at least 0.4×P99, at least 0.3 s apart, then refined.
    - PPG peaks and troughs: at least 0.4 s apart, prominence at least 0.3×SD.
  - **Window features** (20 s windows, every 10 s):
    - ECG heart rate and PPG heart rate
    - RMSSD
    - PAT: median time from R-peak to the next PPG trough, within 0.25–0.75 s
    - perfusion index: (P95 − P5 of filtered PPG) / |PPG mean| × 100
    - beat match: fraction of R-peaks with a matching PPG trough
  - **Quality gates:** a window is usable only if all of these hold:
    - ECG saturation under 5%
    - ECG SD under 3× the session median
    - at least 8 R-peaks, with 90% or more plausible RR intervals
    - |PPG DC| above 3e5 (otherwise "poor contact")
    - beat match of 0.8 or more
    - |HR_PPG − HR_ECG| of 5 bpm or less

    Unusable windows get a text reason and no score.
  - **Digital twin** (spec v2, label-free, per person):
    - EMA baseline per feature (α = 0.1). Warm-up uses the first 3 usable windows. SD floors: PAT 15 ms, ln PI 0.10.
    - Z-scores, clipped to ±4.
    - u1 = |z_PAT|, u2 = −z(ln PI), u3 = z(ln LF/HF). u3 is currently fixed at 0, because LF/HF needs at least about 60 s of beats.
    - I = sigmoid(b0 + b1·u1 + b2·u2 + b3·u3), with b = (−2.5, 0.6, 0.9, 0.6). These are fixed hand-set values, not learned.
    - Region split: softmax(W·u) with site prior π over 7 regions (heel, sole/arch, forefoot, dorsum/midfoot, medial, lateral, ankle/rearfoot). π and W are **placeholders**.
    - The baseline is saved atomically to `cardiotwin_state.json` after each window, so it is power-loss safe.
  - **Output:** one JSON line per message, `CT1 {...}`, with types hello, window, wave and hb. Window results are fsynced to `cardiotwin_windows.jsonl`.
  - **Robustness:**
    - The output thread is separate, so a slow console can't stall capture.
    - Memory is bounded for continuous runs.
    - Waveform chunks tile in exact sample indices, so there is no drift.
    - USB unplug and SIGTERM are handled cleanly.

- `host_dashboard.py` + `dash_template.html` (laptop, standard library only, display only)
  - Follows the picocom log file (`--file pico.log`) or reads the serial port directly.
  - Serves `http://127.0.0.1:8000` with:
    - ECG and PPG waveforms with beat markers
    - zoom from 5 s to 5 min, and pause/resume
    - window KPI tiles with the quality-gate reason
    - twin intensity timeline (scored, warm-up, gated)
    - region panel with editable π and W
    - an "established vs not" panel
  - Keeps 15 minutes of history (`--keep`).

- **Offline reference and analysis** (laptop, numpy/scipy): `twin.py`, `afe_ecg_ppg.py`, `afe_validate.py`. These were used to cross-check the board code.

### Run commands

- **Live on the board**, inside picocom started with `--logfile ~/cardiotwin/pico.log`:
  `python3 cardiotwin_board.py --live --profile AFE4950_Tagged.cfg --reset [--raw-out name.jsonl]`
- **Replay on the board:**
  `python3 cardiotwin_board.py --replay demo_session.jsonl --realtime --speed 6 --reset`
- **Dashboard on the laptop:**
  `python3 host_dashboard.py --file pico.log --keep 900`

### Raw data format (`.jsonl`)

One packet per line:
`{"fifo_pointer": 10, "raw_words": [11 ints], "host_time": <unix s>}`

- Decoding: `v = w - 2**24 if w >= 2**23 else w`.
- Index 6 is PPG; indices 0–5 and 7–10 are ECG, in time order.
- Values are raw ADC counts.

---

## 4. What is verified, and what is not

**Verified:**
- Packet framing and decoding are consistent.
- With electrodes on, the ECG shows clear QRS complexes with P and T waves.
- ECG and PPG heart rates agree within about 1 bpm on clean windows (for example 86.5 vs 86.7, 90.4 vs 90.7, 84.2 vs 83.1).
- On-board results match the numpy/scipy reference.
- Continuous live streaming and processing run on the AM62B. Tested for several minutes.
- The SD card file system was clean after an accidental power cut (e2fsck), and all data files parsed OK.

**Inferred, not confirmed:**
- Stream roles (A = ECG, B = PPG), from the sample rates and signal behaviour.
- PPG polarity: the trough is used as the systolic point.
- PAT as a pulse-arrival measure.
- Perfusion index meaning.
- Units: raw counts. GUI CSV scaling is about 5.722e-7 V per count (1.2 V / 2^21). The TIA gain and Rf for µA are unknown.
- Low 2 bits used as a tag in "Tagged" captures. The meaning of tag 3 is unknown.

**Coarse only:**
- Smartwatch comparison: single readings per recording. Not a ±3 bpm validation.

**Not done:**
- Per-window LF/HF.
- Any pain or stress labels.
- Any validation of the region split.
- EMG.
- Activity data (accelerometer or IMU).
- Autostart service on the board (`cardiotwin.service` exists but is untested).

---

## 5. Data collected so far (single subject: a team member)

| File | Site | Condition | Quality notes |
|---|---|---|---|
| `ecg_ppg1.jsonl` | wrist PPG, chest ECG | rest | clean |
| `ecg_ppg2.jsonl` | wrist | rest, minor movement and laughing | mostly clean |
| `ecg_ppg3.jsonl` | wrist | arm semi-raised | some artifacts |
| `achilles1.jsonl` | lower calf, PPG pressed by hand | rest | ECG clean; **PPG gated: poor contact in all windows** |
| `calf_01.jsonl` | lower calf, strapped | rest (to confirm) | not yet analysed |
| `stress.jsonl` | **site to confirm** | "stress" (**stressor, timing and duration to document**) | not yet analysed |
| `demo_session.jsonl` | joined ecg_ppg1, 2, 3 and achilles1 | demo only | not for training |

On the demo session the twin scored about 0.3 at rest and 0.04–0.12 with minor movement. With the arm raised it scored about 0.8, and it rejected every calf window as poor contact. This is a single-subject behaviour check, not evidence about pain.

---

## 6. Gaps between the Round 1 proposal and reality (address in the final)

1. **"ECG from near the calf":** the ECG needs electrodes across the heart. Two electrodes on one calf pick up almost no cardiac signal. The working setup is chest ECG (timing reference) plus PPG at the calf. Present it that way.
2. **"Increased heart rate and leg blood flow indicate leg stress":** this has not been shown with our data. Heart rate also rises with exercise, emotion and caffeine. Present it as a hypothesis to test.
3. **"User-reported pain events" feed the twin:** not implemented. There are no labels yet. This is the key missing piece for any prediction claim.
4. **"Compare with user activity":** no activity or motion sensor yet.
5. **"Predict pain regions":** one PPG site cannot localise pain. The region split is a placeholder hypothesis.
6. **"EVM processes the signals":** the EVM only acquires. All processing is on the AM62B (this is a strength; say it clearly).
7. **EMG:** not started. Mark it as optional future work.
8. **Sharing with clinicians and manufacturers:** the current system is fully local (board + laptop). There is no cloud, export, sharing or anonymisation yet. See D11.

---

## 7. Remaining tasks before the final (prioritised)

### Must do
1. **Analyse `stress.jsonl` and `calf_01.jsonl`:** check the quality-gate pass rate and the feature values. Document each recording's site, posture, stressor and timing.
2. **Reliable calf PPG:** fixed strap protocol (sensor position just above the Achilles, elastic wrap, seated, leg supported, 2 min rest). Repeat until at least 80% of windows pass the gates.
3. **Recording protocol and log:** a standard procedure plus a metadata table with file, time, subject, site, posture, condition and stressor, stress start/end, self-reported pain or stress (0–10) and location.
4. **Pain/stress self-report input:** a simple way to mark events during a recording, for example a button or keypress on the dashboard, or a timestamp list, so the twin has labels. This addresses the Round 1 "user-reported pain events" commitment.
5. **Small labelled pilot (exploratory):**
   - Rest vs stressor sessions, for example cold pressor, mental arithmetic, or calf raises/standing load.
   - Collect from a few consenting volunteers.
   - Test whether the twin intensity or the features differ between conditions, within each person.
   - Report it honestly as small-sample and exploratory.
6. **Update the slides and message:**
   - W3 done (live board pipeline).
   - Roadmap as above.
   - Remove overclaims ("delivers recommendations", "validated").

### Should do
8. **Longer resting sessions** (5 min or more), so the baseline is better and per-window LF/HF (u3) can be enabled.
9. **Board autostart and storage:** enable `cardiotwin.service`, and add rolling raw-data files (for example one file every 20–60 s) for power-loss-safe storage.
10. **Teammate's ML track: "quantized continual-learning model":**
    - Inputs could be the window features (HR_ECG, HR_PPG, RMSSD, PAT, PI, beat match) plus quality flags.
    - Labels come from task 4.
    - Must run on the AM62B (int8 quantization, small model).
    - Compare against the current fixed-prior twin as the baseline.
    - Use leave-one-session-out or leave-one-subject-out evaluation.
    - Warn about overfitting with single-subject data, and make sure it learns condition, not sensor site or session.

### Nice to have
11. Activity context: accelerometer/IMU (sitting, standing, walking) to separate exercise from strain.
12. Data export from the dashboard (CSV summary per session) as the "for clinicians" artefact.
13. EMG feasibility note: what hardware it would need and why.

---

## 8. Suggested final-presentation framing

- **Problem and vision:** unchanged from Round 1.
- **What we built:** an end-to-end edge pipeline.
  - EVM, then the AM62B doing filtering, beat detection, features, quality gates and the per-person digital twin, all on device.
  - Live dashboard.
- **Evidence:**
  - ECG/PPG heart-rate agreement of about 1 bpm.
  - The board matches the reference implementation.
  - The quality gates reject bad data.
  - Pilot results, if collected (stated as exploratory).
- **Honest limits:**
  - No validated link to pain yet.
  - The region map is a hypothesis.
  - Single-site sensing.
- **Next steps:** labelled study, cloud sharing for clinicians and manufacturers, activity sensing, EMG, ML model on the edge.

---

## 9. Open questions to fill in

- Where was the PPG for `stress.jsonl`, and what was the stressor and its timing?
- Was `calf_01.jsonl` at rest? Were the strap and position the same as planned?
- Do we have the real W and π values from spec v2, or do they stay placeholders?

---

## 10. Development steps missing from the code (concrete work items)

Every step must keep the rule that **the board computes and the laptop displays**. Board code stays standard-library Python.

### D1. Session recording on the board
- **Goal:** each recording is self-contained and survives a power cut.
- **Command:** `cardiotwin_board.py --live --session NAME --site calf --posture seated --condition rest`
- **Creates** `sessions/NAME/` on the board, containing:
  - `meta.json`: site, posture, condition, start time, software version and profile.
  - `raw_000.jsonl, raw_001.jsonl …`: raw packets rotated every 60 s. Each file is fsynced and closed, so a power cut loses at most the current minute.
  - `windows.jsonl`: per-window features, gates and twin output.
  - `events.jsonl`: labels from D2.
- **Replay:** `--replay sessions/NAME/` replays the whole folder in order.

### D2. Pain/stress event marking (labels)
- **Goal:** the "user-reported pain events" from Round 1.
- **Dashboard:** a "Mark event" panel with type (pain, stress, discomfort, none), intensity 0–10, foot region (7 regions) and a note.
- **Delivery:** the laptop sends the event to the board over the serial console, as a typed line or a small `cardiotwin_event` command. The board timestamps it in capture seconds and writes it to `events.jsonl`. The board stays the single source of truth.
- **Protocol markers:** "stressor start" and "stressor end".

### D3. Feature/label table export (for analysis and ML)
- **Tool:** `make_dataset.py sessions/*/ -o dataset.csv`, run on the laptop or the board.
- **One row per 20 s window:** session, subject, site, condition, t0, every feature, every gate flag, usable, twin I and u, and the nearest event label or protocol phase.
- **Report:** usable-window rate per session and condition, plus feature means and SDs per condition. This is the first evidence table for the final.

### D4. Live contact/quality indicator
- **Goal:** fix the sensors before recording, not after.
- **Board:** a 2 s "quick check" message, with PPG level and pulse found (yes/no), plus ECG beats found (yes/no).
- **Dashboard:** large green/red badges, "ECG OK" and "PPG contact OK", so the strap can be adjusted before recording starts.

### D5. Enable LF/HF (u3) in the twin
- Keep a rolling 120 s RR-interval buffer on the board.
- Compute LF/HF with a stdlib Lomb–Scargle (port it from `twin.py`) once at least 60 s of clean beats are available.
- Feed z(ln LF/HF) into u3, with an SD floor of 0.30.
- Gate it on beat quality.

### D6. Region model as a config file (board side)
- Move π and W from the dashboard into `twin_config.json` on the board. Include the fixed b, α and SD floors.
- The board computes the 7-region split and outputs it in each window message.
- The dashboard only shows it. Editing in the dashboard becomes "export config", which is copied to the board.
- Keep the label "hypothesis" until data supports it.

### D7. ML scorer (teammate's quantized continual model)
- **Input:** D3 features (HR_ECG, HR_PPG, RMSSD, PAT, PI, beat match, and LF/HF when D5 is done) as z-scores against the personal baseline.
- **Labels:** from D2.
- **Start small:** logistic regression or a tiny MLP. Quantize to int8, export as `model.json` (scale, zero-point, weights).
- **Board inference in pure Python:** `--model model.json` adds a second score, `P(stress/pain)`, next to the twin I.
- **Continual learning:**
  - An online update (SGD step) when a new labelled window arrives.
  - A small replay buffer of past windows to avoid forgetting.
  - Learning-rate limits.
  - The updated model is saved atomically, like the twin state.
- **Evaluation:**
  - Leave-one-session-out, or leave-one-subject-out if there are several subjects.
  - Compare with the fixed-prior twin and with chance.
  - Check it isn't just learning site or session.

### D8. Autostart and safe shutdown
- **Service:** test and enable `cardiotwin.service`, so the board starts at boot.
- **EVM detection:** wait or retry until `/dev/ttyACM*` appears.
- **Restart:** restart on failure.
- **Session name:** a default auto-generated name, for example a timestamp.
- **Shutdown:** a safe-shutdown command, `sync`, then stop the service, then `poweroff`.

### D9. Session summary export
- **Dashboard button:** "Export session", producing `summary.csv` plus a printable HTML or PDF.
- **Contents:**
  - session metadata and usable-window rate
  - feature trends
  - twin and ML scores
  - marked events
  - the limitations text
- This is the "data for clinicians" artefact from Round 1 (no clinical claims).

### D10. Regression tests
- `tests/replay_check.py` replays the stored recordings, for example `ecg_ppg1` and `ecg_ppg2`.
- It asserts:
  - HR within ±0.5 bpm of reference values
  - the same gate decisions
  - twin I within ±0.02
- Run it after every code change before copying to the SD card.

### D11. Cloud sharing layer (clinicians and shoe manufacturers)
- **Goal:** the Round 1 promise of sending data to medical professionals, and anonymously to shoe manufacturers.
- **Architecture:** the board has no network, so the laptop acts as the gateway.
  ```
  AM62B (all processing) --serial--> Laptop gateway --HTTPS--> Cloud backend --> Clinician portal
                                                                               \-> Manufacturer view (anonymised aggregates)
  ```
  Later, if the board gets Ethernet or Wi-Fi, it can upload directly.
- **What is uploaded:**
  - Derived data only, never raw waveforms by default.
  - Per-window features, quality flags, twin scores, events, and session metadata.
  - Raw data only with explicit opt-in.
- **Two access levels:**
  - **Clinician:** a user's own sessions, linked to that user (with consent).
  - **Manufacturer:** anonymised, aggregated data only: no IDs, coarse demographics, grouped by foot type or region, and minimum group sizes before showing anything.
- **Backend (simple first version):**
  - A small REST API, for example FastAPI or Flask, or a managed option such as Firebase or Supabase.
  - Endpoints:
    - `POST /sessions` (metadata)
    - `POST /sessions/{id}/windows` (batches of window rows)
    - `POST /sessions/{id}/events`
    - `GET` endpoints for the portals
  - A database (PostgreSQL or Firestore) with tables for users, sessions, windows, events and models.
- **Gateway script:** `cloud_sync.py` on the laptop.
  - Reads the session folders and windows logs (D1).
  - Uploads new rows in batches.
  - Keeps a local "already synced" marker, so it works offline and retries later.
- **Privacy and security** (needed even for a prototype):
  - HTTPS only, plus per-device API tokens.
  - Pseudonymous user IDs, with no names in the data.
  - A consent flag per user and per data type.
  - A delete-my-data endpoint.
  - The anonymisation step runs before anything reaches the manufacturer view.
- **ML link (optional):**
  - The cloud aggregates model updates from several devices (federated-style): each board updates its local model (D7), and only the weight updates are uploaded.
  - An improved global model is sent back.
  - Raw personal data stays on the device.
- **Portal:** a web page per role:
  - **Clinician:** session list, trends, events, the D9 summary.
  - **Manufacturer:** aggregate charts.
- **Prototype scope for the final:** one backend, the gateway sync, a clinician view of one user's sessions, and one anonymised aggregate chart. State that it is not a certified medical-data system.

### Suggested order for the final week
D10 → D1 → D4 → D2 → D3 → (collect labelled sessions) → D11 (basic sync + clinician view) → D5 → D6 → D7 → D9 → D8.

**Minimum for a strong final:** D1, D2, D3, D4, plus a few labelled sessions and their evidence table.
