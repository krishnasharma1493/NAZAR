"""test_integration.py — the whole flow, end to end, with no hardware.

    python backend/test_integration.py

Drives the real API through a real scan in REPLAY mode: health -> discover ->
scan/start -> report measured chip marks -> poll for the verdict -> generate and
verify signed evidence. Same code paths the demo uses; only the bytes come from
disk instead of the radio.

This is the rehearsal that does not need the ESP32, and it is also the check
that the demo fallback actually works before it is needed on stage.

Takes about a minute: the scan is paced in real time, exactly as it would be.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

FIXTURE = "scan_20260907_205427_camera.csv"
CAMERA = "28:18:fd:db:c2:f6"


def main():
    os.environ["NAZAR_REPLAY"] = FIXTURE
    # import AFTER setting the env var so the app comes up in replay mode
    from fastapi.testclient import TestClient
    import main as app_main
    import evidence as ev

    c = TestClient(app_main.app, raise_server_exceptions=False)
    step = 0

    def say(msg, detail=""):
        nonlocal step
        step += 1
        print(f"  {step}. {msg}" + (f"  — {detail}" if detail else ""))

    h = c.get("/health").json()
    assert h["ok"], h
    assert h["data"]["mode"] == "replay", "must announce replay mode"
    assert "not a live measurement" in h["data"]["warning"].lower()
    say("GET /health", f"replay mode announced, {h['data']['devices_seen']} device(s)")

    d = c.post("/discover", json={"classify": True}).json()
    assert d["ok"], d
    devs = d["data"]["devices"]
    assert any(x["mac"] == CAMERA for x in devs), "camera not discovered"
    cam = next(x for x in devs if x["mac"] == CAMERA)
    assert cam["candidate"], "camera should pass the F5 filter"
    assert cam["randomised"] is False
    say("POST /discover", f"{len(devs)} device(s), camera is a candidate, "
                          f"score {cam['score']}")

    s = c.post("/scan/start", json={"mac": CAMERA, "channel": 7}).json()
    assert s["ok"], s
    sid = s["data"]["scan_id"]
    assert s["data"]["sequence"] and s["data"]["chip_s"] > 0
    assert s["data"]["lead_colour"] in ("white", "black")
    say("POST /scan/start", f"{sid}, {len(s['data']['sequence'])} chips, "
                            f"lead-in {s['data']['lead_colour']}")

    # a second scan while one runs must be refused
    assert c.post("/scan/start", json={"mac": CAMERA, "channel": 7}).status_code == 409
    say("POST /scan/start again", "correctly refused with 409")

    # report measured chip paint times, as the browser does
    t0 = s["data"]["t0_ms"]
    marks = [{"chip": i, "value": v, "t_ms": t0 + i * s["data"]["chip_s"] * 1000 + 7}
             for i, v in enumerate(s["data"]["sequence"])]
    m = c.post(f"/scan/{sid}", json={"marks": marks}).json()
    assert m["ok"] and m["data"]["marks_received"] == len(marks)
    say("POST /scan/{id} marks", f"{len(marks)} chip timestamps accepted")

    deadline = time.time() + 120
    res = None
    while time.time() < deadline:
        r = c.get(f"/scan/{sid}")
        body = r.json()
        if r.status_code != 200:
            raise AssertionError(f"scan failed: {body}")
        if body["data"]["status"] == "done":
            res = body["data"]; break
        time.sleep(1.0)
    assert res, "scan never finished"
    say("GET /scan/{id}", f"verdict {res['verdict']}, p={res['p_value']:.4f}, "
                          f"lag={res['lag_s']}s, t0 from {res['t0_source']}")

    assert res["mode"] == "replay", "verdict must be flagged as replayed"
    assert res["verdict"] == "WATCHING", f"fallback demo must reproduce the verdict: {res}"
    assert res["p_value"] < 0.01
    assert 0.05 < res["lag_s"] < 2.0
    assert res["correlogram"] and res["series"]["kbps"]
    drops = res.get("drops") or {}
    assert not any(drops.get(k) for k in ("table_full", "serial", "late_bins")), drops
    say("verdict checks", "WATCHING, p<0.01, lag in window, no capture loss")

    e = c.post(f"/evidence/{sid}", json={}).json()
    assert e["ok"], e
    env = e["data"]["envelope"]
    ok, why = ev.verify(env)
    assert ok, why
    assert env["record"]["verdict"] == "WATCHING"
    pdf = Path(app_main.EVIDENCE) / e["data"]["pdf_file"]
    assert pdf.exists() and pdf.read_bytes().startswith(b"%PDF")
    say("POST /evidence/{id}", f"{e['data']['pdf_file']}, signature valid")

    env["record"]["statistics"]["p_value"] = 1e-12
    bad, why = ev.verify(env)
    assert not bad and "hash mismatch" in why
    say("tamper check", "altered record rejected")

    rp = c.post("/replay", json={"capture": res["capture_file"]}).json()
    assert rp["ok"] and rp["data"]["verdict"] == "WATCHING"
    say("POST /replay on the new capture", f"p={rp['data']['p_value']:.4f} reproduced")

    print("\n  END-TO-END OK — sweep, stimulus, verdict, evidence, replay")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"\n  FAILED: {e}")
        sys.exit(1)
