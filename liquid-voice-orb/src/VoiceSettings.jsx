import { useEffect, useRef, useState } from 'react';
import { NATURAL_VOICES, PACING, SPEECH_LANGS, listDeviceVoices, speak, stopSpeaking } from './voice.js';

const PREVIEW = "Hi, I'm here with you. Take all the time you need.";

const PACING_HINT = {
  quick: 'Replies soon after you pause.',
  natural: 'Waits through short pauses and unfinished sentences.',
  patient: 'Gives you plenty of time to find your words.',
};

function Segmented({ value, options, onChange, label }) {
  return (
    <div className="segmented" role="radiogroup" aria-label={label}>
      {options.map(([id, text]) => (
        <button key={id} type="button" role="radio" aria-checked={value === id} onClick={() => onChange(id)}>
          {text}
        </button>
      ))}
    </div>
  );
}

/** Bottom sheet for voice and pacing. `canPreview` is off mid-session so the mic doesn't hear the sample. */
export default function VoiceSettings({ settings, update, onClose, canPreview }) {
  const [deviceVoices, setDeviceVoices] = useState([]);
  const [previewing, setPreviewing] = useState(false);
  const [note, setNote] = useState('');
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const previewingRef = useRef(false);
  previewingRef.current = previewing;

  useEffect(() => {
    listDeviceVoices().then(setDeviceVoices);
    const onKey = (e) => e.key === 'Escape' && closeRef.current();
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      if (previewingRef.current) stopSpeaking();
    };
  }, []);

  const preview = async () => {
    if (previewing) {
      stopSpeaking();
      return;
    }
    setNote('');
    setPreviewing('loading');
    await speak(PREVIEW, settings, {
      onStart: () => setPreviewing('playing'),
      onFallback: (e) => setNote(`Natural voice unavailable, so this is the device voice. (${e.message})`),
    });
    setPreviewing(false);
  };

  const natural = settings.source === 'natural';

  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="sheet" role="dialog" aria-modal="true" aria-label="Voice settings" onClick={(e) => e.stopPropagation()}>
        <header className="sheet-head">
          <h2>Voice &amp; listening</h2>
          <button className="ghost" onClick={onClose}>
            Done
          </button>
        </header>

        <section>
          <h3>Voice type</h3>
          <Segmented
            label="Voice type"
            value={settings.source}
            onChange={(source) => update({ source })}
            options={[
              ['natural', 'Natural voices'],
              ['device', 'Device voices'],
            ]}
          />
          <p className="hint">
            {natural
              ? 'Lifelike Gemini voices. Each new line takes a few seconds to generate (replies start after the first sentence is ready); anything heard before replays instantly. Uses your Gemini quota.'
              : 'Voices built into this device. Instant, works offline, sounds more robotic.'}
          </p>
        </section>

        <section>
          <h3>Voice</h3>
          {natural ? (
            <div className="voice-grid" role="radiogroup" aria-label="Natural voice">
              {NATURAL_VOICES.map((v) => (
                <button
                  key={v.id}
                  type="button"
                  role="radio"
                  aria-checked={settings.naturalVoice === v.id}
                  onClick={() => update({ naturalVoice: v.id })}
                >
                  <span>{v.id}</span>
                  <small>{v.note}</small>
                </button>
              ))}
            </div>
          ) : (
            <select value={settings.voiceURI} onChange={(e) => update({ voiceURI: e.target.value })} aria-label="Device voice">
              <option value="">Automatic</option>
              {deviceVoices.map((v) => (
                <option key={v.voiceURI} value={v.voiceURI}>
                  {v.name} ({v.lang})
                </option>
              ))}
            </select>
          )}
        </section>

        <section className="sliders">
          <label>
            <span>
              Speed <output>{settings.rate.toFixed(2)}×</output>
            </span>
            <input type="range" min="0.75" max="1.25" step="0.05" value={settings.rate} onChange={(e) => update({ rate: Number(e.target.value) })} />
          </label>
          {!natural && (
            <label>
              <span>
                Pitch <output>{settings.pitch.toFixed(2)}</output>
              </span>
              <input type="range" min="0.75" max="1.25" step="0.05" value={settings.pitch} onChange={(e) => update({ pitch: Number(e.target.value) })} />
            </label>
          )}
        </section>

        <section>
          <h3>When to reply</h3>
          <Segmented
            label="Pacing"
            value={settings.pacing}
            onChange={(pacing) => update({ pacing })}
            options={Object.entries(PACING).map(([id, p]) => [id, p.label])}
          />
          <p className="hint">{PACING_HINT[settings.pacing]}</p>
        </section>

        <section>
          <h3>Your accent</h3>
          <select value={settings.speechLang} onChange={(e) => update({ speechLang: e.target.value })} aria-label="Speech recognition language">
            {SPEECH_LANGS.map((l) => (
              <option key={l.id} value={l.id}>
                {l.id ? l.label : `${l.label} (${navigator.language || 'en-US'})`}
              </option>
            ))}
          </select>
          <p className="hint">Pick the English closest to how you speak, so you're understood better.</p>
        </section>

        {canPreview ? (
          <button className="preview" onClick={preview}>
            {{ loading: 'Generating voice… (tap to cancel)', playing: 'Stop preview' }[previewing] || 'Preview voice'}
          </button>
        ) : (
          <p className="hint">Changes apply from the next reply.</p>
        )}
        {note && <p className="hint warn">{note}</p>}
      </div>
    </div>
  );
}
