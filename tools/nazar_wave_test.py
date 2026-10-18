#!/usr/bin/env python3
"""nazar_wave_test.py — controlled, timestamped Gate 1 trial.

Cues you to wave on a schedule, records when each cue fired on the same clock
as the capture, and afterwards compares wave windows against still windows.

    python tools/nazar_wave_test.py --channel 7 --mac 28:18:fd:db:c2:f6

This is NOT the F7 detector. It is the Gate 1 acceptance check: does waving
cause the uplink bitrate to rise, at times we decided in advance? Gate 2
replaces the hand with the m-sequence torch stimulus and this arithmetic with
the matched filter in detector.py.

Design notes that matter:

* Gaps between waves are randomised. A fixed cadence could align with the
  encoder's own GOP / keyframe period and manufacture a correlation out of
  nothing.
* The first GUARD_S after every cue transition is discarded — human reaction
  time plus encoder settling. Including it would dilute the wave windows with
  still traffic and bias the result toward CLEAR.
* The permutation test circularly shifts the cue labels, so the null keeps the
  block structure of the schedule instead of assuming independent bins.
"""
import argparse, csv, json, math, random, sys, time
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import _bytecode; _bytecode.purge()   # stale .pyc after same-length config edits
import config
import serial

GUARD_S = 0.5          # discarded after each cue transition
RE_D = None


def open_port(port, baud):
    from serial_reader import find_port
    port = find_port(port)          # macOS renumbers usbserial-* between plug-ins
    print(f"  using {port}")
    s = serial.Serial()
    s.port, s.baudrate, s.timeout = port, baud, 0.05
    s.dtr = False
    s.open()
    s.rts = True; time.sleep(0.1); s.rts = False
    time.sleep(0.5); s.reset_input_buffer()
    return s


def wait_ready(s, timeout=6.0):
    """Boot banner, or failing that a direct interrogation.

    open_port() resets the board and then flushes the input buffer, which can
    race with the banner and swallow it. Missing the banner is not evidence of a
    dead board, so ask for #STATE before giving up.
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        if s.readline().decode("utf-8", "replace").strip().startswith("#NAZAR_SNIFFER_READY"):
            return True
    s.reset_input_buffer()
    s.write(b"?")
    t0 = time.time()
    while time.time() - t0 < 2.0:
        if s.readline().decode("utf-8", "replace").strip().startswith("#STATE,"):
            return True
    return False


def lock(s, ch):
    s.write(f"C{ch}\n".encode())
    t0 = time.time()
    while time.time() - t0 < 3:
        line = s.readline().decode("utf-8", "replace").strip()
        if line.startswith("#LOCK,"):
            got = int(line.split(",")[1])
            if got != ch:
                sys.exit(f"lock mismatch: asked {ch}, board says {got}")
            return
        if line.startswith("#ERR"):
            sys.exit(line)
    sys.exit("no #LOCK echo — board did not confirm the channel lock")


def build_schedule(n_waves, wave_s, gap_lo, gap_hi, lead_s, rng):
    """[(t_rel, 'WAVE'|'STILL'), ...] — randomised gaps, never a fixed cadence."""
    sched, t = [], lead_s
    sched.append((0.0, "STILL"))
    for _ in range(n_waves):
        sched.append((t, "WAVE"))
        t += wave_s
        sched.append((t, "STILL"))
        t += rng.uniform(gap_lo, gap_hi)
    return sched, t


def run(s, mac, sched, total_s):
    """Drive the cues while draining the serial port. Returns rows + cue log."""
    rows, cues = [], []
    offset = None            # host_clock - board_millis/1000, estimated by minimum latency
    i, t0 = 0, time.monotonic()
    last_shown = None
    print("\n" + "=" * 58)
    while True:
        now = time.monotonic()
        el = now - t0
        if el > total_s:
            break
        # fire cues on schedule
        while i < len(sched) and el >= sched[i][0]:
            t_rel, kind = sched[i]
            cues.append({"t_host": now, "t_rel": t_rel, "kind": kind})
            i += 1
            last_shown = kind
            bar = "\a>>>>>  W A V E   N O W  <<<<<" if kind == "WAVE" else "       hold still"
            print(f"[{el:6.1f}s] {bar}", flush=True)
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
    print("=" * 58 + "\n")
    if offset is None:
        sys.exit(f"no traffic from {mac} — is it still streaming?")
    # map cue host-times onto the board clock
    for c in cues:
        c["t_board_ms"] = (c["t_host"] - offset) * 1000.0
    return rows, cues, offset


def analyse(rows, cues, n_perm):
    t = np.array([r["t_ms"] for r in rows], dtype=float)
    kb = np.array([r["up_bytes"] for r in rows]) * 8 / 1000 / (config.BIN_MS / 1000)

    # label every bin by the cue in force, discarding GUARD_S after transitions
    label = np.full(len(t), "", dtype=object)
    for j, c in enumerate(cues):
        start = c["t_board_ms"] + GUARD_S * 1000
        end = cues[j + 1]["t_board_ms"] if j + 1 < len(cues) else t.max() + 1
        label[(t >= start) & (t < end)] = c["kind"]

    # Any cue that is not STILL is a stimulus block, so the same analysis serves
    # the hand-wave trial and the screen-flash trial without duplicating it.
    is_stim = (label != "") & (label != "STILL")
    wave = kb[is_stim]
    still = kb[label == "STILL"]
    if len(wave) < 5 or len(still) < 5:
        sys.exit("not enough labelled bins — did the camera stream throughout?")

    obs = wave.mean() - still.mean()

    # circular-shift permutation on the labels: keeps the block structure
    lab = is_stim.astype(int)
    valid = label != ""
    rng = np.random.default_rng(0)
    null = np.empty(n_perm)
    for k in range(n_perm):
        sh = np.roll(lab, rng.integers(1, len(lab)))
        w, st = kb[valid & (sh == 1)], kb[valid & (sh == 0)]
        null[k] = w.mean() - st.mean() if len(w) > 4 and len(st) > 4 else -np.inf
    p = (np.sum(null >= obs) + 1) / (n_perm + 1)

    # Per-wave breakdown. "Did every wave move it?" is a stronger and far more
    # assumption-light claim than "were the pooled means different?" — a sign
    # test needs no model of the encoder's noise distribution at all.
    per_wave = []
    for j, c in enumerate(cues):
        if c["kind"] == "STILL":
            continue
        s0 = c["t_board_ms"] + GUARD_S * 1000
        s1 = cues[j + 1]["t_board_ms"] if j + 1 < len(cues) else t.max() + 1
        m = (t >= s0) & (t < s1)
        if not m.any():
            continue
        per_wave.append({"t_rel_s": round((c["t_board_ms"] - t.min()) / 1000, 1),
                         "bins": int(m.sum()), "mean_kbps": float(kb[m].mean()),
                         "peak_kbps": float(kb[m].max()),
                         "ratio": float(kb[m].mean() / (still.mean() + 1e-9))})
    hits = sum(1 for w in per_wave if w["ratio"] > 1.0)
    n_w = len(per_wave)
    # one-sided sign test: P(X >= hits) under Binomial(n, 0.5)
    p_sign = sum(math.comb(n_w, k) for k in range(hits, n_w + 1)) / (2 ** n_w) if n_w else 1.0

    return {
        "per_wave": per_wave, "waves_above_baseline": hits, "n_waves": n_w,
        "p_sign_test": float(p_sign),
        "bins_wave": int(len(wave)), "bins_still": int(len(still)),
        "mean_wave_kbps": float(wave.mean()), "mean_still_kbps": float(still.mean()),
        "median_still_kbps": float(np.median(still)),
        "ratio": float(wave.mean() / (still.mean() + 1e-9)),
        "delta_kbps": float(obs), "p_value": float(p),
        "still_mad_kbps": float(1.4826 * np.median(np.abs(still - np.median(still)))),
    }, t, kb, label


def plot(t, kb, label):
    W = 100
    t0 = t.min(); rel = (t - t0) / 1000.0
    edges = np.linspace(0, rel.max(), W + 1)
    col, mark = [], []
    for i in range(W):
        m = (rel >= edges[i]) & (rel < edges[i + 1])
        col.append(kb[m].max() if m.any() else 0)
        lb = label[m]
        stim = ((lb != "") & (lb != "STILL")).any()
        mark.append("W" if stim else ("." if (lb == "STILL").any() else " "))
    col = np.array(col); top = max(col.max(), 1)
    H = 14
    print(f"\nuplink kbps (peak per column), peak {top:.0f}\n")
    for y in range(H):
        row = "".join("█" if col[x] >= top * (H - y) / H else " " for x in range(W))
        print(f"{top*(H-y)/H:7.0f} |{row}")
        
    print(" " * 8 + "+" + "-" * W)
    print(" " * 9 + "".join(mark) + "   W = wave window")


def replay(path):
    """Run the identical analysis on a saved trial. Same code path, no hardware."""
    if path.suffix != ".csv":
        path = path.with_suffix(".csv")
    meta = json.load(open(path.with_suffix(".json")))
    rows = [{"t_ms": int(x["t_ms"]), "up_bytes": int(x["up_bytes"]),
             "dn_bytes": int(x["dn_bytes"]), "up_pkts": int(x["up_pkts"]),
             "rssi": int(x["rssi"])} for x in csv.DictReader(open(path))]
    res, t, kb, label = analyse(rows, meta["cues"], config.N_PERMUTATIONS)
    print(f"replay of {path.name}  ({meta['mac']}, ch {meta['channel']})")
    plot(t, kb, label)
    report(res)


def report(res, context="gate1"):
    """context="gate1" judges the kill-switch gate; "stimulus" judges only
    whether the stimulus is reaching the lens well enough to run a real scan."""
    print(f"\n{'':<22}{'bins':>6}{'mean kbps':>12}")
    print(f"{'WAVE windows':<22}{res['bins_wave']:>6}{res['mean_wave_kbps']:>12.0f}")
    print(f"{'STILL windows':<22}{res['bins_still']:>6}{res['mean_still_kbps']:>12.0f}")
    print(f"\n{'#':>2} {'at':>8} {'bins':>5} {'mean kbps':>10} {'peak':>8}{'lift':>8}")
    for i, w in enumerate(res["per_wave"], 1):
        print(f"{i:>2} {w['t_rel_s']:>7.1f}s {w['bins']:>5} {w['mean_kbps']:>10.0f}"
              f" {w['peak_kbps']:>8.0f}{w['ratio']:>7.2f}x")
    print(f"\n  lift          {res['ratio']:.2f}x  (+{res['delta_kbps']:.0f} kbps)")
    print(f"  still noise   {res['still_mad_kbps']:.0f} kbps MAD")
    print(f"  waves up      {res['waves_above_baseline']}/{res['n_waves']}"
          f"   sign test p = {res['p_sign_test']:.4f}")
    print(f"  magnitude p   {res['p_value']:.4f}   "
          f"({config.N_PERMUTATIONS} circular-shift permutations)")
    hits, n = res["waves_above_baseline"], res["n_waves"]
    if context == "stimulus":
        # This is a geometry check, not a gate. It answers one question: is the
        # stimulus reaching the lens well enough to be worth a 46 s scan? A
        # perfect score is not required and demanding one sends people away from
        # a setup that works — 7/8 here preceded a Gate 2 pass at p = 0.0055.
        good = hits >= max(1, round(0.75 * n)) and res["p_sign_test"] < 0.05
        if good:
            print(f"\nSTIMULUS REACHES THE CAMERA — {hits}/{n} flashes raised the "
                  f"bitrate ({res['ratio']:.2f}x, sign p={res['p_sign_test']:.4f}).")
            print("  Geometry is good enough. Run nazar_gate2.py now, without moving anything.")
        else:
            print(f"\nSTIMULUS TOO WEAK — only {hits}/{n} flashes raised the bitrate "
                  f"(sign p={res['p_sign_test']:.4f}).")
            print("  Move the camera closer, fill more of its frame, kill the room")
            print("  lights, and make sure nothing else in shot is moving. A full")
            print("  scan from here would waste 46 s and read CLEAR.")
        return
    ok = hits == n and res["ratio"] > 1.0
    if ok:
        print(f"\nGATE 1 PASS — every wave raised the uplink bitrate "
              f"({res['ratio']:.2f}x overall), on timings fixed in advance.")
        print(f"  Gate 2's p < {config.P_THRESHOLD} bar is not this test's job.")
    else:
        print("\nGATE 1 NOT SHOWN — see PRD Rule 1 before spending more time")


def main():
    ap = argparse.ArgumentParser(description="Controlled Gate 1 wave trial")
    ap.add_argument("--port", default=config.SERIAL_PORT)
    ap.add_argument("--baud", type=int, default=config.SERIAL_BAUD)
    ap.add_argument("--channel", type=int)
    ap.add_argument("--mac")
    ap.add_argument("--waves", type=int, default=6)
    ap.add_argument("--wave-s", type=float, default=3.0)
    ap.add_argument("--gap-lo", type=float, default=4.0)
    ap.add_argument("--gap-hi", type=float, default=7.0)
    ap.add_argument("--lead-s", type=float, default=5.0)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--replay", help="re-analyse a saved wave_<stamp>.csv (+ .json), no hardware")
    a = ap.parse_args()

    if a.replay:
        return replay(Path(a.replay))

    if not a.channel or not a.mac:
        ap.error("--channel and --mac are required unless --replay is given")
    mac = a.mac.lower()
    rng = random.Random(a.seed)
    sched, total = build_schedule(a.waves, a.wave_s, a.gap_lo, a.gap_hi, a.lead_s, rng)
    total += 4.0

    print(f"opening {a.port} @ {a.baud}")
    s = open_port(a.port, a.baud)
    if not wait_ready(s):
        sys.exit("no #NAZAR_SNIFFER_READY — close the Arduino Serial Monitor?")
    lock(s, a.channel)
    print(f"channel {a.channel} locked, watching {mac}")
    print(f"\n{a.waves} waves of {a.wave_s}s, randomised {a.gap_lo}-{a.gap_hi}s gaps, "
          f"~{total:.0f}s total")
    print("Wave your whole hand across the lens, close to it, for the WHOLE window.")
    for k in (3, 2, 1):
        print(f"  starting in {k}...", flush=True); time.sleep(1)

    rows, cues, offset = run(s, mac, sched, total)
    s.close()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    d = Path(__file__).resolve().parents[1] / "backend" / "captures"
    d.mkdir(parents=True, exist_ok=True)
    csv_path = d / f"wave_{stamp}.csv"
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["t_ms", "mac", "up_bytes", "dn_bytes", "up_pkts", "rssi"])
        for r in rows:
            w.writerow([r["t_ms"], mac, r["up_bytes"], r["dn_bytes"], r["up_pkts"], r["rssi"]])

    res, t, kb, label = analyse(rows, cues, config.N_PERMUTATIONS)

    json_path = d / f"wave_{stamp}.json"
    json.dump({"mac": mac, "channel": a.channel, "bin_ms": config.BIN_MS,
               "guard_s": GUARD_S, "schedule": sched, "clock_offset_s": offset,
               "cues": [{k: v for k, v in c.items() if k != "t_host"} for c in cues],
               "result": res, "capture": csv_path.name},
              open(json_path, "w"), indent=2)

    plot(t, kb, label)
    print(f"\n{'':<22}{'bins':>6}{'mean kbps':>12}")
    print(f"{'WAVE windows':<22}{res['bins_wave']:>6}{res['mean_wave_kbps']:>12.0f}")
    print(f"{'STILL windows':<22}{res['bins_still']:>6}{res['mean_still_kbps']:>12.0f}")
    print(f"\n{'#':>2} {'at':>8} {'bins':>5} {'mean kbps':>10} {'peak':>8}{'lift':>8}")
    for i, w in enumerate(res["per_wave"], 1):
        print(f"{i:>2} {w['t_rel_s']:>7.1f}s {w['bins']:>5} {w['mean_kbps']:>10.0f}"
              f" {w['peak_kbps']:>8.0f}{w['ratio']:>7.2f}x")

    print(f"\n  lift          {res['ratio']:.2f}x  (+{res['delta_kbps']:.0f} kbps)")
    print(f"  still noise   {res['still_mad_kbps']:.0f} kbps MAD")
    print(f"  waves up      {res['waves_above_baseline']}/{res['n_waves']}"
          f"   sign test p = {res['p_sign_test']:.4f}")
    print(f"  magnitude p   {res['p_value']:.4f}   "
          f"({config.N_PERMUTATIONS} circular-shift permutations)")

    # PRD Rule 1 defines Gate 1 as the bitrate rising when you wave, on known
    # timings. It does NOT require p < P_THRESHOLD — that is Gate 2's bar, and
    # Gate 2 earns it with the m-sequence's processing gain, not a hand.
    ok = res["waves_above_baseline"] == res["n_waves"] and res["ratio"] > 1.0
    if ok:
        print(f"\nGATE 1 PASS — every wave raised the uplink bitrate "
              f"({res['ratio']:.2f}x overall), on timings fixed in advance.")
        print(f"  Gate 2's p < {config.P_THRESHOLD} bar is not this test's job.")
    else:
        print("\nGATE 1 NOT SHOWN — see PRD Rule 1 before spending more time")
    print(f"saved {csv_path.name} + {json_path.name}\n")


if __name__ == "__main__":
    main()
