"""Local model client (Ollama). Local model only, so cost is always 0.

- ``generate(prompt)``: plain text (used by the brief parser).
- ``generate_json(prompt, schema, system=None)``: JSON constrained by a JSON
  schema through Ollama's ``format`` parameter, thinking off, temperature 0.2,
  validated, retried once, then a clear ``LLMError``.
- ``FakeLLM``: a test double that returns canned JSON. It is selected only by
  passing it in (dependency injection, e.g. ``run_pipeline(..., llm_client=FakeLLM(...))``);
  no setting or environment variable switches it on.

Logging: steps call the model through ``RunContext.llm_json``, which logs every call.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

TIMEOUT_S = 120  # CPU-only generation of a short reply can be slow
TEMPERATURE = 0.2


class LLMError(RuntimeError):
    """The model could not be reached, or replied with something unusable twice."""


def _settings():
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen3:8b")
    return host, model


def generate(prompt: str) -> dict:
    """Return {"text", "model", "cost": 0}, plus "error" (text "") if Ollama fails."""
    host, model = _settings()
    try:
        r = requests.post(
            f"{host}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False, "think": False,
                  "options": {"temperature": TEMPERATURE}},
            timeout=TIMEOUT_S,
        )
        r.raise_for_status()
        return {"text": r.json().get("response", ""), "model": model, "cost": 0}
    except requests.RequestException as e:
        return {
            "text": "",
            "model": model,
            "cost": 0,
            "error": f"Could not get a reply from Ollama at {host} (model {model}): {e}. "
            "Is Ollama running and the model pulled?",
        }


# --- a small JSON-schema checker (the subset our schemas use) ------------------

_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def validate(value, schema: dict, path: str = "$") -> list:
    """Return a list of problems (empty when ``value`` matches ``schema``)."""
    problems = []
    expected = schema.get("type")
    if expected is not None:
        kinds = expected if isinstance(expected, list) else [expected]
        ok = False
        for kind in kinds:
            if kind == "integer":
                ok = ok or (isinstance(value, int) and not isinstance(value, bool))
            elif kind == "number":
                ok = ok or (isinstance(value, (int, float)) and not isinstance(value, bool))
            elif kind in _TYPES:
                ok = ok or isinstance(value, _TYPES[kind])
        if not ok:
            return [f"{path}: expected {expected}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{path}: missing key '{key}'")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                problems += validate(value[key], sub, f"{path}.{key}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append(f"{path}: needs at least {schema['minItems']} items")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                problems += validate(item, schema["items"], f"{path}[{i}]")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path}: {value} < {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path}: {value} > {schema['maximum']}")
    return problems


def _parse(text: str, schema: dict) -> dict:
    data = json.loads(text)
    problems = validate(data, schema)
    if problems:
        raise ValueError("; ".join(problems[:5]))
    return data


def generate_json(prompt: str, schema: dict, system: str | None = None, name: str | None = None) -> dict:
    """Ask the local model for JSON that matches ``schema``.

    Returns {"data": dict, "tokens": int, "model": str, "cost": 0, "seconds": float, "attempts": int}.
    Raises LLMError if Ollama is unreachable, or if the reply is invalid twice.
    """
    host, model = _settings()
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    started, tokens, last_error = time.monotonic(), 0, ""
    for attempt in (1, 2):
        try:
            r = requests.post(
                f"{host}/api/chat",
                json={"model": model, "messages": messages, "format": schema, "stream": False,
                      "think": False, "options": {"temperature": TEMPERATURE}},
                timeout=TIMEOUT_S,
            )
            r.raise_for_status()
            body = r.json()
        except (requests.RequestException, ValueError) as e:
            raise LLMError(f"Could not get a reply from Ollama at {host} (model {model}): {e}. "
                           "Is Ollama running and the model pulled?") from e
        tokens += int(body.get("prompt_eval_count") or 0) + int(body.get("eval_count") or 0)
        text = (body.get("message") or {}).get("content", "")
        try:
            data = _parse(text, schema)
            return {"data": data, "tokens": tokens, "model": model, "cost": 0,
                    "seconds": round(time.monotonic() - started, 2), "attempts": attempt}
        except ValueError as e:
            last_error = str(e)
    raise LLMError(f"The model {model} replied with invalid JSON twice ({name or 'call'}): {last_error}")


class OllamaClient:
    """The default client: real local model calls."""

    def generate_json(self, prompt, schema, system=None, name=None) -> dict:
        return generate_json(prompt, schema, system, name)


class FakeLLM:
    """MOCKED test double: returns canned JSON instead of calling a model.

    ``replies`` maps a call name (e.g. "researcher.extract") to a dict, a list of
    dicts (used in turn, the last one repeats) or a callable ``(prompt) -> dict``.
    Each reply is checked against the schema, like a real reply. Select it only by
    passing it in; it is never chosen by a setting.
    """
    model = "fake-llm (MOCKED)"

    def __init__(self, replies: dict):
        self.replies, self.calls = dict(replies), []

    @classmethod
    def from_file(cls, path) -> "FakeLLM":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def generate_json(self, prompt, schema, system=None, name=None) -> dict:
        self.calls.append({"name": name, "prompt": prompt, "system": system})
        if name not in self.replies:
            raise LLMError(f"FakeLLM has no canned reply for '{name}'")
        reply = self.replies[name]
        if isinstance(reply, list):
            used = sum(1 for c in self.calls if c["name"] == name) - 1
            reply = reply[min(used, len(reply) - 1)]
        if callable(reply):
            reply = reply(prompt)
        problems = validate(reply, schema)
        if problems:
            raise LLMError(f"FakeLLM reply for '{name}' does not match the schema: {problems[:3]}")
        return {"data": json.loads(json.dumps(reply)), "tokens": max(1, len(prompt) // 4),
                "model": self.model, "cost": 0, "seconds": 0.0, "attempts": 1}
