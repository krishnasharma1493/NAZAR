// App.jsx — four screens.  (PRD §11)
//
// Sweep -> Stimulus -> Verdict -> Find it. No router, no state library, no
// styling budget: presentation is worth 5 marks in Round 1 and zero in Round 2.
//
// The one presentational rule that matters: never show a verdict without the
// number behind it. A p-value in large type with the decision rule beside it is
// the whole pitch.

import React, { useEffect, useRef, useState } from 'react'
import * as api from './api.js'
import { getTorch, testFire } from './torch.js'
import Stimulus from './Stimulus.jsx'
import Spotlight from './Spotlight.jsx'
import SignalCheck from './SignalCheck.jsx'

const fmtP = (p) => (p < 0.0001 ? p.toExponential(1) : p.toFixed(4))

export default function App() {
  const [screen, setScreen] = useState('sweep')
  const [devices, setDevices] = useState([])
  const [target, setTarget] = useState(null)
  const [scan, setScan] = useState(null)
  const [result, setResult] = useState(null)
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)
  const [hw, setHw] = useState(null)

  useEffect(() => {
    api.health().then(setHw).catch((e) => setHw({ error: e.message }))
  }, [])

  async function doSweep() {
    setErr(null); setBusy(true)
    try {
      const d = await api.discover(true)
      setDevices(d.devices)
      if (d.drops && (d.drops.table_full || d.drops.serial || d.drops.late_bins))
        setErr(`capture loss detected: ${JSON.stringify(d.drops)} — see README`)
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  // Selecting a candidate starts the coded scan directly.
  //
  // The 20 s signal check used to sit here as a mandatory gate. It is a good
  // diagnostic but a bad demo step: it requires holding the torch on the lens
  // BEFORE the scan, which gives away the camera's position and undercuts the
  // whole hidden-camera premise. Both safety states it guarded are enforced
  // server side regardless — POST /scan/start refuses a camera that is not
  // streaming video (discovery.streaming_state), and a scan whose torch did not
  // fire comes back with invalid_reason set. So nothing is lost by skipping it.
  // It remains reachable per-device from the sweep table.
  async function pickTarget(dev) {
    try { await startScan(dev, 'auto') } catch { /* startScan already showed it */ }
  }

  function checkTarget(dev) { setTarget(dev); setScreen('check') }

  async function startScan(dev, direction = 'auto') {
    setErr(null); setBusy(true); setTarget(dev)
    try {
      const t0req = Date.now()
      // The check's measurement stream releases the serial port asynchronously,
      // so the first attempt can lose a race with its own teardown. One retry
      // costs a second and turns a dead-looking button into a working one.
      let s
      for (let attempt = 0; ; attempt++) {
        try { s = await api.scanStart(dev.mac, dev.channel, direction); break }
        catch (e) {
          if (attempt >= 1 || !/busy/i.test(e.message)) throw e
          await new Promise((r) => setTimeout(r, 1200))
        }
      }
      // This phone's clock minus the laptop's, corrected for half the round
      // trip. The chip timestamps we report back are on OUR clock; without
      // this the backend aligns the stimulus to the wrong instant.
      s.clock_skew_ms = s.server_ms
        ? (t0req + Date.now()) / 2 - s.server_ms
        : 0
      setScan(s); setScreen('stimulus')
    } catch (e) {
      // Do NOT navigate away. Throwing the operator back to Sweep discarded the
      // check result they had just earned and looked like the button doing
      // nothing. The caller shows the error where the button is.
      setErr(e.message); setBusy(false)
      throw e
    }
  }

  async function pollResult() {
    for (let i = 0; i < 60; i++) {
      try {
        const r = await api.scanGet(scan.scan_id)
        if (r.status === 'done') { setResult(r); setScreen('verdict'); setBusy(false); return }
      } catch (e) { setErr(e.message); setScreen('verdict'); setBusy(false); return }
      await new Promise((r) => setTimeout(r, 1000))
    }
    setErr('scan did not finish'); setScreen('verdict'); setBusy(false)
  }

  return (
    <div className="app">
      <header>
        <h1>NAZAR</h1>
        <span className="tag">Wi-Fi causality detection of concealed cameras</span>
        <span className={'hw ' + (hw?.error ? 'bad' : hw ? 'good' : '')}>
          {hw?.error ? `sniffer: ${hw.error}`
            : hw ? (hw.mode === 'replay' ? 'replay source loaded'
                    : ['sniffer ready', hw.port,
                       hw.board && `ch${hw.board.channel}`,
                       hw.board && `table ${hw.board.max_dev}`]
                        .filter(Boolean).join(' · '))
            : 'checking sniffer…'}
        </span>
      </header>

      {hw?.warnings?.length > 0 && (
        <div className="err">
          {hw.warnings.map((w, i) => <div key={i}>{w}</div>)}
        </div>
      )}

      {hw?.mode === 'replay' && (
        <div className="replaybar">
          REPLAY MODE — recorded data through the live pipeline.
          <span className="mono"> {hw.replay_source}</span>. Not a live measurement.
        </div>
      )}
      <div style={{ display: 'none' }}>
      </div>

      {err && <div className="err">{err}</div>}

      {screen === 'sweep' && (
        <Sweep devices={devices} busy={busy} onSweep={doSweep} onPick={pickTarget}
               onCheck={checkTarget} />
      )}

      {screen === 'check' && target && (
        <SignalCheck target={target}
                     onPass={(dir) => startScan(target, dir)}
                     onCancel={() => setScreen('sweep')} />
      )}

      {screen === 'stimulus' && scan && (
        <Stimulus
          sequence={scan.sequence} chipS={scan.chip_s} t0Ms={scan.t0_ms}
          repeats={scan.repeats || 1}
          onDone={async (marks, stimulus) => {
            setBusy(true)
            // Send the measured chip timestamps before polling; the backend
            // realigns t0 to the real paint time of chip 0.
            try {
              if (marks?.length)
                await api.scanMarks(scan.scan_id, marks, scan.clock_skew_ms, stimulus)
            }
            catch (e) { setErr(`could not report chip timings: ${e.message}`) }
            pollResult()
          }}
          onAbort={() => { setScreen('sweep'); setBusy(false) }}
        />
      )}

      {screen === 'verdict' && (
        <Verdict result={result} target={target} onFind={() => setScreen('find')}
                 onAgain={() => { setScreen('sweep'); setResult(null) }} />
      )}

      {screen === 'find' && (
        <FindIt target={target} scanId={result?.scan_id} lagS={result?.lag_s} />
      )}
    </div>
  )
}

function TorchCheck() {
  const [status, setStatus] = useState('checking')
  const [why, setWhy] = useState(null)
  const [firing, setFiring] = useState(false)

  useEffect(() => {
    getTorch().then(({ torch, why }) => {
      setStatus(torch ? 'ready' : 'unavailable'); setWhy(why)
    })
  }, [])

  async function fire() {
    setFiring(true)
    const r = await testFire(1800)
    setFiring(false)
    setStatus(r.ok ? 'ready' : 'unavailable')
    setWhy(r.why)
  }

  return (
    <div className={'torchbox ' + status}>
      <div className="torchrow">
        <strong>
          {status === 'checking' ? 'Checking the torch…'
            : status === 'ready' ? 'Phone torch ready — this is the stimulus'
            : 'Phone torch unavailable'}
        </strong>
        {status === 'ready' && (
          <button onClick={fire} disabled={firing}>
            {firing ? 'firing…' : 'Test torch (2 s)'}
          </button>
        )}
      </div>
      {status === 'ready' && (
        <p className="fine">Press Test torch and watch the back of the phone. If it
          does not light, the scan will not work — fix it now, not after 60 seconds.</p>
      )}
      {status === 'unavailable' && (
        <p className="fine">{why}<br />
          NAZAR will fall back to flashing this screen, which still works but is
          optically much weaker — a screen lights a room diffusely at a metre, a
          torch reaches across it. Prefer an Android phone over HTTPS.</p>
      )}
    </div>
  )
}

function Sweep({ devices, busy, onSweep, onPick, onCheck }) {
  return (
    <section>
      <p className="lead">One sweep of channels 1–13. Nothing is transmitted; the
        radio only listens.</p>
      <TorchCheck />
      <button className="big" onClick={onSweep} disabled={busy}>
        {busy ? 'sweeping…' : 'Sweep the room'}
      </button>
      {devices.length > 0 && (
        <>
          <div className="count">{devices.length} devices on air ·{' '}
            {devices.filter((d) => d.candidate).length} candidates</div>
          <table>
            <thead><tr>
              <th>MAC</th><th>ch</th><th>up kbps</th><th>ratio</th>
              <th>vendor</th><th>MAC type</th><th>P(cam)</th><th></th>
            </tr></thead>
            <tbody>
              {devices.map((d) => (
                <tr key={d.mac} className={d.candidate ? 'cand' : ''}>
                  <td className="mono">{d.mac}</td>
                  <td>{d.channel}</td>
                  <td className={d.up_kbps >= 200 ? 'streaming' : 'quiet'}>
                    {d.up_kbps.toFixed(0)}</td>
                  <td>{d.uplink_ratio}</td>
                  <td>{d.vendor || '—'}</td>
                  <td>{d.randomised ? 'randomised' : 'factory'}</td>
                  <td>{d.camera_probability == null ? '—'
                       : d.camera_probability.toFixed(2)}</td>
                  <td>
                    <button onClick={() => onPick(d)} disabled={busy}>Scan</button>
                    <button className="linky" style={{ marginLeft: 10 }}
                            onClick={() => onCheck(d)}
                            disabled={busy} title="Optional 20 s signal check — needs the torch on the lens, so it reveals the camera">
                      check
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="fine">Green uplink = actually streaming video. A camera on
            keep-alives (tens of kbps) has nothing to measure and will always read
            CLEAR — open its live view first.
            <br />P(cam) is a pre-filter trained on a handful of local devices. It
            orders the list; it never decides anything.</p>
        </>
      )}
    </section>
  )
}

function Verdict({ result, target, onFind, onAgain }) {
  if (!result) return <section><p>No result.</p>
    <button onClick={onAgain}>Back</button></section>
  const invalid = result.verdict === 'NO STIMULUS'
  const watching = result.verdict === 'WATCHING'
  return (
    <section>
      {result.mode === 'replay' && (
        <div className="replaybar">REPLAY — this verdict was computed from a
          recorded capture, not a live sweep.</div>
      )}
      <div className={'verdict ' + (invalid ? 'invalid' : watching ? 'watching' : 'clear')}>
        {result.verdict}
      </div>
      {invalid && <div className="err">{result.invalid_reason}</div>}
      <div className="pval">p = {fmtP(result.p_value)}</div>
      <p className="lead">
        {invalid
          ? <>No verdict: the optical stimulus did not run, so nothing was
             measured. This is <strong>not</strong> a finding that the device is
             clear. Check the torch on the Sweep screen and scan again.</>
          : watching
          ? <>The hypothesis that <span className="mono">{result.mac}</span> cannot
             see you is rejected at p &lt; {result.p_threshold}. Its uplink bitrate
             tracked our light pattern with a {result.lag_s.toFixed(2)} s lag.</>
          : <>No time-locked response from <span className="mono">{result.mac}</span>.
             That is not proof of absence — an offline or SD-card camera transmits
             nothing to measure.</>}
      </p>
      <Correlogram lags={result.lags_s} values={result.correlogram}
                   peakLag={result.lag_s} />
      <dl className="stats">
        <div><dt>peak</dt><dd>{result.peak.toFixed(3)}</dd></div>
        <div><dt>lag</dt><dd>{result.lag_s.toFixed(2)} s</dd></div>
        <div><dt>null</dt><dd>{result.permutations} {result.null_kind}</dd></div>
        <div><dt>rule</dt><dd>p &lt; {result.p_threshold} and 0.05s &lt; lag &lt; 2.0s</dd></div>
        <div><dt>capture</dt><dd className="mono">{result.capture_file}</dd></div>
      </dl>
      <button className="big" onClick={onFind}>Find it</button>
      <button onClick={onAgain}>Sweep again</button>
    </section>
  )
}

function Correlogram({ lags, values, peakLag }) {
  if (!values?.length) return null
  const W = 560, H = 130, pad = 24
  const lo = Math.min(0, ...values), hi = Math.max(...values)
  const x = (i) => pad + (i / (values.length - 1)) * (W - 2 * pad)
  const y = (v) => H - pad - ((v - lo) / (hi - lo || 1)) * (H - 2 * pad)
  const d = values.map((v, i) => `${i ? 'L' : 'M'}${x(i)},${y(v)}`).join(' ')
  const pi = values.indexOf(Math.max(...values))
  return (
    <svg className="corr" viewBox={`0 0 ${W} ${H}`} role="img"
         aria-label="correlation against lag">
      <line x1={pad} y1={y(0)} x2={W - pad} y2={y(0)} stroke="#444" />
      <path d={d} fill="none" stroke="#e8e8ea" strokeWidth="2" />
      <line x1={x(pi)} y1={pad - 8} x2={x(pi)} y2={H - pad} stroke="#e0483d" />
      <text x={x(pi) + 6} y={pad - 2} fill="#e0483d" fontSize="11">
        peak @ {peakLag?.toFixed(2)}s
      </text>
      <text x={pad} y={H - 6} fill="#888" fontSize="10">0 s</text>
      <text x={W - pad - 20} y={H - 6} fill="#888" fontSize="10">
        {lags[lags.length - 1]} s
      </text>
    </svg>
  )
}

function FindIt({ target, scanId, lagS }) {
  const [live, setLive] = useState(null)
  const [tracking, setTracking] = useState(false)
  const [sound, setSound] = useState(true)
  const [bearing, setBearing] = useState(null)
  const [saved, setSaved] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const [trace, setTrace] = useState([])
  const [spot, setSpot] = useState(false)
  const stopRef = useRef(null)
  const audioRef = useRef(null)
  const tickRef = useRef(null)
  const levelRef = useRef(0)

  useEffect(() => () => { stopRef.current?.(); stopAudio() }, [])

  // Geiger-style ticks: the closer you are, the faster and higher they click.
  // Audio carries small changes far better than a bar you have to watch.
  function stopAudio() {
    clearTimeout(tickRef.current); tickRef.current = null
    audioRef.current?.close?.(); audioRef.current = null
  }
  function startAudio() {
    const Ctx = window.AudioContext || window.webkitAudioContext
    if (!Ctx) return
    const ctx = new Ctx(); audioRef.current = ctx
    const loop = () => {
      const lv = levelRef.current
      const o = ctx.createOscillator(), g = ctx.createGain()
      o.frequency.value = 420 + lv * 900
      g.gain.setValueAtTime(0.0001, ctx.currentTime)
      g.gain.exponentialRampToValueAtTime(0.22, ctx.currentTime + 0.005)
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.05)
      o.connect(g); g.connect(ctx.destination)
      o.start(); o.stop(ctx.currentTime + 0.06)
      tickRef.current = setTimeout(loop, 60 + (1 - lv) * 700)
    }
    loop()
  }

  function toggle() {
    if (tracking) {
      stopRef.current?.(); stopRef.current = null; stopAudio(); setTracking(false); return
    }
    setErr(null); setTrace([]); setTracking(true)
    if (sound) startAudio()          // the button press is the gesture audio needs
    stopRef.current = api.rssiStream(target.mac, target.channel,
      (d) => {
        setLive(d)
        levelRef.current = d.level ?? 0
        if (d.rssi != null) setTrace((p) => [...p.slice(-119), d.level ?? 0])
      },
      (e) => { setErr(e); setTracking(false); stopAudio() })
  }

  async function sweepBearing() {
    setBusy(true); setErr(null)
    stopRef.current?.(); stopRef.current = null; stopAudio(); setTracking(false)
    const marks = []
    let handler = null
    try {
      if (window.DeviceOrientationEvent?.requestPermission)
        await window.DeviceOrientationEvent.requestPermission().catch(() => {})
      handler = (e) => {
        const deg = e.webkitCompassHeading ?? (e.alpha == null ? null : 360 - e.alpha)
        if (deg != null) marks.push([Date.now(), deg])
      }
      window.addEventListener('deviceorientation', handler)
      const r = await api.bearing(target.mac, 20, target.channel, marks)
      if (!marks.length)
        setErr('no compass on this device — bearing needs a phone. The live meter still works.')
      setBearing(r.bearing)
    } catch (e) { setErr(e.message) } finally {
      if (handler) window.removeEventListener('deviceorientation', handler)
      setBusy(false)
    }
  }

  async function saveEvidence() {
    setBusy(true); setErr(null)
    try { setSaved(await api.makeEvidence(scanId, { bearing })) }
    catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  const lv = live?.level ?? 0
  const stale = live?.stale_s != null && live.stale_s > 2
  const quiet = live && (live.rate_hz ?? 0) < 2
  const state = !tracking || stale ? 'steady' : (live?.state ?? 'steady')
  const fromPeak = live?.from_peak_db

  return (
    <section>
      <h2>Find it</h2>
      <p className="lead"><strong>Spotlight</strong> is the one that pinpoints.
        Sweep the phone torch around the room: light is line-of-sight, so the
        direction that raises the camera's bitrate is the direction the camera
        is in. The ESP32 stays where it is — only the phone moves.
        <br /><span className="fine">Radio proximity is available below, but
        multipath means RSSI cannot separate the vent from the plant, and it
        needs the sniffer carried with you.</span></p>
      {err && <div className="err">{err}</div>}

      {spot && (
        <Spotlight target={target} lagS={lagS ?? 0.6}
                   onClose={() => setSpot(false)} />
      )}

      <div className="ctrls">
        <button className="big primary" onClick={() => {
          stopRef.current?.(); stopRef.current = null; stopAudio(); setTracking(false)
          setSpot(true)
        }}>Spotlight — sweep the torch</button>
        <button onClick={toggle}>
          {tracking ? 'Stop' : 'RF proximity'}
        </button>
        <label className="snd">
          <input type="checkbox" checked={sound}
                 onChange={(e) => {
                   setSound(e.target.checked)
                   if (!e.target.checked) stopAudio()
                   else if (tracking) startAudio()
                 }} /> sound
        </label>
      </div>

      <div className={'meter' + (tracking ? ' on' : '')}>
        <div className="meter-read">
          <span className="dbm">{fromPeak == null ? '—' : (fromPeak >= 0 ? '0.0' : fromPeak.toFixed(1))}</span>
          <span className="dbm-unit">dB from strongest</span>
          <strong className={'state ' + state}>
            {!tracking ? '' : stale ? 'no signal' : state}
          </strong>
        </div>

        <div className="bar big-bar">
          <div className="fill" style={{ width: (lv * 100) + '%' }} />
        </div>

        <svg className="trace" viewBox="0 0 240 44" preserveAspectRatio="none">
          {trace.length > 1 && (
            <polyline fill="none" stroke="currentColor" strokeWidth="1.5"
              points={trace.map((v, i) =>
                `${(i / Math.max(trace.length - 1, 1)) * 240},${44 - v * 42}`).join(' ')} />
          )}
        </svg>

        <div className="meter-foot">
          <span>{live?.rssi == null ? '—' : `${live.rssi.toFixed(1)} dBm`}</span>
          <span className="fine">
            {!tracking ? 'walk the room while this runs — carry the sniffer'
              : quiet ? `only ${live?.rate_hz ?? 0}/s from this device — it is barely transmitting`
              : `${live?.rate_hz ?? 0} updates/s · peak ${live?.peak ?? '—'} dBm`}
          </span>
          <span>{live?.range ? `${live.range[0]}…${live.range[1]}` : ''}</span>
        </div>
      </div>

      <button onClick={sweepBearing} disabled={busy}>
        Bearing sweep (turn 360° over 20 s)
      </button>
      {bearing && (
        bearing.reliable
          ? <div className="bearing">
              <div className="arrow" style={{ transform: `rotate(${bearing.bearing_deg}deg)` }}>↑</div>
              <div>{bearing.bearing_deg}° · {bearing.arrow?.label}</div>
              <div className="fine">amplitude {bearing.amplitude_db} dB, p={bearing.p_value}</div>
            </div>
          : <div className="fine">{bearing.reason}</div>
      )}

      <hr />
      <button className="big" onClick={saveEvidence} disabled={busy || !scanId}>
        Save evidence report
      </button>
      {saved && (
        <div className="saved">
          <div>Signed and written:</div>
          <div className="mono">{saved.json_file}</div>
          <div className="mono">{saved.pdf_file}</div>
          <p className="fine">Ed25519 over the SHA-256 of the canonical record.
            Verify with <span className="mono">python backend/evidence.py --verify</span>.
            It proves the file is unaltered — not that the measurement was honest.</p>
        </div>
      )}
    </section>
  )
}
