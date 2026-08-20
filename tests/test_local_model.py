"""The model transport: one module, two wire formats, and a hard promise that
every failure returns an empty value - cloudmap must stay fully useful with no
model installed, so "no model" is a normal state, not an error.

No real server is contacted: urllib.request.urlopen is faked per test.
"""

import io
import json

import pytest

from cloudmap import local_model


class _Served:
    """Capture the request body and serve a canned JSON response."""

    def __init__(self, response):
        self.response, self.body = response, None

    def __call__(self, req, timeout=None):
        self.body = json.loads(req.data.decode())
        return io.BytesIO(json.dumps(self.response).encode())


# --- wire-format selection --------------------------------------------------------

@pytest.mark.parametrize("url,openai", [
    ("http://localhost:11434/api/generate", False),
    ("http://localhost:11434", False),
    ("http://localhost:1234/v1/chat/completions", True),
    ("http://localhost:8080/v1/completions", True),
    ("http://gpu-box:8000/openai/v1/chat/completions", True),
    ("http://localhost:9000/chat/completions", True),
])
def test_the_url_shape_selects_the_wire_format(url, openai):
    assert local_model._is_openai_compatible(url) is openai


def test_ollama_speaks_prompt_and_reads_response(monkeypatch):
    served = _Served({"response": "hello"})
    monkeypatch.setattr(local_model, "LLM_URL", "http://localhost:11434/api/generate")
    monkeypatch.setattr(local_model.urllib.request, "urlopen", served)

    assert local_model.generate("hi") == "hello"
    assert served.body["prompt"] == "hi"
    assert served.body["format"] == "json"
    assert served.body["options"] == {"temperature": 0}


def test_openai_compatible_speaks_messages_and_reads_choices(monkeypatch):
    served = _Served({"choices": [{"message": {"content": "hello"}}]})
    monkeypatch.setattr(local_model, "LLM_URL", "http://localhost:1234/v1/chat/completions")
    monkeypatch.setattr(local_model.urllib.request, "urlopen", served)

    assert local_model.generate("hi") == "hello"
    assert served.body["messages"] == [{"role": "user", "content": "hi"}]
    assert served.body["response_format"] == {"type": "json_object"}
    assert "prompt" not in served.body


# --- the never-raises contract ----------------------------------------------------

def test_a_dead_server_returns_empty_never_raises(monkeypatch):
    def down(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(local_model.urllib.request, "urlopen", down)

    assert local_model.generate("hi") == ""
    assert local_model.generate_json("hi") == {}


@pytest.mark.parametrize("response", [
    {},                                        # no recognisable field
    {"choices": []},                           # openai shape, empty
    {"response": None},                        # ollama shape, null
])
def test_a_malformed_payload_returns_empty(monkeypatch, response):
    monkeypatch.setattr(local_model, "LLM_URL", "http://localhost:1234/v1/chat/completions"
                        if "choices" in response else "http://localhost:11434/api/generate")
    monkeypatch.setattr(local_model.urllib.request, "urlopen", _Served(response))

    assert local_model.generate("hi") == ""


@pytest.mark.parametrize("text", ["not json", "[]", "null", ""])
def test_generate_json_returns_a_dict_or_nothing(monkeypatch, text):
    monkeypatch.setattr(local_model, "generate", lambda *a, **k: text)

    assert local_model.generate_json("hi") == {}
