"""serial_reader.py — port -> 100 ms bins, writes capture CSV.  (F2 host side)

Canonical serial client. The standalone tools in tools/ predate this and keep
their own copies deliberately: they are the Gate 1 instruments and are not
touched once verified.

Line grammar is strict. Anything that does not match is counted, never coerced —
a silently mis-parsed field is a wrong byte count, which is a wrong bitrate,
which is a failed detection with no error message anywhere.
"""
from __future__ import annotations

import csv
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import serial

from config import SERIAL_PORT, SERIAL_BAUD, BIN_MS, SCAN_DWELL_MS, CHANNELS

# CH340 / CH341 USB-serial bridge, as fitted to the OceanLabz ESP32 board.
CH340_VID = 0x1A86
CH340_PIDS = (0x7523, 0x7522, 0x5523)


def find_port(preferred=None):
    """Locate the sniffer's serial port.

    macOS renumbers /dev/cu.usbserial-* between plug-ins and between USB ports —
    the same board appeared as -110 and later as -10. Hard-coding the path is a
    guaranteed failure at the venue, so the port is identified by its USB IDs.

    `preferred` (config.SERIAL_PORT) pins a specific path when it is a real
    device; "auto", None or a stale path fall through to detection.
    """
    from serial.tools import list_ports
    if preferred and str(preferred).lower() != "auto" and Path(str(preferred)).exists():
        return str(preferred)

    ports = list(list_ports.comports())
    exact = [p.device for p in ports
             if p.vid == CH340_VID and (p.pid in CH340_PIDS or p.pid is None)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise RuntimeError(
            "more than one CH340 device attached: " + ", ".join(exact) +
            " — unplug the others, or set SERIAL_PORT in config.py")

    # Fall back to name matching, for a bridge we do not have IDs for
    named = [p.device for p in ports
             if "usbserial" in p.device or "wchusbserial" in p.device
             or "usbmodem" in p.device]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        raise RuntimeError("several USB serial devices: " + ", ".join(named) +
                           " — set SERIAL_PORT in config.py")

    seen = ", ".join(p.device for p in ports) or "none"
    raise RuntimeError(
        "no ESP32 found. Ports visible: " + seen + ". Check the USB cable is a "
        "DATA cable, and that the board's power LED is on")


RE_D = re.compile(r"^D,(\d+),([0-9a-f:]{17}),(\d+),(\d+),(\d+),(-?\d+)$")
RE_S = re.compile(r"^S,(\d+),([0-9a-f:]{17}),(\d+),(\d+),(\d+),(-?\d+)$")
RE_DROP = re.compile(r"^#DROP,(\d+),(\d+),(\d+),(\d+),(\d+)$")


@dataclass
class Drops:
    table_full: int = 0
    serial: int = 0
    late_bins: int = 0
    unparsed: int = 0
    malformed: int = 0          # counted by this parser, not the firmware

    @property
    def critical(self) -> int:
        """Counters that invalidate a capture. `unparsed` is normal and excluded."""
        return self.table_full + self.serial + self.late_bins + self.malformed


class Sniffer:
    mode = "live"

    def __init__(self, port=SERIAL_PORT, baud=SERIAL_BAUD, timeout=0.2):
        # Resolved late so a board plugged in after import is still found.
        self.port, self.baud, self.timeout = find_port(port), baud, timeout
        self.s = None
        self.drops = Drops()
        self.offset = None      # host_monotonic - board_ms/1000, min-latency estimate

    def __enter__(self):
        self.open(); return self

    def __exit__(self, *a):
        self.close()

    def open(self):
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = self.port, self.baud, self.timeout
        s.dtr = False
        s.open()
        s.rts = True; time.sleep(0.1); s.rts = False     # pulse EN
        time.sleep(0.5); s.reset_input_buffer()
        self.s = s
        if not self._ready():
            raise RuntimeError("sniffer not streaming — check USB, or the "
                               "Arduino Serial Monitor is holding the port")
        return self

    def close(self):
        if self.s and self.s.is_open:
            self.s.close()

    def _line(self):
        return self.s.readline().decode("utf-8", "replace").strip()

    def _ready(self, timeout=6.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self._line().startswith("#NAZAR_SNIFFER_READY"):
                return True
        # The banner races with the buffer flush in open(); missing it is not
        # evidence of a dead board, so ask directly.
        self.s.reset_input_buffer(); self.s.write(b"?")
        t0 = time.time()
        while time.time() - t0 < 2.0:
            if self._line().startswith("#STATE,"):
                return True
        return False

    def _note(self, line):
        m = RE_DROP.match(line)
        if m:
            _, full, ser, late, unp = m.groups()
            self.drops.table_full = int(full); self.drops.serial = int(ser)
            self.drops.late_bins = int(late); self.drops.unparsed = int(unp)

    def state(self, timeout=2.0):
        """Ask the board what it thinks it is doing (the `?` command).

        Returns the parsed #STATE line. This is what makes /health honest: the
        firmware only emits D, lines once a channel is LOCKED, so counting
        devices on an unlocked port always reports zero and tells you nothing.
        """
        self.s.reset_input_buffer()
        self.s.write(b"?")
        t0 = time.time()
        while time.time() - t0 < timeout:
            line = self._line()
            if not line.startswith("#STATE,"):
                continue
            parts = line.split(",")
            if len(parts) < 9:
                continue
            out = {"mode": parts[1], "channel": int(parts[2]),
                   "bin_ms": int(parts[3]), "max_dev": int(parts[4]),
                   "drops": {"table_full": int(parts[5]), "serial": int(parts[6]),
                             "late_bins": int(parts[7]), "unparsed": int(parts[8])}}
            for extra in parts[9:]:
                if "=" in extra:
                    k, v = extra.split("=", 1)
                    out[k] = int(v) if v.isdigit() else v
            return out
        raise RuntimeError("board did not answer `?`")

    def lock(self, channel):
        """F4. Returns only once the board has ECHOED the lock."""
        if channel not in CHANNELS:
            raise ValueError("channel must be 1-13")
        self.s.write(f"C{channel}\n".encode())
        t0 = time.time()
        while time.time() - t0 < 3.0:
            line = self._line()
            if line.startswith("#LOCK,"):
                got = int(line.split(",")[1])
                if got != channel:
                    raise RuntimeError(f"lock mismatch: asked {channel}, board says {got}")
                return got
            if line.startswith("#ERR"):
                raise RuntimeError(line)
        raise RuntimeError("no #LOCK echo — board did not confirm the channel lock")

    def scan(self):
        """F3. One sweep of channels 1-13. Returns {mac: {...}}."""
        self.s.write(b"S")
        found, dwell = {}, SCAN_DWELL_MS / 1000
        deadline = time.time() + len(list(CHANNELS)) * dwell + 8
        while time.time() < deadline:
            line = self._line()
            if line.startswith("#SCAN_PASS_DONE"):
                break
            if line.startswith("#"):
                self._note(line); continue
            m = RE_S.match(line)
            if not m:
                if line:
                    self.drops.malformed += 1
                continue
            ch, mac, up, dn, pkts, rssi = m.groups()
            e = found.setdefault(mac, {"mac": mac, "up": 0, "dn": 0, "rssi": -99, "channel": 0})
            if int(up) + int(dn) > e["up"] + e["dn"]:
                e.update(up=int(up), dn=int(dn), rssi=int(rssi), channel=int(ch))
        for e in found.values():
            e["up_kbps"] = e["up"] * 8 / 1000 / dwell
            e["dn_kbps"] = e["dn"] * 8 / 1000 / dwell
        return found

    def stream(self, seconds, mac=None):
        """Drain D lines for `seconds`. Yields dict rows; tracks the clock offset."""
        t_end = time.monotonic() + seconds
        while time.monotonic() < t_end:
            line = self._line()
            if not line:
                continue
            if line.startswith("#"):
                self._note(line); continue
            m = RE_D.match(line)
            if not m:
                self.drops.malformed += 1
                continue
            t_ms, dmac, up, dn, pkts, rssi = m.groups()
            lat = time.monotonic() - int(t_ms) / 1000.0
            self.offset = lat if self.offset is None else min(self.offset, lat)
            if mac and dmac != mac.lower():
                continue
            yield {"t_ms": int(t_ms), "mac": dmac, "up_bytes": int(up),
                   "dn_bytes": int(dn), "up_pkts": int(pkts), "rssi": int(rssi)}

    def host_to_board_ms(self, t_host):
        """Map a host monotonic instant onto the board's millis() clock."""
        if self.offset is None:
            raise RuntimeError("no traffic seen yet — clock offset unknown")
        return (t_host - self.offset) * 1000.0


class ReplaySniffer:
    """Drop-in stand-in for Sniffer that replays a saved capture in real time.

    PRD §12: "a recorded video of a working demo is an admission of failure; a
    replay through the live pipeline is engineering practice." This is that
    fallback — the detector, the API and the UI are the real ones; only the
    bytes come from disk instead of the radio.

    It paces rows by their own timestamps, so a 30 s capture takes 30 s and the
    stimulus screen stays in step. It reports `mode == "replay"` so nothing
    downstream can mistake it for a live measurement.
    """

    mode = "replay"

    def __init__(self, source, port=None, baud=None, timeout=0.2):
        self.source = Path(source)
        if not self.source.exists():
            raise RuntimeError(f"replay source not found: {source}")
        self.rows = read_capture(self.source)
        if not self.rows:
            raise RuntimeError(f"replay source is empty: {source}")
        # The sidecar carries the stimulus t0 that was actually transmitted when
        # this capture was recorded. Replaying against a freshly computed t0
        # would misalign the correlation and turn a known WATCHING into CLEAR,
        # which would make the fallback worse than useless.
        self.sidecar = {}
        side = self.source.with_suffix(".json")
        if side.exists():
            import json
            try:
                self.sidecar = json.load(open(side))
            except Exception:
                self.sidecar = {}
        self.drops = Drops()
        self.offset = None
        self._t0_board = self.rows[0]["t_ms"]
        self._start = None
        self._i = 0

    def __enter__(self):
        self.open(); return self

    def __exit__(self, *a):
        self.close()

    def open(self):
        self._start = time.monotonic()
        self._i = 0
        self.offset = self._start - self._t0_board / 1000.0
        return self

    def close(self):
        pass

    def lock(self, channel):
        return channel

    def scan(self):
        dwell = SCAN_DWELL_MS / 1000
        found = {}
        for r in self.rows:
            e = found.setdefault(r["mac"], {"mac": r["mac"], "up": 0, "dn": 0,
                                            "rssi": r["rssi"], "channel": 0})
            e["up"] += r["up_bytes"]; e["dn"] += r["dn_bytes"]; e["rssi"] = r["rssi"]
        span = max((self.rows[-1]["t_ms"] - self.rows[0]["t_ms"]) / 1000.0, dwell)
        for e in found.values():
            # normalise to a single dwell so the numbers read like a real sweep
            e["up_kbps"] = e["up"] * 8 / 1000 / span
            e["dn_kbps"] = e["dn"] * 8 / 1000 / span
            e["up"] = int(e["up"] * dwell / span); e["dn"] = int(e["dn"] * dwell / span)
        time.sleep(min(1.0, span * 0.05))
        return found

    def stream(self, seconds, mac=None):
        """Yield rows at the pace their own timestamps imply."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and self._i < len(self.rows):
            r = self.rows[self._i]
            due = self._start + (r["t_ms"] - self._t0_board) / 1000.0
            now = time.monotonic()
            if due > now:
                if due >= end:
                    break
                time.sleep(min(due - now, 0.05))
                continue
            self._i += 1
            if mac and r["mac"] != mac.lower():
                continue
            yield dict(r)

    def host_to_board_ms(self, t_host):
        return (t_host - self.offset) * 1000.0


def resolve_replay_source(name):
    """Find a capture by bare filename in captures/ then fixtures/."""
    here = Path(__file__).resolve().parent
    safe = Path(str(name)).name
    for d in (here / "captures", here / "fixtures"):
        if (d / safe).exists():
            return d / safe
    p = Path(name)
    return p if p.exists() else None


def make_sniffer(port=SERIAL_PORT, baud=SERIAL_BAUD):
    """Live sniffer, or the replay stand-in when a replay source is configured.

    Precedence: NAZAR_REPLAY env var, then config.REPLAY_SOURCE, else hardware.
    """
    import os
    from config import REPLAY_SOURCE
    name = os.environ.get("NAZAR_REPLAY") or REPLAY_SOURCE
    if name:
        src = resolve_replay_source(name)
        if src is None:
            raise RuntimeError(f"replay source not found: {name}")
        return ReplaySniffer(src)
    return Sniffer(port, baud)


FIELDS = ["t_ms", "mac", "up_bytes", "dn_bytes", "up_pkts", "rssi"]


def write_capture(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in FIELDS})
    return path


def read_capture(path):
    with open(path) as fh:
        return [{k: (v if k == "mac" else int(v)) for k, v in r.items() if k in FIELDS}
                for r in csv.DictReader(fh)]
