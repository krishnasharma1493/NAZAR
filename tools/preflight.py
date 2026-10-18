#!/usr/bin/env python3
"""preflight.py — one command that says whether NAZAR is ready.  (PRD §13)

    python tools/preflight.py            # software only, no hardware needed
    python tools/preflight.py --hardware # also exercise the ESP32 and the camera

The software half is automated and must be green before travelling. The hardware
half needs the board attached; the camera half needs the camera streaming.
Anything this cannot check is printed as a manual tick-list rather than silently
assumed — an unchecked box is better than a false green.
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY_BIN = ROOT / ".venv" / "bin" / "python"
if not PY_BIN.exists():
    PY_BIN = Path(sys.executable)

OK, BAD, SKIP = "  OK  ", " FAIL ", " SKIP "
results = []


def check(name, fn):
    try:
        detail = fn()
        results.append((OK, name, detail or ""))
    except Exception as e:
        results.append((BAD, name, str(e).split("\n")[0][:140]))


def skip(name, why):
    results.append((SKIP, name, why))


def run(cmd, cwd=ROOT, timeout=900):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stdout + p.stderr).strip().split("\n")[-1])
    return (p.stdout.strip().split("\n") or [""])[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hardware", action="store_true")
    a = ap.parse_args()

    sys.path.insert(0, str(ROOT / "backend"))
    import _bytecode; _bytecode.purge()   # stale .pyc after same-length config edits
    import config

    # ── software ─────────────────────────────────────────────────────
    check("python deps import",
          lambda: run([str(PY_BIN), "-c",
                       "import numpy,scipy,sklearn,serial,fastapi,uvicorn,cryptography,reportlab,httpx;"
                       "print('all import')"]))
    check("detector unit tests (PRD §12)",
          lambda: run([str(PY_BIN), str(ROOT / "backend" / "test_detector.py")]))
    check("optical probe (spotlight) tests",
          lambda: run([str(PY_BIN), str(ROOT / "backend" / "test_probe.py")]))
    check("whole-system pipeline tests",
          lambda: run([str(PY_BIN), str(ROOT / "backend" / "test_pipeline.py")]))
    check("end-to-end integration (replay mode, ~1 min)",
          lambda: run([str(PY_BIN), str(ROOT / "backend" / "test_integration.py")]))
    check("replay reproduces the known-good verdict", _replay_fixture)
    check("firmware/config constants in sync", _constants)
    check("signing key present", _signing_key)
    check("evidence PDF renders", _pdf)
    check("saved captures available as fallback", _captures)

    if (ROOT / "frontend" / "node_modules").exists():
        check("frontend builds", lambda: run(["npm", "run", "build"],
                                             cwd=ROOT / "frontend"))
    else:
        skip("frontend builds", "node_modules missing — run `npm install` in frontend/ "
                                "WHILE YOU STILL HAVE INTERNET")

    # ── hardware ─────────────────────────────────────────────────────
    if a.hardware:
        check("ESP32 streams under channel lock", _sniffer)
        check("channel scan returns devices", _scan)
    else:
        skip("ESP32 streams under channel lock", "run with --hardware")
        skip("channel scan returns devices", "run with --hardware")

    print()
    for status, name, detail in results:
        print(f"[{status}] {name}" + (f"  — {detail}" if detail else ""))
    bad = sum(1 for s, _, _ in results if s == BAD)
    skipped = sum(1 for s, _, _ in results if s == SKIP)
    print(f"\n{len(results)-bad-skipped} passed, {bad} failed, {skipped} skipped")

    print("\nManual, cannot be automated — tick these yourself (PRD §13):")
    for line in [
        "CP-E35A paired in ezykam+, motion tracking OFF, pointed at a still surface",
        "camera charged/powered; adapter and cable packed",
        "spare USB-C DATA cable in the bag",
        "laptop charged; charger packed",
        "Arduino IDE + esp32 core installed on the demo laptop (for a re-flash on site)",
        "Serial Monitor CLOSED before running any Python",
        "five consecutive blind hide-and-find runs completed",
        "deck says ESP32 everywhere, never ESP32-S3",
        "roles assigned: who talks, who drives, who walks to the camera",
    ]:
        print(f"  [ ] {line}")
    return 1 if bad else 0


def _replay_fixture():
    sys.path.insert(0, str(ROOT / "backend"))
    import replay
    f = ROOT / "backend" / "fixtures" / "scan_20260907_205427_camera.csv"
    if not f.exists():
        raise RuntimeError("regression fixture missing")
    r = replay.replay(f, verbose=False)
    if r["verdict"] != "WATCHING":
        raise RuntimeError(f"fixture now returns {r['verdict']} (p={r['p_value']})")
    return f"p={r['p_value']:.4f} lag={r['lag_s']}s WATCHING"


def _constants():
    """Compare the two SOURCE files textually.

    Deliberately does not import config: an import can return a stale cached
    value after a same-length edit, and a drift check that can be fooled by the
    very edit it is meant to catch is worse than none.
    """
    import re
    cfg_src = (ROOT / "backend" / "config.py").read_text()
    ino = (ROOT / "firmware" / "nazar_sniffer" / "nazar_sniffer.ino").read_text()
    pairs = [("BIN_MS", "BIN_MS"), ("MAX_DEV", "MAX_DEVICES"),
             ("SERIAL_BAUD", "SERIAL_BAUD"), ("SCAN_DWELL_MS", "SCAN_DWELL_MS")]
    for ino_name, cfg_name in pairs:
        c = re.search(rf"^{cfg_name}\s*=\s*(\d+)", cfg_src, re.M)
        s = re.search(rf"#define\s+{ino_name}\s+(\d+)", ino)
        if not c or not s:
            raise RuntimeError(f"{ino_name}: not found (config={c}, sketch={s})")
        if int(c.group(1)) != int(s.group(1)):
            raise RuntimeError(f"{ino_name}: sketch={s.group(1)} config={c.group(1)}")
    return "BIN_MS, MAX_DEV, baud, dwell all match (read from source)"


def _signing_key():
    sys.path.insert(0, str(ROOT / "backend"))
    import evidence
    _, p = evidence.ensure_key()
    return f"{p.name} ({oct(p.stat().st_mode)[-3:]})"


def _pdf():
    import json
    sys.path.insert(0, str(ROOT / "backend"))
    import detector, evidence, replay
    from serial_reader import read_capture
    f = ROOT / "backend" / "fixtures" / "scan_20260907_205427_camera.csv"
    res = replay.replay(f, verbose=False)
    rows = read_capture(f)
    t, kb = detector.series_from_rows(rows, res["mac"])
    meta = json.load(open(f.with_suffix(".json")))
    jp, pp = evidence.generate(res, mac=res["mac"], channel=meta["channel"], t_ms=t,
                               bitrate_kbps=kb, t0_ms=meta["t0_ms"],
                               stimulus=detector.build_stimulus(t, meta["t0_ms"]),
                               capture_file=f.name, stem="preflight")
    ok, why = evidence.verify(json.load(open(jp)))
    if not ok:
        raise RuntimeError(why)
    return f"{pp.name} {pp.stat().st_size} bytes, signature valid"


def _captures():
    fx = len(list((ROOT / "backend" / "fixtures").glob("*.csv")))
    cap = len(list((ROOT / "backend" / "captures").glob("*.csv")))
    if fx < 3:
        raise RuntimeError(f"only {fx} committed fixtures; a fresh clone needs these")
    return f"{fx} committed fixtures + {cap} local captures"


def _sniffer():
    sys.path.insert(0, str(ROOT / "backend"))
    from serial_reader import Sniffer
    import config
    sn = Sniffer(config.SERIAL_PORT, config.SERIAL_BAUD)
    sn.open()
    try:
        # The firmware only emits D, lines once a channel is LOCKED, so an
        # unlocked stream proves nothing but that the port opened.
        sn.lock(6)
        rows = list(sn.stream(3.0))
        d = sn.drops
        if d.critical:
            raise RuntimeError(f"loss counters non-zero: {vars(d)}")
        macs = len({r["mac"] for r in rows})
        note = "" if macs else "  (ch6 quiet — not a fault, but verify with a scan)"
        return f"{sn.port} · lock ch6 confirmed · {len(rows)} bins, {macs} devices{note}"
    finally:
        sn.close()


def _scan():
    sys.path.insert(0, str(ROOT / "backend"))
    from serial_reader import Sniffer
    import discovery, config
    sn = Sniffer(config.SERIAL_PORT, config.SERIAL_BAUD)
    sn.open()
    try:
        found = sn.scan()
    finally:
        sn.close()
    rows = discovery.candidates(found, include_all=True)
    if not rows:
        raise RuntimeError("channel scan saw nothing on air")
    top = rows[0]
    return f"{len(rows)} devices, top {top['mac']} {top['up_kbps']:.0f} kbps ch{top['channel']}"


if __name__ == "__main__":
    sys.exit(main())
