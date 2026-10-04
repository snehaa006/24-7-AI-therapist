// Memory across sessions (step 4). The browser keeps an anonymous id; the server keeps the notes.

const KEY = 'therapist-user-id';

function newId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  // randomUUID needs a secure context (https or localhost); fall back elsewhere, e.g. a LAN address.
  const bytes = window.crypto.getRandomValues(new Uint8Array(16));
  return [...bytes].map((b, i) => ([4, 6, 8, 10].includes(i) ? '-' : '') + b.toString(16).padStart(2, '0')).join('');
}

let cached = null;

/** Anonymous id for this browser. Clearing site data starts afresh. */
export function userId() {
  if (cached) return cached;
  try {
    cached = localStorage.getItem(KEY);
    if (!cached) {
      cached = newId();
      localStorage.setItem(KEY, cached);
    }
  } catch {
    cached = newId(); // private mode: remembered for this tab only
  }
  return cached;
}

export const newSessionId = newId;

async function call(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    const data = await res.json().catch(() => null);
    throw new Error(data?.detail || `Server error ${res.status}`);
  }
  return res.json();
}

const q = () => `user_id=${encodeURIComponent(userId())}`;

export const fetchMemories = () => call(`/api/memories?${q()}`);
export const deleteMemory = (id) => call(`/api/memories/${id}?${q()}`, { method: 'DELETE' });
export const forgetAll = () => call(`/api/memories?${q()}`, { method: 'DELETE' });

/** Greeting that picks up from last time, or null for a new user (or if it takes too long). */
export async function fetchGreeting(timeoutMs = 6000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const { greeting } = await call('/api/session/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId() }),
      signal: ctrl.signal,
    });
    return greeting || null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Ask the server to pull memories out of this session. `beacon` is for tab close:
 * sendBeacon survives the page going away; otherwise a keepalive fetch.
 */
export function saveSession(sessionId, history, { beacon = false } = {}) {
  const body = JSON.stringify({ user_id: userId(), session_id: sessionId, history });
  if (beacon && navigator.sendBeacon) {
    return navigator.sendBeacon('/api/session/end', new Blob([body], { type: 'application/json' }));
  }
  fetch('/api/session/end', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, keepalive: true }).catch(() => {});
  return true;
}
