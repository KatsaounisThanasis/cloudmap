"""Render a graph as a Mermaid flowchart (quick text/preview output)."""


def to_mermaid(graph, seed_id):
    idmap = {nid: f"N{i}" for i, nid in enumerate(graph.nodes)}
    lines = ["graph LR"]
    for nid, mid in idmap.items():
        n = graph.nodes[nid]
        short = n.type.split("/")[-1]
        label = f"{n.name}<br/><small>{short}</small>".replace('"', "'")
        lines.append(f'  {mid}["{label}"]')
    for e in graph.edges:
        s, t = idmap.get(e.source), idmap.get(e.target)
        if s and t:
            k = e.kind.replace("|", "/")
            if e.origin == "model":
                lines.append(f'  {s} -. "{k} (model)" .-> {t}')   # dashed = model guess
            else:
                lines.append(f'  {s} -->|{k}| {t}')
    for nid, mid in idmap.items():
        if graph.nodes[nid].external:
            lines.append(f"  style {mid} fill:#f5f5f5,stroke:#999,stroke-dasharray:4 3,color:#666")
    if seed_id in idmap:
        lines.append(f"  style {idmap[seed_id]} fill:#ffe0b2,stroke:#d79b00,stroke-width:3px")
    return "\n".join(lines) + "\n"
