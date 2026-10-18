<div align="center">

# NAZAR
### Wi-Fi Causality Detection of Concealed Cameras

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.13+](https://img.shields.io/badge/Python-3.13+-3776AB.svg?logo=python&logoColor=white)](https://python.org)
[![Hardware: ESP32](https://img.shields.io/badge/Hardware-ESP32--WROOM--32-E7352C.svg?logo=espressif&logoColor=white)](https://www.espressif.com/)
[![Backend: FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Frontend: React 18](https://img.shields.io/badge/Frontend-React%2018%20%7C%20Vite-61DAFB.svg?logo=react&logoColor=black)](https://react.dev)
[![Empirical Status](https://img.shields.io/badge/Empirical%20Validation-Gate%201%20%26%202%20PASSED-brightgreen.svg)]()

<p align="center">
  <b>A phone-guided surveillance detector that uses coded optical excitation to statistically prove if a hidden Wi-Fi camera is watching you.</b>
</p>

*Built by **Krishna Sharma** for **Nari Kavach 2026** (Theme: Cyber Safety) · Organized by **IEEE Women in Engineering (WiE)**, Faculty of Technology, University of Delhi.*

---

</div>

## 📌 Overview & Core Insight

Covert voyeurism through hidden wireless cameras in hotels, rental accommodations, changing rooms, and private spaces is an escalating violation of personal dignity and safety. Miniature spy cameras are readily available online for under ₹1,500 ($18) and are easily concealed in smoke detectors, digital clocks, power sockets, or screw heads.

### Why Traditional Detectors Fail:
1. **Network Scanners (Fing, WiFiman):** Only scan the Local Area Network your phone is connected to. They **completely fail** if the camera is on the host's private Wi-Fi, a hidden SSID, behind Client/AP Isolation, or on a mobile hotspot.
2. **RF Bug Detectors:** Measure raw broadband RF power. In any modern building, they beep uncontrollably due to smartphones, smart TVs, routers, and Bluetooth devices, causing unbearable false positives.
3. **Optical Lens Finders:** Require users to crawl across every square inch of a room peering through red LEDs at exact retro-reflection angles.

### The NAZAR Solution: Physical Causality
NAZAR does not merely detect that a wireless device exists in the vicinity—**it mathematically proves that the camera has line-of-sight to you**.

```
┌─────────────────┐       Coded Light Strobe        ┌────────────────────────┐
│   Smartphone    │ ──────────────────────────────> │ Hidden Surveillance    │
│  Camera Torch   │   (15-chip m-sequence @ 0.5 Hz) │ Camera (H.264 Encoder) │
└─────────────────┘                                 └───────────┬────────────┘
                                                                │
                                              Dynamic Re-encode │ Encrypted 802.11
                                                Bitrate Surge   │ Uplink Traffic
                                                                ▼
┌─────────────────┐       Real-time USB UART        ┌────────────────────────┐
│  Host Backend   │ <────────────────────────────── │ ESP32 Promiscuous      │
│  (FastAPI / ML) │        (460,800 baud)           │ Sniffer (Core 0 / 1)   │
└────────┬────────┘                                 └────────────────────────┘
         │
         ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                    MATCHED FILTER & CAUSALITY DETECTOR                     │
│  • Normal-Scores Rank Transform (Invariance to periodic keyframe bursts)   │
│  • 51-bin Running Median Detrending & Circular FFT Cross-Correlation       │
│  • Dual-shape Search (Level & Edge) combined via Westfall-Young minP       │
│  • 2,000-Surrogate Permutation Test: Verdict = WATCHING (p < 0.01)         │
└────────────────────────────────────────────────────────────────────────────┘
```

When a camera's field of view experiences sudden visual motion or illumination changes, video compression efficiency drops sharply. The encoder is forced to emit substantially larger P-frames or full I-frame keyframes. 

Even though the payload is encrypted (WPA2/WPA3, TLS/SRTP), raw 802.11 MAC frame lengths and arrival times are broadcast in the clear. By strobing an optical sequence and passively sniffing uplink packets, NAZAR measures the deterministic time-lock between light and bitrate.

---

## 🔬 Scientific Methodology & Engineering Architecture

### 1. Coded Optical Excitation (Processing Gain)
Rather than requiring erratic human motion (jumping jacks, towel waving) or a naive periodic flash, NAZAR strobes the smartphone torch using a **15-chip maximal-length sequence (m-sequence)**:

```python
M_SEQUENCE = [1, 1, 1, 1, 0, 1, 0, 1, 1, 0, 0, 1, 0, 0, 0]
```

- **Timing:** 2.0 seconds per chip (30 seconds per sequence pass), repeated 3 times (90 seconds total).
- **Processing Gain:** The autocorrelation of an m-sequence is a Kronecker delta, yielding **~12 dB of processing gain** over ambient Wi-Fi noise without user fatigue.

### 2. High-Speed Promiscuous Sniffer Firmware (`firmware/`)
Implemented on a commodity **ESP32-WROOM-32** using dual-core FreeRTOS:
- **Core 0 (IRAM Callback):** Intercepts 802.11 data frames at up to 2,000 packets/sec via `esp_wifi_set_promiscuous_rx_cb()`. Extracts frame length, direction (ToDS/FromDS), and increments counters in a statically allocated device table. Never blocks, never allocates heap memory, and never prints to serial.
- **Core 1 (100 ms Aggregation):** Atomically snapshots active devices every 100 ms and emits clean CSV telemetry over UART at **460,800 baud** with loss counters (`#DROP: table_full, serial_tx, late_bins`).

### 3. Heavy-Tailed Signal Processing & Causality Engine (`backend/detector.py`)
- **Normal-Scores Rank Transform:** Camera bitrates exhibit extreme kurtosis (MAD/median ratios of 0.83–1.27) due to periodic I-frames. Transforming packet counts to rank quantiles (`r / (N + 1) → Φ⁻¹(r)`) prevents outlier bursts from biasing the correlation.
- **Circular FFT Cross-Correlation:** Zero-padded circular correlation guarantees strict mathematical exchangeability of temporal shifts, eliminating edge-truncation bias.
- **Dual Response Shapes:** Searches both direct lens sensor saturation (**Level Mode**) and off-axis transient re-encodings (**Edge Mode**), combined via the **Westfall-Young minP** algorithm to prevent multiple-comparison penalties.
- **2,000-Surrogate Permutation Test:** Verifies the observed peak correlation against 2,000 balanced binary sequences that were *never transmitted*, returning an empirical p-value:

```math
p = \frac{\sum_{i=1}^{M} \mathbb{I}(R_i^{\text{null}} \ge R^{\text{observed}}) + 1}{M + 1}
```

- **Verdict:** If `p < 0.01` and physical lag is within `[-0.8 s, +2.2 s]`, the system confirms **`WATCHING`** with 99% statistical confidence.

### 4. Physical Localisation ("Find It" Screen)
Once confirmed, the user pinpoints the physical housing:
- **Optical Spotlight:** User sweeps suspected objects (vents, clocks, smoke detectors) for 14 s each. A **software lock-in amplifier** (Welch's t-test + 90° quadrature control) scores and ranks objects on a live leaderboard.
- **RF Proximity:** Two-pole EWMA band-pass filter on RSSI isolates intentional operator movement from multipath jitter with Geiger-counter audio feedback.
- **Body-Shadow Bearing Fit:** The user rotates 360° holding the ESP32 against their chest. Fitting a cosine model to the 3–6 dB torso attenuation estimates the compass angle of signal arrival.

### 5. Cryptographic Evidence Chain (`backend/evidence.py`)
Generates tamper-evident forensic reports containing raw 100 ms time-series data, stimulus timestamps, detector statistics, and hardware versions:
- Canonical JSON hashed with **SHA-256**.
- Cryptographically signed with a local, private **Ed25519** key.
- Renders an automated, court-ready one-page summary PDF via ReportLab.

---

## 📊 Empirical Validation & Measured Results

All metrics derived from physical hardware trials on a **CP Plus CP-E35A** 3MP pan-tilt Wi-Fi camera:

| Test Gate | Protocol | Measured Result | Verdict |
|---|---|---|---|
| **Gate 1: Physical Modulation** | Controlled 6-wave hand motion trial (`fixtures/wave_*.csv`) | **6/6 waves** induced immediate bitrate lift (avg +1,238 kbps, 1.89x baseline, sign-test p = 0.0156) | **PASSED** |
| **Gate 2: Coded Target Scan** | 90 s m-sequence strobe on active camera (`fixtures/scan_*_camera3.csv`) | Peak correlation: **0.439**, physical lag: **+0.50 s**, empirical **p = 0.0055** (p < 0.01) | **WATCHING** |
| **Gate 2: Negative Control** | Laptop streaming 1080p video, unable to see light (`fixtures/scan_*_laptop.csv`) | Empirical **p = 0.4748** (statistically indistinguishable from pure chance) | **CLEAR** |
| **Phone Torch Latency Fix** | Verified on direct-lens direct capture (`fixtures/scan_*_negative_lag.csv`) | Re-derived: **p = 0.0060**, lag: **-0.70 s**, shape: `level` | **WATCHING** |
| **False-Positive Calibration** | 800 synthetic Monte Carlo trials across heavy-tailed Student-t noise | False-positive rate at p < 0.01: **0.0025** (ultra-conservative) | **CALIBRATED** |

---

## 🛠️ Hardware Bill of Materials (BOM)

| Component | Specification | Approximate Cost | System Role |
|---|---|---|---|
| **ESP32 DevKit** | ESP32-WROOM-32 (Xtensa LX6 @ 240 MHz, CH340C bridge) | ₹659 (~$8.00) | Promiscuous 802.11 sniffer. **Zero soldering, no breadboard, uses 0 GPIO pins.** |
| **USB Data Cable** | Type-C to Type-A/C verified data cable | Standard | Power and 460,800 baud serial telemetry stream. |
| **Host Machine** | Laptop (macOS / Linux / Windows) running Python 3.13 | Owned | FastAPI backend, signal processing pipeline, and web UI server. |
| **Client Device** | Android smartphone (Chrome browser) | Owned | Mobile UI, optical torch modulation, and handheld directional probe. |
| **Test Camera** | CP Plus CP-E35A (3MP Pan-Tilt Wi-Fi Camera) | Reference target | Empirical hardware validation fixture. |

---

## 🚀 Quick Start Guide

### 1. Prerequisites
- **Python 3.13+**
- **Node.js 18+** & `npm`
- **Arduino IDE 2.x** with the `esp32` board package (Espressif Systems)

### 2. Installation
```bash
# Clone the repository
git clone https://github.com/krishnasharma1493/NAZAR.git
cd NAZAR

# Create and activate Python virtual environment
python3 -m venv .venv
source .venv/bin/activate    # On Windows: .venv\Scripts\activate

# Install backend dependencies (unpinned for clean dependency resolution)
pip install -r requirements.txt

# Install frontend dependencies
cd frontend && npm install && cd ..
```

### 3. Flash the ESP32 Firmware
1. Open `firmware/nazar_sniffer/nazar_sniffer.ino` in Arduino IDE.
2. Select **Board: ESP32 Dev Module**.
3. Set **Upload Speed: 460800** (avoid 921600 due to CH340 bridge limitations).
4. Connect the ESP32 via USB and click **Upload**.

### 4. Running the Complete System
Launch the entire stack with a single command:

```bash
# Mode 1: Live Hardware Execution
python tools/run_demo.py

# Mode 2: Full-Pipeline Replay (Zero Hardware Required)
# Reruns verified ground-truth captures through the live detector, API, and UI
python tools/run_demo.py --replay

# Mode 3: Mobile Phone Torch Over LAN HTTPS
# Provides self-signed TLS so Android Chrome exposes the Camera Torch API
python tools/run_demo.py --phone
```

Open your browser at **http://127.0.0.1:8000** (or the HTTPS LAN URL displayed in the terminal for phone mode).

---

## 🧪 Testing & Verification Suite

NAZAR includes an automated preflight test suite that verifies dependencies, algorithm accuracy, and replay fidelity:

```bash
# Run comprehensive software test suite (~2 mins)
python tools/preflight.py

# Run with connected ESP32 hardware validation
python tools/preflight.py --hardware

# Run detector statistical unit tests against committed fixtures
python backend/test_detector.py

# Run complete integration pipeline test
python backend/test_integration.py
```

---

## 📁 Repository Structure

```text
NAZAR/
├── backend/
│   ├── config.py              # Single source of truth for all mathematical thresholds
│   ├── main.py                # FastAPI server (REST endpoints + SSE event streams)
│   ├── detector.py            # Matched filter, rank transform, permutation null test
│   ├── discovery.py           # Uplink dominance filter, factory MAC & OUI analysis
│   ├── classifier.py          # Gradient-boosted camera likelihood pre-filter
│   ├── probe.py               # Lock-in amplifier & quadrature testing for spotlight
│   ├── localise.py            # Two-pole EWMA RSSI hot/cold tracker & bearing fit
│   ├── evidence.py            # Canonical JSON, SHA-256 hashing, Ed25519 signing
│   ├── serial_reader.py       # USB auto-discovery, line grammar, clock synchronization
│   ├── replay.py              # Real-time wall-clock paced capture player
│   └── fixtures/              # Verified ground-truth captures for unit testing
├── firmware/
│   └── nazar_sniffer/
│       └── nazar_sniffer.ino  # Dual-core ESP32 promiscuous 802.11 sniffer
├── frontend/
│   ├── index.html             # High-contrast mobile-first application shell
│   └── src/
│       ├── App.jsx            # Core state machine (Sweep -> Scan -> Verdict -> Find)
│       ├── Stimulus.jsx       # 15-chip m-sequence controller & visual countdown ring
│       ├── SignalCheck.jsx    # 20 s pre-scan optical coupling verification
│       ├── Spotlight.jsx      # Directional lock-in amplifier sweeping interface
│       └── torch.js           # Low-latency Web MediaStream Camera Torch driver
├── tools/
│   ├── run_demo.py            # Production orchestrator (Backend + Vite build + LAN HTTPS)
│   ├── preflight.py           # Complete end-to-end system verification runner
│   ├── venue_recon.py         # On-site RF spectrum analyzer & packet loss profiler
│   ├── set_max_devices.py     # Synchronizes device table size between backend & sketch
│   ├── nazar_read.py          # Standalone real-time ASCII bitrate sparkline monitor
│   └── nazar_gate2.py         # CLI-driven stimulus scan and detector runner
├── LICENSE                    # MIT License
├── requirements.txt           # Python package requirements
└── README.md                  # System overview and documentation (this file)
```

---

## 📡 Backend API Reference

The FastAPI service exposes RESTful endpoints and Server-Sent Events (SSE):

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Hardware status, connected serial port, and operating mode (`live` / `replay`). |
| `POST` | `/discover` | Triggers channel sweep (1–13) and returns candidate devices ranked by uplink ratio. |
| `POST` | `/scan/start` | Locks sniffer to target channel and initializes a new coded scan session. |
| `GET` | `/scan/{scan_id}` | Polls the detector verdict, peak cross-correlation, encoder lag, and empirical p-value. |
| `GET/POST`| `/localise/{mac}` | Streams live EWMA band-passed RSSI readings and computes body-shadow compass bearing. |
| `POST` | `/evidence/{scan_id}`| Generates tamper-evident SHA-256 / Ed25519 cryptographic JSON and ReportLab PDF. |
| `POST` | `/replay` | Replays pre-recorded CSV captures through the live processing pipeline. |

---

## ⚖️ Scientific Honesty & Operational Boundaries

To maintain scientific integrity, NAZAR adheres to strict boundaries:
- **Active Streaming Only:** NAZAR detects cameras actively streaming live video over Wi-Fi. It cannot detect unpowered cameras or offline cameras recording solely to internal SD cards.
- **Meaning of `CLEAR`:** A `CLEAR` verdict strictly means that no statistically significant causal response was observed during the 90-second window. It is not an absolute certificate of absence.
- **Pre-Filter vs. Verdict:** The Machine Learning classifier is solely an advisory pre-filter to rank candidates; the **matched-filter permutation test (p < 0.01) is the sole basis for a detection verdict**.

---

## 👤 Author

**Krishna Sharma**  
Faculty of Technology, University of Delhi  
*AI/ML & Full-Stack Developer | Geospatial AI • Embedded Systems • RAG • Computer Vision*  
GitHub: [@krishnasharma1493](https://github.com/krishnasharma1493)

---

## 📄 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
