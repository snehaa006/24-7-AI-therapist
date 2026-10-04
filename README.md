# 24-7-AI-therapist

A voice-first AI companion you can talk to any time. This build covers **steps 1 to 6** of the plan:

1. **Voice loop.** Mic → speech-to-text → reply → spoken output (browser Web Speech API).
2. **Real conversation.** Replies come from Gemini with a system prompt (listen, reflect, ask one follow-up, no advice lists). The whole session history is sent each turn.
3. **Safety.** Every message is screened for crisis signs. A crisis gets a fixed, human-written message (not AI) and an on-screen card with tap-to-call helpline numbers.
4. **Memory.** At the end of a session, lasting details and patterns are saved. Next time, the AI greets you with something from last time and keeps those details in mind.
5. **Action and follow-through.** When it fits, the AI suggests one small thing to do (based on what you said and what helped before). Do it now as a guided exercise, set a reminder for later, or say no. Afterwards it asks how it felt and remembers.
6. **Feedback loop.** Every action has a type (walk, breathing, music…). The app counts how each type went for you and picks the next suggestion's type from that, in code: more of what helped, none of what failed twice. **Memories** shows what helps.

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
cp .env.example .env        # then paste your key from https://aistudio.google.com/apikey (or several, see below)
uvicorn main:app --reload --reload-include .env --port 8000   # restarts when .env changes too
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
| 5 | Open http://localhost:5173/?fastExercise=1, talk about something stressful for a couple of turns, answer the suggestion with "later, in 1 minute", wait | The reminder fires, the check-in runs the exercise, and your answer to "How did that feel?" shows up in **Memories** |
| 6 | On http://localhost:5173/?fastExercise=1 say "can we do breathing?", answer "it didn't help"; then "let's go for a walk", answer "I feel better". Tap **End**, start again and talk for a couple of turns | The suggestion is a walk. **Memories → What helps** shows "Walk – helped 1 of 1" and "Breathing – helped 0 of 1" |

The frontend has Playwright tests with the mic and speech output faked: `cd liquid-voice-orb && npm run test:e2e` (first time: `npx playwright install chromium`). Most mock the backend; the step 5 and 6 tests (`actions.spec.js`, `feedback.spec.js`) run the real backend with Gemini stubbed (`server/tests/e2e_server.py`), so they need the server's packages: it uses `server/.venv` if present, else `python3` (or set `PYTHON`). The step 5 test waits a real minute for the reminder, so the suite takes about 4 minutes.

## How a turn works

- The app greets you, then listens. A pause alone doesn't end your turn:
  - If your words trail off ("…and", "…because", "um"), it waits longer and shows **Take your time**.
  - Otherwise, after a short pause, a fast Gemini model (`POST /api/turn`) reads what you've said, plus the AI's last question, and decides whether your thought sounds finished. If not, it keeps waiting.
  - After a long silence (5.5 s on Natural pacing) the turn ends regardless.
  - The reply is requested at the same moment as that check, not after it, so it's usually ready as soon as the turn ends. (Skipped while you're answering a suggestion or a check-in, because those answers save a reminder or an outcome.)
  - If you start talking again while it's preparing a reply, that reply is dropped and your turn continues.
- Your words go to `POST /api/chat` with the full session history. Gemini answers and the reply is spoken. The prompt asks for short, emotionally present replies in everyday spoken language, and tells Gemini the words come from speech recognition, so it answers what you most likely meant when a word is misheard.
- Then it listens again. **Let me speak** cuts the reply short. You can also type instead of talking.
- The orb follows your mic while you talk and the AI's voice while it speaks.

## Safety (step 3)

Each user message is checked two ways, in `server/safety.py`:

1. **Keywords.** A regex list (suicide, kill myself, self-harm, want to die, better off dead…). A match is always a crisis, with no model call. The list is kept tight so figures of speech ("my feet are killing me", "I'm dying to see it") don't match.
2. **Gemini.** A separate call to a fast model (`GEMINI_SAFETY_MODEL`, temperature 0, JSON output) labels the latest message `normal` or `crisis`, using the last six turns for context, so it catches messages without keywords and understands a "yes" to "are you safe?". It runs at the same time as the reply call, so it adds no delay. If it fails (usually the free per-minute quota), it is asked once more on a second model (`GEMINI_SAFETY_FALLBACK_MODEL`, default `GEMINI_MODEL`), which has its own quota. **Only if both fail does the message count as a crisis.**

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

## Action and follow-through (step 5)

- **Suggesting.** From your second message on, a fast model (`GEMINI_ACTION_MODEL`, JSON schema) looks at the recent conversation, your memories and how past actions went, and decides whether to suggest ONE small thing (3 to 6 minutes, doable where you are). It runs alongside the reply, so it adds no delay. If it says yes, its suggestion ("Want to try a five-minute walk with your favourite music?") replaces the reply. At most one suggestion per session, and **never on a crisis turn or after a crisis in the same session.**
- **Your answer** goes through the same fast model (JSON: `now` / `later` with minutes or a clock time / `no` / `other`), not a regex. The safety check still runs first.
  - **Now:** Gemini (`GEMINI_EXERCISE_MODEL`) writes the exercise for you, as JSON `{title, intro, steps: [{say, seconds}], closing}`, using your words, memories and what helped before. The server checks it: 3 to 6 minutes in total, 4 to 15 steps, 5 to 90 s each, plain spoken text (no markdown, links, emojis), nothing medical or risky (fasting, hard exertion, driving, medication, alcohol…). If it fails, it tries once more, then uses a built-in breathing exercise, so the flow never breaks.
  - **Later / at 6:** the AI confirms the time ("I'll check in with you at 6:00 pm") and saves a reminder. If you didn't say when, it asks. The exercise is written in the background and stored with the reminder.
  - **No** (or you talk about something else): it drops it and carries on.
- **Exercise mode.** Shows the current step, a countdown, overall progress, and Pause / Stop. Each step is spoken as it starts; with natural voices, the next step's audio is generated while the current one runs. The mic is off during the steps and comes back on at the end, when the AI asks "How did that feel?"
- **Reminders** are stored in SQLite (user id, action, exercise JSON, due time, status). The app sets a timer and shows a browser notification when one is due (it asks for permission when the first one is set). If you're mid-conversation, the check-in waits for a pause. Opening the app with a due or overdue reminder starts the session with its check-in: "Ready to do it now?" (now, later, or no).
- **Outcome.** Your answer to "How did that feel?" is labelled yes / somewhat / no by the fast model and saved (action, type, done or stopped early or skipped, helped, your words) to SQLite and as a memory note ("Tried a five-minute walk: it helped."). Skipping a check-in is saved too. `GET /api/outcomes?user_id=…` lists them.
- **Limit:** reminders only fire while the app is open in a browser tab (an in-app timer, no push server or service worker). A reminder missed while the tab was closed becomes a check-in the next time you open the app.
- **Testing shortcuts:** say "in 1 minute" for a reminder a minute away, and open the app with `?fastExercise=1` to squeeze every exercise into about 20 seconds.

Cost: one extra fast-model call per message while a suggestion is possible or an answer is expected, and one exercise call (two if the first fails the checks) per accepted action.

Tests: `server/tests/test_actions.py` (validation and fallback, suggesting, now / later / no, reminders, check-in, outcome storage; Gemini stubbed, plus a live test if `GEMINI_API_KEY` is set) and `liquid-voice-orb/e2e/actions.spec.js`.

## Feedback loop (step 6)

- **Types.** Every suggestion, reminder and outcome has a type from a fixed list in `server/feedback.py` (`TYPES`): breathing, walk, stretch, grounding, music, journaling, reach_out, rest. The suggest call's JSON schema returns `type` (an enum of that list) with the action and line. Databases from step 5 get a `type` column on first use; old rows are labelled once by keyword matching ("a walk with music" → walk; nothing matched → `other`, which is shown but never suggested).
- **Stats, in code.** Per person and type: tries (done or started), helped (yes 1, somewhat 0.5, no 0), skipped, stopped early, and when last tried. The score is a weighted average of past outcomes where each older one counts 0.6× the one after it, starting from a neutral 0.5 so a single result doesn't decide everything. Skipped counts 0.25; stopping early gets half credit; "done but didn't say" doesn't count either way.
- **Picking the next type, in code.** Highest score wins. Types never tried get a +0.15 bonus, so it still explores (enough to beat a "somewhat", not a "yes"). A type whose last two tries were both "no" is never picked, unless every type is like that. Ties go to the type tried longest ago, then list order (so a new user starts with breathing).
- **What Gemini does.** The suggest call gets the chosen type plus one line like "Worked for them: walk (helped 2 of 2). Didn't work: breathing (helped 0 of 1)." and only writes the wording; a reply of a different type is dropped. The exercise prompt gets the same line and the type.
- **Asking for something.** "Can we do breathing?", "let's go for a walk", "how about a song": the app does that right away (a guided exercise of that type) and records how it went, even after the session's one suggestion. This check is a strict keyword pattern, with no model call; "I tried breathing" or "I want to rest" are not asks.
- **What helps.** **Memories** on the start screen lists each type you've tried and how it went ("Walk – helped 2 of 2", "Breathing – helped 0 of 1, skipped 1") with when it was last tried. **Reset what helps** clears the outcomes and the memory notes made from them. API: `GET /api/stats?user_id=…` (per-type stats, best first, and `next`, the type that would be suggested) and `DELETE /api/stats?user_id=…`.
- **Crisis rules are unchanged:** no suggestions or asked-for exercises on or after a crisis turn, and nothing said in a crisis turn is stored.

No extra Gemini calls: the scoring is local, and the prompts got shorter (one line instead of a list of past outcomes).

Tests: `server/tests/test_feedback.py` (scoring: no history, recent outcomes count more, the exploration bonus, the two-"no" rule, every type failing; keyword labels and asks; the migration; the stats endpoint and reset; "walk helped, breathing didn't → walk is suggested"; a live test if `GEMINI_API_KEY` is set) and `liquid-voice-orb/e2e/feedback.spec.js` (the same done-check through the UI and the real backend).

## Voice and pacing

Tap **Voice** (on the start screen or during a session):

- **Natural voices.** 16 Gemini voices (Sulafat, Achernar, Vindemiatrix…) via `POST /api/speak`. They sound lifelike, but Gemini makes the whole clip before sending it, so new lines take a few seconds. To cut the wait, the first sentence is generated on its own and the rest in parallel. Generated audio is cached in `server/.tts-cache/`, so the greeting and previews replay instantly. Uses your Gemini quota (up to two voice requests per reply), which is low for text-to-speech on the free tier. If a request fails, the app switches to the device voice for the rest of the session and tells you.
- **Device voices.** Any voice built into your browser or OS. Instant, with pitch control.
- **Speed**, and **When to reply**: Quick, Natural or Patient.
- **Your accent**: the speech recognition language. Choosing your variant of English (India, UK, Australia…) makes transcription noticeably more accurate than the browser default.

Settings are saved in this browser.

## Notes

- Speech recognition uses Chrome's built-in service, which sends audio to Google. It needs internet and works in Chrome or Edge, not Firefox.
- On Gemini's free tier, prompts may be used by Google to improve its products. Use test conversations only.
- Models are set in `server/.env` (`GEMINI_MODEL`, `GEMINI_TURN_MODEL`, `GEMINI_SAFETY_MODEL`, `GEMINI_TTS_MODEL`, `GEMINI_MEMORY_MODEL`, `GEMINI_ACTION_MODEL`, `GEMINI_EXERCISE_MODEL`). Check current model IDs in AI Studio.
- **Groq or Grok instead of Gemini.** Set `LLM_PROVIDER=groq` with `GROQ_API_KEY` (free tier, no card, very fast: `llama-3.3-70b-versatile` for replies and the safety check, `llama-3.1-8b-instant` for the quick checks), or `LLM_PROVIDER=grok` with `XAI_API_KEY` (xAI, paid; `grok-4.3` with reasoning off). All text calls then go to that service through `server/compat.py`; models are set with `GROQ_MODEL` / `GROQ_FAST_MODEL` or `GROK_MODEL`. Natural voices still use Gemini text-to-speech. Several Groq or xAI keys rotate exactly like Gemini keys (`GROQ_API_KEYS=key1,key2`).
- **Several API keys.** Put more than one key in `server/.env` (`GEMINI_API_KEYS=key1,key2,key3`, or `GEMINI_API_KEY_2`, `_3`…). Calls take turns across the keys, and a key that hits its quota (429) or stops working is rested and the call retried at once on the next key (`server/keys.py`). `GET /api/health` shows how many keys are set and resting.
- Each turn now uses up to five Gemini calls (turn check, safety label, reply, action check, voice). On the free tier, switch to device voices if you hit limits. Running out of quota on the safety model makes every message count as a crisis, by design.
- The safety layer is a backstop, not a guarantee. It has not been clinically reviewed.
- Exercises are gentle and short, and checked for risky content, but they are not clinically reviewed.
- Memories, reminders and outcomes are stored as plain text on the server, keyed only by the browser's id. Anyone with that id can read them. Fine for local testing; add real accounts and encryption before hosting this for other people.
