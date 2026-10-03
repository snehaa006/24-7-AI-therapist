import asyncio
import os
import uuid

import pytest
from fastapi.testclient import TestClient

import main
import memory
import safety

USER = "user-" + "a" * 12
OTHER = "user-" + "b" * 12


def sid():
    return str(uuid.uuid4())


@pytest.fixture
def api(monkeypatch, tmp_path):
    """App client with a throwaway database and Gemini stubbed out."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "_client", None)
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory.db")
    state = {
        "found": {"facts": [], "patterns": [], "drop_ids": [], "summary": ""},
        "extract_calls": [],
        "greeting_calls": [],
        "reply_memories": None,
        "greeting": "Hi again. How did the interview go?",
    }

    async def fake_extract(client, model, known, turns):
        state["extract_calls"].append({"known": known, "turns": turns, "input": memory.extract_input(known, turns)})
        return state["found"]

    async def fake_greeting(client, model, user_id):
        state["greeting_calls"].append(memory.prompt_block(user_id))
        if isinstance(state["greeting"], Exception):
            raise state["greeting"]
        return state["greeting"]

    async def fake_reply(contents, memories=""):
        state["reply_memories"] = memories
        return "Tell me more."

    async def fake_label(client, model, turns):
        return "normal"

    monkeypatch.setattr(memory, "gemini_extract", fake_extract)
    monkeypatch.setattr(memory, "gemini_greeting", fake_greeting)
    monkeypatch.setattr(main, "generate_reply", fake_reply)
    monkeypatch.setattr(safety, "gemini_label", fake_label)
    return TestClient(main.app), state


def end(client, history, user=USER, session=None):
    return client.post("/api/session/end", json={"user_id": user, "session_id": session or sid(), "history": history})


TALK = [
    {"role": "assistant", "text": "Hi, I'm here, and I'm listening. What's on your mind today?"},
    {"role": "user", "text": "I have a job interview at the bakery on Friday and I'm nervous"},
    {"role": "assistant", "text": "A bakery interview on Friday. What part feels most nerve-racking?"},
]


def test_session_end_saves_facts_and_patterns(api):
    client, state = api
    state["found"] = {
        "facts": ["They have a job interview at a bakery on Friday."],
        "patterns": ["Gets nervous before big events."],
        "drop_ids": [],
        "summary": "Talked about nerves before a bakery job interview.",
    }
    assert end(client, TALK).json()["saved"] == 2
    body = client.get("/api/memories", params={"user_id": USER}).json()
    assert [(m["kind"], m["text"]) for m in body["memories"]] == [
        ("fact", "They have a job interview at a bakery on Friday."),
        ("pattern", "Gets nervous before big events."),
    ]
    assert body["last_summary"] == "Talked about nerves before a bakery job interview."
    # Other users see nothing.
    assert client.get("/api/memories", params={"user_id": OTHER}).json()["memories"] == []


def test_end_is_saved_once_per_session(api):
    """The End button and the tab-close beacon can both fire for one session."""
    client, state = api
    state["found"]["facts"] = ["Has a cat called Miso."]
    session = sid()
    end(client, TALK, session=session)
    assert end(client, TALK, session=session).json()["saved"] == 0
    assert len(state["extract_calls"]) == 1


def test_beacon_style_request(api):
    """sendBeacon posts a JSON blob with no response read; the body is the same."""
    client, state = api
    import json

    payload = json.dumps({"user_id": USER, "session_id": sid(), "history": TALK})
    res = client.post("/api/session/end", content=payload, headers={"Content-Type": "application/json"})
    assert res.status_code == 200
    assert len(state["extract_calls"]) == 1


def test_greeting_only_session_is_skipped(api):
    client, state = api
    assert end(client, TALK[:1]).json()["saved"] == 0
    assert state["extract_calls"] == []


def test_crisis_content_never_reaches_memory(api):
    client, state = api
    history = [
        *TALK,
        {"role": "user", "text": "honestly some nights I feel like there's no way out", "crisis": True},
        {"role": "assistant", "text": "I'm really glad you told me. Please call the helpline.", "crisis": True},
        {"role": "user", "text": "I want to kill myself"},  # keyword hit even if not flagged
        {"role": "assistant", "text": "Script reply that should also be dropped"},
        {"role": "user", "text": "my sister Maya is visiting next week"},
    ]
    state["found"] = {
        "facts": ["Their sister Maya visits next week.", "They have had suicidal thoughts."],
        "patterns": ["Talks about self-harm when stressed."],
        "drop_ids": [],
        "summary": "They said they want to die.",
    }
    end(client, history)
    sent = state["extract_calls"][0]["input"]
    assert "no way out" not in sent and "kill myself" not in sent
    assert "helpline" not in sent and "Script reply" not in sent
    assert "Maya" in sent and "bakery" in sent
    body = client.get("/api/memories", params={"user_id": USER}).json()
    assert [m["text"] for m in body["memories"]] == ["Their sister Maya visits next week."]
    assert body["last_summary"] == ""


def test_merge_dedupes_and_replaces(api):
    client, state = api
    state["found"] = {"facts": ["Works night shifts at a hospital.", "Lives with two flatmates."], "patterns": [], "drop_ids": [], "summary": "s1"}
    end(client, TALK)
    other = {"facts": ["Lives alone."], "patterns": [], "drop_ids": [], "summary": ""}
    state["found"] = other
    end(client, TALK, user=OTHER)
    mems = {m["text"]: m["id"] for m in client.get("/api/memories", params={"user_id": USER}).json()["memories"]}
    other_id = client.get("/api/memories", params={"user_id": OTHER}).json()["memories"][0]["id"]

    state["found"] = {
        "facts": ["works night shifts at a hospital", "Moved into a flat on their own."],
        "patterns": [],
        "drop_ids": [mems["Lives with two flatmates."], other_id],  # someone else's id is ignored
        "summary": "s2",
    }
    assert end(client, TALK).json()["saved"] == 1
    # Gemini was shown what's already known, with ids.
    assert f"[{mems['Lives with two flatmates.']}] fact: Lives with two flatmates." in state["extract_calls"][-1]["input"]
    texts = [m["text"] for m in client.get("/api/memories", params={"user_id": USER}).json()["memories"]]
    assert texts == ["Works night shifts at a hospital.", "Moved into a flat on their own."]
    assert client.get("/api/memories", params={"user_id": OTHER}).json()["memories"][0]["text"] == "Lives alone."


def test_memories_are_capped(api, monkeypatch):
    client, state = api
    monkeypatch.setattr(memory, "MAX_MEMORIES", 3)
    state["found"] = {"facts": [f"Fact number {i}." for i in range(5)], "patterns": [], "drop_ids": [], "summary": ""}
    end(client, TALK)
    texts = [m["text"] for m in client.get("/api/memories", params={"user_id": USER}).json()["memories"]]
    assert texts == ["Fact number 2.", "Fact number 3.", "Fact number 4."]


def test_memories_go_into_the_system_prompt(api):
    client, state = api
    state["found"] = {"facts": ["Their dog is called Biscuit."], "patterns": [], "drop_ids": [], "summary": "Talked about walking Biscuit."}
    end(client, TALK)
    history = [{"role": "user", "text": "hey"}]
    client.post("/api/chat", json={"history": history, "user_id": USER})
    assert "Their dog is called Biscuit." in state["reply_memories"]
    assert "Last time: Talked about walking Biscuit." in state["reply_memories"]
    client.post("/api/chat", json={"history": history})
    assert state["reply_memories"] == ""


def test_new_user_gets_the_usual_greeting(api):
    client, state = api
    assert client.post("/api/session/start", json={"user_id": USER}).json() == {"greeting": None}
    assert state["greeting_calls"] == []


def test_returning_user_gets_a_greeting_about_last_time(api):
    """Done when: the AI brings up a detail from a previous session."""
    client, state = api
    state["found"] = {
        "facts": ["They have a job interview at a bakery on Friday."],
        "patterns": [],
        "drop_ids": [],
        "summary": "Talked about nerves before the bakery interview.",
    }
    end(client, TALK)
    state["greeting"] = "Hi again. Last time you were nervous about the bakery interview. How did it go?"
    body = client.post("/api/session/start", json={"user_id": USER}).json()
    assert "bakery interview" in body["greeting"]
    assert "bakery on Friday" in state["greeting_calls"][0], "the greeting call is given the memories"


def test_greeting_falls_back_when_gemini_fails(api):
    client, state = api
    state["found"]["facts"] = ["Has a cat called Miso."]
    end(client, TALK)
    state["greeting"] = RuntimeError("quota")
    assert client.post("/api/session/start", json={"user_id": USER}).json() == {"greeting": None}


def test_delete_one_and_forget_all(api):
    client, state = api
    state["found"] = {"facts": ["A.", "B."], "patterns": ["C."], "drop_ids": [], "summary": "Sum."}
    end(client, TALK)
    mems = client.get("/api/memories", params={"user_id": USER}).json()["memories"]
    assert client.delete(f"/api/memories/{mems[0]['id']}", params={"user_id": OTHER}).status_code == 404
    assert client.delete(f"/api/memories/{mems[0]['id']}", params={"user_id": USER}).json() == {"ok": True}
    assert [m["text"] for m in client.get("/api/memories", params={"user_id": USER}).json()["memories"]] == ["B.", "C."]
    client.delete("/api/memories", params={"user_id": USER})
    assert client.get("/api/memories", params={"user_id": USER}).json() == {"memories": [], "last_summary": ""}
    assert client.post("/api/session/start", json={"user_id": USER}).json() == {"greeting": None}


def test_bad_user_id_rejected(api):
    client, _ = api
    assert client.get("/api/memories", params={"user_id": "x"}).status_code == 422
    assert client.post("/api/session/start", json={"user_id": "../../etc"}).status_code == 422


def test_opening_greeting_stays_in_the_conversation():
    turns = [main.Turn(role="assistant", text="Hi again, how did the interview go?"), main.Turn(role="user", text="it went well")]
    contents = main.to_contents(turns)
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[1].parts[0].text == "Hi again, how did the interview go?"


# ── optional live check against real Gemini ──────────────────────────────────

LIVE_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


@pytest.mark.skipif(not LIVE_KEY or LIVE_KEY in {"your-key-here", "test-key"}, reason="GEMINI_API_KEY not set")
def test_live_remembers_a_detail(monkeypatch, tmp_path):
    from google import genai

    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory.db")
    gemini = genai.Client(api_key=LIVE_KEY)
    turns = [
        main.Turn(role="user", text="My dog Biscuit has been sick all week and I've barely slept"),
        main.Turn(role="assistant", text="That sounds exhausting. What's been happening with Biscuit?"),
        main.Turn(role="user", text="The vet thinks it's his stomach, we go back on Thursday"),
    ]
    found = asyncio.run(memory.gemini_extract(gemini, main.MEMORY_MODEL, [], turns))
    memory.merge(USER, sid(), found)
    assert any("Biscuit" in m["text"] for m in memory.list_memories(USER)), found
    greeting = asyncio.run(memory.gemini_greeting(gemini, main.MEMORY_MODEL, USER))
    assert any(w in greeting.lower() for w in ("biscuit", "vet", "dog")), greeting
