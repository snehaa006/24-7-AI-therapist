"""Groq / Grok adapter: Gemini-style calls become OpenAI-style chat completions. No network."""

import asyncio
import json

import httpx
import pytest
from google.genai import errors, types

import compat
import keys
import safety


def groq():
    return compat.provider("groq")


def test_unknown_provider_is_none():
    assert compat.provider("gemini") is None


def test_chat_history_becomes_messages():
    contents = [
        types.Content(role="user", parts=[types.Part(text="(opens the app)")]),
        types.Content(role="model", parts=[types.Part(text="Hi, what's on your mind?")]),
        types.Content(role="user", parts=[types.Part(text="work")]),
    ]
    config = types.GenerateContentConfig(system_instruction="Be warm.", temperature=0.9, max_output_tokens=300)
    body = compat.request_body(groq(), "gemini-2.5-flash", contents, config)
    assert body["model"] == "openai/gpt-oss-120b"
    assert body["messages"] == [
        {"role": "system", "content": "Be warm."},
        {"role": "user", "content": "(opens the app)"},
        {"role": "assistant", "content": "Hi, what's on your mind?"},
        {"role": "user", "content": "work"},
    ]
    assert body["temperature"] == 0.9 and body["reasoning_effort"] == "low"
    assert body["max_tokens"] == 300 + compat.REASONING_ROOM  # room for the reasoning
    assert "response_format" not in body


def test_quick_checks_use_the_fast_model():
    assert compat.request_body(groq(), "gemini-2.5-flash-lite", "hi", None)["model"] == "openai/gpt-oss-20b"


def test_groq_json_mode_puts_schema_in_the_prompt():
    config = types.GenerateContentConfig(
        system_instruction="Label it.", response_mime_type="application/json", response_schema=safety.LABEL_SCHEMA
    )
    body = compat.request_body(groq(), "m", "hello", config)
    assert body["response_format"] == {"type": "json_object"}
    system = body["messages"][0]["content"]
    assert system.startswith("Label it.") and '"enum": ["normal", "crisis"]' in system and "JSON" in system


def test_grok_uses_json_schema_and_no_reasoning():
    config = types.GenerateContentConfig(response_mime_type="application/json", response_schema=safety.LABEL_SCHEMA)
    body = compat.request_body(compat.provider("grok"), "m", "hello", config)
    assert body["model"] == "grok-4.3" and body["reasoning_effort"] == "none"
    schema = body["response_format"]["json_schema"]["schema"]
    assert schema == {
        "type": "object",
        "properties": {"label": {"type": "string", "enum": ["normal", "crisis"]}},
        "required": ["label"],
        "additionalProperties": False,
    }


def client_with(handler) -> compat.CompatClient:
    http = httpx.AsyncClient(base_url="https://example.test/v1", transport=httpx.MockTransport(handler))
    return compat.CompatClient(groq(), "k1", http=http)


def test_reply_text_is_returned():
    def handler(request):
        assert request.headers["authorization"] == "Bearer k1"
        assert request.url.path == "/v1/chat/completions"
        assert json.loads(request.content)["messages"][-1]["content"] == "hi"
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Hey you."}}]})

    resp = asyncio.run(client_with(handler).aio.models.generate_content(model="m", contents="hi"))
    assert resp.text == "Hey you."


def test_rate_limit_becomes_a_quota_error_for_key_rotation():
    def handler(request):
        return httpx.Response(429, json={"error": {"message": "Rate limit reached for model", "type": "tokens"}})

    with pytest.raises(errors.APIError) as e:
        asyncio.run(client_with(handler).aio.models.generate_content(model="m", contents="hi"))
    assert e.value.code == 429 and keys.failure(e.value) == "quota"
    assert "Groq: Rate limit reached" in str(e.value)


def test_bad_key_is_recognised():
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "Invalid API Key"}})

    with pytest.raises(errors.APIError) as e:
        asyncio.run(client_with(handler).aio.models.generate_content(model="m", contents="hi"))
    assert keys.failure(e.value) == "bad_key"


def test_groq_keys_load_like_gemini_keys(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEYS", "g1,g2")
    monkeypatch.setenv("GROQ_API_KEY_3", "g3")
    assert keys.load_keys("GROQ_API_KEY", also=()) == ["g1", "g2", "g3"]


def test_groq_key_rotation_skips_a_rate_limited_key():
    """Same rotation as Gemini: a Groq key at its limit is rested and the next key answers."""
    used = []

    def handler(request):
        key = request.headers["authorization"].split()[-1]
        used.append(key)
        if key == "g1":
            return httpx.Response(429, json={"error": {"message": "Rate limit reached"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": f"from {key}"}}]})

    transport = httpx.MockTransport(handler)

    def make(api_key):
        return compat.CompatClient(groq(), api_key, http=httpx.AsyncClient(base_url="https://example.test/v1", transport=transport))

    pool = keys.RotatingClient(["g1", "g2"], make_client=make)
    ask = lambda: asyncio.run(pool.aio.models.generate_content(model="m", contents="hi")).text  # noqa: E731
    assert ask() == "from g2"
    assert ask() == "from g2"  # g1 is resting
    assert used == ["g1", "g2", "g2"]


def test_whisper_transcription_request():
    def handler(request):
        assert request.url.path == "/v1/audio/transcriptions"
        body = request.content
        assert b'name="model"' in body and b"whisper-large-v3-turbo" in body
        assert b'name="language"' in body and b"\r\nen\r\n" in body
        assert b"How was your day?" in body  # the listener's question, as context
        assert b'filename="turn.webm"' in body
        return httpx.Response(200, json={"text": " I made pasta today. "})

    text = asyncio.run(client_with(handler).transcribe(b"x" * 2000, "audio/webm;codecs=opus", "whisper-large-v3-turbo", "en", "How was your day?"))
    assert text == "I made pasta today."


def test_transcribe_endpoint(monkeypatch):
    import base64

    from fastapi.testclient import TestClient

    import main

    for k in ("GROQ_API_KEY", "GROQ_API_KEYS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(main, "_stt", None)
    api = TestClient(main.app)
    audio = base64.b64encode(b"x" * 2000).decode()
    assert api.get("/api/transcribe").json()["available"] is False
    assert api.post("/api/transcribe", json={"audio": audio}).status_code == 404

    monkeypatch.setenv("GROQ_API_KEY", "g1")
    calls = []

    async def fake(self, audio, mime, model, language="", prompt=""):
        calls.append((len(audio), language, prompt))
        return "I made pasta today"

    monkeypatch.setattr(compat.CompatClient, "transcribe", fake)
    r = api.post("/api/transcribe", json={"audio": audio, "lang": "en-IN", "prompt": "How was your day?"})
    assert r.json() == {"text": "I made pasta today"}
    assert calls == [(2000, "en", "How was your day?")]
    # too short to be speech: no call
    assert api.post("/api/transcribe", json={"audio": base64.b64encode(b"x").decode()}).json() == {"text": ""}


def test_retired_groq_models_in_env_are_swapped(monkeypatch):
    monkeypatch.setenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    monkeypatch.setenv("GROQ_FAST_MODEL", "llama-3.1-8b-instant")
    p = compat.provider("groq")
    assert (p.model, p.fast_model) == ("openai/gpt-oss-120b", "openai/gpt-oss-20b")


def test_no_reasoning_room_when_reasoning_is_off():
    body = compat.request_body(compat.provider("grok"), "m", "hi", types.GenerateContentConfig(max_output_tokens=20))
    assert body["max_tokens"] == 20


def test_voice_model_gets_only_the_words(monkeypatch, tmp_path):
    """Newer voice models read instructions aloud, so nothing goes in front of the text."""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    import main

    sent = []

    async def generate_content(model, contents, config):
        sent.append(contents)
        audio = SimpleNamespace(data=b"\0\0" * 100, mime_type="audio/L16;rate=24000")
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(inline_data=audio)]))])

    pool = SimpleNamespace(keys=["k"], aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    monkeypatch.setattr(main, "gemini_client", lambda: pool)
    monkeypatch.setattr(main, "TTS_CACHE", tmp_path)
    r = TestClient(main.app).post("/api/speak", json={"text": "Hi, I'm here.", "voice": "Sulafat"})
    assert r.status_code == 200 and sent == ["Hi, I'm here."]
