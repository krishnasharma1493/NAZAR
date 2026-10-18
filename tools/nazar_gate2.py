#!/usr/bin/env python3
"""nazar_gate2.py — the full Gate 2 scan: stimulus -> capture -> detector.

    python tools/nazar_gate2.py --channel 7 --mac 28:18:fd:db:c2:f6
    python tools/nazar_gate2.py --channel 7 --mac <laptop-mac>   # negative control

Strobes the m-sequence full-screen while capturing the device's uplink, then
runs detector.detect. Saves capture + sidecar so replay.py reproduces the
verdict without hardware.

GATE 2 (PRD §14): p < 0.01 for the camera, p > 0.5 for a laptop, quiet room.

Two details that are easy to get wrong and impossible to notice afterwards:

* The lead-in holds the screen at the LAST chip's value, so the transition into
  chip 0 is a real one. The detector's regressor derives edges with wraparound;
  if the lead-in were blank instead, the first edge would be fictional.
* t0 is stamped after the first chip is actually painted, and mapped onto the
  board's clock via the minimum-latency offset — not the host clock.
"""
import argparse, json, sys, time
import tkinter as tk
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import _bytecode; _bytecode.purge()
import detector
from config import (SERIAL_PORT, SERIAL_BAUD, M_SEQUENCE, CHIP_S, SCAN_S,
                    P_THRESHOLD, BIN_MS, SEQ_REPEATS, SEQ_PERIOD_S)
from serial_reader import Sniffer, write_capture, RE_D
import replay as replay_mod

WHITE, BLACK = "#ffffff", "#000000"


def run_scan(sn, mac, root, frame, sequence, chip_s, tail_s, repeats=1):
    """Paint the sequence while draining the port. Returns (rows, t0_board_ms)."""
    rows, t0_host = [], None
    chip = -1
    total = len(sequence) * chip_s * repeats + tail_s
    start = time.monotonic()
    while True:
        now = time.monotonic()
        el = now - start
        if el > total:
            break
        want = int(el // chip_s)
        if want != chip and want < len(sequence) * repeats:
            chip = want
            frame.configure(bg=WHITE if sequence[chip % len(sequence)] else BLACK)
            root.update_idletasks(); root.update()
            if chip == 0:
                t0_host = time.monotonic()      # stamped after the pixels land
        root.update()
        line = sn._line()
        if not line:
            continue
        if line.startswith("#"):
            sn._note(line); continue
        m = RE_D.match(line)
        if not m:
            sn.drops.malformed += 1
            continue
        t_ms, dmac, up, dn, pkts, rssi = m.groups()
        lat = time.monotonic() - int(t_ms) / 1000.0
        sn.offset = lat if sn.offset is None else min(sn.offset, lat)
        if dmac != mac.lower():
            continue
        rows.append({"t_ms": int(t_ms), "mac": dmac, "up_bytes": int(up),
                     "dn_bytes": int(dn), "up_pkts": int(pkts), "rssi": int(rssi)})
    return rows, (sn.host_to_board_ms(t0_host) if t0_host and sn.offset else None)


def main():
    ap = argparse.ArgumentParser(description="Gate 2: m-sequence scan + detector")
    ap.add_argument("--port", default=SERIAL_PORT)
    ap.add_argument("--baud", type=int, default=SERIAL_BAUD)
    ap.add_argument("--channel", type=int, required=True)
    ap.add_argument("--mac", required=True)
    ap.add_argument("--label", default="", help="e.g. 'camera' or 'laptop-control'")
    ap.add_argument("--lead-s", type=float, default=6.0)
    ap.add_argument("--tail-s", type=float, default=4.0, help="covers the last chip's response")
    ap.add_argument("--setup-s", type=float, default=12.0)
    a = ap.parse_args()

    mac = a.mac.lower()
    seq = list(M_SEQUENCE)

    sn = Sniffer(a.port, a.baud)          # resolves the port by USB ID
    print(f"opening {sn.port} @ {a.baud}")
    sn.open()
    ch = sn.lock(a.channel)
    print(f"channel {ch} locked, watching {mac}")
    print(f"stimulus: {len(seq)} chips x {CHIP_S}s x {SEQ_REPEATS} passes = {SCAN_S}s "
          f"(+{a.lead_s}s lead-in, +{a.tail_s}s tail)")

    print("\nSet up:")
    print("  · point the camera at this screen, filling its frame")
    print("  · lights off, brightness max, ezykam+ live view open")
    try:
        input(f"\npress Enter to start the {a.setup_s:.0f}s aiming countdown ")
    except EOFError:
        pass

    root = tk.Tk(); root.title("NAZAR")
    root.attributes("-fullscreen", True)
    frame = tk.Frame(root, bg=BLACK); frame.pack(fill="both", expand=True)
    lbl = tk.Label(frame, bg=BLACK, fg="#555555", font=("Helvetica", 20),
                   text="aiming...")
    lbl.pack(expand=True)
    root.update()

    # Lead-in holds the LAST chip's value so the edge into chip 0 is genuine.
    lead_colour = WHITE if seq[-1] else BLACK
    t0 = time.monotonic()
    while time.monotonic() - t0 < a.setup_s:
        left = a.setup_s - (time.monotonic() - t0)
        lbl.configure(text=f"point the camera at this screen\n\nstarting in {left:.0f}s")
        root.update(); sn._line()
    lbl.destroy()
    frame.configure(bg=lead_colour); root.update()
    t0 = time.monotonic()
    while time.monotonic() - t0 < a.lead_s:
        root.update(); sn._line()

    rows, t0_board = run_scan(sn, mac, root, frame, seq, CHIP_S, a.tail_s,
                              repeats=SEQ_REPEATS)
    root.destroy(); sn.close()

    if not rows:
        sys.exit(f"no traffic from {mac} — did it stop streaming?")
    if t0_board is None:
        sys.exit("could not map the stimulus clock onto the board clock")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = f"_{a.label}" if a.label else ""
    d = Path(__file__).resolve().parents[1] / "backend" / "captures"
    csv_path = write_capture(d / f"scan_{stamp}{tag}.csv", rows)
    json.dump({"mac": mac, "channel": ch, "t0_ms": t0_board, "sequence": seq,
               "chip_s": CHIP_S, "bin_ms": BIN_MS, "label": a.label,
               "repeats": SEQ_REPEATS,
               "stimulus": "fullscreen_flash", "lead_colour": lead_colour,
               "clock_offset_s": sn.offset,
               "drops": vars(sn.drops)},
              open(csv_path.with_suffix(".json"), "w"), indent=2)

    print(f"\ndrops: {vars(sn.drops)}")
    if sn.drops.critical:
        print("!! non-zero loss counters — this capture is suspect (PRD §9)")

    res = replay_mod.replay(csv_path, verbose=True)
    ok = res["p_value"] < P_THRESHOLD and res["verdict"] == "WATCHING"
    print(("GATE 2 (this device): p < %.2f, WATCHING" % P_THRESHOLD) if ok
          else "this device: not WATCHING")
    print(f"saved {csv_path.name} + {csv_path.with_suffix('.json').name}\n")


if __name__ == "__main__":
    main()
