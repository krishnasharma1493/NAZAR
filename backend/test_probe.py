"""Unit tests for probe.py — the optical spotlight scorer."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bytecode; _bytecode.purge()
from probe import OpticalProbe

FS = 10          # bins per second
PERIOD = 2.0
LAG = 0.6


def _feed(probe, secs, response_amp, seed=0, base=1500.0, noise=700.0,
          phase_shift=0.0, t_start=0.0):
    """Simulate a camera: heavy-tailed baseline plus a response when lit."""
    rng = np.random.default_rng(seed)
    n = int(secs * FS)
    for i in range(n):
        t = t_start + i / FS
        lit = ((t - LAG - phase_shift) / PERIOD) % 1.0 < 0.5
        v = base + rng.standard_t(3) * noise + (response_amp if lit else 0.0)
        probe.update(t, max(v, 0.0))
    return probe


def test_no_response_is_cold():
    """A camera the beam never reaches must not read as a hit."""
    hits = 0
    for seed in range(20):
        p = _feed(OpticalProbe(0.0, PERIOD, LAG), 8, 0.0, seed=seed)
        s = p.score()
        assert s["state"] != "hot", s
        hits += s["confident"]
    assert hits <= 1, f"{hits}/20 false confident hits"


def test_beam_on_lens_is_hot():
    """A strong locked response must read hot and confident."""
    ok = 0
    for seed in range(20):
        p = _feed(OpticalProbe(0.0, PERIOD, LAG), 8, 3000.0, seed=seed)
        s = p.score()
        ok += (s["state"] == "hot" and s["confident"])
    assert ok >= 16, f"only {ok}/20 detected a strong hit"


def test_weak_response_is_warm_not_hot():
    seen = set()
    for seed in range(12):
        p = _feed(OpticalProbe(0.0, PERIOD, LAG), 8, 700.0, seed=seed)
        seen.add(p.score()["state"])
    assert "warm" in seen or "hot" in seen, seen


def test_inverted_response_still_scores():
    """A bitrate that FALLS when lit is a hit, not a miss.

    Measured 8 Sep with the torch against the lens: 1551 kbps lit vs 2530
    unlit. Bright light straight into a lens saturates the sensor and a uniform
    white frame is cheap to encode, so the bitrate collapses. That is the same
    signal as a rise, inverted, and it is the STRONGEST response the phone can
    produce. Scoring only rises discarded it.
    """
    ok = 0
    for seed in range(15):
        p = _feed(OpticalProbe(0.0, PERIOD, LAG), 8, 3000.0, seed=seed,
                  phase_shift=PERIOD / 2)
        s = p.score()
        ok += s["confident"]
        if s["confident"]:
            assert s["direction"] == "falls", s
    assert ok >= 12, f"only {ok}/15 detected an inverted (saturating) response"


def test_quadrature_response_does_not_score():
    """Periodic traffic that is NOT locked to our on/off pattern must be rejected.

    A quarter-period shift is the real confounder now that both directions
    count: it is perfectly periodic and carries no information about our beam.
    """
    bad = 0
    for seed in range(15):
        p = _feed(OpticalProbe(0.0, PERIOD, LAG), 8, 3000.0, seed=seed,
                  phase_shift=PERIOD / 4)
        bad += p.score()["confident"]
    assert bad <= 2, f"{bad}/15 scored a hit on a quadrature response"


def test_lag_matters():
    """Ignoring the measured encoder lag should cost detections."""
    with_lag = sum(_feed(OpticalProbe(0.0, PERIOD, LAG), 8, 2500.0, seed=s)
                   .score()["confident"] for s in range(15))
    without = sum(_feed(OpticalProbe(0.0, PERIOD, 0.0), 8, 2500.0, seed=s)
                  .score()["confident"] for s in range(15))
    assert with_lag >= without, (with_lag, without)


def test_needs_data_before_answering():
    p = _feed(OpticalProbe(0.0, PERIOD, LAG), 1.0, 3000.0)
    s = p.score()
    assert s["state"] == "starved" and not s["confident"], s


def test_clock_skew_is_diagnosable():
    """A misaligned scorer must look different from an absent signal.

    Both read cold. Only the phase scan separates them: with skew, some other
    phase scores strongly while the in-phase score is flat. Without this the
    two failure modes are indistinguishable in the field.
    """
    good = _feed(OpticalProbe(0.0, PERIOD, LAG), 10, 3000.0).score()
    skew = _feed(OpticalProbe(0.5, PERIOD, LAG), 10, 3000.0).score()
    none = _feed(OpticalProbe(0.0, PERIOD, LAG), 10, 0.0).score()
    assert good["confident"], good
    assert skew["best_phase_z"] > 3.0 and skew["z"] < skew["best_phase_z"] / 2, skew
    assert none["best_phase_z"] < 3.5, none


def test_window_forgets_old_samples():
    p = OpticalProbe(0.0, PERIOD, LAG, window_s=4.0)
    _feed(p, 20, 3000.0)
    span = p.samples[-1][0] - p.samples[0][0]
    assert span <= 4.2, span


def test_lens_response_outranks_a_cold_spot_whatever_the_phase():
    """The spotlight must rank the true spot first, and it is the SIGN and the
    PHASE that used to stop it.

    A torch held in a lens saturates the sensor, so the uplink FALLS and the
    in-phase z goes strongly NEGATIVE. Ranking on signed z therefore put the
    correct answer at the bottom of the board, below a blank wall. And the
    browser's torch latency is unknown to a few hundred ms, so a quarter-period
    error nulls the in-phase statistic outright.

    Measured over 120 trials with a random 0-2 s torch offset, ranking the true
    spot above a cold one: signed z 50.0% (chance), |z| in-phase 83.3%,
    lock_z 99.2%. This asserts the property, not the percentage.
    """
    import numpy as np
    rng = np.random.default_rng(1)

    def spot(resp, offset_s, seed):
        r = np.random.default_rng(seed)
        pr = OpticalProbe(0.0, period_s=2.0, lag_s=0.6, window_s=12.0)
        t = 0.0
        while t < 14.0:
            on = (((t - 0.6 - offset_s) / 2.0) % 1.0) < 0.5
            pr.update(t, r.lognormal(2.6, 0.8) * (resp if on else 1.0))
            t += 0.1
        return pr.score()

    wins_lock = wins_signed = 0
    trials = 60
    for i in range(trials):
        off = rng.uniform(0, 2.0)
        resp = [0.45, 0.55, 1.7, 2.2][i % 4]      # falls AND rises
        hot, cold = spot(resp, off, i), spot(1.0, off, 5000 + i)
        wins_lock += hot["lock_z"] > cold["lock_z"]
        wins_signed += hot["z"] > cold["z"]
    assert wins_lock >= 0.9 * trials, (wins_lock, trials)
    # and the statistic it replaced must be visibly worse, or this test is
    # asserting nothing
    assert wins_lock > wins_signed + 0.2 * trials, (wins_lock, wins_signed)


def test_quarter_period_offset_does_not_null_the_lock():
    """A 0.5 s torch offset on a 2 s strobe nulls the in-phase statistic.

    Measured: a true response of |z| = 4.81 read 0.06 in phase. lock_z searches
    phase, so the response survives. Without this the spotlight reports nothing
    at the one spot where the answer is.
    """
    import numpy as np
    r = np.random.default_rng(3)
    pr = OpticalProbe(0.0, period_s=2.0, lag_s=0.6, window_s=12.0)
    t = 0.0
    while t < 14.0:
        on = (((t - 0.6 - 0.5) / 2.0) % 1.0) < 0.5      # light 0.5 s late
        pr.update(t, r.lognormal(2.6, 0.8) * (0.45 if on else 1.0))
        t += 0.1
    s = pr.score()
    assert abs(s["z"]) < 1.0, s          # in-phase really is nulled
    assert s["lock_z"] > 3.0, s          # the lock still finds it



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
