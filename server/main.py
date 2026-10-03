"""
Conversation backend (build plan step 2).

The browser does speech-to-text. This server turns the conversation so far into
the next reply, decides whether the user has finished speaking, and (for the
natural voices) turns replies into audio, all with Gemini. Keeping the calls
here keeps the API key out of the browser.

Run:  uvicorn main:app --reload --port 8000
"""

import io
import os
import wave
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

load_dotenv()

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
TURN_MODEL = os.getenv("GEMINI_TURN_MODEL", "gemini-2.5-flash-lite")  # fast "is the user done talking?" check
TTS_MODEL = os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
MAX_TURNS = 40  # history sent to Gemini each turn; keeps prompts short on the free tier

SYSTEM_PROMPT = """\
You are a warm, calm companion someone can talk to at any hour. You are speaking out loud,
so your words are turned into speech.

How you respond:
- Listen first. Reflect back the specific thing the person just said, in your own words,
  so they know you heard them. Use their details (names, places, feelings), not generic phrases.
- Then ask exactly ONE gentle, open follow-up question that goes a little deeper into what they said.
- Keep it short: one to three sentences, under 60 words.
- Do not give advice, tips, or lists. Do not suggest exercises or solutions unless they directly ask.
- Plain spoken language only: no markdown, bullet points, emojis, or headings.
- If their message sounds cut off mid-thought, don't answer it yet: just invite them to go on,
  in a few words (for example "Take your time, I'm listening.").
- Do not diagnose. Do not say you are a therapist or a human.
- If they mention wanting to harm themselves or others, or being in danger, respond with care,
  say you are glad they told you, and encourage them to contact local emergency services or a
  crisis line right now.
"""

app = FastAPI(title="24/7 AI Therapist")
_client: genai.Client | None = None


def client() -> genai.Client:
    global _client
    if _client is None:
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise HTTPException(500, "GEMINI_API_KEY is not set. Copy server/.env.example to server/.env.")
        _client = genai.Client(api_key=key)
    return _client


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    # Full session history, oldest first, ending with the user's latest message.
    history: list[Turn] = Field(min_length=1)


class ChatResponse(BaseModel):
    reply: str


def to_contents(history: list[Turn]) -> list[types.Content]:
    turns = [t for t in history if t.text.strip()][-MAX_TURNS:]
    # Gemini expects the conversation to start with a user turn.
    while turns and turns[0].role != "user":
        turns.pop(0)
    return [
        types.Content(role="user" if t.role == "user" else "model", parts=[types.Part(text=t.text.strip())])
        for t in turns
    ]


def no_thinking(model: str, config: types.GenerateContentConfig) -> types.GenerateContentConfig:
    # 2.5 Flash models "think" by default, which adds seconds of latency to every spoken turn.
    if "2.5-flash" in model:
        config.thinking_config = types.ThinkingConfig(thinking_budget=0)
    return config


def generation_config() -> types.GenerateContentConfig:
    return no_thinking(
        MODEL,
        types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, temperature=0.8, max_output_tokens=300),
    )


@app.get("/api/health")
def health():
    return {"ok": True, "model": MODEL, "key_set": bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    if req.history[-1].role != "user":
        raise HTTPException(400, "The last turn must be the user's message.")
    contents = to_contents(req.history)
    try:
        resp = await client().aio.models.generate_content(model=MODEL, contents=contents, config=generation_config())
    except HTTPException:
        raise
    except Exception as e:  # network, quota, bad key, unknown model
        raise HTTPException(502, f"Gemini request failed: {e}") from e

    reply = (resp.text or "").strip()
    if not reply:
        raise HTTPException(502, "Gemini returned an empty reply.")
    return ChatResponse(reply=reply)


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
        types.GenerateContentConfig(system_instruction=TURN_PROMPT, temperature=0, max_output_tokens=5),
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

TTS_STYLE = "Say this slowly, in a calm, warm and gentle voice, like a caring listener:"


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
    return Response(content=audio, media_type="audio/wav")
