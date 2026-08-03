"""Enrichment scope and the blind spot it leaves behind.

Config-level dependencies (Key Vault references, connection strings, backend
hostnames) live in app settings, which Resource Graph does not return. So an
app's inbound edges exist only once THAT app has been enriched - enriching just
the seed makes the graph asymmetric, and "what breaks if I touch this shared
vault" is precisely the question that asymmetry breaks. These tests lock which
apps get enriched, and that whatever is skipped is declared rather than passed
off as an empty result.
"""

import json

from cloudmap.ask import warnings
from cloudmap.cli import _enrichment_targets
from cloudmap.ingest.azure import enrich_webapps
from cloudmap.model import Edge, Graph, Node
from cloudmap.render.json_out import to_json


def _estate():
    """Two apps reading one shared vault - the shape the upward view needs."""
    kv = Node(id="/kv", name="kv", type="microsoft.keyvault/vaults")
    a = Node(id="/appA", name="appA", type="microsoft.web/sites", raw={"id": "/appA"})
    b = Node(id="/appB", name="appB", type="microsoft.web/sites", raw={"id": "/appB"})
    return Graph(nodes={n.id: n for n in (kv, a, b)},
                 edges=[Edge(a.id, kv.id, "reads-secret"), Edge(b.id, kv.id, "reads-secret")])


def _names(nodes):
    return sorted(n.name for n in nodes)


def test_auto_enriches_every_app_when_the_seed_is_shared_infrastructure():
    # Tracing the vault: only the apps' own config can reveal who reads it.
    chosen, skipped = _enrichment_targets(_estate(), "/kv", "auto", "both")

    assert _names(chosen) == ["appA", "appB"]
    assert skipped == []


def test_auto_enriches_only_the_seed_when_the_seed_is_an_app():
    # The app's own config already yields its downward view; the rest of the
    # tenant is not worth an az round-trip each.
    chosen, skipped = _enrichment_targets(_estate(), "/appA", "auto", "both")

    assert _names(chosen) == ["appA"]
    assert _names(skipped) == ["appB"]


def test_auto_widens_for_an_app_seed_asked_upward():
    # "What depends on appA" can be another app calling it by hostname, which
    # only that other app's config shows.
    chosen, _skipped = _enrichment_targets(_estate(), "/appA", "auto", "up")

    assert _names(chosen) == ["appA", "appB"]


def test_explicit_modes_override_auto():
    g = _estate()
    assert _names(_enrichment_targets(g, "/kv", "all", "both")[0]) == ["appA", "appB"]
    assert _enrichment_targets(g, "/kv", "seed", "both")[0] == []      # seed is not an app
    assert _names(_enrichment_targets(g, "/appA", "seed", "up")[0]) == ["appA"]
    assert _enrichment_targets(g, "/kv", "none", "both")[0] == []


def test_skipped_apps_are_reported_as_a_blind_spot_not_an_empty_answer():
    # The whole point: a map that never looked for inbound config edges must say
    # so, and that admission has to survive into the artifact and out of `ask`.
    _chosen, skipped = _enrichment_targets(_estate(), "/appA", "auto", "both")
    blind = [f"{len(skipped)} web app(s) in scope were not deep-enriched"]

    doc = json.loads(to_json(_estate(), "/appA", meta={"blind_spots": blind}))
    assert doc["meta"]["blind_spots"] == blind
    assert doc["meta"]["complete"] is False       # not looked for == not complete

    g = _estate()
    g.meta = {"complete": False, "blind_spots": blind}
    assert any("blind spot" in w for w in warnings(g))


def test_bulk_enrich_attributes_each_read_gap_to_its_app():
    # No name/resourceGroup -> unenrichable. Two of them must produce two gaps,
    # each naming the app, so a gap is never anonymous in a tenant-wide pass.
    out = enrich_webapps([{"id": "/appA", "name": "", "resourceGroup": ""},
                          {"id": "/appB", "name": "appB", "resourceGroup": ""}])

    assert len(out["errors"]) == 2
    assert any(m.startswith("/appA: ") for m in out["errors"])
    assert any(m.startswith("appB: ") for m in out["errors"])
    assert out["enriched"] == ["/appA", "/appB"]


def test_bulk_enrich_of_nothing_touches_nothing():
    assert enrich_webapps([])["enriched"] == []
