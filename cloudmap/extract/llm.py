"""LLM-assisted extraction: a LOCAL model proposes dependency edges from a
resource's JSON; the deterministic Resolver then VERIFIES each proposal against
the scanned resources. The model generalizes to any resource type without a
hand-written extractor; the verifier keeps hallucinated edges out of the trusted
graph (unverifiable targets become explicit `external` nodes).

Local by design (ollama) - the resource JSON never leaves the machine.
"""

import json
import re

from ..local_model import DEFAULT_MODEL, OLLAMA_URL, generate_json  # noqa: F401  (re-exported)
from ..model import Edge, Node

# A plausible dependency target is a hostname / resource name / ARM id - never a
# secret or connection string. Reject anything that smells like one.
_SAFE_TARGET = re.compile(r"^[A-Za-z0-9._/\-]{1,160}$")
_SECRETISH = re.compile(r"(password|secret|accountkey|sharedaccesskey|=|;| )", re.I)

_PROMPT = """You are an Azure architecture analyzer. Given ONE Azure resource as JSON,
list the OTHER Azure resources it depends on.
Output ONLY JSON: {{"edges":[{{"target":"<resource name or hostname>","relationship":"<kind>"}}]}}
- relationship: short label e.g. hosted-on, reads-secret, connects-to, vnet-integration,
  sends-telemetry, pulls-image, uses-workspace, routes-to
- derive targets from properties, siteConfig.appSettings, connection strings, ids, hostnames
- NEVER output secret values, passwords or keys - only resource names or hostnames

Resource JSON:
{resource}
"""


def propose_edges(resource_raw, model=None, timeout=600):
    """Ask the local model for candidate edges. Returns [(target, relationship)].
    Empty list on any failure (ollama down, bad JSON, timeout)."""
    prompt = _PROMPT.format(resource=json.dumps(resource_raw, indent=2))
    parsed = generate_json(prompt, model=model, timeout=timeout)

    out = []
    for e in parsed.get("edges", []) if isinstance(parsed, dict) else []:
        tgt = str(e.get("target", "")).strip()
        rel = (str(e.get("relationship", "") or "references").strip())[:40]
        if tgt and _SAFE_TARGET.match(tgt) and not _SECRETISH.search(tgt):
            out.append((tgt, rel))
    return out


def _resolve(hint, resolver):
    """Verify a proposed target against scanned resources -> node id, else None."""
    h = hint.strip().lower()
    if h.startswith("/subscriptions/"):
        return resolver.by_resource_id(hint)
    return (resolver.host_lookup(h) or resolver.kv_by_name.get(h)
            or resolver.storage_by_name.get(h) or resolver.acr_by_loginserver.get(h)
            or resolver.by_name.get(h) or resolver.by_name.get(h.split(".")[0]))


def llm_edges_for_seed(seed_node, resolver, model=None):
    """Propose edges for the seed via the local model, then verify each. Returns
    (external_nodes, edges). Verified targets become normal edges; unverifiable
    ones become explicit external nodes (flagged as model-proposed)."""
    ext, edges, seen = {}, [], set()
    for tgt, rel in propose_edges(seed_node.raw, model=model):
        nid = _resolve(tgt, resolver)
        if nid and nid != seed_node.id:
            edges.append(Edge(seed_node.id, nid, rel, origin="model",
                              evidence="proposed by local model (target verified in scope)"))
        elif not nid:
            key = f"external://{tgt.lower()}"
            if key not in seen:
                seen.add(key)
                ext[key] = Node(id=key, name=tgt, type="external/llm-proposed",
                                external=True,
                                note="proposed by local model; not found in scanned scope")
            edges.append(Edge(seed_node.id, key, rel, origin="model",
                              evidence="proposed by local model (target NOT verified)"))
    return list(ext.values()), edges
