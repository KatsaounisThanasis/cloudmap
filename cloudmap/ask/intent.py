"""Turn a developer's question into ONE deterministic query.

Rules first, deliberately: the phrasings people actually type are keyword-shaped,
a rule match costs nothing, and it works on a machine with no model installed. A
local model is only a FALLBACK for phrasings the rules miss (opt-in, `--llm`), and
even then it may do exactly two things - name a query from a fixed list and name a
resource - both of which are validated against this graph before anything runs.
The model picks a route; it never supplies an answer. A wrong route is visible,
because the query and subject it chose are printed with the answer.
"""

import re

from ..local_model import generate_json
from .queries import QUERIES

SUPPORTED = (
    "what breaks if I touch <resource>     - what depends on it (blast radius)",
    "what does <resource> depend on        - what it needs to work",
    "how does <resource> reach <other>     - the paths between two resources",
    "what is shared in this map            - resources several others depend on",
    "what should I not trust               - the model-proposed guesses",
    "explain this map                      - a summary of what was traced",
)

# Checked in order: the specific, whole-map questions first, so "which paths cross
# a shared vault" is understood as a question about sharing, not as a path lookup.
_RULES = (
    ("guesses", (r"(not trust|untrusted|unverified|guess|how sure|confidence|"
                 r"model[- ]proposed|reliable)")),
    ("shared", (r"(shared|in common|common dependenc|several (apps|resources|teams)|"
                r"more than one (app|resource|team))")),
    ("impact", (r"(what breaks|breaks if|what depends|who depends|dependents|blast radius|"
                r"impact|affected|if i (touch|change|delete|remove|restart|move|redeploy|"
                r"rotate|break))")),
    # The verbs of "I am about to change this": whatever the sentence around them
    # looks like, the question is always who else feels it. Catching them with a
    # rule keeps the common real-world phrasings off the model entirely.
    ("impact", (r"\b(rotat\w*|restart\w*|delet\w*|decommission\w*|redeploy\w*|resiz\w*|"
                r"migrat\w*|patch\w*|upgrad\w*|drain\w*|scal\w*|reboot\w*)\b")),
    ("depends", r"(depends? on|dependenc(y|ies)|what does .*(need|use)|needs|uses|downstream)"),
    ("paths", r"(paths?|reach|route|how does .*(talk|connect|get) )"),
    ("summary", r"(explain|summar|overview|what is (this|in this)|describe|shape of)"),
)

_NEEDS_SUBJECT = ("impact", "depends")


def parse(question, graph):
    """Rule-based routing. Returns a plan dict, or None if no rule recognised it."""
    text = (question or "").strip()
    if not text:
        return None
    subjects = match_nodes(graph, text)
    for query, pattern in _RULES:
        if re.search(pattern, text, re.I):
            return _plan(query, subjects)
    return None


def _plan(query, subjects):
    plan = {"query": query}
    if query == "paths":
        if subjects:
            plan["subject"] = subjects[0]
        if len(subjects) > 1:
            plan["target"] = subjects[1]
    elif query in _NEEDS_SUBJECT and subjects:
        plan["subject"] = subjects[0]
    return plan


def match_nodes(graph, text):
    """Resource names mentioned in `text`, in the order they appear.

    Longest name wins on overlap, so a question about `myapi` is not read as a
    question about `api` - the same label-boundary discipline the extractors use.
    """
    low = text.lower()
    hits = []
    for nid, node in graph.nodes.items():
        name = (node.name or "").lower()
        if len(name) < 3 or name not in low:
            continue
        hits.append((low.index(name), len(name), nid))

    hits.sort(key=lambda h: (-h[1], h[0]))
    claimed, chosen = [], []
    for pos, length, nid in hits:
        span = (pos, pos + length)
        if any(span[0] >= c[0] and span[1] <= c[1] for c in claimed):
            continue
        claimed.append(span)
        chosen.append((pos, nid))
    chosen.sort()
    return [nid for _pos, nid in chosen]


_PROMPT = """You route a question to ONE query over a cloud dependency map.
Output ONLY JSON: {{"query":"<name>","subject":"<resource name or empty>","target":"<resource name or empty>"}}
Allowed query names and what they mean:
- impact: what breaks if the subject changes (what depends on it)
- depends: what the subject itself depends on
- paths: how subject reaches target
- shared: resources that several others depend on
- guesses: which parts of the map are unverified
- summary: explain the whole map
Use ONLY resource names from this list, copied exactly: {names}
Question: {question}
"""


def llm_parse(question, graph, model=None, timeout=120):
    """Fallback routing by the local model. Anything it returns is validated: an
    unknown query name is refused, and a resource it invents is dropped because it
    resolves against this graph or not at all."""
    names = sorted({n.name for n in graph.nodes.values() if n.name})
    out = generate_json(
        _PROMPT.format(names=", ".join(names[:200]), question=question),
        model=model, timeout=timeout)

    query = str(out.get("query", "")).strip().lower()
    if query not in QUERIES:
        return None
    plan = {"query": query}
    for key in ("subject", "target"):
        hint = str(out.get(key) or "").strip()
        if not hint:
            continue
        resolved = match_nodes(graph, hint)
        if resolved:
            plan[key] = resolved[0]
    return plan
