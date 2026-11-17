"""Minimal Ollama client. Local model only, so cost is always 0."""
import os

import requests
from dotenv import load_dotenv

load_dotenv()

TIMEOUT_S = 120  # CPU-only generation of a short reply can be slow


def generate(prompt: str) -> dict:
    """Return {"text", "model", "cost": 0}, plus "error" (text "") if Ollama fails."""
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen3:8b")
    try:
        r = requests.post(
            f"{host}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
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
