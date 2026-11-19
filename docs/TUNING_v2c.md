# Tuning v2c (2026-11-19)

Changes are in `board/twin_config.json` only, plus one crash fix in `board/activity_model.py`.
The model code and the v1 output fields are unchanged; the regression check on the 5 old recordings still passes.

## Data
One subject (resting HR about 90 bpm on Apple Watch), run with `--rest-hr 90`:
- `sit_clean.jsonl`: seated still, 19 windows. The PPG dropped out in windows 3–5, 10 and 16–18.
- `walk3.jsonl`: walking, 21 windows. The ECG was good throughout, and the PPG was motion-corrupted, as expected.

## Crash fix
In `activity_model.py`, `Analytics.update` crashed with `round(None)` when a window had no cardiac peak in the PPG grid, which happened live while walking. A `None` value is now passed through as `null`. Regression test: `test_v2c_no_stride_peak_does_not_crash`.

## Config changes

| Key | Old | New | Why |
|---|---|---|---|
| activity.purity_low | 0.30 | 0.35 | Walking pulse share was 0.04–0.32 |
| activity.purity_still | [0.5, 0.8] | [0.35, 0.5] | Seated pulse share was mostly 0.46–0.91 |
| activity.w.purity | 0.8 | 3.0 | Pulse share was the only feature that separated the false "walking" windows while seated |
| activity.walk_score | 0.70 | 0.80 | Pairs with the purity veto |
| activity.ratio_still | 0.3 | 0.6 | Seated extra-rhythm ratio was 0.26–0.59 on clean windows; walking was at least 1.14 |
| activity.ratio_walk | [0.5, 1.5] | [0.7, 1.5] | Keeps a gap above `ratio_still` |
| activity.min_sharp | 5.0 | 4.0 | Real walking windows had sharpness 4.1–4.9 |
| activity.cad_persist_hz | 0.15 | 0.25 | Real walking cadence wobbled by up to 0.2 Hz between windows |
| strain.deadband_sd | 1.0 | 1.5 | PPG perfusion noise while seated was driving strain |
| strain.w.perf | 0.3 | 0.15 | Same |
| strain.w.pi_trend | 0.2 | 0.1 | Same |

## Result (replay with `--rest-hr 90`)

| | Sitting before | Sitting after | Walking before | Walking after |
|---|---|---|---|---|
| Labelled sitting | 1 | 6 | 0 | 0 |
| Labelled walking | 4 | 2 | 8 | 13 |
| Strain, average | 0.57 | 0.44 | 0.70 | 0.69 |
| Worst area, average | 0.50 | 0.32 | 0.67 | 0.70 |
| Windows with an elevated area | 10 | 3 | 17 | 19 |

While walking, the elevated regions are heel (19 windows), sole/arch (18) and forefoot (18).

On the older recordings, which are unlabelled: the resting ones get more sitting labels and lower area scores, and `stress` stays high (area 0.63).

## Limits
- Tuned on one person and two recordings of about 3 minutes each, and evaluated on the same data. This is an in-sample fit, not a validation.
- Two seated windows (3 and 18) still read as walking. Both had poor PPG, and their features overlap with walking.
- The region weights are unchanged hypotheses, so area scores show modelled load, not measured tissue stress.

## Reproduce
`python3 -I tools/tune_eval.py` replays every recording in `data/` with the code defaults and with `board/twin_config.json` (or `--config other.json`) and prints the counts above. The two labelled recordings are `data/sit_clean.jsonl` and `data/walk3.jsonl`.
