"""
Safety layer (build plan step 3).

Every user message is screened two ways before a reply is allowed through:
  1. A keyword/regex check. A match is always treated as a crisis, with no model call.
  2. A separate Gemini call (fast model, temperature 0, JSON) that labels the latest message
     "normal" or "crisis" using the recent conversation. If that call fails (usually a quota
     limit, which is counted per model), it is tried once more on a second model. If both fail,
     a much broader word list decides: any risk word means crisis, otherwise normal.

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
normal: anything else, including sadness, stress, grief, figures of speech
("this traffic is killing me", "I'm dying to see it"), and good news or everyday chat (hobbies,
food, shows, feeling better, saying an exercise helped).
The text is a live speech transcript without punctuation, and speech recognition often mishears
words, so it can be garbled ("I have dle ID HD"). Read it for meaning. A garbled or unclear message
with no clear sign of risk is normal: the listener will simply ask what they meant.
Everyday stress is normal too: deadlines, exams, a demanding boss, not being able to focus,
feeling overwhelmed, asking "what should I do?".
When the words do point to possible risk but you're unsure how serious, choose crisis."""

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
    resp = await client.aio.models.generate_content(model=model, contents=classifier_input(turns), config=config)
    return parse_label(resp.text or "")


def parse_label(text: str) -> str:
    """'normal' or 'crisis' from the model's answer. Some models wrap the JSON in prose or code fences."""
    try:
        label = str(json.loads(text)["label"]).strip().lower()
    except (ValueError, KeyError, TypeError):
        m = re.search(r'"label"\s*:\s*"(\w+)"', text) or re.search(r"\b(crisis|normal)\b", text, re.I)
        if not m:
            raise ValueError(f"No label in the answer: {text[:80]!r}")
        label = m.group(1).lower()
    if label not in ("normal", "crisis"):
        raise ValueError(f"Unknown label {label!r}")
    return label


# When no safety model can be reached (quota, outage, a retired model), the message still gets a
# check: this much broader list than _PATTERNS. Any match counts as a crisis, as before; with no
# match the message is treated as normal, so a technical problem doesn't give everyone the crisis card.
_RISK_WORDS = re.compile(
    r"\b(die|dying|died|dead|death|kill\w*|suicid\w*|hurt\w*|harm\w*|cut(ting)? (my|me)|overdos\w*|pills?"
    r"|end (it|my|everything)|give up on (life|everything)|no point|hopeless|can'?t (go on|take (it|this) anymore)"
    r"|disappear|not (be )?here anymore|gone forever|unsafe|danger\w*|abus\w*|hits? me|beat(s|ing)? me|threat\w*"
    r"|weapon|gun|knife|rope|jump(ing)? (off|from)|bridge|goodbye (letters?|notes?|forever)|unalive\w*"
    r"|(cutting|hurting|harming|burning|starving) myself|hang(ing|ed)? myself|(end|ending|take|taking|took) (my|my own) life"
    r"|better off without|(want|wanna|reason|point) to (live|be alive)|giving (my |all my )?(things|stuff|belongings) away"
    r"|(never|not) wake up|burden to (everyone|you|them))\b"
)


def risk_words(text: str) -> bool:
    return keyword_crisis(text) or bool(_RISK_WORDS.search(_normalise(text)))


async def classify(client, model: str, turns, fallback: str | None = None, fallback_client=None) -> bool:
    """True if the latest message is a crisis. If `model` fails, `fallback` is tried (on `fallback_client`,
    e.g. Gemini when replies come from Groq); if that fails too, the broad risk-word list decides."""
    tries = [(client, model)]
    other = fallback_client or client
    if fallback and (other is not client or fallback != model):
        tries.append((other, fallback))
    for i, (c, m) in enumerate(tries):
        try:
            label = await gemini_label(c, m, turns)
        except Exception as e:
            last = i == len(tries) - 1
            log.warning(
                "Safety check failed (%s)%s: %s",
                m,
                ", so this message counts as a crisis" if last else f", trying {tries[i + 1][1]}",
                getattr(e, "message", None) or e,
            )
            continue
        if label != "normal":
            log.warning("Safety check (%s) labelled the latest message a crisis.", m)
        return label != "normal"
    latest = next((t.text for t in reversed(turns) if t.role == "user"), "")
    if risk_words(latest):
        log.warning("No safety model answered and the message has risk words, so it counts as a crisis.")
        return True
    log.warning("No safety model answered; the message has no risk words, so it's treated as normal.")
    return False


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
