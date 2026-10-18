#!/usr/bin/env python3
"""set_max_devices.py — change the one venue tuning lever, safely.

    python tools/set_max_devices.py 24            # edit both files
    python tools/set_max_devices.py 24 --flash    # edit, then re-flash the board

`MAX_DEVICES` in config.py and `MAX_DEV` in the sketch MUST match. They live in
two files with no build system to enforce it, and a mismatch produces a wrong
device table with no error message anywhere. Doing this by hand at 2 a.m. in a
noisy hall is exactly when it gets forgotten, so this does both and verifies.

Lowering the table is the documented first response to non-zero drop counters:
serial load is linear in the device count, and NAZAR only ever correlates one
MAC at a time, so a smaller table costs nothing that matters.
"""
import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import _bytecode; _bytecode.purge()   # so the next run reads the edited values
CONFIG = ROOT / "backend" / "config.py"
SKETCH = ROOT / "firmware" / "nazar_sniffer" / "nazar_sniffer.ino"
CLI = Path("/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/"
           "resources/arduino-cli")


def current():
    c = re.search(r"^MAX_DEVICES\s*=\s*(\d+)", CONFIG.read_text(), re.M)
    s = re.search(r"#define\s+MAX_DEV\s+(\d+)", SKETCH.read_text())
    return (int(c.group(1)) if c else None), (int(s.group(1)) if s else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("value", type=int, nargs="?", help="new device-table size")
    ap.add_argument("--flash", action="store_true", help="re-flash after editing")
    ap.add_argument("--check", action="store_true", help="report and exit")
    a = ap.parse_args()

    cfg, ino = current()
    print(f"config.MAX_DEVICES = {cfg}\nsketch MAX_DEV     = {ino}")
    if cfg != ino:
        print("  !! MISMATCH — they must be equal")
    if a.check or a.value is None:
        return 0 if cfg == ino else 1

    n = a.value
    if not 4 <= n <= 60:
        sys.exit("refusing: keep the table between 4 and 60 entries")

    for path, pattern, repl in [
        (CONFIG, r"^(MAX_DEVICES\s*=\s*)\d+", rf"\g<1>{n}"),
        (SKETCH, r"(#define\s+MAX_DEV\s+)\d+", rf"\g<1>{n}"),
    ]:
        shutil.copy(path, path.with_suffix(path.suffix + ".bak"))
        text = re.sub(pattern, repl, path.read_text(), count=1,
                      flags=re.M if path is CONFIG else 0)
        path.write_text(text)

    cfg, ino = current()
    if cfg != n or ino != n:
        sys.exit(f"edit failed: config={cfg} sketch={ino}. Restore the .bak files.")
    print(f"\nboth set to {n}  (.bak files left beside the originals)")

    if not a.flash:
        print("\nThe board is still running the OLD table. Re-flash before measuring:")
        print(f"  python tools/set_max_devices.py {n} --flash")
        return 0

    if not CLI.exists():
        sys.exit(f"arduino-cli not found at {CLI} — flash from the Arduino IDE instead")
    sys.path.insert(0, str(ROOT / "backend"))
    from serial_reader import find_port
    import config as _c
    port = find_port(_c.SERIAL_PORT)
    print(f"\ncompiling and uploading to {port} …")
    for cmd in (
        [str(CLI), "compile", "--fqbn", "esp32:esp32:esp32", str(SKETCH.parent)],
        [str(CLI), "upload", "-p", port, "--fqbn",
         "esp32:esp32:esp32:UploadSpeed=460800", str(SKETCH.parent)],
    ):
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            print((p.stdout + p.stderr)[-1200:])
            sys.exit("flash failed — upload from the Arduino IDE (Upload Speed 460800)")
    print("flashed. Re-run: python tools/venue_recon.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
