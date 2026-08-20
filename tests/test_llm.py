"""The LLM path must stay a proposer behind a verifier: only proposals whose
target resolves to a real scanned resource survive, secret-looking targets are
rejected, and with no model available it contributes nothing. No model server
needed - generate_json is monkeypatched."""

from cloudmap.extract import llm
from cloudmap.extract.extractors import Resolver
from cloudmap.extract.llm import llm_edges_for_seed, propose_edges
from cloudmap.model import Node


def _seed_and_resolver():
    seed = Node(id="/web", name="web", type="microsoft.web/sites")
    kv = Node(id="/kv1", name="kv1", type="microsoft.keyvault/vaults")
    return seed, Resolver({seed.id: seed, kv.id: kv})


def test_only_verified_targets_survive(monkeypatch):
    seed, resolver = _seed_and_resolver()
    monkeypatch.setattr(llm, "generate_json", lambda *a, **k: {"edges": [
        {"target": "kv1", "relationship": "reads-secret"},          # resolves -> kept
        {"target": "ghost-not-in-scope", "relationship": "connects-to"},  # -> dropped
    ]})
    ext, edges = llm_edges_for_seed(seed, resolver)

    assert ext == []                              # unverifiable proposals never shown
    assert len(edges) == 1
    assert edges[0].target == "/kv1"
    assert edges[0].kind == "reads-secret"
    assert edges[0].origin == "model"             # the relationship is the model's, drawn dashed
    assert edges[0].evidence                      # says it was model-proposed + verified


def test_secretish_targets_are_rejected(monkeypatch):
    monkeypatch.setattr(llm, "generate_json", lambda *a, **k: {"edges": [
        {"target": "Password=hunter2", "relationship": "x"},
        {"target": "DefaultEndpoints;AccountKey=abc", "relationship": "y"},
    ]})
    assert propose_edges({"type": "microsoft.web/sites"}) == []


def test_no_model_available_contributes_nothing(monkeypatch):
    seed, resolver = _seed_and_resolver()
    monkeypatch.setattr(llm, "generate_json", lambda *a, **k: {})   # model down -> {}

    assert llm_edges_for_seed(seed, resolver) == ([], [])
