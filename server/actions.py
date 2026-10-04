"""
Action and follow-through (build plan step 5).

When it fits, the AI suggests ONE small action, based on what the person said and what helped
them before. They can do it now (a guided exercise, written by Gemini for them and checked here),
later (a reminder, stored in SQLite, that opens with a check-in), or not at all. Afterwards the
app asks how it felt and saves the outcome, to inform future suggestions.

Every decision (suggest or not; yes / later / no; did it help) is a small JSON call to a fast
model, never a regex. Nothing here runs on a crisis turn.

Step 6: every reminder and outcome has a type (feedback.TYPES). The type to suggest is picked in
code from how each type went (feedback.py); Gemini writes the wording and the exercise for it.
"""

import json
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from google.genai import types

import feedback
import memory
import safety

# ── Storage ──────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    type TEXT,                    -- feedback.TYPES key (step 6)
    exercise TEXT,                -- JSON, filled in once generated
    due_at REAL NOT NULL,         -- unix seconds
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'skipped', 'cancelled')),
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS reminders_user ON reminders (user_id, status);
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    reminder_id INTEGER,
    action TEXT NOT NULL,
    type TEXT,                    -- feedback.TYPES key (step 6)
    status TEXT NOT NULL CHECK (status IN ('done', 'skipped')),
    helped TEXT CHECK (helped IN ('yes', 'no', 'somewhat', 'unknown')),
    words TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS outcomes_user ON outcomes (user_id);
"""

MAX_AHEAD = 7 * 24 * 3600  # reminders further out than a week are refused


_migrated: set[str] = set()  # databases already checked this run


def migrate(conn) -> None:
    """Step 5 databases have no `type` column: add it, and label the old rows once, by keywords."""
    for table in ("reminders", "outcomes"):
        if "type" in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN type TEXT")
        rows = conn.execute(f"SELECT id, action FROM {table}").fetchall()
        conn.executemany(f"UPDATE {table} SET type = ? WHERE id = ?", [(feedback.label(r["action"]), r["id"]) for r in rows])


@contextmanager
def db():
    with memory.db() as conn:
        conn.executescript(SCHEMA)
        if str(memory.DB_PATH) not in _migrated:
            migrate(conn)
            _migrated.add(str(memory.DB_PATH))
        yield conn


def type_of(action: str, action_type: str | None) -> str:
    """A stored type for an action: the one given if it's on the list, else by keywords."""
    return feedback.valid(action_type) or feedback.label(action)


def _reminder(row) -> dict:
    r = dict(row)
    r["exercise"] = json.loads(r["exercise"]) if r["exercise"] else None
    return r


def create_reminder(
    user_id: str, action: str, due_at: float, exercise: dict | None = None, action_type: str | None = None
) -> dict:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (user_id, action, type, exercise, due_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, action, type_of(action, action_type), json.dumps(exercise) if exercise else None, due_at, time.time()),
        )
        rid = cur.lastrowid
    return get_reminder(user_id, rid)


def get_reminder(user_id: str, reminder_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id)).fetchone()
    return _reminder(row) if row else None


def pending_reminders(user_id: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE user_id = ? AND status = 'pending' ORDER BY due_at", (user_id,)
        ).fetchall()
    return [_reminder(r) for r in rows]


def update_reminder(user_id: str, reminder_id: int, **fields) -> bool:
    if "exercise" in fields:
        fields["exercise"] = json.dumps(fields["exercise"]) if fields["exercise"] else None
    sets = ", ".join(f"{k} = ?" for k in fields)
    with db() as conn:
        cur = conn.execute(
            f"UPDATE reminders SET {sets} WHERE id = ? AND user_id = ?", (*fields.values(), reminder_id, user_id)
        )
        return cur.rowcount > 0


HELPED_NOTE = {
    "yes": "it helped",
    "somewhat": "it helped a little",
    "no": "it didn't help",
    "unknown": "didn't say how it went",
}


def record_outcome(
    user_id: str,
    action: str,
    status: str,
    helped: str | None = None,
    words: str = "",
    reminder_id: int | None = None,
    action_type: str | None = None,
) -> dict:
    """Save how an action went, close its reminder, and add a note to memory (for future suggestions).
    status 'skipped' with no `helped`: planned but not done. With `helped`: started, then stopped early."""
    words = words.strip()[:500]
    if safety.keyword_crisis(words):
        words = ""  # crisis content is never stored
    if status == "done":
        helped = helped if helped in HELPED_NOTE else "unknown"
    elif helped not in HELPED_NOTE:
        helped = None
    if not feedback.valid(action_type) and reminder_id is not None and (rem := get_reminder(user_id, reminder_id)):
        action_type = rem["type"]
    action_type = type_of(action, action_type)
    now = time.time()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO outcomes (user_id, reminder_id, action, type, status, helped, words, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, reminder_id, action, action_type, status, helped, words, now),
        )
        oid = cur.lastrowid
        if reminder_id is not None:
            conn.execute(
                "UPDATE reminders SET status = ? WHERE id = ? AND user_id = ?",
                ("done" if status == "done" else "skipped", reminder_id, user_id),
            )
    if status == "done":
        note = f"Tried {action}: {HELPED_NOTE[helped]}."
    elif helped:  # started, then stopped early
        note = f"Started {action} but stopped early: {HELPED_NOTE[helped]}."
    else:
        note = f"Planned {action} but skipped it."
    if words:
        note += f' They said: "{words[:120]}"'
    memory.add_note(user_id, note, kind="pattern")
    return {
        "id": oid,
        "action": action,
        "type": action_type,
        "status": status,
        "helped": helped,
        "words": words,
        "reminder_id": reminder_id,
    }


def list_outcomes(user_id: str, limit: int = 20) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, reminder_id, action, type, status, helped, words, created_at FROM outcomes "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def user_stats(user_id: str | None) -> dict[str, dict]:
    """Per-type stats (feedback.stats) from everything this person has tried."""
    return feedback.stats(list_outcomes(user_id, 1000) if user_id else [])


_OUTCOME_NOTE = re.compile(r"^(Tried|Started|Planned) ")


def reset_outcomes(user_id: str) -> int:
    """Forget what helped: the outcomes and the memory notes made from them. Reminders stay."""
    with db() as conn:
        n = conn.execute("DELETE FROM outcomes WHERE user_id = ?", (user_id,)).rowcount
    for m in memory.list_memories(user_id):
        if m["kind"] == "pattern" and _OUTCOME_NOTE.match(m["text"]):
            memory.delete_memory(user_id, m["id"])
    return n


# ── Exercises ────────────────────────────────────────────────────────────────

MIN_TOTAL, MAX_TOTAL = 180, 360  # seconds
MIN_STEPS, MAX_STEPS = 4, 15
MIN_STEP, MAX_STEP = 5, 90

EXERCISE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "intro": {"type": "STRING"},
        "steps": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"say": {"type": "STRING"}, "seconds": {"type": "INTEGER"}},
                "required": ["say", "seconds"],
            },
        },
        "closing": {"type": "STRING"},
    },
    "required": ["title", "intro", "steps", "closing"],
}

# Used when generation fails twice, so the flow never breaks. Must pass validate_exercise.
FALLBACK_EXERCISE = {
    "title": "Slow breathing",
    "intro": "Let's take a few minutes to slow down together. Find a comfortable way to sit, and just follow my voice.",
    "steps": [
        {"say": "Let your shoulders drop, and rest your hands somewhere comfortable.", "seconds": 20},
        {"say": "Breathe in gently through your nose for a count of four, then out through your mouth for a count of six. Keep going at your own pace.", "seconds": 60},
        {"say": "Notice your feet on the floor, and the weight of your body being held.", "seconds": 30},
        {"say": "Keep breathing slowly. In for four, out for six. If your mind wanders, that's fine, just come back to the breath.", "seconds": 60},
        {"say": "Notice one thing you can hear right now, and one thing you can feel.", "seconds": 30},
        {"say": "Take three more slow breaths, a little longer on each breath out.", "seconds": 40},
    ],
    "closing": "Well done. Let your breathing go back to normal, and take a moment before you move on.",
}

# Plain spoken text: no markdown, links or emojis.
_NOT_PLAIN = re.compile(r"[*#_`~<>\[\]{}|\\]|https?://|www\.|[☀-➿\U0001F000-\U0001FAFF]")

# Nothing medical or risky: food restriction, hard exertion, driving, medication, extremes.
_RISKY = re.compile(
    r"\b(fast(ing|ed)? (for|until)|fasting|skip (a |your |the )?(meal|breakfast|lunch|dinner)s?|don'?t eat|without eating|"
    r"calorie|diet|purge|sprint\w*|run as (fast|hard)|as (hard|fast) as (you )?(can|possible)|max(imum)? effort|"
    r"high[- ]intensity|hiit|burpees?|push[- ]?ups?|heavy (weights?|lifting)|lift weights|exhaust\w*|until it hurts|"
    r"through the pain|driv(e|es|ing)|behind the wheel|medication|meds|pills?|dos(e|age)|prescri\w+|diagnos\w*|"
    r"alcohol|beer|wine|drunk|cigarette|vape|ice bath|cold plunge|hyperventilat\w*|"
    r"hold your breath for (as long|\d{2,}))\b",
    re.I,
)


def risky(text: str) -> bool:
    return bool(_RISKY.search(text)) or safety.keyword_crisis(text)


def _spoken(text, field: str, max_len: int, errors: list[str]) -> str:
    if not isinstance(text, str) or not text.strip():
        errors.append(f"{field} is empty")
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        errors.append(f"{field} is too long")
    if _NOT_PLAIN.search(text):
        errors.append(f"{field} is not plain spoken text")
    if risky(text):
        errors.append(f"{field} has unsafe content")
    return text


def validate_exercise(ex) -> tuple[dict | None, list[str]]:
    """Check a generated exercise. Returns (clean exercise, []) or (None, reasons)."""
    errors: list[str] = []
    if not isinstance(ex, dict):
        return None, ["not an object"]
    clean = {
        "title": _spoken(ex.get("title"), "title", 80, errors),
        "intro": _spoken(ex.get("intro"), "intro", 400, errors),
        "closing": _spoken(ex.get("closing"), "closing", 400, errors),
        "steps": [],
    }
    steps = ex.get("steps")
    if not isinstance(steps, list) or not MIN_STEPS <= len(steps) <= MAX_STEPS:
        errors.append(f"needs {MIN_STEPS} to {MAX_STEPS} steps")
        steps = steps if isinstance(steps, list) else []
    for i, s in enumerate(steps, 1):
        s = s if isinstance(s, dict) else {}
        say = _spoken(s.get("say"), f"step {i}", 300, errors)
        secs = s.get("seconds")
        if isinstance(secs, bool) or not isinstance(secs, (int, float)) or not MIN_STEP <= secs <= MAX_STEP:
            errors.append(f"step {i} must last {MIN_STEP} to {MAX_STEP} seconds")
            secs = 0
        clean["steps"].append({"say": say, "seconds": int(secs)})
    total = sum(s["seconds"] for s in clean["steps"])
    if not MIN_TOTAL <= total <= MAX_TOTAL:
        errors.append(f"total {total}s is outside {MIN_TOTAL // 60} to {MAX_TOTAL // 60} minutes")
    return (None, errors) if errors else (clean, [])


EXERCISE_PROMPT = """\
Write a short guided exercise to be read aloud, step by step, to one person right now.
Make it theirs: use their words, what they like and what helped them before.
Safe and gentle only: breathing, senses, light stretching or walking, music, writing, kindness.
No food or fasting, no hard exercise, no driving, nothing medical.
3 to 6 minutes in total, 4 to 15 steps, each step 5 to 90 seconds (time to say it and do it).
Plain spoken sentences: no lists, markdown or emojis."""


def exercise_input(user_id: str | None, action: str, words: list[str], action_type: str | None = None) -> str:
    parts = [f"Exercise: {action} (type: {type_of(action, action_type)})"]
    if tried := feedback.history_line(user_stats(user_id)):
        parts.append(tried)
    if words:
        parts.append("They just said:\n" + "\n".join(f"- {w[:300]}" for w in words[-3:]))
    if mems := memory.prompt_block(user_id).strip():
        parts.append(mems)
    return "\n\n".join(parts)


def _config(model: str, system: str, schema: dict, max_tokens: int, temperature: float) -> types.GenerateContentConfig:
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        max_output_tokens=max_tokens,
        response_mime_type="application/json",
        response_schema=schema,
    )
    if "2.5-flash" in model:
        config.thinking_config = types.ThinkingConfig(thinking_budget=0)
    return config


async def _json_call(client, model, system, contents, schema, max_tokens=200, temperature=0.0) -> dict:
    config = _config(model, system, schema, max_tokens, temperature)
    resp = await client.aio.models.generate_content(model=model, contents=contents, config=config)
    return json.loads(resp.text)


async def gemini_exercise(client, model: str, prompt: str) -> dict:
    return await _json_call(client, model, EXERCISE_PROMPT, prompt, EXERCISE_SCHEMA, max_tokens=1500, temperature=0.8)


async def make_exercise(
    client, model: str, user_id: str | None, action: str, words: list[str], action_type: str | None = None
) -> tuple[dict, str]:
    """A validated exercise for `action`. Tries Gemini twice, then the built-in one. → (exercise, 'gemini'|'fallback')"""
    prompt = exercise_input(user_id, action, words, action_type)
    for _ in range(2):
        try:
            clean, _errors = validate_exercise(await gemini_exercise(client, model, prompt))
        except Exception:  # quota, network, bad JSON
            clean = None
        if clean:
            return clean, "gemini"
    return json.loads(json.dumps(FALLBACK_EXERCISE)), "fallback"


# ── Should we suggest something? (fast model) ────────────────────────────────

SUGGEST_PROMPT = """\
You help a supportive listener decide whether to suggest ONE small action right now.
Suggest only if the person has said what's weighing on them and a small 3 to 6 minute thing
they can do where they are could help. Not if they're mid-story or asked for something else.
The action must be of the given type; shape it to them (their words, what they enjoy).
Nothing intense, medical, about food, or involving driving.
type: the given type.
action: a short phrase, e.g. "a five-minute walk with your favourite music".
line: one or two warm spoken sentences: briefly reflect what they said, then ask if they'd
like to try it. If suggest is false, leave action and line empty."""

SUGGEST_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "suggest": {"type": "BOOLEAN"},
        "type": {"type": "STRING", "enum": list(feedback.TYPES)},
        "action": {"type": "STRING"},
        "line": {"type": "STRING"},
    },
    "required": ["suggest", "type", "action", "line"],
}

MIN_USER_TURNS = 2  # let them say what's going on before anything is suggested


def suggest_input(user_id: str | None, turns, action_type: str, history: str = "") -> str:
    recent = [t for t in turns if t.text.strip()][-6:]
    talk = "\n".join(f"{'User' if t.role == 'user' else 'Listener'}: {t.text.strip()}" for t in recent)
    parts = [f"Type: {action_type}", history, memory.prompt_block(user_id).strip()]
    return "\n\n".join([*(p for p in parts if p), f"Conversation:\n{talk}"])


async def gemini_suggest(client, model: str, prompt: str) -> dict:
    return await _json_call(client, model, SUGGEST_PROMPT, prompt, SUGGEST_SCHEMA, max_tokens=200, temperature=0.4)


async def consider(client, model: str, user_id: str | None, turns) -> dict | None:
    """{action, line, action_type} if now is a good moment to suggest something, else None.
    The type is chosen here from what helped before; failures mean no suggestion."""
    if sum(t.role == "user" for t in turns) < MIN_USER_TURNS or any(t.crisis for t in turns):
        return None
    st = user_stats(user_id)
    chosen = feedback.choose(st)
    try:
        out = await gemini_suggest(client, model, suggest_input(user_id, turns, chosen, feedback.history_line(st)))
    except Exception:
        return None
    action, line = (out.get("action") or "").strip(), (out.get("line") or "").strip()
    if not out.get("suggest") or out.get("type") != chosen or not action or not line or len(action) > 120 or len(line) > 400:
        return None
    if risky(action) or risky(line) or _NOT_PLAIN.search(line):
        return None
    return {"action": action, "line": line, "action_type": chosen}


# ── Their answer: now, later (when?), or no (fast model) ─────────────────────

DECIDE_PROMPT = """\
The listener offered the person a small activity. Classify the person's answer.
now: yes, sure, let's do it now.
later: they want to do it later or at a time; set minutes (from now, e.g. "in 20 minutes",
"in an hour") OR clock (24-hour HH:MM local time, e.g. "at 6" in the day means 18:00) if they gave one.
no: they don't want to.
other: they ignored it or talked about something else.
Leave minutes 0 and clock empty if no time was given."""

DECIDE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "decision": {"type": "STRING", "enum": ["now", "later", "no", "other"]},
        "minutes": {"type": "INTEGER"},
        "clock": {"type": "STRING"},
    },
    "required": ["decision", "minutes", "clock"],
}


async def gemini_decide(client, model: str, prompt: str) -> dict:
    return await _json_call(client, model, DECIDE_PROMPT, prompt, DECIDE_SCHEMA, max_tokens=60)


async def decide(client, model: str, offer: str, answer: str, now: float, tz_offset: int) -> dict:
    """→ {decision, due_at|None}. A failed call counts as 'other' (the offer is dropped, talk carries on)."""
    local = datetime.fromtimestamp(now, timezone(timedelta(minutes=tz_offset)))
    prompt = f'Local time now: {local:%H:%M}\nOffer: "{offer}"\nAnswer: "{answer}"'
    try:
        out = await gemini_decide(client, model, prompt)
    except Exception:
        return {"decision": "other", "due_at": None}
    decision = out.get("decision") if out.get("decision") in {"now", "later", "no", "other"} else "other"
    due = due_time(out.get("minutes") or 0, out.get("clock") or "", now, tz_offset) if decision == "later" else None
    return {"decision": decision, "due_at": due}


def due_time(minutes: int, clock: str, now: float, tz_offset: int) -> float | None:
    """When a 'later' answer is due, in unix seconds, or None if no usable time was given."""
    if isinstance(minutes, int) and 1 <= minutes <= MAX_AHEAD // 60:
        return now + minutes * 60
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", clock or "")
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        return None
    tz = timezone(timedelta(minutes=tz_offset))
    local_now = datetime.fromtimestamp(now, tz)
    at = local_now.replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0)
    if at <= local_now:
        at += timedelta(days=1)
    return at.timestamp()


def when_text(due_at: float, now: float, tz_offset: int) -> str:
    """'in 1 minute', 'in 20 minutes', 'at 6:00 pm' or 'tomorrow at 9:30 am', spoken naturally."""
    mins = round((due_at - now) / 60)
    if mins < 60:
        return f"in {mins} minute{'s' if mins != 1 else ''}"
    tz = timezone(timedelta(minutes=tz_offset))
    at, today = datetime.fromtimestamp(due_at, tz), datetime.fromtimestamp(now, tz).date()
    clock = f"{at.hour % 12 or 12}:{at.minute:02d} {'am' if at.hour < 12 else 'pm'}"
    if at.date() == today:
        return f"at {clock}"
    if at.date() == today + timedelta(days=1):
        return f"tomorrow at {clock}"
    return f"on {at:%A} at {clock}"


# ── How did it feel? (fast model) ────────────────────────────────────────────

HELPED_PROMPT = """\
After a short activity the person was asked how it felt. Did it help?
yes: better, calmer, good, it helped. somewhat: a bit, mixed, not sure. no: didn't help, worse."""

HELPED_SCHEMA = {
    "type": "OBJECT",
    "properties": {"helped": {"type": "STRING", "enum": ["yes", "somewhat", "no"]}},
    "required": ["helped"],
}


async def gemini_helped(client, model: str, prompt: str) -> str:
    return (await _json_call(client, model, HELPED_PROMPT, prompt, HELPED_SCHEMA, max_tokens=20))["helped"]


async def helped(client, model: str, action: str, answer: str) -> str:
    try:
        label = await gemini_helped(client, model, f'Activity: {action}\nTheir answer: "{answer}"')
    except Exception:
        return "unknown"
    return label if label in HELPED_NOTE else "unknown"
