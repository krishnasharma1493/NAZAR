# NAZAR — Product Requirements & Build Blueprint

### Wi-Fi Causality Detection of Concealed Cameras

> **Version:** 1.0
> **Document snapshot:** Written 6 Sep 2026. Schedule and gates are dated; treat the milestone table as authoritative and everything else as reference.
> **Audience:** The four-person build team, AI coding assistants, and any judge who asks how this was planned.
> **Event:** Nari Kavach 2026 · Theme 04 Cyber Safety · IEEE WiE, Faculty of Technology, University of Delhi
> **Finale:** 9 Sep 2026 · 160 minutes on site · live demo to a faculty panel + Q&A
> **Purpose:** One source of truth for scope, architecture, config, contracts, gates and the demo runbook.

---

## ⚠️ Read First — Two Rules

### Rule 1: The Gate 1 kill switch

**By the night of 7 Sep you must be able to plot a camera's uplink bitrate rising when you wave your hand in front of it.**

If that does not happen, NAZAR is dead and you switch to the software-only fallback that night. Do not spend the 8th hoping. Every hour after a failed Gate 1 is an hour stolen from a project that could still ship.

### Rule 2: Never pin a version from this document

No dependency version appears anywhere in this file, deliberately. Before installing anything:

```bash
pip index versions <package>      # or check pypi.org
pip install <package>             # latest
pip-audit                         # once, before the 9th
```

For an AI assistant working from this file: search for the current version and state what you verified. Do not invent semver strings. This costs ten minutes and is the *only* dependency hygiene this project warrants — there is no production deployment to protect.

---

## Table of Contents

1. [Product Definition](#1-product-definition)
2. [Scope — Goals and Non-Goals](#2-scope--goals-and-non-goals)
3. [Functional Requirements](#3-functional-requirements)
4. [Tech Stack](#4-tech-stack)
5. [Repository Structure](#5-repository-structure)
6. [Configuration](#6-configuration)
7. [Firmware Architecture](#7-firmware-architecture)
8. [Detection Pipeline](#8-detection-pipeline)
9. [Data Contracts](#9-data-contracts)
10. [API Documentation Standard](#10-api-documentation-standard)
11. [Frontend](#11-frontend)
12. [Testing & Validation Guide](#12-testing--validation-guide)
13. [Pre-Demo Checklist](#13-pre-demo-checklist)
14. [Milestones & Gates](#14-milestones--gates)
15. [Demo Runbook](#15-demo-runbook)
16. [Q&A Preparation](#16-qa-preparation)
17. [Risk Register](#17-risk-register)
18. [Deliberately Out of Scope](#18-deliberately-out-of-scope)
19. [Document Maintenance](#19-document-maintenance)

---

## 1. Product Definition

**One line:** A phone-guided sweep that finds the hidden camera watching you, and proves statistically that it is watching *you* — not merely that a device exists.

**The mechanism, in three sentences.** H.264/H.265 encoders only code what changed since the last frame, so a camera's uplink bitrate rises when its scene changes. That bitrate is visible in 802.11 frame lengths and timestamps even though the video payload is encrypted. If we perturb the scene on a pattern we choose, only a camera that can see the user will echo that pattern in its traffic — and a matched filter plus a permutation test turns that echo into a p-value.

**Why this is different from everything shipping.** Consumer apps (Fing, SpyFy, Sweep) scan devices on the network you have joined. A camera on the property owner's router, on a hidden SSID, or behind AP client isolation is never listed. NAZAR captures the air interface directly, so network membership is irrelevant.

**Prior art, disclosed.** Scene–traffic correlation is established in the literature (DeWiCam 2018, SnoopDog 2020, MotionCompass 2021, LocCams 2023). None of it shipped, for two reasons: it required monitor-mode Wi-Fi drivers that phones do not have, and it required exaggerated user movement. Our contribution is practicality — a ₹659 ESP32 replaces the monitor-mode laptop, and a coded optical stimulus replaces the jumping jacks.

---

## 2. Scope — Goals and Non-Goals

### Goals (must be true on 9 Sep)

| # | Goal |
|---|------|
| G1 | Detect a streaming 2.4 GHz Wi-Fi camera in a room, with a p-value, in under 60 seconds |
| G2 | Work without joining the camera's network |
| G3 | Give the user a direction to walk in |
| G4 | Produce a signed, timestamped evidence file |
| G5 | Survive a judge hiding the camera while the team is out of the room |

### Non-goals (state these plainly; do not let scope creep back in)

| # | Non-goal | Why |
|---|----------|-----|
| N1 | Offline / SD-card cameras | They transmit nothing. No signal exists to measure. |
| N2 | 5 GHz coverage | ESP32 is 2.4 GHz only. Second radio = future work. |
| N3 | Metre-level localisation | RSSI is multipath-ridden. Direction only. |
| N4 | Native mobile app | Laptop UI demos identically and is far more reliable. |
| N5 | Multi-user, accounts, cloud, persistence | Single user, one laptop, eight-day lifespan. |

---

## 3. Functional Requirements

| ID | Requirement | Priority | Owner |
|----|-------------|----------|-------|
| F1 | ESP32 captures 802.11 data-frame headers in promiscuous mode | P0 | Firmware |
| F2 | Aggregate per-MAC into 100 ms bins on-device; emit CSV over USB serial | P0 | Firmware |
| F3 | Channel scan (hop 1–13) to locate candidate devices | P0 | Firmware |
| F4 | Channel lock before any measurement | P0 | Firmware |
| F5 | Discovery filter: uplink dominance + sustained rate + OUI lookup | P0 | Backend |
| F6 | Phone torch strobes a 15-chip m-sequence, 2 s/chip, repeated `SEQ_REPEATS` times so a camera lit for only part of a room sweep still catches a full pass; transitions timestamped and posted back | P0 | Frontend |
| F7 | Matched filter + surrogate-sequence permutation test → peak, lag, p-value | P0 | Backend |
| F8 | Verdict rule: `p < 0.01 AND 0.05s < lag < 2.0s` | P0 | Backend |
| F9 | Every capture written to disk; replay mode runs the identical pipeline | P0 | Backend |
| F10 | RSSI hot/cold indicator while walking — live SSE stream at ~8 Hz, EWMA smoothed, peak-hold | P1 | Backend |
| F11 | Body-shadow bearing: rotate 360°, fit cosine, show arrow | P1 | Backend |
| F11b | **Optical spotlight**: strobed torch swept around the room, camera uplink scored by lock-in against the beam's phase at the measured encoder lag, with a quadrature control. Pinpoints the object where RSSI can only give proximity. | P1 | Frontend + Backend |
| F12 | Gradient-boosted classifier as a pre-filter (camera / other) | P1 | ML |
| F13 | Evidence report: JSON + PDF, SHA-256 hashed, Ed25519 signed | P1 | Backend |
| F14 | IR / lens-glint fallback scan | P2 | — |

**Priority rule:** nothing at P2 starts until every P0 passes on real hardware. F14 is almost certainly not happening; it exists in this table so that the Q&A answer is "descoped deliberately," not "we forgot."

---

## 4. Tech Stack

> No version numbers. See ⚠️ Rule 2.

| Layer | Choice | Notes |
|-------|--------|-------|
| **Microcontroller** | **ESP32 WROOM-32 C Type CH340 USB Dual Core WiFi + Bluetooth (30 Pins), OceanLabz** | Chip: ESP32-D0WDQ6. Classic ESP32, **not** S3. 2.4 GHz (802.11 b/g/n), dual core @ 240 MHz, 520 KB SRAM, 4 MB flash. Ships pre-loaded with a vendor demo sketch — overwritten on first upload, ignore it. |
| **USB bridge** | CH340C, USB Type-C | Driver must be installed **before** first plug-in. No port appears without it. |
| **Firmware IDE** | **Arduino IDE 2.x** | Board: *ESP32 Dev Module*. Add via Boards Manager → "esp32" (Espressif Systems). Sketch folder name must match the `.ino` filename. |
| **Firmware language** | C++ (Arduino core for ESP32) | `setup()` / `loop()`; `loop()` runs on core 1, Wi-Fi driver on core 0 |
| **Capture API** | `esp_wifi_set_promiscuous()` | ESP-IDF call, reachable from Arduino via `#include "esp_wifi.h"` |
| **Transport** | USB serial, 460800 baud | Not BLE. Wired is the demo-safe choice. Dropped from 921600 — see §6. |
| **Serial client** | Python + pyserial | |
| **Signal processing** | NumPy, SciPy | Matched filter, median detrend, permutation test |
| **Classification** | scikit-learn, gradient boosting | Pre-filter only, not the proof |
| **API layer** | FastAPI | Binds `127.0.0.1` — no CORS, no auth |
| **Frontend** | React (Vite), plain JS | Four screens, no router needed |
| **Evidence** | SHA-256, Ed25519, PDF renderer | `cryptography` + a PDF lib |
| **Storage** | Flat files — CSV and JSON on disk | No database |

**Hardware BOM (actual, as purchased):**

| Item | Qty | Notes |
|------|-----|-------|
| **ESP32 WROOM-32 C Type CH340 USB Dual Core WiFi + Bluetooth (30 Pins) \| Pre-Loaded with Tested Code** (OceanLabz) | 1 | ₹659. 30-pin DevKit, on-board Type-C, CH340C bridge, EN + BOOT buttons, on-board 2.4 GHz antenna. Pin count is irrelevant — NAZAR uses zero GPIO. |
| **CP Plus CP-E35A** — 3MP PT Wi-Fi camera, 4 mm lens, IR, DC 5V 1A Type-C | 1 | **2.4 GHz only** (cannot join 5 GHz — confirmed in the CP Plus manual). Pairs via the **ezykam+** app. **Disable motion tracking / auto-tracking before any test** — a pan-tilt camera that rotates changes the whole frame and destroys the correlation. Point it at a still surface, not a window. |
| Secondary Android phone (IP Webcam app) | 1 | Backup target; set H.264, force 2.4 GHz, keep screen awake |
| Type-C data cable | 1 | Must be a data cable — test by transferring a file |
| Laptop | 1 | Runs everything |

**No soldering. No breadboard. No jumper wires. NAZAR uses zero GPIO pins.**

---

## 5. Repository Structure

```
nazar/
├── firmware/
│   └── nazar_sniffer/
│       └── nazar_sniffer.ino          # promiscuous capture + 100 ms aggregation + serial CSV
│
├── backend/
│   ├── main.py                        # FastAPI app, route mounts
│   ├── config.py                      # ALL tunables — single source of truth
│   ├── serial_reader.py               # port → 100 ms bins, writes capture CSV
│   ├── discovery.py                   # OUI table + uplink-dominance filter
│   ├── classifier.py                  # train() and predict()
│   ├── detector.py                    # matched filter + permutation test  ← the core
│   ├── localise.py                    # RSSI EWMA + body-shadow bearing fit
│   ├── evidence.py                    # hash, sign, render PDF
│   ├── replay.py                      # run the pipeline on a saved capture
│   ├── captures/                      # raw CSVs (gitignored)
│   └── evidence/                      # signed reports (gitignored)
│
├── frontend/
│   └── src/
│       ├── App.jsx                    # four screens
│       ├── api.js                     # fetch wrappers
│       └── Stimulus.jsx               # torch strobe controller + timestamp log
│
├── data/training/                     # labelled captures for the classifier (gitignored)
├── tools/
│   └── nazar_read.py                  # standalone Gate 1 live-view tool
├── docs/
│   └── NAZAR_PRD.md                   # this file
└── README.md                          # flash, run, demo — written for a stranger
```

### `.gitignore`

```gitignore
# Captures and derived data — large, machine-specific
captures/
evidence/
data/

# Signing keys — never commit
signing_key*
*.pem
*.key

# Python
__pycache__/
*.pyc
.venv/
venv/

# Node
node_modules/
dist/

# OS
.DS_Store
```

**Rules:**
- Commit source, `README`, `config.py`, `requirements.txt`, `docs/`.
- Never commit captures (large, meaningless out of context) or the Ed25519 signing key.
- Generate the signing key once with a script; keep it local; document that fact in the README.

---

## 6. Configuration

Every tunable lives in one file. On 9 Sep you will have ~30 minutes to recalibrate in the venue; if thresholds are scattered inline across the detector, you will not manage it.

```python
# backend/config.py

# ── Serial ───────────────────────────────────────────────────────────
SERIAL_PORT     = "auto"                    # auto-detect the CH340 by USB ID; macOS renumbers
                                            # /dev/cu.usbserial-* between plug-ins (seen as -110 and -10)
SERIAL_BAUD     = 460800                    # dropped from 921600 — CH340C on this board corrupts above ~460k;
                                            # measured 0/2152 valid lines at 921600 vs 1 corrupt at 460800

# ── Capture ──────────────────────────────────────────────────────────
BIN_MS          = 100                       # must match firmware BIN_MS
CHANNELS        = range(1, 14)
SCAN_DWELL_MS   = 600
MAX_DEVICES     = 40                        # must match firmware MAX_DEV

# ── Discovery filter ─────────────────────────────────────────────────
UPLINK_RATIO    = 3.0        # up_bytes / (dn_bytes + 1)
MIN_KBPS        = 100        # sustained uplink to be a candidate
MIN_SUSTAIN_S   = 10

# ── Stimulus ─────────────────────────────────────────────────────────
M_SEQUENCE      = [1,1,1,1,0,1,0,1,1,0,0,1,0,0,0]   # length-15, near-ideal autocorrelation
CHIP_S          = 2.0        # ≥ encoder GOP settling time
SCAN_S          = CHIP_S * len(M_SEQUENCE)          # 30 s
STIM_MODE       = "edge"     # legacy single-shape pin; normally unset
STIM_SHAPES     = ("level", "edge")   # BOTH are searched. Geometry decides
                            # which the camera shows: a torch held in the lens
                            # gives a sustained level change, off-axis light a
                            # transient at each edge. Measured 8 Sep on one
                            # capture: level p=0.0015, edge p=0.10; on fixture
                            # s_002 the reverse. Combined by Westfall-Young
                            # minP so each shape is ranked against its OWN
                            # null, and every surrogate goes through the same
                            # reduction — the multiple-comparison cost is paid.
EDGE_WIN_S      = 1.0        # response boxcar after each transition

# ── Detector ─────────────────────────────────────────────────────────
MAX_LAG_S       = 3.0
LAG_WINDOW      = (-0.8, 2.2)   # SIGNED. Positive is the physical encode +
                                # transmit delay. Negative lags are searched
                                # because the phone and laptop keep separate
                                # clocks and the browser torch call has its own
                                # latency. Measured 8 Sep: a 2.3x response
                                # peaked at -0.5 s and scored CLEAR (p=0.83)
                                # only because the window began at +0.05 s.
                                # Surrogates search the identical window, so
                                # this costs power, not calibration:
                                # P(p<0.01 | noise) = 0.0025 over 800 runs.
N_PERMUTATIONS  = 2000
P_THRESHOLD     = 0.01
DETREND_WIN     = 51            # median filter window, in bins

# ── Localisation ─────────────────────────────────────────────────────
RSSI_ALPHA      = 0.2           # EWMA smoothing
BEARING_SWEEP_S = 20

# ── Evidence ─────────────────────────────────────────────────────────
SIGNING_KEY_PATH = "signing_key.pem"
SOFTWARE_VERSION = "nazar-1.0"
```

**Firmware constants must be kept in sync manually** — `BIN_MS` and `MAX_DEV` appear in both `nazar_sniffer.ino` and `config.py`. Note it in the README; there is no build system to enforce it and inventing one is not worth the time.

---

## 7. Firmware Architecture

### Toolchain and first-run procedure

| Step | Action | Success signal |
|------|--------|----------------|
| 1 | Install the **CH340** driver, restart | — |
| 2 | Install **Arduino IDE 2.x** | — |
| 3 | Boards Manager → search `esp32` → install **Espressif Systems** package | — |
| 4 | Tools → Board → **ESP32 Dev Module**; Tools → Port → `COM*` / `/dev/cu.usbserial-*`; Tools → **Upload Speed → 460800** | A port appears |
| 5 | Upload **Blink** (`LED_BUILTIN` → pin `2`) | Blue LED blinks |
| 6 | Upload `nazar_sniffer.ino` (folder name must match) | `#NAZAR_SNIFFER_READY` at 460800 baud |

**Do step 5 before step 6.** It separates "the board is broken" from "my code is wrong," and you will want that certainty at midnight on the 8th.

**If no port appears:** the CH340 driver did not install, or the cable is charge-only. Nothing else causes this. Test the cable by transferring a file from your phone.

**If upload fails with "Failed to connect":** hold **BOOT**, tap **EN**, release EN, keep holding BOOT until the upload bar moves. The auto-reset handshake over the CH340 control lines occasionally fails; the two buttons are the manual override.

**The Arduino Serial Monitor and the Python reader cannot both hold the port.** Close the Serial Monitor before running Python. This will catch someone on the team at least once.

### Producer / consumer split

```
callback  (core 0, ~2000×/sec)  →  accumulate bytes into fixed table   [fast, silent]
loop()    (core 1, 10×/sec)     →  snapshot, zero, print CSV           [slow, chatty]
```

This split is the whole design. The callback never prints, never allocates, never blocks — printing inside it would stall the Wi-Fi driver and drop the very packets the correlation needs.

### Frame parsing

802.11 data frame layout:

```
[ FrameCtl 2 ][ Duration 2 ][ Addr1 6 ][ Addr2 6 ][ Addr3 6 ][ Seq 2 ][ encrypted payload ][ FCS 4 ]
```

Direction bits in `FrameCtl` byte 1 determine which address field identifies the device in the room:

| ToDS | FromDS | Direction | Device is | Offset |
|------|--------|-----------|-----------|--------|
| 1 | 0 | station → AP (**uplink**) | Addr2 | 10 |
| 0 | 1 | AP → station (downlink) | Addr1 | 4 |
| other | | ad-hoc / WDS | ignored | — |

**Uplink is the signal.** A camera uploads video; its uplink bitrate is what the entire project measures.

### Concurrency

The device table is shared between two cores. All reads and writes are wrapped in `portENTER_CRITICAL` / `portEXIT_CRITICAL`. `flushBin()` holds the lock only long enough to `memcpy` a snapshot and zero the counters, then releases before printing.

A lost `+=` between cores is a wrong byte count → wrong bitrate → failed detection, with no error message anywhere. This is the single highest-consequence bug class in the firmware.

### Memory

`dev_t table[40]` ≈ 1.3 KB, statically allocated, cannot grow. Table full → packet dropped. Predictable beats complete.

### Serial command protocol

| Command | Effect |
|---------|--------|
| `S` | Channel scan: hop 1–13, emit `S,` lines per channel |
| `C<n>` | Lock channel *n*, begin streaming `D,` lines |
| `?` | Print current state |

---

## 8. Detection Pipeline

Six stages. Stage 4 is the product; everything else is plumbing.

| # | Stage | What it does |
|---|-------|--------------|
| 1 | **Discover** | Enumerate MACs on air; filter by uplink dominance, sustained rate, OUI |
| 2 | **Classify** | Gradient boosting narrows candidates. *A filter, not the proof.* |
| 3 | **Stimulate** | Torch strobes the m-sequence; chip transitions timestamped and synced to the capture clock |
| 4 | **Correlate** | Detrend, robust-normalise, matched-filter against the stimulus, permutation test |
| 5 | **Locate** | RSSI EWMA gradient; body-shadow bearing null |
| 6 | **Prove** | Hash + sign the full measurement record |

### The detector, in full

```python
import numpy as np
from scipy.ndimage import median_filter
from config import BIN_MS, MAX_LAG_S, LAG_WINDOW, N_PERMUTATIONS, P_THRESHOLD, DETREND_WIN

FS = 1000 // BIN_MS          # bins per second

def detect(bitrate, stim):
    """bitrate: up_bytes per bin.  stim: ±1 per bin, time-aligned."""
    # 1. Remove slow drift from unrelated traffic
    z = bitrate - median_filter(bitrate, size=DETREND_WIN)

    # 2. Robust normalise — MAD, not std, so one burst can't dominate
    mad = 1.4826 * np.median(np.abs(z - np.median(z)))
    z = z / (mad + 1e-9)

    s = stim - stim.mean()
    n, L = len(z), int(MAX_LAG_S * FS)

    # CIRCULAR cross-correlation, so that every shift is exactly exchangeable
    R = np.fft.irfft(np.fft.rfft(z) * np.conj(np.fft.rfft(s)), n)
    R = R / (np.linalg.norm(z) * np.linalg.norm(s) + 1e-9)

    # Search only lags that could produce a verdict. The verdict rule already
    # refuses anything outside LAG_WINDOW, so lags beyond it cannot win a
    # detection but do raise the null's maximum. Observed and surrogates are
    # searched over the identical window, so calibration is unchanged: measured
    # false-positive rate 0.005 at p < 0.01 over 400 noise runs.
    lo_i, hi_i = int(LAG_WINDOW[0] * FS), int(LAG_WINDOW[1] * FS) + 1
    peak = R[lo_i:hi_i].max()
    lag = (lo_i + R[lo_i:hi_i].argmax()) / FS

    # 3. Empirical null: sequences we never transmitted (see note below)
    null = np.array([corr_peak(z, surrogate) for surrogate in null_stims])
    p = (np.sum(null >= peak) + 1) / (len(null) + 1)

    return {
        "peak": float(peak),
        "lag_s": float(lag),
        "p_value": float(p),
        "verdict": "WATCHING" if (p < P_THRESHOLD and
                                  LAG_WINDOW[0] < lag < LAG_WINDOW[1]) else "CLEAR",
    }
```

**Why the stimulus regressor encodes transitions, not chip level.** An H.264
encoder codes what *changed*. A white-to-black transition is therefore as strong
a stimulus as black-to-white, and the bitrate rises at both. Correlating the
bitrate against the chip *level* averages the rising and falling responses
toward zero. Measured on the CP-E35A, 7 Sep, 16 transitions: level gave 6/8
consistency (sign test p = 0.14), transitions gave 13/16 (p = 0.0106), with the
response peaking 0.5-0.8 s after each edge. `detect()` is unchanged — this is
how the caller builds `stim`. The m-sequence is unaffected: by the shift-and-add
property its transition train is itself a shift of an m-sequence, so the
thumbtack autocorrelation survives.

**Why the m-sequence and not a plain square wave.** An m-sequence has a near-ideal thumbtack autocorrelation — one sharp peak, negligible sidelobes. A square wave correlates strongly with any periodic interference in the room. The sequence buys roughly 12 dB of processing gain, which is the difference between working in a quiet bedroom and working in a hall with 200 phones.

**Why the bitrate is rank-transformed before correlating.** Camera bitrate is
violently heavy-tailed: measured MAD/median of 0.83-1.27 on the CP-E35A, with
single bins twenty times the median. Correlating the raw series lets a handful
of bursts dominate the dot product and bury a genuine response. Each bin is
therefore replaced by its normal score (average rank mapped through the normal
quantile), which is distribution-free — only the ORDER of bitrates matters.

Measured 8 Sep across every capture held, with synthetic controls:

| | camera3 | 12:47 torch | 12:59 torch | laptop control | misaimed |
|---|---|---|---|---|---|
| raw | 0.0055 | **0.128 (missed)** | **0.029 (missed)** | 0.475 | 0.103 |
| normal scores | 0.0040 | 0.0015* | 0.0065 | 0.506 | 0.017 |

\* after the clock-skew correction below. False-positive rate at p < 0.01 stayed
at or below threshold on gaussian, heavy-tailed and bursty t(2) noise, and at
zero on unrelated periodic traffic, while power at a 1.5x lift rose from 13% to
93%.

**Two alignment bugs found the same day, both of which read as "no camera".**
The phone stamps chip timestamps on ITS clock while the backend aligns on the
laptop's; a few hundred milliseconds of skew is a fraction of a chip and cancels
the correlation. Shifting t0 back 0.2 s turned a real capture from p = 0.029
CLEAR into p = 0.0015 WATCHING. The phone now measures the offset against a
server timestamp and corrects before reporting. Separately, `int(0.05 * FS)`
floored to 0, so lag 0.0 was searchable but never acceptable under the verdict
rule — a capture peaking there returned p < 0.01 and still read CLEAR.

**Why the null is surrogate sequences, not circular shifts of our own.**
Measured 7 Sep. Circularly shifting the transmitted stimulus and taking the peak
over a lag window produces null windows that overlap so heavily their maximum is
simply the global maximum of `R`. The p-value then degenerates into "did the
global peak land inside the lag window", whose probability is `L/n` — about 7.5%
for a 3 s lag search over a 40 s record. Measured false-positive rate at
p < 0.01 was **7.3%**, not 1%. The deeper limit is that a 30 s capture holds only
about `n/L` ≈ 13 independent shift positions, so p < 0.01 is *unreachable* that
way however strong the signal.

The null is therefore the same statistic computed against m-sequence-like
sequences that were **never transmitted**. Each is an independent draw, so
resolution is 1/(N+1). Measured: false-positive rate 0.000 at p < 0.01 and 0.024
at p < 0.05 (slightly conservative, erring toward CLEAR — the right direction for
an instrument that accuses people), while an injected response still returns
p = 0.002 with the lag recovered exactly. Asserted in `backend/test_detector.py`,
which must pass before any commit to `detector.py`.

**Why the permutation test.** It converts "the app says so" into "the null hypothesis that this device cannot see you is rejected at p < 0.01." No other team will show one, and a faculty panel will recognise it immediately.

---

## 9. Data Contracts

### Serial line — ESP32 → host

One line per active device per 100 ms bin:

```
D,<t_ms>,<mac>,<up_bytes>,<dn_bytes>,<up_pkts>,<rssi>
D,412300,34:ea:e7:aa:bb:cc,18420,240,14,-52
```

Scan mode:

```
S,<channel>,<mac>,<up_bytes>,<dn_bytes>,<pkts>,<rssi>
```

Control lines are prefixed `#` and ignored by data parsers:

| Line | Meaning |
|------|---------|
| `#NAZAR_SNIFFER_READY` | Boot complete, radio in promiscuous mode |
| `#LOCK,<n>` | Channel lock **confirmed** by the board. The host must wait for this before treating a capture as valid — a board still hopping while the host believes it is locked produces a capture that looks healthy and means nothing. |
| `#SCAN_PASS_DONE` | Channel sweep finished |
| `#DROP,<t_ms>,<table_full>,<serial>,<late_bins>,<unparsed>` | Emitted once per second. Cumulative loss counters — see below. |
| `#STATE,<mode>,<ch>,<bin_ms>,<max_dev>,...` | Response to `?` |
| `#ERR,<message>` | Rejected command |

**The drop counters are not diagnostics, they are correctness.** A gap in the
bitrate series does not announce itself: it yields a correlation that is
quietly wrong while still looking perfectly healthy. Any non-zero, climbing
counter invalidates the capture in progress.

| Counter | Meaning | First response |
|---------|---------|----------------|
| `table_full` | A new MAC arrived with no free slot | Expected in a crowded hall; the dropped devices are the least active |
| `serial` | TX buffer could not take a line, so it was discarded rather than blocking `loop()` | Reduce `MAX_DEVICES` (see README) |
| `late_bins` | `loop()` missed a 100 ms deadline; bins were skipped and the clock resynchronised | Serious — the time axis is the correlation's lag axis |
| `unparsed` | Runt, ad-hoc/WDS, or group-addressed frame | Normal, always non-zero |

### Scan result object

```json
{
  "scan_id": "s_004",
  "mac": "34:ea:e7:aa:bb:cc",
  "vendor": "Tuya",
  "channel": 6,
  "peak": 0.71,
  "lag_s": 0.4,
  "p_value": 0.0005,
  "permutations": 2000,
  "verdict": "WATCHING"
}
```

### Evidence record

The scan result, plus: full bitrate series, stimulus sequence and chip timestamps, RSSI track, UTC timestamp, `SOFTWARE_VERSION`. Then `sha256` of the canonical JSON, then an Ed25519 signature over that hash.

Pitch line: *a screenshot is not evidence; a signed, reproducible measurement record is something a lawyer can work with.*

---

## 10. API Documentation Standard

Backend binds `127.0.0.1`. **No auth, no CORS, no rate limiting** — nothing external can reach it. Every response follows one of two shapes:

```json
{ "ok": true,  "data": { } }
{ "ok": false, "error": "human readable message" }
```

### Endpoint index

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/health` | Is the ESP32 connected and streaming? |
| POST | `/discover` | Channel sweep → candidate device list |
| POST | `/scan/start` | Lock channel, return `scan_id` + stimulus sequence + `t0` |
| GET | `/scan/{id}` | Poll for result |
| GET | `/localise/{mac}` | Smoothed RSSI + bearing estimate |
| POST | `/evidence/{scan_id}` | Generate and return the signed report |
| POST | `/replay` | Run the pipeline on a saved capture file |

### Worked example (use this template for the rest)

```
### POST /scan/start

Description: Lock the radio to a channel and begin a stimulus-correlation scan.
Auth: none (localhost only)

Request body:
  {
    "mac": "34:ea:e7:aa:bb:cc",   // Required. Candidate from /discover.
    "channel": 6                   // Required. From /discover.
  }

Response — 200 OK
  {
    "ok": true,
    "data": {
      "scan_id": "s_004",
      "sequence": [1,1,1,1,0,1,0,1,1,0,0,1,0,0,0],
      "chip_s": 2.0,
      "t0_ms": 1725600000123     // frontend starts strobing at this instant
    }
  }

Response — 400
  { "ok": false, "error": "channel must be 1-13" }

Response — 503
  { "ok": false, "error": "sniffer not streaming — check USB" }

Use cases
  - Frontend calls this after the user picks a candidate, then drives the torch
    from t0_ms using sequence and chip_s.

Business rules
  - Only one scan may be active at a time; a second call returns 409.
  - The channel is locked for the whole scan. No hopping mid-scan, ever.
```

---

## 11. Frontend

Four screens, no router, no state library, no styling budget. Presentation is worth 5 marks in Round 1 and **zero** in Round 2.

| # | Screen | Contents |
|---|--------|----------|
| 1 | **Sweep** | One button. Live device count. Calls `/discover`. |
| 2 | **Stimulus** | "Hold still. Point your phone at the room." 30 s ring. Torch strobing. |
| 3 | **Verdict** | Correlogram with the peak marked, p-value in large type, one sentence naming the device. |
| 4 | **Find it** | Hot/cold bar, bearing arrow, "Save evidence report". |

**Torch control.** The browser torch API is unreliable across devices. The safe fallback is a **full-screen white/black flash** driven by the same sequence — it is a weaker stimulus optically but it always works, and it can be tested tonight. Decide by the 7th based on what the CP Plus camera actually responds to.

---

## 12. Testing & Validation Guide

### Unit — run before every commit to `detector.py`

| Test | Expectation |
|------|-------------|
| Pure Gaussian noise input | `p > 0.05` |
| Synthetic signal, known lag injected | `p < 0.01`, recovered lag within one bin |
| Constant input (all zeros) | No crash, no divide-by-zero |

### Negative controls — must all return CLEAR

Laptop streaming video · phone on a call · smart bulb · the router itself

*If a laptop on a video call pointed at you returns WATCHING, that is a true positive, not a bug. Say so in Q&A.*

### Positive controls — must all return WATCHING

| Variable | Values |
|----------|--------|
| Camera | CP Plus CP-E35A (ezykam+, tracking off), backup Android phone (IP Webcam, H.264) |
| Position | three placements in the room |
| Lighting | lights off, lights on |
| Distance | 1 m, 3 m |

### Environment ladder

1. Quiet room, few devices → establishes that it works at all
2. Lab or canteen, 50+ devices → establishes that it works where the demo is
3. The actual venue on the 9th → recalibrate thresholds in `config.py`

### Blind test — the real acceptance criterion

Teammate hides the camera while the rest of the team is outside. Team returns, sweeps, finds it. **Five consecutive successes before the 9th or the demo is not ready.**

### Replay harness

Every capture is written to `captures/`. `replay.py` feeds a saved CSV through the identical detector. This gives regression testing while tuning, and a defensible fallback if the venue RF is hostile — real code on real recorded data, not a video. *A recorded video of a working demo is an admission of failure; a replay through the live pipeline is engineering practice.*

---

## 13. Pre-Demo Checklist

Run this on the evening of 8 Sep, and again in the 160 minutes.

### Hardware
- [ ] ESP32 flashed with the final firmware; `#NAZAR_SNIFFER_READY` seen
- [ ] CH340 driver working on the demo laptop (the one you will actually use)
- [ ] Arduino IDE present on that laptop, ESP32 board package installed, in case a re-flash is needed on site
- [ ] CP-E35A paired in **ezykam+**; motion tracking confirmed **off**
- [ ] Spare USB-C data cable in the bag
- [ ] Camera charged / powered; power adapter and cable packed
- [ ] Camera motion tracking **off**; pointed at a still surface
- [ ] Backup phone camera configured and tested (H.264, 2.4 GHz, screen awake)
- [ ] Laptop fully charged; charger packed

### Software
- [ ] `git pull` clean; everything committed
- [ ] Fresh clone → `pip install -r requirements.txt` → runs (proves no undocumented local state)
- [ ] `config.py` thresholds match the last successful hostile-environment test
- [ ] `replay.py` produces a correct verdict on a known-good capture
- [ ] At least three good captures saved in `captures/` as fallbacks
- [ ] Signing key present; evidence PDF renders

### Demo
- [ ] Five consecutive blind hide-and-find runs completed
- [ ] Serial Monitor closed before running Python (this will bite someone)
- [ ] Deck updated: every "ESP32-S3" changed to "ESP32"
- [ ] Q&A answers rehearsed out loud, not just read
- [ ] Roles assigned: who talks, who drives the laptop, who walks to the camera

---

## 14. Milestones & Gates

| Date | Deliverable | Gate |
|------|-------------|------|
| **6 Sep** | Board flashed; Blink works; sniffer streaming `D,` lines; channel scan finds the camera | Serial output visible, camera's MAC identified |
| **7 Sep** | Bin aggregation + live view | **GATE 1 — ✅ PASSED 7 Sep 19:50.** Controlled 6-wave trial, timings fixed in advance: 6/6 waves raised the uplink bitrate, 1.89x overall (+1238 kbps against 509 kbps MAD), sign test p = 0.0156. Fixture: `captures/wave_20260907_195034.{csv,json}`, re-runnable with `nazar_wave_test.py --replay`. |
| **7 Sep (late)** | Stimulus + detector | **GATE 2 — ✅ PASSED 8 Sep 01:40.** Camera: `p = 0.0055`, peak 0.439, lag 0.50 s, WATCHING (`scan_20260908_014034_camera3`). Negative control, a device pushing real traffic that cannot see the screen: CLEAR, `p = 0.4748` (`scan_20260908_013018_laptop-control`). Both captures had zero critical loss counters. Independently corroborated by an event-locked analysis that does not use the detector: camera 5/7 transitions, lift 2.26x; control 1/7, lift 0.92x. |
| **8 Sep (am)** | Hostile-environment test | Works in a room with 50+ devices; thresholds re-tuned; replay green |
| **8 Sep** | **Full implementation complete** | ✅ F1-F13 built and tested. `detector.py` (8/8 unit tests incl. a false-positive-rate assertion), `serial_reader.py`, `discovery.py`, `classifier.py`, `localise.py`, `evidence.py`, `replay.py`, `main.py` (7 endpoints), React frontend (4 screens, builds clean). Whole-system suite 9/9. `tools/preflight.py` green. |
| **8 Sep (pm)** | Hardware sign-off, rehearsal, **freeze** | Gate 2 confirming run + laptop negative control; five blind runs; `preflight.py --hardware` green; deck final; commit and stop changing code |
| **9 Sep** | Finale | — |

**Plan change, 8 Sep — the finale is offline and the prototype must arrive
finished.** The organisers require a working prototype on arrival; the venue is
not a build environment. §15's 160 minutes are therefore recalibrated: they are
for RF recon, threshold recalibration in the actual room, and rehearsal — *not*
for implementation, installation or debugging. Everything in F1-F13 is built and
tested before travelling, `npm install` and `pip install` are done while
internet still exists, and `tools/preflight.py` must be green before the laptop
is packed. The only thing that changes on site is `config.py`.

**Gate 2 evidence standard.** The camera half was first attempted at 01:28 on
8 Sep and **failed** (CLEAR, p = 0.10). The cause was physical, not statistical:
the camera was uploading 2.2 Mbps at a busy scene rather than the screen, and an
event-locked check showed no transition response at all (3/7 edges, lift 0.76x).
Nothing in `config.py` was touched. The stimulus geometry was fixed, verified in
60 s with `nazar_flash_test.py` (7/8 flashes, sign p = 0.035), and the scan
re-run — passing at p = 0.0055. That failed capture is kept as
`fixtures/scan_20260908_012849_camera2` and asserted in the test suite: a real
camera, streaming hard, that the detector must keep calling CLEAR because the
light never reached it.

**Gate 1 evidence standard.** Three earlier captures showed 6-11x spikes but had
no record of when the hand actually moved, so they were kept as preliminary
fixtures and not counted. The gate was called only on a trial that logged its
own cue times onto the capture clock. An uncontrolled spike is not a
measurement — the CP-E35A's own bitrate varies by ~50% MAD/median with nothing
happening in front of it.

**The schedule compressed** when the shortlist landed on the 5th. F10–F13 are now genuinely at risk. If you reach the evening of the 8th with Gates 1 and 2 passed but no bearing arrow and no signed PDF, **demo what you have.** A working correlation with a p-value beats a half-built feature list.

---

| **9 Sep** | Phone-torch scan corrected | **Two defects found and fixed on the 8 Sep 23:41 capture.** The torch mark was timestamped after the awaited torch command, placing `t0` up to a second late so a 2.3x response sat at -0.70 s and scored CLEAR (p=0.8271); and only edge response shapes were searched, while a torch held in the lens produces a sustained level change. With the midpoint timestamp, a signed lag window and both shapes searched, the same capture reads **WATCHING, p = 0.0060, lag -0.70 s, shape `level`** with no change to the data. Negative control unchanged: **CLEAR, p = 0.7916**. False-positive rate on heavy-tailed noise: **0.0025 at the p<0.01 gate over 800 runs**. Fixture: `fixtures/scan_20260908_234109_negative_lag.{csv,json}`. |

## 15. Demo Runbook — the 160 minutes

| Minutes | Activity |
|---------|----------|
| 0–20 | RF recon of the venue. Channel occupancy scan. Note the noisiest channels. |
| 20–50 | Recalibrate `config.py` **in the actual demo room**. Ask for a side room or a corner. |
| 50–90 | Integration, smoke tests, hide-and-find dry runs |
| 90–120 | Three full rehearsals, timed |
| 120–150 | Pitch deck: reuse Round 1 slides, add today's measured results |
| 150–160 | Buffer. Charge everything. Test the projector. |

**Set the demo up before you present.** Hand a judge the camera and ask them to hide it while the team is out of the room. Ceding control of the hiding is what makes it undeniable — and it costs nothing, because the method does not care where the camera is.

### Definition of done

> A judge hides a Wi-Fi camera in the room while the team is outside. The team returns, runs one sweep, reads out the p-value, walks to the vent, and removes the camera. Under four minutes, no retries.

---

## 16. Q&A Preparation

Rehearse these aloud. A faculty panel will probe.

**"What about a camera with an SD card and no Wi-Fi?"**
Invisible to us — it transmits nothing. We won't pretend otherwise. Worth adding: covert cameras sold for remote voyeurism overwhelmingly stream, because an attacker who must physically retrieve an SD card takes on far more risk. We address the streaming majority.

**"Isn't this just SnoopDog?"**
Derived from it, and we cite it. SnoopDog needed monitor-mode Wi-Fi drivers no phone ships, and asked users to perform jumping jacks. We moved capture to a ₹659 ESP32 and replaced body motion with a coded optical stimulus worth roughly 12 dB more processing gain. That's the difference between a paper and something a woman can use in a hotel room.

**"Is passive sniffing legal?"**
We capture 802.11 headers only — no decryption, no payload. It's a self-defence sweep of a space you are lawfully occupying. Formal legal review before deployment, not before a prototype.

**"5 GHz?"**
Out of scope; the ESP32 is 2.4 GHz only. The cheap covert cameras that dominate the Indian market are 2.4 GHz. A second radio module extends it.

**"What if someone's laptop on a video call correlates with you?"**
Then it's a true positive — that laptop's camera can see you. It demonstrates the method is measuring the right thing.

**"Can the camera's owner detect your sweep?"**
No. The RF side is entirely passive. The only emission is our own light.

**"Where's your authentication / database / security layer?"**
This is a measurement instrument, not a service. It binds to localhost, has one user, and lives for one session. Adding auth would be theatre.

**"What's next?"**
Keychain-scale sniffer paired to a phone app; 5 GHz support; partnership with hotel-rating platforms to make a clean sweep a listed amenity.

---

## 17. Risk Register

| Risk | Severity | Mitigation |
|------|----------|------------|
| Correlation buried in venue RF noise | **Critical** | m-sequence processing gain; channel lock; permutation test; on-site recalibration; replay harness |
| Gate 1 fails on 7 Sep | **Critical** | Pre-committed switch to the software-only fallback that night |
| ESP32 drops packets under load | High | On-device aggregation; `MAX_DEV` capped at 40 |
| Serial port renamed between plug-ins | High | Resolved: the port is auto-detected by CH340 USB ID rather than hard-coded. Caught 8 Sep when the board reappeared as `usbserial-10`. |
| Serial link drops mid-demo | High | USB not BLE; spare cable; test on the demo laptop specifically. **Full replay mode**: `NAZAR_REPLAY=<capture>` runs the entire system — detector, API, UI — off a recorded capture, announced as replay everywhere it appears. |
| Camera DOA or won't pair | High | Backup phone camera configured and tested in advance; failing that, replay mode demonstrates the live pipeline on recorded data |
| Browser torch API unavailable | Medium | Full-screen white/black flash fallback, decided by the 7th |
| Bright venue lighting weakens the stimulus | Medium | Stand closer; aim at a wall; lengthen scan; ask for light control |
| CP-E35A pans (motion tracking) during scan | Medium | **Disable tracking in the ezykam+ app before any test.** Pan-tilt movement changes the entire frame and produces bitrate spikes uncorrelated with our stimulus. |
| Judge knows the prior art | Medium | Disclose it ourselves on slide 2, first |
| Firmware/config constants drift out of sync | Low | `tools/set_max_devices.py` edits both files and re-flashes; `tools/preflight.py` compares the two sources textually and fails on drift |
| A `config.py` edit silently does not take effect | Medium | Python reuses a cached `.pyc` when a same-length edit lands in the same second — exactly the shape of `MAX_DEVICES 40->24` and `P_THRESHOLD 0.01->0.05`. Reproduced 8 Sep. Every entry point now purges bytecode before importing config, and the drift check reads source rather than importing. |
| A backend fault is reported to the operator as a camera fault | **High** | The 20 s Check graded a dead measurement stream as "no data from the camera", sending the operator to fix a camera that was streaming 1.9 Mbps. Root cause 8 Sep: `tot_pkts`/`tot_bytes` were incremented unbound in `/probe/{mac}/stream`, raising `UnboundLocalError` on the target's first packet. The Check now has a distinct `streamfail` grade that names the fault as ours and tells the operator to change nothing. `pyflakes` over `backend/` is clean and catches this class of defect. |
| Camera changes channel between sweep and measurement | Medium | A camera follows its router; the UI may hold a channel from an earlier sweep, and locking the wrong one yields zero packets that look exactly like a silent camera. Observed 8 Sep (camera moved ch7 -> ch6). `_find_channel_for()` re-sweeps and re-locks once after 4 s of target silence, in both the probe and localise streams, and the UI is told it happened. |
| The serial port is held by a screen the operator has finished with | **High** | One process owns the port, so any live meter blocks the next action. The 20 s check kept its measurement stream open while the verdict was on screen, and "Run the 90 s scan" returned 409 `serial port busy` — which looked like a dead button. Fixed 8 Sep three ways: the check closes its stream the moment it grades; every streaming endpoint takes a hard `max_s` bound so a backgrounded iOS tab cannot pin the port; and `_await_port()` waits ~4 s for an asynchronous teardown (measured 0.66 s) instead of refusing instantly. A start that still fails is shown where the button is, not by navigating away. |
| The modelled stimulus drifts from the light actually emitted | **Critical** | The phone's torch marks were timestamped AFTER the awaited `applyConstraints` call, so `t0` landed up to a second late and the camera's real response fell at a NEGATIVE lag. Measured 8 Sep 23:41: a 2.3x response scored CLEAR at p=0.8271. Fixed three ways — the transition is timestamped at the midpoint of the torch command rather than after it, the measured torch latency is reported in every mark and sidecar, and `LAG_WINDOW` is now signed (-0.8, 2.2) so a residual offset cannot put the answer outside the search. Fixture `scan_20260908_234109_negative_lag.csv` asserts it. |
| The camera's response SHAPE depends on geometry | **Critical** | A torch held in the lens gives a sustained LEVEL change for the whole chip; off-axis light gives a transient at each EDGE. Searching one shape throws the other away: on the 23:41 capture level gave p=0.0015 and edge p=0.10, and on fixture s_002 the reverse. The detector now searches both (`config.STIM_SHAPES`) and combines them by Westfall-Young minP, standardising each shape against its own surrogate null — a plain maximum let the noisier level shape swamp a genuine edge detection, costing s_002 a tenfold loss in p. |
| A one-sided direction pinned from the 20 s check | High | The check's sign is only as good as its phase alignment, and with a 2 s strobe period a 1 s timing error inverts it exactly. On 8 Sep it reported "falls" for a camera whose bitrate rose, and the one-sided scan returned CLEAR at p=0.049 on data that scores 0.0040 both-sided. The product no longer pins direction: scans and replays run `auto`. Measured cost is a factor of ~1.6 in p; the cost of being wrong is the entire detection. |
| Find It ranked spots by SIGNED response | **Critical** | The spotlight sorted spots by the signed in-phase lock-in `z`. A torch held in a lens saturates the sensor, so the uplink FALLS and `z` goes strongly negative — the correct answer was sorted to the BOTTOM of the board, below a blank wall. Compounding it, the browser's torch latency is unknown to a few hundred ms and a quarter-period error (0.5 s on the 2 s strobe) nulls the in-phase statistic outright: measured 0.06 against a true 4.81. Ranking now uses `lock_z`, the strength of the lock at its best phase with the sign discarded. Measured over 120 trials with a random 0-2 s torch offset, ranking the true spot above a cold one: signed z 50.0% (chance), in-phase magnitude 83.3%, `lock_z` 99.2%. Because it searches phase, `lock_z` is inflated on noise — equally for every spot, which is why it may rank but must never be reported as a p-value. `test_probe.py` asserts both properties. |
| Leaving Find It held the serial port | High | The screen's cleanup began `clearInterval(beam)` and `beam` did not exist, so a ReferenceError was thrown before the three calls that stop the strobe, extinguish the torch and close the measurement stream. The torch stayed lit and the port stayed held until the 10 minute server default expired, so the next action failed with 409. Fixed, and the stream is now bounded at 240 s server side and reopened while the screen is open. |

---

## 18. Deliberately Out of Scope

The reference blueprint this document borrows its structure from describes a multi-user cloud SaaS. Almost none of it applies. Cut, with reasons, so that any gap is a decision rather than an oversight:

| Cut | Reason |
|-----|--------|
| Auth, JWT, OAuth, roles, sessions | One user, one laptop, localhost. No attack surface exists. |
| MongoDB / any database, and the MongoDB MCP server | Session-scoped data. CSV + JSON are sufficient and directly inspectable. |
| CORS, helmet, rate limiting, mongo-sanitize | Backend binds `127.0.0.1`. Nothing external can reach it. |
| Sentry, Winston, structured logging | `print()` plus a capture file. Eight-day lifespan. |
| Postman collection | Seven endpoints, one consumer, no external integrators. |
| Docker, CI, deployment, health/readiness probes | Runs from a terminal on one machine. Nothing polls it. |
| TypeScript, Zod, React Hook Form, TanStack Query | Two React files. Types and form libraries would cost time and buy nothing. |
| Native mobile app | Laptop UI demos identically and is far more reliable on stage. |

---

## 19. Document Maintenance

**Update this file when:**

- A gate passes or fails → update §14 immediately; the milestone table is the team's shared reality
- A threshold changes during testing → update §6 and note *why* in the commit message
- A requirement is descoped → move it in §3 rather than deleting it, so Q&A has an answer
- Hardware changes → update §4 BOM **and** every mention in the deck. The deck and the earlier build plan still say "ESP32-S3" in places; the board actually purchased is the **ESP32 WROOM-32 (classic)**. Every slide must read "ESP32" before the 9th — a judge who reads S3 on the architecture slide and then sees a WROOM-32 on the table will ask about it.

**Review triggers:**

- Any change to `M_SEQUENCE`, `CHIP_S`, or `P_THRESHOLD` → re-run the full positive and negative control set
- Any firmware change to `BIN_MS` or `MAX_DEV` → update `config.py` in the same commit
- Any change to the frame-parsing offsets → re-verify against a known device before trusting any result

**For AI assistants working from this file:** follow ⚠️ Rule 2 — search for current dependency versions and state what you verified. Do not add version pins here. And treat §14 as binding: if Gate 1 has not passed, the correct advice is to stop, not to optimise.
