// Stimulus.jsx — Screen 2. Torch strobe controller + timestamp log.  (F6, PRD §11)
//
// Strobes the 15-chip m-sequence at 2 s/chip (30 s) and records the instant each
// chip was ACTUALLY painted. Those marks are posted back and the detector aligns
// to them: a schedule we intended to run is not evidence of what the camera saw.
//
// TORCH vs FLASH
// The torch is the stronger stimulus and the one worth having on a phone. It
// needs `navigator.mediaDevices`, which browsers expose ONLY in a secure context
// — https:// or localhost. Over plain http:// to a laptop's LAN address the API
// is simply absent, so the component falls back to a full-screen white/black
// flash. The flash is weaker optically but needs no permissions and always
// works; it is the mode NAZAR's Gate 2 pass was measured with.
//
// The lead-in deliberately holds the LAST chip's value, so the transition into
// chip 0 is a real edge — the detector derives chip edges with wraparound.

import React, { useCallback, useEffect, useRef, useState } from 'react'

const RING = 132          // ring radius in SVG units
const CIRC = 2 * Math.PI * RING

import { getTorch, runStrobe } from './torch.js'

// Kept for callers that imported it from here.
export async function acquireTorch() { return getTorch() }

export default function Stimulus({ sequence, chipS, t0Ms, repeats = 1, onDone, onAbort }) {
  const [chip, setChip] = useState(-1)          // -1 = lead-in, length = finished
  const [mode, setMode] = useState('flash')     // 'torch' | 'flash'
  const [why, setWhy] = useState(null)          // why torch was unavailable
  const [left, setLeft] = useState(null)        // seconds remaining
  const [progress, setProgress] = useState(0)   // 0..1 through the 30 s

  const torchRef = useRef(null)
  const marksRef = useRef([])
  const rafRef = useRef(0)
  const wakeRef = useRef(null)
  const doneRef = useRef(false)
  const strobeRef = useRef(null)
  const [health, setHealth] = useState({ transitions: 0, confirmed: 0, failed: 0 })

  const periodMs = sequence.length * chipS * 1000
  const totalMs = periodMs * repeats
  const leadOn = sequence[sequence.length - 1] === 1
  const nChips = sequence.length * repeats

  const finish = useCallback(() => {
    if (doneRef.current) return
    doneRef.current = true
    const s = strobeRef.current
    s?.stop()
    // The capture travels with proof that the light actually ran. A scan whose
    // torch never switched has no modulation and must not be read as CLEAR.
    onDone?.(marksRef.current, s?.stats ?? { transitions: 0, confirmed: 0, failed: 0 })
  }, [onDone])

  // Torch + keep the screen awake. A phone that sleeps mid-scan takes the
  // stimulus with it and the capture is 30 s of nothing.
  useEffect(() => {
    let cancelled = false
    getTorch().then(({ torch, why }) => {
      if (cancelled) return
      if (torch) { torchRef.current = torch; setMode('torch') } else setWhy(why)
    })
    navigator.wakeLock?.request('screen')
      .then((s) => { if (cancelled) s.release(); else wakeRef.current = s })
      .catch(() => {})
    return () => {
      cancelled = true
      strobeRef.current?.stop()
      wakeRef.current?.release().catch(() => {})
    }
  }, [])

  // The STROBE. One timer, outside React, awaiting each torch command and
  // recording whether it actually applied. Previously this lived inside a
  // setChip updater with failures swallowed, and the light often never moved.
  useEffect(() => {
    let cancelled = false
    getTorch().then(({ torch }) => {
      if (cancelled) return
      const st = runStrobe({
        t0Ms, periodMs: chipS * 1000, torch,
        valueAt: (now) => {
          const el = now - t0Ms
          if (el < 0) return leadOn
          if (el >= totalMs) return false
          return sequence[Math.floor(el / (chipS * 1000)) % sequence.length] === 1
        },
        onChange: (on, t_ms, ok) => {
          const el = t_ms - t0Ms
          if (el >= 0 && el < totalMs) {
            marksRef.current.push({
              chip: Math.floor(el / (chipS * 1000)), value: on ? 1 : 0,
              t_ms, confirmed: ok,
              // reported so a slow torch is visible in the sidecar rather than
              // silently biasing t0 — see runStrobe
              latency_ms: st.stats.latencyMs, latency_max_ms: st.stats.latencyMaxMs,
            })
          }
          setHealth({ ...st.stats })
        },
      })
      strobeRef.current = st
    })
    return () => { cancelled = true; strobeRef.current?.stop() }
  }, [t0Ms, chipS, sequence, totalMs, leadOn])

  // The clock. One rAF loop drives the ring and the countdown ONLY.
  useEffect(() => {
    const tick = () => {
      const el = Date.now() - t0Ms
      if (el < 0) {
        setChip(-1)
        setLeft(Math.ceil(-el / 1000))
        setProgress(0)
        rafRef.current = requestAnimationFrame(tick)
        return
      }
      if (el >= totalMs) { setChip(nChips); setLeft(0); setProgress(1); finish(); return }

      const idx = Math.min(Math.floor(el / (chipS * 1000)), nChips - 1)
      setLeft(Math.ceil((totalMs - el) / 1000))
      setProgress(el / totalMs)
      setChip(idx)          // display only; the strobe timer owns the light
      rafRef.current = requestAnimationFrame(tick)
    }
    rafRef.current = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(rafRef.current)
  }, [sequence, chipS, t0Ms, totalMs, nChips, finish])

  const waiting = chip < 0
  const running = chip >= 0 && chip < nChips
  const lit = waiting ? leadOn : (running ? sequence[chip % sequence.length] === 1 : false)
  const pass = running ? Math.floor(chip / sequence.length) + 1 : 1

  // In flash mode the page IS the light source, so it goes edge-to-edge
  // white/black. In torch mode the screen stays dark and out of the way.
  const flash = mode === 'flash'
  const bg = flash ? (lit ? '#ffffff' : '#000000') : '#08080b'
  const fg = flash ? (lit ? '#1a1a1a' : '#6a6a6a') : '#e8e8ea'
  const dim = flash ? (lit ? '#666' : '#444') : '#7a7a85'

  return (
    <div className="stimulus" style={{ background: bg, color: fg }}>
      <div className="stim-inner">
        <svg className="ring" viewBox="0 0 300 300" aria-hidden="true">
          <circle cx="150" cy="150" r={RING} fill="none" stroke={dim}
                  strokeWidth="6" opacity="0.25" />
          <circle cx="150" cy="150" r={RING} fill="none" stroke={fg}
                  strokeWidth="6" strokeLinecap="round"
                  strokeDasharray={CIRC}
                  strokeDashoffset={CIRC * (1 - progress)}
                  transform="rotate(-90 150 150)" />
        </svg>

        <div className="stim-centre">
          <div className="stim-count">{left ?? ''}</div>
          <div className="stim-unit">{waiting ? 'starting' : 'seconds left'}</div>
        </div>

        <div className="stim-copy">
          <div className="stim-head">
            {waiting ? 'Hold still.' : 'Hold still. Point it at the room.'}
          </div>
          <div className="stim-sub" style={{ color: dim }}>
            {running ? `chip ${chip + 1} of ${sequence.length}` : ' '}
          </div>
        </div>

        <div className="stim-chips">
          {sequence.map((v, i) => (
            <span key={i} className={'chipdot' + (v ? ' one' : '') +
              (i === chip ? ' active' : '') + (i < chip ? ' past' : '')}
              style={{ background: fg }} />
          ))}
        </div>

        <div className="stim-note" style={{ color: dim }}>
          {mode === 'torch'
            ? `PHONE TORCH · ${health.confirmed} flashes confirmed`
              + (health.failed ? ` · ${health.failed} REFUSED` : '')
            : `SCREEN FLASH (weaker fallback)${why ? ` — ${why}` : ''}`}
          {' · '}
          <button className="linky" style={{ color: dim }}
                  onClick={() => { torchRef.current?.set(false); onAbort?.() }}>
            abort
          </button>
        </div>
      </div>
    </div>
  )
}
