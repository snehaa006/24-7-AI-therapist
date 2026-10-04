"""
Memory across sessions (build plan step 4).

At the end of a session, Gemini pulls out lasting facts and patterns as JSON, which are merged
with what is already stored (duplicates and outdated items dropped). Crisis content is never
stored. Memories go into the system prompt, and returning users get a greeting that picks up
from last time. Stored in SQLite (server/memory.db), keyed by an anonymous id from the browser.
"""

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from google.genai import types

import safety

DB_PATH = Path(os.getenv("MEMORY_DB", Path(__file__).parent / "memory.db"))
MAX_MEMORIES = 40  # per user; oldest go first. Keeps the system prompt short.
USER_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


# ── Storage ──────────────────────────────────────────────────────────────────


@contextmanager
def db():
    """A connection that commits on success and always closes."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('fact', 'pattern')),
            text TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS memories_user ON memories (user_id);
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            ended_at REAL NOT NULL,
            summary TEXT NOT NULL DEFAULT ''
        );
        """
    )
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def list_memories(user_id: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT id, kind, text FROM memories WHERE user_id = ? ORDER BY id", (user_id,))
        return [dict(r) for r in rows]


def last_summary(user_id: str) -> str:
    with db() as conn:
        row = conn.execute(
            "SELECT summary FROM sessions WHERE user_id = ? AND summary != '' ORDER BY ended_at DESC LIMIT 1", (user_id,)
        ).fetchone()
    return row["summary"] if row else ""


def delete_memory(user_id: str, memory_id: int) -> bool:
    with db() as conn:
        return conn.execute("DELETE FROM memories WHERE id = ? AND user_id = ?", (memory_id, user_id)).rowcount > 0


def forget_all(user_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))
        conn.execute("UPDATE sessions SET summary = '' WHERE user_id = ?", (user_id,))


def session_saved(session_id: str) -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is not None


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def merge(user_id: str, session_id: str, found: dict) -> int:
    """Apply an extraction result. Returns how many new memories were added."""
    added = 0
    with db() as conn:
        for mid in found.get("drop_ids", []):
            conn.execute("DELETE FROM memories WHERE id = ? AND user_id = ?", (int(mid), user_id))
        seen = {_key(r["text"]) for r in conn.execute("SELECT text FROM memories WHERE user_id = ?", (user_id,))}
        now = time.time()
        for kind, items in (("fact", found.get("facts", [])), ("pattern", found.get("patterns", []))):
            for text in items:
                text = text.strip()
                if not text or len(text) > 300 or safety.keyword_crisis(text) or _key(text) in seen:
                    continue
                seen.add(_key(text))
                conn.execute(
                    "INSERT INTO memories (user_id, kind, text, created_at) VALUES (?, ?, ?, ?)", (user_id, kind, text, now)
                )
                added += 1
        conn.execute(
            "DELETE FROM memories WHERE user_id = ? AND id NOT IN "
            "(SELECT id FROM memories WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
            (user_id, user_id, MAX_MEMORIES),
        )
        summary = (found.get("summary") or "").strip()
        if safety.keyword_crisis(summary):
            summary = ""
        conn.execute(
            "INSERT OR REPLACE INTO sessions (id, user_id, ended_at, summary) VALUES (?, ?, ?, ?)",
            (session_id, user_id, now, summary[:400]),
        )
    return added


def add_note(user_id: str, text: str, kind: str = "fact") -> bool:
    """Save one note outside the end-of-session extraction (e.g. how an exercise went)."""
    text = text.strip()
    if not text or safety.keyword_crisis(text):
        return False
    with db() as conn:
        seen = {_key(r["text"]) for r in conn.execute("SELECT text FROM memories WHERE user_id = ?", (user_id,))}
        if _key(text) in seen:
            return False
        conn.execute(
            "INSERT INTO memories (user_id, kind, text, created_at) VALUES (?, ?, ?, ?)",
            (user_id, kind, text[:300], time.time()),
        )
        conn.execute(
            "DELETE FROM memories WHERE user_id = ? AND id NOT IN "
            "(SELECT id FROM memories WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
            (user_id, user_id, MAX_MEMORIES),
        )
    return True


# ── Extraction ───────────────────────────────────────────────────────────────

EXTRACT_PROMPT = """\
You keep notes for a supportive listener so they remember this person next time.
From the conversation, return:
- facts: lasting details (people, work, places, upcoming events), one short sentence each,
  e.g. "Their sister Maya is getting married in June."
- patterns: recurring feelings or themes, e.g. "Feels anxious on Sunday nights."
- drop_ids: ids of known notes that are now wrong or replaced by a new one.
- summary: one sentence on what they talked about this time.
Don't repeat known notes. Only use what the user said. Never note anything about suicide,
self-harm or a crisis. Empty lists are fine."""

EXTRACT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "facts": {"type": "ARRAY", "items": {"type": "STRING"}},
        "patterns": {"type": "ARRAY", "items": {"type": "STRING"}},
        "drop_ids": {"type": "ARRAY", "items": {"type": "INTEGER"}},
        "summary": {"type": "STRING"},
    },
    "required": ["facts", "patterns", "drop_ids", "summary"],
}


def safe_turns(turns) -> list:
    """The conversation minus crisis content: flagged turns, keyword hits, and the reply to either."""
    kept, skip_reply = [], False
    for t in turns:
        if t.role == "user":
            skip_reply = t.crisis or safety.keyword_crisis(t.text)
            if not skip_reply:
                kept.append(t)
        elif not (t.crisis or skip_reply):
            kept.append(t)
    return kept


def extract_input(known: list[dict], turns) -> str:
    notes = "\n".join(f"[{m['id']}] {m['kind']}: {m['text']}" for m in known) or "(none)"
    talk = "\n".join(f"{'User' if t.role == 'user' else 'Listener'}: {t.text.strip()}" for t in turns)
    return f"Known notes:\n{notes}\n\nConversation:\n{talk}"


async def gemini_extract(client, model: str, known: list[dict], turns) -> dict:
    config = types.GenerateContentConfig(
        system_instruction=EXTRACT_PROMPT,
        temperature=0.2,
        max_output_tokens=800,
        response_mime_type="application/json",
        response_schema=EXTRACT_SCHEMA,
    )
    if "2.5-flash" in model:
        config.thinking_config = types.ThinkingConfig(thinking_budget=0)
    resp = await client.aio.models.generate_content(model=model, contents=extract_input(known, turns), config=config)
    return json.loads(resp.text)


# ── Using memories ───────────────────────────────────────────────────────────


def prompt_block(user_id: str | None) -> str:
    """Extra system-prompt text for a returning user ('' if nothing is remembered)."""
    if not user_id:
        return ""
    mems, summary = list_memories(user_id), last_summary(user_id)
    if not mems and not summary:
        return ""
    lines = [f"- {m['text']}" for m in mems]
    if summary:
        lines.append(f"- Last time: {summary}")
    return (
        "\nWhat you remember from earlier sessions (bring it up only when it fits, never as a list,"
        " and don't mention notes or memory):\n" + "\n".join(lines) + "\n"
    )


GREETING_PROMPT = """\
You are a warm companion greeting someone you have talked with before. Say hello and gently
pick up one specific thing from last time, then ask how it is going now. One or two spoken
sentences, under 40 words, no markdown or emojis. Don't mention notes or memory."""


async def gemini_greeting(client, model: str, user_id: str) -> str:
    config = types.GenerateContentConfig(system_instruction=GREETING_PROMPT, temperature=0.8, max_output_tokens=120)
    if "2.5-flash" in model:
        config.thinking_config = types.ThinkingConfig(thinking_budget=0)
    resp = await client.aio.models.generate_content(model=model, contents=prompt_block(user_id), config=config)
    return (resp.text or "").strip()
