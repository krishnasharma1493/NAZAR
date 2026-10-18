"""test_pipeline.py — whole-system test, no hardware required.

    python backend/test_pipeline.py

Covers every module that does not need the ESP32 physically attached:
discovery, classifier, localise, detector, replay, evidence, and all seven API
endpoints. Hardware paths are asserted to FAIL CLEANLY when the board is absent
— a confusing error at the venue is itself a defect.

Run this, then backend/test_detector.py, before trusting anything.
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _bytecode; _bytecode.purge()
import classifier
import detector
import discovery
import evidence as ev
import localise as loc
from config import M_SEQUENCE, CHIP_S, BIN_MS, P_THRESHOLD
from serial_reader import read_capture, write_capture, RE_D, RE_S, Drops

FIX = Path(__file__).resolve().parent / "fixtures"
REAL = FIX / "scan_20260907_205427_camera.csv"
CAMERA = "28:18:fd:db:c2:f6"


def test_serial_line_grammar():
    good = "D,412300,34:ea:e7:aa:bb:cc,18420,240,14,-52"
    assert RE_D.match(good)
    assert RE_S.match("S,6,34:ea:e7:aa:bb:cc,18420,240,14,-52")
    # the exact corruption seen on 7 Sep: an ESP-IDF log spliced into a line
    assert not RE_D.match(good + "E (3745) wifi:failed to post")
    assert not RE_D.match("D,412300,NOTAMAC,1,2,3,-52")
    assert not RE_D.match("D,412300,34:ea:e7:aa:bb:cc,18420,240,14")
    d = Drops(table_full=1, unparsed=999)
    assert d.critical == 1, "unparsed must not count as a critical loss"


def test_discovery_ranks_the_camera_first():
    scan = [{"mac": CAMERA, "up": 119950, "dn": 1635, "rssi": -71, "channel": 7},
            {"mac": "d6:73:dc:28:9a:17", "up": 15232, "dn": 0, "rssi": -64, "channel": 7},
            {"mac": "ca:01:63:56:e2:05", "up": 6750, "dn": 13230, "rssi": -36, "channel": 7}]
    rows = discovery.candidates(scan, include_all=True)
    assert rows[0]["mac"] == CAMERA
    assert rows[0]["candidate"] is True
    assert discovery.is_randomised("ca:01:63:56:e2:05") is True
    assert discovery.is_randomised(CAMERA) is False
    # a downlink-dominated device is never a candidate
    assert [r for r in rows if r["mac"].startswith("ca:")][0]["candidate"] is False


def test_localise_recovers_bearing_and_refuses_noise():
    h = np.linspace(0, 350, 24)
    rng = np.random.default_rng(0)
    good = loc.fit_bearing(h, -60 + 6 * np.cos(np.radians(h - 110)) + rng.normal(0, 1, len(h)))
    assert good["reliable"] and abs(good["bearing_deg"] - 110) < 15
    noise = loc.fit_bearing(h, -60 + rng.normal(0, 3, len(h)))
    assert not noise["reliable"] and noise["reason"]
    assert not loc.fit_bearing(np.linspace(0, 100, 20),
                               np.zeros(20))["reliable"], "partial sweep must refuse"
    # the meter must be rate-independent: the same walk at 10 Hz and at 3 Hz
    # has to read the same, because a quiet camera updates far more slowly than
    # a chatty neighbour and the old per-sample smoothing made that the
    # difference between usable and dead.
    for rate in (10.0, 3.0):
        tr = loc.RssiTracker()
        for i in range(int(6 * rate)):
            tr.update(-70 + 3.0 * (i / rate), i / rate)      # +3 dB/s approach
        assert tr.state() == "warmer", (rate, tr.as_dict())
        assert tr.trend() > 2.0, (rate, tr.trend())
    # Standing still must not claim movement. Asserted as a RATE over time, not
    # on one final sample: with a correctly calibrated threshold the occasional
    # excursion is expected, and a single-sample assertion would be flaky by
    # construction.
    rng = np.random.default_rng(0)
    still = loc.RssiTracker()
    states = []
    for i in range(300):
        still.update(-60 + rng.normal(0, 1.5), i / 10.0)
        states.append(still.state())
    false_rate = np.mean([s != "steady" for s in states[30:]])
    assert false_rate <= 0.10, f"claims movement {false_rate:.0%} of the time while still"
    # the level must use the range actually seen, not a fixed 55 dB scale
    span = loc.RssiTracker()
    for i in range(80):
        span.update(-70 + 0.25 * i, i / 10.0)
    assert span.level() > 0.9, span.as_dict()


def test_replay_reproduces_the_real_verdict():
    import replay as R
    assert REAL.exists(), "the 7 Sep camera capture is the regression fixture"
    res = R.replay(REAL, verbose=False)
    assert res["mac"] == CAMERA
    assert res["verdict"] == "WATCHING", res
    assert res["p_value"] < P_THRESHOLD, res
    assert 0.05 < res["lag_s"] < 2.0, res
    # bit-for-bit stable across runs (the surrogate seed is fixed)
    again = R.replay(REAL, verbose=False)
    assert again["p_value"] == res["p_value"]
    assert again["peak"] == res["peak"]


def test_gate2_negative_control_stays_clear():
    """A device with real traffic that cannot see the screen must return CLEAR.

    Without this, a detector that said WATCHING to everything would look
    identical to a working one. This is the half of Gate 2 that is easiest to
    forget and the first thing a panel will ask for.
    """
    import replay as R
    f = FIX / "scan_20260908_013018_laptop-control.csv"
    res = R.replay(f, verbose=False)
    assert res["verdict"] == "CLEAR", res
    assert res["p_value"] > 0.05, res


def test_stimulus_that_misses_the_lens_stays_clear():
    """A real camera, streaming hard, but aimed away from the screen -> CLEAR.

    Recorded 8 Sep: the camera was uploading 2.2 Mbps at a busy scene and showed
    no transition-locked response (3/7, lift 0.76x). If a future change turns
    this into WATCHING, that change invented a signal.
    """
    import replay as R
    res = R.replay(FIX / "scan_20260908_012849_camera2.csv", verbose=False)
    assert res["verdict"] == "CLEAR", res


def test_rank_transform_still_earns_its_place():
    """A real phone-torch scan, 8 Sep 12:59, which the RAW detector missed.

    The camera was 2.5x noisier in absolute terms than the laptop-screen run and
    heavy-tailed bursts dominated the correlation; the normal-scores transform is
    what recovers it. This asserts that property directly rather than a verdict.

    HONEST NOTE: this capture read WATCHING at p=0.0065 when the detector
    searched one response shape over a positive-only lag window. It is now
    marginal (p~0.015, CLEAR) because the detector searches both response shapes
    and a signed lag window. That is a real, measured loss of power, and it buys
    detections the old settings could not reach at all — see
    test_negative_lag_capture_is_watching. The trade was made deliberately.
    """
    import numpy as np, json, replay as R
    import detector as D
    from serial_reader import read_capture
    csv = FIX / "scan_20260908_125904_s_002.csv"
    meta = json.loads((csv.with_suffix(".json")).read_text())
    rows = read_capture(csv)
    t, feat = D.series_feature(rows, meta["mac"])
    kw = dict(sequence=meta["sequence"], chip_s=meta["chip_s"],
              repeats=meta.get("repeats", 1))
    stim = D.build_family(t, meta["t0_ms"], **kw)
    nulls = D.build_family_surrogates(t, meta["t0_ms"], **kw)
    import config
    keep = config.DETECT_TRANSFORM
    try:
        config.DETECT_TRANSFORM = D.DETECT_TRANSFORM = "rank"
        p_rank = D.detect(feat, stim, nulls, direction="auto")["p_value"]
        config.DETECT_TRANSFORM = D.DETECT_TRANSFORM = "raw"
        p_raw = D.detect(feat, stim, nulls, direction="auto")["p_value"]
    finally:
        config.DETECT_TRANSFORM = D.DETECT_TRANSFORM = keep
    assert p_rank < p_raw / 2, (p_rank, p_raw)


def test_negative_lag_capture_is_watching():
    """8 Sep 23:41: a 2.3x response that the old detector scored CLEAR at 0.83.

    Two defects, both real and both fixed: the torch mark was timestamped after
    the awaited torch command, so t0 landed up to a second late and the response
    sat at -0.70 s, outside a lag window that began at +0.05 s; and the response
    was a sustained LEVEL change from a torch held in the lens, while only edge
    shapes were searched. If either regresses, this returns CLEAR again.
    """
    import replay as R
    res = R.replay(FIX / "scan_20260908_234109_negative_lag.csv", verbose=False)
    assert res["verdict"] == "WATCHING", res
    assert res["p_value"] < 0.01, res
    assert res["lag_s"] < 0, res
    assert res["shape"] == "level", res


def test_lag_search_matches_the_verdict_rule():
    """Every searchable lag must be an acceptable lag.

    int() floors, so int(0.05*10) made lag 0.0 searchable while the verdict rule
    rejects it: a capture whose peak landed there returned p<0.01 and still read
    CLEAR. Measured on the 12:47 scan before the fix.
    """
    import numpy as np
    from config import LAG_WINDOW
    t = np.arange(0, 70_000, BIN_MS)
    stim = detector.build_stimulus(t, 0)
    nulls = detector.build_surrogates(t, 0)
    rng = np.random.default_rng(0)
    r = detector.detect(rng.normal(1500, 400, len(t)), stim, nulls)
    assert LAG_WINDOW[0] <= r["lag_s"] <= LAG_WINDOW[1], r["lag_s"]


def test_gate2_camera_run_is_watching():
    """The confirming Gate 2 capture must keep reproducing its verdict."""
    import replay as R
    res = R.replay(FIX / "scan_20260908_014034_camera3.csv", verbose=False)
    assert res["verdict"] == "WATCHING", res
    assert res["p_value"] < 0.01 and 0.05 < res["lag_s"] < 2.0, res


def test_truncated_capture_is_flagged():
    """A scan cut short is not calibrated, and must say so.

    Measured 8 Sep: a 40 s record against a 60 s stimulus raised the
    false-positive rate from 0.003 to 0.020. Silently returning a p-value from a
    truncated scan would be the worst kind of wrong — confident and unfounded.
    """
    import numpy as np
    t = np.arange(0, 40_000, BIN_MS)
    full = detector.stimulus_coverage(t, 0, repeats=2)
    assert full < 0.9, f"40 s of a 60 s stimulus should read as partial, got {full}"
    t2 = np.arange(0, 62_000, BIN_MS)
    assert detector.stimulus_coverage(t2, 0, repeats=2) >= 0.99


def test_detector_correlates_packet_counts():
    """Packet count, not byte count, is what the detector correlates.

    One I-frame inflates bytes far more than packets, and this camera's
    p95/median byte ratio reaches 12.6. Measured across every real capture,
    packet-count ranks got 5/5 expected verdicts against 4/5 for bytes.
    """
    rows = read_capture(FIX / "scan_20260908_014034_camera3.csv")
    t_b, b = detector.series_from_rows(rows, CAMERA)
    t_p, p = detector.series_feature(rows, CAMERA)
    assert len(t_b) == len(t_p)
    assert not np.allclose(b, p), "the feature must differ from the kbps series"
    assert p.max() < b.max(), "packet counts are smaller in magnitude than kbps"


def test_scan_without_a_stimulus_is_not_clear():
    """A scan whose light never switched must NOT report CLEAR.

    Root cause found 8 Sep: the torch was switched inside a React state updater
    with applyConstraints failures swallowed, so real scans ran with a constant
    torch. No modulation means nothing to detect, and calling that CLEAR tells
    the operator there is no camera when the truth is that no experiment was
    performed.
    """
    from fastapi.testclient import TestClient
    import main
    c = TestClient(main.app, raise_server_exceptions=False)
    sid = "s_test_nostim"
    main._scans[sid] = {
        "scan_id": sid, "mac": CAMERA, "channel": 7, "status": "done",
        "result": {"peak": 0.1, "lag_s": 0.5, "p_value": 0.5, "permutations": 10,
                   "null_kind": "surrogate_sequences", "verdict": "NO STIMULUS",
                   "invalid_reason": "the light only switched 0 times",
                   "correlogram": [0.1], "lags_s": [0.0]},
        "series": {"t_ms": [0], "kbps": [0.0]}, "capture_file": "x.csv",
        "t0_board_ms": 0.0, "mode": "live",
        "stimulus": {"transitions": 0, "confirmed": 0, "failed": 24},
    }
    body = c.get(f"/scan/{sid}").json()["data"]
    assert body["verdict"] == "NO STIMULUS"
    assert body["invalid_reason"]
    assert body["stimulus"]["confirmed"] == 0
    del main._scans[sid]


def test_streaming_test_separates_video_from_heartbeats():
    """Payload size, not bitrate, decides whether a device is sending video.

    A camera whose live view has closed still emits packets in every bin, so
    packet rate and bins/s both look healthy while carrying no video at all.
    Measured 8 Sep: streaming cameras average ~1100 bytes/packet, heartbeats 47.
    Gating on kbps let a sleeping camera through as a "weak stimulus".
    """
    import discovery as disc
    for tag, expect in [("scan_20260908_014034_camera3", True),
                        ("scan_20260908_125904_s_002", True),
                        ("scan_20260908_013018_laptop-control", False)]:
        meta = json.load(open(FIX / f"{tag}.json"))
        rows = read_capture(FIX / f"{tag}.csv")
        ok, detail = disc.streaming_state(rows, meta["mac"])
        assert ok is expect, (tag, detail)
        assert detail["reason"]


def test_classifier_is_a_prefilter_that_never_crashes():
    rows = read_capture(REAL)
    p = classifier.predict(rows, CAMERA)
    assert p is None or 0.0 <= p <= 1.0
    assert classifier.predict(rows, "00:00:00:00:00:00") is None, "unknown MAC -> None"
    assert classifier.features(rows[:3], CAMERA) is None, "too few bins -> None"


def test_evidence_signs_and_detects_tampering():
    import json
    import replay as R
    res = R.replay(REAL, verbose=False)
    rows = read_capture(REAL)
    t, kb = detector.series_from_rows(rows, CAMERA)
    meta = json.load(open(REAL.with_suffix(".json")))
    stim = detector.build_stimulus(t, meta["t0_ms"])
    rec = ev.build_record(res, mac=CAMERA, channel=7, t_ms=t, bitrate_kbps=kb,
                          stimulus=stim, t0_ms=meta["t0_ms"], capture_file=REAL.name)
    env = ev.sign_record(rec)
    ok, why = ev.verify(env)
    assert ok, why
    env["record"]["statistics"]["p_value"] = 1e-9
    bad, why = ev.verify(env)
    assert not bad and "hash mismatch" in why
    # canonical encoding must be order-independent
    a = ev.canonical({"b": 1, "a": [1, 2]})
    b = ev.canonical({"a": [1, 2], "b": 1})
    assert a == b


def test_evidence_pdf_renders():
    import json
    import replay as R
    res = R.replay(REAL, verbose=False)
    rows = read_capture(REAL)
    t, kb = detector.series_from_rows(rows, CAMERA)
    meta = json.load(open(REAL.with_suffix(".json")))
    jp, pp = ev.generate(res, mac=CAMERA, channel=7, t_ms=t, bitrate_kbps=kb,
                         stimulus=detector.build_stimulus(t, meta["t0_ms"]),
                         t0_ms=meta["t0_ms"], capture_file=REAL.name,
                         stem="pipeline_selftest")
    raw = pp.read_bytes()
    assert raw.startswith(b"%PDF") and raw.rstrip().endswith(b"%%EOF")
    assert pp.stat().st_size > 2000
    assert jp.exists()


def test_api_contract_and_clean_hardware_failure():
    from fastapi.testclient import TestClient
    import main
    c = TestClient(main.app, raise_server_exceptions=False)

    r = c.post("/replay", json={"capture": REAL.name})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["data"]["verdict"] == "WATCHING"

    # every failure is the documented envelope, never a stack trace
    for req, code in [
        (lambda: c.post("/scan/start", json={"mac": CAMERA, "channel": 99}), 400),
        (lambda: c.post("/scan/start", json={"mac": "bad", "channel": 7}), 400),
        (lambda: c.get("/scan/s_999"), 404),
        (lambda: c.post("/replay", json={"capture": "nope.csv"}), 404),
        (lambda: c.post("/replay", json={}), 400),
        (lambda: c.post("/evidence/s_999", json={}), 404),
    ]:
        resp = req()
        assert resp.status_code == code, (resp.status_code, code, resp.text)
        body = resp.json()
        assert body["ok"] is False and isinstance(body["error"], str) and body["error"]

    # path traversal must not escape captures/
    assert c.post("/replay", json={"capture": "../../etc/passwd"}).status_code == 404

    # hardware absent -> 503 with a human message, not a traceback
    from serial_reader import find_port
    try:
        find_port(main.SERIAL_PORT); board = True
    except Exception:
        board = False
    if not board:
        h = c.get("/health")
        assert h.status_code == 503 and h.json()["ok"] is False
        assert any(w in h.json()["error"].lower()
                   for w in ("usb", "serial", "esp32", "cable"))
        d = c.post("/discover", json={})
        assert d.status_code == 503 and d.json()["ok"] is False

    assert c.get("/captures").json()["ok"] is True


def test_config_and_firmware_constants_agree():
    """PRD §6: BIN_MS and MAX_DEV live in two files with nothing to enforce it."""
    import re
    root = Path(__file__).resolve().parents[1]
    cfg_src = (root / "backend" / "config.py").read_text()
    ino = (root / "firmware" / "nazar_sniffer" / "nazar_sniffer.ino").read_text()

    def define(name):
        m = re.search(rf"#define\s+{name}\s+(\d+)", ino)
        assert m, f"{name} not found in the sketch"
        return int(m.group(1))

    def setting(name):
        # read the SOURCE, not an import: a stale .pyc would defeat the check
        m = re.search(rf"^{name}\s*=\s*(\d+)", cfg_src, re.M)
        assert m, f"{name} not found in config.py"
        return int(m.group(1))

    assert define("BIN_MS") == setting("BIN_MS"), "BIN_MS drifted between firmware and config"
    assert define("MAX_DEV") == setting("MAX_DEVICES"), "MAX_DEV drifted"
    assert define("SERIAL_BAUD") == setting("SERIAL_BAUD"), "baud drifted"
    assert define("SCAN_DWELL_MS") == setting("SCAN_DWELL_MS"), "scan dwell drifted"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for f in fns:
        try:
            f(); print(f"  PASS  {f.__name__}")
        except Exception:
            bad += 1; print(f"  FAIL  {f.__name__}"); traceback.print_exc()
    print(f"\n{len(fns)-bad}/{len(fns)} passed")
    sys.exit(1 if bad else 0)
