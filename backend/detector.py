"""detector.py — matched filter + permutation test.  (F7, F8)

This is the product; everything else in NAZAR is plumbing. PRD §8 stage 4.

`detect()` is the PRD's function, unchanged. Around it sit the pieces needed to
feed it: building the stimulus regressor from the m-sequence, and turning a
capture into a per-MAC bitrate series on a regular grid.

The claim this file has to support in Q&A is not "the app says so" but "the null
hypothesis that this device cannot see you is rejected at p < 0.01."
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import median_filter
from scipy.stats import norm as _norm, rankdata

from config import (BIN_MS, MAX_LAG_S, LAG_WINDOW, N_PERMUTATIONS, P_THRESHOLD,
                    DETREND_WIN, M_SEQUENCE, CHIP_S, STIM_MODE, EDGE_WIN_S,
                    SEQ_REPEATS, DETECT_TRANSFORM, DETECT_FEATURE)

FS = 1000 // BIN_MS          # bins per second


def surrogate_sequences(n_chips, n, ones=None, exclude=None, seed=0):
    """Balanced binary sequences that were never transmitted."""
    rng = np.random.default_rng(seed)
    ones = (n_chips + 1) // 2 if ones is None else ones
    seen = {tuple(exclude)} if exclude is not None else set()
    out = []
    while len(out) < n:
        q = np.zeros(n_chips, dtype=int)
        q[rng.choice(n_chips, ones, replace=False)] = 1
        if tuple(q) in seen:
            continue
        seen.add(tuple(q))
        out.append(q)
    return out


def build_surrogates(bin_t_ms, t0_ms, n=N_PERMUTATIONS, sequence=None, chip_s=None,
                     mode=None, edge_win_s=None, seed=0, repeats=None):
    """Null stimuli for `detect`: the same construction, other sequences."""
    sequence = M_SEQUENCE if sequence is None else sequence
    seqs = surrogate_sequences(len(sequence), n, ones=int(np.sum(sequence)),
                               exclude=sequence, seed=seed)
    return np.array([build_stimulus(bin_t_ms, t0_ms, sequence=q, chip_s=chip_s,
                                    mode=mode, edge_win_s=edge_win_s,
                                    repeats=repeats) for q in seqs])



def build_family(bin_t_ms, t0_ms, sequence=None, chip_s=None, repeats=None,
                 shapes=None):
    """Every response shape the detector searches, as one (S, n) array."""
    from config import STIM_SHAPES
    shapes = STIM_SHAPES if shapes is None else shapes
    return np.array([build_stimulus(bin_t_ms, t0_ms, sequence=sequence,
                                    chip_s=chip_s, mode=m, repeats=repeats)
                     for m in shapes])


def build_family_surrogates(bin_t_ms, t0_ms, n=None, sequence=None, chip_s=None,
                            repeats=None, shapes=None, seed=0):
    """Surrogates for every shape, (S, M, n). Same sequences across shapes."""
    from config import STIM_SHAPES, N_PERMUTATIONS
    shapes = STIM_SHAPES if shapes is None else shapes
    n = N_PERMUTATIONS if n is None else n
    return np.array([build_surrogates(bin_t_ms, t0_ms, n=n, sequence=sequence,
                                      chip_s=chip_s, mode=m, repeats=repeats,
                                      seed=seed) for m in shapes])

def normal_scores(x):
    """Rank each bin and map to the normal quantile.

    Distribution-free: only the ORDER of the bitrates matters, so one 20 Mbps
    burst cannot outweigh thirty ordinary bins. Ties share an average rank, so a
    constant input maps to a constant rather than to a spurious ramp.
    """
    x = np.asarray(x, dtype=float)
    if len(x) == 0:
        return x
    r = rankdata(x, method="average")
    return _norm.ppf(r / (len(x) + 1.0))


def detect(bitrate, stim, null_stims=None, direction="auto"):
    """bitrate: up_bytes per bin.  stim: ±1 per bin, time-aligned.

    `direction` says which way this camera's bitrate moves under our light:
    "rise", "fall", or "auto" to search both. Searching both doubles the null's
    search space and costs about a factor of ten in p-value, so when the 20 s
    signal check has already measured the direction we use it — a genuine
    pre-registration from independent data, not a choice made after seeing the
    scan.

    `null_stims` is a 2-D array of stimulus vectors that were NEVER transmitted.
    Supply it. Without it the null falls back to circular shifts of `stim`, which
    the PRD specifies but which cannot reach p < 0.01 on a 30 s capture — see the
    note on the empirical null below.
    """
    bitrate = np.asarray(bitrate, dtype=float)
    stim = np.asarray(stim, dtype=float)

    # 0. Robust transform. See config.DETECT_TRANSFORM for the measurements
    #    that chose this: on real captures the raw statistic MISSED two genuine
    #    detections that the transformed one finds at p < 0.01, while the
    #    false-positive rate stayed at or below the threshold.
    if DETECT_TRANSFORM == "rank":
        bitrate = normal_scores(bitrate)

    # 1. Remove slow drift from unrelated traffic
    z = bitrate - median_filter(bitrate, size=DETREND_WIN)

    # 2. Robust normalise — MAD, not std, so one burst can't dominate
    mad = 1.4826 * np.median(np.abs(z - np.median(z)))
    z = z / (mad + 1e-9)

    # `stim` may be a FAMILY of response shapes, (S, n). The camera's response
    # shape depends on geometry — a torch held in the lens gives a sustained
    # level change for the whole chip, off-axis light gives a transient
    # re-encode at each edge. Guessing wrong is fatal: on the 8 Sep 23:41
    # capture the level shape gave p = 0.0015 and the edge shape p = 0.10 on
    # identical data. Both are searched, and EVERY surrogate is searched over
    # the same family, so the Bonferroni cost is paid inside the null rather
    # than assumed away.
    S = np.atleast_2d(np.asarray(stim, dtype=float))
    S = S - S.mean(axis=1, keepdims=True)
    n = S.shape[1]
    L = int(MAX_LAG_S * FS)

    nz = np.linalg.norm(z)
    ns_v = np.linalg.norm(S, axis=1)

    # CIRCULAR cross-correlation, R[l] = sum_k z[k+l] s[k] with wraparound.
    #
    # The PRD writes this as the truncated dot(z[l:], s[:n-l]). Truncation makes
    # the statistic depend on the shift, so the observed value (unrolled) and the
    # permutation null (rolled) are NOT drawn from the same distribution, and the
    # test loses calibration: measured 6% of pure-noise runs returned p < 0.01,
    # a six-fold inflated false-positive rate. Under circular correlation every
    # shift is exactly exchangeable, and rolling the stimulus by r is identical
    # to shifting R by r — so the entire null follows from one FFT.
    # test_detector.py asserts the calibration this buys.
    Z = np.fft.rfft(z)
    Rfull = np.fft.irfft(Z[None, :] * np.conj(np.fft.rfft(S, axis=1)), n,
                         axis=1) / (nz * ns_v[:, None] + 1e-9)   # (S, n)

    # Search only lags that could actually produce a verdict. The verdict rule
    # already refuses anything outside LAG_WINDOW (physically plausible encode +
    # transmit delay), so including lags beyond it cannot win a detection but
    # does raise the null's maximum. Both the observed peak and every surrogate
    # are searched over the identical window, so calibration is preserved —
    # asserted in test_detector.py.
    # Search exactly the lags the verdict can accept. int() floors, so
    # int(0.05*10)=0 made lag 0.0 searchable but never acceptable: a capture
    # whose peak landed there returned p<0.01 and still read CLEAR.
    #
    # NEGATIVE lags are searched too. The phone and the laptop keep independent
    # clocks and the browser's torch call has its own latency, so the modelled
    # stimulus can sit slightly ahead of or behind the light that was actually
    # emitted. On the 8 Sep 23:41 capture the true response sat at -0.5 s and
    # the window started at +0.05 s, so the peak was unreachable and a 2.3x
    # response scored CLEAR at p = 0.83. A window that cannot contain the
    # answer is not a conservative choice, it is a blind spot. Surrogates are
    # searched over the identical signed window, so this costs power, not
    # calibration — test_detector.py asserts the false-positive rate.
    lags = np.arange(int(np.ceil(LAG_WINDOW[0] * FS)),
                     int(np.floor(LAG_WINDOW[1] * FS)) + 1)
    lags = lags[np.abs(lags) < n // 2]
    if lags.size == 0:
        lags = np.array([1])
    widx = lags % n

    # ABSOLUTE correlation: a camera that is time-locked to our light is
    # responding to it, whichever way its bitrate moves.
    #
    # Measured 8 Sep with the torch held against the lens: 1551 kbps lit vs
    # 2530 kbps unlit — the bitrate FALLS. Bright light straight into a lens
    # saturates the sensor, and a uniform white frame is far cheaper to encode
    # than a detailed room. Off-axis light brightens the scene instead and the
    # bitrate RISES. Looking only for a rise threw away the strongest responses
    # we can produce, and during a sweep the two directions partly cancel.
    def _pick(a):
        """Reduce a lag window to the statistic, honouring `direction`."""
        if direction == "rise":
            return a
        if direction == "fall":
            return -a
        return np.abs(a)

    win = Rfull[:, widx]                       # (S, len(lags))
    flat = _pick(win)
    j = int(flat.argmax())
    si, li = np.unravel_index(j, flat.shape)
    signed = float(win[si, li])
    peak = float(flat[si, li])
    lag = float(lags[li]) / FS
    shape_i = int(si)

    # 3. Empirical null: how good a match does chance produce?
    #
    # NOT circular shifts of the transmitted stimulus. Measured 7 Sep: the null
    # windows overlap so heavily that their maximum IS the global maximum of R,
    # so the p-value degenerates into "did the global peak land inside the lag
    # window", whose probability is just L/n. On a 40 s record with a 3 s lag
    # search that is ~7.5%, and 7.3% of pure-noise runs came back p < 0.01. A
    # 30 s capture simply does not contain enough independent shift positions
    # (~n/L, about 13) for that test to reach the p < 0.01 the gate requires.
    #
    # Instead the null is the same statistic computed against sequences that
    # were never transmitted. Each is an independent draw, so the resolution is
    # 1/(N+1) rather than n/L. Measured calibration on pure noise: 0.000 at
    # p < 0.01 and 0.024 at p < 0.05 — slightly conservative, erring toward
    # CLEAR, which is the right direction for an instrument that accuses people.
    # test_detector.py asserts both the calibration and the power.
    if null_stims is None:
        guard = L + int(round(EDGE_WIN_S * FS))
        lo, hi = (guard, n - guard) if n - 2 * guard >= 8 else (1, max(2, n))
        idx = (np.arange(lo, hi)[:, None] + lags[None, :]) % n
        null_s = _pick(Rfull[:, idx]).max(axis=(0, 2))
        null_kind = "circular_shift"
    else:
        NS = np.asarray(null_stims, dtype=float)
        if NS.ndim == 2:                       # (M, n) -> one shape
            NS = NS[None, :, :]
        NS = NS - NS.mean(axis=2, keepdims=True)          # (S, M, n)
        norms = np.linalg.norm(NS, axis=2)
        Rn = np.fft.irfft(Z[None, None, :] * np.conj(np.fft.rfft(NS, axis=2)),
                          n, axis=2) / (nz * norms[..., None] + 1e-9)
        # Per-shape maxima over the lag window: (S, M)
        null_s = _pick(Rn[:, :, widx]).max(axis=2)
        null_kind = "surrogate_sequences"
    if null_kind == "surrogate_sequences" and null_s.shape[0] > 1:
        # Westfall-Young minP across the shape family.
        #
        # Taking a plain max over shapes does not work: the level and edge
        # regressors have very different null scales — the level shape carries
        # more low-frequency power, so its surrogate maxima run higher and it
        # swamps a genuine edge detection. Measured 8 Sep on fixture s_002 (a
        # true edge responder): edge alone p = 0.0065, raw max over both shapes
        # p = 0.0615, a tenfold loss to a shape that was not even the winner.
        #
        # So each shape is first converted to its own rank within its own null,
        # which is scale-free, and the shapes are combined on that common
        # footing. Every surrogate is put through the identical reduction, so
        # the test stays exact — the FPR assertion in test_detector.py holds.
        peaks_s = _pick(win).max(axis=1)                     # (S,)
        pool = np.concatenate([peaks_s[:, None], null_s], axis=1)   # (S, M+1)
        # upper rank: how many of this shape's values are >= each value
        order = np.argsort(np.argsort(-pool, axis=1, kind="stable"),
                           axis=1, kind="stable") + 1
        T_all = (order / pool.shape[1]).min(axis=0)          # minP per column
        T_obs, T_null = T_all[0], T_all[1:]
        p = (np.sum(T_null <= T_obs) + 1) / (len(T_null) + 1)
        # report the shape that actually won on the standardised scale
        shape_i = int((order[:, 0] / pool.shape[1]).argmin())
        li = int(_pick(win)[shape_i].argmax())
        signed = float(win[shape_i, li]); peak = float(_pick(win)[shape_i, li])
        lag = float(lags[li]) / FS
        null = null_s[shape_i]
    else:
        null = null_s.max(axis=0) if null_kind == "surrogate_sequences" else null_s
        p = (np.sum(null >= peak) + 1) / (len(null) + 1)

    return {
        "peak": float(peak),
        "signed_peak": signed,
        "direction_mode": direction,
        "direction": "bitrate falls (lens saturated)" if signed < 0
                     else "bitrate rises (scene brightened)",
        "lag_s": float(lag),
        "p_value": float(p),
        # `lag` came from the searched window, so the bound check is inclusive.
        # A strict comparison silently rejected a peak sitting exactly on the
        # boundary after the window was widened.
        "verdict": "WATCHING" if (p < P_THRESHOLD and
                                  LAG_WINDOW[0] <= lag <= LAG_WINDOW[1]) else "CLEAR",
        "shape": ("level", "edge")[shape_i] if Rfull.shape[0] > 1 else None,
        "shapes_searched": int(Rfull.shape[0]),
        "permutations": int(len(null)),
        "null_kind": null_kind,
        "transform": DETECT_TRANSFORM,
        # the winning shape's correlogram, over the searched (signed) lags
        "correlogram": Rfull[shape_i, widx].tolist(),
        "lags_s": (lags / FS).tolist(),
    }


# ── Stimulus construction ────────────────────────────────────────────

def stimulus_coverage(bin_t_ms, t0_ms, sequence=None, chip_s=None, repeats=None):
    """Fraction of the stimulus window the capture actually spans.

    A record shorter than the transmitted sequence truncates the stimulus, which
    breaks the periodic structure the surrogate null assumes. Measured 8 Sep: a
    40 s record against a 60 s stimulus pushed the false-positive rate from
    0.003 to 0.020. Callers surface this rather than quietly reporting a p-value
    from a scan that was cut short.
    """
    sequence = M_SEQUENCE if sequence is None else sequence
    chip_s = CHIP_S if chip_s is None else chip_s
    repeats = SEQ_REPEATS if repeats is None else repeats
    t = np.asarray(bin_t_ms, dtype=float)
    if len(t) == 0:
        return 0.0
    total_ms = len(sequence) * chip_s * repeats * 1000.0
    span = min(t.max(), t0_ms + total_ms) - max(t.min(), t0_ms)
    return float(max(0.0, min(1.0, span / total_ms)))


def build_stimulus(bin_t_ms, t0_ms, sequence=None, chip_s=None, mode=None,
                   edge_win_s=None, repeats=None):
    """Regressor for `detect`, aligned to the capture's own bin timestamps.

    mode="edge"  — a boxcar of edge_win_s after every chip TRANSITION. The
                   encoder codes change, so both white->black and black->white
                   raise the bitrate. See PRD §8 and config.STIM_MODE.
    mode="level" — the raw chip value, ±1. Kept for comparison and because it
                   is what a naive reading of the literature would use.

    Bins outside the stimulus window get 0, so lead-in and tail contribute
    nothing to the correlation instead of being coerced to one polarity.
    """
    sequence = M_SEQUENCE if sequence is None else sequence
    chip_s = CHIP_S if chip_s is None else chip_s
    mode = STIM_MODE if mode is None else mode
    edge_win_s = EDGE_WIN_S if edge_win_s is None else edge_win_s
    repeats = SEQ_REPEATS if repeats is None else repeats

    t = np.asarray(bin_t_ms, dtype=float)
    rel = (t - t0_ms) / 1000.0
    period = len(sequence) * chip_s
    total = period * repeats
    inside = (rel >= 0) & (rel < total)

    if mode == "level":
        stim = np.zeros(len(t))
        idx = ((rel[inside] % period) / chip_s).astype(int)
        idx = np.clip(idx, 0, len(sequence) - 1)
        stim[inside] = 2 * np.asarray(sequence)[idx] - 1
        return stim

    if mode != "edge":
        raise ValueError(f"unknown STIM_MODE {mode!r}")

    # Transition train, WITH wraparound: d[k] = seq[k] XOR seq[k-1]. The lead-in
    # holds the screen at the last chip's value (see the stimulus driver), so the
    # edge into chip 0 is a real one and d[0] is meaningful.
    #
    # By the shift-and-add property, d is itself an m-sequence, so it inherits
    # the thumbtack autocorrelation. Deriving edges WITHOUT wraparound destroys
    # that: measured sidelobe 0.441 versus 0.130, which inflates the permutation
    # null and costs roughly a decade of p-value.
    seq = np.asarray(sequence)
    d = seq ^ np.roll(seq, 1)
    stim = np.zeros(len(t))
    for r in range(repeats):
        for k in range(len(seq)):
            if not d[k]:
                continue
            edge = r * period + k * chip_s
            stim[(rel >= edge) & (rel < edge + edge_win_s)] = 1.0
    return stim


def transition_times(sequence=None, chip_s=None, repeats=None):
    """Chip transition instants, in seconds from t0. Used by the stimulus driver."""
    sequence = M_SEQUENCE if sequence is None else sequence
    chip_s = CHIP_S if chip_s is None else chip_s
    repeats = SEQ_REPEATS if repeats is None else repeats
    seq = list(sequence)
    period = len(seq) * chip_s
    return [r * period + k * chip_s
            for r in range(repeats) for k in range(len(seq))
            if seq[k] != seq[k - 1]]        # k=0 compares against seq[-1]


# ── Capture -> regular bitrate series ────────────────────────────────

def series_feature(rows, mac, bin_ms=BIN_MS, feature=None):
    """The quantity the detector correlates, on a regular grid.

    Packet count rather than byte count by default: one I-frame inflates bytes
    far more than packets, and this camera's p95/median byte ratio reaches 12.6.
    See config.DETECT_FEATURE for the comparison that chose it.
    """
    feature = DETECT_FEATURE if feature is None else feature
    key = "up_pkts" if feature == "packets" else "up_bytes"
    mac = mac.lower()
    sel = [r for r in rows if r["mac"].lower() == mac]
    if not sel:
        return np.array([]), np.array([])
    t = np.array([int(r["t_ms"]) for r in sel], dtype=np.int64)
    v = np.array([float(r[key]) for r in sel], dtype=float)
    lo, hi = t.min(), t.max()
    grid = np.arange(lo, hi + bin_ms, bin_ms, dtype=np.int64)
    out = np.zeros(len(grid))
    idx = ((t - lo) // bin_ms).astype(int)
    np.add.at(out, np.clip(idx, 0, len(grid) - 1), v)
    return grid, out


def series_from_rows(rows, mac, bin_ms=BIN_MS):
    """Per-MAC uplink bitrate on a REGULAR grid.

    The firmware only emits a line for a device that was active in a bin, so a
    silent bin is absent rather than zero. Reindexing onto a regular grid is
    what makes those silences explicit; leaving them out would compress the time
    axis, and the time axis is the correlation's lag axis.

    Returns (t_ms, kbps) with gaps filled as 0.
    """
    mac = mac.lower()
    sel = [r for r in rows if r["mac"].lower() == mac]
    if not sel:
        return np.array([]), np.array([])
    t = np.array([int(r["t_ms"]) for r in sel], dtype=np.int64)
    up = np.array([int(r["up_bytes"]) for r in sel], dtype=float)
    lo, hi = t.min(), t.max()
    grid = np.arange(lo, hi + bin_ms, bin_ms, dtype=np.int64)
    vals = np.zeros(len(grid))
    idx = ((t - lo) // bin_ms).astype(int)
    np.add.at(vals, np.clip(idx, 0, len(grid) - 1), up)
    return grid, vals * 8 / 1000 / (bin_ms / 1000)     # kbps
