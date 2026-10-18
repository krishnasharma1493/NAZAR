"""classifier.py — gradient-boosted camera/other pre-filter.  (F12, PRD §8 stage 2)

**A filter, not the proof.** It narrows candidates before spending 30 s of
stimulus on each. The verdict always comes from detector.py, which is the only
thing that establishes causality. Nothing here ever produces a WATCHING.

Trained on our own captures: the CP-E35A as the positive class, every other MAC
seen on air as the negative. That is a small and local training set — a few
devices in one flat — so the model is used with a deliberately low threshold and
`predict` never *rejects* a device outright, it only reorders. Say this plainly
if a judge asks how much data trained it.

    python backend/classifier.py --train        # rebuild from backend/captures
    python backend/classifier.py --report       # cross-validated accuracy
"""
from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np

from config import BIN_MS

MODEL_PATH = Path(__file__).resolve().parent / "classifier_model.pkl"
CAPTURES = Path(__file__).resolve().parent / "captures"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TRAINING = Path(__file__).resolve().parents[1] / "data" / "training"

# The camera we own. Captures are labelled from this; nothing else is assumed.
KNOWN_CAMERA_MACS = {"28:18:fd:db:c2:f6"}

FEATURES = ["mean_kbps", "median_kbps", "mad_kbps", "cv", "duty_cycle",
            "p95_over_median", "mean_pkt_bytes", "uplink_ratio",
            "acf_1s", "zero_bin_frac", "randomised_mac"]


def features(rows, mac):
    """Feature vector for one MAC within one capture. Order matches FEATURES."""
    mac = mac.lower()
    sel = [r for r in rows if r["mac"].lower() == mac]
    if len(sel) < 20:
        return None
    up = np.array([r["up_bytes"] for r in sel], dtype=float)
    dn = np.array([r["dn_bytes"] for r in sel], dtype=float)
    pk = np.array([r["up_pkts"] for r in sel], dtype=float)
    kb = up * 8 / 1000 / (BIN_MS / 1000)

    med = float(np.median(kb)) or 1e-9
    mad = float(1.4826 * np.median(np.abs(kb - np.median(kb))))
    acf = 0.0
    lag = max(int(1000 / BIN_MS), 1)
    if len(kb) > lag + 5 and kb.std() > 0:
        a, b = kb[:-lag], kb[lag:]
        acf = float(np.corrcoef(a, b)[0, 1]) if a.std() > 0 and b.std() > 0 else 0.0
    return [
        float(kb.mean()), med, mad,
        float(kb.std() / (kb.mean() + 1e-9)),
        float((kb > med * 0.5).mean()),
        float(np.percentile(kb, 95) / med),
        float(up.sum() / max(pk.sum(), 1)),
        float(up.sum() / (dn.sum() + 1.0)),
        acf,
        float((up == 0).mean()),
        float(bool(int(mac.split(":")[0], 16) & 0x02)),
    ]


def _dataset():
    """Build (X, y, meta) from every capture on disk, plus data/training."""
    from serial_reader import read_capture
    X, y, meta = [], [], []
    # Fixtures are copies of some captures. Deduplicate by filename so the same
    # recording is not counted twice — duplicated rows would quietly inflate any
    # accuracy figure we quote.
    files, seen = [], set()
    for src in (FIXTURES, TRAINING, CAPTURES):
        for f in sorted(src.glob("*.csv")):
            if f.name in seen:
                continue
            seen.add(f.name); files.append(f)
    for f in files:
        try:
            rows = read_capture(f)
        except Exception:
            continue
        for mac in {r["mac"].lower() for r in rows}:
            v = features(rows, mac)
            if v is None:
                continue
            label = 1 if mac in KNOWN_CAMERA_MACS else 0
            X.append(v); y.append(label); meta.append((f.name, mac))
    return np.array(X, dtype=float), np.array(y), meta


def train(verbose=True):
    from sklearn.ensemble import GradientBoostingClassifier
    X, y, meta = _dataset()
    if len(set(y.tolist())) < 2:
        raise SystemExit(f"need both classes; got {len(X)} samples, labels {set(y.tolist())}")
    clf = GradientBoostingClassifier(random_state=0, n_estimators=120, max_depth=2)
    clf.fit(X, y)
    MODEL_PATH.write_bytes(pickle.dumps({"model": clf, "features": FEATURES}))
    if verbose:
        pos = int(y.sum())
        print(f"trained on {len(X)} device-captures ({pos} camera, {len(X)-pos} other)")
        print(f"saved {MODEL_PATH.name}")
        for n, imp in sorted(zip(FEATURES, clf.feature_importances_),
                             key=lambda kv: -kv[1])[:6]:
            print(f"  {n:18} {imp:.3f}")
    return clf, X, y, meta


def report():
    from sklearn.model_selection import LeaveOneOut, cross_val_score
    from sklearn.ensemble import GradientBoostingClassifier
    X, y, meta = _dataset()
    pos = int(y.sum())
    print(f"dataset: {len(X)} device-captures, {pos} camera / {len(X)-pos} other")
    if len(X) < 6 or pos < 2:
        print("too small to cross-validate honestly"); return
    clf = GradientBoostingClassifier(random_state=0, n_estimators=120, max_depth=2)
    s = cross_val_score(clf, X, y, cv=LeaveOneOut())
    print(f"leave-one-out accuracy: {s.mean():.3f} over {len(s)} folds")
    print("NOTE: a few devices in one flat. This is a pre-filter, not evidence.")


def load():
    if not MODEL_PATH.exists():
        return None
    return pickle.loads(MODEL_PATH.read_bytes())


def predict(rows, mac, model=None):
    """P(camera) in [0,1], or None if the model or the data is insufficient.

    Callers must treat None and low scores as 'unknown', never as 'not a camera'.
    """
    model = model or load()
    v = features(rows, mac)
    if model is None or v is None:
        return None
    return float(model["model"].predict_proba(np.array([v], dtype=float))[0][1])


if __name__ == "__main__":
    import argparse, sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.report:
        report()
    if a.train or not (a.report or a.train):
        train()
