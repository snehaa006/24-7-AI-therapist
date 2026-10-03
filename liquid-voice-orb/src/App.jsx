import { useEffect, useRef, useState } from 'react';
import LiquidOrb from './LiquidOrb.jsx';
import { AudioEngine } from './audioEngine.js';

const STATUS = {
  idle: 'Idle',
  demo: 'Playing a simulated voice',
  mic: 'Listening to your microphone',
  file: 'Playing your audio file',
};

export default function App() {
  const engineRef = useRef(null);
  if (!engineRef.current) engineRef.current = new AudioEngine();
  const engine = engineRef.current;

  const fileInputRef = useRef(null);
  const [mode, setMode] = useState('idle');
  const [error, setError] = useState('');

  useEffect(() => () => engine.stop(), [engine]);

  const stopAll = () => {
    engine.stop();
    setMode('idle');
  };

  const toggleDemo = () => {
    setError('');
    if (mode === 'demo') return stopAll();
    engine.startDemo();
    setMode('demo');
  };

  const toggleMic = async () => {
    setError('');
    if (mode === 'mic') return stopAll();
    try {
      await engine.startMic();
      setMode('mic');
    } catch {
      setMode('idle');
      setError('Microphone access was blocked. Allow it in your browser settings and try again.');
    }
  };

  const onFilePicked = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    setError('');
    try {
      await engine.playFile(file, () => setMode('idle'));
      setMode('file');
    } catch {
      setMode('idle');
      setError('That file could not be played. Try an MP3, WAV or M4A file.');
    }
  };

  const toggleFile = () => {
    setError('');
    if (mode === 'file') return stopAll();
    fileInputRef.current?.click();
  };

  return (
    <main className="stage">
      <LiquidOrb engine={engine} />

      <div className="dock">
        <p className="status" role="status" aria-live="polite" data-tone={error ? 'error' : 'info'}>
          {error || STATUS[mode]}
        </p>
        <div className="controls">
          <button className="btn" aria-pressed={mode === 'demo'} onClick={toggleDemo}>
            {mode === 'demo' ? 'Stop demo voice' : 'Play demo voice'}
          </button>
          <button className="btn" aria-pressed={mode === 'mic'} onClick={toggleMic}>
            {mode === 'mic' ? 'Stop microphone' : 'Use microphone'}
          </button>
          <button className="btn" aria-pressed={mode === 'file'} onClick={toggleFile}>
            {mode === 'file' ? 'Stop audio file' : 'Load audio file'}
          </button>
        </div>
        <input ref={fileInputRef} type="file" accept="audio/*" hidden onChange={onFilePicked} />
      </div>
    </main>
  );
}
