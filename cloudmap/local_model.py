"""The one place cloudmap talks to a model - and it talks to a LOCAL one.

Keeping the transport in a single module is a design promise rather than tidiness:
there is exactly one outbound call in the whole tool, it points at localhost, and
it serves the only two jobs a model is allowed to do here - proposing candidate
edges that a deterministic rule then verifies, and narrating an answer the graph
already produced. No resource JSON ever reaches a third party.

Two wire formats are spoken, chosen from the URL:
- ollama's native /api/generate (the default, http://localhost:11434)
- the OpenAI-compatible /v1/chat/completions that LM Studio, llama.cpp server,
  vLLM, LocalAI (and ollama itself) expose - any URL whose path contains /v1/
  or ends in /chat/completions is treated as OpenAI-compatible.

Configure with CLOUDMAP_LLM_URL and CLOUDMAP_LLM_MODEL. CLOUDMAP_OLLAMA_URL is
honoured as a legacy alias.

Every failure returns an empty value instead of raising: cloudmap must stay fully
useful with no model installed, so "no model" is a normal state, not an error.
"""

import json
import os
import urllib.parse
import urllib.request

LLM_URL = (os.environ.get("CLOUDMAP_LLM_URL")
           or os.environ.get("CLOUDMAP_OLLAMA_URL")
           or "http://localhost:11434/api/generate")
DEFAULT_MODEL = os.environ.get("CLOUDMAP_LLM_MODEL", "qwen2.5-coder:3b")

# Legacy alias: extract.llm re-exported this name in earlier releases.
OLLAMA_URL = LLM_URL


def _is_openai_compatible(url):
    path = urllib.parse.urlparse(url).path
    return "/v1/" in path or path.rstrip("/").endswith("chat/completions")


def _request_body(prompt, model, json_format):
    if _is_openai_compatible(LLM_URL):
        body = {
            "model": model or DEFAULT_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "stream": False,
        }
        if json_format:
            body["response_format"] = {"type": "json_object"}
        return body
    body = {
        "model": model or DEFAULT_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0},
    }
    if json_format:
        body["format"] = "json"
    return body


def _response_text(resp):
    if _is_openai_compatible(LLM_URL):
        choices = resp.get("choices") or []
        if not choices:
            return ""
        return (choices[0].get("message") or {}).get("content", "") or ""
    return resp.get("response", "") or ""


def generate(prompt, model=None, timeout=600, json_format=True):
    """Ask the local model once. Returns the response text, or "" on any failure
    (server absent, not running, timeout, malformed payload)."""
    try:
        req = urllib.request.Request(
            LLM_URL, data=json.dumps(_request_body(prompt, model, json_format)).encode(),
            headers={"Content-Type": "application/json"})
        resp = json.load(urllib.request.urlopen(req, timeout=timeout))
        return _response_text(resp)
    except Exception:
        return ""


def generate_json(prompt, model=None, timeout=600):
    """generate() plus a strict JSON parse. Returns {} on any failure, so callers
    never have to distinguish "model down" from "model answered nonsense"."""
    try:
        parsed = json.loads(generate(prompt, model=model, timeout=timeout, json_format=True))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}
