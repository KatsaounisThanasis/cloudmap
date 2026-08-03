"""Larger, logically-wired estate: two web apps + AKS sharing a plan, vnet, key
vault, ACR and Log Analytics, plus databases, private endpoints and an app
gateway. Exercises the graph at scale - especially that shared infrastructure
does NOT bridge one app's blast radius into a sibling app's private stack.
"""

import os

from cloudmap.graph import blast_radius, build_graph, find_seeds
from cloudmap.ingest.fixture import load_fixture

FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "estate.json")


def _names(seed_name="nw-storefront", direction="both"):
    g = build_graph(load_fixture(FIX))
    seed = find_seeds(g, seed_name)[0]
    return {n.name for n in blast_radius(g, seed, direction=direction).nodes.values()}


def test_storefront_pulls_its_real_stack():
    names = _names()
    assert {"nw-plan", "nw-vnet", "nw-sql", "nw-redis", "nwstorage", "nw-kv",
            "nwacr", "nw-appi", "nw-law", "nw-agw"} <= names


def test_shared_infra_does_not_bridge_to_sibling_app():
    names = _names()
    # api's OWN dependencies must not leak in through the shared plan / vnet / vault
    assert "nw-pg" not in names
    assert "nw-sb" not in names
    assert "nw-cosmos" not in names
    assert "nw-api" not in names


def test_reverse_from_shared_vault_finds_both_apps():
    # seed the shared vault, ask "who uses it" -> both apps surface
    names = _names(seed_name="nw-kv", direction="up")
    assert {"nw-storefront", "nw-api"} <= names
