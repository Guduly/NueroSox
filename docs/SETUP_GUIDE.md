# CardioTwin: sensor setup and fresh recordings

Research prototype, not diagnostic.

## 0. Safety first (read before attaching anything)

- Your manual allows recordings on a person only with an instructor-approved, electrically isolated setup.
- **Safe setup:** the EVM is plugged into the **laptop running on battery, with its charger unplugged**. Nothing else connects the person to mains power.
- **Not yet approved:** the person wired to the EVM while the EVM is plugged into the SK-AM62B on its wall adapter. Do not do this until the instructor confirms it is isolated.
- Never plug or unplug cables while the electrodes are on someone. Remove the electrodes first.
- Stop if the skin becomes irritated or the person feels anything unusual.

## 1. Sensor placement

This follows the reference photo, manual Figure 8 on page 6.

### ECG: three electrodes

| Electrode | Where |
|---|---|
| 1 | Upper right chest, just below the collarbone, toward the shoulder |
| 2 | Upper left chest, just below the collarbone, toward the shoulder |
| 3 (reference) | Lower left abdomen, just below the ribs |

- Clean and dry the skin first. Press each electrode firmly.
- Use the same lead colours you used for the earlier good recordings (`ecg_ppg1–3`). The manual says only "colour-coded leads". In the photo the upper-left clip appears black and the lower clip red, but it is low resolution, so check the cable labels.
- Let the cables hang loose so they don't pull.

**Why here:** an ECG measures the heart's electrical activity as a voltage between two points. The voltage is largest when the two points are on opposite sides of the heart. The two upper-chest electrodes do that, and the third electrode is a reference that cuts noise. This gives a sharp, reliable beat (R-peak) with exact timing. Electrodes on the foot or calf would be far from the heart and close together, so they would pick up almost nothing but noise.

**Its job in CardioTwin:** the ECG is the **timing reference**. It says exactly when each heartbeat starts. It is not used to measure the foot.

### PPG: optical sensor

- **Where:** the back of the lower leg, just above the Achilles tendon, where the calf muscle narrows into the tendon. Use the soft skin there, not the bony tendon itself.
- **How:** the sensor window faces the skin. Wrap it firmly with an elastic bandage or athletic tape: snug, but not tight enough to cut off blood flow. Don't hold it by hand, because the earlier calf recording failed on exactly that.
- **Posture:** the person sits or lies still, with the leg relaxed and supported (heel resting, calf not tensed). Rest 1–2 minutes before recording.

**Why here:** PPG shines light into the skin and measures how much blood volume pulses beneath it, so it reports **local blood flow at that spot**. The Achilles area is the closest practical site to the foot and the rear-foot region. A good strap fits there, and the tissue there is related to common heel and Achilles pain.

**Its job in CardioTwin:** the twin compares the **local pulse** (PPG at the leg) with the **heartbeat timing** (ECG). The features are:
- the delay from heartbeat to pulse arrival at the leg
- the pulse strength (perfusion index)

Both are tracked against the person's own baseline.

**Limits:**
- These features are inferred and unvalidated.
- One sensor site cannot tell which part of the foot hurts.
- Any link to pain needs a study with reported pain to test.

## 2. Fresh recordings with the live graph

### Option A: safe setup (EVM on the battery laptop)

Use this one tonight. Processing for the live preview runs on the laptop, while the raw data is saved. You then replay the saved file on the board, so the board does the official processing.

**Before you start:** have the laptop on battery with the charger unplugged, the EVM plugged into the laptop, and the sensors placed as above.

1. **Laptop, Terminal B (graph):**
   ```
   cd ~/cardiotwin
   : > laptop_live.log
   python3 host_dashboard.py --file laptop_live.log --keep 900
   ```
   Open `http://127.0.0.1:8000` in the browser.

2. **Laptop, Terminal A (recording plus live preview):** pick a new file name for each recording, for example `calf_01`.
   ```
   cd ~/cardiotwin
   python3 cardiotwin_board.py --live --profile AFE4950_Tagged.cfg --reset --seconds 180 --raw-out calf_01.jsonl --state laptop_state.json --log laptop_windows.jsonl > laptop_live.log
   ```
   - If it says permission denied on `/dev/ttyACM0`, run the same command with `sudo` in front.
   - The graph appears about 25 s after the register settings load, then updates every 10 s.
   - It stops by itself after 3 minutes. To stop earlier, press Ctrl+C.

3. **Check quality while it runs.**
   - **Good:** windows are marked warm-up or scored.
   - **Bad:** windows say "PPG signal level too low (poor contact)". Then stop, retighten the strap, and record again with a new file name.

4. **Remove the electrodes**, then unplug the EVM.

5. **Process it on the board.**
   1. Copy the file to the SD card on the laptop (use your own partition name if it isn't `sda2`):
      ```
      sudo mount /dev/sda2 /mnt
      sudo cp ~/cardiotwin/calf_01.jsonl /mnt/root/afe_data/
      sync
      sudo umount /mnt
      ```
   2. Put the card in the board and boot it. Start picocom with logging in Terminal A and the dashboard in Terminal B, as usual:
      ```
      cd ~/cardiotwin
      : > pico.log
      picocom -b 115200 --logfile ~/cardiotwin/pico.log /dev/ttyUSB0
      ```
      ```
      cd ~/cardiotwin
      python3 host_dashboard.py --file pico.log --keep 900
      ```
   3. On the board (inside picocom):
      ```
      cd /root/afe_data
      python3 cardiotwin_board.py --replay calf_01.jsonl --realtime --speed 4 --reset
      ```

### Option B: live on the board

Use this only after the instructor approves isolation. It is the same as the floating-input live runs you already did, but with sensors attached:

```
cd /root/afe_data
python3 cardiotwin_board.py --live --profile AFE4950_Tagged.cfg --reset --raw-out calf_live_01.jsonl
```

Run this on the board inside picocom with `--logfile`, with `host_dashboard.py --file pico.log` running on the laptop.

## 3. Recording log

Fill in one line per recording:

| File | Date/time | Person | PPG site | Posture | Strap | Duration | Notes (movement, pain score 0–10 and location if any) |
|---|---|---|---|---|---|---|---|
| calf_01.jsonl | | | lower calf above Achilles | seated, leg supported | elastic bandage | 3 min | |
