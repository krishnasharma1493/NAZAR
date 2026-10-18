#!/usr/bin/env python3
"""nazar_read.py — standalone Gate 1 live-view tool.

Not part of the backend. One job: prove that a camera's uplink bitrate rises
when you wave your hand in front of it (PRD §14, Gate 1).

    python tools/nazar_read.py --scan            # hop 1-13, list what is on air
    python tools/nazar_read.py --channel 6       # lock and watch every device
    python tools/nazar_read.py --channel 6 --mac 34:ea:e7:aa:bb:cc

Close the Arduino Serial Monitor first — it and this cannot both hold the port.
"""
import argparse, csv, re, sys, time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import _bytecode; _bytecode.purge()   # stale .pyc after same-length config edits
import config
import serial

BAR_W = 44

# Strict line grammar. Anything that does not match exactly is discarded and
# counted, never coerced. A silently mis-parsed field is a wrong byte count,
# which is a wrong bitrate, which is a failed detection with no error anywhere.
RE_D = re.compile(r"^D,(\d+),([0-9a-f:]{17}),(\d+),(\d+),(\d+),(-?\d+)$")
RE_S = re.compile(r"^S,(\d+),([0-9a-f:]{17}),(\d+),(\d+),(\d+),(-?\d+)$")
malformed = 0


def parse(rx, line):
    global malformed
    m = rx.match(line)
    if not m:
        malformed += 1
        return None
    return m.groups()


def open_port(port, baud):
    from serial_reader import find_port
    port = find_port(port)          # macOS renumbers usbserial-* between plug-ins
    print(f"  using {port}")
    s = serial.Serial()
    s.port, s.baudrate, s.timeout = port, baud, 0.2
    s.dtr = False                      # do not hold the board in reset
    s.open()
    s.rts = True; time.sleep(0.1); s.rts = False   # pulse EN so we catch the banner
    time.sleep(0.4)
    s.reset_input_buffer()
    return s


def wait_ready(s, timeout=6.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        line = s.readline().decode("utf-8", "replace").strip()
        if line.startswith("#NAZAR_SNIFFER_READY"):
            return True
        if line:
            print("  ...", line)
    return False


def cmd_scan(s):
    print("Scanning channels 1-13, ~%.1f s ..." % (13 * config.SCAN_DWELL_MS / 1000))
    s.write(b"S")
    found = defaultdict(lambda: {"up": 0, "dn": 0, "rssi": -99, "ch": 0})
    while True:
        raw = s.readline().decode("utf-8", "replace").strip()
        if not raw:
            continue
        if raw.startswith("#SCAN_PASS_DONE"):
            break
        if not raw.startswith("S,"):
            continue
        g = parse(RE_S, raw)
        if not g:
            continue
        ch, mac, up, dn, pkts, rssi = g
        e = found[mac]
        # Track the busiest channel per device by total traffic, not by uplink
        # alone — a downlink-only device would otherwise keep its zeroed default.
        if int(up) + int(dn) > e["up"] + e["dn"]:
            e.update(up=int(up), dn=int(dn), rssi=int(rssi), ch=int(ch))

    dwell = config.SCAN_DWELL_MS / 1000
    rows = sorted(found.items(), key=lambda kv: -kv[1]["up"])
    print(f"\n{'MAC':<19}{'CH':>3}{'UP kbps':>10}{'DN kbps':>10}{'RSSI':>7}  candidate?")
    print("-" * 62)
    for mac, e in rows:
        up_k = e["up"] * 8 / 1000 / dwell
        dn_k = e["dn"] * 8 / 1000 / dwell
        # Same shape as the F5 discovery filter, shown here only as a hint.
        cand = up_k >= config.MIN_KBPS and e["up"] / (e["dn"] + 1) >= config.UPLINK_RATIO
        print(f"{mac:<19}{e['ch']:>3}{up_k:>10.1f}{dn_k:>10.1f}{e['rssi']:>7}  {'<-- yes' if cand else ''}")
    if not rows:
        print("(nothing seen — is anything actually transmitting?)")
    if malformed:
        print(f"\n!! {malformed} malformed lines discarded — something else is "
              f"writing to the serial port")
    print()


def cmd_watch(s, channel, only_mac, csv_path):
    s.write(f"C{channel}\n".encode())
    # F4: do not proceed until the board confirms the lock. A board still hopping
    # while we believe it is locked yields a capture that looks fine and is not.
    t0 = time.time()
    while True:
        line = s.readline().decode("utf-8", "replace").strip()
        if line.startswith("#LOCK,"):
            got = int(line.split(",")[1])
            if got != channel:
                sys.exit(f"lock mismatch: asked {channel}, board says {got}")
            print(f"channel lock confirmed: {got}")
            break
        if line.startswith("#ERR"):
            sys.exit(line)
        if time.time() - t0 > 3:
            sys.exit("no #LOCK echo — board did not confirm the channel lock")

    hist = defaultdict(lambda: deque(maxlen=40))
    last = {}
    peak = defaultdict(float)
    drops = "waiting"
    writer = None
    if csv_path:
        fh = open(csv_path, "w", newline="")
        writer = csv.writer(fh)
        writer.writerow(["t_ms", "mac", "up_bytes", "dn_bytes", "up_pkts", "rssi"])

    print("\nwave your hand in front of the camera — ctrl-c to stop\n")
    t_paint = 0.0
    try:
        while True:
            raw = s.readline().decode("utf-8", "replace").strip()
            if not raw:
                continue
            if raw.startswith("#DROP,"):
                _, t, full, ser, late, unp = raw.split(",")
                drops = f"table_full={full} serial={ser} late_bins={late} unparsed={unp}"
                continue
            if not raw.startswith("D,"):
                continue
            g = parse(RE_D, raw)
            if not g:
                continue
            t_ms, mac, up, dn, pkts, rssi = g
            if only_mac and mac.lower() != only_mac.lower():
                continue
            if writer:
                writer.writerow([t_ms, mac, up, dn, pkts, rssi])
            kbps = int(up) * 8 / 1000 / (config.BIN_MS / 1000)
            hist[mac].append(kbps)
            last[mac] = (kbps, int(rssi))
            peak[mac] = max(peak[mac], kbps)

            now = time.time()
            if now - t_paint < 0.2:
                continue
            t_paint = now
            rows = sorted(last.items(), key=lambda kv: -kv[1][0])[:8]
            bad = f"   malformed: {malformed}" if malformed else ""
            out = ["\033[2J\033[H",
                   f"channel {channel}   bin {config.BIN_MS} ms   drops: {drops}{bad}\n"]
            for mac, (kbps, rssi) in rows:
                scale = max(peak[mac], 1.0)
                fill = int(BAR_W * min(kbps / scale, 1.0))
                spark = "".join("▁▂▃▄▅▆▇█"[min(7, int(7 * v / scale))] for v in hist[mac])
                out.append(f"{mac}  {kbps:8.1f} kbps  {rssi:>4} dBm  peak {peak[mac]:7.1f}")
                out.append("  [" + "█" * fill + " " * (BAR_W - fill) + "]")
                out.append("  " + spark + "\n")
            print("\n".join(out), flush=True)
    except KeyboardInterrupt:
        if csv_path:
            fh.close()
            print(f"\nsaved {csv_path}")


def main():
    ap = argparse.ArgumentParser(description="NAZAR Gate 1 live view")
    ap.add_argument("--port", default=config.SERIAL_PORT)
    ap.add_argument("--baud", type=int, default=config.SERIAL_BAUD)
    ap.add_argument("--scan", action="store_true", help="channel sweep, then exit")
    ap.add_argument("--channel", type=int, help="lock this channel and watch")
    ap.add_argument("--mac", help="watch only this MAC")
    ap.add_argument("--save", action="store_true", help="write the capture to backend/captures/")
    a = ap.parse_args()

    print(f"opening {a.port} @ {a.baud}")
    s = open_port(a.port, a.baud)
    if not wait_ready(s):
        sys.exit("no #NAZAR_SNIFFER_READY — wrong baud, wrong sketch, or the "
                 "Arduino Serial Monitor still holds the port")
    print("sniffer ready")

    if a.scan:
        cmd_scan(s)
    elif a.channel:
        path = None
        if a.save:
            d = Path(__file__).resolve().parents[1] / "backend" / "captures"
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"gate1_{datetime.now():%Y%m%d_%H%M%S}.csv"
        cmd_watch(s, a.channel, a.mac, path)
    else:
        ap.error("give --scan or --channel N")
    s.close()


if __name__ == "__main__":
    main()
