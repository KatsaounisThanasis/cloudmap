"""Adapter seam: auto-detect the input shape, and round-trip the neutral format.

These lock the anti-vendor-lock contract - the core reads one neutral Graph, and
a map cloudmap wrote can be re-opened without re-running extraction (what the
viewer and AI phases will rely on).
"""

import os

from cloudmap.adapters import AzureAdapter, load_graph
from cloudmap.model import Edge, Graph, Node
from cloudmap.render.json_out import to_json

FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "acme_orders.json")


def test_load_graph_autodetects_raw_azure():
    g = load_graph(FIX)
    assert any(n.name == "webapp-orders-dev" for n in g.nodes.values())
    assert g.edges and all(e.origin == "extracted" for e in g.edges)   # rules ran


def test_azure_adapter_matches_only_azure_shapes():
    assert AzureAdapter.matches([{"id": "/x", "type": "Microsoft.Web/sites"}])
    assert not AzureAdapter.matches({"nodes": [], "edges": []})        # neutral graph
    assert not AzureAdapter.matches([])


def test_neutral_roundtrip_preserves_provenance(tmp_path):
    # a graph with BOTH a verified and a model edge -> save -> reload unchanged
    seed = Node(id="/web", name="web", type="microsoft.web/sites")
    a = Node(id="/a", name="a", type="microsoft.storage/storageaccounts")
    b = Node(id="/b", name="b", type="microsoft.keyvault/vaults")
    g = Graph(
        nodes={n.id: n for n in (seed, a, b)},
        edges=[
            Edge("/web", "/a", "connects-to", origin="extracted", evidence="app config"),
            Edge("/web", "/b", "reads-secret", origin="model", evidence="proposed by local model"),
        ],
        distances={"/web": 0, "/a": 1, "/b": 1},
    )
    p = tmp_path / "graph.json"
    p.write_text(to_json(g, "/web"), encoding="utf-8")

    g2 = load_graph(str(p))
    by = {(e.source, e.target): e for e in g2.edges}

    assert by[("/web", "/a")].origin == "extracted"
    assert by[("/web", "/a")].evidence == "app config"
    assert by[("/web", "/b")].origin == "model"          # the guess stays a guess
    assert g2.distances["/a"] == 1                        # hop distance survives
