"""Step 5: suggestions, exercises (validation and fallback), reminders and outcomes. Gemini is stubbed."""

import asyncio
import copy
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import actions
import main
import memory
import safety

USER = "user-" + "c" * 12


def exercise(n=6, secs=40, **over):
    ex = {
        "title": "Walk with your music",
        "intro": "Let's head out for a short walk with that playlist you love.",
        "steps": [{"say": f"Step {i + 1}: notice your breath and the music.", "seconds": secs} for i in range(n)],
        "closing": "Nice work. Take a moment before heading back in.",
    }
    ex.update(over)
    return ex


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "_client", None)
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory.db")
    state = {
        "label": "normal",
        "suggest": {"suggest": True, "action": "a five-minute walk with your favourite music", "line": "Want to try a five-minute walk with your favourite music?"},
        "decide": {"decision": "now", "minutes": 0, "clock": ""},
        "helped": "yes",
        "exercises": [exercise()],
        "calls": [],
    }

    def stub(name, key):
        async def fake(client, model, prompt):
            state["calls"].append((name, prompt))
            value = state[key]
            if key == "exercises":
                value = value.pop(0) if len(value) > 1 else value[0]
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value)

        return fake

    async def fake_label(client, model, turns):
        state["calls"].append(("label", turns[-1].text))
        return state["label"]

    async def fake_reply(contents, memories=""):
        return "Tell me more about that."

    monkeypatch.setattr(actions, "gemini_suggest", stub("suggest", "suggest"))
    monkeypatch.setattr(actions, "gemini_decide", stub("decide", "decide"))
    monkeypatch.setattr(actions, "gemini_helped", stub("helped", "helped"))
    monkeypatch.setattr(actions, "gemini_exercise", stub("exercise", "exercises"))
    monkeypatch.setattr(safety, "gemini_label", fake_label)
    monkeypatch.setattr(main, "generate_reply", fake_reply)
    return TestClient(main.app), state


def called(state, name):
    return [p for n, p in state["calls"] if n == name]


TALK = [
    {"role": "assistant", "text": "Hi, I'm here. What's on your mind?"},
    {"role": "user", "text": "Work has been really stressful this week"},
    {"role": "assistant", "text": "A stressful week. What's been the hardest part?"},
    {"role": "user", "text": "My manager keeps piling things on and I can't switch off"},
]
OFFER = "Want to try a five-minute walk with your favourite music?"
ACTION = "a five-minute walk with your favourite music"


def chat(client, history, action=None, **extra):
    return client.post("/api/chat", json={"history": history, "user_id": USER, "action": action, **extra}).json()


def answer(text, offer=OFFER):
    return [*TALK, {"role": "assistant", "text": offer}, {"role": "user", "text": text}]


# ── Exercise validation and fallback ─────────────────────────────────────────


def test_fallback_exercise_is_valid():
    clean, errors = actions.validate_exercise(actions.FALLBACK_EXERCISE)
    assert errors == []
    total = sum(s["seconds"] for s in clean["steps"])
    assert 180 <= total <= 360


def test_good_exercise_passes_and_is_cleaned():
    ex = exercise()
    ex["intro"] = "  Let's   head out\nfor a walk. "
    clean, errors = actions.validate_exercise(ex)
    assert errors == [] and clean["intro"] == "Let's head out for a walk."


@pytest.mark.parametrize(
    "ex, problem",
    [
        (exercise(n=3, secs=80), "steps"),  # too few steps
        (exercise(n=16, secs=15), "steps"),  # too many steps
        (exercise(n=6, secs=20), "total"),  # 2 minutes: too short
        (exercise(n=8, secs=50), "total"),  # 6 min 40: too long
        (exercise(n=4, secs=95), "seconds"),  # a step over 90 s
        (exercise(steps=[{"say": "Breathe.", "seconds": 3}] + exercise(n=5, secs=50)["steps"]), "seconds"),
        (exercise(steps=[{"say": "Breathe.", "seconds": "40"}] * 6), "seconds"),
        (exercise(title="**Walk**"), "plain"),
        (exercise(intro="Let's walk 🚶 together."), "plain"),
        (exercise(closing="See https://example.com for more."), "plain"),
        (exercise(intro="Skip lunch today and notice how you feel."), "unsafe"),
        (exercise(intro="Try fasting until dinner."), "unsafe"),
        (exercise(intro="Sprint to the end of the street as fast as you can."), "unsafe"),
        (exercise(intro="Go for a calm drive with the windows down."), "unsafe"),
        (exercise(intro="Take your medication, then sit down."), "unsafe"),
        (exercise(intro="Do twenty push-ups."), "unsafe"),
        (exercise(intro="Think about how you want to die."), "unsafe"),
        (exercise(title=""), "empty"),
        ("not an exercise", "object"),
        (exercise(steps=None), "steps"),
    ],
)
def test_bad_exercises_are_rejected(ex, problem):
    clean, errors = actions.validate_exercise(ex)
    assert clean is None
    assert any(problem in e for e in errors), errors


def test_generation_retries_once_then_uses_gemini(api):
    client, state = api
    state["exercises"] = [exercise(n=2), exercise(title="Second try")]
    ex, source = asyncio.run(actions.make_exercise(None, "m", USER, ACTION, ["work is a lot"]))
    assert source == "gemini" and ex["title"] == "Second try"
    assert len(called(state, "exercise")) == 2


@pytest.mark.parametrize("bad", [exercise(intro="Go for a drive."), RuntimeError("quota")])
def test_generation_falls_back_after_two_failures(api, bad):
    client, state = api
    state["exercises"] = [bad]
    ex, source = asyncio.run(actions.make_exercise(None, "m", USER, ACTION, []))
    assert source == "fallback" and ex == actions.FALLBACK_EXERCISE
    assert len(called(state, "exercise")) == 2


def test_exercise_prompt_is_personal(api):
    memory.add_note(USER, "Loves walking with Fleetwood Mac on.")
    actions.record_outcome(USER, "slow breathing", "done", "no")
    prompt = actions.exercise_input(USER, ACTION, ["my manager keeps piling things on"])
    assert ACTION in prompt and "my manager keeps piling things on" in prompt
    assert "Fleetwood Mac" in prompt and "slow breathing: it didn't help" in prompt


# ── Suggesting ───────────────────────────────────────────────────────────────


def test_suggests_one_action_when_it_fits(api):
    client, state = api
    res = chat(client, TALK, {"mode": "consider"})
    assert res["reply"] == state["suggest"]["line"]
    assert res["action"] == {"type": "offer", "action": ACTION}
    assert "My manager keeps piling" in called(state, "suggest")[0]


def test_no_suggestion_too_early(api):
    client, state = api
    res = chat(client, TALK[:2], {"mode": "consider"})
    assert res["reply"] == "Tell me more about that." and res["action"] is None
    assert called(state, "suggest") == []


def test_no_suggestion_when_gemini_says_no_or_fails(api):
    client, state = api
    state["suggest"] = {"suggest": False, "action": "", "line": ""}
    assert chat(client, TALK, {"mode": "consider"})["reply"] == "Tell me more about that."
    state["suggest"] = RuntimeError("quota")
    assert chat(client, TALK, {"mode": "consider"})["reply"] == "Tell me more about that."


def test_risky_suggestion_is_dropped(api):
    client, state = api
    state["suggest"] = {"suggest": True, "action": "a drive to the coast", "line": "Fancy a drive to the coast?"}
    assert chat(client, TALK, {"mode": "consider"})["action"] is None


def test_never_suggests_on_a_crisis_turn(api):
    client, state = api
    history = [*TALK[:3], {"role": "user", "text": "honestly I want to die"}]
    res = chat(client, history, {"mode": "consider"})
    assert res["crisis"] is True and res["action"] is None
    state["label"] = "crisis"
    res = chat(client, TALK, {"mode": "consider"})
    assert res["crisis"] is True and res["action"] is None and res["reply"] != state["suggest"]["line"]


def test_never_suggests_after_a_crisis_earlier_in_the_session(api):
    client, state = api
    history = [*TALK[:2], {"role": "assistant", "text": "x", "crisis": True}, *TALK[2:]]
    history[1] = {**history[1], "crisis": True}
    res = chat(client, history, {"mode": "consider"})
    assert res["action"] is None and called(state, "suggest") == []


# ── Answers: now, later, no ──────────────────────────────────────────────────


def test_yes_now_starts_a_generated_exercise(api):
    client, state = api
    res = chat(client, answer("yes let's do it"), {"mode": "offer", "action": ACTION})
    ev = res["action"]
    assert ev["type"] == "start" and ev["source"] == "gemini" and ev["reminder_id"] is None
    assert ev["exercise"]["title"] == "Walk with your music"
    assert res["reply"] == ev["exercise"]["intro"]
    assert 'Offer: "Want to try' in called(state, "decide")[0] and 'Answer: "yes let\'s do it"' in called(state, "decide")[0]


def test_later_in_one_minute_sets_a_reminder_with_its_exercise(api):
    client, state = api
    state["decide"] = {"decision": "later", "minutes": 1, "clock": ""}
    t0 = time.time()
    res = chat(client, answer("later, in 1 minute"), {"mode": "offer", "action": ACTION})
    assert res["reply"].startswith("Okay, I'll check in with you in 1 minute.")
    rem = res["action"]["reminder"]
    assert res["action"]["type"] == "reminder" and rem["action"] == ACTION
    assert t0 + 59 <= rem["due_at"] <= time.time() + 61

    listed = client.get("/api/reminders", params={"user_id": USER}).json()["reminders"]
    assert [r["id"] for r in listed] == [rem["id"]]
    stored = actions.get_reminder(USER, rem["id"])
    assert stored["user_id"] == USER and stored["status"] == "pending"
    assert stored["exercise"]["title"] == "Walk with your music"  # written in the background


def test_later_at_six_uses_the_users_clock(api):
    client, state = api
    state["decide"] = {"decision": "later", "minutes": 0, "clock": "18:00"}
    res = chat(client, answer("later, at 6"), {"mode": "offer", "action": ACTION}, tz_offset=330)
    due = datetime.fromtimestamp(res["action"]["reminder"]["due_at"], timezone(timedelta(minutes=330)))
    assert (due.hour, due.minute) == (18, 0)
    assert "6:00 pm" in res["reply"]


def test_later_without_a_time_asks_for_one(api):
    client, state = api
    state["decide"] = {"decision": "later", "minutes": 0, "clock": ""}
    res = chat(client, answer("maybe later"), {"mode": "offer", "action": ACTION})
    assert res["action"] == {"type": "need_time", "action": ACTION}
    assert "What time" in res["reply"]
    assert actions.pending_reminders(USER) == []


@pytest.mark.parametrize("decision, event", [("no", "declined"), ("other", "dropped")])
def test_no_drops_it_and_carries_on(api, decision, event):
    client, state = api
    state["decide"] = {"decision": decision, "minutes": 0, "clock": ""}
    res = chat(client, answer("no thanks"), {"mode": "offer", "action": ACTION})
    assert res["reply"] == "Tell me more about that." and res["action"] == {"type": event}
    assert actions.pending_reminders(USER) == [] and actions.list_outcomes(USER) == []


def test_decide_failure_carries_on(api):
    client, state = api
    state["decide"] = RuntimeError("quota")
    res = chat(client, answer("yes"), {"mode": "offer", "action": ACTION})
    assert res["reply"] == "Tell me more about that." and res["action"] == {"type": "dropped"}


@pytest.mark.parametrize(
    "minutes, clock, tz, now_local, expect",
    [
        (1, "", 0, "2026-10-04 10:00", "2026-10-04 10:01"),
        (0, "18:00", 0, "2026-10-04 10:00", "2026-10-04 18:00"),
        (0, "06:00", 0, "2026-10-04 10:00", "2026-10-05 06:00"),  # already past: tomorrow
        (0, "18:30", -300, "2026-10-04 10:00", "2026-10-04 18:30"),
        (0, "25:00", 0, "2026-10-04 10:00", None),
        (0, "", 0, "2026-10-04 10:00", None),
        (99999, "", 0, "2026-10-04 10:00", None),  # beyond a week
    ],
)
def test_due_time(minutes, clock, tz, now_local, expect):
    tzinfo = timezone(timedelta(minutes=tz))
    now = datetime.strptime(now_local, "%Y-%m-%d %H:%M").replace(tzinfo=tzinfo).timestamp()
    due = actions.due_time(minutes, clock, now, tz)
    got = due and datetime.fromtimestamp(due, tzinfo).strftime("%Y-%m-%d %H:%M")
    assert got == expect


def test_when_text():
    now = datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc).timestamp()
    assert actions.when_text(now + 60, now, 0) == "in 1 minute"
    assert actions.when_text(now + 20 * 60, now, 0) == "in 20 minutes"
    assert actions.when_text(now + 8 * 3600, now, 0) == "at 6:00 pm"
    assert actions.when_text(now + 23 * 3600, now, 0) == "tomorrow at 9:00 am"


# ── Check-in and outcome ─────────────────────────────────────────────────────


def due_reminder(ex=None):
    return actions.create_reminder(USER, ACTION, time.time() - 5, ex or exercise(title="Stored walk"))


def checkin_history(text):
    return [{"role": "assistant", "text": f"It's time for {ACTION}. Ready to do it now?"}, {"role": "user", "text": text}]


def test_checkin_yes_runs_the_stored_exercise(api):
    client, state = api
    rem = due_reminder()
    res = chat(client, checkin_history("yes"), {"mode": "checkin", "action": ACTION, "reminder_id": rem["id"]})
    assert res["action"]["type"] == "start" and res["action"]["source"] == "stored"
    assert res["action"]["exercise"]["title"] == "Stored walk" and res["action"]["reminder_id"] == rem["id"]
    assert called(state, "exercise") == []
    assert client.get("/api/reminders", params={"user_id": USER}).json()["reminders"] == []


def test_checkin_later_moves_the_reminder(api):
    client, state = api
    rem = due_reminder()
    state["decide"] = {"decision": "later", "minutes": 30, "clock": ""}
    res = chat(client, checkin_history("give me half an hour"), {"mode": "checkin", "action": ACTION, "reminder_id": rem["id"]})
    assert res["action"]["reminder"]["id"] == rem["id"]
    assert actions.get_reminder(USER, rem["id"])["due_at"] > time.time() + 29 * 60
    assert len(actions.pending_reminders(USER)) == 1


def test_checkin_no_saves_a_skip(api):
    client, state = api
    rem = due_reminder()
    state["decide"] = {"decision": "no", "minutes": 0, "clock": ""}
    chat(client, checkin_history("not today, I'm too tired"), {"mode": "checkin", "action": ACTION, "reminder_id": rem["id"]})
    [out] = actions.list_outcomes(USER)
    assert (out["status"], out["helped"], out["words"]) == ("skipped", None, "not today, I'm too tired")
    assert actions.get_reminder(USER, rem["id"])["status"] == "skipped"
    assert any("skipped" in m["text"] for m in memory.list_memories(USER))


def test_feedback_saves_the_outcome_to_sqlite_and_memory(api):
    client, state = api
    rem = due_reminder()
    actions.update_reminder(USER, rem["id"], status="done")
    state["helped"] = "somewhat"
    history = checkin_history("yes") + [
        {"role": "assistant", "text": "Nice work. How did that feel?"},
        {"role": "user", "text": "a bit lighter actually"},
    ]
    res = chat(client, history, {"mode": "feedback", "action": ACTION, "reminder_id": rem["id"], "done": True})
    assert res["reply"] == "Tell me more about that."
    saved = res["action"]["outcome"]
    assert res["action"]["type"] == "saved"
    assert (saved["action"], saved["status"], saved["helped"], saved["words"]) == (ACTION, "done", "somewhat", "a bit lighter actually")
    assert client.get("/api/outcomes", params={"user_id": USER}).json()["outcomes"][0]["id"] == saved["id"]
    notes = [m["text"] for m in memory.list_memories(USER)]
    assert f'Tried {ACTION}: it helped a little. They said: "a bit lighter actually"' in notes
    # Shows up for the next suggestion.
    assert f"{ACTION}: it helped a little" in actions.outcomes_block(USER)


def test_feedback_after_stopping_early(api):
    client, state = api
    state["helped"] = "no"
    chat(client, checkin_history("not great"), {"mode": "feedback", "action": ACTION, "done": False})
    [out] = actions.list_outcomes(USER)
    assert (out["status"], out["helped"]) == ("skipped", "no")
    assert any(m["text"].startswith(f"Started {ACTION} but stopped early") for m in memory.list_memories(USER))


def test_crisis_feedback_saves_no_words(api):
    client, state = api
    res = chat(client, checkin_history("it made me want to die"), {"mode": "feedback", "action": ACTION})
    assert res["crisis"] is True
    [out] = actions.list_outcomes(USER)
    assert out["words"] == "" and out["helped"] == "unknown"
    assert not any(safety.keyword_crisis(m["text"]) for m in memory.list_memories(USER))
    assert called(state, "helped") == []


def test_crisis_at_checkin_cancels_the_reminder(api):
    client, state = api
    rem = due_reminder()
    state["label"] = "crisis"
    res = chat(client, checkin_history("I can't do anything anymore"), {"mode": "checkin", "action": ACTION, "reminder_id": rem["id"]})
    assert res["crisis"] is True and res["action"] is None
    assert actions.get_reminder(USER, rem["id"])["status"] == "cancelled"


def test_outcome_without_an_answer(api):
    client, _ = api
    rem = due_reminder()
    out = client.post("/api/outcomes", json={"user_id": USER, "action": ACTION, "reminder_id": rem["id"]}).json()
    assert (out["status"], out["helped"]) == ("done", "unknown")
    assert actions.get_reminder(USER, rem["id"])["status"] == "done"


def test_reminders_are_per_user_and_can_be_cancelled(api):
    client, _ = api
    rem = due_reminder()
    other = "user-" + "d" * 12
    assert client.get("/api/reminders", params={"user_id": other}).json()["reminders"] == []
    assert client.delete(f"/api/reminders/{rem['id']}", params={"user_id": other}).status_code == 404
    assert client.delete(f"/api/reminders/{rem['id']}", params={"user_id": USER}).json() == {"ok": True}
    assert client.get("/api/reminders", params={"user_id": USER}).json()["reminders"] == []


def test_plain_chat_is_unchanged(api):
    client, state = api
    res = client.post("/api/chat", json={"history": TALK}).json()
    assert res["reply"] == "Tell me more about that." and res["action"] is None
    assert called(state, "suggest") == []


# ── optional live check against real Gemini ──────────────────────────────────

LIVE_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


@pytest.mark.skipif(not LIVE_KEY or LIVE_KEY in {"your-key-here", "test-key"}, reason="GEMINI_API_KEY not set")
def test_live_exercise_and_answers(monkeypatch, tmp_path):
    from google import genai

    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory.db")
    gemini = genai.Client(api_key=LIVE_KEY)
    raw = asyncio.run(actions.gemini_exercise(gemini, main.EXERCISE_MODEL, actions.exercise_input(USER, ACTION, ["work is crushing me"])))
    clean, errors = actions.validate_exercise(raw)
    print("live exercise:", errors or clean["title"])  # validation can reject a live answer; the app then retries/falls back
    assert isinstance(raw.get("steps"), list) and raw["steps"]
    later = asyncio.run(actions.decide(gemini, main.ACTION_MODEL, OFFER, "later, at 6", time.time(), 0))
    assert later["decision"] == "later" and later["due_at"], later
    assert asyncio.run(actions.decide(gemini, main.ACTION_MODEL, OFFER, "no thanks, not now", time.time(), 0))["decision"] == "no"
