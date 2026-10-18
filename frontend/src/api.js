// api.js — fetch wrappers for the localhost FastAPI backend.  (PRD §10)
//
// Every response is {ok:true,data} or {ok:false,error}. call() unwraps that so
// screens deal in data or a thrown Error, never in envelopes.

// Dev: Vite proxies /api -> 127.0.0.1:8000.
// Built: the backend itself serves this bundle, so the API is same-origin.
// Never an absolute http://127.0.0.1:8000 here — that is cross-origin, and the
// backend has no CORS by design (PRD §10), so every call would fail silently.
const BASE = import.meta.env.DEV ? '/api' : ''

async function call(path, { method = 'GET', body, timeout = 120000 } = {}) {
  const ctl = new AbortController()
  const timer = setTimeout(() => ctl.abort(), timeout)
  try {
    const res = await fetch(BASE + path, {
      method,
      signal: ctl.signal,
      headers: body ? { 'content-type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    })
    const json = await res.json().catch(() => null)
    if (!json) throw new Error(`${res.status} ${res.statusText}`)
    if (!json.ok) throw new Error(json.error || `${res.status}`)
    return json.data
  } catch (e) {
    if (e.name === 'AbortError') throw new Error('backend did not answer — is it running?')
    throw e
  } finally {
    clearTimeout(timer)
  }
}

export const health = () => call('/health', { timeout: 20000 })
export const discover = (classify = true) =>
  call('/discover', { method: 'POST', body: { classify }, timeout: 120000 })
export const scanStart = (mac, channel, direction = 'auto') =>
  call('/scan/start', { method: 'POST', body: { mac, channel, direction } })
export const scanGet = (id) => call(`/scan/${id}`, { timeout: 20000 })
// Report when each chip was ACTUALLY painted, so the detector aligns to what
// the camera saw rather than to the schedule we intended.
export const scanMarks = (id, marks, clock_skew_ms = 0, stimulus = null) =>
  call(`/scan/${id}`, { method: 'POST',
                        body: { marks, clock_skew_ms, stimulus }, timeout: 20000 })
export const localise = (mac, seconds, channel) =>
  call(`/localise/${mac}?seconds=${seconds}&channel=${channel}`, { timeout: 90000 })
export const bearing = (mac, seconds, channel, headings) =>
  call(`/localise/${mac}`, { method: 'POST', body: { seconds, channel, headings }, timeout: 90000 })
export const makeEvidence = (id, extra = {}) =>
  call(`/evidence/${id}`, { method: 'POST', body: extra })
export const replay = (capture) =>
  call('/replay', { method: 'POST', body: { capture } })
export const captures = () => call('/captures')
export const evidencePdfUrl = (id) => `${BASE}/evidence/${id}`

// Live RSSI. An EventSource, not polling: each poll would reopen the serial
// port, which resets the board and drops the channel lock.
// Optical spotlight. The page strobes the torch on the schedule this stream
// hands back, and the backend scores the camera's uplink against that phase.
export function probeStream(mac, channel, opts, onData, onError) {
  const q = new URLSearchParams({ channel, ...opts }).toString()
  const es = new EventSource(`${BASE}/probe/${mac}/stream?${q}`)
  es.onmessage = (e) => { try { onData(JSON.parse(e.data)) } catch {} }
  es.onerror = () => { onError?.('probe stream dropped'); es.close() }
  return () => es.close()
}

export function rssiStream(mac, channel, onData, onError) {
  const es = new EventSource(`${BASE}/localise/${mac}/stream?channel=${channel}&hz=8`)
  es.onmessage = (e) => { try { onData(JSON.parse(e.data)) } catch {} }
  es.onerror = () => { onError?.('signal stream dropped'); es.close() }
  return () => es.close()
}
