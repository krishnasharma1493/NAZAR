# Regression fixtures — committed on purpose

`backend/captures/` is gitignored (captures are large and meaningless out of
context). These few are the exception: the test suite and `tools/preflight.py`
assert against them, so a fresh clone with no hardware must still have them.

| File | What it is |
|------|------------|
| `scan_20260907_205427_camera.*` | Gate 2 m-sequence scan of the CP-E35A. `replay.py` must return WATCHING, p<0.01, lag 0.60 s. **The** regression fixture. |
| `wave_20260907_195034.*` | Gate 1 controlled 6-wave trial, 6/6 waves raised the bitrate. Cue times logged on the capture clock. |
| `flash_20260907_203323.*` | Full-screen flash trial that established the encoder responds to transitions, not brightness. |
| `gate1_20260907_183554.csv` | 148 s of camera traffic, 8 MACs — negative-class training data for the classifier. |
| `scan_20260908_014034_camera3.*` | **Gate 2 pass, camera half.** p=0.0055, peak 0.439, lag 0.50 s, WATCHING. The confirming run, taken with the detector exactly as it stands now. |
| `scan_20260908_013018_laptop-control.*` | **Gate 2 pass, negative control.** A device pushing real traffic that cannot see the screen: CLEAR, p=0.4748. Event-locked rise 1/7. |
| `scan_20260908_012849_camera2.*` | A camera run that FAILED (CLEAR, p=0.10) because the camera was aimed at a busy scene rather than the screen. Kept deliberately: it is the regression test that the detector still says CLEAR when the stimulus does not reach the lens. |

| `scan_20260908_125904_s_002.*` | **Phone-torch scan, 8 Sep 12:59.** The raw detector called this CLEAR (p=0.029); with normal-scores it is WATCHING at p=0.0065. Kept as the regression that pins the robust transform. |

Do not edit these. If a threshold change alters what they produce, that is the
test telling you something, not a file that needs updating.
