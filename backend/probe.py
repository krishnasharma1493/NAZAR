"""probe.py — the torch as a directional probe.  (Find it, optical mode)

RSSI tells you which end of the room. It cannot tell you the vent from the plant
from the smoke detector, because multipath corrupts it at exactly the scale you
care about.

Light does not have that problem: it only reaches the camera when you point near
it. So sweep the torch, and watch the camera's uplink. The strongest response
comes from shining straight into the lens, where flare and auto-exposure force a
large re-encode — which is precisely the pinpoint we want.

Two things make this measurable rather than wishful:

* **The beam is strobed, not steady.** A steady beam produces a bitrate rise
  indistinguishable from the encoder's own variation, which is ~50% MAD/median
  on the CP-E35A. A square-wave beam produces a response *locked to a phase we
  know*, and everything not at that phase averages away. This is a lock-in
  amplifier, and it is the same trick as the main detector, applied to a rolling
  window instead of a whole scan.

* **The known lag is used, not guessed.** The scan already measured the encode
  plus transmit delay for this camera (0.50-0.60 s in our runs). Labelling bins
  without shifting by that lag would mix on-windows with off-responses and
  cancel the very thing we are looking for.

The quadrature score is the honesty check. A real response is locked to the
beam's phase, so in-phase should dominate the 90-degrees-out-of-phase score.
Something that merely happens to be bursty scores equally on both, and the UI
refuses to call it a hit.
"""
from __future__ import annotations

from collections import deque

import numpy as np
from scipy.stats import norm as _norm, rankdata


class OpticalProbe:
    """Rolling lock-in score for a strobed torch.

    Feed it (time_s, kbps) as bins arrive. `score()` returns how strongly the
    camera's uplink is following the beam right now.
    """

    def __init__(self, t0_s, period_s=2.0, lag_s=0.6, window_s=6.0,
                 guard_s=0.25, min_per_phase=8):
        self.t0 = float(t0_s)
        self.period = float(period_s)
        self.lag = float(lag_s)
        self.window = float(window_s)
        self.guard = float(guard_s)
        self.min_per_phase = int(min_per_phase)
        self.samples = deque(maxlen=2000)      # (t_s, kbps)
        self.last_t = None

    def update(self, t_s, kbps):
        self.last_t = float(t_s)
        self.samples.append((float(t_s), float(kbps)))
        cutoff = self.last_t - self.window
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    # ── internals ────────────────────────────────────────────────────
    def _split(self, phase_offset=0.0, ranked=None):
        """Bins during beam-ON vs beam-OFF, shifted by the encoder lag.

        `phase_offset` in periods; 0.25 gives the quadrature control.
        """
        ranked = self._ranked() if ranked is None else ranked
        on, off = [], []
        for t, v in self.samples:
            ph = ((t - self.t0 - self.lag) / self.period + phase_offset) % 1.0
            # drop the settling window either side of a transition
            g = self.guard / self.period
            if ph < g or abs(ph - 0.5) < g or ph > 1.0 - g:
                continue
            (on if ph < 0.5 else off).append(ranked.get(t, v))
        return np.asarray(on), np.asarray(off)

    def _ranked(self):
        """Normal scores over the window, for the same reason the detector uses
        them: camera bitrate is heavy-tailed (MAD/median 1.27 measured) and a
        couple of bursts otherwise decide the statistic."""
        if not self.samples:
            return {}
        vals = np.array([v for _, v in self.samples], dtype=float)
        ns = _norm.ppf(rankdata(vals, method="average") / (len(vals) + 1.0))
        return {t: ns[i] for i, (t, _) in enumerate(self.samples)}

    @staticmethod
    def _welch(a, b):
        """Welch t statistic for mean(a) - mean(b)."""
        if len(a) < 3 or len(b) < 3:
            return 0.0
        va, vb = a.var(ddof=1), b.var(ddof=1)
        se = np.sqrt(va / len(a) + vb / len(b))
        if se <= 1e-9:
            return 0.0
        return float((a.mean() - b.mean()) / se)

    # ── output ───────────────────────────────────────────────────────
    def score(self):
        ranked = self._ranked()
        on, off = self._split(ranked=ranked)
        if len(on) < self.min_per_phase or len(off) < self.min_per_phase:
            return {"state": "starved", "score": 0.0, "z": 0.0, "quad_z": 0.0,
                    "ratio": None, "on_bins": int(len(on)), "off_bins": int(len(off)),
                    "kbps_on": None, "kbps_off": None, "confident": False,
                    "data_rate": self.data_rate(),
                    "need_bins": self.min_per_phase}

        z = self._welch(on, off)
        qa, qb = self._split(phase_offset=0.25, ranked=ranked)
        quad = abs(self._welch(qa, qb)) if len(qa) >= 3 and len(qb) >= 3 else 0.0

        # EITHER DIRECTION counts. Torch straight into a lens saturates the
        # sensor and the bitrate FALLS (measured: 1551 kbps lit vs 2530 unlit);
        # off-axis light brightens the scene and it RISES. Both are the camera
        # following our beam. Scoring only rises threw away the strongest
        # response the phone can produce.
        mag = abs(z)
        clean = max(mag - quad, 0.0)
        score = float(min(clean / 8.0, 1.0))
        confident = bool(mag >= 3.0 and mag >= 2.0 * max(quad, 0.5))

        if not confident:
            state = "cold" if score < 0.25 else "warm"
        else:
            state = "hot" if score >= 0.5 else "warm"

        raw_on = np.array([v for tt, v in self.samples
                           if ((tt - self.t0 - self.lag) / self.period) % 1.0 < 0.5])
        raw_off = np.array([v for tt, v in self.samples
                            if ((tt - self.t0 - self.lag) / self.period) % 1.0 >= 0.5])
        profile = self.phase_scan()
        best = max(profile, key=lambda kv: kv[1])
        # `lock_z` is the RANKING statistic for the spotlight, and it is the max
        # |z| over phase offsets, not the in-phase |z|.
        #
        # Two independent things break the in-phase number, and both are normal
        # rather than exceptional. The response sign depends on geometry — a
        # torch in the lens saturates the sensor and the bitrate FALLS — and the
        # phase depends on the browser's torch latency, which we do not know to
        # better than a few hundred ms. A quarter-period error (0.5 s here) nulls
        # the in-phase statistic completely: measured 0.06 against a true 4.81.
        #
        # Measured over 120 trials with a random 0-2 s torch offset, ranking the
        # true spot above a cold one: signed z 50.0% (chance), |z| in-phase
        # 83.3%, max over phases 99.2%. Searching phase inflates the number on
        # noise, but it inflates EVERY spot equally, and the spotlight ranks
        # spots against each other — it never reports a p-value. The scan is
        # what makes hypothesis-test claims, and it does not use this.
        return {"state": state, "score": round(score, 3), "z": round(z, 2),
                "abs_z": round(mag, 2),
                "direction": "falls" if z < 0 else "rises",
                "best_phase": best[0], "best_phase_z": best[1],
                "lock_z": best[1],
                "phase_profile": profile,
                "data_rate": self.data_rate(),
                "quad_z": round(quad, 2), "confident": confident,
                "ratio": round(float(raw_on.mean() / (raw_off.mean() + 1e-9)), 2)
                         if len(raw_on) and len(raw_off) else None,
                "kbps_on": round(float(raw_on.mean())) if len(raw_on) else None,
                "kbps_off": round(float(raw_off.mean())) if len(raw_off) else None,
                "on_bins": int(len(on)), "off_bins": int(len(off))}

    def phase_scan(self, n=8):
        """Score at several phase offsets — the alignment diagnostic.

        If the in-phase score is flat but some other phase scores strongly, the
        beam and the scorer disagree about when the light was on. That is a
        clock problem, not an absence of signal, and it is invisible without
        this: a misaligned lock-in looks exactly like nothing being there.
        """
        out = []
        for i in range(n):
            off = i / n
            a, b = self._split(phase_offset=off)
            if len(a) < 3 or len(b) < 3:
                out.append((round(off, 3), 0.0))
            else:
                out.append((round(off, 3), round(abs(self._welch(a, b)), 2)))
        return out

    def data_rate(self):
        """Bins per second actually arriving — the other way this fails."""
        if len(self.samples) < 2:
            return 0.0
        span = self.samples[-1][0] - self.samples[0][0]
        return 0.0 if span <= 0 else round((len(self.samples) - 1) / span, 1)

    def beam_on(self, t_s=None):
        """Is the beam meant to be lit right now? Drives the local preview."""
        t = self.last_t if t_s is None else t_s
        if t is None:
            return False
        return ((t - self.t0) / self.period) % 1.0 < 0.5
