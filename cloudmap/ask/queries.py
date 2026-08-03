"""The queries an answer is actually made of - every one computed from the graph.

Nothing in this module asks a model anything. "What breaks if I touch X" is not a
language problem, it is a traversal: walk the edges that point AT X. Computing it
here rather than prompting for it is what makes the answer worth trusting, and it
is why a model can never promote a guess to a fact in the Ask layer - it is never
the thing producing the facts.

Trust aggregation: a conclusion is only as good as the weakest hop behind it. A
path is `verified` only when EVERY edge on it came from a deterministic rule and
no node along the way is an unverified external reference. One model-proposed hop
makes the whole conclusion a guess, and it is labelled as one.
"""

from ..graph import friendly_type

QUERIES = ("impact", "depends", "paths", "shared", "guesses", "summary")


def _adjacency(graph):
    """edges indexed by source (downstream) and by target (upstream)."""
    down, up = {}, {}
    for e in graph.edges:
        down.setdefault(e.source, []).append(e)
        up.setdefault(e.target, []).append(e)
    return down, up


def _name(graph, nid):
    node = graph.nodes.get(nid)
    return (node.name if node and node.name else nid)


def _hop(graph, edge):
    """One step, carrying its own proof so the reader can audit instead of trust."""
    return {
        "source": _name(graph, edge.source),
        "target": _name(graph, edge.target),
        "kind": edge.kind,
        "origin": edge.origin,
        "evidence": edge.evidence,
    }


def _chain(subject, path):
    """The ordered node ids along a path, whichever direction it was walked in."""
    chain = [subject]
    for e in path:
        chain.append(e.target if e.source == chain[-1] else e.source)
    return chain


def _trust(graph, path, subject):
    """Grade a path: verified, or a guess with the reason it is one.

    Only the hops in BETWEEN count as crossings. That an endpoint is an unverified
    external reference is already reported on the finding itself (`external`), and
    the edge reaching it is real - the app genuinely names that host. Passing
    *through* something unverified is different: the conclusion then rests on a
    resource nobody confirmed exists, so the whole path becomes a guess.
    """
    for e in path:
        if e.origin != "extracted":
            return "unverified", (f"a model proposed the hop {_name(graph, e.source)} "
                                  f"--{e.kind}--> {_name(graph, e.target)}")
    for nid in _chain(subject, path)[1:-1]:
        node = graph.nodes.get(nid)
        if node is not None and node.external:
            return "unverified", (f"the path passes through {node.name}, which was referenced "
                                  "but never verified as a real resource")
    return "verified", ""


def _walk(graph, start, adjacency, pick, max_hops=None):
    """BFS over ONE direction, keeping the shortest path (as edges) to each node.

    One direction only, never reversing - the same rule blast_radius uses, so a
    shared resource (plan, vault, vnet) cannot bridge the subject to resources it
    has nothing to do with."""
    paths, queue, order = {start: []}, [(start, 0)], []
    while queue:
        cur, dist = queue.pop(0)
        if max_hops is not None and dist >= max_hops:
            continue
        for e in adjacency.get(cur, []):
            nxt = pick(e)
            if nxt in paths or nxt not in graph.nodes:
                continue
            paths[nxt] = paths[cur] + [e]
            order.append(nxt)
            queue.append((nxt, dist + 1))
    return paths, order


def _findings(graph, subject, paths, order):
    out = []
    for nid in order:
        path = paths[nid]
        node = graph.nodes[nid]
        trust, why = _trust(graph, path, subject)
        out.append({
            "id": nid,
            "name": node.name,
            "type": friendly_type(node.type),
            "hops": len(path),
            # what the number MEANS for this query - the renderer should not have to
            # guess whether a count is hops, steps or dependents.
            "metric": f"{len(path)} hop(s)",
            "trust": trust,
            "why": why,
            "external": node.external,
            "path": [_hop(graph, e) for e in path],
        })
    out.sort(key=lambda f: (f["hops"], f["name"]))
    return out


def _tally(findings):
    return {
        "total": len(findings),
        "verified": sum(1 for f in findings if f["trust"] == "verified"),
        "unverified": sum(1 for f in findings if f["trust"] != "verified"),
    }


def impact(graph, subject, max_hops=None):
    """What breaks if `subject` changes: everything that depends ON it (upstream)."""
    down, up = _adjacency(graph)
    paths, order = _walk(graph, subject, up, lambda e: e.source, max_hops=max_hops)
    findings = _findings(graph, subject, paths, order)
    name = _name(graph, subject)
    headline = (f"Nothing in this map depends on {name}."
                if not findings else
                f"{len(findings)} resource(s) depend on {name} - changing it can break them.")
    result = {"query": "impact", "subject": subject, "subject_name": name,
              "headline": headline, "findings": findings, "facts": _tally(findings)}
    # An empty answer is easy to misread as "nothing to see here" when the map was
    # simply walked the other way round. Say what IS there - a fact, not a guess.
    if not findings and down.get(subject):
        result["hint"] = (f"{name} does have {len(down[subject])} dependency(ies) of its own - "
                          f"ask what {name} depends on.")
    return result


def depends(graph, subject, max_hops=None):
    """What `subject` itself needs to work (downstream)."""
    down, up = _adjacency(graph)
    paths, order = _walk(graph, subject, down, lambda e: e.target, max_hops=max_hops)
    findings = _findings(graph, subject, paths, order)
    name = _name(graph, subject)
    headline = (f"{name} has no dependencies in this map."
                if not findings else
                f"{name} depends on {len(findings)} resource(s).")
    result = {"query": "depends", "subject": subject, "subject_name": name,
              "headline": headline, "findings": findings, "facts": _tally(findings)}
    if not findings and up.get(subject):
        result["hint"] = (f"{len(up[subject])} resource(s) do depend on {name} - "
                          f"ask what breaks if you touch {name}.")
    return result


def paths(graph, subject, target, max_paths=25, max_depth=8):
    """Every way `subject` reaches `target`, following dependency direction."""
    down, _up = _adjacency(graph)
    found, stack = [], [(subject, [], {subject})]
    truncated = False
    while stack:
        cur, path, seen = stack.pop()
        if len(path) >= max_depth:
            continue
        for e in down.get(cur, []):
            if e.target in seen:
                continue
            if e.target == target:
                found.append(path + [e])
                if len(found) >= max_paths:
                    truncated = True
                    stack = []
                    break
            else:
                stack.append((e.target, path + [e], seen | {e.target}))

    findings = []
    for path in sorted(found, key=len):
        trust, why = _trust(graph, path, subject)
        findings.append({
            "id": None, "name": " -> ".join([_name(graph, subject)]
                                            + [_name(graph, e.target) for e in path]),
            "type": "path", "hops": len(path), "metric": f"{len(path)} step(s)",
            "trust": trust, "why": why,
            "external": False, "path": [_hop(graph, e) for e in path],
        })
    a, b = _name(graph, subject), _name(graph, target)
    headline = (f"No path from {a} to {b} in this map."
                if not findings else
                f"{len(findings)} path(s) from {a} to {b}"
                f"{' (list capped)' if truncated else ''}.")
    facts = _tally(findings)
    facts["capped"] = truncated
    return {"query": "paths", "subject": subject, "subject_name": a, "target": target,
            "target_name": b, "headline": headline, "findings": findings, "facts": facts}


def shared(graph, min_dependents=2):
    """Resources several others depend on - where one change hits more than one team."""
    _down, up = _adjacency(graph)
    findings = []
    for nid, edges in up.items():
        node = graph.nodes.get(nid)
        if node is None:
            continue
        dependents = sorted({f"{_name(graph, e.source)} ({e.kind})" for e in edges})
        if len({e.source for e in edges}) < min_dependents:
            continue
        unproven = [e for e in edges if e.origin != "extracted"]
        findings.append({
            "id": nid, "name": node.name, "type": friendly_type(node.type),
            "hops": 0, "metric": f"{len({e.source for e in edges})} dependents",
            "external": node.external,
            "trust": "unverified" if unproven else "verified",
            "why": (f"{len(unproven)} of the incoming dependencies are model-proposed"
                    if unproven else ""),
            "dependents": dependents,
            # the dependents ARE the finding here, so listing them twice (once as a
            # path, once as a name) would just be noise.
            "path": [],
        })
    findings.sort(key=lambda f: (-len(f["dependents"]), f["name"]))
    headline = ("Nothing in this map is depended on by more than one resource."
                if not findings else
                f"{len(findings)} shared resource(s): one change there affects several "
                "dependents.")
    return {"query": "shared", "subject": None, "subject_name": None,
            "headline": headline, "findings": findings, "facts": _tally(findings)}


def guesses(graph):
    """What NOT to trust in this map: model-proposed edges and unverified references."""
    findings = []
    for e in graph.edges:
        if e.origin == "extracted":
            continue
        findings.append({
            "id": None, "name": f"{_name(graph, e.source)} --{e.kind}--> {_name(graph, e.target)}",
            "type": "model-proposed edge", "hops": 1, "metric": "", "trust": "unverified",
            "external": False,
            "why": e.evidence or "proposed by the local model, no deterministic proof",
            "path": [_hop(graph, e)],
        })
    for node in graph.nodes.values():
        if not node.external:
            continue
        findings.append({
            "id": node.id, "name": node.name,
            "type": f"unverified reference ({node.type or 'unknown type'})",
            "hops": 0, "metric": "", "trust": "unverified", "external": True,
            "why": node.note or "referenced by a resource but not found in the scanned scope",
            "path": [],
        })
    headline = ("Every edge in this map was verified by a deterministic rule, and every "
                "node was found in the scanned scope."
                if not findings else
                f"{len(findings)} item(s) in this map are NOT proven and should be treated "
                "as guesses.")
    return {"query": "guesses", "subject": None, "subject_name": None,
            "headline": headline, "findings": findings, "facts": _tally(findings)}


def summary(graph):
    """Explain the map itself: what is in it, and how much of it is proven."""
    by_type = {}
    for node in graph.nodes.values():
        by_type[friendly_type(node.type)] = by_type.get(friendly_type(node.type), 0) + 1
    model_edges = sum(1 for e in graph.edges if e.origin != "extracted")
    external = sum(1 for n in graph.nodes.values() if n.external)
    meta = getattr(graph, "meta", None) or {}
    seed = meta.get("seed")
    findings = [
        {"id": None, "name": f"{count} x {label}", "type": "resource type", "hops": 0,
         "metric": "", "trust": "verified", "why": "", "external": False, "path": []}
        for label, count in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    headline = (f"{len(graph.nodes)} resource(s) and {len(graph.edges)} dependency edge(s)"
                + (f", traced from {_name(graph, seed)}" if seed else "") + ".")
    return {
        "query": "summary", "subject": seed, "subject_name": _name(graph, seed) if seed else None,
        "headline": headline, "findings": findings,
        "facts": {
            "resources": len(graph.nodes),
            "edges": len(graph.edges),
            "verified_edges": len(graph.edges) - model_edges,
            "model_edges": model_edges,
            "external_unverified": external,
            "complete": meta.get("complete", True),
        },
    }
