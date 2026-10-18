"""main.py — FastAPI app, route mounts.  (PRD §10)

Binds 127.0.0.1. No auth, no CORS, no rate limiting, no database — nothing
external can reach it, there is one user, and it lives for one session. This is
a measurement instrument, not a service.

Every response is {"ok": true, "data": {...}} or {"ok": false, "error": "..."}.

The serial port is a single shared resource, so one scan runs at a time and a
second attempt returns 409. Captures run on a background thread; the frontend
drives the optical stimulus and polls GET /scan/{id}.

    uvicorn main:app --host 127.0.0.1 --port 8000     (from backend/)
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import Body, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

import _bytecode; _bytecode.purge()   # never serve a stale config
import classifier
import detector
import discovery
import evidence as evidence_mod
import localise as localise_mod
import probe as probe_mod
from config import (BIN_MS, CHANNELS, CHIP_S, M_SEQUENCE, P_THRESHOLD, SCAN_S, STIM_MODE,
                    STIM_SHAPES,
                    SERIAL_BAUD, SERIAL_PORT, SOFTWARE_VERSION, BEARING_SWEEP_S,
                    SEQ_REPEATS, SEQ_PERIOD_S)
from serial_reader import (make_sniffer, write_capture, read_capture, find_port,
                           RE_D as _RE_D)

app = FastAPI(title="NAZAR", version=SOFTWARE_VERSION, docs_url="/docs")

CAPTURES = Path(__file__).resolve().parent / "captures"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
EVIDENCE = Path(__file__).resolve().parent / "evidence"

_lock = threading.Lock()          # guards the serial port
_scans: dict[str, dict] = {}
_active: str | None = None
_seq = 0


def ok(data: Any) -> JSONResponse:
    return JSONResponse({"ok": True, "data": data})


def err(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _sniffer():
    sn = make_sniffer(SERIAL_PORT, SERIAL_BAUD)
    sn.open()
    return sn


def _await_port(timeout=4.0, poll=0.1):
    """Wait for the serial port to come free, rather than refusing instantly.

    The port is released asynchronously: a browser closes an SSE stream, the
    server notices on the next yield, the worker leaves its loop and only then
    closes the port. Measured at 0.66 s on this machine. A scan started the
    instant the 20 s check ends therefore raced that teardown and returned
    "serial port busy" — which reads to the operator as a broken button.

    Returns True if the port looks free. Callers still take `_lock` normally.
    """
    deadline = time.monotonic() + timeout
    while _lock.locked() and time.monotonic() < deadline:
        time.sleep(poll)
    return not _lock.locked()


def _find_channel_for(sn, mac):
    """Sweep and return the channel this MAC is actually transmitting on.

    A camera can change channel when its router does, and the UI may be holding
    a channel from an earlier sweep. Locking the wrong channel yields zero
    packets, which used to surface as "the camera is not streaming" — pointing
    the operator at the camera when the fault was ours.
    """
    try:
        found = sn.scan()
    except Exception:
        return None
    e = found.get(mac.lower())
    if e and e.get("up", 0) > 0:
        return int(e["channel"])
    return None


def _replay_mode() -> str | None:
    """The configured replay source, or None when running off the radio."""
    import os
    from config import REPLAY_SOURCE
    return os.environ.get("NAZAR_REPLAY") or REPLAY_SOURCE


def _epoch_ms(monotonic_t: float) -> float:
    return (monotonic_t + (time.time() - time.monotonic())) * 1000.0


# ── GET /health ──────────────────────────────────────────────────────
@app.get("/health")
def health():
    """Is the ESP32 connected and streaming?"""
    rep = _replay_mode()
    if rep:
        try:
            with _lock:
                sn = _sniffer()
                try:
                    rows = list(sn.stream(1.0))
                finally:
                    sn.close()
        except Exception as e:
            return err(f"replay source unusable — {e}", 503)
        return ok({"mode": "replay", "replay_source": str(rep), "streaming": True,
                   "busy": False, "devices_seen": len({r['mac'] for r in rows}),
                   "software_version": SOFTWARE_VERSION,
                   "warning": "REPLAY MODE — recorded data through the live "
                              "pipeline. Not a live measurement."})
    try:
        port = find_port(SERIAL_PORT)
    except Exception as e:
        return err(str(e), 503)
    if _lock.locked():
        return ok({"mode": "live", "port": port, "streaming": True, "busy": True,
                   "note": "a scan is in progress; port held"})
    try:
        with _lock:
            sn = _sniffer()
            try:
                st = sn.state()
            finally:
                sn.close()
    except Exception as e:
        return err(f"sniffer not streaming — {e}", 503)

    from config import MAX_DEVICES, BIN_MS as CFG_BIN
    warnings = []
    # The board runs whatever was last flashed. If someone changed config.py and
    # did not re-flash, every byte count is against a different table size and
    # nothing says so. Catch it here, at the cheapest possible moment.
    if st["max_dev"] != MAX_DEVICES:
        warnings.append(
            f"board was flashed with MAX_DEV={st['max_dev']} but config says "
            f"MAX_DEVICES={MAX_DEVICES} — re-flash: "
            f"python tools/set_max_devices.py {MAX_DEVICES} --flash")
    if st["bin_ms"] != CFG_BIN:
        warnings.append(
            f"board BIN_MS={st['bin_ms']} but config says {CFG_BIN} — re-flash")
    if any(st["drops"][k] for k in ("table_full", "serial", "late_bins")):
        warnings.append(f"non-zero loss counters since boot: {st['drops']}")

    return ok({"mode": "live", "port": port, "baud": SERIAL_BAUD,
               "streaming": True, "busy": False,
               "board": {"state": st["mode"], "channel": st["channel"],
                         "bin_ms": st["bin_ms"], "max_dev": st["max_dev"],
                         "drops": st["drops"]},
               "warnings": warnings,
               "software_version": SOFTWARE_VERSION})


# ── POST /discover ───────────────────────────────────────────────────
@app.post("/discover")
def discover(body: dict = Body(default={})):
    """Channel sweep -> candidate device list, ranked."""
    classify = bool(body.get("classify", True))
    if _lock.locked():
        return err("a scan is already running", 409)
    try:
        with _lock:
            sn = _sniffer()
            try:
                found = sn.scan()
                rows = discovery.candidates(found, include_all=True)
                probs = {}
                if classify and rows:
                    ch = rows[0]["channel"] or 6
                    sn.lock(ch)
                    cap = list(sn.stream(6.0))
                    for r in rows:
                        if r["channel"] == ch:
                            probs[r["mac"]] = classifier.predict(cap, r["mac"])
                drops = vars(sn.drops)
            finally:
                sn.close()
    except Exception as e:
        return err(f"sniffer not streaming — {e}", 503)
    for r in rows:
        r["camera_probability"] = probs.get(r["mac"])
    return ok({"devices": rows,
               "candidates": [r for r in rows if r["candidate"]],
               "drops": drops,
               "note": "camera_probability is a pre-filter, never a verdict"})


# ── POST /scan/start ─────────────────────────────────────────────────
@app.post("/scan/start")
def scan_start(body: dict = Body(...)):
    """Lock the radio to a channel and begin a stimulus-correlation scan."""
    global _active, _seq
    mac = str(body.get("mac", "")).lower()
    channel = body.get("channel")
    direction = str(body.get("direction", "auto")).lower()
    if direction not in ("auto", "rise", "fall"):
        direction = "auto"
    # Response SHAPE also varies with geometry: off-axis light gives a transient
    # re-encode at each edge, a torch held in the lens gives a sustained level
    # change for the whole chip. Searching both costs a Bonferroni factor of two
    # and loses genuine detections, so the 20 s check measures it once instead.
    stim_mode = str(body.get("stim_mode", "")).lower()
    if stim_mode not in ("edge", "level"):
        stim_mode = None
    lead_s = float(body.get("lead_s", 5.0))
    tail_s = float(body.get("tail_s", 4.0))
    if not mac or len(mac.split(":")) != 6:
        return err("mac must be a colon-separated MAC address")
    if not isinstance(channel, int) or channel not in CHANNELS:
        return err("channel must be 1-13")
    if _active is not None and _scans[_active]["status"] == "running":
        return err("a scan is already running", 409)
    if not _await_port():
        return err("serial port busy — another measurement is still running", 409)

    # Refuse to spend 90 s measuring a device that is not sending video.
    # A camera on keep-alives has no uplink to modulate, so the scan can only
    # return CLEAR — which reads as "no camera" and is the single most
    # misleading thing this program can say.
    if not _replay_mode():
        try:
            with _lock:
                sn = _sniffer()
                try:
                    sn.lock(channel)
                    rows = [r for r in sn.stream(3.0) if r["mac"] == mac]
                finally:
                    sn.close()
        except Exception as e:
            return err(f"sniffer not streaming — {e}", 503)
        okstream, detail = discovery.streaming_state(rows, mac)
        if not okstream and not rows:
            # Nothing at all on this channel: the channel is probably stale.
            try:
                with _lock:
                    sn = _sniffer()
                    try:
                        ch2 = _find_channel_for(sn, mac)
                        if ch2 and ch2 != channel:
                            sn.lock(ch2)
                            rows = [r for r in sn.stream(3.0) if r["mac"] == mac]
                            channel = ch2
                    finally:
                        sn.close()
            except Exception:
                pass
            okstream, detail = discovery.streaming_state(rows, mac)
        if not okstream:
            return err(
                f"{mac} is not streaming video: {detail['reason']} "
                f"({detail['kbps']:.0f} kbps, {detail['packets_per_bin']} packets/bin, "
                f"{detail['bytes_per_packet']:.0f} bytes/packet). "
                f"Open the camera's live view and keep that phone in the foreground. "
                f"A scan now could only return CLEAR.", 409)

    _seq += 1
    scan_id = f"s_{_seq:03d}"
    t0_mono = time.monotonic() + lead_s
    state = {"scan_id": scan_id, "mac": mac, "channel": channel,
             "status": "running", "started_utc": datetime.utcnow().isoformat(),
             "t0_epoch_ms": _epoch_ms(t0_mono), "result": None, "error": None,
             "capture_file": None, "marks": None, "direction": direction,
             "stim_mode": stim_mode,
             "mode": _replay_mode() and "replay" or "live"}
    _scans[scan_id] = state
    _active = scan_id
    threading.Thread(target=_run_scan, args=(scan_id, t0_mono, tail_s),
                     daemon=True).start()
    return ok({"scan_id": scan_id, "sequence": list(M_SEQUENCE), "chip_s": CHIP_S,
               "repeats": SEQ_REPEATS, "period_s": SEQ_PERIOD_S,
               # the phone measures its clock against this and corrects the chip
               # timestamps it reports back; a half-second skew is a quarter of
               # a chip and quietly wrecks the correlation
               "server_ms": time.time() * 1000.0,
               "t0_ms": state["t0_epoch_ms"], "scan_s": SCAN_S,
               "lead_colour": "white" if M_SEQUENCE[-1] else "black",
               "note": "hold the lead-in at lead_colour so the edge into chip 0 is real"})


def _run_scan(scan_id, t0_mono, tail_s):
    st = _scans[scan_id]
    try:
        with _lock:
            sn = _sniffer()
            try:
                sn.lock(st["channel"])
                end = t0_mono + SCAN_S + tail_s
                rows = []
                for r in sn.stream(max(end - time.monotonic(), 1.0)):
                    if r["mac"] == st["mac"]:
                        rows.append(r)
                if not rows:
                    raise RuntimeError(f"no traffic from {st['mac']} during the scan")
                # Prefer the frontend's measured paint time for chip 0 over the
                # t0 we asked for. Falls back to the plan if no marks arrived.
                t0_used, t0_source = t0_mono, "planned"
                marks = st.get("marks")
                if marks:
                    first = min(marks, key=lambda m: m.get("chip", 0))
                    if int(first.get("chip", -1)) == 0 and first.get("t_ms"):
                        epoch_off = time.time() - time.monotonic()
                        skew = float(st.get("clock_skew_ms") or 0.0)
                        t0_used = (float(first["t_ms"]) - skew) / 1000.0 - epoch_off
                        t0_source = "measured" + ("+skew" if skew else "")
                t0_board = sn.host_to_board_ms(t0_used)
                # In replay the transmitted t0 is a recorded fact, not something
                # happening now. Use it, and say so in the result.
                side_t0 = getattr(sn, "sidecar", {}).get("t0_ms")
                if getattr(sn, "mode", "live") == "replay" and side_t0 is not None:
                    t0_board, t0_source = float(side_t0), "replay_sidecar"
                st["t0_source"] = t0_source
                st["drops"] = vars(sn.drops)
            finally:
                sn.close()

        t, kb = detector.series_from_rows(rows, st["mac"])
        _, feat = detector.series_feature(rows, st["mac"])
        mode = st.get("stim_mode")
        if mode:                      # caller pinned one shape
            stim = detector.build_stimulus(t, t0_board, repeats=SEQ_REPEATS, mode=mode)
            nulls = detector.build_surrogates(t, t0_board, repeats=SEQ_REPEATS, mode=mode)
        else:
            # Search the whole shape family. Which one a camera shows depends on
            # where the torch is pointed, and picking wrong costs the detection
            # outright — measured 8 Sep, level p=0.0015 vs edge p=0.10 on the
            # same capture. The surrogates are searched over the same family.
            stim = detector.build_family(t, t0_board, repeats=SEQ_REPEATS)
            nulls = detector.build_family_surrogates(t, t0_board, repeats=SEQ_REPEATS)
        res = detector.detect(feat, stim, nulls, direction=st.get("direction", "auto"))

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = write_capture(CAPTURES / f"scan_{stamp}_{scan_id}.csv", rows)
        import json
        json.dump({"mac": st["mac"], "channel": st["channel"], "t0_ms": t0_board,
                   "sequence": list(M_SEQUENCE), "chip_s": CHIP_S, "bin_ms": BIN_MS,
                   "repeats": SEQ_REPEATS, "direction": st.get("direction", "auto"),
                   "stimulus": st.get("stimulus"),
                   # Record what was actually SEARCHED. Writing the config
                   # default here made every sidecar look as though the
                   # scan had pinned one shape, so replay pinned it too and
                   # reached a different verdict from the live pipeline.
                   "stim_mode": st.get("stim_mode"),
                   "stim_pinned": bool(st.get("stim_mode")),
                   "stim_shapes": None if st.get("stim_mode") else list(STIM_SHAPES),
                   "scan_id": scan_id, "drops": st.get("drops"),
                   "t0_source": st.get("t0_source"), "mode": st.get("mode")},
                  open(path.with_suffix(".json"), "w"), indent=2)

        # A scan whose light never actually switched has no modulation to find.
        # Reporting CLEAR there would say "no camera" when the truth is "no
        # experiment was performed" — the worst thing this program can say.
        stim_health = st.get("stimulus") or {}
        confirmed = int(stim_health.get("confirmed", -1))
        expected = 8 * SEQ_REPEATS          # chip transitions per pass, wraparound included
        if confirmed >= 0 and confirmed < expected * 0.5:
            res = dict(res)
            res["verdict"] = "NO STIMULUS"
            res["invalid_reason"] = (
                f"the light only switched {confirmed} times out of ~{expected}. "
                f"The torch did not run, so this scan measures nothing. It is not "
                f"evidence that the device is clear.")

        st.update(status="done", result=res, capture_file=path.name,
                  series={"t_ms": [int(x) for x in t],
                          "kbps": [round(float(x), 2) for x in kb]},
                  t0_board_ms=t0_board,
                  rssi=[r["rssi"] for r in rows])
    except Exception as e:
        st.update(status="error", error=str(e))


# ── GET /scan/{id} ───────────────────────────────────────────────────
@app.post("/scan/{scan_id}")
def scan_marks(scan_id: str, body: dict = Body(default={})):
    """Report the instants each chip was ACTUALLY painted.

    The frontend plans to start at t0_ms, but browser scheduling and paint
    latency drift. The measured mark for chip 0 is the real start of the
    stimulus, so the detector aligns to that rather than to our intention. A
    schedule we meant to run is not evidence of what the camera saw.

    body: {"marks": [{"chip":0,"value":1,"t_ms":<epoch ms>}, ...]}
    """
    st = _scans.get(scan_id)
    if st is None:
        return err("unknown scan_id", 404)
    marks = body.get("marks")
    if not isinstance(marks, list) or not marks:
        return err("marks must be a non-empty list")
    st["marks"] = marks
    st["clock_skew_ms"] = body.get("clock_skew_ms")
    st["stimulus"] = body.get("stimulus")
    return ok({"scan_id": scan_id, "marks_received": len(marks),
               "clock_skew_ms": st["clock_skew_ms"],
               "stimulus": st["stimulus"]})


@app.get("/scan/{scan_id}")
def scan_get(scan_id: str):
    """Poll for result."""
    st = _scans.get(scan_id)
    if st is None:
        return err("unknown scan_id", 404)
    if st["status"] == "running":
        return ok({"scan_id": scan_id, "status": "running",
                   "t0_ms": st["t0_epoch_ms"]})
    if st["status"] == "error":
        return err(st["error"], 500)
    r = st["result"]
    return ok({"scan_id": scan_id, "status": "done", "mac": st["mac"],
               "channel": st["channel"], "vendor": discovery.vendor(st["mac"]),
               "peak": r["peak"], "lag_s": r["lag_s"], "p_value": r["p_value"],
               "permutations": r["permutations"], "null_kind": r["null_kind"],
               "verdict": r["verdict"], "p_threshold": P_THRESHOLD,
               "correlogram": r["correlogram"], "lags_s": r["lags_s"],
               "series": st["series"], "capture_file": st["capture_file"],
               "drops": st.get("drops"), "mode": st.get("mode", "live"),
               "t0_source": st.get("t0_source", "planned"),
               "stimulus": st.get("stimulus"),
               "invalid_reason": r.get("invalid_reason")})


# ── GET|POST /localise/{mac} ─────────────────────────────────────────
@app.get("/localise/{mac}")
def localise_get(mac: str, seconds: float = 5.0, channel: int | None = None):
    """Smoothed RSSI + hot/cold trend, for walking the room."""
    return _localise(mac, seconds, channel, headings=None)


@app.post("/localise/{mac}")
def localise_post(mac: str, body: dict = Body(default={})):
    """As GET, plus a bearing fit when heading samples are supplied.

    body: {"seconds": 20, "channel": 7,
           "headings": [[epoch_ms, degrees], ...]}   from the phone's compass
    """
    return _localise(mac, float(body.get("seconds", BEARING_SWEEP_S)),
                     body.get("channel"), body.get("headings"))


def _localise(mac, seconds, channel, headings):
    mac = mac.lower()
    if not _await_port():
        return err("serial port busy", 409)
    try:
        with _lock:
            sn = _sniffer()
            try:
                if channel:
                    sn.lock(int(channel))
                rows = [r for r in sn.stream(min(max(seconds, 1.0), 60.0))
                        if r["mac"] == mac]
                # board millis -> wall clock, via the same minimum-latency offset
                # the scan uses. Needed to pair RSSI with the phone's compass.
                offset = sn.offset
            finally:
                sn.close()
    except Exception as e:
        return err(f"sniffer not streaming — {e}", 503)
    if not rows:
        return err(f"no traffic from {mac} — wrong channel, or it stopped streaming", 404)

    tr = localise_mod.RssiTracker()
    for r in rows:
        tr.update(r["rssi"], r["t_ms"] / 1000.0)
    out = tr.as_dict()
    out["mac"] = mac
    out["bearing"] = None

    if headings:
        # Pair each RSSI sample with the compass heading nearest in TIME, on one
        # clock. A sample matched to the wrong heading rotates the fitted bearing
        # and the arrow points somewhere the camera is not.
        if offset is None:
            return err("no traffic seen, so the clocks could not be aligned", 503)
        epoch_off = time.time() - time.monotonic()
        try:
            hs = sorted((float(h[0]), float(h[1])) for h in headings)
        except (TypeError, ValueError, IndexError):
            return err("headings must be [[epoch_ms, degrees], ...]")
        MAX_PAIR_GAP_MS = 1500.0
        pairs, unmatched = [], 0
        import bisect
        times = [h[0] for h in hs]
        for r in rows:
            epoch_ms = (r["t_ms"] / 1000.0 + offset + epoch_off) * 1000.0
            i = bisect.bisect_left(times, epoch_ms)
            best = min((h for h in hs[max(i - 1, 0):i + 1]),
                       key=lambda h: abs(h[0] - epoch_ms), default=None)
            if best is None or abs(best[0] - epoch_ms) > MAX_PAIR_GAP_MS:
                unmatched += 1
                continue
            pairs.append((best[1], r["rssi"]))
        if not pairs:
            return err("no RSSI sample lined up with a compass reading — is the "
                       "phone reporting orientation?", 400)
        b = localise_mod.fit_bearing([p[0] for p in pairs], [p[1] for p in pairs])
        b["arrow"] = localise_mod.bearing_arrow(b["bearing_deg"]) if b["reliable"] else None
        b["paired_samples"] = len(pairs)
        b["unmatched_samples"] = unmatched
        out["bearing"] = b
    return ok(out)


# ── GET /localise/{mac}/stream ───────────────────────────────────────
@app.get("/localise/{mac}/stream")
async def localise_stream(request: Request, mac: str, channel: int | None = None,
                          hz: float = 8.0, max_s: float = 300.0):
    """Live RSSI, as Server-Sent Events. The hot/cold meter.

    A button that takes a 5-second reading is unusable for walking a room, and
    worse than unusable here: every call reopened the port, which pulses RTS,
    which RESETS the board and drops the channel lock. This holds the port for
    the walk and streams a smoothed value continuously.

    The reader runs in a thread and the response is an async generator, so a
    client that closes the tab is noticed at once. That matters: a sync
    generator blocked on a serial read never sees the disconnect and holds the
    port until max_s, locking out every later request.

    Emits at `hz` whether or not frames arrive, so a device that goes quiet
    reads as stale rather than as a frozen meter.
    """
    import asyncio

    mac_l = mac.lower()
    if not _await_port():
        return err("serial port busy", 409)

    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=32)
    stop = threading.Event()

    def push(item):
        def _put():
            if not q.full():
                q.put_nowait(item)
        loop.call_soon_threadsafe(_put)

    def worker():
        try:
            with _lock:
                sn = _sniffer()
                try:
                    if channel:
                        sn.lock(int(channel))
                    # 0.2 s would cap the loop near 5 Hz and the meter visibly
                    # lags a walking pace.
                    sn.s.timeout = 0.02
                    tr = localise_mod.RssiTracker()
                    last_emit = 0.0
                    last_rx = None
                    started = time.monotonic()
                    period = 1.0 / max(hz, 1.0)
                    relocked = False
                    while not stop.is_set() and time.monotonic() - started < max_s:
                        # If the target says nothing for 4 s, the channel is
                        # probably stale. Re-find it once rather than reporting
                        # "not streaming" at a camera that is streaming fine.
                        if (not relocked and last_rx is None
                                and time.monotonic() - started > 4.0):
                            relocked = True
                            ch2 = _find_channel_for(sn, mac_l)
                            if ch2:
                                sn.lock(ch2)
                                sn.s.timeout = 0.02
                                started = time.monotonic()
                                # No t0 here: the localise meter has no strobe
                                # schedule to align to, unlike the probe stream.
                                push({"state": "relocked", "channel": ch2,
                                      "mac": mac_l,
                                      "note": f"target was silent; re-locked to channel {ch2}"})
                        line = sn._line()
                        if line.startswith("#"):
                            sn._note(line)
                        elif line:
                            m = _RE_D.match(line)
                            if m and m.group(2) == mac_l:
                                # board millis is the sample clock; it is
                                # monotonic and free of host scheduling jitter
                                tr.update(int(m.group(6)), int(m.group(1)) / 1000.0)
                                last_rx = time.monotonic()
                        now = time.monotonic()
                        if now - last_emit < period:
                            continue
                        last_emit = now
                        payload = tr.as_dict()
                        payload.update({
                            "mac": mac_l,
                            "stale_s": None if last_rx is None else round(now - last_rx, 2),
                            "drops": vars(sn.drops)})
                        push(payload)
                finally:
                    sn.close()
        except Exception as e:
            push({"error": str(e)})
        finally:
            push(None)

    threading.Thread(target=worker, daemon=True).start()

    async def gen():
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item)}\n\n"
                if await request.is_disconnected():
                    break
        finally:
            stop.set()          # releases the serial port

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})


# ── GET /probe/{mac}/stream ──────────────────────────────────────────
@app.get("/probe/{mac}/stream")
async def probe_stream(request: Request, mac: str, channel: int | None = None,
                       period_s: float = 2.0, lag_s: float = 0.6,
                       window_s: float = 6.0, hz: float = 8.0,
                       max_s: float = 600.0):
    """Optical spotlight: sweep the strobed torch, watch the camera's uplink.

    RSSI gets you to the right side of the room; multipath stops it doing more.
    Light is line-of-sight, so the beam direction that raises the camera's
    bitrate is the direction the camera is in — vent, plant or smoke detector.

    The beam is STROBED and the response scored by lock-in against that phase,
    shifted by the encoder lag the scan already measured for this camera. A
    steady beam would produce a rise indistinguishable from the encoder's own
    ~50% MAD variation.

    `t0_ms` in every event is the epoch instant the strobe schedule starts from;
    the phone aligns its torch to it. Measured lock time is ~5 s on a 2x
    response, so hold each spot for about five seconds.
    """
    import asyncio

    mac_l = mac.lower()
    if not _await_port():
        return err("serial port busy", 409)

    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=32)
    stop = threading.Event()

    def push(item):
        def _put():
            if not q.full():
                q.put_nowait(item)
        loop.call_soon_threadsafe(_put)

    def worker():
        try:
            with _lock:
                sn = _sniffer()
                try:
                    if channel:
                        sn.lock(int(channel))
                    sn.s.timeout = 0.02
                    # the strobe starts shortly ahead so the phone can catch up
                    t0_mono = time.monotonic() + 1.5
                    t0_epoch = _epoch_ms(t0_mono)
                    pr = None
                    last_emit = 0.0
                    last_rx = None
                    # Counters for the bytes/packet test that tells video apart
                    # from heartbeats. These MUST be initialised: incrementing
                    # them unbound raised UnboundLocalError on the target's very
                    # first packet, killing the stream and leaving the UI to
                    # report "no data from the camera" at a camera that was
                    # streaming 1.9 Mbps.
                    tot_pkts = 0
                    tot_bytes = 0
                    started = time.monotonic()
                    relocked = False
                    while not stop.is_set() and time.monotonic() - started < max_s:
                        # Same stale-channel recovery as the localise stream: a
                        # camera follows its router, and the UI may still hold a
                        # channel from an earlier sweep.
                        if (not relocked and last_rx is None
                                and time.monotonic() - started > 4.0):
                            relocked = True
                            ch2 = _find_channel_for(sn, mac_l)
                            if ch2:
                                sn.lock(ch2)
                                sn.s.timeout = 0.02
                                started = time.monotonic()
                                push({"state": "relocked", "channel": ch2,
                                      "t0_ms": t0_epoch, "mac": mac_l,
                                      "note": f"target was silent; re-locked to channel {ch2}"})
                        line = sn._line()
                        if line.startswith("#"):
                            sn._note(line)
                        elif line:
                            m = _RE_D.match(line)
                            if m and m.group(2) == mac_l:
                                # This loop reads the port directly, so it must
                                # maintain the board<->host clock offset itself;
                                # Sniffer.stream() is what normally does it.
                                lat = time.monotonic() - int(m.group(1)) / 1000.0
                                sn.offset = lat if sn.offset is None else min(sn.offset, lat)
                                if pr is None:
                                    # score on the board clock, like the scan
                                    pr = probe_mod.OpticalProbe(
                                        sn.host_to_board_ms(t0_mono) / 1000.0,
                                        period_s=period_s, lag_s=lag_s,
                                        window_s=window_s)
                                if pr is not None:
                                    # packet count, for the same reason the
                                    # detector uses it: one I-frame inflates
                                    # bytes far more than packets
                                    pr.update(int(m.group(1)) / 1000.0,
                                              float(m.group(5)))
                                    tot_pkts += int(m.group(5))
                                    tot_bytes += int(m.group(3))
                                last_rx = time.monotonic()
                        now = time.monotonic()
                        if now - last_emit < 1.0 / max(hz, 1.0):
                            continue
                        last_emit = now
                        payload = (pr.score() if pr is not None
                                   else {"state": "measuring", "score": 0.0,
                                         "confident": False})
                        payload.update({
                            "mac": mac_l, "t0_ms": t0_epoch, "period_s": period_s,
                            "lag_s": lag_s,
                            # the phone corrects its own clock against this, so
                            # the strobe and the scorer agree on when the light
                            # was on even if the two clocks disagree
                            "server_ms": time.time() * 1000.0,
                            "beam_on": bool(pr.beam_on()) if pr is not None else False,
                            "bytes_per_packet": round(tot_bytes / max(tot_pkts, 1), 1),
                            "packets_seen": tot_pkts,
                            "stale_s": None if last_rx is None else round(now - last_rx, 2),
                            "drops": vars(sn.drops)})
                        push(payload)
                finally:
                    sn.close()
        except Exception as e:
            push({"error": str(e)})
        finally:
            push(None)

    threading.Thread(target=worker, daemon=True).start()

    async def gen():
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item)}\n\n"
                if await request.is_disconnected():
                    break
        finally:
            stop.set()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})


# ── POST /evidence/{scan_id} ─────────────────────────────────────────
@app.post("/evidence/{scan_id}")
def evidence_make(scan_id: str, body: dict = Body(default={})):
    """Generate and return the signed report."""
    st = _scans.get(scan_id)
    if st is None:
        return err("unknown scan_id", 404)
    if st["status"] != "done":
        return err(f"scan is {st['status']}", 409)
    fmt = str(body.get("format", "json")).lower()
    t = np.array(st["series"]["t_ms"])
    kb = np.array(st["series"]["kbps"])
    stim = detector.build_stimulus(t, st["t0_board_ms"])
    jp, pp = evidence_mod.generate(
        st["result"], mac=st["mac"], channel=st["channel"], t_ms=t,
        bitrate_kbps=kb, stimulus=stim, t0_ms=st["t0_board_ms"],
        rssi=st.get("rssi"), bearing=body.get("bearing"),
        capture_file=st["capture_file"],
        stem=f"nazar_{scan_id}_{datetime.now():%Y%m%d_%H%M%S}")
    if fmt == "pdf":
        return FileResponse(pp, media_type="application/pdf", filename=pp.name)
    import json
    return ok({"json_file": jp.name, "pdf_file": pp.name,
               "envelope": json.load(open(jp))})


# ── POST /replay ─────────────────────────────────────────────────────
@app.post("/replay")
def replay_run(body: dict = Body(...)):
    """Run the pipeline on a saved capture file."""
    import replay as replay_mod
    name = body.get("capture")
    if not name:
        return err("capture filename required")
    # Never escape the capture directories. Fixtures are searched too: a fresh
    # clone has no captures/ at all, and the committed fixtures are exactly the
    # fallback the replay demo depends on.
    safe = Path(str(name)).name
    path = next((d / safe for d in (CAPTURES, FIXTURES) if (d / safe).exists()), None)
    if path is None:
        return err(f"no such capture: {safe}", 404)
    try:
        res = replay_mod.replay(path, body.get("mac"), body.get("t0_ms"), verbose=False)
    except SystemExit as e:
        return err(str(e))
    except Exception as e:
        return err(f"replay failed: {e}", 500)
    return ok(res)


@app.get("/captures")
def captures_list():
    """Not in the PRD's index; the UI needs it to offer replay files."""
    return ok({"captures": sorted(p.name for p in CAPTURES.glob("*.csv")),
               "fixtures": sorted(p.name for p in FIXTURES.glob("*.csv"))})


# ── Static UI ────────────────────────────────────────────────────────
# Serving the built frontend from the API process makes the whole demo one
# origin on one port: no CORS, no proxy, no second server to forget to start.
# Mounted last so every API route above still matches first.
_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
if _DIST.is_dir():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="ui")
