// SignalCheck.jsx — 20 seconds that decide whether the scan is worth running.
//
// Detection is not fragile code, it is a marginal measurement. Measured across
// live runs: it succeeds at a 1.7-2.0x stimulus lift and fails at 1.4x, and no
// practical scan length rescues a weak one. So measure the lift FIRST, in 20 s,
// and fix the setup before spending 90 s on a scan that was never going to work.

import React, { useEffect, useRef, useState } from 'react'
import * as api from './api.js'
import { getTorch, runStrobe } from './torch.js'

const CHECK_S = 20

export default function SignalCheck({ target, onPass, onCancel }) {
  const [left, setLeft] = useState(CHECK_S)
  const [live, setLive] = useState(null)
  const [done, setDone] = useState(null)
  const [mode, setMode] = useState('flash')
  const [err, setErr] = useState(null)
  const [why, setWhy] = useState(null)
  const [beamStats, setBeamStats] = useState({ confirmed: 0, failed: 0 })
  // The light must be driven by OUR strobe, not by the backend's view of it.
  // beam_on only exists once the backend has camera packets, so with a sleeping
  // camera the screen fallback never fired and nothing lit up at all.
  const [beamOn, setBeamOn] = useState(false)
  // finish() runs from an interval created once, so it closes over the FIRST
  // render's state. Reading beamStats there always saw zero and reported "the
  // torch did not fire" while the torch was visibly firing. State is for
  // display; the ref is what asynchronous code may read.
  const beamStatsRef = useRef({ confirmed: 0, failed: 0 })
  const [note, setNote] = useState(null)
  const [starting, setStarting] = useState(false)
  const errRef = useRef(null)
  const liveRef = useRef(null), torchRef = useRef(null), stopRef = useRef(null)
  const t0Ref = useRef(null), skewRef = useRef(null), beamRef = useRef(false)

  useEffect(() => {
    let dead = false
    getTorch().then(({ torch, why }) => {
      if (dead) return
      if (torch) { torchRef.current = torch; setMode('torch') } else setWhy(why)
    })
    navigator.wakeLock?.request('screen').catch(() => {})
    stopRef.current = api.probeStream(target.mac, target.channel,
      { period_s: 2.0, lag_s: 0.6, window_s: CHECK_S, hz: 6,
        // Hard server-side bound. iOS Safari does not reliably close an
        // EventSource when the tab is backgrounded, and the probe worker holds
        // the serial port for as long as it runs — which is what made the
        // scan button return "serial port busy".
        max_s: CHECK_S + 15 },
      (d) => {
        // A backend fault is OUR failure, not the camera's. Record it so
        // finish() can say so instead of grading it "no data from the camera",
        // which sent the operator to fix a camera that was streaming fine.
        if (d.error) { setErr(d.error); errRef.current = d.error; return }
        // A re-lock notice carries no measurement; keeping it would blank the
        // readings the grade is computed from.
        if (d.state === 'relocked') { setNote(d.note); return }
        setLive(d); liveRef.current = d; t0Ref.current = d.t0_ms
        if (d.server_ms) {
          const s = Date.now() - d.server_ms
          skewRef.current = skewRef.current == null ? s : Math.min(skewRef.current, s)
        }
      }, (e) => { setErr(e); errRef.current = e })
    // One verified strobe, outside React. Every torch command is awaited and
    // counted, so a light that refuses to switch is visible instead of looking
    // like an absent camera.
    let strobe = null
    const startedAt = Date.now()
    getTorch().then(({ torch }) => {
      if (dead) return
      strobe = runStrobe({
        t0Ms: 0, periodMs: 2000, torch,
        valueAt: (now) => {
          // Free-run on our own clock until the backend hands us a t0 to lock
          // to. Waiting for it meant the light never started when the camera
          // was quiet, which is exactly when you most need to see it working.
          const t0 = t0Ref.current ?? startedAt
          const n = now - (t0Ref.current == null ? 0 : (skewRef.current ?? 0))
          return (((n - t0) / 2000) % 1 + 1) % 1 < 0.5
        },
        onChange: (on) => {
          beamRef.current = on
          setBeamOn(on)
          beamStatsRef.current = { ...strobe.stats }
          setBeamStats({ ...strobe.stats })
        },
      })
    })
    const tick = setInterval(() => setLeft((s) => {
      if (s <= 1) { clearInterval(tick); finish() ; return 0 }
      return s - 1
    }), 1000)
    return () => { dead = true; strobe?.stop(); clearInterval(tick)
                   torchRef.current?.set(false); stopRef.current?.() }
  }, [])

  function finish() {
    // Close the stream FIRST. The verdict screen keeps this component mounted,
    // so the effect cleanup does not run, and the probe worker would go on
    // holding the serial port while the operator reads the result — making the
    // next action ("Run the 90 s scan") fail with a 409.
    stopRef.current?.(); stopRef.current = null
    torchRef.current?.set(false)
    const d = liveRef.current
    const bs = beamStatsRef.current
    // Order matters: a dead stream must never be reported as a quiet camera.
    if (!d && errRef.current) {
      setDone({ grade: 'streamfail', detail: errRef.current }); return
    }
    if (bs.confirmed < 4) {
      setDone({ grade: 'notorch', fired: bs.confirmed, failed: bs.failed }); return
    }
    // A camera on keep-alives still produces bins, so bins/s alone does not
    // catch it. 24 kbps is not a weak response, it is no video.
    // Same test the backend uses: video fills packets to near the MTU,
    // heartbeats do not. Packet RATE alone cannot tell them apart.
    const bpp = d?.bytes_per_packet ?? 0
    if (!d || d.state === 'starved' || bpp < 400) {
      setDone({ grade: 'nodata', rate: d?.data_rate ?? 0, bpp }); return
    }
    // Either direction counts: a torch in the lens saturates the sensor and the
    // bitrate falls; off-axis light brightens the scene and it rises. Strength
    // is how far the ratio is from 1 in either direction.
    const lift = d.ratio ?? 1
    const strength = lift > 0 ? Math.max(lift, 1 / lift) : 0
    const grade = strength >= 2.0 ? 'good' : strength >= 1.5 ? 'marginal' : 'poor'
    setDone({ grade, lift, strength, dir: lift < 1 ? 'falls' : 'rises',
              z: d.z, kOn: d.kbps_on, kOff: d.kbps_off })
    navigator.vibrate?.(grade === 'good' ? [80, 50, 80] : 60)
  }

  const lit = mode === 'flash' && beamOn
  return (
    <div className="spot" style={{ background: lit ? '#fff' : '#07070a',
                                   color: lit ? '#111' : '#e8e8ea' }}>
      <div className="spot-inner wide">
        {!done ? (
          <>
            <div className="spot-score">{left}</div>
            <div className="spot-label">Checking the stimulus reaches the camera</div>
            <div className={'torchline ' + mode}>
              {mode === 'torch'
                ? `TORCH ON — ${beamStats.confirmed} flashes fired`
                  + (beamStats.failed ? ` · ${beamStats.failed} refused` : '')
                : `NO TORCH — the screen is flashing instead${why ? `: ${why}` : ''}`}
            </div>
            <div className="spot-hint">
              Point the torch <strong>straight at the camera lens</strong>, close
              as you can. Lights off. Hold still until this reaches zero.
            </div>
            {note && <div className="spot-hint" style={{opacity:.7}}>{note}</div>}
            <div className="spot-meta">
              {`${beamStats.confirmed} flashes fired`}
              {beamStats.failed ? ` · ${beamStats.failed} REFUSED by the browser` : ''}
              {' · '}
              {live?.data_rate != null ? `${live.data_rate} bins/s` : 'waiting for packets'}
              {live?.ratio ? ` · so far ${live.ratio}x` : ''}
            </div>
          </>
        ) : (
          <>
            <div className={'spot-score s-' + (done.grade === 'good' ? 'hot'
                            : done.grade === 'marginal' ? 'warm' : '')}>
              {(done.grade === 'nodata' || done.grade === 'streamfail')
                ? '—' : `${(done.strength ?? 0).toFixed(1)}x`}
            </div>
            <div className="spot-label">
              {done.grade === 'good' ? 'Strong — run the scan'
                : done.grade === 'marginal' ? 'Marginal — improve it if you can'
                : done.grade === 'notorch' ? 'The torch did not fire'
                : done.grade === 'streamfail' ? 'NAZAR failed, not the camera'
                : done.grade === 'nodata' ? 'No data from the camera'
                : 'Too weak — the scan will fail'}
            </div>
            <div className="spot-hint">
              {done.grade === 'streamfail'
                ? <>The measurement stream stopped before any packet was scored,
                    so nothing was measured. This is a backend fault, not a
                    problem with your camera or your torch — do not change the
                    setup. <br /><span style={{opacity:.6}}>{done.detail}</span></>
                : done.grade === 'notorch'
                ? <>Only {done.fired} flashes fired{done.failed ? `, ${done.failed} refused` : ''}.
                    The browser would not switch the torch, so no light was
                    modulated and nothing could be measured. Reload the page and
                    allow camera access, or use the screen-flash fallback.</>
                : done.grade === 'nodata'
                ? <>Its packets average {Math.round(done.bpp ?? 0)} bytes — heartbeats,
                    not video, which fills packets to about 1100. Open the live
                    view and keep that app in the FOREGROUND with the screen
                    timeout set to Never. Nothing can be measured until it streams.
                    <br /><span style={{opacity:.6}}>({done.rate} bins/s)</span> Open the camera&apos;s live view and keep
                    that phone awake, then check again.</>
                : done.grade === 'good'
                  ? <>The camera&apos;s uplink follows your torch — it {done.dir}{' '}
                      to {done.lift.toFixed(2)}x of its unlit rate, a {done.strength.toFixed(1)}x
                      swing. {done.dir === 'falls' && 'That is the lens saturating, which is a strong hit. '}
                      A 90 s scan detects at this level essentially every time.</>
                  : <>Measured only a {done.strength.toFixed(1)}x swing. Get the torch closer and
                      straight into the lens, turn the room lights off, and point
                      the camera at a still surface — a moving scene is what
                      drowns the signal. Below 1.5x no scan length saves it.</>}
            </div>
            <div className="spot-meta">
              {done.kOn != null && `${done.kOn} kbps lit vs ${done.kOff} unlit · z=${done.z}`}
            </div>
            <button className="big primary wide-btn" disabled={starting}
                    onClick={async () => {
                      setErr(null); setStarting(true)
                      try {
                        // Always 'auto'. Pinning the direction from this check
                        // looked like free statistical power, but the check's
                        // sign is only as good as its phase alignment: with a
                        // 2 s strobe, a 1 s timing error inverts it exactly.
                        // That happened on 8 Sep — the check read "falls", the
                        // camera actually rose, and the one-sided scan returned
                        // CLEAR at p = 0.049 on data that scores 0.0040 both-
                        // sided. Measured cost of 'auto' on real captures is a
                        // factor of ~1.6 in p; the cost of being wrong is the
                        // whole detection.
                        await onPass('auto')
                      } catch (e) {
                        // Stay here and say why. The scan used to fail silently
                        // behind this overlay and look like a dead button.
                        setErr(e.message); setStarting(false)
                      }
                    }}>
              {starting ? 'Starting the scan…'
                : done.grade === 'good' ? 'Run the 90 s scan' : 'Scan anyway'}
            </button>
            <button className="wide-btn" onClick={onCancel}>Back</button>
          </>
        )}
        {err && <div className="err">{err}</div>}
      </div>
    </div>
  )
}
