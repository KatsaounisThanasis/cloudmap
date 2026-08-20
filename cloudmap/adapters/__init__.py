"""Adapters: turn a raw export into the neutral cloudmap Graph.

The rest of the tool never learns which cloud (or file shape) the data came from
- that knowledge is quarantined here. Adding a cloud = adding one adapter.
The neutral shape and accepted inputs are documented in FORMAT.md.

Today:
- AzureAdapter        raw Azure Resource Graph  -> Graph  (runs extraction rules)
- neutral graph JSON  a file cloudmap wrote     -> Graph  (loaded back, no re-extract)
"""

import json

from ..graph import build_graph
from ..model import Edge, Graph, Node


class AzureAdapter:
    """Raw Azure Resource Graph (a list of resources, each with `properties`) ->
    neutral Graph. The Azure-specific rules live in extract/extractors.py; this
    just names the seam and delegates."""

    name = "azure"

    @staticmethod
    def matches(data):
        resources = _azure_resources(data)
        return bool(resources) and any(
            str(r.get("type", "")).lower().startswith("microsoft.") for r in resources
        )

    @staticmethod
    def to_graph(data):
        return build_graph(_azure_resources(data))


def _azure_resources(data):
    if isinstance(data, dict):
        return data.get("data") or data.get("resources") or []
    if isinstance(data, list):
        return data
    return []


def _looks_neutral(data):
    """A graph cloudmap itself wrote: a dict carrying both `nodes` and `edges`."""
    return isinstance(data, dict) and isinstance(data.get("nodes"), list) \
        and isinstance(data.get("edges"), list)


def graph_from_neutral(data):
    """Reconstruct a Graph from cloudmap's own JSON output (post-extraction, so no
    rules run). Provenance (origin/evidence), hop distances and the map's own meta
    (seed, complete, read_gaps) are preserved - a reloaded map keeps its caveats."""
    nodes = {}
    for n in data.get("nodes", []):
        nodes[n["id"]] = Node(
            id=n["id"],
            name=n.get("name", ""),
            type=n.get("type", ""),
            resource_group=n.get("resourceGroup", ""),
            location=n.get("location", ""),
            external=bool(n.get("external")),
            note=n.get("note", ""),
        )
    edges = [
        Edge(
            source=e["source"],
            target=e["target"],
            kind=e.get("kind", ""),
            origin=e.get("origin", "extracted"),
            evidence=e.get("evidence", ""),
        )
        for e in data.get("edges", [])
    ]
    distances = {n["id"]: n["hops"] for n in data.get("nodes", []) if n.get("hops") is not None}
    meta = dict(data.get("meta") or {})
    if data.get("seed"):
        meta.setdefault("seed", data["seed"])
    return Graph(nodes=nodes, edges=edges, distances=distances, meta=meta)


def load_graph(path):
    """Read a file and return a neutral Graph, auto-detecting the input shape:
    a neutral cloudmap graph is loaded as-is; anything else is treated as a raw
    cloud export and sent through the matching adapter (Azure today)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if _looks_neutral(data):
        return graph_from_neutral(data)
    graph = AzureAdapter.to_graph(data)
    # A capture's own honesty flags (truncated, enriched, scrubbed) ride along:
    # re-tracing a truncated export must not produce a map that claims to be
    # complete just because the file was re-read from disk.
    if isinstance(data, dict) and isinstance(data.get("meta"), dict):
        graph.meta.update(data["meta"])
    return graph
