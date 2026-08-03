"""Load resources from a JSON fixture that mirrors `az graph query` output.

Accepts either a bare list of resources, or the object shape the CLI returns
(`{"data": [...]}`), so the same file works for fixtures and captured live data.
"""

import json


def load_fixture(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return data.get("data") or data.get("resources") or []
    return data
