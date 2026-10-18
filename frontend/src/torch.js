// torch.js — the phone's torch, driven deterministically and VERIFIED.
//
// NAZAR's stimulus is the phone torch:
//     phone torch -> camera lens -> Wi-Fi uplink -> ESP32 -> NAZAR
//
// Two defects here caused real scans to return CLEAR against a real camera:
//
//  1. The torch was switched inside a React setState updater. React may invoke
//     updaters more than once or defer them; side effects there are unsupported.
//     Under concurrent rendering the light frequently did not switch at all.
//  2. applyConstraints failures were swallowed by .catch(() => {}). On Android
//     the constraint often rejects when called in quick succession, leaving the
//     torch stuck in its last state with nothing reporting it.
//
// A scan with a constant torch has no modulation, so the detector correctly
// finds nothing — and the operator is told the camera is clear. That is the
// worst failure this program can have, so the strobe is now driven by one
// timer outside React, every transition is awaited and confirmed, and the
// confirmed count travels with the capture. If the light did not run, the
// verdict says so instead of saying CLEAR.

let handle = null
let why = null
let acquiring = null

export function torchReady() { return !!handle }
export function torchWhy() { return why }

export async function getTorch() {
  if (handle) return { torch: handle, why: null }
  if (acquiring) return acquiring
  acquiring = (async () => {
    if (!window.isSecureContext) {
      why = 'needs https — the browser hides the camera API on http'
      return { torch: null, why }
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      why = 'this browser has no camera API'
      return { torch: null, why }
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: 'environment' } },
      })
      const track = stream.getVideoTracks()[0]
      const caps = track.getCapabilities?.() || {}
      if (!caps.torch) {
        track.stop()
        why = 'this camera reports no torch'
        return { torch: null, why }
      }
      handle = {
        state: false,
        ok: 0,
        failed: 0,
        // Awaited and checked. Returns whether the light ACTUALLY changed.
        set: async (on) => {
          try {
            await track.applyConstraints({ advanced: [{ torch: on }] })
            handle.state = on
            handle.ok++
            return true
          } catch (e) {
            handle.failed++
            return false
          }
        },
        stop: () => { try { track.applyConstraints({ advanced: [{ torch: false }] }) } catch {} },
        release: () => { try { track.stop() } catch {} ; handle = null },
      }
      why = null
      return { torch: handle, why: null }
    } catch (e) {
      why = e.name === 'NotAllowedError'
        ? 'camera permission denied — allow it and reload'
        : (e.message || String(e))
      return { torch: null, why }
    } finally {
      acquiring = null
    }
  })()
  return acquiring
}

/**
 * Run a square-wave strobe outside React entirely.
 *
 * onChange(on, t_ms, confirmed) fires once per transition, AFTER the light has
 * actually been commanded, with whether the command succeeded. In screen-flash
 * mode there is nothing to confirm, so `confirmed` is true and the caller
 * paints the page.
 *
 * Returns a stop() function and a live stats object.
 */
export function runStrobe({ t0Ms, periodMs, valueAt, onChange, torch }) {
  let last = null
  const stats = { transitions: 0, confirmed: 0, failed: 0 }
  let stopped = false

  const timer = setInterval(async () => {
    if (stopped) return
    const now = Date.now()
    const on = valueAt(now)
    if (on === last) return
    last = on
    stats.transitions++
    let ok = true
    // The torch command is SLOW — applyConstraints on iOS takes hundreds of
    // milliseconds. Timestamping the transition after the await made every
    // mark late by that latency, so the backend placed t0 up to a second after
    // the light actually changed and the camera's real response landed at a
    // NEGATIVE lag the detector was not searching. Measured on the 8 Sep 23:41
    // capture: a 2.3x response scored p = 0.83 CLEAR. The light changes
    // somewhere between issuing the command and the promise resolving, so the
    // midpoint is the estimator with the smallest worst-case error.
    const tCmd = Date.now()
    if (torch) ok = await torch.set(on)
    const tDone = Date.now()
    const latency = tDone - tCmd
    stats.latencyMs = stats.latencyMs === undefined
      ? latency : (stats.latencyMs * (stats.transitions - 1) + latency) / stats.transitions
    stats.latencyMaxMs = Math.max(stats.latencyMaxMs ?? 0, latency)
    if (ok) stats.confirmed++; else stats.failed++
    onChange?.(on, Math.round((tCmd + tDone) / 2), ok)
  }, 25)

  return {
    stats,
    stop: () => { stopped = true; clearInterval(timer); torch?.set(false) },
  }
}

// Fire the torch so a human can SEE it work, before committing to a scan.
export async function testFire(ms = 1800) {
  const { torch, why } = await getTorch()
  if (!torch) return { ok: false, why }
  const on = await torch.set(true)
  await new Promise((r) => setTimeout(r, ms))
  await torch.set(false)
  return { ok: on, why: on ? null : 'the torch command was refused by the browser' }
}
