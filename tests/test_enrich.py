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

import pytest

from cloudmap.ask import warnings
from cloudmap.cli import _enrichment_targets
from cloudmap.graph import build_graph, find_seeds
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


# --- what `auto` decides to enrich, and why ---------------------------------------

def _estate_with_many_apps(n=50):
    S = "/subscriptions/s/resourcegroups/rg/providers"
    rows = [{"id": f"{S}/microsoft.keyvault/vaults/kv", "name": "kv",
             "type": "microsoft.keyvault/vaults",
             "properties": {"vaultUri": "https://kv.vault.azure.net/"}},
            {"id": f"{S}/microsoft.compute/virtualmachines/vm", "name": "vm",
             "type": "microsoft.compute/virtualmachines", "properties": {}},
            {"id": f"{S}/microsoft.network/virtualnetworks/vnet", "name": "vnet",
             "type": "microsoft.network/virtualnetworks", "properties": {}}]
    for i in range(n):
        rows.append({"id": f"{S}/microsoft.web/sites/app-{i}", "name": f"app-{i}",
                     "type": "microsoft.web/sites", "properties": {}})
    return rows


@pytest.mark.parametrize("seed_name,expect_fanout", [
    ("kv", True),      # a vault's dependents really do hide in app config
    ("vm", False),     # a VM's relationships are already in the ARM data
    ("vnet", False),   # ditto a VNet's
])
def test_auto_only_reads_every_app_when_the_answer_needs_it(seed_name, expect_fanout):
    """Regression from a live run: tracing a VM used to deep-enrich all 289 web
    apps in the subscription and take minutes, to print edges that were in the
    ARM data the whole time. The same fan-out on a Key Vault is the only way to
    learn who reads it, so the cost is spent there and nowhere else."""
    from cloudmap.cli import _enrichment_targets

    graph = build_graph(_estate_with_many_apps())
    seed = find_seeds(graph, seed_name)[0]
    chosen, skipped = _enrichment_targets(graph, seed, "auto", "up")

    assert (len(chosen) > 1) is expect_fanout
    assert len(chosen) + len(skipped) == 50      # every app is accounted for


def test_a_workload_seed_still_only_enriches_itself():
    from cloudmap.cli import _enrichment_targets

    graph = build_graph(_estate_with_many_apps())
    seed = find_seeds(graph, "app-7")[0]
    chosen, _ = _enrichment_targets(graph, seed, "auto", "down")

    assert [n.name for n in chosen] == ["app-7"]


def test_enrich_all_still_overrides_the_heuristic():
    from cloudmap.cli import _enrichment_targets

    graph = build_graph(_estate_with_many_apps())
    seed = find_seeds(graph, "vm")[0]
    chosen, skipped = _enrichment_targets(graph, seed, "all", "up")

    assert len(chosen) == 50 and skipped == []


def test_a_capture_never_writes_kubernetes_secret_material(tmp_path, monkeypatch, capsys):
    """`kubernetes_text` holds DECODED Kubernetes secret values, kept only so the
    extractors can spot a connection string inside one. A capture is a file
    people commit, so the field is dropped before writing - under --no-scrub too,
    where the scrub pass that also deletes it never runs."""
    import json as _json

    from cloudmap import cli

    rows = [{"id": "/subscriptions/s/x/aks", "name": "aks", "type":
             "microsoft.containerservice/managedclusters",
             "kubernetes_text": "sec_decoded:AccountKey=REALSECRETVALUE123",
             "properties": {}}]
    monkeypatch.setattr("cloudmap.ingest.azure.query_live", lambda **k: (rows, False))
    monkeypatch.setattr("cloudmap.ingest.azure.enrich_webapps",
                        lambda *a, **k: {"role_assignments": [], "diagnostics": [],
                                         "errors": [], "enriched": []})
    monkeypatch.setattr("cloudmap.ingest.azure.enrich_aks_clusters",
                        lambda *a, **k: {"errors": []})

    out = tmp_path / "capture.json"
    rc = cli.main(["capture", "--allow-live", "--single-sub", "--no-scrub", "-o", str(out)])
    written = out.read_text()

    assert rc == 0
    assert "REALSECRETVALUE123" not in written
    assert "kubernetes_text" not in _json.loads(written)["data"][0]
    assert "never written to an export" in capsys.readouterr().err


def test_enrichment_concurrency_is_env_tunable(monkeypatch):
    from cloudmap.ingest.azure import _enrich_workers

    assert _enrich_workers() == 12                       # the default
    monkeypatch.setenv("CLOUDMAP_ENRICH_WORKERS", "24")
    assert _enrich_workers() == 24
    monkeypatch.setenv("CLOUDMAP_ENRICH_WORKERS", "0")   # nonsense clamps to 1
    assert _enrich_workers() == 1
    monkeypatch.setenv("CLOUDMAP_ENRICH_WORKERS", "abc")  # garbage falls back
    assert _enrich_workers() == 12
