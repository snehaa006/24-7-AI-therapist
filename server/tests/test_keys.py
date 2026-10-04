"""Gemini key rotation: keys from the environment, round-robin, and skipping keys that run out."""

import asyncio
from types import SimpleNamespace

import pytest
from google.genai import errors

import keys


def api_error(code: int, message: str = "") -> errors.APIError:
    return errors.APIError(code, {"error": {"code": code, "message": message, "status": ""}})


class FakePool:
    """Stands in for genai.Client. `fail[key]` is a list of errors that key raises, one per call."""

    def __init__(self, fail=None):
        self.fail = fail or {}
        self.calls = []

    def __call__(self, api_key):
        async def generate_content(**kwargs):
            self.calls.append(api_key)
            if self.fail.get(api_key):
                raise self.fail[api_key].pop(0)
            return f"ok from {api_key}"

        return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))


def run(client, **kw):
    return asyncio.run(client.aio.models.generate_content(model="m", contents="hi", **kw))


@pytest.fixture
def clean_env(monkeypatch):
    for k in ("GEMINI_API_KEYS", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GEMINI_API_KEY_10"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_load_keys_from_every_form(clean_env):
    clean_env.setenv("GEMINI_API_KEYS", "a, b\nc")
    clean_env.setenv("GEMINI_API_KEY", "b")  # duplicate dropped
    clean_env.setenv("GEMINI_API_KEY_10", "z")
    clean_env.setenv("GEMINI_API_KEY_2", "d")
    assert keys.load_keys() == ["a", "b", "c", "d", "z"]


def test_placeholder_is_not_a_key(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "your-key-here")
    assert keys.load_keys() == []


def test_round_robin_spreads_calls():
    pool = FakePool()
    c = keys.RotatingClient(["a", "b", "c"], make_client=pool)
    for _ in range(4):
        run(c)
    assert pool.calls == ["a", "b", "c", "a"]


def test_quota_error_moves_to_next_key_and_rests_the_key():
    pool = FakePool({"a": [api_error(429, "Resource has been exhausted")]})
    c = keys.RotatingClient(["a", "b"], make_client=pool)
    assert run(c) == "ok from b"
    assert c.status() == {"keys": 2, "resting": 1}
    # a is resting, so b answers the next calls too
    run(c)
    run(c)
    assert pool.calls == ["a", "b", "b", "b"]


def test_invalid_key_is_skipped():
    pool = FakePool({"a": [api_error(400, "API key not valid. Please pass a valid API key.")]})
    c = keys.RotatingClient(["a", "b"], make_client=pool)
    assert run(c) == "ok from b"
    assert c.status()["resting"] == 1


def test_overloaded_model_tries_another_key_without_resting():
    pool = FakePool({"a": [api_error(503, "The model is overloaded.")]})
    c = keys.RotatingClient(["a", "b"], make_client=pool)
    assert run(c) == "ok from b"
    assert c.status()["resting"] == 0


def test_other_errors_are_raised_at_once():
    pool = FakePool({"a": [api_error(400, "Invalid JSON payload")]})
    c = keys.RotatingClient(["a", "b"], make_client=pool)
    with pytest.raises(errors.APIError):
        run(c)
    assert pool.calls == ["a"]


def test_all_keys_out_raises_the_last_error():
    pool = FakePool({"a": [api_error(429)], "b": [api_error(429)]})
    c = keys.RotatingClient(["a", "b"], make_client=pool)
    with pytest.raises(errors.APIError):
        run(c)
    # every key resting: only the one back soonest is tried, so it fails (or works) fast
    assert run(c) == "ok from a"
    assert pool.calls == ["a", "b", "a"]


def test_quota_is_per_model():
    """A key out of text-to-speech quota still answers chat."""
    pool = FakePool({"a": [api_error(429)]})
    c = keys.RotatingClient(["a"], make_client=pool)
    with pytest.raises(errors.APIError):
        asyncio.run(c.aio.models.generate_content(model="tts", contents="hi"))
    assert c.status("tts")["resting"] == 1 and c.status("chat")["resting"] == 0
    assert asyncio.run(c.aio.models.generate_content(model="chat", contents="hi")) == "ok from a"


def test_rest_follows_googles_retry_delay():
    err = errors.APIError(
        429,
        {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED",
                   "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "58144s"}]}},
    )
    assert keys.retry_delay(err) == 58144
    c = keys.RotatingClient(["a", "b"], make_client=FakePool({"a": [err]}))
    run(c)
    assert c._rest_until[(0, "m")] - c._rest_until.get((1, "m"), 0) > 58000
    msg = keys.quota_message(err, "Natural voice", 2)
    assert "16 hours" in msg and "same project" in msg and "{" not in msg


def test_rest_doubles_on_repeated_quota_errors():
    pool = FakePool({"a": [api_error(429), api_error(429)]})
    c = keys.RotatingClient(["a"], make_client=pool)
    for _ in range(2):
        with pytest.raises(errors.APIError):
            run(c)
    assert c._strikes[(0, "m")] == 2
    c._rest_until.clear()
    run(c)
    assert (0, "m") not in c._strikes


def test_gemini_tuning_per_model_family():
    from google.genai import types

    base = types.GenerateContentConfig(temperature=0, max_output_tokens=200)
    old = keys.gemini_tuning("gemini-2.5-flash-lite", base)
    assert old.thinking_config.thinking_budget == 0 and old.temperature == 0

    lite = keys.gemini_tuning("gemini-3.5-flash-lite", base)
    assert lite.thinking_config.thinking_level == types.ThinkingLevel.MINIMAL
    assert lite.temperature is None and lite.max_output_tokens == 200

    flash = keys.gemini_tuning("gemini-3.8-flash", base)
    assert flash.thinking_config.thinking_level == types.ThinkingLevel.LOW  # 3.8 Flash rejects minimal
    assert flash.max_output_tokens == 200 + keys.THINKING_ROOM

    assert base.thinking_config is None, "the caller's config is not changed"
    assert keys.gemini_tuning("gemini-3.8-flash-tts", base) is base


def test_pool_applies_prepare_to_each_call():
    seen = []

    def make(api_key):
        async def generate_content(**kw):
            seen.append(kw["config"])
            return "ok"

        return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))

    c = keys.RotatingClient(["a"], make_client=make, prepare=lambda model, cfg: f"{model}:{cfg}")
    asyncio.run(c.aio.models.generate_content(model="m", contents="hi", config="cfg"))
    assert seen == ["m:cfg"]


def test_model_retired_for_one_key_tries_the_next():
    gone = api_error(404, "This model models/gemini-2.5-flash-lite is no longer available to new users.")
    pool = FakePool({"a": [gone]})
    c = keys.RotatingClient(["a", "b"], make_client=pool)
    assert run(c) == "ok from b"
    assert c.status("m")["resting"] == 1 and c.status("other")["resting"] == 0


def test_key_whose_account_needs_terms_acceptance_is_skipped():
    terms = api_error(400, "Groq: The model `canopylabs/orpheus-v1-english` requires terms acceptance.")
    c = keys.RotatingClient(["a", "b"], make_client=FakePool({"a": [terms]}))
    assert run(c) == "ok from b"
