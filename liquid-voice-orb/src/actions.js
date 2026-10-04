// Action and follow-through (step 5): reminders, outcomes, browser notifications, fast test mode.
import { useCallback, useEffect, useRef, useState } from 'react';
import { userId } from './memory.js';

// ?fastExercise=1 squeezes every exercise into ~20 s, for testing.
export const FAST_EXERCISE = new URLSearchParams(window.location.search).get('fastExercise') === '1';
const FAST_TOTAL = 20;

/** Step durations to actually use (scaled down in fast mode). */
export function stepSeconds(exercise) {
  const total = exercise.steps.reduce((n, s) => n + s.seconds, 0);
  const scale = FAST_EXERCISE && total > FAST_TOTAL ? FAST_TOTAL / total : 1;
  return exercise.steps.map((s) => s.seconds * scale);
}

async function call(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) throw new Error(`Server error ${res.status}`);
  return res.json();
}

const q = () => `user_id=${encodeURIComponent(userId())}`;

export const fetchReminders = () => call(`/api/reminders?${q()}`).then((d) => (Array.isArray(d?.reminders) ? d.reminders : []));

/** An exercise that ended without an answer to "how did that feel?" (the session was closed). */
export function saveOutcome({ action, reminder_id = null, done = true }) {
  const body = JSON.stringify({ user_id: userId(), action, reminder_id, done });
  fetch('/api/outcomes', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, keepalive: true }).catch(() => {});
}

/** Ask once, when the first reminder is set. */
export function askNotificationPermission() {
  if (typeof Notification === 'undefined' || Notification.permission !== 'default') return;
  Notification.requestPermission?.().catch(() => {});
}

function notify(reminder) {
  if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
  try {
    const n = new Notification('Time for your check-in', { body: `You planned ${reminder.action}.`, tag: `reminder-${reminder.id}` });
    n.onclick = () => window.focus();
  } catch {
    /* some mobile browsers only allow notifications from a service worker */
  }
}

const key = (r) => `${r.id}@${r.due_at}`;

/**
 * Pending reminders for this browser, with an in-app timer. When one comes due, it shows a
 * notification and calls onDue(reminder) once. Only works while this tab is open.
 */
export function useReminders(onDue) {
  const [reminders, setReminders] = useState([]);
  const [due, setDue] = useState(null); // the reminder waiting for a check-in
  const fired = useRef(new Set()); // `${id}@${due_at}`, so a reminder moved to later can fire again
  const onDueRef = useRef(onDue);
  onDueRef.current = onDue;

  const refresh = useCallback(() => {
    fetchReminders()
      .then(setReminders)
      .catch(() => {});
  }, []);

  useEffect(refresh, [refresh]);

  useEffect(() => {
    const check = () => {
      const now = Date.now();
      const ready = reminders.filter((r) => r.due_at * 1000 <= now);
      const next = ready.find((r) => !fired.current.has(key(r)));
      if (!next) return;
      ready.forEach((r) => fired.current.add(key(r))); // one check-in at a time, the earliest
      setDue(next);
      notify(next);
      onDueRef.current?.(next);
    };
    check();
    const upcoming = reminders.filter((r) => !fired.current.has(key(r))).map((r) => r.due_at * 1000 - Date.now());
    const timers = upcoming.filter((ms) => ms > 0).map((ms) => setTimeout(check, Math.min(ms + 50, 2 ** 31 - 1)));
    const backup = setInterval(check, 30_000); // in case a timer was throttled in a background tab
    return () => {
      timers.forEach(clearTimeout);
      clearInterval(backup);
    };
  }, [reminders]);

  const clearDue = useCallback(() => setDue(null), []);
  return { reminders, due, clearDue, refresh };
}
