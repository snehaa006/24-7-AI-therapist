// Runs in the page before the app. Replaces speech recognition and speech output so tests
// can "talk" with window.__say(text) and read what was spoken from window.__spoken.
export function installFakes() {
  window.__spoken = [];
  window.__rec = null;

  class FakeRecognition {
    start() {
      window.__rec = this;
    }
    stop() {
      this.abort();
    }
    abort() {
      if (window.__rec === this) window.__rec = null;
    }
  }
  window.SpeechRecognition = FakeRecognition;
  window.webkitSpeechRecognition = FakeRecognition;

  // One final transcript, as Chrome delivers it.
  window.__say = (text) => {
    const result = Object.assign([{ transcript: text }], { isFinal: true });
    window.__rec?.onresult?.({ resultIndex: 0, results: [result] });
  };

  class FakeUtterance {
    constructor(text) {
      this.text = text;
    }
  }
  window.SpeechSynthesisUtterance = FakeUtterance;
  const synth = {
    speak(u) {
      window.__spoken.push(u.text);
      setTimeout(() => u.onend?.(), 10);
    },
    cancel() {},
    getVoices: () => [],
    addEventListener() {},
  };
  Object.defineProperty(window, 'speechSynthesis', { value: synth, configurable: true });

  // Count beacons in storage, which outlives the page that sent them.
  const beacon = navigator.sendBeacon.bind(navigator);
  navigator.sendBeacon = (...args) => {
    localStorage.setItem('__beacons', String(Number(localStorage.getItem('__beacons')) + 1));
    return beacon(...args);
  };

  localStorage.setItem('voice-settings-v1', JSON.stringify({ source: 'device', pacing: 'quick' }));
}
