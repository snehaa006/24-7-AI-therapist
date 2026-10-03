# 24-7-AI-therapist

A voice-first AI companion you can talk to any time. This build covers **steps 1 and 2** of the plan:

1. **Voice loop.** Mic → speech-to-text → reply → spoken output (browser Web Speech API).
2. **Real conversation.** Replies come from Gemini with a system prompt (listen, reflect, ask one follow-up, no advice lists). The whole session history is sent each turn.

```
liquid-voice-orb/   React + Vite frontend (orb, landing page, voice session)
server/             FastAPI backend that calls Gemini (keeps the API key out of the browser)
```

## Run it

**1. Backend** (Python 3.10+)

```bash
cd server
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then paste your key from https://aistudio.google.com/apikey
uvicorn main:app --reload --port 8000
```

**2. Frontend** (Node 18+), in a second terminal

```bash
cd liquid-voice-orb
npm install
npm run dev
```

Open http://localhost:5173 **in Chrome**, slide **Start Now**, and allow the microphone.

## Checking each step

| Step | How to test | Done when |
|---|---|---|
| 1 | Open http://localhost:5173/?mode=fixed (no backend or key needed) | You say "I need to talk" and hear the fixed reply, every time |
| 2 | Open http://localhost:5173 with the backend running | A five-turn talk where every reply follows from what you said. Open **Transcript** to review it |

## How a turn works

- The app greets you, then listens. A pause alone doesn't end your turn:
  - If your words trail off ("…and", "…because", "um"), it waits longer and shows **Take your time**.
  - Otherwise, after a short pause, a fast Gemini model (`POST /api/turn`) reads what you've said, plus the AI's last question, and decides whether your thought sounds finished. If not, it keeps waiting.
  - After a long silence (6 s on Natural pacing) the turn ends regardless.
  - If you start talking again while it's preparing a reply, that reply is dropped and your turn continues.
- Your words go to `POST /api/chat` with the full session history. Gemini answers and the reply is spoken.
- Then it listens again. **Let me speak** cuts the reply short. You can also type instead of talking.
- The orb follows your mic while you talk and the AI's voice while it speaks.

## Voice and pacing

Tap **Voice** (on the start screen or during a session):

- **Natural voices.** 16 Gemini voices (Sulafat, Achernar, Vindemiatrix…) via `POST /api/speak`. They sound lifelike but add a moment before each reply and use your Gemini quota, which is low for text-to-speech on the free tier. If a request fails, the app switches to the device voice for the rest of the session and tells you.
- **Device voices.** Any voice built into your browser or OS. Instant, with pitch control.
- **Speed**, and **When to reply**: Quick, Natural or Patient.

Settings are saved in this browser.

## Notes

- Speech recognition uses Chrome's built-in service, which sends audio to Google. It needs internet and works in Chrome or Edge, not Firefox.
- On Gemini's free tier, prompts may be used by Google to improve its products. Use test conversations only.
- Models are set in `server/.env` (`GEMINI_MODEL`, `GEMINI_TURN_MODEL`, `GEMINI_TTS_MODEL`). Check current model IDs in AI Studio.
- Each turn now uses up to three Gemini calls (turn check, reply, voice). On the free tier, switch to device voices if you hit limits.
- This build has no safety layer yet (step 3). The system prompt only points people to emergency help if they mention danger.
