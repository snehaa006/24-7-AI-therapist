import { useEffect, useRef, useState } from 'react';
import LiquidOrb from './LiquidOrb.jsx';
import SwipeToStart from './SwipeToStart.jsx';
import VoiceSettings from './VoiceSettings.jsx';
import { useSettings } from './settings.js';
import { AudioEngine } from './audioEngine.js';
import { voiceSupported } from './voice.js';
import { FIXED_MODE, useTherapySession } from './useTherapySession.js';

const BG = '#040817'; // must match --bg in styles.css (the orb canvas paints it)

const PHASE_LABEL = {
  idle: 'Paused',
  speaking: 'Speaking',
  listening: 'Listening',
  thinking: 'Thinking',
};

function useElapsed(running) {
  const [secs, setSecs] = useState(0);
  useEffect(() => {
    if (!running) return undefined;
    setSecs(0);
    const t0 = Date.now();
    const id = setInterval(() => setSecs(Math.floor((Date.now() - t0) / 1000)), 1000);
    return () => clearInterval(id);
  }, [running]);
  return `${String(Math.floor(secs / 60)).padStart(2, '0')}:${String(secs % 60).padStart(2, '0')}`;
}

function Backdrop() {
  return (
    <div className="backdrop" aria-hidden="true">
      <div className="backdrop-grid" />
      <svg className="backdrop-waves" viewBox="0 0 400 800" preserveAspectRatio="xMidYMid slice">
        <defs>
          <filter id="soft" x="-20%" y="-20%" width="140%" height="140%">
            <feGaussianBlur stdDeviation="6" />
          </filter>
        </defs>
        <g fill="none" strokeLinecap="round">
          <path d="M120 70 C 220 30, 300 120, 420 80" stroke="#1f5cff" strokeWidth="18" opacity="0.35" filter="url(#soft)" />
          <path d="M120 70 C 220 30, 300 120, 420 80" stroke="#7fb0ff" strokeWidth="1.2" opacity="0.5" />
          <path d="M-20 700 C 90 640, 230 760, 420 690" stroke="#1f5cff" strokeWidth="22" opacity="0.3" filter="url(#soft)" />
          <path d="M-20 700 C 90 640, 230 760, 420 690" stroke="#7fb0ff" strokeWidth="1.2" opacity="0.45" />
          <path d="M-20 730 C 120 690, 260 780, 420 730" stroke="#3a6bff" strokeWidth="0.8" opacity="0.35" />
        </g>
      </svg>
    </div>
  );
}

function GearButton({ onClick }) {
  return (
    <button className="ghost icon" onClick={onClick} aria-label="Voice settings">
      <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
        <path
          d="M4 7h10M18 7h2M4 17h4M12 17h8M16 5v4M10 15v4"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
      </svg>
      <span>Voice</span>
    </button>
  );
}

function Landing({ onStart, onSettings }) {
  return (
    <section className="screen landing">
      <header className="hero">
        <div className="hero-top">
          <p className="eyebrow">
            <span className="dot" /> Available 24/7
          </p>
          <GearButton onClick={onSettings} />
        </div>
        <h1>
          Someone
          <br />
          to talk to.
        </h1>
        <p className="lede">Say what's on your mind, out loud. It listens, asks one gentle question at a time, and never rushes you.</p>
      </header>

      <div className="landing-foot">
        <ul className="chips">
          <li>Voice first</li>
          <li>No booking</li>
          <li>No judgement</li>
        </ul>
        {voiceSupported ? (
          <SwipeToStart label="Start Now" onStart={onStart} />
        ) : (
          <p className="notice">Voice needs Chrome (desktop or Android). Open this page in Chrome to start a session.</p>
        )}
        <p className="fineprint">
          Not a replacement for professional care. In an emergency, contact your local emergency number.
          {FIXED_MODE && ' · Test mode: fixed reply'}
        </p>
      </div>
    </section>
  );
}

function Session({ session, onEnd, onSettings }) {
  const { phase, history, interim, waiting, error, interrupt, send } = session;
  const clock = useElapsed(true);
  const [showLog, setShowLog] = useState(false);
  const [draft, setDraft] = useState('');
  const logEnd = useRef(null);

  const lastAI = [...history].reverse().find((t) => t.role === 'assistant');
  const lastUser = [...history].reverse().find((t) => t.role === 'user');
  const userLine = phase === 'listening' ? interim : lastUser?.text;
  const userTurns = history.filter((t) => t.role === 'user').length;

  useEffect(() => logEnd.current?.scrollIntoView({ block: 'end' }), [history, showLog]);

  return (
    <section className="screen session">
      <header className="session-bar">
        <div className="session-meta">
          <span className="live" data-phase={phase} />
          <span>Session</span>
          <span className="clock">{clock}</span>
        </div>
        <div className="session-buttons">
          <GearButton onClick={onSettings} />
          <button className="ghost" onClick={onEnd}>
            End
          </button>
        </div>
      </header>

      <div className="captions" aria-live="polite">
        <p className="phase" data-phase={phase} data-waiting={waiting}>
          {phase === 'listening' && waiting ? 'Take your time' : PHASE_LABEL[phase]}
          {phase === 'thinking' && <span className="ellipsis" />}
        </p>
        {!showLog && (
          <>
            <p className="ai-line">{lastAI?.text}</p>
            <p className="user-line">{userLine || (phase === 'listening' ? 'Go ahead, I’m listening…' : '')}</p>
          </>
        )}
        {error && <p className="error">{error}</p>}
      </div>

      <footer className="session-foot">
        <div className="session-actions">
          <button className="ghost" onClick={() => setShowLog((v) => !v)} aria-expanded={showLog}>
            {showLog ? 'Hide transcript' : `Transcript · ${userTurns}`}
          </button>
          {phase === 'speaking' && (
            <button className="ghost" onClick={interrupt}>
              Let me speak
            </button>
          )}
        </div>

        {showLog && (
          <ol className="log">
            {history.map((t, i) => (
              <li key={i} data-role={t.role}>
                <span>{t.role === 'user' ? 'You' : 'AI'}</span>
                {t.text}
              </li>
            ))}
            <li ref={logEnd} className="log-end" />
          </ol>
        )}

        <form
          className="type-box"
          onSubmit={(e) => {
            e.preventDefault();
            send(draft);
            setDraft('');
          }}
        >
          <input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="Or type instead…" aria-label="Type a message" />
          <button type="submit" disabled={!draft.trim() || phase === 'thinking'} aria-label="Send">
            <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
              <path d="M5 12h13M13 6l6 6-6 6" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        </form>
      </footer>
    </section>
  );
}

export default function App() {
  const engineRef = useRef(null);
  if (!engineRef.current) engineRef.current = new AudioEngine();
  const engine = engineRef.current;

  const [settings, updateSettings] = useSettings();
  const session = useTherapySession(engine, settings);
  const [inSession, setInSession] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const openSettings = () => setShowSettings(true);

  const start = () => {
    session.start(); // must run inside the gesture so speech output is allowed
    setInSession(true);
  };
  const end = () => {
    session.end();
    setInSession(false);
  };

  return (
    <main className="stage">
      <LiquidOrb engine={engine} background={BG} distance={inSession ? 9 : 10.5} lift={inSession ? 0.08 : 0.02} />
      <Backdrop />
      {inSession ? (
        <Session session={session} onEnd={end} onSettings={openSettings} />
      ) : (
        <Landing onStart={start} onSettings={openSettings} />
      )}
      {showSettings && (
        <VoiceSettings settings={settings} update={updateSettings} onClose={() => setShowSettings(false)} canPreview={!inSession} />
      )}
    </main>
  );
}
