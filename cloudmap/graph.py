"""Build the dependency graph and compute a component's blast radius.

`blast_radius` walks edges from a seed with DIRECTION CONSISTENCY: from the seed
it may go both downstream (what the seed depends on) and upstream (what depends
on the seed), but once it has stepped in one direction it keeps going that way -
it never reverses. That single rule keeps a shared resource (App Service Plan,
VNet, Key Vault, Storage) from bridging the seed to unrelated apps: reaching a
shared plan downstream never walks back up to the other apps hosted on it.
"""

from .extract.extractors import extract_edges
from .model import Graph, Node

# Friendly, architecture-level names per resource type (for the high-level view).
FRIENDLY = {
    "microsoft.web/sites": "Web App",
    "microsoft.web/serverfarms": "App Service Plan",
    "microsoft.keyvault/vaults": "Key Vault",
    "microsoft.storage/storageaccounts": "Storage",
    "microsoft.sql/servers": "SQL Server",
    "microsoft.dbforpostgresql/flexibleservers": "PostgreSQL",
    "microsoft.dbforpostgresql/servers": "PostgreSQL",
    "microsoft.dbformysql/flexibleservers": "MySQL",
    "microsoft.dbformysql/servers": "MySQL",
    "microsoft.documentdb/databaseaccounts": "Cosmos DB",
    "microsoft.cache/redis": "Redis",
    "microsoft.servicebus/namespaces": "Service Bus",
    "microsoft.eventhub/namespaces": "Event Hub",
    "microsoft.search/searchservices": "Cognitive Search",
    "microsoft.cognitiveservices/accounts": "Azure AI / OpenAI",
    "microsoft.containerregistry/registries": "Container Registry",
    "microsoft.containerservice/managedclusters": "AKS",
    "microsoft.app/containerapps": "Container App",
    "microsoft.app/managedenvironments": "Container Apps Environment",
    "microsoft.operationalinsights/workspaces": "Log Analytics",
    "microsoft.insights/components": "App Insights",
    "microsoft.network/virtualnetworks": "Virtual Network",
    "microsoft.network/privateendpoints": "Private Endpoint",
    "microsoft.network/applicationgateways": "App Gateway",
    "microsoft.apimanagement/service": "API Management",
    "microsoft.managedidentity/userassignedidentities": "Managed Identity",
    "microsoft.machinelearningservices/workspaces": "ML Workspace",
}


def friendly_type(t):
    return FRIENDLY.get(t) or (t.split("/")[-1] if "/" in t else t or "resource")


# Id prefixes minted by collapse_high_level. They are also how a reader of a saved
# map can tell it was collapsed - see is_high_level.
HIGH_LEVEL_PREFIXES = ("type::", "ext::")


def is_high_level(graph):
    """True if this map is the architecture view: instances were merged into one
    box per resource type, so instance names no longer exist in it. Worth knowing
    before telling someone their resource is not in the map - it may well be,
    inside a group."""
    return any(str(nid).startswith(HIGH_LEVEL_PREFIXES) for nid in graph.nodes)


def _merge_semis(existing, addition):
    """Union of two '; '-joined lists, order preserved. Collapsing many instances
    into one box folds their arrows into one, and both the relationship kinds and
    their proofs have to survive that fold - a grouped arrow with no evidence is
    an unfalsifiable claim."""
    parts = [p for p in (existing or "").split("; ") if p]
    for p in (addition or "").split("; "):
        if p and p not in parts:
            parts.append(p)
    return "; ".join(parts)


def collapse_high_level(graph, seed_id):
    """Collapse the graph to an architecture-level view: the seed keeps its name,
    every other node is grouped by resource TYPE (one box per type, labelled by
    role, e.g. 'Storage', 'Key Vault'), so the diagram shows the shape rather than
    instance names. Multiple instances of a type collapse into one, noting the count.
    """
    from .model import Edge, Graph, Node

    def group_key(nid):
        n = graph.nodes[nid]
        if nid == seed_id:
            return seed_id
        return f"ext::{n.type}" if n.external else f"type::{n.type}"   # HIGH_LEVEL_PREFIXES

    groups = {}
    for nid in graph.nodes:
        groups.setdefault(group_key(nid), []).append(nid)

    new_nodes, remap, distances = {}, {}, {}
    for gkey, members in groups.items():
        rep = graph.nodes[members[0]]
        for m in members:
            remap[m] = gkey
        dist = min((graph.distances or {}).get(m, 0) for m in members)
        if gkey == seed_id:
            new_nodes[gkey] = rep
        else:
            label = rep.type.split("/", 1)[-1] if rep.external else friendly_type(rep.type)
            if len(members) > 1:
                label += f" ×{len(members)}"
            # an external group keeps its members' reasons: "why is this unverified"
            # must not be lost to grouping either
            note = ""
            for m in members:
                note = _merge_semis(note, graph.nodes[m].note)
            new_nodes[gkey] = Node(id=gkey, name=label, type=rep.type,
                                   external=rep.external, note=note)
        distances[gkey] = dist

    merged = {}
    for e in graph.edges:
        s, t = remap.get(e.source), remap.get(e.target)
        if not (s and t) or s == t:
            continue
        key = (s, t)
        if key in merged:
            m = merged[key]
            m.kind = _merge_semis(m.kind, e.kind)
            m.evidence = _merge_semis(m.evidence, e.evidence)
            if e.origin == "extracted":     # any verified member -> arrow is verified
                m.origin = "extracted"
        else:
            merged[key] = Edge(source=s, target=t, kind=e.kind, origin=e.origin,
                               evidence=e.evidence)

    return Graph(nodes=new_nodes, edges=list(merged.values()), distances=distances)


def node_from_id(rid, external=False, note=""):
    """Build a Node from a bare ARM id (name = last segment, type from provider),
    for targets referenced but not present in the scanned resource set."""
    typ = ""
    low = rid.lower()
    if "/providers/" in low:
        after = rid.split("/providers/", 1)[1].split("/")
        if len(after) >= 3:
            typ = f"{after[0]}/{after[1]}".lower()
    return Node(id=rid, name=rid.rstrip("/").rsplit("/", 1)[-1], type=typ,
                external=external, note=note)


def build_graph(resources):
    nodes = {}
    for r in resources:
        rid = r.get("id")
        if not rid:
            continue
        nodes[rid] = Node(
            id=rid,
            name=r.get("name") or rid.rsplit("/", 1)[-1],
            type=(r.get("type") or "").lower(),
            resource_group=r.get("resourceGroup") or "",
            subscription=r.get("subscriptionId") or "",
            location=r.get("location") or "",
            kind=r.get("kind") or "",
            tags=r.get("tags") or {},
            raw=r,
        )
    return Graph(nodes=nodes, edges=extract_edges(nodes))


def find_seeds(graph, name):
    name_l = name.lower()
    exact = [n.id for n in graph.nodes.values() if n.name.lower() == name_l or n.id.lower() == name_l]
    if exact:
        return exact
    return [n.id for n in graph.nodes.values()
            if name_l in n.name.lower() or n.id.lower().endswith("/" + name_l)]


def blast_radius(graph, seed_id, direction="both", max_hops=None):
    down, up = {}, {}
    for e in graph.edges:
        down.setdefault(e.source, []).append(e.target)
        up.setdefault(e.target, []).append(e.source)

    seed_dirs = {"both": ("down", "up"), "down": ("down",), "up": ("up",)}[direction]

    visited = {seed_id: 0}
    queue = [(seed_id, 0, "seed")]       # (node, distance, direction it was reached by)
    while queue:
        cur, dist, arrived = queue.pop(0)
        if max_hops is not None and dist >= max_hops:
            continue
        dirs = seed_dirs if arrived == "seed" else (arrived,)   # never reverse direction
        step = []
        if "down" in dirs:
            step += [(t, "down") for t in down.get(cur, [])]
        if "up" in dirs:
            step += [(s, "up") for s in up.get(cur, [])]
        for nb, d in step:
            if nb not in visited and nb in graph.nodes:
                visited[nb] = dist + 1
                queue.append((nb, dist + 1, d))

    sub_nodes = {nid: graph.nodes[nid] for nid in visited}
    sub_edges = [e for e in graph.edges if e.source in visited and e.target in visited]
    return Graph(nodes=sub_nodes, edges=sub_edges, distances=visited)
