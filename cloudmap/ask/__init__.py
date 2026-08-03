"""Ask: answer questions about a map that has already been verified.

Read-only by construction, and that is this layer's whole trust story. The facts
in an answer are computed from the graph (`queries.py`); a local model may at most
route an unusual phrasing (`intent.py`) or put the computed facts into prose
(`narration.py`). Because the model never produces the facts, it cannot turn a guess
into a fact - the worst it can do is choose the wrong query, which the printed
query name and subject make immediately obvious.

An answer also inherits the map's honesty: if the artifact says it is incomplete,
every answer drawn from it carries that warning.
"""

from ..graph import HIGH_LEVEL_PREFIXES, is_high_level
from . import queries
from .intent import SUPPORTED, llm_parse, parse
from .narration import narrate

__all__ = ["SUPPORTED", "answer", "narrate", "warnings"]


def answer(graph, question, allow_llm_intent=False, max_hops=None, model=None):
    """Route `question` to one deterministic query and run it against `graph`."""
    plan = parse(question, graph)
    if plan is None and allow_llm_intent:
        plan = llm_parse(question, graph, model=model)
    if plan is None:
        result = _error("I could not turn that into a query over this map.")
    else:
        result = _run(graph, plan, max_hops=max_hops)
    result["question"] = question
    result["warnings"] = warnings(graph)
    return result


def _run(graph, plan, max_hops=None):
    query = plan.get("query")
    subject, target = plan.get("subject"), plan.get("target")

    if query in ("impact", "depends") and not subject:
        return _error(_no_subject(graph, "Name a resource from this map in the question."),
                      query=query)
    if query == "paths" and not (subject and target):
        return _error(_no_subject(graph, "Name two resources: 'how does <resource> reach "
                                         "<other>'."), query=query)

    if query == "impact":
        return queries.impact(graph, subject, max_hops=max_hops)
    if query == "depends":
        return queries.depends(graph, subject, max_hops=max_hops)
    if query == "paths":
        return queries.paths(graph, subject, target)
    if query == "shared":
        return queries.shared(graph)
    if query == "guesses":
        return queries.guesses(graph)
    if query == "summary":
        return queries.summary(graph)
    return _error(f"Unknown query '{query}'.")


def _no_subject(graph, default):
    """Why the resource was not found matters. In a collapsed map the instance is
    usually right there on the diagram, inside a group box - saying "name a
    resource" sends the reader looking for a typo that does not exist."""
    if not is_high_level(graph):
        return default
    groups = sorted({n.name for n in graph.nodes.values()
                     if str(n.id).startswith(HIGH_LEVEL_PREFIXES)})
    return ("This map is the high-level view: resources are grouped into one box per "
            "type, so instance names do not exist in it. Ask about a group instead "
            f"({', '.join(groups[:6])}{', ...' if len(groups) > 6 else ''}), or re-run "
            "the trace with --level detail to keep instance names.")


def _error(message, query=None):
    return {"query": query, "subject": None, "subject_name": None, "error": message,
            "headline": message, "findings": [], "facts": {}, "supported": list(SUPPORTED)}


def warnings(graph):
    """What the reader must know before believing the answer: the map's own limits."""
    meta = getattr(graph, "meta", None) or {}
    out = []
    if meta.get("complete") is False:
        out.append("This map is marked INCOMPLETE - resources and edges may be missing, "
                   "so this answer can be missing them too.")
    if meta.get("truncated"):
        out.append("The scan behind this map hit its pagination cap.")
    for gap in meta.get("read_gaps") or []:
        out.append(f"read gap in the source scan: {gap}")
    # a class of edge the scan never went looking for: an empty answer here means
    # "not looked for", not "nothing found"
    for spot in meta.get("blind_spots") or []:
        out.append(f"blind spot in the source scan: {spot}")

    grouped = sum(1 for nid in graph.nodes if str(nid).startswith(HIGH_LEVEL_PREFIXES))
    if grouped:
        out.append(f"This is the high-level view: {grouped} box(es) each stand for every "
                   "instance of a resource type, so counts and names here are per type, "
                   "not per instance.")

    model_edges = sum(1 for e in graph.edges if e.origin != "extracted")
    if model_edges:
        out.append(f"{model_edges} edge(s) in this map are model-proposed guesses; findings "
                   "that rely on them are marked unverified.")
    external = sum(1 for n in graph.nodes.values() if n.external)
    if external:
        out.append(f"{external} node(s) are referenced but were never verified as real "
                   "resources.")
    return out
