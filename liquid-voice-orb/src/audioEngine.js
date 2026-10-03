// ─────────────────────────────────────────────────────────────────────────────
// AudioEngine: turns any audio source into four smoothed numbers (0..1):
//   level  – overall loudness (RMS)
//   bass   – ~60–250 Hz
//   mid    – ~250–2000 Hz
//   treble – ~2000–8000 Hz
//
// Hook your AI's voice output in with ONE of:
//   engine.attachElement(audioEl)   – an <audio>/<video> element (plays to speakers)
//   engine.attachStream(mediaStream)– e.g. a remote WebRTC track (NOT sent to speakers here)
//   engine.attachNode(audioNode)    – any Web Audio node (you route it to speakers yourself)
// Call engine.stop() when the AI stops talking, or just let the audio fall silent.
// ─────────────────────────────────────────────────────────────────────────────

const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);

// Tuning: shape each band so ordinary speech spans the full 0..1 range.
const GAIN = { level: 3.4, bass: 1.3, mid: 1.5, treble: 2.2 };
const CURVE = 1.8;
// Envelope follower speeds (higher = snappier). Fast attack, slower release feels like liquid.
const ATTACK = 28;
const RELEASE = 7;

export class AudioEngine {
  constructor() {
    this.ctx = null;
    this.analyser = null;
    this.bands = { level: 0, bass: 0, mid: 0, treble: 0 };

    this._freq = null;
    this._time = null;
    this._node = null;
    this._stream = null;
    this._audioEl = null;
    this._url = null;
    this._elementSources = new WeakMap();
    this._demo = false;
    this._demoStart = 0;
  }

  /** Create the AudioContext lazily (must happen after a user gesture). */
  ensure() {
    if (!this.ctx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      this.ctx = new AC();
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 1024;
      this.analyser.smoothingTimeConstant = 0.55;
      this._freq = new Uint8Array(this.analyser.frequencyBinCount);
      this._time = new Uint8Array(this.analyser.fftSize);
    }
    if (this.ctx.state === 'suspended') this.ctx.resume();
    return this.ctx;
  }

  // ── sources ────────────────────────────────────────────────────────────────
  attachElement(el) {
    this.ensure();
    this._detach();
    let src = this._elementSources.get(el);
    if (!src) {
      src = this.ctx.createMediaElementSource(el);
      this._elementSources.set(el, src);
    }
    src.connect(this.analyser);
    this.analyser.connect(this.ctx.destination); // keep it audible
    this._node = src;
  }

  attachStream(stream, { toSpeakers = false } = {}) {
    this.ensure();
    this._detach();
    const src = this.ctx.createMediaStreamSource(stream);
    src.connect(this.analyser);
    if (toSpeakers) this.analyser.connect(this.ctx.destination);
    this._node = src;
  }

  attachNode(node) {
    this.ensure();
    this._detach();
    node.connect(this.analyser);
    this._node = node;
  }

  // ── built-in test sources ──────────────────────────────────────────────────
  startDemo() {
    this.stop();
    this._demo = true;
    this._demoStart = performance.now();
  }

  async startMic() {
    this.stop();
    this.ensure();
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true },
    });
    this._stream = stream;
    this.attachStream(stream); // not routed to speakers → no feedback
  }

  async playFile(file, onEnded) {
    this.stop();
    this.ensure();
    const url = URL.createObjectURL(file);
    const el = new Audio(url);
    el.onended = () => {
      this.stop();
      onEnded?.();
    };
    this._audioEl = el;
    this._url = url;
    this.attachElement(el);
    await el.play();
  }

  // ── conversation modes (mic stays open between turns) ──────────────────────
  /** Ask for the mic once and keep the stream for the whole session. */
  async openMic() {
    this.ensure();
    if (this._stream) return;
    this._stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true },
    });
  }

  /** Orb follows the user's voice. */
  showMic() {
    this._demo = false;
    if (this._stream) this.attachStream(this._stream);
  }

  /** Orb pulses like speech (browser text-to-speech can't be routed through Web Audio). */
  showVoice() {
    this._detach();
    this._demo = true;
    this._demoStart = performance.now();
  }

  /** Orb rests. */
  showIdle() {
    this._demo = false;
    this._detach();
  }

  stop() {
    this._demo = false;
    this._detach();
    if (this._stream) {
      this._stream.getTracks().forEach((t) => t.stop());
      this._stream = null;
    }
    if (this._audioEl) {
      this._audioEl.onended = null;
      this._audioEl.pause();
      this._audioEl = null;
    }
    if (this._url) {
      URL.revokeObjectURL(this._url);
      this._url = null;
    }
  }

  _detach() {
    if (this._node && this.analyser) {
      try {
        this._node.disconnect(this.analyser);
      } catch {
        /* already disconnected */
      }
    }
    this._node = null;
    if (this.analyser) {
      try {
        this.analyser.disconnect();
      } catch {
        /* nothing connected */
      }
    }
  }

  // ── per-frame sampling ─────────────────────────────────────────────────────
  /** Call once per animation frame. Returns the smoothed {level,bass,mid,treble}. */
  update(dt) {
    let raw;
    if (this._demo) raw = this._demoFrame();
    else if (this.analyser) raw = this._analyse();
    else raw = { level: 0, bass: 0, mid: 0, treble: 0 };

    const up = 1 - Math.exp(-dt * ATTACK);
    const down = 1 - Math.exp(-dt * RELEASE);
    for (const k of Object.keys(this.bands)) {
      const cur = this.bands[k];
      const tgt = raw[k];
      this.bands[k] = cur + (tgt - cur) * (tgt > cur ? up : down);
    }
    return this.bands;
  }

  _analyse() {
    const a = this.analyser;
    a.getByteFrequencyData(this._freq);
    a.getByteTimeDomainData(this._time);

    let sum = 0;
    for (let i = 0; i < this._time.length; i++) {
      const v = (this._time[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / this._time.length);

    const binHz = this.ctx.sampleRate / a.fftSize;
    const avg = (lo, hi) => {
      const i0 = Math.max(1, Math.floor(lo / binHz));
      const i1 = Math.min(this._freq.length - 1, Math.ceil(hi / binHz));
      let s = 0;
      for (let i = i0; i <= i1; i++) s += this._freq[i];
      return s / ((i1 - i0 + 1) * 255);
    };
    const shape = (v, g) => clamp01(Math.pow(v, CURVE) * g);

    return {
      level: clamp01(rms * GAIN.level),
      bass: shape(avg(60, 250), GAIN.bass),
      mid: shape(avg(250, 2000), GAIN.mid),
      treble: shape(avg(2000, 8000), GAIN.treble),
    };
  }

  // Synthetic "speech": phrases with pauses, syllable pulses, occasional fricative hiss.
  _demoFrame() {
    const t = (performance.now() - this._demoStart) / 1000;
    const phrase = 0.5 + 0.5 * Math.sin(t * 0.45) + 0.3 * Math.sin(t * 1.1 + 1.3);
    const gate = Math.min(1, Math.max(0, (phrase - 0.35) / 0.3));
    const syll = Math.pow(Math.max(0, Math.sin(t * Math.PI * 2 * 3.6 + 0.8 * Math.sin(t * 1.3))), 1.5);
    const level = clamp01(gate * (0.25 + 0.75 * syll) * (0.75 + 0.25 * Math.sin(t * 7.3)));
    return {
      level,
      bass: clamp01(level * (0.65 + 0.35 * Math.sin(t * 2.1 + 1))),
      mid: clamp01(level * (0.55 + 0.45 * Math.sin(t * 5.7))),
      treble: clamp01(level * Math.max(0, Math.sin(t * 11.3) * 0.5 + 0.5) * 0.85),
    };
  }
}
