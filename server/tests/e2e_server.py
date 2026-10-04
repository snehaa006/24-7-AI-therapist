"""
Backend for the Playwright full-stack tests (liquid-voice-orb/e2e/actions.spec.js, feedback.spec.js):
the real app, with a throwaway SQLite database and every Gemini call stubbed with canned, rule-based
answers. Like the real model, the stubs keep to the action type the server picked.

    python tests/e2e_server.py 8011
"""

import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["MEMORY_DB"] = str(Path(tempfile.mkdtemp()) / "e2e.db")
os.environ["GEMINI_API_KEY"] = "stub"

import uvicorn  # noqa: E402

import actions  # noqa: E402
import main  # noqa: E402
import memory  # noqa: E402
import safety  # noqa: E402

WALK = {
    "title": "Walk with your music",
    "intro": "Let's do a short walk with that playlist you love.",
    "steps": [
        {"say": "Put on a song you love and stand up slowly.", "seconds": 30},
        {"say": "Start walking at an easy pace, letting your arms swing.", "seconds": 60},
        {"say": "Notice three things you can see as you walk.", "seconds": 45},
        {"say": "Let your breath slow down to match your steps.", "seconds": 45},
        {"say": "Slow down and come to a gentle stop.", "seconds": 20},
    ],
    "closing": "Nice work. Take a moment before you sit back down.",
}
BREATHING = {
    "title": "Slow breathing",
    "intro": "Let's slow your breathing down together.",
    "steps": [
        {"say": "Sit comfortably and let your shoulders drop.", "seconds": 30},
        {"say": "Breathe in for four, and out for six.", "seconds": 60},
        {"say": "Keep that slow rhythm going.", "seconds": 45},
        {"say": "Notice the air, cool on the way in.", "seconds": 45},
        {"say": "Let your breathing settle back to normal.", "seconds": 20},
    ],
    "closing": "Well done. Take a moment before you carry on.",
}
EXERCISES = {"walk": WALK, "breathing": BREATHING}
SUGGESTIONS = {  # type → (action, line)
    "breathing": ("a few minutes of slow breathing", "That sounds like a lot. Want to try a few minutes of slow breathing?"),
    "walk": (
        "a five-minute walk with your favourite music",
        "That sounds like a lot. Want to try a five-minute walk with your favourite music?",
    ),
}


async def label(client, model, turns):
    return "crisis" if "die" in turns[-1].text.lower() else "normal"


async def reply(contents, memories=""):
    return "I hear you. Tell me more?"


async def suggest(client, model, prompt):
    kind = re.search(r"^Type: (\w+)", prompt, re.M)[1]
    action, line = SUGGESTIONS.get(kind, (f"a little {kind}", f"Want to try a little {kind}?"))
    return {"suggest": True, "type": kind, "action": action, "line": line}


async def decide(client, model, prompt):
    answer = prompt.rsplit("Answer:", 1)[-1].lower()
    if "minute" in answer:
        return {"decision": "later", "minutes": 1, "clock": ""}
    if "later" in answer:
        return {"decision": "later", "minutes": 0, "clock": ""}
    if "no" in answer.split():
        return {"decision": "no", "minutes": 0, "clock": ""}
    return {"decision": "now", "minutes": 0, "clock": ""}


async def helped(client, model, prompt):
    answer = prompt.lower()
    if "didn't help" in answer or "worse" in answer:
        return "no"
    return "yes" if any(w in answer for w in ("better", "calmer", "lighter")) else "somewhat"


async def exercise(client, model, prompt):
    return EXERCISES.get(re.search(r"\(type: (\w+)\)", prompt)[1], WALK)


async def extract(client, model, known, turns):
    return {"facts": [], "patterns": [], "drop_ids": [], "summary": ""}


async def greeting(client, model, user_id):
    return None


safety.gemini_label = label
main.generate_reply = reply
actions.gemini_suggest = suggest
actions.gemini_decide = decide
actions.gemini_helped = helped
actions.gemini_exercise = exercise
memory.gemini_extract = extract
memory.gemini_greeting = greeting

if __name__ == "__main__":
    uvicorn.run(main.app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8011, log_level="warning")
