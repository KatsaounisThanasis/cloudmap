"""The one place cloudmap talks to a model - and it talks to a LOCAL one.

Keeping the transport in a single module is a design promise rather than tidiness:
there is exactly one outbound call in the whole tool, it points at localhost
(ollama), and it serves the only two jobs a model is allowed to do here -
proposing candidate edges that a deterministic rule then verifies, and narrating
an answer the graph already produced. No resource JSON ever reaches a third party.

Every failure returns an empty value instead of raising: cloudmap must stay fully
useful with no model installed, so "no model" is a normal state, not an error.
"""

import json
import os
import urllib.request

OLLAMA_URL = os.environ.get("CLOUDMAP_OLLAMA_URL", "http://localhost:11434/api/generate")
DEFAULT_MODEL = os.environ.get("CLOUDMAP_LLM_MODEL", "qwen2.5-coder:3b")


def generate(prompt, model=None, timeout=600, json_format=True):
    """Ask the local model once. Returns the response text, or "" on any failure
    (ollama absent, not running, timeout, malformed payload)."""
    body = {
        "model": model or DEFAULT_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0},
    }
    if json_format:
        body["format"] = "json"
    try:
        req = urllib.request.Request(
            OLLAMA_URL, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        resp = json.load(urllib.request.urlopen(req, timeout=timeout))
    except Exception:
        return ""
    return resp.get("response", "") or ""


def generate_json(prompt, model=None, timeout=600):
    """generate() plus a strict JSON parse. Returns {} on any failure, so callers
    never have to distinguish "model down" from "model answered nonsense"."""
    try:
        parsed = json.loads(generate(prompt, model=model, timeout=timeout, json_format=True))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}
