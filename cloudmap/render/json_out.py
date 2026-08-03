"""Serialize a graph to a plain JSON inventory (nodes + edges + hop distance)."""

import json


def to_json(graph, seed_id, meta=None):
    meta = meta or {}
    external = sum(1 for n in graph.nodes.values() if n.external)
    model_edges = sum(1 for e in graph.edges if e.origin == "model")
    return json.dumps(
        {
            "seed": seed_id,
            "meta": {
                # complete=False means the graph is known to be missing edges/nodes
                # (scan truncated, a live read failed, or a whole class of edge was
                # never looked for) - the artifact says so.
                "complete": not (meta.get("truncated") or meta.get("read_gaps")
                                 or meta.get("blind_spots")),
                "truncated": bool(meta.get("truncated")),
                "read_gaps": list(meta.get("read_gaps") or []),
                # edges we know we did not go looking for (see cli._enrich_live)
                "blind_spots": list(meta.get("blind_spots") or []),
                "external_unverified": external,
                # how many edges are model-proposed guesses vs verified extractions
                "model_edges": model_edges,
            },
            "nodes": [
                {
                    "id": n.id,
                    "name": n.name,
                    "type": n.type,
                    "resourceGroup": n.resource_group,
                    "location": n.location,
                    "hops": (graph.distances or {}).get(n.id),
                    "external": n.external,
                    "note": n.note,
                }
                for n in graph.nodes.values()
            ],
            "edges": [
                {
                    "source": e.source,
                    "target": e.target,
                    "kind": e.kind,
                    "origin": e.origin,       # "extracted" (verified) | "model" (guess)
                    "evidence": e.evidence,   # the proof behind this edge
                }
                for e in graph.edges
            ],
        },
        indent=2,
    )
