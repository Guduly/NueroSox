# CardioTwin live demo: runbook for the operator

**Roles:** the operator runs the computer. The test subject wears the sensors and sits still.

**Where things run:**
- "LAPTOP" means a terminal on the Arch laptop.
- "BOARD" means commands typed inside picocom, which is the AM62B.
- The board does all the processing. The laptop only shows the graph.

---

## A. Before the demo (about 5 min)

**Safety:** only wire the subject to the EVM in this setup if the instructor has approved it. Never plug or unplug any cable while electrodes are on the subject.

1. **Cables**
   - SD card in the AM62B.
   - EVM USB cable into the AM62B's USB-A port.
   - AM62B debug USB cable into the laptop.
   - AM62B power adapter plugged in.
   - PPG sensor cable into the EVM (J1).
2. Power on the AM62B and wait about 1 minute.

3. **LAPTOP, Terminal A (board console with logging)**
   ```
   cd ~/cardiotwin
   : > pico.log
   picocom -b 115200 --logfile ~/cardiotwin/pico.log /dev/ttyUSB0
   ```
   Press Enter. If it asks for a login, type `root` and press Enter.

4. **BOARD (inside Terminal A): check the EVM is detected**
   ```
   cd /root/afe_data
   ls /dev/ttyACM*
   ```
   It must show `/dev/ttyACM0  /dev/ttyACM1`. If it shows nothing, unplug and replug the EVM USB at the board (no electrodes on the subject) and check again.

5. **LAPTOP, Terminal B (new terminal): the dashboard**
   ```
   cd ~/cardiotwin
   python3 host_dashboard.py --file pico.log --keep 900
   ```
   Open **http://127.0.0.1:8000** in the browser. It says "waiting for data" for now. Leave this terminal alone.

6. **Subject: put the sensors on.**
   - **ECG electrodes:** one on the upper right chest and one on the upper left chest, each just below the collarbone. One on the lower left belly, below the ribs.
   - **PPG sensor:** on the back of the lower calf just above the Achilles tendon, firmly wrapped with a bandage or tape.
   - Subject sits still, leg relaxed and supported.

---

## B. Run the live demo

7. **BOARD (in Terminal A), type exactly:**
   ```
   python3 cardiotwin_board.py --live --profile AFE4950_Tagged.cfg --reset
   ```

8. **What happens, and when:**

   | Time | What you see |
   |---|---|
   | 0–5 s | `Applied 50/210 … 210/210 register settings` |
   | ~25 s | First waveforms appear in the browser |
   | ~25–55 s | First 3 good windows are **baseline warm-up** (white bars) |
   | after ~55 s | **Scored** windows (blue bars) every 10 s |

   **Recommended:** add `--calibrate-rest 60` (the subject sits still for the first minute so the board learns their own resting heart rate), or `--rest-hr 88` if you know it. Add `--raw-out /root/afe_data/run1.jsonl` to also save the raw data for replay.

   Terminal A fills with lines of numbers. That is normal, so don't type in it.

9. **During the talk, in the browser (Live tab, one screen):**
   - **Top row:** activity (sitting / walking / uncertain), inferred strain, heart rate, resting level, signal quality, then the current window's pulse rate, HRV, pulse arrival, perfusion and beat match.
   - **Foot (left):** regions shaded by the board's *model projection* for the next 2 min. Darker = more projected load; an orange dot means the region is above the alert level. "Top of foot" and "ankle" are not on the sole, so they are shown as dots beside it. Hatched grey means no data, stale or poor contact. Never call a grey foot "no risk".
   - **Signals (right):** live ECG and pulse. **Pause** freezes them (data keeps recording); the slider and length menu move and zoom.
   - **Last 5 minutes (bottom):** click a column to review that moment (badge changes to "Reviewing"; click "Back to live").
   - If the board stops, a red bar says how long it has been, and the values are hidden rather than shown as current.
   - **History tab:** full region heatmap and twin intensity. **Model tab:** region weights and how to read the page.
   - Walking needs two consecutive windows, so it appears 20-30 s after a change. Inferred strain is a model index, **not muscle tension**.
   - **What to say:** "These are model projections from one sensor, not measured pain locations. Thresholds were tuned on one person's sitting and walking recordings and are not validated."
   - **Do not say:** that it detects pain, muscle tension or injury, or that the subject "walked" (say "the model labels this walking").
   - *Optional:* the subject can do something that changes physiology, such as raising an arm or a few deep breaths, to show the score changing. Change only one thing.

10. **Stop**
    - **BOARD (in Terminal A):** press **Ctrl+C** once and wait for the prompt `root@am62xx-evm:~/afe_data#`.
    - Remove the electrodes from the subject before unplugging anything.
    - Leave the browser open. The graph stays visible.

---

## C. Backup: if live does not work, use a recording (still processed on the board)

If the live run errors out, shows no graph after 60 s, or the signal is unusable:

**BOARD (in Terminal A).** If something is still running, press Ctrl+C first. Then:
```
cd /root/afe_data
python3 cardiotwin_board.py --replay demo_session.jsonl --realtime --speed 6 --reset
```
This plays four earlier real recordings: rest, movement, arm raised, and calf. It takes about 45 s. The dashboard clears and redraws by itself. You don't need to touch Terminal B.

**What to say:** "This is a recorded session; the board is doing all the processing live from the file."

Other recordings on the board can be replayed the same way: `calf_01.jsonl` and `stress.jsonl`.

---

## D. Quick fixes

| Problem | Fix |
|---|---|
| `picocom` says the port is busy | Another picocom or dashboard is using `/dev/ttyUSB0`. Close other terminals and retry step 3. |
| Browser says "host server not reachable" | Terminal B was closed. Rerun step 5. |
| Browser stuck on "waiting for data" | Picocom must be started with `--logfile` (step 3). Restart it that way, then rerun step 7. |
| `FileNotFoundError: /dev/ttyACM0` | EVM not detected. With no electrodes on the subject, replug the EVM USB at the board and check step 4. |
| All windows say "PPG signal level too low" | Tighten the calf strap. Press Ctrl+C and rerun step 7. |
| All windows say "ECG beats not reliable" | Check the electrodes are stuck firmly and the leads are connected. The subject must sit still. |
| Graph stops updating | Check that Terminal A is still scrolling. If it stopped, rerun step 7. If it is still scrolling, reload the browser page. |
| Anything else | Switch to the backup (section C). |

---

## E. Reset between runs
- **Fresh run:** Ctrl+C in Terminal A, then rerun step 7. The dashboard clears itself.
- **Keep the learned baseline:** leave out `--reset` in step 7.
