// Spotlight.jsx — find WHICH SPOT, by comparison rather than by verdict.
//
// The scan already told you a camera is watching. The question here is where,
// and that is a comparison, not a hypothesis test.
//
// Measured against this camera's real bitrate noise (MAD/median = 1.27), asking
// "is this spot hot?" in a few seconds is hopeless: a 2x response is detected
// 8-12% of the time even at a 15 s dwell. Asking "which of these spots is
// hottest?" is 5-6x better — 89% correct at a 3x response, 67% at 2x, and the
// true spot lands in the top two 82-95% of the time. So: sample several spots,
// rank them, and let the leader tell you where to look.
//
// Each measurement is a lock-in against the strobe's own phase, shifted by the
// encoder lag the scan measured, on rank-transformed bitrate.

import React, { useEffect, useRef, useState } from 'react'
import * as api from './api.js'
import { getTorch, runStrobe } from './torch.js'

const SETTLE_S = 2          // you are still moving when you press the button
const DWELL_S = 12          // measured: 12 s ranks the true spot first far more often than 8 s

const PLACES = ['Vent', 'Smoke alarm', 'Shelf', 'Plant', 'Mirror', 'Clock',
                'Socket', 'Corner', 'TV / screen', 'Soft toy', 'Door', 'Other']

export default function Spotlight({ target, lagS = 0.6, onClose }) {
  const [live, setLive] = useState(null)
  const [mode, setMode] = useState('flash')
  const [why, setWhy] = useState(null)
  const [err, setErr] = useState(null)
  const [phase, setPhase] = useState('idle')     // idle | settle | measure
  const [left, setLeft] = useState(0)
  const [spots, setSpots] = useState([])
  const [place, setPlace] = useState(PLACES[0])
  const [beamStats, setBeamStats] = useState({ confirmed: 0, failed: 0 })
  // The light must be driven by OUR strobe, not by the backend's view of it.
  // beam_on only exists once the backend has camera packets, so with a sleeping
  // camera the screen fallback never fired and nothing lit up at all.
  const [beamOn, setBeamOn] = useState(false)
  const beamStatsRef = useRef({ confirmed: 0, failed: 0 })

  const torchRef = useRef(null)
  const strobeRef = useRef(null)
  const stopRef = useRef(null)
  const audioRef = useRef(null)
  const tickRef = useRef(null)
  const liveRef = useRef(null)
  const skewRef = useRef(null)
  const t0Ref = useRef(null)
  const beamRef = useRef(false)

  useEffect(() => {
    let dead = false
    getTorch().then(({ torch, why }) => {
      if (dead) return
      if (torch) { torchRef.current = torch; setMode('torch') } else setWhy(why)
    })
    navigator.wakeLock?.request('screen').catch(() => {})

    // Find It is a hunting screen and can be open for minutes, but the stream
    // holds the serial port for as long as it runs, so it is bounded server
    // side and simply reopened. An unbounded stream plus a backgrounded phone
    // (iOS does not reliably close an EventSource) pinned the port for the
    // whole 10 minute default, and everything else then failed with 409.
    const open = () => {
      stopRef.current = api.probeStream(target.mac, target.channel,
        { period_s: 2.0, lag_s: lagS, window_s: DWELL_S, hz: 8, max_s: 240 },
        (d) => {
          if (d.error) { setErr(d.error); return }
          // A re-lock notice carries no measurement and no t0; letting it
          // through would blank the reading and unalign the strobe.
          if (d.state === 'relocked') { setErr(d.note); return }
          setLive(d); liveRef.current = d
          t0Ref.current = d.t0_ms
          if (d.server_ms) {
            const s = Date.now() - d.server_ms
            skewRef.current = skewRef.current == null ? s : Math.min(skewRef.current, s)
          }
        },
        () => { if (!dead) setTimeout(() => { if (!dead) open() }, 600) })
    }
    open()

    // The beam runs continuously on the LAPTOP's clock, so the strobe and the
    // scorer agree even when the two devices disagree about the time.
    // One verified strobe, outside React. Every torch command is awaited and
    // counted, so a light that refuses to switch is visible instead of looking
    // like an absent camera.
    let strobe = null
    const startedAt = Date.now()
    strobeRef.current = null
    getTorch().then(({ torch }) => {
      if (dead) return
      strobe = strobeRef.current = runStrobe({
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

    startAudio()
    return () => {
      // `clearInterval(beam)` used to stand here and `beam` does not exist, so
      // this threw a ReferenceError before reaching the three calls below: the
      // strobe kept running, the torch stayed lit, and the probe stream went on
      // holding the serial port after you left the screen.
      dead = true
      strobe?.stop()
      torchRef.current?.set(false); stopRef.current?.(); stopAudio()
    }
  }, [])

  function stopAudio() { clearTimeout(tickRef.current); tickRef.current = null
    audioRef.current?.close?.(); audioRef.current = null }
  function startAudio() {
    const Ctx = window.AudioContext || window.webkitAudioContext
    if (!Ctx) return
    const ctx = new Ctx(); audioRef.current = ctx
    const loop = () => {
      const L = liveRef.current
      const s = Math.max(0, Math.min(((L?.lock_z ?? L?.abs_z ?? 0)) / 6, 1))
      const o = ctx.createOscillator(), g = ctx.createGain()
      o.frequency.value = 420 + s * 900
      g.gain.setValueAtTime(0.0001, ctx.currentTime)
      g.gain.exponentialRampToValueAtTime(0.2, ctx.currentTime + 0.005)
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.05)
      o.connect(g); g.connect(ctx.destination)
      o.start(); o.stop(ctx.currentTime + 0.06)
      tickRef.current = setTimeout(loop, 60 + (1 - s) * 700)
    }
    loop()
  }

  function chime() {
    const ctx = audioRef.current
    if (!ctx) return
    ;[0, 0.13].forEach((delay, i) => {
      const o = ctx.createOscillator(), g = ctx.createGain()
      o.type = 'square'; o.frequency.value = i ? 1320 : 880
      const t0 = ctx.currentTime + delay
      g.gain.setValueAtTime(0.0001, t0)
      g.gain.exponentialRampToValueAtTime(0.3, t0 + 0.01)
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.22)
      o.connect(g); g.connect(ctx.destination)
      o.start(t0); o.stop(t0 + 0.24)
    })
  }

  // Settle, then measure one dwell, then read the score off the rolling window.
  async function measure() {
    if (phase !== 'idle') return
    setErr(null); setPhase('settle')
    for (let s = SETTLE_S; s > 0; s--) { setLeft(s); await wait(1000) }
    setPhase('measure')
    for (let s = DWELL_S; s > 0; s--) { setLeft(s); await wait(1000) }
    const d = liveRef.current
    const bs = beamStatsRef.current
    setPhase('idle'); setLeft(0)
    if (bs.confirmed < 4) {
      setErr(`the light only switched ${bs.confirmed} times`
             + (bs.failed ? ` (${bs.failed} refused by the browser)` : '')
             + ' — nothing was modulated, so this measurement is meaningless.')
      return
    }
    if (!d || d.state === 'starved') {
      setErr(`only ${d?.data_rate ?? 0} bins/s — the camera is barely transmitting. `
             + 'Open its live view, or nothing here can work.')
      return
    }
    navigator.vibrate?.(90)
    // Rank on lock_z: the strength of the lock at its BEST phase, sign
    // discarded. Ranking on the signed in-phase z put the lens — the strongest
    // response there is — at the BOTTOM of the board, below a blank wall,
    // because a torch held in a lens saturates the sensor and the bitrate
    // falls. Measured over 120 trials with a random torch offset, ranking the
    // true spot first: signed z 50.0% (chance), lock_z 99.2%.
    const strength = d.lock_z ?? d.best_phase_z ?? Math.abs(d.z ?? 0)
    setSpots((prev) => {
      const next = [...prev, { id: Date.now(), name: place, z: strength,
                               inphase: d.z ?? 0, dir: d.direction,
                               ratio: d.ratio, bins: (d.on_bins ?? 0) + (d.off_bins ?? 0) }]
      const top = [...next].sort((a, b) => b.z - a.z)[0]
      if (next.length > 1 && top.id === next[next.length - 1].id) chime()
      return next
    })
  }

  const wait = (ms) => new Promise((r) => setTimeout(r, ms))
  const ranked = [...spots].sort((a, b) => b.z - a.z)
  const leader = ranked[0]
  const runner = ranked[1]
  // Compare spots by RATIO, not by difference.
  //
  // The score is a |t| statistic whose floor is about 1.0 — that is what a spot
  // with nothing watching reads, and it is a property of the statistic, not a
  // threshold anyone chose. So the real find measured 2.2 against 1.0 elsewhere:
  // more than double, but a difference of only 1.2, which failed the old
  // `> 1.5` test and displayed as "close, measure again". The instrument was
  // right and the wording undersold it. Near a floor of 1, the multiple is the
  // meaningful quantity.
  const FLOOR = 1.0
  const clear = leader && runner ? leader.z / Math.max(runner.z, 0.5) : 0
  const flash = mode === 'flash'
  const lit = flash && beamOn
  const busy = phase !== 'idle'

  return (
    <div className="spot" style={{ background: lit ? '#fff' : '#07070a',
                                   color: lit ? '#111' : '#e8e8ea' }}>
      <div className="spot-inner wide">
        {busy ? (
          <>
            <div className="spot-score">{left}</div>
            <div className="spot-label">
              {phase === 'settle' ? 'hold still…' : `measuring ${place}`}
            </div>
            <div className="spot-hint">Keep the beam on the same spot until this reaches zero.</div>
            <div className="spot-meta">
              {live?.lock_z != null
                ? `strength now ${live.lock_z.toFixed(1)}`
                  + (live.direction ? ` · uplink ${live.direction}` : '')
                : 'waiting for packets'}
            </div>
          </>
        ) : (
          <>
            <div className="spot-label">Point at a spot, then measure it</div>
            <div className="chips">
              {PLACES.map((n) => (
                <button key={n} className={'chip' + (n === place ? ' on' : '')}
                        onClick={() => setPlace(n)}>{n}</button>
              ))}
            </div>
            <div className="spot-meta">
              {live?.lock_z != null
                ? `strength now ${live.lock_z.toFixed(1)}`
                  + (live.direction ? ` · uplink ${live.direction}` : '')
                : 'waiting for packets'}
            </div>
            <button className="big primary wide-btn" onClick={measure}>
              Measure “{place}” ({SETTLE_S + DWELL_S}s)
            </button>
          </>
        )}

        {spots.length > 0 && (
          <div className="board">
            <div className="board-head">
              {spots.length === 1
                ? 'Measure another spot to compare — one alone means nothing'
                : clear >= 1.8
                  ? <><strong>{leader.name}</strong> responds{' '}
                      <strong>{clear.toFixed(1)}x</strong> as strongly as the next
                      spot — search here</>
                  : clear >= 1.35
                    ? <>Leading: <strong>{leader.name}</strong>, {clear.toFixed(1)}x
                        the next spot — measure again to confirm</>
                    : <>Too close to call — {leader.name} and {runner.name} are
                        within {clear.toFixed(1)}x. Measure both again</>}
            </div>
            {ranked.map((s, i) => {
              // Anchored at the floor, so a spot with nothing there is visibly
              // empty. Scaling from zero made 2.2 and 1.0 read as 37% and 17%
              // — nearly the same bar for a doubled response.
              const w = Math.max(2, Math.min(100, ((s.z - FLOOR) / 3) * 100))
              return (
                <div key={s.id} className={'row' + (i === 0 && spots.length > 1 ? ' lead' : '')}>
                  <span className="rname">{i + 1}. {s.name}
                    {s.dir && <em className="fine"> · {s.dir}</em>}</span>
                  <span className="rbar"><i style={{ width: w + '%' }} /></span>
                  <span className="rz">{s.z.toFixed(1)}</span>
                </div>
              )
            })}
            <div className="fine board-foot">
              Score is the strength of the lock at its best phase, with the
              direction discarded — a torch in a lens saturates the sensor and
              the uplink <em>falls</em>, which is a hit, not a miss. It ranks
              spots against each other; it is not a p-value, and only the 90 s
              scan makes that claim. A spot with nothing watching reads about
              1.0, so the bar shows how far above that each spot reached.
              {leader && runner && clear < 1.35 &&
                ' Re-measure the top two — repeats sharpen the ranking.'}
            </div>
          </div>
        )}

        <div className="spot-foot">
          {mode === 'torch'
            ? `phone torch · ${beamStats.confirmed} flashes`
              + (beamStats.failed ? ` · ${beamStats.failed} REFUSED` : '')
            : `screen flash (weaker)${why ? ` — ${why}` : ''}`}
          {live?.data_rate != null && ` · ${live.data_rate} bins/s`}
          {' · '}<button className="linky" onClick={onClose}>done</button>
        </div>
      </div>
      {err && <div className="err spot-err">{err}</div>}
    </div>
  )
}
