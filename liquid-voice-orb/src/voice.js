// ─────────────────────────────────────────────────────────────────────────────
// Browser voice I/O (build plan steps 1–6): Web Speech API for speech-to-text,
// SpeechSynthesis for text-to-speech. Works best in Chrome.
// ─────────────────────────────────────────────────────────────────────────────

const SR = typeof window !== 'undefined' && (window.SpeechRecognition || window.webkitSpeechRecognition);

export const voiceSupported = Boolean(SR) && typeof window !== 'undefined' && 'speechSynthesis' in window;

// How long the user can pause before their turn is considered finished.
const END_OF_TURN_MS = 1400;

/**
 * Listens for one user turn. Chrome's recogniser stops on its own after short
 * silences, so it is restarted until the user has said something and then
 * paused for END_OF_TURN_MS.
 */
export class TurnListener {
  constructor({ onInterim, onTurn, onError }) {
    this.onInterim = onInterim;
    this.onTurn = onTurn;
    this.onError = onError;
    this._rec = null;
    this._active = false;
    this._committed = '';
    this._pending = '';
    this._timer = 0;
  }

  start() {
    this.stop();
    this._active = true;
    this._committed = '';
    this._pending = '';
    this._open();
  }

  stop() {
    this._active = false;
    clearTimeout(this._timer);
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

  _text() {
    return `${this._committed} ${this._pending}`.replace(/\s+/g, ' ').trim();
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
      const text = this._text();
      this.onInterim?.(text);
      clearTimeout(this._timer);
      if (text) this._timer = setTimeout(() => this._finish(), END_OF_TURN_MS);
    };

    rec.onerror = (e) => {
      if (e.error === 'no-speech' || e.error === 'aborted') return; // onend restarts
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
        this.stop();
        this.onError?.('Microphone access was blocked. Allow it for this site and start again.');
        return;
      }
      if (e.error === 'network') {
        this.stop();
        this.onError?.('Speech recognition needs an internet connection (Chrome sends audio to Google to transcribe it).');
        return;
      }
      if (e.error === 'audio-capture') {
        this.stop();
        this.onError?.('No microphone was found.');
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

  _finish() {
    const text = this._text();
    if (!this._active || !text) return;
    this.stop();
    this.onTurn?.(text);
  }
}

// ── Text to speech ───────────────────────────────────────────────────────────

let voicePromise = null;
function pickVoice() {
  if (!voicePromise) {
    voicePromise = new Promise((resolve) => {
      const choose = () => {
        const voices = window.speechSynthesis.getVoices();
        if (!voices.length) return false;
        const lang = (navigator.language || 'en-US').slice(0, 2);
        const local = voices.filter((v) => v.lang?.startsWith(lang));
        const preferred = ['Google UK English Female', 'Google US English', 'Samantha', 'Microsoft Aria', 'Microsoft Jenny'];
        const byName = preferred.map((n) => local.find((v) => v.name.includes(n))).find(Boolean);
        resolve(byName || local[0] || voices[0]);
        return true;
      };
      if (choose()) return;
      window.speechSynthesis.addEventListener('voiceschanged', choose, { once: true });
      setTimeout(() => resolve(null), 1500);
    });
  }
  return voicePromise;
}

// Chrome cuts off single utterances after ~15 s, so speak sentence by sentence.
function sentences(text) {
  return text.match(/[^.!?]+[.!?]*\s*/g)?.map((s) => s.trim()).filter(Boolean) ?? [text];
}

let current = null; // keeps the utterance referenced so Chrome doesn't drop its onend

/** Speak `text`; resolves when finished or cancelled. */
export async function speak(text) {
  const synth = window.speechSynthesis;
  synth.cancel();
  const voice = await pickVoice();
  const token = {};
  current = token;

  for (const part of sentences(text)) {
    if (current !== token) return;
    await new Promise((resolve) => {
      const u = new SpeechSynthesisUtterance(part);
      if (voice) u.voice = voice;
      u.rate = 0.98;
      u.pitch = 1;
      // Fallback in case Chrome never fires onend.
      const guard = setTimeout(resolve, 2500 + part.split(/\s+/).length * 450);
      u.onend = u.onerror = () => {
        clearTimeout(guard);
        resolve();
      };
      token.utterance = u;
      synth.speak(u);
    });
  }
  if (current === token) current = null;
}

export function stopSpeaking() {
  current = null;
  window.speechSynthesis?.cancel();
}
