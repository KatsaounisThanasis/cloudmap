"""A guard on the repository itself.

Fixtures are the one place where real tenant data can plausibly end up in git -
someone captures an export to debug an extractor, it works, and it gets committed.
The scrubber exists to prevent that, but a scrubber only helps if it was actually
run. This test checks the committed artifacts directly, so forgetting fails CI
rather than leaking quietly.
"""

import glob
import json
import os
import re

from cloudmap.scrub import _SECRET_PATTERNS

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")

_GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


def _obviously_fake(guid):
    """Hand-written and scrubber-generated GUIDs run the same character several
    times (1111-2222, 00000001-0000-...); a real v4 GUID is random and rarely
    does. This is a smell test, not a proof - a real GUID with a 4-long run would
    slip through. It exists to catch the careless paste, not a determined one."""
    body = guid.replace("-", "").lower()
    longest = run = 1
    for prev, cur in zip(body, body[1:]):
        run = run + 1 if cur == prev else 1
        longest = max(longest, run)
    return longest >= 4 or len(set(body)) <= 4


def _fixture_files():
    return sorted(glob.glob(os.path.join(FIXTURES, "*.json")))


def test_there_are_fixtures_to_check():
    assert _fixture_files(), "no fixtures found - this guard would pass vacuously"


def test_no_fixture_contains_a_credential():
    for path in _fixture_files():
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for pat in _SECRET_PATTERNS:
            hits = [m.group(0) for m in pat.finditer(text) if "REDACTED" not in m.group(0)]
            assert not hits, f"{os.path.basename(path)} looks like it carries a secret: {hits[:2]}"


def test_no_fixture_contains_a_real_looking_guid():
    from cloudmap.scrub import KEEP_GUIDS

    for path in _fixture_files():
        with open(path, encoding="utf-8") as f:
            text = f.read()
        suspicious = [g for g in _GUID.findall(text)
                      if not _obviously_fake(g) and g.lower() not in KEEP_GUIDS]
        assert not suspicious, (
            f"{os.path.basename(path)} contains GUID(s) that do not look scrubbed: "
            f"{suspicious[:3]} - run `cloudmap scrub` on it")


def test_every_fixture_parses_as_an_export():
    for path in _fixture_files():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        rows = data.get("data", data) if isinstance(data, dict) else data
        assert isinstance(rows, list) and rows, f"{os.path.basename(path)} has no rows"
