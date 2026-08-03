"""The HTML viewer must be a single self-contained file (no CDN) that embeds the
graph data and marks model edges distinctly."""

import json

from cloudmap.model import Edge, Graph, Node
from cloudmap.render.html import to_html


def _graph():
    seed = Node(id="/web", name="web", type="microsoft.web/sites")
    a = Node(id="/a", name="stg", type="microsoft.storage/storageaccounts")
    b = Node(id="/b", name="guessed", type="microsoft.keyvault/vaults")
    return Graph(
        nodes={n.id: n for n in (seed, a, b)},
        edges=[Edge("/web", "/a", "connects-to", origin="extracted", evidence="app config"),
               Edge("/web", "/b", "reads-secret", origin="model")],
        distances={"/web": 0, "/a": 1, "/b": 1},
    )


def test_html_is_self_contained_and_has_data():
    html = to_html(_graph(), "/web")
    assert "<svg" in html and "<script>" in html
    # no external resources fetched (the only http is the SVG XML namespace, not a fetch)
    assert "cdn" not in html.lower()
    assert "<link" not in html.lower() and "src=" not in html and "@import" not in html
    assert "/*__DATA__*/null" not in html           # placeholder was filled


def test_html_embeds_provenance():
    html = to_html(_graph(), "/web", meta={"truncated": True})
    line = next(ln for ln in html.splitlines() if ln.startswith("const DATA ="))
    blob = line[len("const DATA ="):].strip().rstrip(";")
    payload = json.loads(blob.replace("<\\/", "</"))

    origins = {(e["source"], e["target"]): e["origin"] for e in payload["edges"]}
    assert origins[("/web", "/b")] == "model"
    assert payload["meta"]["complete"] is False       # truncated carried into the viewer
    assert payload["seedName"] == "web"
