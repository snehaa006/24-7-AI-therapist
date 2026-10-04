import { useEffect, useState } from 'react';

const KEY = 'voice-settings-v1';

export const DEFAULT_SETTINGS = {
  source: 'natural', // 'natural' (Gemini voices) | 'device' (built-in browser voices)
  naturalVoice: 'Sulafat',
  voiceURI: '', // device voice; '' = best available
  rate: 1,
  pitch: 1,
  pacing: 'natural', // see PACING in voice.js
  speechLang: '', // speech recognition language, e.g. 'en-IN'; '' = the browser's (SPEECH_LANGS in voice.js)
};

function load() {
  try {
    return { ...DEFAULT_SETTINGS, ...JSON.parse(localStorage.getItem(KEY) || '{}') };
  } catch {
    return DEFAULT_SETTINGS;
  }
}

/** Voice and pacing preferences, remembered in this browser. */
export function useSettings() {
  const [settings, setSettings] = useState(load);
  useEffect(() => {
    try {
      localStorage.setItem(KEY, JSON.stringify(settings));
    } catch {
      /* private mode: settings just won't persist */
    }
  }, [settings]);
  const update = (patch) => setSettings((s) => ({ ...s, ...patch }));
  return [settings, update];
}
