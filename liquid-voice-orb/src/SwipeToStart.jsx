import { useRef, useState } from 'react';

const DONE_AT = 0.7; // fraction of the track the knob must travel

/**
 * Pill with a draggable knob. Slide it right — or just tap / press Enter — to start.
 * `onStart` runs inside the pointer/keyboard event so the browser allows audio.
 */
export default function SwipeToStart({ label = 'Start Now', onStart }) {
  const trackRef = useRef(null);
  const knobRef = useRef(null);
  const drag = useRef(null);
  const [x, setX] = useState(0);
  const [settling, setSettling] = useState(false);

  const maxX = () => {
    const t = trackRef.current;
    const k = knobRef.current;
    return t && k ? t.clientWidth - k.offsetWidth - 2 * k.offsetLeft : 1;
  };

  const finish = () => {
    setSettling(true);
    setX(maxX());
    onStart();
  };

  const onPointerDown = (e) => {
    if (e.button !== 0) return;
    drag.current = { startX: e.clientX, moved: false };
    setSettling(false);
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const onPointerMove = (e) => {
    if (!drag.current) return;
    const dx = e.clientX - drag.current.startX;
    if (Math.abs(dx) > 4) drag.current.moved = true;
    setX(Math.min(maxX(), Math.max(0, dx)));
  };

  const onPointerUp = () => {
    if (!drag.current) return;
    const { moved } = drag.current;
    drag.current = null;
    if (!moved || x / maxX() >= DONE_AT) return finish(); // a tap counts too
    setSettling(true);
    setX(0);
  };

  const progress = Math.min(1, x / maxX());

  return (
    <button
      ref={trackRef}
      type="button"
      className="swipe"
      aria-label={label}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={() => {
        drag.current = null;
        setSettling(true);
        setX(0);
      }}
      onClick={(e) => {
        // keyboard activation (pointer taps are handled in onPointerUp)
        if (e.detail === 0) finish();
      }}
    >
      <span
        ref={knobRef}
        className="swipe-knob"
        data-settling={settling}
        style={{ transform: `translateX(${x}px)` }}
        aria-hidden="true"
      >
        <svg viewBox="0 0 24 24" width="22" height="22">
          <path d="M5 12h13M13 6l6 6-6 6" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </span>
      <span className="swipe-label" style={{ opacity: 1 - progress * 1.4 }} aria-hidden="true">
        {label}
      </span>
      <svg className="swipe-chevron" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
        <path d="M9 6l6 6-6 6" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </button>
  );
}
