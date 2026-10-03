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

- The app greets you, then listens. Your turn ends after a ~1.4 s pause.
- Your words go to `POST /api/chat` with the full session history. Gemini answers and the browser speaks the reply.
- Then it listens again. **Let me speak** cuts the reply short. You can also type instead of talking.
- The orb follows your mic while you talk and pulses while the AI speaks.

## Notes

- Speech recognition uses Chrome's built-in service, which sends audio to Google. It needs internet and works in Chrome or Edge, not Firefox.
- On Gemini's free tier, prompts may be used by Google to improve its products. Use test conversations only.
- Set `GEMINI_MODEL` in `server/.env` to change the model. Check the current Flash model ID in AI Studio.
- This build has no safety layer yet (step 3). The system prompt only points people to emergency help if they mention danger.
