"""evidence.py — hash, sign, render PDF.  (F13, PRD §8 stage 6)

A screenshot is not evidence. A signed, reproducible measurement record is
something a lawyer can work with.

The record holds everything needed to re-derive the verdict without trusting
this program: the full bitrate series, the stimulus sequence and its t0, the
detector settings, the RSSI track and the software version. `replay.py` can
regenerate the p-value from the same numbers.

Signing key: Ed25519, generated once, local, never committed. It proves the file
has not been altered since NAZAR wrote it. It does NOT prove the measurement was
honest — nothing cryptographic can. Say so if asked.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

from config import (SIGNING_KEY_PATH, SOFTWARE_VERSION, BIN_MS, CHIP_S,
                    M_SEQUENCE, N_PERMUTATIONS, P_THRESHOLD, LAG_WINDOW,
                    DETREND_WIN, STIM_MODE, EDGE_WIN_S)

EVIDENCE_DIR = Path(__file__).resolve().parent / "evidence"


def ensure_key(path=SIGNING_KEY_PATH):
    """Load the signing key, generating it on first use. Never overwrites."""
    p = Path(path)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent / p
    if p.exists():
        return serialization.load_pem_private_key(p.read_bytes(), password=None), p
    key = Ed25519PrivateKey.generate()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()))
    os.chmod(p, 0o600)
    return key, p


def canonical(obj) -> bytes:
    """Byte-exact JSON: sorted keys, no incidental whitespace, UTF-8.

    The hash must be reproducible by anyone with the record, so the encoding has
    to be pinned rather than left to a default.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def build_record(scan_result, *, mac, channel, t_ms, bitrate_kbps, stimulus=None,
                 t0_ms=None, rssi=None, bearing=None, capture_file=None,
                 sequence=None, chip_s=None, notes=None):
    return {
        "software_version": SOFTWARE_VERSION,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "device": {"mac": mac, "channel": channel},
        "verdict": scan_result.get("verdict"),
        "statistics": {
            "peak": scan_result.get("peak"),
            "lag_s": scan_result.get("lag_s"),
            "p_value": scan_result.get("p_value"),
            "permutations": scan_result.get("permutations"),
            "null_kind": scan_result.get("null_kind"),
        },
        "settings": {
            "bin_ms": BIN_MS, "chip_s": chip_s or CHIP_S,
            "sequence": list(sequence or M_SEQUENCE),
            "stim_mode": STIM_MODE, "edge_win_s": EDGE_WIN_S,
            "n_permutations": N_PERMUTATIONS, "p_threshold": P_THRESHOLD,
            "lag_window_s": list(LAG_WINDOW), "detrend_win": DETREND_WIN,
        },
        "measurement": {
            "t0_ms": t0_ms,
            "t_ms": [int(x) for x in t_ms],
            "bitrate_kbps": [round(float(x), 3) for x in bitrate_kbps],
            "stimulus": None if stimulus is None else [float(x) for x in stimulus],
            "rssi": None if rssi is None else [int(x) for x in rssi],
            "correlogram": scan_result.get("correlogram"),
            "lags_s": scan_result.get("lags_s"),
        },
        "localisation": bearing,
        "capture_file": capture_file,
        "notes": notes,
    }


def sign_record(record, key_path=SIGNING_KEY_PATH):
    """Return {record, sha256, signature, public_key} — the signed envelope."""
    key, path = ensure_key(key_path)
    payload = canonical(record)
    digest = hashlib.sha256(payload).hexdigest()
    sig = key.sign(digest.encode("ascii"))
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    return {
        "record": record,
        "sha256": digest,
        "signature": sig.hex(),
        "public_key": pub.hex(),
        "algorithm": "Ed25519 over the SHA-256 hex digest of the canonical JSON record",
        "key_file": str(path),
    }


def verify(envelope) -> tuple[bool, str]:
    """Recompute the hash and check the signature. Returns (ok, reason)."""
    try:
        digest = hashlib.sha256(canonical(envelope["record"])).hexdigest()
        if digest != envelope["sha256"]:
            return False, "hash mismatch — the record was altered after signing"
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(envelope["public_key"]))
        pub.verify(bytes.fromhex(envelope["signature"]), digest.encode("ascii"))
        return True, "signature valid"
    except Exception as e:
        return False, f"signature invalid: {e}"


def write_json(envelope, path):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelope, indent=2, ensure_ascii=False))
    return path


def render_pdf(envelope, path):
    """One page a non-engineer can read, with the correlogram drawn."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as pdfcanvas

    rec = envelope["record"]
    st = rec["statistics"]
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    c = pdfcanvas.Canvas(str(path), pagesize=A4)
    W, H = A4
    y = H - 22 * mm

    c.setFont("Helvetica-Bold", 20)
    c.drawString(20 * mm, y, "NAZAR — Measurement Record")
    y -= 8 * mm
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, y, f"{rec['software_version']}   generated {rec['generated_utc']}")
    y -= 12 * mm

    verdict = rec["verdict"] or "UNKNOWN"
    c.setFont("Helvetica-Bold", 30)
    c.drawString(20 * mm, y, verdict)
    # measure the verdict so the p-value never collides with it
    vw = c.stringWidth(verdict, "Helvetica-Bold", 30)
    pv = st.get("p_value")
    c.setFont("Helvetica-Bold", 15)
    c.drawString(20 * mm + vw + 8 * mm, y + 2,
                 f"p = {pv:.4g}" if isinstance(pv, (int, float)) else "p = n/a")
    y -= 8 * mm
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(.35, .35, .35)
    c.drawString(20 * mm, y,
                 "WATCHING = the hypothesis that this device cannot see the user is "
                 "rejected at the threshold below.")
    c.setFillColorRGB(0, 0, 0)
    y -= 10 * mm

    c.setFont("Helvetica", 10)
    rows = [
        ("Device (MAC)", rec["device"]["mac"]),
        ("Channel", str(rec["device"]["channel"])),
        ("Correlation peak", f"{st.get('peak'):.3f}" if st.get("peak") is not None else "n/a"),
        ("Lag", f"{st.get('lag_s'):.2f} s" if st.get("lag_s") is not None else "n/a"),
        ("Null model", f"{st.get('permutations')} {st.get('null_kind')}"),
        ("Decision rule", f"p < {rec['settings']['p_threshold']} and "
                          f"{rec['settings']['lag_window_s'][0]}s < lag < "
                          f"{rec['settings']['lag_window_s'][1]}s"),
        ("Stimulus", f"{len(rec['settings']['sequence'])}-chip m-sequence, "
                     f"{rec['settings']['chip_s']} s/chip, mode={rec['settings']['stim_mode']}"),
        ("Capture", str(rec.get("capture_file") or "n/a")),
    ]
    for k, v in rows:
        c.setFont("Helvetica-Bold", 10); c.drawString(20 * mm, y, f"{k}:")
        c.setFont("Helvetica", 10); c.drawString(62 * mm, y, str(v))
        y -= 6 * mm

    bearing = rec.get("localisation")
    if bearing and bearing.get("reliable"):
        c.setFont("Helvetica-Bold", 10); c.drawString(20 * mm, y, "Bearing:")
        c.setFont("Helvetica", 10)
        c.drawString(62 * mm, y, f"{bearing['bearing_deg']} deg "
                                 f"(amplitude {bearing['amplitude_db']} dB, p={bearing['p_value']})")
        y -= 6 * mm

    # correlogram
    R = rec["measurement"].get("correlogram") or []
    if R:
        y -= 6 * mm
        c.setFont("Helvetica-Bold", 11); c.drawString(20 * mm, y, "Correlogram")
        y -= 4 * mm
        x0, w, h = 20 * mm, 170 * mm, 38 * mm
        top, bot = max(R), min(min(R), 0.0)
        rng = (top - bot) or 1.0
        c.setStrokeColorRGB(.8, .8, .8); c.rect(x0, y - h, w, h)
        zero_y = y - h + (0 - bot) / rng * h
        c.line(x0, zero_y, x0 + w, zero_y)
        c.setStrokeColorRGB(0, 0, 0)
        pts = [(x0 + i / max(len(R) - 1, 1) * w, y - h + (v - bot) / rng * h)
               for i, v in enumerate(R)]
        for a, b in zip(pts, pts[1:]):
            c.line(a[0], a[1], b[0], b[1])
        pi = R.index(max(R))
        c.setStrokeColorRGB(.85, .1, .1)
        c.line(pts[pi][0], y - h, pts[pi][0], y)
        c.setFont("Helvetica", 8); c.setFillColorRGB(.85, .1, .1)
        c.drawString(pts[pi][0] + 2, y - 4 * mm,
                     f"peak {max(R):.3f} @ {st.get('lag_s')}s")
        c.setFillColorRGB(0, 0, 0)
        y -= h + 8 * mm

    c.setFont("Helvetica-Bold", 10); c.drawString(20 * mm, y, "Integrity")
    y -= 5 * mm
    c.setFont("Courier", 7)
    c.drawString(20 * mm, y, f"sha256    {envelope['sha256']}"); y -= 4 * mm
    sig = envelope["signature"]
    c.drawString(20 * mm, y, f"signature {sig[:64]}"); y -= 4 * mm
    c.drawString(20 * mm, y, f"          {sig[64:]}"); y -= 4 * mm
    c.drawString(20 * mm, y, f"pubkey    {envelope['public_key']}"); y -= 6 * mm
    c.setFont("Helvetica", 8)
    c.drawString(20 * mm, y, "Ed25519 over the SHA-256 digest of the canonical JSON record. "
                             "Verify with: python backend/evidence.py --verify <file.json>")
    y -= 4 * mm
    c.drawString(20 * mm, y, "The signature proves the record is unaltered since NAZAR wrote it. "
                             "It does not, and cannot, prove the measurement was honest.")
    c.showPage(); c.save()
    return path


def generate(scan_result, *, out_dir=EVIDENCE_DIR, stem=None, **kw):
    """Build, sign, and write both JSON and PDF. Returns (json_path, pdf_path)."""
    rec = build_record(scan_result, **kw)
    env = sign_record(rec)
    stem = stem or f"nazar_{kw['mac'].replace(':', '')}_{datetime.now():%Y%m%d_%H%M%S}"
    return (write_json(env, Path(out_dir) / f"{stem}.json"),
            render_pdf(env, Path(out_dir) / f"{stem}.pdf"))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Verify a NAZAR evidence file")
    ap.add_argument("--verify", metavar="FILE", required=True)
    a = ap.parse_args()
    env = json.load(open(a.verify))
    ok, why = verify(env)
    print(("OK   " if ok else "FAIL ") + why)
    print(f"  verdict {env['record']['verdict']}  "
          f"p={env['record']['statistics']['p_value']}  "
          f"device {env['record']['device']['mac']}")
    raise SystemExit(0 if ok else 1)
