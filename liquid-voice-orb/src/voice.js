// ─────────────────────────────────────────────────────────────────────────────
// Voice I/O. Speech-to-text: browser Web Speech API (works best in Chrome).
// Text-to-speech: either the device's built-in voices (SpeechSynthesis) or the
// natural Gemini voices served by the backend (/api/speak).
// ─────────────────────────────────────────────────────────────────────────────

const SR = typeof window !== 'undefined' && (window.SpeechRecognition || window.webkitSpeechRecognition);

export const voiceSupported = Boolean(SR) && typeof window !== 'undefined' && 'speechSynthesis' in window;

/**
 * How long to wait through silence (ms).
 *   check    – pause after which we ask "has the user finished their thought?"
 *   trailing – pause to wait when the words so far end on "and", "because", "um"…
 *   max      – longest silence before the turn ends no matter what
 */
export const PACING = {
  quick: { label: 'Quick', check: 900, trailing: 2200, max: 3500 },
  natural: { label: 'Natural', check: 1300, trailing: 3000, max: 6000 },
  patient: { label: 'Patient', check: 2000, trailing: 4500, max: 9000 },
};

// Endings that almost always mean the person is still forming the sentence.
const TRAILING =
  /\b(and|but|or|so|because|cause|cuz|like|um+|uh+|erm?|hmm+|then|the|a|an|to|of|for|with|about|my|i|i'm|im|i was|i just|that|which|if|when|while|just|really|actually|maybe|kind of|sort of|you know|i mean|i think|i feel|is|was|are|were)$/i;

const words = (t) => (t ? t.split(/\s+/).filter(Boolean).length : 0);

/**
 * Listens for one user turn and decides when it is over, the way a person would:
 * short pauses are allowed, trailing words buy more time, and an `isComplete(text)`
 * check (Gemini) decides whether the thought sounds finished.
 *
 * After a turn is handed off with onTurn, the recogniser keeps running until stop()
 * (called when the reply starts playing). If the user carries on talking before then,
 * onResume fires and the same turn continues — nothing they said is lost.
 */
export class TurnListener {
  constructor({ onInterim, onTurn, onResume, onWaiting, onError, isComplete }) {
    Object.assign(this, { onInterim, onTurn, onResume, onWaiting, onError, isComplete });
    this.pacing = PACING.natural;
    this._rec = null;
    this._active = false;
    this._committed = '';
    this._pending = '';
    this._sentWords = -1; // >= 0 while a finished turn is waiting for its reply
    this._gen = 0; // bumps on every new bit of speech so stale checks are ignored
    this._timer = 0;
    this._maxTimer = 0;
  }

  setPacing(name) {
    this.pacing = PACING[name] || PACING.natural;
  }

  start() {
    this.stop();
    this._active = true;
    this._committed = '';
    this._pending = '';
    this._sentWords = -1;
    this._open();
  }

  stop() {
    this._active = false;
    this._sentWords = -1;
    this._gen++;
    this._clearTimers();
    if (this._rec) {
      this._rec.onresult = this._rec.onend = this._rec.onerror = null;
      try {
        this._rec.abort();
      } catch {
        /* not running */
      }
      this._rec = null;
    }
  }

  _clearTimers() {
    clearTimeout(this._timer);
    clearTimeout(this._maxTimer);
  }

  _text() {
    return `${this._committed} ${this._pending}`.replace(/\s+/g, ' ').trim();
  }

  _heard() {
    const text = this._text();
    if (!text) return;
    this._gen++;

    if (this._sentWords >= 0) {
      if (words(text) <= this._sentWords) return; // same words re-finalised, nothing new
      this._sentWords = -1;
      this.onResume?.(); // they kept talking: cancel the pending reply
    }

    this.onInterim?.(text);
    this.onWaiting?.(false);
    this._clearTimers();
    const p = this.pacing;
    const trailing = TRAILING.test(text);
    this._timer = setTimeout(() => this._decide(), trailing ? p.trailing : p.check);
    this._maxTimer = setTimeout(() => this._finish(), p.max);
  }

  async _decide() {
    const gen = this._gen;
    const text = this._text();
    if (!this._active || this._sentWords >= 0 || !text) return;

    if (TRAILING.test(text)) {
      this.onWaiting?.(true); // still mid-sentence: hold until they go on or max silence
      return;
    }

    let complete = true;
    if (this.isComplete) {
      const timeout = new Promise((r) => setTimeout(() => r(true), 1800));
      complete = await Promise.race([this.isComplete(text).catch(() => true), timeout]);
    }
    if (gen !== this._gen) return; // they spoke again while we were checking
    if (complete) this._finish();
    else this.onWaiting?.(true);
  }

  _finish() {
    const text = this._text();
    if (!this._active || this._sentWords >= 0 || !text) return;
    this._clearTimers();
    this._gen++;
    this._sentWords = words(text);
    this.onWaiting?.(false);
    this.onTurn?.(text);
  }

  _open() {
    const rec = new SR();
    rec.lang = navigator.language || 'en-US';
    rec.continuous = true;
    rec.interimResults = true;
    rec.maxAlternatives = 1;

    rec.onresult = (e) => {
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) this._committed += ` ${r[0].transcript}`;
        else interim += r[0].transcript;
      }
      this._pending = interim;
      this._heard();
    };

    rec.onerror = (e) => {
      if (e.error === 'no-speech' || e.error === 'aborted') return; // onend restarts
      const fatal = {
        'not-allowed': 'Microphone access was blocked. Allow it for this site and start again.',
        'service-not-allowed': 'Microphone access was blocked. Allow it for this site and start again.',
        network: 'Speech recognition needs an internet connection (Chrome sends audio to Google to transcribe it).',
        'audio-capture': 'No microphone was found.',
      }[e.error];
      if (fatal) {
        this.stop();
        this.onError?.(fatal);
      }
    };

    rec.onend = () => {
      if (!this._active) return;
      // Keep anything heard as an interim result before the recogniser stopped.
      if (this._pending) {
        this._committed += ` ${this._pending}`;
        this._pending = '';
      }
      setTimeout(() => this._active && this._rec === rec && this._open(), 120);
    };

    this._rec = rec;
    try {
      rec.start();
    } catch {
      /* already started */
    }
  }
}

// ── Device voices (SpeechSynthesis) ──────────────────────────────────────────

let voicesPromise = null;

/** All voices the device offers, sorted so the user's language comes first. */
export function listDeviceVoices() {
  if (!voicesPromise) {
    voicesPromise = new Promise((resolve) => {
      const done = () => {
        const voices = window.speechSynthesis.getVoices();
        if (!voices.length) return false;
        const lang = (navigator.language || 'en-US').slice(0, 2);
        resolve(
          [...voices].sort((a, b) => Number(b.lang.startsWith(lang)) - Number(a.lang.startsWith(lang)) || a.name.localeCompare(b.name))
        );
        return true;
      };
      if (done()) return;
      window.speechSynthesis.addEventListener('voiceschanged', done, { once: true });
      setTimeout(() => resolve(window.speechSynthesis.getVoices()), 1500);
    });
  }
  return voicesPromise;
}

async function pickDeviceVoice(voiceURI) {
  const voices = await listDeviceVoices();
  if (!voices.length) return null;
  const chosen = voices.find((v) => v.voiceURI === voiceURI);
  if (chosen) return chosen;
  const lang = (navigator.language || 'en-US').slice(0, 2);
  const preferred = ['Google UK English Female', 'Google US English', 'Samantha', 'Microsoft Aria', 'Microsoft Jenny'];
  const local = voices.filter((v) => v.lang?.startsWith(lang));
  return preferred.map((n) => local.find((v) => v.name.includes(n))).find(Boolean) || local[0] || voices[0];
}

// Chrome cuts off single utterances after ~15 s, so speak sentence by sentence.
function sentences(text) {
  return text.match(/[^.!?]+[.!?]*\s*/g)?.map((s) => s.trim()).filter(Boolean) ?? [text];
}

let current = null; // the in-flight speech; replaced or cleared to cancel it

async function speakDevice(text, { voiceURI, rate = 1, pitch = 1 }, token, onStart) {
  const synth = window.speechSynthesis;
  const voice = await pickDeviceVoice(voiceURI);
  let started = false;
  for (const part of sentences(text)) {
    if (current !== token) return;
    await new Promise((resolve) => {
      const u = new SpeechSynthesisUtterance(part);
      if (voice) u.voice = voice;
      u.rate = rate;
      u.pitch = pitch;
      // Fallback in case Chrome never fires onend.
      const guard = setTimeout(resolve, 2500 + (part.split(/\s+/).length * 450) / rate);
      u.onend = u.onerror = () => {
        clearTimeout(guard);
        resolve();
      };
      token.utterance = u; // keeps it referenced so Chrome doesn't drop its onend
      synth.speak(u);
      if (!started) {
        started = true;
        onStart?.(null);
      }
    });
  }
}

// ── Natural voices (Gemini TTS via the backend) ──────────────────────────────

export const NATURAL_VOICES = [
  { id: 'Sulafat', note: 'Warm' },
  { id: 'Achernar', note: 'Soft' },
  { id: 'Vindemiatrix', note: 'Gentle' },
  { id: 'Enceladus', note: 'Breathy' },
  { id: 'Algieba', note: 'Smooth' },
  { id: 'Despina', note: 'Smooth' },
  { id: 'Achird', note: 'Friendly' },
  { id: 'Schedar', note: 'Even' },
  { id: 'Gacrux', note: 'Mature' },
  { id: 'Iapetus', note: 'Clear' },
  { id: 'Kore', note: 'Firm' },
  { id: 'Charon', note: 'Informative' },
  { id: 'Aoede', note: 'Breezy' },
  { id: 'Leda', note: 'Youthful' },
  { id: 'Puck', note: 'Upbeat' },
  { id: 'Zephyr', note: 'Bright' },
];

// Audio already generated in this tab, keyed by voice + text (previews, greeting, retries).
const audioCache = new Map();

function fetchVoice(text, voice) {
  const key = `${voice}|${text}`;
  if (!audioCache.has(key)) {
    const p = fetch('/api/speak', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, voice }),
    }).then(async (res) => {
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail || `Voice error ${res.status}`);
      }
      return res.blob();
    });
    p.catch(() => audioCache.delete(key)); // don't cache failures
    audioCache.set(key, p);
    if (audioCache.size > 40) audioCache.delete(audioCache.keys().next().value);
  }
  return audioCache.get(key);
}

// Gemini generates the whole clip before sending it, and longer clips take longer.
// So the first sentence goes on its own (short → starts playing sooner) while the
// rest is generated in parallel and is usually ready by the time it's needed.
function voiceChunks(text) {
  const parts = sentences(text);
  let first = '';
  while (parts.length && first.length < 15) first = `${first} ${parts.shift()}`.trim();
  return parts.length ? [first, parts.join(' ')] : [first];
}

async function playBlob(blob, rate, token, onStart) {
  const url = URL.createObjectURL(blob);
  const el = new Audio(url);
  el.playbackRate = rate;
  el.preservesPitch = true;
  token.audio = el;
  try {
    await new Promise((resolve, reject) => {
      el.onended = el.onpause = resolve;
      el.onerror = () => reject(new Error('Could not play the voice audio.'));
      onStart?.(el);
      el.play().catch(reject);
    });
  } finally {
    URL.revokeObjectURL(url);
  }
}

async function speakNatural(text, { naturalVoice, rate = 1 }, token, onStart) {
  const chunks = voiceChunks(text);
  const clips = chunks.map((c) => fetchVoice(c, naturalVoice)); // all requested at once
  clips.forEach((p) => p.catch(() => {})); // handled below, in order
  for (let i = 0; i < chunks.length; i++) {
    token.remaining = chunks.slice(i).join(' '); // what the device voice should say if this fails
    const blob = await clips[i];
    if (current !== token) return;
    await playBlob(blob, rate, token, onStart);
    if (current !== token) return;
  }
}

/**
 * Speak `text` with the chosen voice settings; resolves when finished or cancelled.
 * `onStart(audioEl|null)` fires when sound begins, and again for each natural-voice clip
 * (audioEl is set for natural voices, so it can drive the orb). Natural voices fall back to the device voice on failure,
 * calling `onFallback(error)`.
 */
export async function speak(text, settings = {}, { onStart, onFallback } = {}) {
  stopSpeaking();
  const token = {};
  current = token;
  try {
    if (settings.source === 'natural') {
      try {
        await speakNatural(text, settings, token, onStart);
        return;
      } catch (e) {
        if (current !== token) return;
        onFallback?.(e);
        await speakDevice(token.remaining || text, settings, token, onStart);
        return;
      }
    }
    await speakDevice(text, settings, token, onStart);
  } finally {
    if (current === token) current = null;
  }
}

/** Start generating the natural-voice audio for `text` now, so it plays at once when spoken later. */
export function prefetchSpeech(text, settings = {}) {
  if (settings.source !== 'natural' || !text) return;
  voiceChunks(text).forEach((c) => fetchVoice(c, settings.naturalVoice).catch(() => {}));
}

export function stopSpeaking() {
  const token = current;
  current = null;
  if (token?.audio) token.audio.pause();
  window.speechSynthesis?.cancel();
}
