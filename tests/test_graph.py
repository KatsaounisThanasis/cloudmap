import os

from cloudmap.graph import blast_radius, build_graph, find_seeds
from cloudmap.ingest.fixture import load_fixture

FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "contoso.json")


def _graph():
    return build_graph(load_fixture(FIX))


def test_seed_found_unique():
    assert len(find_seeds(_graph(), "contoso-web")) == 1


def test_web_blast_radius_pulls_real_deps():
    g = _graph()
    seed = find_seeds(g, "contoso-web")[0]
    names = {n.name for n in blast_radius(g, seed).nodes.values()}
    assert {"contoso-plan", "contoso-kv", "contoso-sql", "contosostg",
            "contoso-appi", "contoso-agw"} <= names


def test_edge_kinds_are_derived():
    g = _graph()
    seed = find_seeds(g, "contoso-web")[0]
    sub = blast_radius(g, seed)
    kinds = {(sub.nodes[e.source].name, sub.nodes[e.target].name): e.kind for e in sub.edges}
    assert kinds[("contoso-web", "contoso-plan")] == "hosted-on"
    # KV edge carries BOTH the secret read and the RBAC role, merged
    assert "reads-secret" in kinds[("contoso-web", "contoso-kv")]
    assert "role: Key Vault Secrets User" in kinds[("contoso-web", "contoso-kv")]
    assert kinds[("contoso-agw", "contoso-web")] == "routes-to"


def test_shared_resource_does_not_bridge_to_siblings():
    g = _graph()
    seed = find_seeds(g, "contoso-web")[0]
    names = {n.name for n in blast_radius(g, seed).nodes.values()}
    assert "contoso-vnet" in names       # directly integrated with the seed
    assert "contoso-aks" not in names    # only reachable via the shared vnet -> not bridged


def test_direction_consistency_no_forward_leak_on_reverse():
    # Seeding the vault and asking "who reads it" (up) must surface the consuming
    # app but NOT that app's own downstream dependencies.
    g = _graph()
    seed = find_seeds(g, "contoso-kv")[0]
    names = {n.name for n in blast_radius(g, seed, direction="up").nodes.values()}
    assert "contoso-web" in names        # the app that reads the vault
    assert "contoso-plan" not in names   # the app's OWN deps must not leak in
    assert "contoso-sql" not in names
