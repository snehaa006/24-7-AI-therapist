"""
Groq or Grok (xAI) for the text calls, as an alternative to Gemini.

Set LLM_PROVIDER=groq (and GROQ_API_KEY) or LLM_PROVIDER=grok (and XAI_API_KEY) in server/.env.
Every text call (reply, end-of-turn check, safety label, suggestions, exercises, memory) then goes
to that service's OpenAI-compatible chat API. The natural voices stay on Gemini text-to-speech
(or use the device voices).

`CompatClient` stands in for `genai.Client`: it takes the same `generate_content(model, contents,
config)` call the app already makes and translates it to a chat completion. Errors are raised as
google.genai `APIError`s, so key rotation (keys.py) and the error messages work unchanged.
"""

import json
import os
from dataclasses import dataclass

import httpx
from google.genai import errors, types

TIMEOUT = 30.0


@dataclass(frozen=True)
class Provider:
    name: str  # shown in messages
    key_env: str  # XAI_API_KEY → also XAI_API_KEYS, XAI_API_KEY_2…
    base_url: str
    model: str  # replies, exercises, memory, safety
    fast_model: str  # quick checks: end of turn, suggest?, yes/later/no, did it help?
    reasoning_effort: str  # sent when not empty
    strict_schema: bool  # json_schema output on every model; else JSON mode with the schema in the prompt


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def provider(name: str) -> Provider | None:
    if name == "groq":
        return Provider(
            name="Groq",
            key_env="GROQ_API_KEY",
            base_url=_env("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
            model=_env("GROQ_MODEL", "llama-3.3-70b-versatile"),
            fast_model=_env("GROQ_FAST_MODEL", "llama-3.1-8b-instant"),
            reasoning_effort=_env("GROQ_REASONING_EFFORT", ""),
            strict_schema=False,  # only some Groq models take json_schema
        )
    if name == "grok":
        model = _env("GROK_MODEL", "grok-4.3")
        return Provider(
            name="Grok",
            key_env="XAI_API_KEY",
            base_url=_env("XAI_BASE_URL", "https://api.x.ai/v1"),
            model=model,
            fast_model=_env("GROK_FAST_MODEL", model),
            reasoning_effort=_env("GROK_REASONING_EFFORT", "none"),  # no thinking: spoken replies need speed
            strict_schema=True,
        )
    return None


def json_schema(schema):
    """Gemini's schema dialect (type "OBJECT", "STRING"…) → standard JSON Schema."""
    if isinstance(schema, list):
        return [json_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k == "type" and isinstance(v, str):
            out[k] = v.lower()
        elif k == "properties":
            out[k] = {name: json_schema(s) for name, s in v.items()}
        elif k in ("items", "anyOf"):
            out[k] = json_schema(v)
        elif k in ("propertyOrdering", "nullable"):
            continue
        else:
            out[k] = v
    if out.get("type") == "object":
        out.setdefault("additionalProperties", False)
    return out


def messages(contents, system: str | None) -> list[dict]:
    """Gemini contents (a string, or a list of Content with roles user/model) → chat messages."""
    msgs = [{"role": "system", "content": system}] if system else []
    if isinstance(contents, str):
        return msgs + [{"role": "user", "content": contents}]
    for c in contents:
        if isinstance(c, str):
            msgs.append({"role": "user", "content": c})
            continue
        text = "".join(p.text or "" for p in (c.parts or []))
        msgs.append({"role": "assistant" if c.role == "model" else "user", "content": text})
    return msgs


def request_body(p: Provider, model: str, contents, config: types.GenerateContentConfig | None) -> dict:
    """`model` is the app's (Gemini) model setting: the "lite" ones are the quick checks."""
    config = config or types.GenerateContentConfig()
    system = config.system_instruction if isinstance(config.system_instruction, str) else None
    schema = json_schema(config.response_schema) if config.response_schema is not None else None
    if schema and not p.strict_schema:
        system = f"{system or ''}\n\nAnswer with a single JSON object, and nothing else, matching this JSON Schema:\n{json.dumps(schema)}".strip()
    body = {"model": p.fast_model if "lite" in model else p.model, "messages": messages(contents, system)}
    if config.temperature is not None:
        body["temperature"] = config.temperature
    if config.max_output_tokens:
        body["max_tokens"] = config.max_output_tokens
    if p.reasoning_effort:
        body["reasoning_effort"] = p.reasoning_effort
    if schema and p.strict_schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}
    elif schema or config.response_mime_type == "application/json":
        body["response_format"] = {"type": "json_object"}
    return body


class Response:
    """The one part of a Gemini response the app reads: `.text`."""

    def __init__(self, text: str):
        self.text = text


class CompatClient:
    def __init__(self, p: Provider, api_key: str, http: httpx.AsyncClient | None = None):
        self.p = p
        self.api_key = api_key
        self._http = http
        self.aio = self  # same shape as genai.Client: client.aio.models.generate_content(...)
        self.models = self

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=self.p.base_url, timeout=TIMEOUT)
        return self._http

    async def generate_content(self, *, model: str, contents, config: types.GenerateContentConfig | None = None):
        resp = await self._client().post(
            "/chat/completions",
            json=request_body(self.p, model, contents, config),
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        try:
            data = resp.json()
        except ValueError:
            data = {"error": {"message": resp.text[:300]}}
        if resp.status_code >= 400:
            err = data.get("error") if isinstance(data.get("error"), dict) else {"message": str(data.get("error") or data)}
            message = f"{self.p.name}: {err.get('message') or err}"
            raise errors.APIError(resp.status_code, {"error": {"code": resp.status_code, "message": message, "status": ""}})
        return Response(data["choices"][0]["message"].get("content") or "")
