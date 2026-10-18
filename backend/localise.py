"""localise.py — RSSI EWMA + body-shadow bearing fit.  (F10, F11, PRD §8 stage 5)

Direction, never distance. RSSI is multipath-ridden; metre-level localisation is
an explicit non-goal (PRD N3). Two products:

* **Hot/cold** — smoothed RSSI plus a trend, for walking the room.
* **Bearing** — the user rotates 360 degrees holding the sniffer in front of
  them. Their own body attenuates the signal when it sits between the sniffer
  and the camera, so RSSI varies roughly as a cosine of heading, peaking when
  the user faces the camera. Fit that cosine and the phase is the bearing.

The fit reports its own confidence and refuses to answer when the modulation is
indistinguishable from noise. An arrow pointing confidently at nothing is worse
than no arrow: the user walks the wrong way and stops trusting the instrument.
"""
from __future__ import annotations

import math
from collections import deque

import numpy as np

from config import RSSI_ALPHA, BEARING_SWEEP_S

MIN_BEARING_SAMPLES = 12
MIN_HEADING_COVERAGE_DEG = 270.0
BEARING_P_THRESHOLD = 0.05


class RssiTracker:
    """Hot/cold meter. F10.

    Everything here is measured in SECONDS, not samples. The previous version
    smoothed per sample, so its behaviour depended on how chatty the device
    happened to be: measured 8 Sep, active devices give ~10 frames/s but quiet
    ones give ~4.4/s, and an intermittently-streaming camera far less. A
    20-sample trend window therefore spanned 2 s or 20 s depending on the
    target, and the meter felt fine on a busy device and dead on the one you
    actually care about.

    Three things make it feel like an instrument rather than a readout:

    * **Two time constants.** A fast EWMA (0.4 s) tracks where you are; a slow
      one (3 s) remembers where you were. Their difference is a band-pass that
      responds to movement and ignores both jitter and slow drift.
    * **A trend in dB per second**, fitted over a fixed time window, so the
      threshold means the same thing whatever the frame rate.
    * **An auto-ranging level.** Absolute RSSI spans ~55 dB but a walk across a
      room moves it 5-15 dB, so a fixed scale barely twitches. The level is
      scaled to the range actually seen this session, with a floor on the span
      so noise alone cannot fill the bar.
    """

    def __init__(self, tau_fast=0.4, tau_slow=3.0, trend_window_s=1.5,
                 min_span_db=8.0, k_enter=1.5, k_leave=1.0, min_db_s=0.6,
                 max_window_s=4.0, min_points=8, persist_s=0.25):
        self.tau_fast, self.tau_slow = tau_fast, tau_slow
        self.trend_window_s = trend_window_s
        self.min_span_db = min_span_db
        # The bar for "this is real movement" is set by the STANDARD ERROR of
        # the fitted slope, not by the raw noise. Fitting a slope through noisy
        # points has its own uncertainty, and comparing the slope to the noise
        # instead measured 68% false movement while standing perfectly still.
        # An SE-based threshold self-calibrates for frame rate, window length
        # and how noisy the room happens to be.
        #
        # k_enter and persist_s were swept together against real captured noise
        # on 8 Sep, scoring a walk-in / pause / walk-out profile at 2-10 Hz:
        # these settings give 57% of a walk correctly called at 2% false
        # movement while standing still. Comparing the slope to the raw noise
        # instead of to its own standard error gave 68% false movement; no
        # persistence gave 4%; both together give 2% without going deaf.
        self.k_enter, self.k_leave = k_enter, k_leave
        self.min_db_s = min_db_s
        self.max_window_s, self.min_points = max_window_s, min_points
        # A state must hold for persist_s before it is shown. Real movement is
        # sustained; noise excursions are not. This buys most of the stability
        # a higher threshold would, without going deaf to a genuine walk.
        self.persist_s = persist_s
        self._cand = "steady"
        self._cand_since = None
        self.fast = self.slow = None
        self.last_t = None
        self.samples = deque(maxlen=600)      # (t_s, raw_dbm)
        self.lo = self.hi = None
        self.peak = None
        self._state = "steady"
        self.n = 0

    # ── input ────────────────────────────────────────────────────────
    def update(self, rssi, t_s=None):
        """t_s in SECONDS. Omitted, samples are assumed 0.1 s apart."""
        rssi = float(rssi)
        if t_s is None:
            t_s = (self.last_t or 0.0) + 0.1
        if self.last_t is not None and t_s <= self.last_t:
            t_s = self.last_t + 1e-3
        dt = 0.1 if self.last_t is None else min(t_s - self.last_t, 5.0)
        self.last_t = t_s
        self.n += 1

        if self.fast is None:
            self.fast = self.slow = rssi
        else:
            self.fast += (1 - math.exp(-dt / self.tau_fast)) * (rssi - self.fast)
            self.slow += (1 - math.exp(-dt / self.tau_slow)) * (rssi - self.slow)

        self.samples.append((t_s, rssi))
        self.peak = rssi if self.peak is None else max(self.peak, rssi)
        # Range tracked on the SMOOTHED value, so a single spike cannot stretch
        # the scale and flatten everything else for the rest of the session.
        self.lo = self.fast if self.lo is None else min(self.lo, self.fast)
        self.hi = self.fast if self.hi is None else max(self.hi, self.fast)
        self._advance()
        return self.fast

    # ── derived ──────────────────────────────────────────────────────
    @property
    def value(self):
        return self.fast

    def rate_hz(self):
        w = [s for s in self.samples if s[0] >= self.last_t - 2.0] if self.samples else []
        return 0.0 if len(w) < 2 else round((len(w) - 1) / max(w[-1][0] - w[0][0], 1e-3), 1)

    def _fit(self):
        """(slope dB/s, standard error of that slope).

        The window widens until it holds `min_points`, capped at max_window_s,
        so a quiet device is fitted over more time instead of returning nothing.
        """
        if not self.samples:
            return 0.0, float("inf")
        w = self.trend_window_s
        while True:
            pts = [s for s in self.samples if s[0] >= self.last_t - w]
            if len(pts) >= self.min_points or w >= self.max_window_s:
                break
            w = min(w * 1.6, self.max_window_s)
        if len(pts) < 4:
            return 0.0, float("inf")
        x = np.array([p[0] for p in pts]); y = np.array([p[1] for p in pts])
        xc = x - x.mean()
        sxx = float(np.sum(xc ** 2))
        if sxx <= 1e-6:
            return 0.0, float("inf")
        slope, intercept = np.polyfit(xc, y, 1)
        resid = y - (slope * xc + intercept)
        dof = max(len(x) - 2, 1)
        se = float(np.sqrt(np.sum(resid ** 2) / dof / sxx))
        return float(slope), se

    def trend(self, window_s=None):
        """dB per SECOND. Positive means the signal is getting stronger."""
        return self._fit()[0]

    def trend_error(self):
        return self._fit()[1]

    def noise_db(self):
        """Short-term jitter, so the threshold can adapt to a noisy room."""
        pts = [s[1] for s in self.samples if s[0] >= self.last_t - 3.0]
        if len(pts) < 6:
            return 0.0
        r = np.asarray(pts, dtype=float)
        return float(1.4826 * np.median(np.abs(r - np.median(r))))

    def _advance(self):
        """Step the warmer/colder machine. Driven by ARRIVING DATA, not by
        whoever happens to ask: a caller that polls state() once after a run
        would otherwise never accumulate any persistence, and a caller that
        polls faster than updates arrive would accumulate it too quickly."""
        if self.fast is None or self.n < 5:
            return
        sl, se = self._fit()
        if not np.isfinite(se):
            return
        enter = max(self.k_enter * se, self.min_db_s)
        leave = self.k_leave * se

        # what the instantaneous fit says right now
        if self._state == "steady":
            want = "warmer" if sl >= enter else ("colder" if sl <= -enter else "steady")
        elif abs(sl) < leave:
            want = "steady"
        else:
            want = "warmer" if sl > 0 else "colder"

        if want != self._cand:
            self._cand, self._cand_since = want, self.last_t
        # returning to steady is immediate; claiming movement must persist
        if want == self._state:
            self._cand_since = self.last_t
        elif want == "steady" or (self._cand_since is not None
                                  and self.last_t - self._cand_since >= self.persist_s):
            self._state = want

    def state(self):
        """warmer / colder / steady. A pure read of the machine above."""
        return self._state

    def level(self):
        """0..1 across the range actually seen this session."""
        if self.fast is None or self.lo is None:
            return 0.0
        lo, hi = self.lo, self.hi
        if hi - lo < self.min_span_db:            # pad a narrow range
            mid = (hi + lo) / 2
            lo, hi = mid - self.min_span_db / 2, mid + self.min_span_db / 2
        return float(max(0.0, min(1.0, (self.fast - lo) / (hi - lo))))

    def as_dict(self):
        return {
            "rssi": None if self.fast is None else round(self.fast, 1),
            "raw": None if not self.samples else self.samples[-1][1],
            "slow": None if self.slow is None else round(self.slow, 1),
            "delta_db": None if self.fast is None else round(self.fast - self.slow, 2),
            "from_peak_db": None if self.fast is None or self.peak is None
                            else round(self.fast - self.peak, 1),
            "peak": self.peak,
            "level": round(self.level(), 3),
            "range": None if self.lo is None else [round(self.lo, 1), round(self.hi, 1)],
            "trend_db_s": round(self.trend(), 2),
            "trend_se": round(self.trend_error(), 2) if np.isfinite(self.trend_error()) else None,
            "noise_db": round(self.noise_db(), 2),
            "rate_hz": self.rate_hz(),
            "state": self.state(),
            "samples": self.n,
        }


def fit_bearing(headings_deg, rssi):
    """Least-squares fit of rssi ~ A + B*cos(theta - phi). F11.

    Returns bearing_deg (the phase, i.e. the direction of maximum signal),
    amplitude, r2, p_value and a `reliable` flag. The p-value comes from
    permuting the heading labels: if the modulation is real, shuffling headings
    destroys it.
    """
    th = np.radians(np.asarray(headings_deg, dtype=float))
    y = np.asarray(rssi, dtype=float)
    n = len(y)
    out = {"samples": int(n), "reliable": False, "bearing_deg": None,
           "amplitude_db": 0.0, "r2": 0.0, "p_value": 1.0, "reason": None}
    if n < MIN_BEARING_SAMPLES:
        out["reason"] = f"need at least {MIN_BEARING_SAMPLES} samples, got {n}"
        return out

    span = _heading_span(np.degrees(th) % 360)
    if span < MIN_HEADING_COVERAGE_DEG:
        out["reason"] = f"only {span:.0f} degrees of the turn covered; sweep the full circle"
        return out

    def _fit(theta, vals):
        X = np.column_stack([np.ones(len(theta)), np.cos(theta), np.sin(theta)])
        beta, *_ = np.linalg.lstsq(X, vals, rcond=None)
        pred = X @ beta
        ss_res = float(np.sum((vals - pred) ** 2))
        ss_tot = float(np.sum((vals - vals.mean()) ** 2))
        amp = float(np.hypot(beta[1], beta[2]))
        phi = float(np.degrees(np.arctan2(beta[2], beta[1])) % 360)
        r2 = 0.0 if ss_tot <= 0 else 1 - ss_res / ss_tot
        return amp, phi, r2

    amp, phi, r2 = _fit(th, y)

    rng = np.random.default_rng(0)
    null = np.array([_fit(rng.permutation(th), y)[0] for _ in range(1000)])
    p = float((np.sum(null >= amp) + 1) / (len(null) + 1))

    out.update(bearing_deg=round(phi, 1), amplitude_db=round(amp, 2),
               r2=round(r2, 3), p_value=round(p, 4))
    if p >= BEARING_P_THRESHOLD:
        out["reason"] = ("no reliable direction — the RSSI modulation is within "
                         "noise. Stand closer, or sweep again more slowly.")
    else:
        out["reliable"] = True
    return out


def _heading_span(deg):
    """Largest arc actually covered, in degrees (0-360)."""
    if len(deg) < 2:
        return 0.0
    d = np.sort(np.asarray(deg) % 360)
    gaps = np.diff(np.concatenate([d, [d[0] + 360]]))
    return float(360 - gaps.max())


def bearing_arrow(bearing_deg):
    """Nearest of 8 compass arrows, for the UI."""
    if bearing_deg is None:
        return "?"
    arrows = ["^", "/", ">", "\\", "v", "/", "<", "\\"]
    names = ["ahead", "ahead-right", "right", "behind-right",
             "behind", "behind-left", "left", "ahead-left"]
    i = int((bearing_deg % 360 + 22.5) // 45) % 8
    return {"arrow": arrows[i], "label": names[i], "deg": round(bearing_deg % 360, 1)}
