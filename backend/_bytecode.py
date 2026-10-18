"""Defeat stale bytecode caching of config.py.

CPython decides a cached .pyc is still valid from the source's (mtime, size).
A same-length edit inside the same second changes neither, so the stale .pyc is
reused and the program runs with the OLD value, silently.

Venue edits are precisely that shape — `MAX_DEVICES 40 -> 24`,
`P_THRESHOLD 0.01 -> 0.05`, `CHIP_S 2.0 -> 3.0`. Reproduced 8 Sep: config.py
read 40 on disk while the running process reported 24.

Every entry point calls purge() before importing config.
"""
import shutil
import sys
from pathlib import Path


def purge():
    sys.dont_write_bytecode = True
    here = Path(__file__).resolve().parent
    for d in list(here.rglob("__pycache__")):
        shutil.rmtree(d, ignore_errors=True)
