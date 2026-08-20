"""LLM-assisted extraction: a LOCAL model reads ONE resource's JSON and proposes
which other resources it depends on; the deterministic Resolver then VERIFIES each
proposal against the scanned set. This is the discovery half of "map anything" -
it catches dependencies expressed as free text (a hostname or name in a setting)
that no hand-written rule and no ARM-id pass would find.

Trust is preserved by two rails, not by trusting the model:
1. The model is an EXTRACTOR, told to output only targets that appear verbatim in
   the JSON - not to invent.
2. Only proposals whose target RESOLVES to a real scanned resource survive. An
   unverifiable proposal is dropped, never shown - a model guess we cannot confirm
   is exactly what must not reach the map. Verified edges are still marked
   origin="model" (drawn dashed), because the model supplied the relationship.

Local by design - the model runs on your machine (ollama by default, any
OpenAI-compatible server via CLOUDMAP_LLM_URL) and the resource JSON never
leaves it.
"""

import json
import re

from ..local_model import DEFAULT_MODEL, LLM_URL, OLLAMA_URL, generate_json  # noqa: F401  (re-exported)
from ..model import Edge

# A plausible dependency target is a hostname / resource name / ARM id - never a
# secret or connection string. Reject anything that smells like one.
_SAFE_TARGET = re.compile(r"^[A-Za-z0-9._/\-]{1,160}$")
_SECRETISH = re.compile(r"(password|secret|accountkey|sharedaccesskey|=|;| )", re.I)

_PROMPT = """You analyse ONE Azure resource to find the OTHER Azure resources it depends on.
Work ONLY from the JSON below. Every target you output MUST appear verbatim in the JSON
(a hostname, a resource name, or an ARM resource id inside a setting, connection string,
endpoint or property). Do NOT invent, guess or infer a resource that is not written there.

Output ONLY JSON of this shape:
{{"edges":[{{"target":"<hostname | name | ARM id from the JSON>","relationship":"<short kind>"}}]}}
- relationship: one of hosted-on, reads-secret, connects-to, sends-telemetry, pulls-image,
  routes-to, uses-workspace, vnet-integration, or a short lowercase verb phrase.
- Prefer the most specific hostname or name. Never output a secret, password, key or a
  connection-string value - only the resource it points at.
- If nothing is referenced, output {{"edges":[]}}.

Resource type: {rtype}
Resource JSON:
{resource}
"""


def propose_edges(resource_raw, model=None, timeout=600):
    """Ask the local model for candidate edges. Returns [(target, relationship)].
    Empty list on any failure (model server down, bad JSON, timeout)."""
    prompt = _PROMPT.format(rtype=resource_raw.get("type", "unknown"),
                            resource=json.dumps(resource_raw, indent=2))
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
    """Propose edges for the seed via the local model, keep only the ones whose
    target resolves to a real scanned resource. Returns (external_nodes, edges) -
    external_nodes is always empty (an unverifiable model proposal is dropped, not
    shown); the tuple shape is kept for the caller. Verified edges are origin
    "model" so renderers draw them dashed and the ask layer treats them as guesses."""
    edges, seen = [], set()
    for tgt, rel in propose_edges(seed_node.raw, model=model):
        nid = _resolve(tgt, resolver)
        if not nid or nid == seed_node.id or (seed_node.id, nid) in seen:
            continue                       # unverifiable or redundant -> drop it
        seen.add((seed_node.id, nid))
        edges.append(Edge(seed_node.id, nid, rel, origin="model",
                          evidence=f"proposed by local model from the resource's own JSON "
                                   f"(target '{tgt}' verified as a scanned resource)"))
    return [], edges
