"""
Safety layer (build plan step 3).

Every user message is screened two ways before a reply is allowed through:
  1. A keyword/regex check. A match is always treated as a crisis, with no model call.
  2. A separate Gemini call (fast model, temperature 0, JSON) that labels the latest message
     "normal" or "crisis" using the recent conversation. If that call fails for any reason,
     the message is treated as a crisis.

A crisis gets a fixed, human-written script (never AI text) plus helpline details from config.
"""

import json
import logging
import os
import re

from google.genai import types

log = logging.getLogger("uvicorn.error")  # shows in the uvicorn terminal

# ── 1. Keywords ──────────────────────────────────────────────────────────────
# Deliberately tight around self-harm and suicide so everyday figures of speech
# ("this traffic is killing me", "I'm dying to see it") don't trip it. Subtler
# messages are the Gemini check's job.
_PATTERNS = [
    r"\bsuicid(e|al)\b",
    r"\bkill(ing)? ?my ?self\b",
    r"\bkms\b",
    r"\bunalive\b",
    r"\bself[- ]?harm",
    r"\b(hang|hanging|hanged) myself\b",
    r"\b(slit|slitting|cut|cutting) my wrists?\b",
    r"\b(cutting|burning|harming|hurting|starving) myself\b",
    r"\b(want|wanna|wanted|going|gonna|plan|planning|tempted|thinking about|think about|thought about|feel like|urge)"
    r"\b[\w\s']{0,15}\b(hurt|harm|cut|burn|kill) myself\b",
    r"\b(end|ending|ended|take|taking|took) (my|my own) life\b",
    r"\bend it all\b",
    r"\b(want|wanna|wanted|wish|wishing|ready|deserve|deserved) (to )?die\b",
    r"\bwish (i was|i were|i'd been|i had been) (dead|never born)\b",
    r"\bbetter off (dead|without me)\b",
    r"\bno (reason|point) (to|in) (live|living|being alive|going on)\b",
    r"\b(don't|dont|do not) want to (live|be alive|exist|wake up|go on living)\b",
    r"\b(take|taking|took|thinking about|thought about) an overdose\b",
    r"\boverdos(e|ed|ing) on (pills|my pills|meds|my meds|medication|tablets|sleeping pills)\b",
]
_KEYWORDS = re.compile("|".join(f"(?:{p})" for p in _PATTERNS))


def _normalise(text: str) -> str:
    text = text.lower().replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).strip()


def keyword_crisis(text: str) -> bool:
    return bool(_KEYWORDS.search(_normalise(text)))


# ── 2. Gemini classifier ─────────────────────────────────────────────────────

CONTEXT_TURNS = 6  # recent turns shown to the classifier (so a "yes" to "are you safe?" is understood)

CLASSIFY_PROMPT = """\
You screen messages sent to a support chat. Label ONLY the user's LATEST message, using the
conversation for context.
crisis: thoughts of suicide or dying, self-harm, intent or plans to hurt themselves or someone
else, being in danger right now (abuse, violence), or saying yes when asked about these.
normal: anything else, including sadness, stress, grief, and figures of speech
("this traffic is killing me", "I'm dying to see it").
When unsure, choose crisis."""

LABEL_SCHEMA = {
    "type": "OBJECT",
    "properties": {"label": {"type": "STRING", "enum": ["normal", "crisis"]}},
    "required": ["label"],
}


def classifier_input(turns) -> str:
    """Recent turns as plain text, ending with the message to label."""
    recent = [t for t in turns if t.text.strip()][-CONTEXT_TURNS:]
    lines = [f"{'User' if t.role == 'user' else 'Listener'}: {t.text.strip()}" for t in recent[:-1]]
    return "\n".join([*lines, f"LATEST user message: {recent[-1].text.strip()}"])


async def gemini_label(client, model: str, turns) -> str:
    config = types.GenerateContentConfig(
        system_instruction=CLASSIFY_PROMPT,
        temperature=0,
        max_output_tokens=200,  # a cap, not a cost: some models pretty-print the JSON, and a cut-off answer counts as a crisis
        response_mime_type="application/json",
        response_schema=LABEL_SCHEMA,
    )
    if "2.5-flash" in model:
        config.thinking_config = types.ThinkingConfig(thinking_budget=0)
    resp = await client.aio.models.generate_content(model=model, contents=classifier_input(turns), config=config)
    return json.loads(resp.text)["label"]


async def classify(client, model: str, turns) -> bool:
    """True if the latest message is a crisis. Any failure counts as a crisis."""
    try:
        return await gemini_label(client, model, turns) != "normal"
    except Exception as e:
        log.warning("Safety check failed (%s), so this message counts as a crisis: %s", model, e)
        return True


# ── 3. What the user gets ────────────────────────────────────────────────────


def resources() -> dict:
    """Helpline details, from server/.env (see .env.example)."""
    return {
        "helpline_name": os.getenv("CRISIS_HELPLINE_NAME", "").strip(),
        "helpline_number": os.getenv("CRISIS_HELPLINE_NUMBER", "").strip(),
        "emergency_number": os.getenv("EMERGENCY_NUMBER", "112").strip() or "112",
        "directory_url": "https://findahelpline.com",
    }


def _spoken_number(number: str) -> str:
    # "988" read as digits ("9 8 8"), not "nine hundred eighty-eight".
    return " ".join(ch for ch in number if ch.isdigit())


def script(res: dict, spoken: bool = False) -> str:
    """The fixed crisis message. `spoken` spells numbers out digit by digit for text-to-speech."""
    num = _spoken_number if spoken else (lambda n: n)
    if res["helpline_number"]:
        name = res["helpline_name"] or "a crisis line"
        call = f"Please call {name} on {num(res['helpline_number'])}. They're there to listen, any time."
    else:
        call = "Please reach out to a crisis line now. The card on your screen can help you find one near you."
    return (
        "I'm really glad you told me, and I'm sorry you're carrying this. "
        "You deserve support from a real person right now. "
        f"{call} "
        f"If you might act on these thoughts, or you're in danger, call {num(res['emergency_number'])} now. "
        "I'm still here with you if you want to keep talking."
    )
