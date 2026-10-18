"""Unit tests for detector.py — PRD §12, "run before every commit to detector.py".

    python -m pytest backend/test_detector.py -q      (or run this file directly)
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bytecode; _bytecode.purge()
import detector
from config import BIN_MS, CHIP_S, M_SEQUENCE, P_THRESHOLD, SEQ_REPEATS, SCAN_S

FS = detector.FS


def _grid(seconds):
    return np.arange(0, int(seconds * 1000), BIN_MS, dtype=np.int64)


def _setup(extra_s=10):
    # must span the WHOLE stimulus: a record shorter than the sequence truncates
    # it, which breaks the periodic structure the null relies on
    t = _grid(SCAN_S + extra_s)
    stim = detector.build_stimulus(t, 0)
    null = detector.build_surrogates(t, 0)
    return t, stim, null


def _truth(t, lag_s=0.4, dur_s=1.0, repeats=None):
    """A device that responds to every chip TRANSITION, which is what the
    CP-E35A was measured to do on 7 Sep."""
    rel = t / 1000.0
    seq = np.asarray(M_SEQUENCE)
    d = seq ^ np.roll(seq, 1)
    period = len(seq) * CHIP_S
    out = np.zeros(len(t))
    for r in range(SEQ_REPEATS if repeats is None else repeats):
        for k in range(len(seq)):
            if d[k]:
                e = r * period + k * CHIP_S
                out[(rel >= e + lag_s) & (rel < e + lag_s + dur_s)] = 1.0
    return out


def test_pure_noise_is_clear():
    """Gaussian noise in -> CLEAR, and p > 0.05 for all but the odd draw.

    PRD §12 words this as "p > 0.05". Asserting that on every seed would be
    wrong: with a correctly calibrated null, 1 run in 20 lands below 0.05 by
    definition, and a test that forbids it is really a test that the detector is
    conservative. The operational requirement is the verdict.
    """
    t, stim, null = _setup()
    ps = []
    for seed in range(20):
        rng = np.random.default_rng(seed)
        r = detector.detect(rng.normal(1500, 400, len(t)), stim, null)
        assert r["verdict"] == "CLEAR", r
        ps.append(r["p_value"])
    assert sum(x <= 0.05 for x in ps) <= 2, f"{sum(x <= 0.05 for x in ps)}/20 below 0.05"


def test_false_positive_rate_is_calibrated():
    """THE test that keeps the demo honest.

    Across many pure-noise runs the false-positive rate at the verdict threshold
    must not exceed the threshold. An anti-conservative detector calls innocent
    devices WATCHING, which is worse than missing a camera.
    """
    t, stim, null = _setup()
    ps = np.array([detector.detect(np.random.default_rng(10_000 + i).normal(1500, 400, len(t)),
                                   stim, null)["p_value"] for i in range(200)])
    assert (ps < P_THRESHOLD).mean() <= P_THRESHOLD + 0.005, \
        f"false-positive rate {(ps < P_THRESHOLD).mean():.3f} exceeds {P_THRESHOLD}"
    assert (ps < 0.05).mean() <= 0.08, f"p<0.05 rate {(ps < 0.05).mean():.3f}"
    assert 0.3 < np.median(ps) < 0.7, f"median p {np.median(ps):.3f} — null is skewed"


def test_injected_signal_recovers_lag():
    """Synthetic signal at a known lag -> p < 0.01, lag within one bin."""
    t, stim, null = _setup()
    lag_s = 0.4
    for seed in range(5):
        rng = np.random.default_rng(seed)
        bitrate = 1500 + rng.normal(0, 300, len(t)) + 2500 * _truth(t, lag_s)
        r = detector.detect(bitrate, stim, null)
        assert r["p_value"] < 0.01, r["p_value"]
        assert abs(r["lag_s"] - lag_s) <= 1.0 / FS, r["lag_s"]
        assert r["verdict"] == "WATCHING", r


def test_unrelated_periodic_traffic_is_clear():
    """PRD §12 negative control: strong periodic traffic that is not ours."""
    t, stim, null = _setup()
    per = np.zeros(len(t)); per[::17] = 1.0
    for seed in range(5):
        rng = np.random.default_rng(seed)
        r = detector.detect(1500 + rng.normal(0, 300, len(t)) + 3000 * per, stim, null)
        assert r["verdict"] == "CLEAR", r["p_value"]


def test_constant_input_does_not_crash():
    """All zeros -> no crash, no divide-by-zero."""
    t, stim, null = _setup()
    r = detector.detect(np.zeros(len(t)), stim, null)
    assert np.isfinite(r["peak"]) and np.isfinite(r["p_value"])
    assert r["verdict"] == "CLEAR"
    r2 = detector.detect(np.full(len(t), 1234.0), stim, null)
    assert np.isfinite(r2["p_value"])


def test_series_from_rows_fills_gaps():
    """Absent bins must become explicit zeros, not a compressed time axis."""
    rows = [{"t_ms": 1000, "mac": "aa:bb:cc:dd:ee:ff", "up_bytes": 1250},
            {"t_ms": 1500, "mac": "aa:bb:cc:dd:ee:ff", "up_bytes": 2500},
            {"t_ms": 1200, "mac": "11:22:33:44:55:66", "up_bytes": 9999}]
    t, kb = detector.series_from_rows(rows, "AA:BB:CC:DD:EE:FF")
    assert len(t) == 6 and np.all(np.diff(t) == BIN_MS)
    assert kb[0] == 100.0 and kb[5] == 200.0
    assert kb[1] == 0.0, "silent bin must be zero, not dropped"


def test_edge_stimulus_matches_transition_count():
    trans = detector.transition_times()
    t = _grid(SCAN_S)
    stim = detector.build_stimulus(t, 0, mode="edge")
    assert stim.sum() == len(trans) * detector.EDGE_WIN_S * FS
    assert len(trans) == 8 * SEQ_REPEATS, "8 edges per pass, wraparound included"


def test_level_and_edge_disagree_on_a_pure_edge_responder():
    """The measurement that set STIM_MODE, as a regression test.

    A device that responds only to transitions must be detectable in edge mode
    and must NOT be reliably detectable in level mode. If this ever inverts,
    STIM_MODE was changed without re-running the controls.
    """
    t = _grid(SCAN_S + 10)
    edge = detector.build_stimulus(t, 0, mode="edge")
    level = detector.build_stimulus(t, 0, mode="level")
    n_edge = detector.build_surrogates(t, 0, mode="edge")
    n_level = detector.build_surrogates(t, 0, mode="level")
    rng = np.random.default_rng(2)
    bitrate = 1500 + rng.normal(0, 300, len(t)) + 2500 * _truth(t)
    assert detector.detect(bitrate, edge, n_edge)["p_value"] < 0.01
    assert detector.detect(bitrate, level, n_level)["p_value"] > 0.01


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
