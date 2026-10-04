import { useEffect, useRef, useState } from 'react';
import { deleteMemory, fetchMemories, forgetAll } from './memory.js';
import { fetchStats, resetStats } from './actions.js';

const DAY = 24 * 3600 * 1000;

/** 'today', 'yesterday', '3 days ago', '2 weeks ago'. */
function ago(seconds) {
  const days = Math.floor((Date.now() - seconds * 1000) / DAY);
  if (days < 1) return 'today';
  if (days < 2) return 'yesterday';
  if (days < 14) return `${days} days ago`;
  return `${Math.floor(days / 7)} weeks ago`;
}

/** Bottom sheet listing what's remembered from past sessions, with delete, and what has helped (step 6). */
export default function MemoryPanel({ onClose }) {
  const [data, setData] = useState(null); // { memories: [{ id, kind, text }], last_summary }
  const [helps, setHelps] = useState([]); // action types with any history, best first
  const [error, setError] = useState('');
  const [confirming, setConfirming] = useState(false);
  const [confirmingReset, setConfirmingReset] = useState(false);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  const load = () =>
    fetchMemories()
      .then(setData)
      .catch((e) => setError(`Couldn't load memories. (${e.message})`));

  const loadHelps = () =>
    fetchStats()
      .then((d) => setHelps((d.types || []).filter((t) => t.tries || t.skipped || t.stopped)))
      .catch(() => setHelps([])); // the memories still show

  useEffect(() => {
    load();
    loadHelps();
    const onKey = (e) => e.key === 'Escape' && closeRef.current();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const remove = async (id) => {
    setError('');
    try {
      await deleteMemory(id);
      setData((d) => ({ ...d, memories: d.memories.filter((m) => m.id !== id) }));
    } catch (e) {
      setError(`Couldn't delete that. (${e.message})`);
    }
  };

  const wipe = async () => {
    setError('');
    try {
      await forgetAll();
      setData({ memories: [], last_summary: '' });
      setConfirming(false);
    } catch (e) {
      setError(`Couldn't clear memories. (${e.message})`);
    }
  };

  const reset = async () => {
    setError('');
    try {
      await resetStats();
      setHelps([]);
      setConfirmingReset(false);
      load(); // the notes about past exercises go too
    } catch (e) {
      setError(`Couldn't reset that. (${e.message})`);
    }
  };

  const groups = [
    ['fact', 'Things you’ve shared'],
    ['pattern', 'Patterns'],
  ];
  const empty = data && !data.memories.length && !data.last_summary && !helps.length;

  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="sheet" role="dialog" aria-modal="true" aria-label="Memories" onClick={(e) => e.stopPropagation()}>
        <header className="sheet-head">
          <h2>What I remember</h2>
          <button className="ghost" onClick={onClose}>
            Done
          </button>
        </header>
        <p className="hint">
          Saved when a session ends, so it can pick up where you left off. Anything said during a crisis is never saved. Stored on
          this app’s server under an anonymous id for this browser.
        </p>

        {!data && !error && <p className="hint">Loading…</p>}
        {empty && <p className="memory-empty">Nothing yet. After your first session, details you share will show up here.</p>}

        {data?.last_summary && (
          <section>
            <h3>Last time</h3>
            <p className="memory-summary">{data.last_summary}</p>
          </section>
        )}

        {data &&
          groups.map(([kind, title]) => {
            const items = data.memories.filter((m) => m.kind === kind);
            if (!items.length) return null;
            return (
              <section key={kind}>
                <h3>{title}</h3>
                <ul className="memory-list">
                  {items.map((m) => (
                    <li key={m.id}>
                      <span>{m.text}</span>
                      <button type="button" onClick={() => remove(m.id)} aria-label={`Forget: ${m.text}`}>
                        ×
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            );
          })}

        {helps.length > 0 && (
          <section aria-label="What helps">
            <h3>What helps</h3>
            <ul className="memory-list helps-list">
              {helps.map((t) => (
                <li key={t.type}>
                  <span>
                    {t.label} – {t.summary}
                  </span>
                  {t.last_tried && <span className="helps-when">{ago(t.last_tried)}</span>}
                </li>
              ))}
            </ul>
            <button
              className={`link-button${confirmingReset ? ' danger' : ''}`}
              onClick={confirmingReset ? reset : () => setConfirmingReset(true)}
            >
              {confirmingReset ? 'Tap again to reset what helps' : 'Reset what helps'}
            </button>
          </section>
        )}

        {data && (data.memories.length > 0 || data.last_summary) && (
          <button className={`preview${confirming ? ' danger' : ''}`} onClick={confirming ? wipe : () => setConfirming(true)}>
            {confirming ? 'Tap again to forget everything' : 'Forget everything'}
          </button>
        )}
        {error && <p className="hint warn">{error}</p>}
      </div>
    </div>
  );
}
