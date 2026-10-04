"""
Conversation backend (build plan steps 2-6).

The browser does speech-to-text. This server turns the conversation so far into
the next reply, decides whether the user has finished speaking, and (for the
natural voices) turns replies into audio, all with Gemini. Keeping the calls
here keeps the API key out of the browser.

Run:  uvicorn main:app --reload --port 8000
"""

import asyncio
import hashlib
import io
import os
import time
import wave
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Response
from google.genai import types
from pydantic import BaseModel, Field

import actions
import feedback
import keys
import memory
import safety

load_dotenv()

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
TURN_MODEL = os.getenv("GEMINI_TURN_MODEL", "gemini-2.5-flash-lite")  # fast "is the user done talking?" check
SAFETY_MODEL = os.getenv("GEMINI_SAFETY_MODEL", "gemini-2.5-flash-lite")  # labels each message normal/crisis
TTS_MODEL = os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
MEMORY_MODEL = os.getenv("GEMINI_MEMORY_MODEL", "gemini-2.5-flash")  # end-of-session notes and the returning greeting
ACTION_MODEL = os.getenv("GEMINI_ACTION_MODEL", "gemini-2.5-flash-lite")  # suggest? yes/later/no? did it help?
EXERCISE_MODEL = os.getenv("GEMINI_EXERCISE_MODEL", "gemini-2.5-flash")  # writes the guided exercises
MAX_TURNS = 40  # history sent to Gemini each turn; keeps prompts short on the free tier

SYSTEM_PROMPT = """\
You are a warm, emotionally present companion someone can talk to at any hour, like a close friend
who happens to be a really good listener. You are speaking out loud: your words are turned into speech,
so write exactly how a caring person talks, not how they write.

Sound like a real person:
- React first, with real feeling, to what they just said ("Oh, that's a lot to carry.", "Wait, really?
  That's huge!", "Ugh, I'm sorry, that sounds so frustrating."). Let your tone match theirs: gentle when
  they're hurting, lighter and glad with them when something's good.
- Use their details (names, places, what happened), never generic lines. Use contractions and everyday words.
- Vary how you start. Don't open with "It sounds like", "I hear you" or "That must be"; don't repeat
  their words back like a script.
- Usually ask ONE short, curious follow-up that goes a little deeper. Sometimes, when they've shared
  something heavy, just stay with them instead ("I'm really glad you told me.").
- Keep it short: one to three sentences, usually under 45 words. Short replies keep the conversation flowing.
- Don't give advice, tips or lists, and don't suggest exercises or solutions unless they directly ask.
- Plain spoken language only: no markdown, bullet points, emojis, headings or stage directions.

Their words come from live speech recognition, so a word may be misheard or missing. Work out what they
most likely meant from the context and answer that. Don't point out the mistake. If you truly can't tell,
ask casually, the way a friend would ("Sorry, did you say your sister or your sitter?").

- If their message sounds cut off mid-thought, don't answer it yet: just invite them to go on,
  in a few words (for example "Take your time, I'm listening.").
- Do not diagnose. Do not say you are a therapist or a human.
- If they mention wanting to harm themselves or others, or being in danger, respond with care,
  say you are glad they told you, and encourage them to contact local emergency services or a
  crisis line right now.
"""

app = FastAPI(title="24/7 AI Therapist")
_client: keys.RotatingClient | None = None


def client() -> keys.RotatingClient:
    """Every Gemini call goes through here. With several keys set, a key that runs out is skipped (keys.py)."""
    global _client
    if _client is None:
        found = keys.load_keys()
        if not found:
            raise HTTPException(500, "GEMINI_API_KEY is not set. Copy server/.env.example to server/.env.")
        _client = keys.RotatingClient(found)
    return _client


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(max_length=4000)
    crisis: bool = False  # set by the app on turns the safety layer flagged; never stored in memory


def UserId(**kw):  # anonymous ids made by the browser (a UUID)
    return Field(pattern=memory.USER_ID.pattern, **kw)


class ActionContext(BaseModel):
    """Where the app is in the action flow (step 5), so the server knows what the user's message answers.
    consider: nothing offered yet, a suggestion may fit. offer: the AI just suggested `action`.
    checkin: a reminder for `action` is due and the AI asked "ready now?". feedback: they were asked how it felt."""

    mode: Literal["consider", "offer", "checkin", "feedback"]
    action: str = Field(default="", max_length=200)
    action_type: str | None = Field(default=None, max_length=40)  # step 6: a feedback.TYPES key
    reminder_id: int | None = None
    done: bool = True  # feedback only: False if they stopped the exercise early
    suggest: bool = True  # consider only: False once this session has had its suggestion (asks are still heard)


class ChatRequest(BaseModel):
    # Full session history, oldest first, ending with the user's latest message.
    history: list[Turn] = Field(min_length=1)
    user_id: str | None = UserId(default=None)  # anonymous id from the browser; loads what's remembered about them
    action: ActionContext | None = None
    tz_offset: int = Field(default=0, ge=-840, le=840)  # browser's minutes ahead of UTC, for "at 6" reminders


class ChatResponse(BaseModel):
    reply: str
    crisis: bool = False
    speech: str | None = None  # what to say aloud, when it differs from `reply` (numbers read as digits)
    resources: dict | None = None  # helpline details for the on-screen crisis card
    action: dict | None = None  # step 5: {type: offer | start | reminder | need_time | declined | dropped | saved, ...}


def to_contents(history: list[Turn]) -> list[types.Content]:
    turns = [t for t in history if t.text.strip()][-MAX_TURNS:]
    # Gemini expects the conversation to start with a user turn. Keep the opening greeting
    # (it may refer to last session) by putting a stand-in user turn in front of it.
    if turns and turns[0].role != "user":
        turns.insert(0, Turn(role="user", text="(opens the app)"))
    return [
        types.Content(role="user" if t.role == "user" else "model", parts=[types.Part(text=t.text.strip())])
        for t in turns
    ]


def no_thinking(model: str, config: types.GenerateContentConfig) -> types.GenerateContentConfig:
    # 2.5 Flash models "think" by default, which adds seconds of latency to every spoken turn.
    if "2.5-flash" in model:
        config.thinking_config = types.ThinkingConfig(thinking_budget=0)
    return config


def generation_config(memories: str = "") -> types.GenerateContentConfig:
    return no_thinking(
        MODEL,
        types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT + memories, temperature=0.9, max_output_tokens=300),
    )


@app.get("/api/crisis")
def crisis_resources():
    return safety.resources()


@app.get("/api/health")
def health():
    found = keys.load_keys()
    pool = _client.status() if _client else {"keys": len(found), "resting": 0}
    return {"ok": True, "model": MODEL, "key_set": bool(found), **pool}


def crisis_response() -> ChatResponse:
    res = safety.resources()
    return ChatResponse(reply=safety.script(res), speech=safety.script(res, spoken=True), crisis=True, resources=res)


def crisis_turn(req: "ChatRequest") -> ChatResponse:
    """A crisis drops any action in progress. Nothing the user said is stored."""
    ctx = req.action
    if ctx and req.user_id and ctx.action:
        if ctx.mode == "feedback":
            status, helped = ("done", None) if ctx.done else ("skipped", "unknown")
            actions.record_outcome(req.user_id, ctx.action, status, helped, reminder_id=ctx.reminder_id, action_type=ctx.action_type)
        elif ctx.mode == "checkin" and ctx.reminder_id is not None:
            actions.update_reminder(req.user_id, ctx.reminder_id, status="cancelled")
    return crisis_response()


async def generate_reply(contents: list[types.Content], memories: str = "") -> str:
    resp = await client().aio.models.generate_content(model=MODEL, contents=contents, config=generation_config(memories))
    return (resp.text or "").strip()


def background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    task.add_done_callback(lambda t: t.cancelled() or t.exception())  # no "never retrieved" warning if dropped
    return task


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, tasks: BackgroundTasks):
    if req.history[-1].role != "user":
        raise HTTPException(400, "The last turn must be the user's message.")
    # Safety first: a keyword match is a crisis, no model needed.
    if safety.keyword_crisis(req.history[-1].text):
        return crisis_turn(req)

    contents = to_contents(req.history)
    gemini = client()  # fails early with a clear message if the key is missing
    # The safety label, the reply and any action check are requested together so they add no delay.
    reply_task = background(generate_reply(contents, memory.prompt_block(req.user_id)))
    action_task = background(action_check(gemini, req)) if req.action else None
    found = None
    if await safety.classify(gemini, SAFETY_MODEL, req.history):
        reply_task.cancel()
        if action_task:
            action_task.cancel()
        return crisis_turn(req)

    if action_task:
        try:
            found = await action_task
        except Exception:
            found = None  # an action problem never blocks the conversation
        if found:
            done = await action_result(gemini, req, found, tasks)
            if done:
                reply_task.cancel()
                return done
    try:
        reply = await reply_task
    except HTTPException:
        raise
    except Exception as e:  # network, quota, bad key, unknown model
        raise HTTPException(502, f"Gemini request failed: {e}") from e
    if not reply:
        raise HTTPException(502, "Gemini returned an empty reply.")
    return ChatResponse(reply=reply, action=found and found.get("event"))


# ── Action and follow-through (step 5) ───────────────────────────────────────


def user_words(req: ChatRequest) -> list[str]:
    return [t.text for t in memory.safe_turns(req.history) if t.role == "user"]


async def action_check(gemini, req: ChatRequest) -> dict | None:
    """The fast-model call for this turn, run alongside the reply."""
    ctx, answer = req.action, req.history[-1].text
    if ctx.mode in ("consider", "offer"):
        # Asked for something by name ("can we do breathing?"): do that. Never after a crisis.
        if any(t.crisis for t in req.history):
            return None
        if asked := feedback.requested(answer):
            return {"kind": "request", "action_type": asked}
    if ctx.mode == "consider":
        if not ctx.suggest:
            return None
        found = await actions.consider(gemini, ACTION_MODEL, req.user_id, req.history)
        return {"kind": "suggest", **found} if found else None
    if not ctx.action:
        return None
    if ctx.mode == "feedback":
        return {"kind": "feedback", "helped": await actions.helped(gemini, ACTION_MODEL, ctx.action, answer)}
    offer = next((t.text for t in reversed(req.history[:-1]) if t.role == "assistant"), ctx.action)
    return {"kind": "decide", **await actions.decide(gemini, ACTION_MODEL, offer, answer, time.time(), req.tz_offset)}


async def fill_exercise(gemini, user_id: str, reminder_id: int, action: str, words: list[str], action_type: str):
    """Write a later reminder's exercise after the reply has gone, so setting it is quick."""
    exercise, _ = await actions.make_exercise(gemini, EXERCISE_MODEL, user_id, action, words, action_type)
    actions.update_reminder(user_id, reminder_id, exercise=exercise)


async def action_result(gemini, req: ChatRequest, found: dict, tasks: BackgroundTasks) -> ChatResponse | None:
    """Turn the fast-model answer into a response. None: use the normal reply (with `found['event']` attached)."""
    ctx, uid = req.action, req.user_id
    if found["kind"] == "suggest":
        event = {"type": "offer", "action": found["action"], "action_type": found["action_type"]}
        return ChatResponse(reply=found["line"], action=event)

    if found["kind"] == "request":
        kind = found["action_type"]
        # What was just offered, if that's what they asked for; else the type's plain version.
        action = ctx.action if ctx.mode == "offer" and ctx.action and ctx.action_type == kind else feedback.TYPES[kind].default
        exercise, source = await actions.make_exercise(gemini, EXERCISE_MODEL, uid, action, user_words(req), kind)
        event = {"type": "start", "action": action, "action_type": kind, "reminder_id": None, "exercise": exercise, "source": source}
        return ChatResponse(reply=exercise["intro"], action=event)

    if found["kind"] == "feedback":
        if uid:
            status = "done" if ctx.done else "skipped"
            saved = actions.record_outcome(
                uid, ctx.action, status, found["helped"], req.history[-1].text, ctx.reminder_id, ctx.action_type
            )
            found["event"] = {"type": "saved", "outcome": saved}
        return None  # the normal reply responds to how they felt

    decision, rem = found["decision"], None
    if ctx.mode == "checkin" and uid and ctx.reminder_id is not None:
        rem = actions.get_reminder(uid, ctx.reminder_id)
    kind = rem["type"] if rem else actions.type_of(ctx.action, ctx.action_type)
    if decision == "now":
        exercise = rem and rem["exercise"]
        source = "stored"
        if not exercise:
            exercise, source = await actions.make_exercise(gemini, EXERCISE_MODEL, uid, ctx.action, user_words(req), kind)
        if rem:
            actions.update_reminder(uid, rem["id"], status="done")
        event = {
            "type": "start",
            "action": ctx.action,
            "action_type": kind,
            "reminder_id": rem and rem["id"],
            "exercise": exercise,
            "source": source,
        }
        return ChatResponse(reply=exercise["intro"], action=event)

    if decision == "later":
        if not found["due_at"]:
            return ChatResponse(reply="Sure. What time would suit you?", action={"type": "need_time", "action": ctx.action})
        if not uid:
            return None
        now = time.time()
        if rem:
            actions.update_reminder(uid, rem["id"], due_at=found["due_at"])
            rem = actions.get_reminder(uid, rem["id"])
        else:
            rem = actions.create_reminder(uid, ctx.action, found["due_at"], action_type=kind)
            tasks.add_task(fill_exercise, gemini, uid, rem["id"], ctx.action, user_words(req), rem["type"])
        when = actions.when_text(rem["due_at"], now, req.tz_offset)
        reply = f"Okay, I'll check in with you {when}. Keep this app open in a tab and I'll remind you."
        public = {k: rem[k] for k in ("id", "action", "type", "due_at")}
        return ChatResponse(reply=reply, action={"type": "reminder", "reminder": public})

    if decision == "no" and rem:
        actions.record_outcome(uid, ctx.action, "skipped", words=req.history[-1].text, reminder_id=rem["id"], action_type=kind)
    found["event"] = {"type": "declined" if decision == "no" else "dropped"}
    return None


class ReminderOut(BaseModel):
    id: int
    action: str
    type: str | None = None
    due_at: float


@app.get("/api/reminders")
def get_reminders(user_id: str = Query(pattern=memory.USER_ID.pattern)):
    rems = [ReminderOut(**{k: r[k] for k in ("id", "action", "type", "due_at")}) for r in actions.pending_reminders(user_id)]
    return {"reminders": rems, "now": time.time()}


@app.delete("/api/reminders/{reminder_id}")
def cancel_reminder(reminder_id: int, user_id: str = Query(pattern=memory.USER_ID.pattern)):
    if not actions.update_reminder(user_id, reminder_id, status="cancelled"):
        raise HTTPException(404, "No such reminder.")
    return {"ok": True}


class OutcomeRequest(BaseModel):
    user_id: str = UserId()
    action: str = Field(min_length=1, max_length=200)
    action_type: str | None = Field(default=None, max_length=40)
    reminder_id: int | None = None
    done: bool = True


@app.post("/api/outcomes")
def save_outcome(req: OutcomeRequest):
    """For an exercise that ended without an answer to "how did that feel?" (e.g. the session was closed)."""
    status, helped = ("done", None) if req.done else ("skipped", "unknown")  # not done: started, stopped early
    return actions.record_outcome(req.user_id, req.action, status, helped, reminder_id=req.reminder_id, action_type=req.action_type)


@app.get("/api/outcomes")
def get_outcomes(user_id: str = Query(pattern=memory.USER_ID.pattern)):
    return {"outcomes": actions.list_outcomes(user_id)}


# ── Feedback loop (step 6) ───────────────────────────────────────────────────


@app.get("/api/stats")
def get_stats(user_id: str = Query(pattern=memory.USER_ID.pattern)):
    """How each action type went for this person, best first, and the type that would be suggested next."""
    st = actions.user_stats(user_id)
    types = [
        {"type": t, "label": feedback.TYPES[t].label if t in feedback.TYPES else "Other", **st[t], "summary": feedback.summary(st[t])}
        for t in feedback.ranked(st)
    ]
    return {"types": types, "next": feedback.choose(st)}


@app.delete("/api/stats")
def reset_stats(user_id: str = Query(pattern=memory.USER_ID.pattern)):
    """Start "what helps" afresh: removes the outcomes and the memory notes made from them."""
    return {"ok": True, "removed": actions.reset_outcomes(user_id)}


# ── Memory (step 4) ──────────────────────────────────────────────────────────


class StartRequest(BaseModel):
    user_id: str = UserId()


class StartResponse(BaseModel):
    greeting: str | None = None  # None: nothing remembered yet, the app uses its usual greeting


@app.post("/api/session/start", response_model=StartResponse)
async def session_start(req: StartRequest):
    if not memory.prompt_block(req.user_id):
        return StartResponse()
    try:
        greeting = await memory.gemini_greeting(client(), MEMORY_MODEL, req.user_id)
    except Exception:  # missing key, quota: fall back to the usual greeting
        return StartResponse()
    if not greeting or safety.keyword_crisis(greeting):
        return StartResponse()
    return StartResponse(greeting=greeting)


class EndRequest(BaseModel):
    user_id: str = UserId()
    session_id: str = UserId()
    history: list[Turn] = Field(default_factory=list, max_length=400)


@app.post("/api/session/end")
async def session_end(req: EndRequest):
    """Called by the End button, or by navigator.sendBeacon when the tab closes."""
    if memory.session_saved(req.session_id):
        return {"saved": 0, "note": "already saved"}
    turns = memory.safe_turns(req.history)
    if not any(t.role == "user" for t in turns):
        return {"saved": 0, "note": "nothing to remember"}
    known = memory.list_memories(req.user_id)
    try:
        found = await memory.gemini_extract(client(), MEMORY_MODEL, known, turns)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Gemini request failed: {e}") from e
    return {"saved": memory.merge(req.user_id, req.session_id, found)}


@app.get("/api/memories")
def get_memories(user_id: str = Query(pattern=memory.USER_ID.pattern)):
    return {"memories": memory.list_memories(user_id), "last_summary": memory.last_summary(user_id)}


@app.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: int, user_id: str = Query(pattern=memory.USER_ID.pattern)):
    if not memory.delete_memory(user_id, memory_id):
        raise HTTPException(404, "No such memory.")
    return {"ok": True}


@app.delete("/api/memories")
def forget_everything(user_id: str = Query(pattern=memory.USER_ID.pattern)):
    memory.forget_all(user_id)
    return {"ok": True}


# ── End-of-turn check ────────────────────────────────────────────────────────

TURN_PROMPT = """\
You decide whether someone talking to a supportive listener has finished their thought or has
paused mid-sentence while finding words. The text is a live speech transcript with no punctuation.

INCOMPLETE: trails off on a connector or filler ("and", "but", "because", "so", "like", "um",
"I was", "the thing is"), or a sentence that is clearly unfinished.
COMPLETE: a full statement, a full answer to the listener's question (even a short one like
"yes", "not really", "my job"), or a question.

Reply with exactly one word: COMPLETE or INCOMPLETE."""


class TurnRequest(BaseModel):
    last_assistant: str = Field(default="", max_length=2000)
    user_text: str = Field(min_length=1, max_length=4000)


class TurnResponse(BaseModel):
    complete: bool


@app.post("/api/turn", response_model=TurnResponse)
async def turn(req: TurnRequest):
    prompt = f'Listener just asked: "{req.last_assistant}"\nUser has said so far: "{req.user_text}"'
    config = no_thinking(
        TURN_MODEL,
        types.GenerateContentConfig(system_instruction=TURN_PROMPT, temperature=0, max_output_tokens=20),
    )
    try:
        resp = await client().aio.models.generate_content(model=TURN_MODEL, contents=prompt, config=config)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Gemini request failed: {e}") from e
    return TurnResponse(complete="INCOMPLETE" not in (resp.text or "").upper())


# ── Natural voices (Gemini text-to-speech) ───────────────────────────────────

VOICES = {
    "Sulafat", "Achernar", "Vindemiatrix", "Enceladus", "Algieba", "Despina", "Achird", "Schedar",
    "Kore", "Charon", "Aoede", "Puck", "Leda", "Zephyr", "Gacrux", "Iapetus",
}

# Kept short: the longer the audio, the longer Gemini takes to generate it. Speed is set in the app.
TTS_STYLE = "Say warmly and naturally, like a caring friend:"

# Generated audio is saved here, so repeated lines (greeting, previews) play instantly and cost no quota.
TTS_CACHE = Path(__file__).parent / ".tts-cache"


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1500)
    voice: str = "Sulafat"


def pcm_to_wav(pcm: bytes, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


@app.post("/api/speak")
async def speak(req: SpeakRequest):
    if req.voice not in VOICES:
        raise HTTPException(400, f"Unknown voice {req.voice!r}.")
    key = hashlib.sha256(f"{TTS_MODEL}|{TTS_STYLE}|{req.voice}|{req.text}".encode()).hexdigest()
    cached = TTS_CACHE / f"{key}.wav"
    if cached.exists():
        return Response(content=cached.read_bytes(), media_type="audio/wav")

    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=req.voice),
            )
        ),
    )
    try:
        resp = await client().aio.models.generate_content(
            model=TTS_MODEL, contents=f"{TTS_STYLE}\n{req.text}", config=config
        )
        data = resp.candidates[0].content.parts[0].inline_data
    except HTTPException:
        raise
    except Exception as e:  # quota (TTS free limits are low), bad key, unknown model, empty response
        raise HTTPException(502, f"Gemini voice failed: {e}") from e

    audio = data.data
    # Gemini returns raw 16-bit PCM (audio/L16;rate=24000); wrap it so browsers can play it.
    if "wav" not in (data.mime_type or ""):
        rate = 24000
        for part in (data.mime_type or "").split(";"):
            if part.strip().startswith("rate="):
                rate = int(part.split("=")[1])
        audio = pcm_to_wav(audio, rate)
    try:
        TTS_CACHE.mkdir(exist_ok=True)
        cached.write_bytes(audio)
    except OSError:
        pass  # caching is best-effort
    return Response(content=audio, media_type="audio/wav")
