#!/usr/bin/env python3
"""replay.py — run the pipeline on a saved capture.  (F9)

    python backend/replay.py backend/captures/scan_20260907_211500.csv
    python backend/replay.py <capture.csv> --mac 28:18:fd:db:c2:f6 --t0-ms 412300

Every scan writes a CSV plus a JSON sidecar holding the stimulus t0, the
sequence and the chip length. Given the sidecar, replay reconstructs the exact
stimulus and runs the identical detector — same code path, no hardware.

Two jobs: regression testing while thresholds are tuned, and a defensible
fallback if the venue RF is hostile. A recorded video of a working demo is an
admission of failure; a replay through the live pipeline is engineering
practice.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bytecode; _bytecode.purge()
import detector
from config import M_SEQUENCE, CHIP_S, P_THRESHOLD
from serial_reader import read_capture


def replay(csv_path, mac=None, t0_ms=None, sequence=None, chip_s=None, verbose=True,
           repeats=None, direction=None):
    csv_path = Path(csv_path)
    side = csv_path.with_suffix(".json")
    meta = json.load(open(side)) if side.exists() else {}

    mac = mac or meta.get("mac")
    if mac is None:
        raise SystemExit("no --mac given and no sidecar to read it from")
    t0_ms = t0_ms if t0_ms is not None else meta.get("t0_ms")
    if t0_ms is None:
        raise SystemExit("no --t0-ms given and no sidecar to read it from")
    sequence = sequence or meta.get("sequence") or M_SEQUENCE
    chip_s = chip_s or meta.get("chip_s") or CHIP_S
    # Captures recorded before repeats existed were a single pass. Defaulting to
    # 1 rather than to the current config keeps every old fixture reproducible.
    repeats = repeats if repeats is not None else meta.get("repeats", 1)
    # Direction is no longer pinned by the product. The 20 s check used to
    # hand the scan a one-sided direction, but its sign is only as good as
    # its phase alignment and a 1 s timing error inverts it exactly — which
    # happened on 8 Sep, pinning "fall" at a camera whose bitrate rose.
    # A stale pin in an old sidecar is therefore a record, not an
    # instruction; replay must mirror what the live pipeline does now.
    direction = direction or "auto"
    # A bare "stim_mode" in an older sidecar merely recorded the config
    # default of the day; the scan never chose it. Only honour it when the
    # capture says the shape was deliberately pinned, otherwise search the
    # same family a live scan would.
    stim_mode = meta.get("stim_mode") if meta.get("stim_pinned") else None

    rows = read_capture(csv_path)
    t, kbps = detector.series_from_rows(rows, mac)      # for display and evidence
    _, feat = detector.series_feature(rows, mac)        # what we correlate
    if len(t) == 0:
        raise SystemExit(f"no rows for {mac} in {csv_path.name}")

    # Must mirror the live scan exactly. Replay is the demo fallback, so a
    # replayed capture that reached a DIFFERENT verdict from the live pipeline
    # would be worse than no fallback at all.
    if stim_mode:                    # the capture pinned one shape
        stim = detector.build_stimulus(t, t0_ms, sequence=sequence, chip_s=chip_s,
                                       repeats=repeats, mode=stim_mode)
        nulls = detector.build_surrogates(t, t0_ms, sequence=sequence, chip_s=chip_s,
                                          repeats=repeats, mode=stim_mode)
    else:
        stim = detector.build_family(t, t0_ms, sequence=sequence, chip_s=chip_s,
                                     repeats=repeats)
        nulls = detector.build_family_surrogates(t, t0_ms, sequence=sequence,
                                                 chip_s=chip_s, repeats=repeats)
    res = detector.detect(feat, stim, nulls, direction=direction)

    cov = detector.stimulus_coverage(t, t0_ms, sequence=sequence, chip_s=chip_s,
                                     repeats=repeats)
    res.update({"mac": mac, "capture": csv_path.name, "bins": int(len(t)),
                "repeats": repeats, "stimulus_coverage": round(cov, 3),
                "duration_s": round((t[-1] - t[0]) / 1000, 1),
                "mean_kbps": float(np.mean(kbps))})
    if verbose:
        report(res)
    return res


def report(res):
    print(f"\ncapture   {res['capture']}")
    print(f"device    {res['mac']}")
    print(f"bins      {res['bins']}  ({res['duration_s']}s, mean {res['mean_kbps']:.0f} kbps)")
    print(f"peak      {res['peak']:.3f} at lag {res['lag_s']:.2f}s")
    print(f"p-value   {res['p_value']:.4f}   ({res['permutations']} {res['null_kind']})")
    if res.get("stimulus_coverage", 1.0) < 0.9:
        print(f"\n!! the capture covers only {res['stimulus_coverage']*100:.0f}% of the "
              f"stimulus window.\n   A truncated scan is not calibrated — treat this "
              f"p-value as unreliable and re-run.")
    print(f"\nVERDICT   {res['verdict']}"
          f"   (needs p < {P_THRESHOLD} and 0.05s < lag < 2.0s)\n")
    _correlogram(res)


def _correlogram(res, W=60):
    R = np.array(res["correlogram"]); lags = np.array(res["lags_s"])
    lo, hi = min(R.min(), 0), max(R.max(), 1e-9)
    print("correlogram")
    for lag, v in zip(lags, R):
        x = int(W * (v - lo) / (hi - lo + 1e-12))
        zero = int(W * (0 - lo) / (hi - lo + 1e-12))
        row = [" "] * (W + 1)
        row[zero] = "|"
        a, b = (zero, x) if x >= zero else (x, zero)
        for i in range(a, b + 1):
            row[i] = "#"
        mark = "  <- peak" if abs(v - R.max()) < 1e-12 else ""
        print(f"  {lag:4.1f}s {v:+.3f} {''.join(row)}{mark}")
    print()


def main():
    ap = argparse.ArgumentParser(description="Replay a saved capture through the detector")
    ap.add_argument("capture")
    ap.add_argument("--mac")
    ap.add_argument("--t0-ms", type=float)
    ap.add_argument("--json", action="store_true", help="emit the result as JSON")
    a = ap.parse_args()
    res = replay(a.capture, a.mac, a.t0_ms, verbose=not a.json)
    if a.json:
        res.pop("correlogram", None); res.pop("lags_s", None)
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
