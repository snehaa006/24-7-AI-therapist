"""Step 6: action types, per-type stats, picking the next type, the migration and the stats endpoint.
Gemini is stubbed (the `api` fixture from test_actions), so everything here is deterministic."""

import asyncio
import os
import sqlite3

import pytest

import actions
import feedback
import main
import memory
from test_actions import TALK, USER, api, called, chat  # noqa: F401  (api is a fixture)


def out(type_, helped="yes", status="done", t=0):
    return {"type": type_, "action": type_, "status": status, "helped": helped, "created_at": t}


def history(*items):
    """Outcomes oldest first: ('walk', 'yes'), ('breathing', 'no', 'skipped') …"""
    return [out(i[0], i[1], i[2] if len(i) > 2 else "done", t=n) for n, i in enumerate(items)]


# ── Scoring ──────────────────────────────────────────────────────────────────


def test_no_history_picks_the_first_configured_type():
    st = feedback.stats([])
    assert set(st) == set(feedback.TYPES)
    assert all(s["tries"] == 0 and s["score"] == feedback.PRIOR + feedback.EXPLORE_BONUS for s in st.values())
    assert feedback.choose(st) == next(iter(feedback.TYPES))
    assert feedback.history_line(st) == ""


def test_walk_helped_and_breathing_did_not_means_walk():
    st = feedback.stats(history(("walk", "yes"), ("breathing", "no")))
    assert feedback.choose(st) == "walk"
    assert st["walk"]["score"] > st["grounding"]["score"] > st["breathing"]["score"]
    assert feedback.history_line(st) == "Worked for them: walk (helped 1 of 1). Didn't work: breathing (helped 0 of 1)."


def test_recent_outcomes_count_more():
    # Same results, opposite order: the one that helped most recently comes out ahead.
    st = feedback.stats(history(("walk", "no"), ("music", "yes"), ("walk", "yes"), ("music", "no")))
    assert st["walk"]["helped"] == st["music"]["helped"] == 1
    assert st["walk"]["score"] > st["music"]["score"]
    assert feedback.ranked(st).index("walk") < feedback.ranked(st).index("music")


def test_helped_values():
    st = feedback.stats(history(("walk", "yes"), ("walk", "somewhat"), ("walk", "no"), ("walk", "unknown")))
    s = st["walk"]
    assert (s["tries"], s["helped"], s["yes"], s["somewhat"], s["no"], s["unknown"]) == (4, 1.5, 1, 1, 1, 1)


def test_skipped_and_stopped_early():
    rows = history(("stretch", None, "skipped"), ("stretch", "no", "skipped"), ("stretch", "yes"))
    rows[-1]["created_at"] = 42
    s = feedback.stats(rows)["stretch"]
    assert (s["tries"], s["skipped"], s["stopped"], s["last_tried"]) == (2, 1, 1, 42)
    assert feedback.summary(s) == "helped 1 of 2, stopped early 1, skipped 1"
    # Only skipped: not tried yet, but no longer at the untried level either.
    only = feedback.stats(history(("stretch", None, "skipped")))
    assert only["stretch"]["tries"] == 0 and feedback.summary(only["stretch"]) == "not tried yet, skipped 1"
    assert only["stretch"]["score"] < only["walk"]["score"]


def test_exploration_bonus_beats_a_somewhat_but_not_a_yes():
    st = feedback.stats(history(("breathing", "somewhat")))
    assert feedback.choose(st) == "walk"  # untried, next in config order
    assert st["walk"]["score"] - st["breathing"]["score"] == pytest.approx(feedback.EXPLORE_BONUS)
    st = feedback.stats(history(("breathing", "yes")))
    assert feedback.choose(st) == "breathing"


def test_explores_untried_types_before_returning_to_a_middling_one():
    tried = []
    rows = []
    for n in range(len(feedback.TYPES) + 1):
        pick = feedback.choose(feedback.stats(rows))
        tried.append(pick)
        rows.append(out(pick, "somewhat", t=n))
    assert tried[: len(feedback.TYPES)] == list(feedback.TYPES)  # each type once
    assert tried[-1] == "breathing"  # then the one tried longest ago


def test_two_nos_in_a_row_are_never_picked():
    # Breathing helped a lot before, but the last two tries didn't.
    rows = history(*[("breathing", "yes")] * 6, ("breathing", "no"), ("breathing", "no"))
    st = feedback.stats(rows)
    assert st["breathing"]["blocked"]
    for t in feedback.TYPES:  # make breathing still score best, so only the rule keeps it out
        if t != "breathing":
            st[t]["score"] = st["breathing"]["score"] - 0.01
    assert feedback.choose(st) != "breathing"


def test_one_no_or_a_no_then_yes_is_not_blocked():
    assert not feedback.stats(history(("walk", "no")))["walk"]["blocked"]
    assert not feedback.stats(history(("walk", "no"), ("walk", "no"), ("walk", "yes")))["walk"]["blocked"]
    # A skip in between isn't a try, so it doesn't break the run of two "no"s.
    assert feedback.stats(history(("walk", "no"), ("walk", None, "skipped"), ("walk", "no")))["walk"]["blocked"]


def test_when_every_type_failed_twice_the_best_is_still_picked():
    rows = []
    for t in feedback.TYPES:
        rows += [out(t, "no"), out(t, "no")]
    rows = [out("walk", "yes")] + rows  # walk helped once, long ago
    rows = [{**r, "created_at": n} for n, r in enumerate(rows)]
    st = feedback.stats(rows)
    assert all(st[t]["blocked"] for t in feedback.TYPES)
    assert feedback.choose(st) == "walk"


def test_other_is_shown_but_never_picked():
    rows = [out("other", "yes", t=n) for n in range(5)]
    st = feedback.stats(rows)
    assert st["other"]["tries"] == 5 and feedback.ranked(st)[0] == "other"
    assert feedback.choose(st) == "breathing"


@pytest.mark.parametrize(
    "action, expect",
    [
        ("a five-minute walk with your favourite music", "walk"),
        ("slow breathing", "breathing"),
        ("box breathing for two minutes", "breathing"),
        ("a gentle neck stretch", "stretch"),
        ("writing three lines about your day", "journaling"),
        ("texting your sister", "reach_out"),
        ("the 5-4-3-2-1 grounding exercise", "grounding"),
        ("put on a song you love", "music"),
        ("a short nap", "rest"),
        ("tidy your desk", "other"),
    ],
)
def test_keyword_labels(action, expect):
    assert feedback.label(action) == expect


@pytest.mark.parametrize(
    "text, expect",
    [
        ("can we do breathing?", "breathing"),
        ("Could we try some breathing", "breathing"),
        ("let’s go for a walk", "walk"),
        ("can you guide me through a quick stretch", "stretch"),
        ("how about a song", "music"),
        ("I'd like to do some journaling", "journaling"),
        ("can we do a grounding exercise", "grounding"),
        ("I tried breathing and it did nothing", None),
        ("walking home I felt awful", None),
        ("I just want to rest and not think", None),
        ("can we not do breathing", None),
        ("can we talk about my mum", None),
    ],
)
def test_requests(text, expect):
    assert feedback.requested(text) == expect


# ── Migration ────────────────────────────────────────────────────────────────

STEP5_SCHEMA = """
CREATE TABLE reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, action TEXT NOT NULL,
    exercise TEXT, due_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL);
CREATE TABLE outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, reminder_id INTEGER,
    action TEXT NOT NULL, status TEXT NOT NULL, helped TEXT, words TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
"""


def test_migration_adds_and_labels_the_type_once(monkeypatch, tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(STEP5_SCHEMA)
        conn.execute("INSERT INTO reminders (user_id, action, due_at, created_at) VALUES (?, 'a short stretch', 1, 1)", (USER,))
        for i, (action, helped) in enumerate([("a walk with music", "yes"), ("slow breathing", "no"), ("tidy your desk", "yes")]):
            conn.execute(
                "INSERT INTO outcomes (user_id, action, status, helped, created_at) VALUES (?, ?, 'done', ?, ?)",
                (USER, action, helped, i),
            )
    monkeypatch.setattr(memory, "DB_PATH", path)

    assert [o["type"] for o in actions.list_outcomes(USER)] == ["other", "breathing", "walk"]
    assert actions.pending_reminders(USER)[0]["type"] == "stretch"
    assert feedback.choose(actions.user_stats(USER)) == "walk"

    # Labelled once: a later change sticks, even in a fresh run.
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE outcomes SET type = 'grounding' WHERE action = 'tidy your desk'")
    actions._migrated.clear()
    assert actions.list_outcomes(USER)[0]["type"] == "grounding"
    # New rows get their type straight away.
    assert actions.record_outcome(USER, "a song you love", "done", "yes", action_type="music")["type"] == "music"


def test_new_database_has_the_column(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "new.db")
    rem = actions.create_reminder(USER, "my thing", 1, action_type="rest")
    assert rem["type"] == "rest"
    assert actions.create_reminder(USER, "a walk", 1, action_type="nonsense")["type"] == "walk"  # off-list: keywords


# ── Through the API ──────────────────────────────────────────────────────────


def test_after_walk_helped_and_breathing_did_not_the_next_suggestion_is_a_walk(api):
    client, state = api
    actions.record_outcome(USER, "a five-minute walk", "done", "yes", action_type="walk")
    actions.record_outcome(USER, "slow breathing", "done", "no", action_type="breathing")
    state["suggest"] = {"suggest": True, "type": "walk", "action": "a short walk outside", "line": "Fancy a short walk outside?"}
    res = chat(client, TALK, {"mode": "consider"})
    assert res["action"] == {"type": "offer", "action": "a short walk outside", "action_type": "walk"}
    prompt = called(state, "suggest")[0]
    assert prompt.startswith("Type: walk\n\nWorked for them: walk (helped 1 of 1). Didn't work: breathing (helped 0 of 1).")


def test_suggestion_of_another_type_is_dropped(api):
    client, state = api
    state["suggest"] = {"suggest": True, "type": "walk", "action": "a walk", "line": "Fancy a walk?"}  # chosen: breathing
    res = chat(client, TALK, {"mode": "consider"})
    assert res["action"] is None and res["reply"] == "Tell me more about that."


def test_type_is_stored_on_the_reminder_and_the_outcome(api):
    client, state = api
    state["decide"] = {"decision": "later", "minutes": 5, "clock": ""}
    ctx = {"mode": "offer", "action": "a song you love", "action_type": "music"}
    history = [*TALK, {"role": "assistant", "text": "Want to put on a song you love?"}, {"role": "user", "text": "in 5 minutes"}]
    rem = chat(client, history, ctx)["action"]["reminder"]
    assert rem["type"] == "music"
    assert client.get("/api/reminders", params={"user_id": USER}).json()["reminders"][0]["type"] == "music"
    assert "(type: music)" in called(state, "exercise")[0]

    # The check-in takes the type from the reminder.
    state["decide"] = {"decision": "now", "minutes": 0, "clock": ""}
    ev = chat(client, [{"role": "assistant", "text": "Ready?"}, {"role": "user", "text": "yes"}],
              {"mode": "checkin", "action": "a song you love", "reminder_id": rem["id"]})["action"]
    assert (ev["type"], ev["action_type"]) == ("start", "music")
    fb = {"mode": "feedback", "action": "a song you love", "action_type": "music", "reminder_id": rem["id"]}
    saved = chat(client, [{"role": "assistant", "text": "How did that feel?"}, {"role": "user", "text": "lovely"}], fb)
    assert saved["action"]["outcome"]["type"] == "music"


def test_asking_for_something_runs_it_and_records_it(api):
    client, state = api
    history = [{"role": "assistant", "text": "Hi"}, {"role": "user", "text": "can we do breathing?"}]
    res = chat(client, history, {"mode": "consider"})
    ev = res["action"]
    assert (ev["type"], ev["action"], ev["action_type"], ev["reminder_id"]) == ("start", "some slow breathing", "breathing", None)
    assert res["reply"] == ev["exercise"]["intro"]
    assert called(state, "suggest") == []  # no suggestion call, and no wait for two messages

    fb = {"mode": "feedback", "action": ev["action"], "action_type": "breathing", "done": True}
    chat(client, [*history, {"role": "assistant", "text": "How did that feel?"}, {"role": "user", "text": "calmer"}], fb)
    assert actions.list_outcomes(USER)[0]["type"] == "breathing"


def test_asking_works_after_the_sessions_suggestion_and_instead_of_an_offer(api):
    client, state = api
    # Already had this session's suggestion: no new one, but an ask is still heard.
    assert chat(client, TALK, {"mode": "consider", "suggest": False})["action"] is None
    assert called(state, "suggest") == []
    history = [*TALK, {"role": "assistant", "text": "Want to try a walk?"}, {"role": "user", "text": "could we try some stretching instead"}]
    ev = chat(client, history, {"mode": "offer", "action": "a walk", "action_type": "walk"})["action"]
    assert (ev["type"], ev["action_type"], ev["action"]) == ("start", "stretch", "a gentle stretch")
    assert called(state, "decide") == []


def test_asking_does_nothing_on_or_after_a_crisis(api):
    client, state = api
    res = chat(client, [{"role": "user", "text": "I want to die, can we do breathing"}], {"mode": "consider"})
    assert res["crisis"] and res["action"] is None
    history = [{"role": "user", "text": "x", "crisis": True}, {"role": "assistant", "text": "y", "crisis": True},
               {"role": "user", "text": "can we do breathing"}]
    assert chat(client, history, {"mode": "consider"})["action"] is None
    state["label"] = "crisis"
    assert chat(client, [{"role": "user", "text": "can we do breathing"}], {"mode": "consider"})["action"] is None
    assert actions.list_outcomes(USER) == [] and called(state, "exercise") == []


def test_crisis_during_feedback_stores_no_words_but_keeps_the_type(api):
    client, state = api
    fb = {"mode": "feedback", "action": "a short walk", "action_type": "walk", "done": False}
    res = chat(client, [{"role": "user", "text": "it made me want to die"}], fb)
    assert res["crisis"]
    [o] = actions.list_outcomes(USER)
    assert (o["type"], o["status"], o["helped"], o["words"]) == ("walk", "skipped", "unknown", "")


def test_stopped_early_without_an_answer_counts_as_stopped(api):
    client, _ = api
    client.post("/api/outcomes", json={"user_id": USER, "action": "a walk", "action_type": "walk", "done": False})
    s = actions.user_stats(USER)["walk"]
    assert (s["tries"], s["stopped"], s["skipped"]) == (1, 1, 0)


def test_stats_endpoint_and_reset(api):
    client, _ = api
    actions.record_outcome(USER, "a short walk", "done", "yes", action_type="walk")
    actions.record_outcome(USER, "a short walk", "done", "yes", action_type="walk")
    actions.record_outcome(USER, "slow breathing", "done", "no", action_type="breathing")
    memory.add_note(USER, "Has a dog called Biscuit.")
    data = client.get("/api/stats", params={"user_id": USER}).json()
    assert data["next"] == "walk"
    assert [t["type"] for t in data["types"]][0] == "walk" and data["types"][-1]["type"] == "breathing"
    walk = data["types"][0]
    assert (walk["label"], walk["summary"], walk["tries"], walk["helped"]) == ("Walk", "helped 2 of 2", 2, 2.0)
    assert walk["last_tried"] and not walk["blocked"]
    assert {t["type"] for t in data["types"]} == set(feedback.TYPES)

    other = "user-" + "e" * 12
    assert client.get("/api/stats", params={"user_id": other}).json()["types"][0]["tries"] == 0
    assert client.get("/api/stats", params={"user_id": "x"}).status_code == 422

    assert client.delete("/api/stats", params={"user_id": USER}).json() == {"ok": True, "removed": 3}
    data = client.get("/api/stats", params={"user_id": USER}).json()
    assert all(t["tries"] == 0 for t in data["types"]) and data["next"] == "breathing"
    assert [m["text"] for m in memory.list_memories(USER)] == ["Has a dog called Biscuit."]  # only outcome notes go


# ── optional live check against real Gemini ──────────────────────────────────

LIVE_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


@pytest.mark.skipif(not LIVE_KEY or LIVE_KEY in {"your-key-here", "test-key", "stub"}, reason="GEMINI_API_KEY not set")
def test_live_suggestion_keeps_to_the_chosen_type(monkeypatch, tmp_path):
    from google import genai

    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory.db")
    actions.record_outcome(USER, "a five-minute walk", "done", "yes", action_type="walk")
    actions.record_outcome(USER, "slow breathing", "done", "no", action_type="breathing")
    turns = [main.Turn(**t) for t in TALK]
    found = asyncio.run(actions.consider(genai.Client(api_key=LIVE_KEY), main.ACTION_MODEL, USER, turns))
    print("live suggestion:", found)  # the model may decide it's not the moment; if it suggests, it's a walk
    assert found is None or found["action_type"] == "walk"
