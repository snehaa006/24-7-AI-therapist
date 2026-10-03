"""
Conversation backend (build plan step 2).

The browser does speech-to-text and text-to-speech; this server only turns the
conversation so far into the next spoken reply with Gemini. Keeping the call
here keeps the API key out of the browser.

Run:  uvicorn main:app --reload --port 8000
"""

import os
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

load_dotenv()

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
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


def generation_config() -> types.GenerateContentConfig:
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=0.8,
        max_output_tokens=300,
    )
    # 2.5 Flash "thinks" by default, which adds seconds of latency to every spoken turn.
    if "2.5-flash" in MODEL:
        config.thinking_config = types.ThinkingConfig(thinking_budget=0)
    return config


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
