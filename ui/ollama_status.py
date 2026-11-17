"""Is the local model ready? One GET to Ollama's ``/api/tags`` (read-only, local)."""
from __future__ import annotations

import os

import requests


def settings() -> tuple:
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen3:8b")
    return host, model


def _matches(name: str, model: str) -> bool:
    if name == model:
        return True
    return ":" not in model and name == f"{model}:latest"


def check(timeout: float = 1.5) -> dict:
    """{"state": "ready" | "loading" | "offline", "model", "host", "message"}. Never raises."""
    host, model = settings()
    try:
        r = requests.get(f"{host}/api/tags", timeout=timeout)
        r.raise_for_status()
        names = [m.get("name") or m.get("model") or "" for m in (r.json().get("models") or [])]
    except Exception:
        return {"state": "offline", "model": model, "host": host,
                "message": f"Ollama is not reachable at {host}. The app works, but the agents need "
                           "the local model to answer."}
    if any(_matches(n, model) for n in names):
        return {"state": "ready", "model": model, "host": host, "message": f"{model} is ready."}
    return {"state": "loading", "model": model, "host": host,
            "message": f"Model loading: {model} is still downloading into Ollama. This can take a few "
                       "minutes on the first start. The page updates when you refresh."}
