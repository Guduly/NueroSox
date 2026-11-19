# NeuroSox: architecture for activity, inferred strain and per-area projected risk

**Audience:** the coding agent (Sonnet) that will implement this. Read all of it before editing any code.
**Status:** 2026-11-17. A first draft of the board module already exists (§2). The design and fixes below take priority over that draft.
**Working tree:** `/home/claude/work/dev/` (a copy of `neurosox_dev.zip`, plus `data/stress.jsonl`). The untouched original is at `/home/claude/work/src/neurosox_dev/`. Keep it unmodified, because it is the reference for regression tests.

---

## 0. The requirement, restated as testable rules

| # | Requirement | Rule the code must meet |
|---|---|---|
| R1 | Keep the existing features | Live dashboard, waveforms, quality gates, EMA baseline, twin I and replay all work as before. **Existing window fields must be numerically identical** to the original pipeline. |
| R2 | Activity per window (20 s window, 10 s hop) | Each window gets `act` ∈ {`sitting`, `walking`, `uncertain`}, plus a confidence and a short signal-based reason. It is computed from ECG/PPG only, with no operator labels and no filenames. |
| R3 | Inferred strain | `strain` from 0 to 1, computed from features and their trends. It is labelled "inferred strain (model index)" everywhere, **never "muscle tension"**. |
| R4 | Per-area projected risk | Each of the 7 regions has its own projection that updates every window. The mapping is explicit and configurable (`twin_config.json`). The old static prior split must not be shown as a prediction. |
| R5 | 2D single-foot graphic | Highlights regions at or above the elevated threshold. Shows the latest activity, its reason and recent history. Poor contact, insufficient data and **stale** results are shown as such, never as current. |
| R6 | Where processing runs | All computation is on the AM62B in **stdlib-only** Python. Results travel in the CT1 stream and the laptop only displays them. **CT1 v1 streams (old logs) still render.** |
| R7 | Honesty | Every new output is labelled a model projection. The final report separates software tests on synthetic data from observations on real recordings. |

Out of scope: any recovery workflow, label or event entry (D2), cloud (D11) and ML training (D7).

---

## 1. Hard constraints

- **Board code is standard-library only.** No numpy or scipy (Python 3.14 on Arago; develop and test with 3.13). The `reference/` scripts and test tooling on the laptop may use numpy and scipy.
- **Board computes, laptop displays.** `host_dashboard.py` must not compute activity, strain or risk. Formatting and staleness checks are allowed.
- **CT1 changes are additive only.** Don't rename or remove existing keys, and don't change existing values.
- **Per-window cost on the board:** under 1 s of CPU on the AM62B (Cortex-A53). On the laptop, aim for under 0.1 s per window as a proxy, and measure it.
- **Atomic, power-safe state.** Use the existing pattern: write `.tmp`, fsync, then `os.replace`. Old state files without the new keys must still load.
- **Never infer activity from filenames,** in code or in tests on real recordings.

---

## 2. Current state of the working tree (what's already done)

| File | State |
|---|---|
| `board/activity_model.py` | **Draft, new.** `Analytics` class with `activity()`, `strain()` and `areas()`, plus `DEFAULT_CONFIG`, `load_config()`, `validate_config()` and `config_summary()`. A Goertzel spectrum (stdlib). Runs end to end. Has the defects listed in §4. |
| `board/cardiotwin_board.py` | **Edited.** Imports `activity_model` and builds `Analytics` in `Pipeline.__init__`. `process_window` builds a `ctx` dict and calls `s.an.update(w, ctx)`. Twin state now persists `ext` (analytics state). `hello` is now `version: 2` with a `model` summary. New `--config` flag. |
| `board/twin_config.json` | **New.** A dump of `DEFAULT_CONFIG`. |
| `laptop/host_dashboard.py`, `laptop/dash_template.html` | **Unchanged.** All dashboard work in §6 is still to do. |
| `tests/` | **Does not exist yet.** |

Scratch outputs from the original pipeline are in `/home/claude/work/scratch/base/*.out`. The draft pipeline's outputs are in `scratch/new/*.out`. `scratch/explore/` holds numpy exploration scripts (laptop only).

---

## 3. What the recordings show (observations, not ground truth)

These are single-subject recordings with no activity labels. **No recording is known to contain walking.**

| Recording | Original gate result | Relevant signal facts |
|---|---|---|
| `ecg_ppg1` (wrist PPG, rest) | 4/4 usable | PPG cardiac purity (fraction of 0.5–4 Hz power at HR harmonics) is 0.83–0.96. Non-cardiac/cardiac amplitude ratio is 0.1–0.3. |
| `ecg_ppg2` (wrist, minor movement) | 4/4 usable | Purity 0.67–0.92 and ratio up to 0.33. |
| `ecg_ppg3` (wrist, arm raised) | 3/5 usable | The last 2 windows have ECG artifacts. Purity drops to 0.3–0.4 and ratio reaches 0.6–1.7. |
| `achilles1` (calf, hand-pressed) | 0/4, poor contact | PPG DC is about 1e5 (below the 3e5 gate). The ECG is clean. |
| `stress` (site, stressor and timing **undocumented**) | 0/17 | ECG HR is **130–148 bpm for the whole recording**. PPG purity is 0.02–0.2 in most windows, with a strong, narrow **0.6–1.1 Hz rhythm** at 1.3–2.9× the pulse amplitude. The PPG DC flips sign around window 9, which suggests the sensor was re-seated. One window (12) has a clean pulse. |

**Interpretation limits.** The 0.6–1.1 Hz rhythm in `stress` is *consistent with* a stride cadence at an ankle sensor during exercise. It could also be rhythmic movement of another kind. Report it as "the model labels these windows walking", never as "the subject walked".

**Physiology that drives the design:**
- A sensor above the Achilles moves once per **stride** (about 0.8–1.0 Hz), not once per step. The step rate (1.6–2.0 Hz) is close to the heart rate during walking, so the step harmonic is unreliable. The **stride band below the HR fundamental** is the main evidence.
- During walking the ECG picks up baseline wander from cable and electrode motion. A rhythm in the QRS-blanked ECG at the same frequency as the PPG rhythm is independent supporting evidence (seen in `stress`: ECG non-cardiac peaks at 0.7–0.85 Hz).
- Heart rate alone can't separate walking from stress, caffeine or illness. It must **never** produce "walking" by itself.

---

## 4. Defects in the draft that must be fixed

Each fix comes from running the draft on the recordings above.

| ID | Defect (observed) | Fix |
|---|---|---|
| F1 | The resting-HR floor starts at the first window's HR. In `stress` (HR 145 at t=0) `hr_elev` stays at about 0, so HR gives no evidence and strain is lower on `stress` than on resting recordings. | Start `hr_ref` from persisted state if present, otherwise from `activity.resting_hr_prior_bpm` (default 72). Let it fall quickly (rate 0.3) when HR is below it. Let it rise slowly (rate 0.02), and **only** on windows labelled `sitting` with purity ≥ 0.8. Persist it in `ext`. `--reset` resets it too. |
| F2 | Strain reaches 0.67–0.86 on resting `ecg_ppg1/2` (windows 2–3), because the twin's `u` values are large right after the 3-window warm-up (for example \|z_PAT\| = 2.2 at rest). | Use a 1-SD deadband on the twin inputs: `x_pat = max(0, u1 − 1)/2` and `x_perf = max(0, u2 − 1)/2`, each capped at 1.5. Compute the HR trend only once at least 4 windows are in history. Recalibrate weights to meet the sanity targets in §8.3. |
| F3 | The activity band is 0.7–2.6 Hz, but the observed stride rhythm sits at 0.6–0.95 Hz (band edge). | Search for the stride rhythm in **0.55–1.30 Hz**, always excluding the ECG-derived cardiac band. Add a check of the 2× harmonic. Add ECG–PPG rhythm agreement as evidence (§5.1). |
| F4 | "Sitting" is given at HR 130 (`stress` window 12) on the strength of one clean-pulse window. | `sitting` requires clean pulse evidence **and** `hr_elev < 15` bpm. Display "sitting" as **"sitting / still"**, because standing still can't be told apart. |
| F5 | "Walking" can be given on the first cadence window, without persistence. | `walking` requires the cadence in **two consecutive windows** (within 0.15 Hz). The first window becomes `uncertain`, with the reason "possible cadence, waiting for confirmation". Expected transition latency is 20–30 s. Document it. |
| F6 | `Twin.save()` runs twice per window (in `Twin.score` and in the pipeline), so there are 2 fsyncs. | Remove the save from `score`; save once in the pipeline after analytics. Keep the old behaviour where a gated window still saves. |
| F7 | `mot_sig` uses a single 0.45 Hz high-pass. That's fine, but it's computed even when the ECG is unreliable. | Compute it only when it's needed (`contact` true). Keep the cost bounded. |
| F8 | `strain_why` and `act_reason` text can get long. | Keep each under about 160 characters. Lead with the decisive evidence. |

---

## 5. Board architecture

```
AFE4950EVM ─USB─► afe4950_linux.py ─► Pipeline.add_packet ─► process_window(t0)
                                              │  (existing: filters, beats, features, gates, Twin.score → I,u)
                                              ▼
                                      ctx = WindowContext(...)            (built in cardiotwin_board.py)
                                              ▼
                              activity_model.Analytics.update(w, ctx)
                               ├─ ActivityEstimator   → act, act_conf, act_score, act_reason, act_basis, mot_*
                               ├─ StrainEstimator     → strain, strain_conf, strain_why, strain_parts
                               └─ AreaProjector       → area[7], area_q, area_reason, area_top, area_elev
                                              ▼
                              Twin.save() (twin EMAs + Analytics.dump() as "ext", atomic)
                                              ▼
                                   emit CT1 window  ─serial─► host_dashboard.py (display only)
```

**Module split.** Keep one file, `activity_model.py`, so the SD-card copy stays simple. Inside it, use three small classes plus `Analytics` as the façade. Each class keeps its own state with `dump()` and `load()`. Pure functions (`spectrum`, `ppg_rhythm`, `ecg_rhythm`, `slope_per_min`) live at module level so they can be unit tested.

**`WindowContext` keys** (a plain dict, built in `process_window` after `Twin.score`):

| key | source | when it's None |
|---|---|---|
| `t_mid` | (t0+t1)/2 | never |
| `ecg_reliable` | the "≥8 R, ≥90% plausible RR" check passed | – |
| `hr`, `rmssd` | `hr_ecg`, `rmssd_ms` | ECG not reliable |
| `ihr_lo`, `ihr_hi` | P5/P95 of instantaneous HR from `okr` | ECG not reliable |
| `ppg_dc`, `contact` | mean raw PPG over the window, `abs(dc) > DC_MIN` | – |
| `mot_sig`, `fb` | detrended, 0.45 Hz high-passed PPG over [t0, t1) | `contact` false |
| `ecg_lf` | the QRS-blanked, 6 Hz low-passed ECG decimated ×10 to the PPG rate, over [t0, t1) | ECG not reliable |
| `ppg_usable`, `ppg_reason` | `w["usable"]`, `w["reason"]` | – |
| `u` | `w["u"]` (twin inputs) | window not scored |
| `ln_pi` | `log(pi_pct)` | PI not measurable |

To build `ecg_lf`, reuse the `x` and `R` already computed in `process_window`. Low-pass `x` at 6 Hz. Linearly interpolate over −80…+120 ms around each R. Take every 10th sample. That's O(n) and stdlib only.

### 5.1 ActivityEstimator

**Inputs:** `mot_sig`, `ecg_lf`, `ihr_lo/hi`, `hr`, `contact` and `ecg_reliable`.

1. **Cardiac mask:** for harmonics h = 1..3, mask [h·ihr_lo/60 − 0.10, h·ihr_hi/60 + 0.10] Hz.
2. **PPG spectrum:** Goertzel on a 0.05 Hz grid over 0.5–4.0 Hz (71 bins × about 1000 samples, already in the draft).
   - `p_card` = power in the cardiac mask divided by total power.
   - **Stride peak:** the highest local maximum in 0.55–1.30 Hz that is outside the mask.
   - `mot_ratio` = √(P_peak / P_cardiac_fundamental_peak).
   - `mot_sharp` = P_peak / median(P over unmasked 0.55–2.6 Hz).
   - `harm2`: true if there is a local maximum within ±0.1 Hz of 2·f_peak, outside the mask, at ≥ 20% of P_peak.
3. **ECG rhythm** (only if `ecg_lf` exists): the same Goertzel over 0.55–1.30 Hz with the cardiac mask applied. `ecg_agree` is true if its strongest unmasked local maximum is within ±0.1 Hz of the PPG `mot_f` and its sharpness is ≥ 5.
4. **Evidence terms**, each clipped to the range shown. Weights live in config `activity.w`.

| term | value | range | default w |
|---|---|---|---|
| cadence | ramps from 0 at ratio 0.5 to +1 at 1.5 if sharp ≥ 5; negative ramp −1…0 if ratio < 0.3 | [−1, 1] | 1.6 |
| purity | −(p_card − 0.5)/0.3 if p_card ≥ 0.5; +0.5·(0.3 − p_card)/0.3 if below 0.3 | [−1, 0.5] | 0.8 |
| harm2 | +1 if present | {0, 1} | 0.3 |
| ecg_agree | +1 if present | {0, 1} | 0.6 |
| persistence | +1 if the previous window had cadence within 0.15 Hz | {0, 1} | 0.5 |
| hr_elev | (hr − hr_ref − 10)/20 | [−0.5, 1] | 0.6 |

   `score = sigmoid(Σ w·x)` and `conf = clip(|Σ w·x| / 2, 0, 1)`.

5. **Label rules.** These are deterministic; implement them as a small pure function `decide(evidence, basis, prev)` so they can be tested:
   - `walking`: basis `ecg+ppg`, cadence > 0, **persistence = 1**, score ≥ 0.70 and conf ≥ 0.40.
   - `sitting`: basis `ecg+ppg`, cadence < 0, p_card ≥ 0.6, hr_elev < 15, score ≤ 0.30 and conf ≥ 0.40.
   - Anything else is `uncertain`.
   - `act_basis` ∈ {`ecg+ppg`, `ecg_only`, `ppg_only`, `none`}. Anything other than `ecg+ppg` is always `uncertain`, with conf ≤ 0.3.
6. **Reason string.** List the decisive evidence first, for example: `"steady 0.85 Hz rhythm 1.9x pulse, also in ECG; same cadence as previous window; HR +38 bpm vs resting floor"`. Insufficient-data cases use fixed texts: `"PPG poor contact: cannot separate sitting from walking"`, `"ECG beats unreliable"` and `"insufficient data"`.

**State** (`dump`/`load`): `hr_ref` and `prev_mot_f` (the previous window's cadence; this one isn't persisted across restarts, so set it to None on load).

### 5.2 StrainEstimator (inferred strain index)

`load = Σ w_k·x_k` and `strain = 1 − exp(−load)`. `strain_conf` = the share of total weight whose inputs were available.

| input x_k | definition (≥ 0 only: straining direction) | default w |
|---|---|---|
| hr | clip((hr − hr_ref)/25, 0, 3) | 0.6 |
| hrv | clip(−z(ln RMSSD), 0, 3)/2. Uses its own EMA (α 0.1, warm-up 3, SD floor 0.25). | 0.25 |
| perf | max(0, u2 − 1)/2, capped at 1.5 (F2) | 0.3 |
| pat | max(0, u1 − 1)/2, capped at 1.5 (F2) | 0.2 |
| hr_trend | slope of HR over the last 6 windows, in bpm/min, divided by 10, clipped to [0, 2]. Needs ≥ 4 windows. | 0.4 |
| pi_trend | −slope(ln PI) per min / 0.15, clipped to [0, 2]. Needs ≥ 4 points. | 0.2 |
| walk | `act_score` if basis is `ecg+ppg`, otherwise 0 | 0.5 |

Output: `strain` (None if there's no reliable ECG HR), `strain_conf`, `strain_why` (the top 3 contributors in plain words) and `strain_parts` (a dict of each input's weighted contribution, rounded to 3 decimals, which the dashboard shows as a tooltip).

**Required wording** in code comments and the UI: "inferred strain, a model index of physiological load. ECG/PPG do not measure muscle tension."

### 5.3 AreaProjector (per-region projected risk)

For each region r with weights `W_r` over the feature vector `f`:

```
f = [walk, walk_exposure, strain, strain_rise, perf_drop, pat_shift]   # all in [0, ~1.5]
  walk           = act_score if basis == ecg+ppg else 0
  walk_exposure  = EMA(walk, α = 0.1)                      # persisted
  strain_rise    = clip(slope(strain)/min / 0.3, 0, 2)
  perf_drop      = x_perf (F2 version) / 1.5   (0 if unavailable)
  pat_shift      = x_pat  (F2 version) / 1.5   (0 if unavailable)
drive_r   = W_r · f
load_r    ← load_r + α_load · (drive_r − load_r)           # recent exposure, persisted
proj_r    = load_r + (drive_r − load_r) · (1 − (1 − α_load)^H)   # value after H more windows of the same drive
area_r    = sigmoid(bias_r + gain · proj_r)
```

With `H = horizon_windows` (default 12, which is 2 min), the projection answers: *if the current pattern continues for H more windows, what does the model's area index reach?* Display it with the horizon, for example "projected over the next 2 min".

**Quality states** (`area_q`):
- `full`: the window is usable, so all inputs are present.
- `partial`: the ECG is reliable but PPG inputs are missing. Compute from the available features. `area_reason` says which inputs are missing (for example "PPG poor contact").
- `none`: no reliable ECG, so strain is None. Then `area = null`, `load_r` is **not updated**, and `area_reason` = "insufficient data: …".

**Outputs:**
- `area` (7 floats, 3 decimals, in config region order)
- `area_q`
- `area_reason`
- `area_top` (the 2 strongest contributing features per region)
- `area_elev` (indices with area ≥ `elevated_threshold`)
- `area_h_s` (horizon in seconds)

**Configurability.** `region_model.features`, `regions[].w`, `bias`, `gain`, `horizon_windows`, `alpha_load` and `elevated_threshold` are all in `twin_config.json`. `validate_config` rejects unknown features and wrong weight lengths, and the board exits with that message **before** capture starts. The regions keep the existing names and order: heel, sole/arch, forefoot, dorsum/midfoot, medial, lateral, ankle/rearfoot. Every region carries a `why` string marked as a hypothesis.

**Static-ranking guard (R4).** The current setup biases differ by at most 0.4 logit. Unit-test that changing a single feature changes the **rank order** of at least 2 regions (§8.1, T-A5). That proves the ranking isn't fixed by the biases alone.

### 5.4 State file (`cardiotwin_state.json`)

```json
{"pat": {...}, "lnpi": {...},
 "ext": {"hr_ref": 72.0, "walk_exp": 0.0, "hrv": {"k":..,"m":..,"v":..,"buf":[..]}, "load": [7 floats or null]}}
```

- An old file without `ext` still loads, with fresh analytics.
- If `ext.load` has the wrong length after a config change, reset it to nulls.

---

## 6. CT1 protocol v2 (additive)

**hello:**
```json
{"type":"hello","version":2,"note":"...model projections...",
 "model":{"features":[...],"horizon_windows":12,"alpha_load":0.2,"gain":1.6,"elevated_threshold":0.5,
          "regions":[{"name":"heel","bias":-2.4,"w":[...],"why":"..."}, ...]}}
```

**window**: all v1 keys stay unchanged. Add:

| key | type | notes |
|---|---|---|
| `act` | str | `sitting` / `walking` / `uncertain` |
| `act_conf`, `act_score` | float / float or absent | |
| `act_reason`, `act_basis` | str | |
| `mot_f`, `mot_ratio`, `p_card`, `ecg_agree`, `hr_elev`, `hr_ref` | float / bool, may be absent | diagnostics for the tooltip |
| `strain`, `strain_conf` | float or null, float | |
| `strain_why`, `strain_parts` | str, {k: float} | |
| `area`, `area_q`, `area_reason`, `area_top`, `area_elev`, `area_h_s` | see §5.3 | `area` is null when `area_q == "none"` |

**Line length.** A window line grows by about 600 bytes, about 0.05 s at 115200 baud. That's acceptable because the output thread already decouples it. Don't put new fields in `wave` messages.

The `Output` thread logs any line starting with `{"type":"window"` to `cardiotwin_windows.jsonl`, so the new fields get logged automatically.

---

## 7. Dashboard architecture (laptop, display only)

### 7.1 `host_dashboard.py`
- **`ingest`:** on `hello`, store `status["board_version"] = m.get("version", 1)` and `status["model"] = m.get("model")`.
- **`build`** and `/data.json`: add `board_version`, `model`, `hb` (already there) and `now` (host wall time). Also add `last_line_age = now − status["last"]`. Window dicts pass through unchanged.
- **`page()` string-replacement anchors are fragile.** Several `h.replace(...)` calls patch exact substrings in the template. **Any template edit that changes an anchor silently disables a feature.** Do two things:
  - Add `_must_replace(h, old, new)`, which asserts `h.count(old) == 1`. Use it for every existing anchored replacement.
  - Add a test that calls `page()` and checks that it succeeds.
  - Prefer adding new behaviour directly in the template (guarded by `if(!S.length)`), not through more replacements.
- Keep the `REGIONS` constant only as the fallback name list when `model` is absent. **Delete `PI_SITE` from the display path** (R4).

### 7.2 Template components (`dash_template.html`)

Add a new card, **"Foot area projection and activity"**. Place it right after "Selected 20 s feature window", and **replace** the old "Region hypothesis" card with it. Leave the "Signal agreement" card where it is.

```
┌ Foot area projection and activity ─────────────────────────────────────────┐
│ [LATEST | HISTORICAL t=…]  [freshness badge]   projected over next 2 min    │
│ ┌───────────────┐  Activity: WALKING (conf 0.82)                            │
│ │  2D foot SVG  │  reason: steady 0.85 Hz rhythm 1.9x pulse, also in ECG …  │
│ │  7 regions    │  Inferred strain: 0.46 ▮▮▮▮▯▯  (HR above floor, walking)   │
│ │  coloured     │  Area list (sorted): heel 0.64 ▲ elevated · forefoot …     │
│ └───────────────┘  quality: partial — PPG poor contact                      │
│ History (last 30 windows): activity strip ▮▮▮▯▯ · strain sparkline · 7-row   │
│ area heat strip                                                              │
│ ▸ Active mapping (from board twin_config.json): table features × regions,    │
│   "Download edited twin_config.json" (does not change live scores)           │
│ Banner: model projections, not measured pain locations; not validated.       │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Foot SVG.** Use one right foot in plantar view, `viewBox="0 0 220 440"`, with regions as `<path id="reg-<i>">`:
- heel: the posterior oval pad
- sole/arch: the midfoot band
- forefoot: the metatarsal heads and toes
- medial: a band along the arch-side edge
- lateral: a band along the outer edge
- dorsum/midfoot: a **dashed-outline ellipse** over the midfoot, labelled "(top of foot)"
- ankle/rearfoot: a **dashed cap** above the heel, labelled "(ankle / Achilles, sensor site)"

Add an "M"/"L" orientation label. Use a generic outline only, not copied from any product or illustration. Every region has a `<title>` tooltip with its name, value, top contributors and `why`.

**Colour encoding.** Use one sequential ramp (light to dark) on `area` 0–1, readable in light and dark themes. Regions at or above `elevated_threshold` also get a thick outline and a "▲" marker in the list, so the meaning doesn't rely on colour alone.

**Display state machine** (`footState(w, freshness)`), evaluated on the **selected window**:

| condition | foot rendering | text |
|---|---|---|
| window has no `act` or `area` keys (v1 stream) | outline only, grey | "No area projection in this stream (board software v1)." Activity reads "—". |
| `freshness == "stale"` or `"no-data"` | grey hatched, values hidden | "STALE: last result at t = … s (no new window for N s)" |
| `area_q == "none"` | grey hatched | `area_reason` (for example "insufficient data: no reliable ECG heart rate") |
| `area_q == "partial"` | colours at 60% opacity + "partial" chip | `area_reason` |
| `area_q == "full"` | full colours | – |

**Never fall back to an older window's values** when the latest window lacks them. The history strip is where older values appear.

**Freshness** (computed in JS from data.json):
- `lag_board = hb.sec − last_window.t1` (board seconds, so it's correct at any `--speed`). Normally this is ≤ about 16 s (TAIL 5 s + hop 10 s + processing). Mark it **stale if > 30 s**.
- `last_line_age > 15 s` (host wall time) means **no-data** ("no data from board").
- When a historical window is selected, the badge reads "HISTORICAL" and freshness doesn't apply. When the selection is the last window, the badge reads "LATEST" and follows live updates (the existing `lastSel` logic already keeps the selection on the last window).

**History.** In the same card, show the last 30 windows (5 min). Draw them as SVG, not canvas, to match the existing code:
- an activity strip of one cell per window: walking (solid), sitting/still (light), uncertain (outlined), no data (hatched)
- a strain sparkline with gaps for null
- a 7-row × N area heat strip

Clicking a cell selects that window, reusing `sel` and `render()`.

**Existing features to keep:** tiles, waveforms (pause, zoom, beat markers), the twin timeline and table, the agreement table and the established/not-established panel. Add rows to the table view for `act`, `strain` and the max area. Update the "What is and is not established" list: activity and area projections are **not validated**, there is **no walking recording**, and the region weights are hypotheses.

**Robustness.** Every new render function starts with `if(!S.length)return;` and treats missing keys as v1. Put a `window.onerror` handler in the template that writes errors into a hidden `<pre id="jserr">`. The tests read it (§8.4).

---

## 8. Verification architecture

Create `tests/` (laptop; numpy is allowed only in `gen_synthetic.py`, but stdlib is preferred). Every test runs with `python3 -I` from the repo root.

### 8.1 `tests/test_activity_model.py` (unittest, stdlib)
- **T-A1:** `spectrum` finds a 0.9 Hz sinusoid at 49.87 Hz to within 0.05 Hz.
- **T-A2:** `ppg_rhythm` on pulse only (1.4 Hz with harmonics) gives p_card > 0.8 and mot_ratio < 0.3. On pulse plus a 0.85 Hz stride at 2× amplitude it gives mot_f ≈ 0.85 and ratio > 1.5.
- **T-A3:** the `decide()` truth table covers every rule in §5.1.5, including persistence and the `sitting`/HR guard.
- **T-A4:** `validate_config` rejects an unknown feature, a wrong weight length and alpha_load = 0. A partial override JSON merges correctly.
- **T-A5:** the static-ranking guard from §5.3.
- **T-A6:** state round trip. `dump` → JSON → `load` gives the same next output. An old state file without `ext` loads.
- **T-A7:** strain never reaches 0.4 when every input sits at baseline, and it is monotonic in each input.

### 8.2 `tests/gen_synthetic.py` and `tests/test_synthetic_replay.py` (software tests)
The generator writes raw-packet `.jsonl` in the real format:
- 11 words per packet, with index 6 = PPG.
- 10 ECG samples per packet; packets at 49.87 Hz, ECG at 498.7 Hz.
- Words stored as `v & 0xFFFFFF`, plus `host_time`.

Signals:
- **ECG:** Gaussian QRS (about 4e4 counts), a small T wave, and noise. RR follows the scenario's HR with ±3% jitter.
- **PPG:** DC 1.2e6 plus a pulse of about 3e4 counts, trough at R + 0.35 s.
- **walking segments:** a stride sinusoid plus its 2nd harmonic (f = 0.85–0.95 Hz, amplitude 2× pulse), ECG baseline wander at the stride frequency, and HR rising 75→105 over 60 s.
- **poor contact:** DC 5e4.

Scenario `sit_walk_sit.jsonl`: sit for 120 s, walk for 120 s, sit for 90 s, poor contact for 40 s, then sit for 60 s. `seed` is fixed.

Assertions, run through `cardiotwin_board.py --replay --reset` with a temporary state and log:
- **T-S1:** sit segments (excluding 30 s after each transition) are ≥ 80% `sitting`, with 0 `walking`.
- **T-S2:** walking becomes `walking` within ≤ 3 windows of onset and is ≥ 80% `walking` after that.
- **T-S3:** after walk→sit, the label leaves `walking` within ≤ 3 windows.
- **T-S4:** poor-contact windows have `act == "uncertain"`, `act_basis` = `ecg_only`, `area_q` = `partial`, and the reason mentions contact.
- **T-S5:** max(area) rises over ≥ 3 consecutive walking windows, and at least one region is in `area_elev` by the end of the walk. Area decays after it.
- **T-S6:** all v1 keys are present in every window. The run finishes with exit code 0.

### 8.3 `tests/replay_check.py` (real recordings: regression and observation)
- **Regression (R1):** for `ecg_ppg1`, `ecg_ppg2`, `ecg_ppg3`, `achilles1` and `stress`, run the **original** `src/neurosox_dev/board/cardiotwin_board.py` and the new board. Every v1 key in every window must be equal (floats within 1e-9), **including `I` and `reason`**.
- **Sanity targets.** These are behaviour checks, **not validation**, and failing them means retuning:
  - `ecg_ppg1`: no `walking`, and no region ≥ elevated threshold.
  - `ecg_ppg2`/`ecg_ppg3`: no `walking` in usable windows.
  - `achilles1`: all windows `uncertain` with basis `ecg_only` and `area_q` `partial`.
  - `stress`: runs without error. With F1, strain is higher than on `ecg_ppg1`.
- **Observation report:** a table per recording, per window with HR, p_card, mot_f, mot_ratio, act, act_conf, strain, max area, area_q and reason. Write it to `tests/out/observations.md` and **label it as observations**.
- **Timing:** total replay time for `stress` (180 s of data), compared with the original (about 1.0 s on the laptop). Report ms per window.

### 8.4 `tests/test_dashboard.py` (headless Chrome is at `/usr/bin/google-chrome`)
1. Produce CT1 logs:
   - a **v1** log from the original board (`scratch/base/ecg_ppg2.out` or a fresh run)
   - a **v2** log of the synthetic scenario
   - a **truncated v2** log that stops mid-walk, plus an hb-only tail where `hb.sec` advances 40 s past the last window (to test staleness)
2. For each log, start `host_dashboard.py --file <log> --port <free>`. Fetch `/data.json` and check its keys. Then run `google-chrome --headless=new --no-sandbox --virtual-time-budget=6000 --dump-dom http://127.0.0.1:<port>/`.
3. In the DOM, assert:
   - `#jserr` is empty.
   - For v1: the text "board software v1", the waveform SVGs are non-empty, and the timeline has bars.
   - For v2: `#reg-0`…`#reg-6` exist, and their fill differs between an early sitting window and a late walking window. Check this by appending lines to the followed file between two DOM dumps, which tests live updating.
   - For the stale log: the text "STALE".
   - For a poor-contact window: the text "PPG poor contact".
4. Save screenshots (`--screenshot`) to `tests/out/` for human review.

### 8.5 Commands (put these in the README)
```
python3 -I tests/gen_synthetic.py --out tests/out/sit_walk_sit.jsonl --seed 1
python3 -I -m unittest discover -s tests -p "test_*.py" -v
python3 -I tests/replay_check.py --orig ../src/neurosox_dev/board/cardiotwin_board.py --data data
```

---

## 9. Work order for the implementing agent

Each step has a done-criterion. Don't start the next step until the current one meets it.

1. **Regression harness first:** `tests/replay_check.py`, regression part only. *Done when* it passes against the current draft. If it fails, the draft changed v1 fields; fix that first.
2. **Refactor `activity_model.py`** into the Activity, Strain and Area classes plus `decide()`. Apply **F1–F8** and add the ECG rhythm (`ecg_lf` in the ctx). Update `DEFAULT_CONFIG` and regenerate `twin_config.json`. *Done when* the §8.1 unit tests pass.
3. **Synthetic generator and the T-S tests.** Tune thresholds **only** in config, and record every change in a comment. *Done when* T-S1–T-S6 pass **and** the §8.3 sanity targets pass. If they conflict, stop and report it; don't overfit.
4. **Host:** the `host_dashboard.py` changes and `_must_replace`. *Done when* `page()` builds and `/data.json` carries `model` and `board_version`.
5. **Template:** the foot card, state machine, history and mapping viewer, with the old region card removed. *Done when* §8.4 passes and the screenshots look right in light and dark themes.
6. **Docs:**
   - Update `README.md` (new fields and commands) and `DEMO_RUNBOOK.md` (what the foot card shows and what to say).
   - Update `NeuroSox_project_context.md`: mark D6 done in board-config form, and add the activity and strain items with honest status.
7. **Final report**, including:
   - changed files
   - commands run
   - software-test results (synthetic), **kept separate** from real-recording observations
   - timing
   - the limitations list in §10

---

## 10. Limitations the final report must state

- **No ground truth for activity.** There is no walking recording and no labels. Accuracy is unmeasured. The synthetic tests only show that the code does what it was designed to do.
- **Standing still looks like sitting.** Rhythmic non-walking movement (fidgeting, cycling, tapping) can look like walking. Walking with a loose PPG strap usually gives `uncertain` or poor contact.
- **Strain is a model index** built from cardiovascular features. It isn't muscle tension. HR also rises with emotion, caffeine, heat and illness.
- **Area projections are hand-set hypotheses.** One PPG site can't localise anything. The regions differ only because of configured weights. There are no pain labels, so no link to future discomfort is established.
- **Single subject,** with an undocumented stress protocol and site.
- **The resting HR prior** (72 bpm) is a population default until the person's own floor has been learned.

---

## 11. Quick reference: files to touch

| File | Change |
|---|---|
| `board/activity_model.py` | refactor, F1–F8, ECG rhythm, `decide()` |
| `board/cardiotwin_board.py` | `ecg_lf` in ctx, remove the double save (F6), compute `mot_sig` only when needed (F7); keep v1 fields untouched |
| `board/twin_config.json` | regenerate from `DEFAULT_CONFIG` |
| `laptop/host_dashboard.py` | hello `model`/`version`, `last_line_age`, `_must_replace`, drop `PI_SITE` from display |
| `laptop/dash_template.html` | foot card, state machine, history, mapping viewer, `#jserr`; remove the old region card |
| `tests/*` | new (§8) |
| `README.md`, `docs/DEMO_RUNBOOK.md`, `docs/NeuroSox_project_context.md` | update |
