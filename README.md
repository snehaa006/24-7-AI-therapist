# 24-7-AI-therapist

A voice-first AI companion you can talk to any time. This build covers **steps 1 to 4** of the plan:

1. **Voice loop.** Mic → speech-to-text → reply → spoken output (browser Web Speech API).
2. **Real conversation.** Replies come from Gemini with a system prompt (listen, reflect, ask one follow-up, no advice lists). The whole session history is sent each turn.
3. **Safety.** Every message is screened for crisis signs. A crisis gets a fixed, human-written message (not AI) and an on-screen card with tap-to-call helpline numbers.
4. **Memory.** At the end of a session, lasting details and patterns are saved. Next time, the AI greets you with something from last time and keeps those details in mind.

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
| 3 | `cd server && pip install -r requirements-dev.txt && pytest` | Every crisis test phrase triggers the safety response; none of the look-alikes ("this traffic is killing me") do |
| 4 | Talk about something specific (a pet's name, an event coming up), tap **End**, then start again | The greeting brings up that detail. **Memories** on the start screen shows what was saved |

The frontend has Playwright tests with the mic, speech output and backend faked: `cd liquid-voice-orb && npm run test:e2e` (first time: `npx playwright install chromium`).

## How a turn works

- The app greets you, then listens. A pause alone doesn't end your turn:
  - If your words trail off ("…and", "…because", "um"), it waits longer and shows **Take your time**.
  - Otherwise, after a short pause, a fast Gemini model (`POST /api/turn`) reads what you've said, plus the AI's last question, and decides whether your thought sounds finished. If not, it keeps waiting.
  - After a long silence (6 s on Natural pacing) the turn ends regardless.
  - If you start talking again while it's preparing a reply, that reply is dropped and your turn continues.
- Your words go to `POST /api/chat` with the full session history. Gemini answers and the reply is spoken.
- Then it listens again. **Let me speak** cuts the reply short. You can also type instead of talking.
- The orb follows your mic while you talk and the AI's voice while it speaks.

## Safety (step 3)

Each user message is checked two ways, in `server/safety.py`:

1. **Keywords.** A regex list (suicide, kill myself, self-harm, want to die, better off dead…). A match is always a crisis, with no model call. The list is kept tight so figures of speech ("my feet are killing me", "I'm dying to see it") don't match.
2. **Gemini.** A separate call to a fast model (`GEMINI_SAFETY_MODEL`, temperature 0, JSON output) labels the latest message `normal` or `crisis`, using the last six turns for context, so it catches messages without keywords and understands a "yes" to "are you safe?". It runs at the same time as the reply call, so it adds no delay. **If it fails (quota, network), the message counts as a crisis.**

On a crisis, the AI reply is thrown away. The app speaks a fixed script and shows a card with tap-to-call links to your helpline and emergency number. The conversation can carry on afterwards. Set the numbers in `server/.env` (`CRISIS_HELPLINE_NAME`, `CRISIS_HELPLINE_NUMBER`, `EMERGENCY_NUMBER`); without a helpline number, the card links to findahelpline.com.

Tests (`server/tests/test_safety.py`) run 22 crisis phrases and 21 look-alikes through the keyword check and the whole `/api/chat` path with Gemini stubbed. If `GEMINI_API_KEY` is set (in your shell or `server/.env`), a live test also sends every phrase to the real classifier, paced for the free tier (about 3 minutes; `LIVE_TEST_DELAY` sets the gap in seconds).

## Memory (step 4)

- **Who you are.** The browser makes an anonymous id and keeps it in localStorage. No account or name. Clearing site data starts afresh.
- **Saving.** When a session ends (the **End** button, or closing the tab, which sends it with `navigator.sendBeacon`), the server sends the conversation to Gemini once, with a JSON response schema. It returns new facts ("Their sister Maya is getting married in June"), patterns ("Feels anxious on Sunday nights"), the ids of saved notes that are now out of date, and a one-line summary. These are merged with what's already saved: exact repeats are skipped, outdated notes removed, and each person keeps at most 40.
- **Never saved:** anything flagged by the safety layer, any message matching a crisis keyword, the reply to either, and any note Gemini returns that matches a crisis keyword.
- **Using it.** Saved notes and last session's summary are added to the system prompt on every reply. Returning users get a short AI-written greeting that picks up from last time (new users, or if that call fails or takes over 6 s, get the usual greeting).
- **Seeing it.** **Memories** on the start screen lists what's saved. Delete any line, or forget everything.
- Stored in SQLite at `server/memory.db` (gitignored; `MEMORY_DB` moves it). Memory costs one Gemini call at the end of each session and one at the start for returning users (`GEMINI_MEMORY_MODEL`).

Tests: `server/tests/test_memory.py` (Gemini stubbed; a live test runs if `GEMINI_API_KEY` is set) and `liquid-voice-orb/e2e/memory.spec.js`.

## Voice and pacing

Tap **Voice** (on the start screen or during a session):

- **Natural voices.** 16 Gemini voices (Sulafat, Achernar, Vindemiatrix…) via `POST /api/speak`. They sound lifelike, but Gemini makes the whole clip before sending it, so new lines take a few seconds. To cut the wait, the first sentence is generated on its own and the rest in parallel. Generated audio is cached in `server/.tts-cache/`, so the greeting and previews replay instantly. Uses your Gemini quota (up to two voice requests per reply), which is low for text-to-speech on the free tier. If a request fails, the app switches to the device voice for the rest of the session and tells you.
- **Device voices.** Any voice built into your browser or OS. Instant, with pitch control.
- **Speed**, and **When to reply**: Quick, Natural or Patient.

Settings are saved in this browser.

## Notes

- Speech recognition uses Chrome's built-in service, which sends audio to Google. It needs internet and works in Chrome or Edge, not Firefox.
- On Gemini's free tier, prompts may be used by Google to improve its products. Use test conversations only.
- Models are set in `server/.env` (`GEMINI_MODEL`, `GEMINI_TURN_MODEL`, `GEMINI_SAFETY_MODEL`, `GEMINI_TTS_MODEL`). Check current model IDs in AI Studio.
- Each turn now uses up to four Gemini calls (turn check, safety label, reply, voice). On the free tier, switch to device voices if you hit limits. Running out of quota on the safety model makes every message count as a crisis, by design.
- The safety layer is a backstop, not a guarantee. It has not been clinically reviewed.
- Memories are stored as plain text on the server, keyed only by the browser's id. Anyone with that id can read them. Fine for local testing; add real accounts and encryption before hosting this for other people.
