"""The scrubber's contract: change every identity, change no relationship.

A scrubbed export is only worth committing if the graph it produces is the same
graph the real export produced. If pseudonymising the vault broke the app setting
that points at it, the resulting fixture would quietly test nothing - which is
worse than having no golden test at all. So the central test here compares the
STRUCTURE of the graph before and after, and separately checks that no real
identifier or credential survived.
"""

import copy
import json
import os

from cloudmap.graph import build_graph
from cloudmap.scrub import redact_credentials, scrub

FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "contoso.json")


def _raw():
    with open(FIX, encoding="utf-8") as f:
        data = json.load(f)
    return data["data"] if isinstance(data, dict) else data


def _shape(graph):
    """A graph's structure with all identity removed: which KINDS of resource
    depend on which, and how. Two graphs with the same shape are the same map."""
    t = {n.id: n.type for n in graph.nodes.values()}
    return sorted((t[e.source], t[e.target], e.kind) for e in graph.edges)


def test_scrubbing_does_not_change_the_graph():
    raw = _raw()
    before = build_graph(copy.deepcopy(raw))
    after = build_graph(scrub(copy.deepcopy(raw))[0])

    assert _shape(after) == _shape(before)
    assert len(after.nodes) == len(before.nodes)


def test_a_reference_still_resolves_to_its_target():
    # The specific correlation the whole exercise is for: the app setting names a
    # vault host, and that host must still be the pseudonymised vault - not some
    # dangling string pointing at a name that no longer exists.
    scrubbed, _ = scrub(copy.deepcopy(_raw()))
    g = build_graph(scrubbed)
    kinds = {e.kind for e in g.edges}

    assert any("reads-secret" in k for k in kinds)
    assert any("connects-to" in k for k in kinds)
    assert all(e.target in g.nodes for e in g.edges)     # nothing dangles


def test_real_identifiers_do_not_survive():
    scrubbed, stats = scrub(copy.deepcopy(_raw()))
    text = json.dumps(scrubbed)

    assert "contoso-web" not in text
    assert "contoso-kv" not in text
    assert "contosostg" not in text
    assert "11111111-1111-1111-1111-111111111111" not in text
    assert stats["tokens"] > 5


def test_service_suffixes_survive_because_the_rules_read_them():
    scrubbed, _ = scrub(copy.deepcopy(_raw()))
    text = json.dumps(scrubbed)

    assert ".vault.azure.net" in text          # how a KV reference is recognised
    assert ".database.windows.net" in text
    assert ".blob.core.windows.net" in text


def test_resource_types_are_never_rewritten():
    scrubbed, _ = scrub(copy.deepcopy(_raw()))

    assert {r["type"] for r in scrubbed} == {r["type"] for r in _raw()}


def test_scrubbing_is_deterministic():
    # Same input twice must give byte-identical output, or a fixture refresh
    # would show up as a diff of pure noise.
    a, _ = scrub(copy.deepcopy(_raw()))
    b, _ = scrub(copy.deepcopy(_raw()))

    assert json.dumps(a) == json.dumps(b)


def test_credentials_are_redacted_not_pseudonymised():
    text, n = redact_credentials(
        "Server=tcp:db.database.windows.net,1433;User ID=svc;Password=hunter2xyz;"
        "AccountKey=abc123def456;")

    assert n >= 2
    assert "hunter2xyz" not in text
    assert "abc123def456" not in text
    assert "db.database.windows.net" in text          # the dependency survives


def test_instrumentation_key_is_kept_as_a_correlation_id():
    # It is a GUID, so the GUID pass pseudonymises it consistently; redacting it
    # instead would sever every app -> App Insights edge.
    text, _ = redact_credentials(
        "InstrumentationKey=8bbf4b0e-9f9a-4d3a-9b1e-2f0f0d7a1c22;IngestionEndpoint=https://x/")

    assert "8bbf4b0e-9f9a-4d3a-9b1e-2f0f0d7a1c22" in text


def test_builtin_role_guids_are_kept_because_they_are_public_constants():
    # Caught by the shape test first: pseudonymising these turned "AcrPull" into
    # "custom role", degrading the fixture while looking like a clean scrub.
    acr_pull = "7f951dda-4ed3-4680-a7ca-43fe172d538d"
    scrubbed, _ = scrub([{
        "id": "/ra1", "name": "ra1", "type": "microsoft.authorization/roleassignments",
        "properties": {"principalId": "aaaaaaaa-1111-2222-3333-444444444444",
                       "roleDefinitionId": f"/providers/Microsoft.Authorization/"
                                           f"roleDefinitions/{acr_pull}",
                       "scope": "/subscriptions/x"}}])
    text = json.dumps(scrubbed)

    assert acr_pull in text                                        # public constant, kept
    assert "aaaaaaaa-1111-2222-3333-444444444444" not in text      # tenant principal, gone


def test_private_addresses_are_kept_as_topology():
    scrubbed, _ = scrub([{"id": "/x", "name": "x", "type": "microsoft.network/virtualnetworks",
                          "properties": {"addressSpace": {"addressPrefixes": ["10.0.0.0/16"]},
                                         "publicIp": "52.174.10.11"}}])
    text = json.dumps(scrubbed)

    assert "10.0.0.0/16" in text            # private range: it IS the diagram
    assert "52.174.10.11" not in text       # public address: identifying
    assert "203.0.113." in text


# A live storage key reached a capture through this: the redactor kept whatever
# preceded the first `=` as if it were a key name, so base64 PADDING made the
# secret its own "head" and it was written back out verbatim.
_KEY = "nlb4rKG7VN7xp9ZY6765nzNKfmpgyWyzF9cAxyG0opKDk07qXYCvfRKPEs5WVYGBBAuMllweeH6u+AStUnRJmw"


def test_base64_padding_is_not_mistaken_for_a_key_name():
    out, n = redact_credentials(_KEY + "==")

    assert out == "REDACTED"
    assert _KEY not in out
    assert n == 1


def test_a_bare_blob_next_to_a_named_key_is_redacted_too():
    out, _ = redact_credentials(f"{_KEY}==;AccountKey=SECOND000000000000000000000000000000000000key==")

    assert _KEY not in out
    assert "SECOND000" not in out
    assert "AccountKey=REDACTED" in out       # the named key stays readable


def test_named_credentials_keep_their_label():
    for text, expected in [
        ("DefaultEndpointsProtocol=https;AccountName=x;AccountKey=" + _KEY + "==",
         "AccountKey=REDACTED"),
        ("https://x.blob.core.windows.net/c?sv=2021&sig=AbC123def456ghi789jkl", "sig=REDACTED"),
        ("Password=hunter2;Database=orders", "Password=REDACTED"),
    ]:
        out, _ = redact_credentials(text)
        assert expected in out, out
        assert "hunter2" not in out


def test_redaction_is_complete_in_one_pass():
    # One pass must reach the fixpoint: if a second pass could still CHANGE the
    # text, then a secret is one pattern-interaction away from shipping. (A second
    # pass re-matching `AccountKey=REDACTED` and rewriting it identically is fine -
    # what matters is that nothing new comes out.)
    text = (f"{_KEY}==;AccountKey={_KEY}==;sig={_KEY};"
            f"Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.abcdefghijklmnopqrst")
    once, _ = redact_credentials(text)
    twice, _ = redact_credentials(once)

    assert once == twice
    assert _KEY not in once
    assert "eyJ0eXAiOiJKV1Qi" not in once
