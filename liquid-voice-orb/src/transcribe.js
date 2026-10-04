// ─────────────────────────────────────────────────────────────────────────────
// Better transcripts. The browser's speech recognition shows words live and decides
// when a turn ends, but it often mishears. Each turn is also recorded from the mic,
// and when the server has a Groq key, Whisper transcribes it (/api/transcribe) and
// its text is used instead. Any problem falls back to what the browser heard.
// ─────────────────────────────────────────────────────────────────────────────

const words = (t) => (t ? t.split(/\s+/).filter(Boolean).length : 0);

/** Records the mic while the user is talking. */
export class TurnRecorder {
  constructor() {
    this.rec = null;
    this.chunks = [];
    this.mime = '';
  }

  start(stream) {
    this.stop();
    this.chunks = [];
    if (!stream || typeof MediaRecorder === 'undefined') return;
    const types = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg'];
    const mime = types.find((t) => MediaRecorder.isTypeSupported?.(t)) || '';
    try {
      const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
      rec.ondataavailable = (e) => e.data?.size && this.chunks.push(e.data);
      rec.start(250); // small chunks, so a snapshot mid-turn has nearly everything
      this.rec = rec;
      this.mime = rec.mimeType || mime;
    } catch {
      this.rec = null;
    }
  }

  stop() {
    if (this.rec && this.rec.state !== 'inactive') {
      try {
        this.rec.stop();
      } catch {
        /* already stopped */
      }
    }
    this.rec = null;
  }

  /** The audio of this turn so far, as one playable file. Recording carries on. */
  async snapshot() {
    if (this.rec?.state === 'recording') {
      try {
        this.rec.requestData();
      } catch {
        /* not recording */
      }
      await new Promise((r) => setTimeout(r, 60));
    }
    return this.chunks.length ? new Blob(this.chunks, { type: this.mime || 'audio/webm' }) : null;
  }
}

function toBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

// Whisper can invent words for near-silence ("Thank you for watching"). Its text is trusted only
// when its length is in line with what the browser heard.
function plausible(text, heard) {
  const n = words(text);
  const h = words(heard);
  return n > 0 && n >= Math.max(1, h * 0.4) && n <= h * 2.5 + 6;
}

let available = null; // null: not known yet; false: the server has no Groq key, so stop asking

/** Whisper's text for the turn recorded so far, or `heard` (the browser's text) if that's not possible. */
export async function whisperText(recorder, heard, { lang = '', prompt = '' } = {}) {
  if (available === false) return heard;
  const blob = await recorder.snapshot();
  if (!blob || blob.size < 1000) return heard;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 5000); // never hold the conversation up for long
  try {
    const res = await fetch('/api/transcribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ audio: await toBase64(blob), mime: blob.type, lang, prompt: prompt.slice(0, 600) }),
      signal: ctrl.signal,
    });
    if (res.status === 404) {
      available = false;
      return heard;
    }
    if (!res.ok) return heard;
    available = true;
    const { text } = await res.json();
    return plausible(text, heard) ? text.trim() : heard;
  } catch {
    return heard;
  } finally {
    clearTimeout(timer);
  }
}
