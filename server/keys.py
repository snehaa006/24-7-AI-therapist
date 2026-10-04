"""
Gemini API key rotation.

Free-tier keys run out (per-minute and per-day limits, counted per model). With several keys set,
every call goes to the next healthy key in turn, which spreads the load, and a key that hits its
quota for a model is rested for that model (for as long as Google says, when it says) and the call
is retried at once on another key. An invalid key is rested for every model. With every key resting
for a model, only the one back soonest is tried, so a used-up model fails fast.

Quotas belong to the Google Cloud project, not the key: several keys made in the same project
share one quota, so for more quota each key must come from a different project.

Set the keys in server/.env, any of:
    GEMINI_API_KEYS=key1,key2,key3
    GEMINI_API_KEY=key1        GEMINI_API_KEY_2=key2   GEMINI_API_KEY_3=key3 …

`RotatingClient` stands in for `genai.Client`: the app only calls `client.aio.models.generate_content`.
"""

import logging
import os
import re
import time
from types import SimpleNamespace

from google import genai
from google.genai import errors

log = logging.getLogger("keys")

PLACEHOLDERS = {"", "your-key-here", "key1", "key2", "key3"}
RATE_LIMIT_REST = 60  # seconds a key rests after a 429; doubles on each 429 in a row (daily quota gone)
MAX_REST = 24 * 60 * 60
BAD_KEY_REST = 30 * 60  # invalid, disabled or not allowed: rest long, someone has to fix it


def load_keys(name: str = "GEMINI_API_KEY", also: tuple[str, ...] = ("GOOGLE_API_KEY",)) -> list[str]:
    """Every key set in the environment (NAMES=a,b · NAME · NAME_2, NAME_3…), in order, without duplicates or placeholders."""
    found = re.split(r"[\s,;]+", os.getenv(f"{name}S", ""))
    found += [os.getenv(name, "")] + [os.getenv(n, "") for n in also]
    numbered = sorted(
        (int(m.group(1)), v) for k, v in os.environ.items() if (m := re.fullmatch(rf"{name}_(\d+)", k))
    )
    found += [v for _, v in numbered]
    keys = []
    for k in (k.strip() for k in found):
        if k not in PLACEHOLDERS and k not in keys:
            keys.append(k)
    return keys


def failure(e: Exception) -> str | None:
    """'quota' or 'bad_key' when another key could succeed, 'busy' for a server-side hiccup, else None."""
    if not isinstance(e, errors.APIError):
        return None
    msg = str(e).lower()
    if e.code == 429 or "resource_exhausted" in msg or "quota" in msg:
        return "quota"
    if e.code in (401, 403) or (e.code == 400 and "api key" in msg):
        return "bad_key"
    if e.code in (500, 502, 503, 504):
        return "busy"
    return None


def retry_delay(e: Exception) -> int:
    """Seconds Google asks us to wait before retrying (its RetryInfo), or 0 if it didn't say."""
    m = re.search(r"retryDelay['\"]?\s*:\s*['\"](\d+(?:\.\d+)?)s", str(getattr(e, "details", "")) + str(e))
    return int(float(m.group(1))) if m else 0


def quota_message(e: Exception, what: str, keys: int) -> str:
    """A short, readable explanation for a used-up quota (the raw error is a page of JSON)."""
    secs = retry_delay(e)
    when = f" It resets in about {round(secs / 3600)} hours." if secs >= 3600 else ""
    tip = " Keys made in the same project share one quota, so add keys from other projects or accounts." if keys > 1 else ""
    return f"{what} limit reached on all {keys} key{'s' * (keys > 1)}.{when}{tip}"


class RotatingClient:
    def __init__(self, keys: list[str], make_client=genai.Client):
        if not keys:
            raise ValueError("No Gemini API keys.")
        self.keys = keys
        self._make = make_client
        self._clients: dict[int, genai.Client] = {}
        self._rest_until: dict[tuple[int, str], float] = {}  # (key, model) → when it may be used again; model '*' = all
        self._strikes: dict[tuple[int, str], int] = {}  # 429s in a row
        self._next = 0
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self.generate_content))

    def _client(self, i: int) -> genai.Client:
        if i not in self._clients:
            self._clients[i] = self._make(api_key=self.keys[i])
        return self._clients[i]

    def _back_at(self, i: int, model: str) -> float:
        return max(self._rest_until.get((i, model), 0.0), self._rest_until.get((i, "*"), 0.0))

    def _order(self, model: str) -> list[int]:
        """Keys to try for one call: healthy ones round-robin from the next in turn; if none, the one back soonest."""
        n, now = len(self.keys), time.monotonic()
        start = self._next
        self._next = (self._next + 1) % n
        ring = [(start + k) % n for k in range(n)]
        healthy = [i for i in ring if self._back_at(i, model) <= now]
        return healthy or [min(ring, key=lambda i: self._back_at(i, model))]

    def _rest(self, i: int, model: str, why: str, e: Exception):
        if why == "quota":
            k = (i, model)
            self._strikes[k] = self._strikes.get(k, 0) + 1
            secs = retry_delay(e) or RATE_LIMIT_REST * 2 ** (self._strikes[k] - 1)
            secs = min(MAX_REST, secs)
        else:
            model, secs = "*", BAD_KEY_REST
        self._rest_until[(i, model)] = time.monotonic() + secs
        log.warning("Gemini key #%d %s for %s, resting it for %ds (%d keys set).", i + 1, why, model, secs, len(self.keys))

    def status(self, model: str | None = None) -> dict:
        """How many keys are set, and how many are resting (for `model`, or for any model)."""
        now = time.monotonic()
        if model:
            resting = sum(self._back_at(i, model) > now for i in range(len(self.keys)))
        else:
            resting = len({i for (i, _), t in self._rest_until.items() if t > now})
        return {"keys": len(self.keys), "resting": resting}

    async def generate_content(self, **kwargs):
        model = str(kwargs.get("model", ""))
        last = None
        for i in self._order(model):
            try:
                resp = await self._client(i).aio.models.generate_content(**kwargs)
            except Exception as e:
                why = failure(e)
                if why is None:
                    raise
                if why != "busy":
                    self._rest(i, model, why, e)
                last = e
                continue
            self._strikes.pop((i, model), None)
            return resp
        raise last
