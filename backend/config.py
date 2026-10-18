# backend/config.py
#
# NAZAR — every tunable lives here. Single source of truth.
# Values taken verbatim from NAZAR_PRD.md §6. On demo day there is ~30 min to
# recalibrate in the venue; nothing below may be duplicated inline elsewhere.

# ── Serial ───────────────────────────────────────────────────────────
SERIAL_PORT     = "auto"                    # auto-detect the CH340 bridge by USB ID.
                                            # macOS renumbers /dev/cu.usbserial-* between
                                            # plug-ins (seen as -110 and -10 on the same
                                            # board), so a fixed path WILL break on the day.
                                            # Pin a path here only to override detection.
SERIAL_BAUD     = 460800                    # 921600 verified corrupt on this CH340C, 7 Sep — see README

# ── Capture ──────────────────────────────────────────────────────────
BIN_MS          = 100                       # must match firmware BIN_MS
CHANNELS        = range(1, 14)
SCAN_DWELL_MS   = 600
MAX_DEVICES     = 40                        # must match firmware MAX_DEV

# ── Discovery filter ─────────────────────────────────────────────────
UPLINK_RATIO    = 3.0        # up_bytes / (dn_bytes + 1)
MIN_KBPS        = 100        # sustained uplink to be a candidate
MIN_SUSTAIN_S   = 10

# ── Stimulus ─────────────────────────────────────────────────────────
M_SEQUENCE      = [1,1,1,1,0,1,0,1,1,0,0,1,0,0,0]   # length-15, near-ideal autocorrelation
CHIP_S          = 2.0        # ≥ encoder GOP settling time

# The sequence is transmitted SEQ_REPEATS times back to back.
#
# A hidden camera is only lit for part of a room sweep, and a camera that misses
# most of one pass cannot be detected at all. Measured by simulation, 8 Sep,
# median p over 5 runs against the fraction of the sweep the camera was lit for:
#
#   repeats  scan    lit 100%   lit 50%   lit 25%   lit 12%
#     1       36 s    0.0017     0.0133    0.0632    0.3328
#     2       66 s    0.0017     0.0017    0.0183    0.1331
#     3       96 s    0.0017     0.0017    0.0017    0.0250
#
# 2 covers a camera lit for a quarter of the sweep; 3 for an eighth.
#
# Set to 3 after three consecutive live runs on 8 Sep detected, detected, then
# MISSED — the difference was the camera's own noise (MAD/median 0.73 and 0.83
# when it worked, 0.99 when it did not) and the stimulus lift (1.77x, 1.68x,
# then 1.43x). Simulated on the WORST recorded noise, detection rate at p<0.01:
#
#     lift    60 s     90 s    120 s
#     2.5x    100%     100%     100%
#     2.0x     91%     100%     100%
#     1.7x     75%      91%     100%
#     1.4x     41%      58%      66%
#
# 90 s buys the margin that turns a 1.7-2.0x lift from "usually" into "always".
# Below 1.4x no practical scan length rescues it: fix the setup instead, which
# is what the pre-scan signal check exists to tell you.
SEQ_REPEATS     = 3
SEQ_PERIOD_S    = CHIP_S * len(M_SEQUENCE)          # 30 s, one pass
SCAN_S          = SEQ_PERIOD_S * SEQ_REPEATS        # 60 s total

# The camera's encoder codes CHANGE, not brightness: a white->black transition
# is as strong a stimulus as black->white. Measured 7 Sep on the CP-E35A, 16
# transitions: correlating against chip LEVEL gave 6/8 consistency (p = 0.14),
# against chip TRANSITIONS 13/16 (p = 0.0106). Correlating against level
# averages rising and falling edges toward zero — a silent near-null.
STIM_MODE       = "edge"     # "edge" (transitions) or "level" (chip value)
STIM_SHAPES     = ("level", "edge")   # response shapes the detector searches.
                            # Geometry decides which one the camera shows: a
                            # torch held in the lens gives a sustained level
                            # change, off-axis light a transient at each edge.
                            # Measured 8 Sep on one capture: level p=0.0015,
                            # edge p=0.10. Both are searched and every
                            # surrogate is searched over both, so the
                            # multiple-comparison cost is paid, not assumed.
EDGE_WIN_S      = 1.0        # response boxcar after each transition; measured
                             # elevation ran +0.5 s to +1.4 s post-transition

# ── Detector ─────────────────────────────────────────────────────────
# Bitrate is transformed to normal scores (average ranks -> the normal quantile)
# before correlating. Camera bitrate is violently heavy-tailed — measured
# MAD/median of 0.83-1.27 on the CP-E35A — and a handful of huge bursts
# otherwise dominate the dot product and bury a real response.
#
# Measured 8 Sep on every capture we hold, plus synthetic controls:
#
#                       camera3   12:47    12:59   laptop  misaimed
#   raw                 0.0055   0.1284*  0.0285*  0.4748   0.1029     (* missed)
#   normal scores       0.0050   0.0060   0.0060   0.7701   0.0140
#
#   false positives at p<0.01     raw      normal scores
#     gaussian noise              0.005        0.0067
#     heavy-tailed noise          0.003        0.0033
#     bursty t(2) noise           n/a          0.0033
#     unrelated periodic traffic  0.00         0.00
#   power at a 1.5x lift          13%          93%
DETECT_TRANSFORM = "rank"    # "rank" (normal scores) or "raw"

# Which measured quantity carries the response. Tested 8 Sep across every real
# capture held, scoring how many gave the expected verdict:
#
#   feature           camera3  12:59  13:27  laptop-ctrl  misaimed   correct
#   bytes (raw)        0.009   0.033  0.003     0.472      0.096      4/5
#   bytes ranked       0.007   0.009  0.025     0.460      0.020      4/5
#   packets            0.009   0.034  0.003     0.760      0.082      4/5
#   packets ranked     0.004   0.009  0.006     0.369      0.018      5/5
#   mean packet size   0.016   0.005  0.410     0.713      0.136      3/5
#
# Packet COUNT is steadier than byte count: a single I-frame inflates bytes far
# more than packets, and the camera's p95/median byte ratio reaches 12.6.
DETECT_FEATURE = "packets"   # "packets" or "bytes"
MAX_LAG_S       = 3.0
LAG_WINDOW      = (-0.8, 2.2)   # signed. Positive is the physical encode +
                                # transmit delay. Negative lags are searched
                                # because the phone and the laptop keep
                                # independent clocks and the browser torch
                                # call has its own latency, so the modelled
                                # stimulus can sit ahead of the real light.
                                # Measured 8 Sep: a 2.3x response peaked at
                                # -0.5 s and scored CLEAR (p=0.83) purely
                                # because the window began at +0.05 s.
                                # Surrogates search the identical window, so
                                # this costs power, not calibration. The bound
                                # is -0.8 rather than -1.5 because the torch
                                # timestamp bias is now fixed at source (see
                                # runStrobe); -0.8 covers the residual half-
                                # latency while a wider window measurably cost
                                # the two Gate 2 captures their verdicts.
N_PERMUTATIONS  = 2000
P_THRESHOLD     = 0.01
DETREND_WIN     = 51            # median filter window, in bins

# ── Localisation ─────────────────────────────────────────────────────
RSSI_ALPHA      = 0.2           # EWMA smoothing
BEARING_SWEEP_S = 20

# ── Demo fallback ────────────────────────────────────────────────────
# Set to a capture filename (in captures/ or fixtures/) to run the ENTIRE system
# off recorded data — same detector, same API, same UI. For when the board dies
# on stage. Overridden by the NAZAR_REPLAY environment variable.
#
# Replay mode is announced loudly in /health and across the top of the UI. A
# replayed measurement must never be mistakable for a live one.
REPLAY_SOURCE   = None

# ── Evidence ─────────────────────────────────────────────────────────
SIGNING_KEY_PATH = "signing_key.pem"
SOFTWARE_VERSION = "nazar-1.0"
