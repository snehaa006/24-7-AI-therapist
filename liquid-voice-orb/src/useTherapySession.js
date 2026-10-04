import { useCallback, useEffect, useRef, useState } from 'react';
import { TurnListener, prefetchSpeech, speak, stopSpeaking } from './voice.js';
import { fetchGreeting, newSessionId, saveSession, userId } from './memory.js';
import { askNotificationPermission, saveOutcome, stepSeconds } from './actions.js';

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

/**
 * → { reply, crisis, speech?, resources?, action? }. On a crisis the server sends a fixed script, never AI text.
 * `action` says where the step 5 flow is, so the server knows what this message answers.
 */
async function fetchReply(history, action) {
  if (FIXED_MODE) return { reply: FIXED_REPLY, crisis: false };
  return postJSON('/api/chat', { history, user_id: userId(), action, tz_offset: -new Date().getTimezoneOffset() });
}

const checkinLine = (r) => `It's time for ${r.action}, like we planned. Ready to do it now?`;

/**
 * Voice conversation loop: speak → listen → think → speak …
 * phase: 'idle' | 'speaking' | 'listening' | 'thinking' | 'exercise'
 * waiting: true while the user has paused mid-thought and we're giving them time.
 * exercise: the guided exercise in progress (step 5), or null.
 * onRemindersChanged: called when a reminder is set, moved or used up.
 */
export function useTherapySession(engine, settings, { onRemindersChanged } = {}) {
  const [phase, setPhase] = useState('idle');
  const [history, setHistory] = useState([]); // [{ role: 'user' | 'assistant', text }]
  const [interim, setInterim] = useState('');
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState('');
  const [crisis, setCrisis] = useState(null); // helpline details while the crisis card is showing
  const [exercise, setExercise] = useState(null); // { title, steps, secs, index, remaining, paused }

  const historyRef = useRef([]);
  const beforeTurnRef = useRef([]); // history before the user's pending turn (restored if they keep talking)
  const sessionRef = useRef(0); // bumps on start/end
  const turnRef = useRef(0); // bumps on every new turn, start and end, so stale async work is ignored
  const listenerRef = useRef(null);
  const settingsRef = useRef(settings);
  settingsRef.current = settings;
  const naturalFailedRef = useRef(false); // after one failure, use the device voice for the rest of the session
  const sessionIdRef = useRef(null); // set while a session is open and not yet saved to memory
  const phaseRef = useRef('idle');
  const interimRef = useRef('');
  const remindersChangedRef = useRef(onRemindersChanged);
  remindersChangedRef.current = onRemindersChanged;

  // Step 5 flow. At most one suggestion per session, never after a crisis. Asking for one by name always works.
  const offerRef = useRef(null); // { mode: 'offer' | 'checkin', action, action_type?, reminder_id? } while waiting for yes/later/no
  const feedbackRef = useRef(null); // { action, action_type, reminder_id, done } while waiting for "how did that feel?"
  const offeredRef = useRef(false);
  const crisisSeenRef = useRef(false);
  const checkinRef = useRef(null); // a due reminder waiting for a good moment
  const runRef = useRef(0); // bumps to stop the running exercise
  const runningRef = useRef(null); // { action, action_type, reminder_id } while an exercise runs
  const pausedRef = useRef(false);

  const commit = (turns) => {
    historyRef.current = turns;
    setHistory(turns);
  };

  const showPhase = (p) => {
    phaseRef.current = p;
    setPhase(p);
  };
  const showInterim = (t) => {
    interimRef.current = t;
    setInterim(t);
  };

  const listen = useCallback(() => {
    showInterim('');
    setWaiting(false);
    showPhase('listening');
    engine.showMic();
    listenerRef.current.setPacing(settingsRef.current.pacing);
    listenerRef.current.start();
  }, [engine]);

  const voiceSettings = () => {
    const s = settingsRef.current;
    const useNatural = s.source === 'natural' && !FIXED_MODE && !naturalFailedRef.current;
    return { ...s, source: useNatural ? 'natural' : 'device' };
  };

  /** Speak without listening afterwards. During an exercise the phase stays 'exercise'. */
  const speakOut = useCallback(
    (text, { inExercise = false } = {}) => {
      const turn = turnRef.current;
      return speak(text, voiceSettings(), {
        onStart: (audioEl) => {
          if (turnRef.current !== turn) return;
          listenerRef.current.stop(); // reply is playing: the user's turn is settled
          if (!inExercise) showPhase('speaking');
          if (audioEl) engine.showAudio(audioEl);
          else engine.showVoice();
        },
        onFallback: (e) => {
          naturalFailedRef.current = true;
          setError(`Natural voice unavailable, using the device voice. (${e.message})`);
        },
      });
    },
    [engine]
  );

  // Defined below; reached through a ref so say() can hand over to a due check-in.
  const checkInRef = useRef(() => false);

  const say = useCallback(
    async (text) => {
      const turn = turnRef.current;
      await speakOut(text);
      if (turnRef.current !== turn) return;
      if (checkInRef.current(true)) return;
      listen();
    },
    [speakOut, listen]
  );

  /** A due reminder: ask "ready to do it now?" once nothing else is going on. → true if it started. */
  checkInRef.current = (afterSpeech = false) => {
    const rem = checkinRef.current;
    if (!rem || !sessionIdRef.current) return false;
    if (runningRef.current || feedbackRef.current || offerRef.current || crisisSeenRef.current) return false;
    if (!afterSpeech && (phaseRef.current !== 'listening' || interimRef.current)) return false;
    checkinRef.current = null;
    turnRef.current++;
    listenerRef.current.stop();
    offerRef.current = { mode: 'checkin', action: rem.action, reminder_id: rem.id };
    const line = checkinLine(rem);
    commit([...historyRef.current, { role: 'assistant', text: line }]);
    say(line);
    return true;
  };

  /** Called when a reminder comes due during the session. */
  const checkIn = useCallback((rem) => {
    checkinRef.current = rem;
    checkInRef.current();
  }, []);

  /** After an exercise: ask how it felt, then listen. The answer is saved as the outcome. */
  const finishExercise = useCallback(
    (done, closing) => {
      const meta = runningRef.current;
      runningRef.current = null;
      pausedRef.current = false;
      setExercise(null);
      if (!meta) return;
      feedbackRef.current = { ...meta, done };
      const line = done ? `${closing} How did that feel?` : "That's okay, we can stop there. How are you feeling now?";
      turnRef.current++;
      commit([...historyRef.current, { role: 'assistant', text: line }]);
      say(line);
    },
    [say]
  );

  /** Count down `secs` (paused while pausedRef is set). Resolves early if the exercise is stopped. */
  const countdown = (secs, run) =>
    new Promise((resolve) => {
      let left = secs * 1000;
      let last = Date.now();
      const id = setInterval(() => {
        const now = Date.now();
        if (!pausedRef.current) left -= now - last;
        last = now;
        if (runRef.current !== run || left <= 0) {
          clearInterval(id);
          resolve();
        }
        setExercise((e) => e && { ...e, remaining: Math.max(0, left / 1000) });
      }, 100);
    });

  /** Guided exercise: intro, then each step spoken as it starts with its timer, then the closing. Mic off throughout. */
  const runExercise = useCallback(
    async ({ exercise: ex, action, action_type, reminder_id }) => {
      const run = ++runRef.current;
      turnRef.current++;
      listenerRef.current.stop();
      runningRef.current = { action, action_type, reminder_id: reminder_id ?? null };
      pausedRef.current = false;
      const secs = stepSeconds(ex);
      setExercise({ title: ex.title, steps: ex.steps, secs, index: -1, remaining: 0, paused: false });
      showPhase('exercise');
      showInterim('');
      engine.showVoice(); // the orb pulses for the whole exercise

      prefetchSpeech(ex.steps[0]?.say, voiceSettings());
      await speakOut(ex.intro, { inExercise: true });
      for (let i = 0; i < ex.steps.length; i++) {
        while (pausedRef.current && runRef.current === run) await new Promise((r) => setTimeout(r, 100));
        if (runRef.current !== run) return;
        setExercise((e) => e && { ...e, index: i, remaining: secs[i] });
        prefetchSpeech(ex.steps[i + 1]?.say, voiceSettings()); // ready by the time this step ends
        await Promise.all([speakOut(ex.steps[i].say, { inExercise: true }).then(() => engine.showVoice()), countdown(secs[i], run)]);
      }
      if (runRef.current !== run) return;
      finishExercise(true, ex.closing);
    },
    [engine, speakOut, finishExercise]
  );

  const pauseExercise = useCallback(() => {
    pausedRef.current = !pausedRef.current;
    if (pausedRef.current) stopSpeaking(); // the step's words aren't repeated; its timer picks up where it left off
    setExercise((e) => e && { ...e, paused: pausedRef.current });
  }, []);

  const stopExercise = useCallback(() => {
    runRef.current++;
    stopSpeaking();
    finishExercise(false);
  }, [finishExercise]);

  /** What the server needs to know about the step 5 flow for this message. */
  const actionContext = () => {
    if (feedbackRef.current) return { mode: 'feedback', ...feedbackRef.current };
    if (offerRef.current) return offerRef.current;
    if (crisisSeenRef.current || FIXED_MODE) return null;
    // After this session's suggestion, the server only listens for "can we do breathing?" (step 6).
    return offeredRef.current ? { mode: 'consider', suggest: false } : { mode: 'consider' };
  };

  /** Update the flow from the server's answer. → true if an exercise was started (it does its own speaking). */
  const applyAction = (ctx, res) => {
    const ev = res.action;
    if (ctx?.mode === 'feedback') feedbackRef.current = null; // one answer is enough
    if ((ctx?.mode === 'offer' || ctx?.mode === 'checkin') && ev?.type !== 'need_time') offerRef.current = null;
    if (res.crisis) {
      crisisSeenRef.current = true;
      offerRef.current = null;
      if (ctx?.mode === 'checkin') remindersChangedRef.current?.();
      return false;
    }
    if (ctx?.mode === 'checkin' || ev?.type === 'reminder') remindersChangedRef.current?.();
    if (ev?.type === 'offer') {
      offeredRef.current = true;
      offerRef.current = { mode: 'offer', action: ev.action, action_type: ev.action_type };
    }
    if (ev?.type === 'reminder') askNotificationPermission();
    if (ev?.type === 'start') {
      offeredRef.current = true; // asked for, or accepted: no other suggestion this session
      runExercise(ev);
      return true;
    }
    return false;
  };

  const respond = useCallback(
    async (text) => {
      const turn = ++turnRef.current;
      const before = historyRef.current;
      beforeTurnRef.current = before;
      const withUser = [...before, { role: 'user', text }];
      commit(withUser);
      showInterim('');
      setError('');
      showPhase('thinking');
      engine.showIdle();

      const ctx = actionContext();
      let res;
      try {
        res = await fetchReply(withUser, ctx);
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
      if (applyAction(ctx, res)) return;
      say(res.speech || res.reply);
    },
    [engine, say, runExercise]
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
      showPhase('listening');
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
      onInterim: showInterim,
      onWaiting: setWaiting,
      onTurn: (text) => handlers.current.onTurn(text),
      onResume: () => handlers.current.onResume(),
      isComplete: FIXED_MODE ? null : (text) => handlers.current.isComplete(text),
      onError: (msg) => {
        setError(msg);
        showPhase('idle');
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

  /** An exercise or check-in left open when the session ends still gets its outcome saved. */
  const closeActions = useCallback(() => {
    runRef.current++;
    const open = runningRef.current ? { ...runningRef.current, done: false } : feedbackRef.current;
    if (open && !FIXED_MODE) saveOutcome(open);
    runningRef.current = feedbackRef.current = offerRef.current = checkinRef.current = null;
    pausedRef.current = false;
    setExercise(null);
  }, []);

  /**
   * Call from a click/tap so the browser allows speech output.
   * `dueReminder`: a reminder that's due, so the session opens with its check-in.
   */
  const start = useCallback(async (dueReminder = null) => {
    const id = ++sessionRef.current;
    turnRef.current++;
    sessionIdRef.current = newSessionId();
    naturalFailedRef.current = false;
    offeredRef.current = crisisSeenRef.current = false;
    closeActions();
    setError('');
    showInterim('');
    setCrisis(null);
    commit([]);
    showPhase('thinking');
    // Ask for the mic while the greeting plays; the stream drives the orb while listening.
    engine.openMic().catch(() => {
      if (sessionRef.current !== id) return;
      setError('Microphone access was blocked. Allow it for this site and start again.');
    });
    if (dueReminder) {
      // Opening the app with a due reminder starts with its check-in.
      offerRef.current = { mode: 'checkin', action: dueReminder.action, reminder_id: dueReminder.id };
      const line = `Hi, welcome back. ${checkinLine(dueReminder)}`;
      commit([{ role: 'assistant', text: line }]);
      say(line);
      return;
    }
    // Returning users get a greeting that picks up from last time.
    const greeting = (!FIXED_MODE && (await fetchGreeting())) || GREETING;
    if (sessionRef.current !== id) return;
    commit([{ role: 'assistant', text: greeting }]);
    say(greeting);
  }, [engine, say, closeActions]);

  const end = useCallback(() => {
    save();
    closeActions();
    sessionRef.current++;
    turnRef.current++;
    listenerRef.current?.stop();
    stopSpeaking();
    engine.stop();
    showPhase('idle');
    showInterim('');
    setWaiting(false);
  }, [engine, save, closeActions]);

  // Closing or leaving the tab mid-session still saves it.
  useEffect(() => {
    const onHide = () => {
      save(true);
      closeActions();
    };
    window.addEventListener('pagehide', onHide);
    return () => window.removeEventListener('pagehide', onHide);
  }, [save, closeActions]);

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

  return {
    phase,
    history,
    interim,
    waiting,
    error,
    crisis,
    dismissCrisis,
    start,
    end,
    interrupt,
    send,
    exercise,
    pauseExercise,
    stopExercise,
    checkIn,
  };
}
