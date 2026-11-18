# NeuroSox / CardioTwin: developer bundle

Read `docs/NeuroSox_project_context.md` first. It covers the architecture, the formulas, what is verified, and the remaining work (D1–D11).

| Folder | Contents |
|---|---|
| `board/` | Runs on the SK-AM62B (Python standard library only). `cardiotwin_board.py` is the full pipeline and digital twin. `afe4950_linux.py` is the EVM USB driver. `.cfg` files are the EVM register profiles. `cardiotwin.service` is an untested autostart unit. |
| `laptop/` | Display only. `host_dashboard.py` and `dash_template.html` serve http://127.0.0.1:8000 from the board's output. |
| `reference/` | numpy/scipy versions used to cross-check the board code (`twin.py`, `afe_ecg_ppg.py`) and a converter/validator (`afe_validate.py`, which writes CSV). Laptop only. |
| `data/` | Raw recordings, single subject. See the table in the context doc for site and quality. `demo_session.jsonl` is several recordings joined together (demo only, not for training). |
| `docs/` | Project context, demo runbook, sensor setup guide. |

## Quick start on any Linux or Mac machine (no hardware needed)
```
cd board
python3 cardiotwin_board.py --replay ../data/ecg_ppg2.jsonl --reset --state /tmp/s.json --log /tmp/w.jsonl > ../laptop/out.log
cd ../laptop
python3 host_dashboard.py --file out.log      # then open http://127.0.0.1:8000
```

## Raw data format
One JSON object per line: `{"fifo_pointer":10, "raw_words":[11 ints], "host_time":<unix s>}`

- Decoding: `v = w - 2**24 if w >= 2**23 else w`.
- Index 6 is PPG (about 49.8 Hz). Indices 0–5 and 7–10 are ECG (about 498 Hz), in time order.
- Values are raw ADC counts. Stream roles are inferred from the data.

## Board output format
Lines `CT1 {json}`, with `type` one of:

- `hello`
- `window`: features, gates, twin I and u
- `wave`: decimated waveforms and beat times
- `hb`: heartbeat

Window records are also stored in `cardiotwin_windows.jsonl`.

## Not included
These are on the team laptop or the board SD card; add them yourself:

- `calf_01.jsonl` and `stress.jsonl`, plus notes on their site, condition and stressor timing
- any newer recordings
