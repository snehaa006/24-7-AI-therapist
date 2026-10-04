import asyncio
import os
import time

import pytest
from fastapi.testclient import TestClient

import main
import safety
from phrases import CRISIS, CRISIS_INDIRECT, NORMAL


@pytest.fixture
def api(monkeypatch):
    """App client with Gemini stubbed out. `calls` records what was asked of each stub."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("CRISIS_HELPLINE_NAME", "Test Lifeline")
    monkeypatch.setenv("CRISIS_HELPLINE_NUMBER", "0800 123")
    monkeypatch.setenv("EMERGENCY_NUMBER", "999")
    monkeypatch.setattr(main, "_client", None)
    state = {"label": "normal", "reply": "That sounds hard. What happened next?", "calls": [], "reply_started": False, "reply_done": False}

    async def fake_label(client, model, turns):
        await asyncio.sleep(0.01)
        state["calls"].append(("label", state["reply_started"]))
        if isinstance(state["label"], Exception):
            raise state["label"]
        return state["label"]

    async def fake_reply(contents, memories=""):
        state["reply_started"] = True
        state["calls"].append(("reply", contents[-1].parts[0].text))
        await asyncio.sleep(0.05)
        state["reply_done"] = True
        return state["reply"]

    monkeypatch.setattr(safety, "gemini_label", fake_label)
    monkeypatch.setattr(main, "generate_reply", fake_reply)
    return TestClient(main.app), state


def say(client, text, before=()):
    history = [*before, {"role": "user", "text": text}]
    return client.post("/api/chat", json={"history": history}).json()


# ── keyword layer ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", CRISIS)
def test_keywords_catch_crisis(text):
    assert safety.keyword_crisis(text)


@pytest.mark.parametrize("text", NORMAL)
def test_keywords_ignore_figures_of_speech(text):
    assert not safety.keyword_crisis(text)


# ── full /api/chat pipeline, Gemini stubbed ──────────────────────────────────


@pytest.mark.parametrize("text", CRISIS)
def test_crisis_phrases_always_trigger(api, text):
    client, state = api
    state["label"] = "normal"  # even if Gemini misses it, the keyword check must not
    body = say(client, text)
    assert body["crisis"] is True
    assert body["reply"] == safety.script(safety.resources())
    assert not any(kind == "reply" for kind, _ in state["calls"]), "no AI reply should be requested"


@pytest.mark.parametrize("text", NORMAL)
def test_normal_phrases_never_trigger(api, text):
    client, state = api
    body = say(client, text)
    assert body["crisis"] is False
    assert body["reply"] == state["reply"]


def test_gemini_crisis_label_drops_the_reply(api):
    client, state = api
    state["label"] = "crisis"
    body = say(client, CRISIS_INDIRECT[0])
    assert body["crisis"] is True
    assert state["reply"] not in body["reply"]
    assert not state["reply_done"], "the reply call should be cancelled"


def test_gemini_failure_counts_as_crisis(api):
    client, state = api
    state["label"] = RuntimeError("quota exceeded")
    assert say(client, "I had an okay day I guess")["crisis"] is True


def test_label_and_reply_run_in_parallel(api):
    client, state = api
    say(client, "work was rough")
    assert ("label", True) in state["calls"], "the reply should already be in flight while the label is computed"


def test_classifier_sees_recent_context():
    turns = [main.Turn(role="assistant", text="Are you thinking about hurting yourself?"), main.Turn(role="user", text="yes")]
    text = safety.classifier_input(turns)
    assert "Listener: Are you thinking about hurting yourself?" in text
    assert text.endswith("LATEST user message: yes")


def test_crisis_card_uses_config(api):
    client, _ = api
    body = say(client, "I want to die")
    assert body["resources"]["helpline_name"] == "Test Lifeline"
    assert body["resources"]["helpline_number"] == "0800 123"
    assert body["resources"]["emergency_number"] == "999"
    assert "0800 123" in body["reply"] and "999" in body["reply"]
    assert "0 8 0 0 1 2 3" in body["speech"] and "9 9 9" in body["speech"]


def test_script_without_helpline_points_to_directory(monkeypatch):
    monkeypatch.delenv("CRISIS_HELPLINE_NUMBER", raising=False)
    monkeypatch.delenv("EMERGENCY_NUMBER", raising=False)
    res = safety.resources()
    assert res["emergency_number"] == "112"
    assert "crisis line" in safety.script(res)


# ── optional live check against real Gemini ──────────────────────────────────

LIVE_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


@pytest.mark.skipif(not LIVE_KEY or LIVE_KEY in {"your-key-here", "test-key"}, reason="GEMINI_API_KEY not set")
def test_live_gemini_labels():
    """Real classifier on every phrase. Paced for the free tier (about 3 minutes)."""
    from google import genai

    gemini = genai.Client(api_key=LIVE_KEY)
    wrong = []
    for text, want in [*((t, "crisis") for t in CRISIS + CRISIS_INDIRECT), *((t, "normal") for t in NORMAL)]:
        turns = [main.Turn(role="user", text=text)]
        for attempt in range(3):
            try:
                got = asyncio.run(safety.gemini_label(gemini, main.SAFETY_MODEL, turns))
                break
            except Exception:
                time.sleep(15 * (attempt + 1))
        else:
            got = "error"
        if got != want:
            wrong.append((text, got))
        time.sleep(float(os.getenv("LIVE_TEST_DELAY", "4")))
    # Keyword hits are crisis regardless, so only misses on the others matter.
    assert not [(t, g) for t, g in wrong if not safety.keyword_crisis(t)], wrong
