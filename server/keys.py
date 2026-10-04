"""
Gemini API key rotation.

Free-tier keys run out (per-minute and per-day limits). With several keys set, every call goes to
the next healthy key in turn, which spreads the load, and a key that hits its quota, or turns out
to be invalid, is rested for a while and the call is retried at once on another key. With every
key resting, the one that rests the shortest is tried anyway.

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
MAX_REST = 60 * 60
BAD_KEY_REST = 30 * 60  # invalid, disabled or not allowed: rest long, someone has to fix it


def load_keys() -> list[str]:
    """Every key set in the environment, in order, without duplicates or the .env.example placeholder."""
    found = re.split(r"[\s,;]+", os.getenv("GEMINI_API_KEYS", ""))
    found += [os.getenv("GEMINI_API_KEY", ""), os.getenv("GOOGLE_API_KEY", "")]
    numbered = sorted(
        (int(m.group(1)), v) for k, v in os.environ.items() if (m := re.fullmatch(r"GEMINI_API_KEY_(\d+)", k))
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


class RotatingClient:
    def __init__(self, keys: list[str], make_client=genai.Client):
        if not keys:
            raise ValueError("No Gemini API keys.")
        self.keys = keys
        self._make = make_client
        self._clients: dict[int, genai.Client] = {}
        self._rest_until = [0.0] * len(keys)
        self._strikes = [0] * len(keys)  # 429s in a row, per key
        self._next = 0
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self.generate_content))

    def _client(self, i: int) -> genai.Client:
        if i not in self._clients:
            self._clients[i] = self._make(api_key=self.keys[i])
        return self._clients[i]

    def _order(self) -> list[int]:
        """Keys to try for one call: healthy ones round-robin from the next in turn, then resting ones, soonest back first."""
        n, now = len(self.keys), time.monotonic()
        start = self._next
        self._next = (self._next + 1) % n
        ring = [(start + k) % n for k in range(n)]
        healthy = [i for i in ring if self._rest_until[i] <= now]
        resting = sorted((i for i in ring if self._rest_until[i] > now), key=lambda i: self._rest_until[i])
        return healthy + resting

    def _rest(self, i: int, why: str):
        if why == "quota":
            self._strikes[i] += 1
            secs = min(MAX_REST, RATE_LIMIT_REST * 2 ** (self._strikes[i] - 1))
        else:
            secs = BAD_KEY_REST
        self._rest_until[i] = time.monotonic() + secs
        log.warning("Gemini key #%d %s, resting it for %ds (%d keys set).", i + 1, why, secs, len(self.keys))

    def status(self) -> dict:
        now = time.monotonic()
        return {"keys": len(self.keys), "resting": sum(t > now for t in self._rest_until)}

    async def generate_content(self, **kwargs):
        last = None
        for i in self._order():
            try:
                resp = await self._client(i).aio.models.generate_content(**kwargs)
            except Exception as e:
                why = failure(e)
                if why is None:
                    raise
                if why != "busy":
                    self._rest(i, why)
                last = e
                continue
            self._strikes[i] = 0
            return resp
        raise last
