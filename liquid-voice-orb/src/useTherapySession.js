import { useCallback, useEffect, useRef, useState } from 'react';
import { TurnListener, speak, stopSpeaking } from './voice.js';
import { fetchGreeting, newSessionId, saveSession, userId } from './memory.js';

const GREETING = "Hi, I'm here, and I'm listening. What's on your mind today?";

// Step 1 check: open the app with ?mode=fixed to skip Gemini and hear this every turn.
export const FIXED_MODE = new URLSearchParams(window.location.search).get('mode') === 'fixed';
const FIXED_REPLY = "I hear you. I'm right here, and you can take your time. Tell me a little more.";

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => null);
    throw new Error(data?.detail || `Server error ${res.status}`);
  }
  return res.json();
}

/** → { reply, crisis, speech?, resources? }. On a crisis the server sends a fixed script, never AI text. */
async function fetchReply(history) {
  if (FIXED_MODE) return { reply: FIXED_REPLY, crisis: false };
  return postJSON('/api/chat', { history, user_id: userId() });
}

/**
 * Voice conversation loop: speak → listen → think → speak …
 * phase: 'idle' | 'speaking' | 'listening' | 'thinking'
 * waiting: true while the user has paused mid-thought and we're giving them time.
 */
export function useTherapySession(engine, settings) {
  const [phase, setPhase] = useState('idle');
  const [history, setHistory] = useState([]); // [{ role: 'user' | 'assistant', text }]
  const [interim, setInterim] = useState('');
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState('');
  const [crisis, setCrisis] = useState(null); // helpline details while the crisis card is showing

  const historyRef = useRef([]);
  const beforeTurnRef = useRef([]); // history before the user's pending turn (restored if they keep talking)
  const sessionRef = useRef(0); // bumps on start/end
  const turnRef = useRef(0); // bumps on every new turn, start and end, so stale async work is ignored
  const listenerRef = useRef(null);
  const settingsRef = useRef(settings);
  settingsRef.current = settings;
  const naturalFailedRef = useRef(false); // after one failure, use the device voice for the rest of the session
  const sessionIdRef = useRef(null); // set while a session is open and not yet saved to memory

  const commit = (turns) => {
    historyRef.current = turns;
    setHistory(turns);
  };

  const listen = useCallback(() => {
    setInterim('');
    setWaiting(false);
    setPhase('listening');
    engine.showMic();
    listenerRef.current.setPacing(settingsRef.current.pacing);
    listenerRef.current.start();
  }, [engine]);

  const say = useCallback(
    async (text) => {
      const turn = turnRef.current;
      const s = settingsRef.current;
      const useNatural = s.source === 'natural' && !FIXED_MODE && !naturalFailedRef.current;
      await speak(
        text,
        { ...s, source: useNatural ? 'natural' : 'device' },
        {
          onStart: (audioEl) => {
            if (turnRef.current !== turn) return;
            listenerRef.current.stop(); // reply is playing: the user's turn is settled
            setPhase('speaking');
            if (audioEl) engine.showAudio(audioEl);
            else engine.showVoice();
          },
          onFallback: (e) => {
            naturalFailedRef.current = true;
            setError(`Natural voice unavailable, using the device voice. (${e.message})`);
          },
        }
      );
      if (turnRef.current !== turn) return;
      listen();
    },
    [engine, listen]
  );

  const respond = useCallback(
    async (text) => {
      const turn = ++turnRef.current;
      const before = historyRef.current;
      beforeTurnRef.current = before;
      const withUser = [...before, { role: 'user', text }];
      commit(withUser);
      setInterim('');
      setError('');
      setPhase('thinking');
      engine.showIdle();

      let res;
      try {
        res = await fetchReply(withUser);
      } catch (e) {
        if (turnRef.current !== turn) return;
        commit(before); // drop the turn so the user can simply say it again
        setError(e.message);
        say('Sorry, I lost my connection for a moment. Could you say that again?');
        return;
      }
      if (turnRef.current !== turn) return;
      if (res.crisis) {
        // Flagged turns are kept out of memory (step 4).
        commit([...before, { role: 'user', text, crisis: true }, { role: 'assistant', text: res.reply, crisis: true }]);
        setCrisis(res.resources);
      } else {
        commit([...withUser, { role: 'assistant', text: res.reply }]);
      }
      say(res.speech || res.reply);
    },
    [engine, say]
  );

  // Listener callbacks always reach the latest closures through this ref.
  const handlers = useRef({});
  handlers.current = {
    onTurn: (text) => respond(text),
    // The user carried on talking before the reply started: drop it and keep listening.
    onResume: () => {
      turnRef.current++;
      stopSpeaking();
      commit(beforeTurnRef.current);
      setPhase('listening');
      engine.showMic();
    },
    isComplete: async (text) => {
      const lastAI = [...historyRef.current].reverse().find((t) => t.role === 'assistant');
      const { complete } = await postJSON('/api/turn', { last_assistant: lastAI?.text || '', user_text: text });
      return complete;
    },
  };
  if (!listenerRef.current) {
    listenerRef.current = new TurnListener({
      onInterim: setInterim,
      onWaiting: setWaiting,
      onTurn: (text) => handlers.current.onTurn(text),
      onResume: () => handlers.current.onResume(),
      isComplete: FIXED_MODE ? null : (text) => handlers.current.isComplete(text),
      onError: (msg) => {
        setError(msg);
        setPhase('idle');
        engine.showIdle();
      },
    });
  }

  /** Send the session to be remembered, once. `beacon` when the tab is closing. */
  const save = useCallback((beacon = false) => {
    const sid = sessionIdRef.current;
    sessionIdRef.current = null;
    if (!sid || FIXED_MODE) return;
    if (!historyRef.current.some((t) => t.role === 'user')) return;
    saveSession(sid, historyRef.current, { beacon });
  }, []);

  /** Call from a click/tap so the browser allows speech output. */
  const start = useCallback(async () => {
    const id = ++sessionRef.current;
    turnRef.current++;
    sessionIdRef.current = newSessionId();
    naturalFailedRef.current = false;
    setError('');
    setInterim('');
    setCrisis(null);
    commit([]);
    setPhase('thinking');
    // Ask for the mic while the greeting plays; the stream drives the orb while listening.
    engine.openMic().catch(() => {
      if (sessionRef.current !== id) return;
      setError('Microphone access was blocked. Allow it for this site and start again.');
    });
    // Returning users get a greeting that picks up from last time.
    const greeting = (!FIXED_MODE && (await fetchGreeting())) || GREETING;
    if (sessionRef.current !== id) return;
    commit([{ role: 'assistant', text: greeting }]);
    say(greeting);
  }, [engine, say]);

  const end = useCallback(() => {
    save();
    sessionRef.current++;
    turnRef.current++;
    listenerRef.current?.stop();
    stopSpeaking();
    engine.stop();
    setPhase('idle');
    setInterim('');
    setWaiting(false);
  }, [engine, save]);

  // Closing or leaving the tab mid-session still saves it.
  useEffect(() => {
    const onHide = () => save(true);
    window.addEventListener('pagehide', onHide);
    return () => window.removeEventListener('pagehide', onHide);
  }, [save]);

  /** Cut the AI off and go straight to listening. */
  const interrupt = useCallback(() => {
    stopSpeaking(); // speak() resolves → say() moves on to listening
  }, []);

  /** Typed fallback, handled exactly like a spoken turn. */
  const send = useCallback(
    (text) => {
      const t = text.trim();
      if (!t) return;
      listenerRef.current?.stop();
      stopSpeaking();
      respond(t);
    },
    [respond]
  );

  useEffect(() => () => end(), [end]);

  const dismissCrisis = useCallback(() => setCrisis(null), []);

  return { phase, history, interim, waiting, error, crisis, dismissCrisis, start, end, interrupt, send };
}
