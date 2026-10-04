"""
Feedback loop (build plan step 6).

Every action has a type from a fixed list. How each type went for a person is counted here, in
code: tries, how much it helped, skips, early stops and when it was last tried, with recent
outcomes counting more. The next suggestion's type is picked from those numbers; Gemini only
writes the wording and the exercise for it.

Everything here is pure: it takes outcome rows (as stored by actions.py) and returns numbers.
"""

import re
from dataclasses import dataclass

# ── Config ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ActionType:
    label: str  # shown in the "What helps" list
    default: str  # the action used when someone asks for this type by name


# The order is the tie-break when nothing separates them (e.g. a brand new user).
TYPES: dict[str, ActionType] = {
    "breathing": ActionType("Breathing", "some slow breathing"),
    "walk": ActionType("Walk", "a short walk"),
    "stretch": ActionType("Stretch", "a gentle stretch"),
    "grounding": ActionType("Grounding", "a grounding exercise"),
    "music": ActionType("Music", "listening to a song you love"),
    "journaling": ActionType("Journaling", "writing a few lines"),
    "reach_out": ActionType("Reach out", "reaching out to someone you trust"),
    "rest": ActionType("Rest", "a few minutes of rest"),
}
OTHER = "other"  # old rows the keywords couldn't place; shown, never suggested

HELPED_VALUE = {"yes": 1.0, "somewhat": 0.5, "no": 0.0}
SKIP_VALUE = 0.25  # planned but skipped: a mild sign it's not for them
DECAY = 0.6  # each older outcome of a type counts this much as the next newer one
PRIOR, PRIOR_WEIGHT = 0.5, 0.5  # every type starts at "maybe", so one result doesn't decide everything
EXPLORE_BONUS = 0.15  # never-tried types: enough to beat a "somewhat", not a "yes"


def valid(action_type) -> str | None:
    return action_type if action_type in TYPES or action_type == OTHER else None


# ── Keyword labelling (old rows, and old clients that send no type) ──────────

# First match wins, so "a walk with your favourite music" is a walk.
_LABELS = [
    ("walk", r"walk\w*|stroll\w*"),
    ("stretch", r"stretch\w*|yoga|shoulder rolls?"),
    ("journaling", r"journal\w*|writ(?:e|es|ing)|jot\w*|diary"),
    ("reach_out", r"reach(?:ing)? out|call(?:ing)?|texts?|texting|message|friend|talk to"),
    ("grounding", r"ground\w*|5-4-3-2-1|senses|body scan|mindful\w*"),
    ("breathing", r"breath\w*|inhale|exhale"),
    ("rest", r"rest(?:ing)?|nap|lie down|sleep|tea"),
    ("music", r"music|songs?|playlist|listen\w*"),
]
_LABEL_RES = [(t, re.compile(rf"\b(?:{p})\b", re.I)) for t, p in _LABELS]


def label(action: str) -> str:
    """The type of a free-text action, by keywords. OTHER if nothing matches."""
    return next((t for t, rx in _LABEL_RES if rx.search(action or "")), OTHER)


# ── "Can we do breathing?" ───────────────────────────────────────────────────

_VERB = r"do|try|have|take|go for|go on|start|guide me through|walk me through|help me with"
_ASK = (
    rf"(?:can|could|shall) (?:we|i|you)(?: (?:{_VERB}))?|let'?s(?: (?:{_VERB}))?|let us (?:{_VERB})"
    rf"|i'?d (?:like|love) to (?:{_VERB})|i (?:want|wanna) to (?:{_VERB})"
    r"|(?:how|what) about|(?:guide|walk|take) me through"
)
_FILLER = r"(?:\s+(?:a|an|some|the|little|bit|of|short|quick|small|gentle|slow|few|more|\d+|five|ten|minutes?))*"
_ASKED = {
    "breathing": r"breath\w*",
    "walk": r"walk|stroll",
    "stretch": r"stretch\w*|yoga",
    "grounding": r"grounding|5-4-3-2-1|body scan",
    "music": r"music|songs?|playlist",
    "journaling": r"journal\w*|writing|write",
    "reach_out": r"reach(?:ing)? out",
    "rest": r"rest|nap",
}
_ASKED_RES = {t: re.compile(rf"\b(?:{_ASK}){_FILLER}\s+(?:{w})\b", re.I) for t, w in _ASKED.items()}


def requested(text: str) -> str | None:
    """The type the person asks for by name ("can we do breathing?", "let's go for a walk"), if any.
    Strict on purpose: "I tried breathing" or "I want to rest" are not requests."""
    text = re.sub(r"[’`]", "'", text or "")
    found = [(m.start(), t) for t, rx in _ASKED_RES.items() if (m := rx.search(text))]
    return min(found)[1] if found else None


# ── Stats and preference ─────────────────────────────────────────────────────


def _value(o: dict) -> float | None:
    """How well one outcome went, 0 to 1. None: done, but they didn't say (no evidence either way)."""
    if o["status"] == "done":
        return HELPED_VALUE.get(o["helped"])
    if o["helped"] is None:  # planned, never started
        return SKIP_VALUE
    return 0.5 * HELPED_VALUE.get(o["helped"], 0.5)  # stopped early: half credit at most


def _stats_for(rows: list[dict]) -> dict:
    """rows: one type's outcomes, newest first."""
    tries = [o for o in rows if o["status"] == "done" or o["helped"] is not None]
    count = {h: sum(o["helped"] == h for o in tries) for h in ("yes", "somewhat", "no", "unknown")}
    weighted = [v for o in rows if (v := _value(o)) is not None]
    num = PRIOR * PRIOR_WEIGHT + sum(v * DECAY**k for k, v in enumerate(weighted))
    den = PRIOR_WEIGHT + sum(DECAY**k for k in range(len(weighted)))
    score = num / den + (EXPLORE_BONUS if not tries else 0.0)
    return {
        "tries": len(tries),
        "helped": sum(HELPED_VALUE.get(o["helped"], 0.0) for o in tries),
        **count,
        "skipped": sum(o["status"] != "done" and o["helped"] is None for o in rows),
        "stopped": sum(o["status"] != "done" and o["helped"] is not None for o in rows),
        "last_tried": max((o["created_at"] for o in tries), default=None),
        "score": round(score, 4),
        # The last two tries both didn't help: don't suggest it again (unless nothing else is left).
        "blocked": len(tries) >= 2 and all(o["helped"] == "no" for o in tries[:2]),
    }


def stats(outcomes: list[dict]) -> dict[str, dict]:
    """Per-type stats from a person's outcomes (any order). Every configured type is included."""
    rows = sorted(outcomes, key=lambda o: (o["created_at"], o.get("id") or 0), reverse=True)
    by_type: dict[str, list[dict]] = {t: [] for t in TYPES}
    for o in rows:
        by_type.setdefault(valid(o.get("type")) or OTHER, []).append(o)
    return {t: _stats_for(r) for t, r in by_type.items()}


def _rank_key(t: str, s: dict):
    # Best score first; on a tie, the one tried longest ago, then config order.
    return (-s["score"], s["last_tried"] or 0, list(TYPES).index(t) if t in TYPES else len(TYPES))


def ranked(st: dict[str, dict]) -> list[str]:
    return sorted(st, key=lambda t: _rank_key(t, st[t]))


def choose(st: dict[str, dict]) -> str:
    """The type to suggest next."""
    order = [t for t in ranked(st) if t in TYPES]
    return next((t for t in order if not st[t]["blocked"]), order[0])  # all blocked: best of a bad lot


def summary(s: dict) -> str:
    """'helped 2 of 2', 'helped 1 of 3, a little 1, skipped 1', 'not tried yet, skipped 2'."""
    parts = [f"helped {s['yes']} of {s['tries']}" if s["tries"] else "not tried yet"]
    if s["somewhat"]:
        parts.append(f"a little {s['somewhat']}")
    if s["stopped"]:
        parts.append(f"stopped early {s['stopped']}")
    if s["skipped"]:
        parts.append(f"skipped {s['skipped']}")
    return ", ".join(parts)


def history_line(st: dict[str, dict]) -> str:
    """One short line for the prompts: what worked for them and what didn't. '' with no history."""
    tried = [t for t in ranked(st) if st[t]["tries"]]
    worked = [t for t in tried if st[t]["yes"] + st[t]["somewhat"] and st[t]["score"] >= PRIOR]
    didnt = [t for t in tried if t not in worked]
    parts = []
    if worked:
        parts.append("Worked for them: " + ", ".join(f"{t} ({summary(st[t])})" for t in worked) + ".")
    if didnt:
        parts.append("Didn't work: " + ", ".join(f"{t} ({summary(st[t])})" for t in didnt) + ".")
    return " ".join(parts)
