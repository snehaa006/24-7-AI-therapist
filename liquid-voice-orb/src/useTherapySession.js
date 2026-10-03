import { useCallback, useEffect, useRef, useState } from 'react';
import { TurnListener, speak, stopSpeaking } from './voice.js';

const GREETING = "Hi, I'm here, and I'm listening. What's on your mind today?";

// Step 1 check: open the app with ?mode=fixed to skip Gemini and hear this every turn.
export const FIXED_MODE = new URLSearchParams(window.location.search).get('mode') === 'fixed';
const FIXED_REPLY = "I hear you. I'm right here, and you can take your time. Tell me a little more.";

async function fetchReply(history) {
  if (FIXED_MODE) return FIXED_REPLY;
  const res = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ history }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail || `Server error ${res.status}`);
  }
  return (await res.json()).reply;
}

/**
 * Voice conversation loop: speak → listen → think → speak …
 * phase: 'idle' | 'speaking' | 'listening' | 'thinking'
 */
export function useTherapySession(engine) {
  const [phase, setPhase] = useState('idle');
  const [history, setHistory] = useState([]); // [{ role: 'user' | 'assistant', text }]
  const [interim, setInterim] = useState('');
  const [error, setError] = useState('');

  const historyRef = useRef([]);
  const sessionRef = useRef(0); // bumps on start/end
  const turnRef = useRef(0); // bumps on every new turn, start and end, so stale async work is ignored
  const listenerRef = useRef(null);

  const commit = (turns) => {
    historyRef.current = turns;
    setHistory(turns);
  };

  const listen = useCallback(() => {
    setInterim('');
    setPhase('listening');
    engine.showMic();
    listenerRef.current?.start();
  }, [engine]);

  const say = useCallback(
    async (text) => {
      const turn = turnRef.current;
      setPhase('speaking');
      engine.showVoice();
      await speak(text);
      if (turnRef.current !== turn) return;
      listen();
    },
    [engine, listen]
  );

  const respond = useCallback(
    async (text) => {
      const turn = ++turnRef.current;
      const before = historyRef.current;
      const withUser = [...before, { role: 'user', text }];
      commit(withUser);
      setInterim('');
      setError('');
      setPhase('thinking');
      engine.showIdle();

      let reply;
      try {
        reply = await fetchReply(withUser);
      } catch (e) {
        if (turnRef.current !== turn) return;
        commit(before); // drop the turn so the user can simply say it again
        setError(e.message);
        say('Sorry, I lost my connection for a moment. Could you say that again?');
        return;
      }
      if (turnRef.current !== turn) return;
      commit([...withUser, { role: 'assistant', text: reply }]);
      say(reply);
    },
    [engine, say]
  );

  // Keep the listener's callbacks pointing at the latest `respond`.
  const respondRef = useRef(respond);
  respondRef.current = respond;
  if (!listenerRef.current) {
    listenerRef.current = new TurnListener({
      onInterim: setInterim,
      onTurn: (text) => respondRef.current(text),
      onError: (msg) => {
        setError(msg);
        setPhase('idle');
        engine.showIdle();
      },
    });
  }

  /** Call from a click/tap so the browser allows speech output. */
  const start = useCallback(() => {
    const id = ++sessionRef.current;
    turnRef.current++;
    setError('');
    setInterim('');
    commit([{ role: 'assistant', text: GREETING }]);
    // Ask for the mic while the greeting plays; the stream drives the orb while listening.
    engine.openMic().catch(() => {
      if (sessionRef.current !== id) return;
      setError('Microphone access was blocked. Allow it for this site and start again.');
    });
    say(GREETING);
  }, [engine, say]);

  const end = useCallback(() => {
    sessionRef.current++;
    turnRef.current++;
    listenerRef.current?.stop();
    stopSpeaking();
    engine.stop();
    setPhase('idle');
    setInterim('');
  }, [engine]);

  /** Cut the AI off and go straight to listening. */
  const interrupt = useCallback(() => {
    stopSpeaking(); // speak() resolves → say() moves on to listening
  }, []);

  /** Typed fallback, handled exactly like a spoken turn. */
  const send = useCallback((text) => {
    const t = text.trim();
    if (!t) return;
    listenerRef.current?.stop();
    stopSpeaking();
    respondRef.current(t);
  }, []);

  useEffect(() => () => end(), [end]);

  return { phase, history, interim, error, start, end, interrupt, send };
}
