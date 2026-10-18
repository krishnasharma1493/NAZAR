#!/usr/bin/env python3
"""run_demo.py — start NAZAR for a rehearsal or the demo itself.

    python tools/run_demo.py                  # live, one process, opens the browser
    python tools/run_demo.py --replay         # no hardware: recorded data, live pipeline
    python tools/run_demo.py --replay <file>  # a specific capture
    python tools/run_demo.py --dev            # Vite dev server too, for editing the UI

One process on one port by default: the backend serves the built UI, so the API
is same-origin. No CORS, no proxy, no second server to forget. Ctrl-C stops
everything.

It refuses to start rather than half-start: the UI must be built, and in live
mode the board must be found and streaming.
"""
import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import _bytecode; _bytecode.purge()
import config

PY_BIN = ROOT / ".venv" / "bin" / "python"
if not PY_BIN.exists():
    PY_BIN = Path(sys.executable)
DIST = ROOT / "frontend" / "dist"
PORT = 8000
UI_PORT = 5173
procs = []


def die(msg, hint=""):
    print(f"\n  STOP: {msg}")
    if hint:
        print(f"  {hint}")
    stop()
    sys.exit(1)


def stop(*_):
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()


def lan_ip():
    """This machine's address on the local network. No traffic is sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))          # TEST-NET-1, just to pick a route
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def ensure_cert(ip):
    """Self-signed cert covering localhost and this machine's LAN address.

    The torch is only reachable from a secure context — browsers hide
    navigator.mediaDevices entirely on plain http://, so a phone on the LAN
    cannot use it without TLS. The cert is self-signed, so the phone shows a
    warning you must accept once.
    """
    from datetime import datetime, timedelta, timezone
    import ipaddress
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    d = ROOT / "backend"
    key_p, crt_p = d / "demo_tls_key.pem", d / "demo_tls_cert.pem"
    if key_p.exists() and crt_p.exists():
        try:
            crt = x509.load_pem_x509_certificate(crt_p.read_bytes())
            san = crt.extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value
            if (ipaddress.ip_address(ip) in san.get_values_for_type(x509.IPAddress)
                    and crt.not_valid_after_utc > datetime.now(timezone.utc)):
                return key_p, crt_p
        except Exception:
            pass
    print(f"  generating a self-signed certificate for {ip} …")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "NAZAR demo")])
    now = datetime.now(timezone.utc)
    crt = (x509.CertificateBuilder()
           .subject_name(name).issuer_name(name)
           .public_key(key.public_key())
           .serial_number(x509.random_serial_number())
           .not_valid_before(now - timedelta(minutes=5))
           .not_valid_after(now + timedelta(days=30))
           .add_extension(x509.SubjectAlternativeName([
               x509.DNSName("localhost"),
               x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
               x509.IPAddress(ipaddress.ip_address(ip)),
           ]), critical=False)
           .sign(key, hashes.SHA256()))
    key_p.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()))
    crt_p.write_bytes(crt.public_bytes(serialization.Encoding.PEM))
    os.chmod(key_p, 0o600)
    return key_p, crt_p


def start_http_redirector(https_url, port=8080):
    """Plain-HTTP listener that bounces to the HTTPS URL.

    Typing `192.168.1.12:8000` in a phone browser tries http://, and a
    TLS-only server refuses it in a way that looks exactly like "the site will
    not open". This makes the bare address work.
    """
    import http.server
    import threading

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(307)
            self.send_header("Location", https_url + self.path)
            self.end_headers()
        do_POST = do_GET

        def log_message(self, *a):
            pass

    try:
        srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), Redirect)
    except OSError:
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def wait_http(url, timeout=40):
    end = time.time() + timeout
    last = ""
    while time.time() < end:
        try:
            ctx = None
            if url.startswith("https://"):
                import ssl
                ctx = ssl._create_unverified_context()   # our own self-signed cert
            with urllib.request.urlopen(url, timeout=3, context=ctx) as r:
                return r.read().decode()
        except Exception as e:
            last = str(e)
            time.sleep(0.4)
    return f"__TIMEOUT__ {last}"


def build_ui():
    if not (ROOT / "frontend" / "node_modules").is_dir():
        die("frontend/node_modules is missing",
            "run `cd frontend && npm install` — needs internet, do it before the venue")
    print("  building the UI …")
    p = subprocess.run(["npm", "run", "build"], cwd=ROOT / "frontend",
                       capture_output=True, text=True)
    if p.returncode != 0:
        print(p.stdout[-1500:], p.stderr[-1500:])
        die("the UI failed to build")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", nargs="?", const="scan_20260908_014034_camera3.csv",
                    default=None, metavar="CAPTURE",
                    help="run off a recorded capture instead of the radio")
    ap.add_argument("--dev", action="store_true", help="also run the Vite dev server")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--phone", action="store_true",
                    help="serve on the LAN over HTTPS so a phone can drive the "
                         "stimulus and use its torch")
    ap.add_argument("--lan", action="store_true",
                    help="serve on the LAN over plain HTTP (flash mode only — "
                         "browsers block the torch outside a secure context)")
    a = ap.parse_args()

    signal.signal(signal.SIGINT, lambda *x: (stop(), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda *x: (stop(), sys.exit(0)))

    print("NAZAR — local demo\n")

    env = dict(os.environ)
    if a.replay:
        from serial_reader import resolve_replay_source
        src = resolve_replay_source(a.replay)
        if src is None:
            die(f"replay capture not found: {a.replay}",
                "look in backend/fixtures/ or backend/captures/")
        env["NAZAR_REPLAY"] = src.name
        print(f"  MODE: REPLAY — {src.name}")
        print("  Recorded data through the live pipeline. Say so if you show it.")
    else:
        from serial_reader import find_port
        try:
            port = find_port(config.SERIAL_PORT)
        except Exception as e:
            die(str(e), "or rehearse without hardware:  python tools/run_demo.py --replay")
        print(f"  MODE: LIVE — sniffer on {port} @ {config.SERIAL_BAUD}")

    if not (DIST / "index.html").exists():
        build_ui()
    else:
        build_ui()          # cheap, and guarantees the bundle matches the source

    host, scheme, shown = "127.0.0.1", "http", "127.0.0.1"
    ssl_args = []
    if a.phone or a.lan:
        host, shown = "0.0.0.0", lan_ip()
        if shown == "127.0.0.1":
            die("could not work out this machine's LAN address",
                "join the same Wi-Fi as the phone first")
        print(f"\n  NOTE: serving on the local network at {shown}.")
        print("  The API has no authentication (PRD §10 assumes localhost only).")
        print("  Anyone on this Wi-Fi can reach it. Use it for the demo, then stop it.")
        if a.phone:
            key_p, crt_p = ensure_cert(shown)
            ssl_args = ["--ssl-keyfile", str(key_p), "--ssl-certfile", str(crt_p)]
            scheme = "https"

    print("  starting backend …")
    procs.append(subprocess.Popen(
        [str(PY_BIN), "-m", "uvicorn", "main:app", "--host", host,
         "--port", str(a.port), "--log-level", "warning"] + ssl_args,
        cwd=ROOT / "backend", env=env))

    base = f"{scheme}://{shown}:{a.port}"
    body = wait_http(f"{base}/health")
    if body.startswith("__TIMEOUT__"):
        die(f"backend did not answer on {base}", body)
    import json
    h = json.loads(body)
    if not h["ok"]:
        print(f"\n  WARNING from /health: {h['error']}")
        if not a.replay:
            print("  The UI will start, but a sweep will fail until this is fixed.")
    else:
        d = h["data"]
        print(f"  backend up — mode={d.get('mode')}, "
              f"{d.get('devices_seen', '?')} device(s) seen")

    index = wait_http(base + "/")
    if index.startswith("__TIMEOUT__") or "<div id=\"root\">" not in index:
        die("the backend is not serving the built UI", index[:200])
    print(f"  UI served from {base}  (same origin — no CORS needed)")

    url = base
    if a.dev:
        print("  starting Vite dev server …")
        procs.append(subprocess.Popen(["npm", "run", "dev"], cwd=ROOT / "frontend",
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        if wait_http(f"http://127.0.0.1:{UI_PORT}/").startswith("__TIMEOUT__"):
            print("  (dev server did not come up; using the built UI instead)")
        else:
            url = f"http://127.0.0.1:{UI_PORT}"
            print(f"  dev UI on {url} (hot reload)")

    print(f"""
  ──────────────────────────────────────────────────────────
   OPEN:  {url}

   Sweep -> pick the candidate -> Test -> the PHONE TORCH strobes
   -> Verdict (p-value) -> Find it -> Save evidence report

   The phone is the light source:
     phone torch -> camera lens -> Wi-Fi -> ESP32 -> NAZAR
   Press "Test torch" on the Sweep screen first and watch it fire.
  ──────────────────────────────────────────────────────────
   Ctrl-C to stop everything.
""")
    if a.phone:
        redir = start_http_redirector(url, 8080)
        if redir:
            print(f"   (plain http://{shown}:8080 also works — it redirects here)")
        print(f"""
   ON THE PHONE
   ------------
   1. Same Wi-Fi as this Mac. Not mobile data, not a guest network,
      and turn off any VPN or Private Relay.
   2. Open  {url}
      Type the https:// yourself. Without it the phone tries http://,
      which this TLS-only port refuses — it looks like "cannot connect".
      Or open  http://{shown}:8080  and let it redirect you.
   3. You will get a certificate warning — it is our own self-signed cert.
      Accept it once ("Advanced" -> "Proceed").
   4. Allow camera access when asked. That is what unlocks the torch;
      no video is captured or sent anywhere.

   If the torch stays unavailable the screen flashes instead — the mode is
   printed on the stimulus screen. iPhones do not expose the torch to the
   browser at all; use an Android phone, or the laptop screen.
""")
    if not a.no_browser and not (a.phone or a.lan):
        subprocess.run(["open", url], capture_output=True)

    try:
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
        print("\n  a process exited; shutting down")
    except KeyboardInterrupt:
        pass
    finally:
        stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
