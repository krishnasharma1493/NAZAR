#!/usr/bin/env python3
"""venue_recon.py — the first twenty minutes on site.  (PRD §15)

    python tools/venue_recon.py                 # 3 sweep passes, then a load test
    python tools/venue_recon.py --passes 5

Answers the four questions that decide whether the demo works in this room:

  1. How crowded is each channel?
  2. Which channel is the camera on, and how busy is it there?
  3. Does the sniffer lose data under this room's load?
  4. Does MAX_DEVICES need to come down?

It changes nothing. It measures, and it tells you what to change.
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import _bytecode; _bytecode.purge()   # stale .pyc after same-length config edits
import config
import discovery
from serial_reader import Sniffer

BAR = 34


def main():
    ap = argparse.ArgumentParser(description="Venue RF recon")
    ap.add_argument("--passes", type=int, default=3)
    ap.add_argument("--load-seconds", type=float, default=20.0)
    ap.add_argument("--camera-mac", default=None, help="if you already know it")
    a = ap.parse_args()

    sn = Sniffer(config.SERIAL_PORT, config.SERIAL_BAUD)
    print(f"opening {sn.port} @ {config.SERIAL_BAUD}")
    sn.open()

    # ── 1. occupancy ────────────────────────────────────────────────
    per_ch = defaultdict(lambda: {"macs": set(), "kbps": 0.0})
    devices = {}
    try:
        for i in range(a.passes):
            print(f"  sweep pass {i+1}/{a.passes} …", flush=True)
            found = sn.scan()
            for mac, e in found.items():
                ch = e["channel"]
                per_ch[ch]["macs"].add(mac)
                per_ch[ch]["kbps"] += e["up_kbps"] + e["dn_kbps"]
                prev = devices.get(mac)
                if prev is None or e["up_kbps"] > prev["up_kbps"]:
                    devices[mac] = e
        for ch in per_ch:
            per_ch[ch]["kbps"] /= a.passes

        rows = discovery.candidates(devices, include_all=True)
        cams = [r for r in rows if r["candidate"]]
        if a.camera_mac:
            cams = [r for r in rows if r["mac"] == a.camera_mac.lower()] or cams

        print(f"\n{'ch':>3}{'devices':>9}{'total kbps':>12}   occupancy")
        busiest = max((v["kbps"] for v in per_ch.values()), default=1.0) or 1.0
        for ch in sorted(per_ch):
            v = per_ch[ch]
            fill = int(BAR * v["kbps"] / busiest)
            print(f"{ch:>3}{len(v['macs']):>9}{v['kbps']:>12.0f}   "
                  f"{'█'*fill}{'·'*(BAR-fill)}")

        total_dev = len(devices)
        print(f"\n{total_dev} distinct devices on air across {len(per_ch)} channels")

        print(f"\n{'MAC':<19}{'ch':>3}{'up kbps':>9}{'ratio':>7}{'MAC type':>12}  candidate")
        for r in rows[:10]:
            print(f"{r['mac']:<19}{r['channel']:>3}{r['up_kbps']:>9.0f}"
                  f"{r['uplink_ratio']:>7.1f}"
                  f"{'randomised' if r['randomised'] else 'factory':>12}"
                  f"  {'YES' if r['candidate'] else ''}")

        # ── 2. load test on the channel that matters ────────────────
        target_ch = cams[0]["channel"] if cams else (
            max(per_ch, key=lambda c: per_ch[c]["kbps"]) if per_ch else 6)
        why = "the camera's channel" if cams else "the busiest channel"
        print(f"\nlocking channel {target_ch} ({why}) for {a.load_seconds:.0f}s "
              f"to measure loss under this room's load …")
        sn.lock(target_ch)
        before = dict(vars(sn.drops))
        rows_seen = list(sn.stream(a.load_seconds))
        d = vars(sn.drops)
        macs_locked = len({r["mac"] for r in rows_seen})
        bins = len({r["t_ms"] for r in rows_seen})
        expected = int(a.load_seconds * 1000 / config.BIN_MS)
        print(f"  {len(rows_seen)} rows, {macs_locked} devices, {bins}/{expected} bins seen")
        print(f"  drops: {d}")
    finally:
        sn.close()

    # ── 3. the verdict, in plain terms ──────────────────────────────
    print("\n" + "=" * 62)
    problems = []
    if d["table_full"]:
        problems.append(
            f"table_full={d['table_full']}: more than {config.MAX_DEVICES} devices on "
            f"channel {target_ch}. The least active are being dropped.")
    if d["serial"]:
        problems.append(
            f"serial={d['serial']}: the link cannot carry this much traffic.")
    if d["late_bins"]:
        problems.append(
            f"late_bins={d['late_bins']}: bins were missed. This is the worst one — "
            f"the time axis IS the correlation's lag axis.")
    if bins < expected * 0.9:
        problems.append(f"only {bins} of {expected} bins arrived.")

    if not problems:
        print("CLEAN — the sniffer keeps up with this room.")
        print(f"  Use channel {target_ch}. No config change needed.")
    else:
        print("LOSS DETECTED — fix before trusting any verdict here:")
        for p in problems:
            print(f"  · {p}")
        suggested = 24 if config.MAX_DEVICES > 24 else 16
        print(f"\n  First move (README): lower MAX_DEVICES {config.MAX_DEVICES} -> {suggested}")
        print(f"    python tools/set_max_devices.py {suggested} --flash")
        print("  Serial load is linear in the device count, and NAZAR only ever")
        print("  correlates one MAC at a time, so a smaller table costs nothing")
        print("  that matters. Re-run this recon afterwards.")

    if cams:
        c = cams[0]
        print(f"\nCandidate to test: {c['mac']} on channel {c['channel']} "
              f"({c['up_kbps']:.0f} kbps up)")
        print(f"  python tools/nazar_flash_test.py --channel {c['channel']} --mac {c['mac']}")
        print(f"  python tools/nazar_gate2.py --channel {c['channel']} --mac {c['mac']} --label venue")
    else:
        print("\nNo candidate met the F5 filter. If the camera should be here, check")
        print("it is actually streaming (open its live view) and sweep again.")
    print("=" * 62)


if __name__ == "__main__":
    main()
