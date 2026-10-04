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
    assert body["model"] == "llama-3.3-70b-versatile"
    assert body["messages"] == [
        {"role": "system", "content": "Be warm."},
        {"role": "user", "content": "(opens the app)"},
        {"role": "assistant", "content": "Hi, what's on your mind?"},
        {"role": "user", "content": "work"},
    ]
    assert body["temperature"] == 0.9 and body["max_tokens"] == 300
    assert "reasoning_effort" not in body and "response_format" not in body


def test_quick_checks_use_the_fast_model():
    assert compat.request_body(groq(), "gemini-2.5-flash-lite", "hi", None)["model"] == "llama-3.1-8b-instant"


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
