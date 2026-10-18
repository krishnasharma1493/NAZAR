"""discovery.py — OUI table + uplink-dominance filter.  (F5, PRD §8 stage 1)

Narrows everything on air to a handful of candidates worth spending 30 s of
stimulus on. This is a *shortlist*, never a verdict. The proof is detector.py.

Three signals, in order of how much they are trusted:

1. **Uplink dominance and sustained rate.** A camera uploads video and receives
   almost nothing. Measured, not inferred. This is the filter that matters.
2. **Globally-administered MAC.** Modern phones and laptops randomise their MAC
   (locally-administered bit set); cameras overwhelmingly do not. Measured on
   7 Sep: every device on air had a randomised address except the camera.
3. **OUI vendor.** A hint only. The table below is small, offline and partial —
   an unknown OUI means nothing, and no candidate is ever rejected for it.
"""
from __future__ import annotations

from config import UPLINK_RATIO, MIN_KBPS, SCAN_DWELL_MS

# Partial, offline, best-effort. Prefixes are lowercase, colon-separated.
# Absence proves nothing: this exists to put a helpful name on a row, not to
# gate anything. Never let a verdict depend on it.
OUI = {
    # Espressif — ESP32-based cameras and IoT, incl. many white-label modules
    "24:0a:c4": "Espressif", "30:ae:a4": "Espressif", "3c:71:bf": "Espressif",
    "84:0d:8e": "Espressif", "8c:aa:b5": "Espressif", "a4:cf:12": "Espressif",
    "b4:e6:2d": "Espressif", "cc:50:e3": "Espressif", "dc:4f:22": "Espressif",
    "ec:fa:bc": "Espressif", "2c:f4:32": "Espressif", "24:62:ab": "Espressif",
    "7c:9e:bd": "Espressif", "e0:98:06": "Espressif", "f4:cf:a2": "Espressif",
    # Hikvision
    "44:47:cc": "Hikvision", "4c:bd:8f": "Hikvision", "54:c4:15": "Hikvision",
    "bc:ad:28": "Hikvision", "c0:56:e3": "Hikvision",
    # Dahua
    "3c:ef:8c": "Dahua", "4c:11:bf": "Dahua", "90:02:a9": "Dahua",
    "e0:50:8b": "Dahua",
    # TP-Link (Tapo cameras)
    "50:c7:bf": "TP-Link", "60:e3:27": "TP-Link", "a0:f3:c1": "TP-Link",
    "ec:08:6b": "TP-Link",
    # Xiaomi / Mi Home
    "28:6c:07": "Xiaomi", "34:ce:00": "Xiaomi", "64:09:80": "Xiaomi",
    "78:11:dc": "Xiaomi", "f0:b4:29": "Xiaomi",
    # Tuya-based white-label (ezykam+, many Indian-market cameras)
    "10:d5:61": "Tuya", "18:69:d8": "Tuya", "68:57:2d": "Tuya",
    "d4:a6:51": "Tuya", "84:e3:42": "Tuya",
    # Amazon (Ring / Blink)
    "44:65:0d": "Amazon", "74:c2:46": "Amazon", "f0:27:2d": "Amazon",
}


def vendor(mac: str) -> str | None:
    return OUI.get(mac.lower()[:8])


def is_randomised(mac: str) -> bool:
    """True if the locally-administered bit is set — a randomised address.

    Phones and laptops randomise; cameras almost never do. A device that both
    transmits hard and keeps a stable factory MAC is unusual, and on 7 Sep the
    camera was the only such device on air.
    """
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, IndexError):
        return False


# A device sending VIDEO fills packets to near the MTU. Heartbeats are a few
# small packets. Measured 8 Sep on real captures:
#
#   streaming camera   9-13 packets/bin, ~1100 bytes/packet, 1500-2000 kbps
#   idle laptop         2   packets/bin,    47 bytes/packet,       9 kbps
#
# Bitrate alone is a poor gate: a camera whose live view has closed still emits
# packets in every bin, so packet RATE looks healthy while carrying no video.
# Payload size is what separates them, and it is the same quantity the detector
# correlates, so the gate and the measurement agree.
MIN_BYTES_PER_PACKET = 400
MIN_PACKETS_PER_BIN = 4.0


def streaming_state(rows, mac, bin_ms=100):
    """Is this device actually sending video right now?

    Returns (ok, detail). `rows` are capture rows for any device; only `mac` is
    considered.
    """
    mac = mac.lower()
    sel = [r for r in rows if r["mac"].lower() == mac]
    if not sel:
        return False, {"reason": "no packets at all from this device",
                       "kbps": 0.0, "bytes_per_packet": 0.0, "packets_per_bin": 0.0}
    pkts = sum(int(r["up_pkts"]) for r in sel)
    byts = sum(int(r["up_bytes"]) for r in sel)
    bins = len({int(r["t_ms"]) for r in sel})
    bpp = byts / max(pkts, 1)
    ppb = pkts / max(bins, 1)
    kbps = byts * 8 / 1000 / max(bins * bin_ms / 1000, 1e-9)
    detail = {"kbps": round(kbps, 1), "bytes_per_packet": round(bpp, 1),
              "packets_per_bin": round(ppb, 1), "bins": bins}
    if bpp < MIN_BYTES_PER_PACKET:
        detail["reason"] = (f"packets average only {bpp:.0f} bytes — heartbeats, "
                            f"not video (video fills packets to ~1100+)")
        return False, detail
    if ppb < MIN_PACKETS_PER_BIN:
        detail["reason"] = (f"only {ppb:.1f} packets per 100 ms — too sparse to "
                            f"measure a response")
        return False, detail
    detail["reason"] = "streaming video"
    return True, detail


def score(dev: dict) -> float:
    """Rank order only. Higher is more camera-like. Never a verdict."""
    up, dn = dev["up_kbps"], dev["dn_kbps"]
    s = min(up / max(MIN_KBPS, 1), 10.0)                 # sustained uplink
    s += min(up / (dn + 1.0), 10.0) / 2.0                # uplink dominance
    if not dev.get("randomised", False):
        s += 3.0                                          # stable factory MAC
    if dev.get("vendor"):
        s += 1.0                                          # known IoT/camera OUI
    return round(s, 2)


def annotate(dev: dict) -> dict:
    """Add vendor / randomised / candidate / score to one scan row."""
    d = dict(dev)
    mac = d["mac"]
    dwell = SCAN_DWELL_MS / 1000
    if "up_kbps" not in d:
        d["up_kbps"] = d.get("up", 0) * 8 / 1000 / dwell
        d["dn_kbps"] = d.get("dn", 0) * 8 / 1000 / dwell
    d["vendor"] = vendor(mac)
    d["randomised"] = is_randomised(mac)
    d["uplink_ratio"] = round(d["up_kbps"] / (d["dn_kbps"] + 1.0), 2)
    d["candidate"] = bool(d["up_kbps"] >= MIN_KBPS and d["uplink_ratio"] >= UPLINK_RATIO)
    d["score"] = score(d)
    return d


def candidates(devices, include_all=False):
    """Annotate and rank a scan. `devices` is an iterable of scan rows.

    Returns candidates by default, everything (ranked) if include_all.
    """
    rows = [annotate(d) for d in (devices.values() if isinstance(devices, dict) else devices)]
    rows.sort(key=lambda r: -r["score"])
    return rows if include_all else [r for r in rows if r["candidate"]]
