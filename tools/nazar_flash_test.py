#!/usr/bin/env python3
"""nazar_flash_test.py — does a full-screen white/black flash move the camera?

PRD §11 leaves one decision open: browser torch API vs full-screen flash, to be
settled on the 7th. This answers the half we can measure — whether the flash
produces a measurable, repeatable uplink response at all. If it does not, the
m-sequence has nothing to correlate and the stimulus must be the torch.

    python tools/nazar_flash_test.py --channel 7 --mac 28:18:fd:db:c2:f6

The screen is driven from this same process that drains the serial port, so the
stimulus timestamps and the capture share one clock — no browser, no guesswork.
Analysis is the identical code path that Gate 1 was validated on.

POSITIONING MATTERS MORE THAN THE CODE. A laptop screen is a far weaker emitter
than a torch: point the camera at the screen, fill as much of its frame as you
can, and kill the room lights. A null result from bad geometry looks exactly
like a null result from a stimulus that does not work.
"""
import argparse, csv, json, random, sys, time
import tkinter as tk
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import _bytecode; _bytecode.purge()
import config
import serial
import nazar_wave_test as wt


def build_schedule(n, on_s, gap_lo, gap_hi, lead_s, rng):
    sched = [(0.0, "STILL")]
    t = lead_s
    for _ in range(n):
        sched.append((t, "FLASH")); t += on_s
        sched.append((t, "STILL")); t += rng.uniform(gap_lo, gap_hi)
    return sched, t


def run(s, mac, sched, total_s, root, canvas):
    rows, cues = [], []
    offset = None
    i, t0 = 0, time.monotonic()
    aborted = False

    def on_key(e):
        nonlocal aborted
        if e.keysym == "Escape":
            aborted = True
    root.bind("<Key>", on_key)

    while not aborted:
        now = time.monotonic()
        el = now - t0
        if el > total_s:
            break
        while i < len(sched) and el >= sched[i][0]:
            t_rel, kind = sched[i]
            colour = "#ffffff" if kind == "FLASH" else "#000000"
            canvas.configure(bg=colour)
            root.update_idletasks(); root.update()
            # timestamp AFTER the pixels are pushed, not before
            cues.append({"t_host": time.monotonic(), "t_rel": t_rel, "kind": kind})
            i += 1
        root.update()
        line = s.readline().decode("utf-8", "replace").strip()
        if not line.startswith("D,"):
            continue
        p = line.split(",")
        if len(p) != 7 or p[2] != mac:
            continue
        t_ms = int(p[1])
        lat = time.monotonic() - t_ms / 1000.0
        offset = lat if offset is None else min(offset, lat)
        rows.append({"t_ms": t_ms, "up_bytes": int(p[3]), "dn_bytes": int(p[4]),
                     "up_pkts": int(p[5]), "rssi": int(p[6])})
    if offset is None:
        return rows, cues, None, aborted
    for c in cues:
        c["t_board_ms"] = (c["t_host"] - offset) * 1000.0
    return rows, cues, offset, aborted


def main():
    ap = argparse.ArgumentParser(description="Full-screen flash sanity check")
    ap.add_argument("--port", default=config.SERIAL_PORT)
    ap.add_argument("--baud", type=int, default=config.SERIAL_BAUD)
    ap.add_argument("--channel", type=int, required=True)
    ap.add_argument("--mac", required=True)
    ap.add_argument("--flashes", type=int, default=8)
    ap.add_argument("--on-s", type=float, default=2.0, help="matches config.CHIP_S")
    ap.add_argument("--gap-lo", type=float, default=3.0)
    ap.add_argument("--gap-hi", type=float, default=5.5)
    ap.add_argument("--lead-s", type=float, default=6.0)
    ap.add_argument("--setup-s", type=float, default=15.0, help="aiming time before the run")
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args()

    mac = a.mac.lower()
    rng = random.Random(a.seed)
    sched, total = build_schedule(a.flashes, a.on_s, a.gap_lo, a.gap_hi, a.lead_s, rng)
    total += 4.0

    print(f"opening serial @ {a.baud}")
    s = wt.open_port(a.port, a.baud)
    if not wt.wait_ready(s):
        sys.exit("no #NAZAR_SNIFFER_READY — close the Arduino Serial Monitor?")
    wt.lock(s, a.channel)
    print(f"channel {a.channel} locked, watching {mac}")
    print(f"\n{a.flashes} flashes of {a.on_s}s, randomised {a.gap_lo}-{a.gap_hi}s gaps, "
          f"~{total:.0f}s total")

    print("\nSet up first:")
    print("  · point the camera at this laptop screen, filling as much of its frame as you can")
    print("  · room lights OFF")
    print("  · screen brightness MAX")
    print("  · keep the ezykam+ live view open so the camera keeps streaming")
    try:
        input(f"\npress Enter to start the {a.setup_s:.0f}s aiming countdown (Esc aborts once running) ")
    except EOFError:
        pass

    root = tk.Tk()
    root.title("NAZAR flash test")
    root.attributes("-fullscreen", True)
    canvas = tk.Frame(root, bg="#000000")
    canvas.pack(fill="both", expand=True)
    msg = tk.Label(canvas, bg="#000000", fg="#666666", justify="center",
                   font=("Helvetica", 22),
                   text=("POINT THE CAMERA AT THIS SCREEN\n\n"
                         "fill its frame · lights off · screen brightness max\n\n"
                         "Esc aborts"))
    msg.pack(expand=True)
    root.update()

    t0 = time.monotonic()
    while time.monotonic() - t0 < a.setup_s:
        left = a.setup_s - (time.monotonic() - t0)
        msg.configure(text=("POINT THE CAMERA AT THIS SCREEN\n\n"
                            "fill its frame · lights off · screen brightness max\n\n"
                            f"starting in {left:.0f}s     ·     Esc aborts"))
        root.update()
        s.readline()
    msg.destroy()
    root.update()

    rows, cues, offset, aborted = run(s, mac, sched, total, root, canvas)
    root.destroy()
    s.close()

    if aborted:
        sys.exit("aborted")
    if offset is None or not rows:
        sys.exit(f"no traffic from {mac} — did the camera stop streaming?")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    d = Path(__file__).resolve().parents[1] / "backend" / "captures"
    csv_path = d / f"flash_{stamp}.csv"
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["t_ms", "mac", "up_bytes", "dn_bytes", "up_pkts", "rssi"])
        for r in rows:
            w.writerow([r["t_ms"], mac, r["up_bytes"], r["dn_bytes"], r["up_pkts"], r["rssi"]])

    res, t, kb, label = wt.analyse(rows, cues, config.N_PERMUTATIONS)
    json.dump({"mac": mac, "channel": a.channel, "bin_ms": config.BIN_MS,
               "stimulus": "fullscreen_flash", "on_s": a.on_s,
               "guard_s": wt.GUARD_S, "schedule": sched, "clock_offset_s": offset,
               "cues": [{k: v for k, v in c.items() if k != "t_host"} for c in cues],
               "result": res, "capture": csv_path.name},
              open(d / f"flash_{stamp}.json", "w"), indent=2)

    print(f"\nfull-screen flash — {mac}, ch {a.channel}")
    wt.plot(t, kb, label)
    wt.report(res, context="stimulus")
    print(f"saved {csv_path.name} + flash_{stamp}.json\n")


if __name__ == "__main__":
    main()
