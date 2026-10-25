"""Tiny client for a local model served by Ollama. Nothing leaves the machine."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("SPEND_LOG_MODEL", "gemma4:e4b")


class LLMUnavailable(Exception):
    pass


def chat_json(system: str, user: str, schema: dict, timeout: int = 120) -> dict:
    """Ask the local model for JSON matching `schema`."""
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "format": schema,
        "stream": False,
        "think": False,
        "options": {"temperature": 0},
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            reply = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError) as e:
        raise LLMUnavailable(f"Couldn't reach Ollama at {OLLAMA_URL} ({e})") from e
    if "error" in reply:
        raise LLMUnavailable(reply["error"])
    return json.loads(reply["message"]["content"])
