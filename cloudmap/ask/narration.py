"""Optional prose over an answer the graph already produced.

The model is handed the computed FACTS only - names, relationships, hop counts,
trust labels - and told to add nothing. The deterministic answer is printed above
the narration either way, so if the prose drifts, what the reader sees is a
narration disagreeing with the facts printed right above it, not a wrong answer.

Empty string on any failure: no ollama means no prose, never a missing answer.
"""

import json

from ..local_model import generate

_PROMPT = """You explain a cloud dependency answer to a developer, in 2-4 sentences.
Rules:
- Use ONLY the facts in the JSON. Never add a resource, relationship or cause that is not there.
- Say plainly which findings are unverified guesses, if any are.
- No headings, no bullet lists, no restating the JSON field names.

Question: {question}

Facts:
{facts}
"""


def narrate(result, model=None, timeout=120, max_findings=20):
    """Prose for a deterministic answer. Returns "" if there is nothing to narrate
    or the local model is unavailable."""
    if not result or result.get("error"):
        return ""

    findings = [
        {
            "name": f.get("name"),
            "type": f.get("type"),
            "hops": f.get("hops"),
            "trust": f.get("trust"),
            "relationships": [h.get("kind") for h in (f.get("path") or [])],
        }
        for f in (result.get("findings") or [])[:max_findings]
    ]
    facts = {
        "query": result.get("query"),
        "subject": result.get("subject_name"),
        "headline": result.get("headline"),
        "counts": result.get("facts"),
        "findings": findings,
        "caveats": result.get("warnings") or [],
    }
    prompt = _PROMPT.format(question=result.get("question", ""),
                            facts=json.dumps(facts, indent=2))
    return generate(prompt, model=model, timeout=timeout, json_format=False).strip()
