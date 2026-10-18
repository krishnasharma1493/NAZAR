# NAZAR — Demo Runbook

Print this or keep it open offline. Commands are copy-paste ready.
Everything assumes `source .venv/bin/activate` from the repo root.

---

## 0 · Before leaving home

```bash
python tools/preflight.py --hardware
```
**12/12 or do not leave.** Then check the bag:

- [ ] ESP32 + **two** USB-C **data** cables
- [ ] CP-E35A + power adapter, charged
- [ ] Backup Android phone (IP Webcam, H.264, 2.4 GHz)
- [ ] Laptop + charger
- [ ] Arduino IDE on the demo laptop (in case of a re-flash)

---

## 1 · First 20 minutes on site — RF recon

```bash
python tools/venue_recon.py
```

Reads out channel occupancy, ranks candidates, then locks the busiest channel
and measures loss under this room's load. It ends with one of two verdicts.

**CLEAN** → note the channel, move on.

**LOSS DETECTED** → lower the device table, which is the only lever:

```bash
python tools/set_max_devices.py 24 --flash
python tools/venue_recon.py            # confirm it is clean now
```

Serial load is linear in the device count and NAZAR correlates one MAC at a
time, so a smaller table costs nothing that matters. If 24 is not enough, try 16.

> `MAX_DEVICES` and the sketch's `MAX_DEV` must always match. `set_max_devices.py`
> edits both and re-flashes; never edit them by hand under time pressure.

### Editing config.py by hand

Every NAZAR tool clears stale bytecode before reading config, so a hand edit
takes effect immediately. This matters because Python decides a cached `.pyc` is
valid from the source's (mtime, size) — and a **same-length** edit within the
same second changes neither. `MAX_DEVICES 40 -> 24` and `P_THRESHOLD 0.01 -> 0.05`
are exactly that shape. Reproduced 8 Sep: the file read 40 while the process
reported 24.

After any threshold change, re-run `python tools/preflight.py` — its
firmware/config drift check reads both source files textually and cannot be
fooled by a cache.

---

## 2 · Minutes 20–50 — prove the stimulus reaches the camera

Power the camera, open the **ezykam+ live view**, keep the phone awake.
Confirm **motion tracking is OFF**. Point the camera at a **still surface**.

```bash
python tools/venue_recon.py --camera-mac <camera-mac>     # find its channel
python tools/nazar_flash_test.py --channel <ch> --mac <camera-mac>
```

- **STIMULUS REACHES THE CAMERA** → go straight to the scan, move nothing.
- **STIMULUS TOO WEAK** → camera closer (0.5–1 m), filling more of its frame,
  room lights **off**, brightness **max**, nothing else moving in shot.

This costs 60 s and it is the single highest-value minute of the day. On 8 Sep a
scan failed at p = 0.10 purely because the camera was aimed at a busy scene; the
flash check found it in one minute and the re-run passed at p = 0.0055.

---

### How to sweep a room you don't know

The stimulus only works on a camera that can **see the light**. A hidden camera
in a corner that catches a split second of a passing beam misses the sequence
entirely.

**Best: bounce, don't aim.** Point the torch at a white ceiling or wall from
near the middle of the room. Diffuse light modulates the whole room at once, so
every camera sees the full pattern regardless of where it is hidden. Weaker per
camera than a direct beam, but it covers everything, and coverage is what
matters when you do not know where to point.

**Otherwise: sweep slowly and repeatedly.** The sequence is transmitted
`SEQ_REPEATS` times (default 2, 60 s total) precisely so a camera lit for only
part of the sweep still catches a full pass. Measured by simulation:

| repeats | scan | lit 100% | lit 50% | lit 25% | lit 12% |
|---|---|---|---|---|---|
| 1 | 36 s | 0.0017 | 0.013 | **0.063** | 0.33 |
| 2 | 66 s | 0.0017 | 0.0017 | 0.018 | 0.13 |
| 3 | 96 s | 0.0017 | 0.0017 | 0.0017 | 0.025 |

A camera lit for a quarter of the sweep is undetectable in one pass and
comfortable in three. For a large or cluttered room raise `SEQ_REPEATS` to 3 —
it costs 30 s per extra pass.

Do not stop moving mid-pass, and do not cut a scan short: a truncated capture is
not calibrated, and both the UI and `replay.py` will say so rather than give you
a p-value you should not trust.

## 3 · Minutes 50–90 — full scans, both directions

```bash
python tools/nazar_gate2.py --channel <ch> --mac <camera-mac>       --label venue-cam
python tools/nazar_gate2.py --channel <ch> --mac <busy-other-mac>   --label venue-control
```

**Pass:** camera WATCHING with `p < 0.01` and lag 0.05–2.0 s; control CLEAR.
**Check `drops` on both** — `table_full`, `serial`, `late_bins` must be 0.
(`unparsed` always climbs; that is normal.)

Run the control every time. It is the first thing a panel will ask for.

---

## 4 · Minutes 90–120 — rehearse the real thing

**One command:**

```bash
python tools/run_demo.py
```

Builds the UI, starts the backend, checks the board, opens the browser at
**http://127.0.0.1:8000**. One process, one port, one origin — no proxy and no
second server to forget. Ctrl-C stops everything.

```bash
python tools/run_demo.py --replay     # no hardware needed
python tools/run_demo.py --dev        # plus Vite hot reload, for editing the UI
python tools/run_demo.py --phone      # drive the stimulus from a phone, torch enabled
```

### The phone is the stimulus

```
   phone torch  ->  camera lens  ->  Wi-Fi uplink  ->  ESP32  ->  NAZAR
```

Run the UI on an Android phone over HTTPS and use its torch for both **Test**
and **Spotlight**. On the Sweep screen, press **Test torch** and watch the back
of the phone light up before committing to a 60 s scan.

The laptop-screen tools (`nazar_gate2.py`, `nazar_flash_test.py`) and the
in-browser screen flash are **fallbacks**, not the intended path. A screen
lights a room diffusely at about a metre; a torch reaches across it. Gate 2
passed on the screen flash, so the fallback is real — but do not choose it.

### Driving the stimulus from a phone

`--phone` serves on the LAN over HTTPS and prints the URL. The certificate is
self-signed, so accept the warning once, then allow camera access — that is what
unlocks the torch.

**HTTPS is not optional here.** Browsers hide `navigator.mediaDevices` entirely
outside a secure context, so over plain `http://` the torch API does not exist
and the phone silently falls back to screen flash. `--lan` gives you plain HTTP
if you only want flash mode.

**iPhones cannot do this.** iOS Safari does not expose the torch to web pages at
all. Use an Android phone, or drive the stimulus from the laptop screen — which
is what Gate 2 passed with.

The stimulus screen prints which mode it is in, and why the torch was refused if
it was. Believe that line rather than assuming.

> `--phone` and `--lan` put an unauthenticated API on the Wi-Fi. That is a
> deliberate, opt-in deviation from PRD §10's localhost-only assumption. Stop the
> server when you are done.

Drive the UI: **Sweep → Scan → Verdict → Find it → Save evidence report.**
Then hide-and-find, timed. Target: under four minutes, no retries.

> **Scan** on a candidate row starts the 90 s coded scan straight away.
> The 20 s signal check is now the small `check` link beside it —
> optional, and a diagnostic only. Do not use it in the demo: it needs the
> torch held on the lens *before* the scan, which reveals the camera and
> undercuts the point. Both things it used to guard are enforced by the
> server anyway — a camera that is not streaming video is refused by
> `POST /scan/start`, and a scan whose torch never fired returns with
> `invalid_reason` set.

### Find it — two instruments

**Live signal (RF)** is proximity. It gets you to the right part of the room and
no further: multipath means RSSI cannot separate the vent from the plant.

**Spotlight (torch)** is the one that pinpoints. Sweep the strobed torch around
the room; light is line-of-sight, so the direction that raises the camera's
bitrate is the direction the camera is in. The strongest response is a direct
hit on the lens, where flare and auto-exposure force a large re-encode.

* **Hold each spot for about five seconds.** Measured lock time is ~5 s on a 2x
  response; dwelling under 4 s essentially never locks (0% at 2 s, 8% at 3 s,
  60% at 4 s, 92% at 5 s).
* Work through the suspects: corners, vents, smoke detectors, shelves, soft
  toys, mirrors, clock radios.
* Listen rather than watch — the ticks speed up and rise in pitch as the score
  climbs, and you are looking at the room, not the phone.
* The score is a lock-in against the beam's own phase, shifted by the encoder
  lag the scan measured. A quadrature score is computed alongside it: if
  something is merely bursty it scores equally on both and the app refuses to
  call it a hit. `2.4x brighter with the beam on` under the number is the claim
  in plain terms.

Room lights off makes this dramatically better, as with the scan.

On **Live signal**, press *Start live signal* and walk. ~10 readings a second, an
auto-ranging bar, a live trace, and Geiger-style ticks that speed up as you
close in — listen rather than watch, it is easier while moving.

**Carry the sniffer, not just the phone.** The signal is measured at the ESP32,
so the laptop and board have to move with you. Walking around holding only the
phone changes nothing.

The big number is **dB from the strongest point you have found**, which is what
you actually navigate by; 0.0 means you are at the best spot so far. If it reads
`only N/s from this device`, the target is barely transmitting — open its live
view or track it while it streams.

> **Full-screen the browser before the stimulus screen.** In flash mode the page
> *is* the light source; browser chrome and a bright desktop behind it cost you
> contrast.

The header shows the board's live state — port, channel and flashed table size.
If you change `config.py` without re-flashing, a red banner says so and gives you
the command.

---

## 5 · On stage

**Hand a judge the camera and ask them to hide it while you leave the room.**
Ceding control of the hiding is what makes it undeniable, and it costs nothing,
because the method does not care where the camera is.

Then: sweep · pick the candidate · run the stimulus · read the p-value aloud ·
walk to it · save the signed report.

### What to say

> "Consumer apps scan the network you have joined. A camera on the owner's
> router, on a hidden SSID, or behind client isolation is never listed. We
> capture the air interface directly, so network membership is irrelevant.
>
> H.264 only codes what changed, so a camera's uplink bitrate rises when its
> scene changes — visible in 802.11 frame lengths even though the video is
> encrypted. We perturb the scene on a pattern we choose. Only a camera that can
> see you echoes that pattern.
>
> This is not 'the app says so'. The null hypothesis that this device cannot see
> me is rejected at p < 0.01."

### The numbers to have ready

| | |
|---|---|
| Camera | `p = 0.0055`, peak 0.439, lag 0.50 s, WATCHING |
| Negative control | CLEAR, `p = 0.4748` |
| Detector false-positive rate | 0.005 at the p < 0.01 threshold, over 400 noise runs |
| Gate 1 | 6/6 cued waves raised the bitrate, timings fixed in advance |
| Hardware | ₹659 ESP32 WROOM-32, 2.4 GHz, zero GPIO used |

---

## 6 · When something breaks

| Symptom | Cause | Fix |
|---|---|---|
| No serial port | Charge-only cable, or board unplugged | Swap to the spare **data** cable |
| `no ESP32 found` | Board renumbered or unplugged | It auto-detects; just re-plug |
| Port opens, nothing parses | Serial Monitor holding the port | Close the Arduino Serial Monitor |
| Everything reads CLEAR | Camera not streaming | Open ezykam+ live view, keep phone awake |
| Camera CLEAR, control CLEAR | Stimulus not reaching the lens | `nazar_flash_test.py`, then fix geometry |
| `table_full` / `serial` climbing | Too many devices | `set_max_devices.py 24 --flash` |
| `late_bins` climbing | Host too slow | Close everything else; this one invalidates captures |
| Edited `config.py`, nothing changed | Stale Python bytecode | Handled automatically by every NAZAR tool. If you ran something else: `rm -rf backend/__pycache__` |
| Camera dead / board dead | — | **Replay mode**, below |

### Last resort — replay mode

```bash
cd backend && NAZAR_REPLAY=scan_20260908_014034_camera3.csv \
  ../.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

The entire system — detector, API, UI — runs off a recorded capture. Real code,
real recorded data, real p-value; only the radio is missing. It is announced as
REPLAY across the UI and in every result.

**Say that you are showing a replay.** A replay through the live pipeline is
engineering practice; a video would be an admission of failure. Do not let it be
mistaken for a live sweep.

---

## 7 · Answers you will need

**"A camera with an SD card and no Wi-Fi?"** Invisible to us — it transmits
nothing, and we will not pretend otherwise. Covert cameras sold for remote
voyeurism overwhelmingly stream, because retrieving a card means going back.

**"Isn't this SnoopDog?"** Derived from it, and we cite it. SnoopDog needed
monitor-mode drivers no phone ships and asked users to do jumping jacks. We
moved capture to a ₹659 ESP32 and replaced body motion with a coded optical
stimulus worth about 12 dB of processing gain.

**"Is passive sniffing legal?"** Headers only, no decryption, no payload. A
self-defence sweep of a space you are lawfully occupying.

**"5 GHz?"** Out of scope; the ESP32 is 2.4 GHz only. The cheap covert cameras
that dominate the Indian market are 2.4 GHz.

**"What if a laptop on a video call correlates?"** Then it is a true positive —
that laptop's camera can see you. It shows the method measures the right thing.

**"Can the owner detect your sweep?"** No. The RF side is entirely passive. The
only emission is our own light.

**"Where is your auth / database?"** This is a measurement instrument, not a
service. Localhost, one user, one session. Auth would be theatre.

**"How do we know it isn't just saying WATCHING to everything?"** Show the
negative control, and the capture of *this same camera* that correctly read
CLEAR when the light could not reach its lens.
