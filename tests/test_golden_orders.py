"""Golden test on REAL-shaped data.

fixtures/acme_orders.json is reconstructed from a hand-verified real Azure
deployment (an internal orders web app, dev environment), in raw Azure Resource
Graph shape, scrubbed of the subscription id, tenant and product names. Unlike a
fixture we invented
to please the code, this mirrors a topology we confirmed against the portal, so
it proves the extractor works on the shape a real tenant actually emits.

The gold standard is a *captured* raw export dropped in here later; the assertions
below stay the same when that swap happens. To produce one:

    cloudmap capture --allow-live --single-sub -o fixtures/captured_real.json
    # read it, then commit it

`capture` scrubs by default, and test_captured_real_export_is_believable below
activates by itself the moment that file exists.
"""

import os

import pytest

from cloudmap.graph import blast_radius, build_graph, find_seeds
from cloudmap.ingest.fixture import load_fixture

FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "acme_orders.json")
CAPTURED = os.path.join(os.path.dirname(__file__), "..", "fixtures", "captured_real.json")


def _blast():
    g = build_graph(load_fixture(FIX))
    seed = find_seeds(g, "webapp-orders-dev")[0]
    return blast_radius(g, seed)


def test_orders_blast_radius_matches_verified_reality():
    sub = _blast()
    by_target = {sub.nodes[e.target].name: e.kind for e in sub.edges}

    # the five dependencies we verified by hand against the real deployment
    assert by_target["plan-orders-dev"] == "hosted-on"
    assert by_target["pg-orders-dev"] == "connects-to"
    assert by_target["stordersdev"] == "connects-to"
    assert "pulls-image" in by_target["acrordersdev"]        # from the docker image
    assert "role: AcrPull" in by_target["acrordersdev"]      # merged with the RBAC grant
    assert by_target["kv-orders-dev"] == "role: Key Vault Secrets User"


def test_orders_edges_are_all_verified_with_evidence():
    sub = _blast()
    for e in sub.edges:
        assert e.origin == "extracted", f"{e.kind} should be a verified extraction"
        assert e.evidence, f"edge to {e.target} must carry its proof"


@pytest.mark.skipif(not os.path.exists(CAPTURED),
                    reason="no captured real export yet - see the module docstring")
def test_captured_real_export_is_believable():
    """The invariants that hold for ANY real tenant, so they can be asserted
    without knowing what is in the capture. A real export is the one input that
    can genuinely surprise the extractors."""
    g = build_graph(load_fixture(CAPTURED))

    assert g.nodes, "a captured export that produces no nodes is a broken capture"
    assert g.edges, "no edges at all means the extractors saw nothing they knew"
    for e in g.edges:
        assert e.source in g.nodes and e.target in g.nodes   # nothing dangles
        assert e.origin == "extracted"                       # a plain capture has no guesses
        assert e.evidence, f"edge {e.kind} to {e.target} must carry its proof"
    assert not any(n.type == "microsoft.authorization/roleassignments"
                   for n in g.nodes.values())


def test_orders_role_assignment_nodes_do_not_leak_into_map():
    # role-assignment resources are how we DERIVE edges, not nodes on the map.
    sub = _blast()
    assert not any(n.type == "microsoft.authorization/roleassignments"
                   for n in sub.nodes.values())
    assert len(sub.nodes) == 6      # seed + 5 dependencies
