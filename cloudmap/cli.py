"""cloudmap command-line interface."""

import argparse
import os
import sys

from .graph import blast_radius, build_graph, collapse_high_level, find_seeds, node_from_id
from .model import Edge
from .render.drawio import to_drawio
from .render.html import to_html
from .render.json_out import to_json
from .render.mermaid import to_mermaid


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cloudmap",
        description="Trace an Azure resource's full dependency graph (blast radius) "
                    "and export it to draw.io / Mermaid / JSON.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("trace", help="trace a component's blast radius")
    t.add_argument("name", help="resource name (or substring) to seed from")
    src = t.add_mutually_exclusive_group(required=True)
    src.add_argument("--from", dest="fixture", help="path to a Resource Graph JSON fixture")
    src.add_argument("--live", action="store_true", help="query live Azure (guarded, opt-in)")
    t.add_argument("--allow-live", action="store_true", help="required together with --live")
    t.add_argument("--single-sub", action="store_true",
                   help="live: query only the active subscription (default: all non-prod subs)")
    t.add_argument("--resolve-secrets", action="store_true",
                   help="live: read KV secret values in-memory to see through KV-backed "
                        "connection strings (never printed or written)")
    t.add_argument("--llm", action="store_true",
                   help="also let a LOCAL model (ollama) propose edges from the seed's JSON; "
                        "each proposal is verified against scanned resources (nothing leaves the machine)")
    t.add_argument("--enrich", choices=["auto", "seed", "all", "none"], default="auto",
                   help="live: which web apps to deep-enrich for the dependencies that live "
                        "in app config (Key Vault refs, connection strings, RBAC). "
                        "auto = the seed alone when the seed is a web app, every app in scope "
                        "when it is not (only other apps' config can reveal what depends on a "
                        "shared resource); all = every app in scope; none = ARM topology only")
    t.add_argument("--level", choices=["high", "detail"], default="high",
                   help="high = architecture view grouped by resource type (default); "
                        "detail = every instance with its real name")
    t.add_argument("--direction", choices=["both", "down", "up"], default="both",
                   help="both = full blast radius (default); down = dependencies; up = dependents")
    t.add_argument("--max-hops", type=int, default=None, help="limit traversal depth")
    t.add_argument("-o", "--out", default=None, help="draw.io output file")
    t.add_argument("--mermaid", default=None, help="also write a Mermaid file")
    t.add_argument("--json", dest="json_out", default=None, help="also write the graph JSON")
    t.add_argument("--html", dest="html_out", default=None,
                   help="also write a self-contained interactive HTML viewer (open in a browser)")

    c = sub.add_parser("capture", help="save a raw live export so it can become a fixture")
    c.add_argument("-o", "--out", required=True, help="where to write the export")
    c.add_argument("--allow-live", action="store_true", help="required: this reads live Azure")
    c.add_argument("--single-sub", action="store_true",
                   help="capture only the active subscription (default: all non-prod subs)")
    c.add_argument("--enrich", choices=["all", "none"], default="all",
                   help="all (default) also captures web app config, which is where the "
                        "interesting dependencies live; none captures ARM topology only")
    c.add_argument("--resolve-secrets", action="store_true",
                   help="resolve Key Vault references while capturing (values are redacted "
                        "by the scrub pass, but see --no-scrub)")
    c.add_argument("--no-scrub", action="store_true",
                   help="write the export UNSCRUBBED - real names, hosts and app settings, "
                        "i.e. credentials on disk. Never commit that file")

    s = sub.add_parser("scrub", help="pseudonymise a raw export so it can be committed")
    s.add_argument("input", help="a raw export (from `capture --no-scrub` or `az graph query`)")
    s.add_argument("-o", "--out", required=True, help="where to write the scrubbed export")

    a = sub.add_parser("ask", help="ask a question about a map you already produced")
    a.add_argument("map", help="a cloudmap graph JSON (written by `trace --json`) or a raw export")
    a.add_argument("question", help='e.g. "what breaks if I touch kv-orders-dev"')
    a.add_argument("--explain", action="store_true",
                   help="also narrate the answer with a LOCAL model (ollama); the computed "
                        "answer is printed either way")
    a.add_argument("--llm", action="store_true",
                   help="if no built-in rule understands the question, let a LOCAL model pick "
                        "the query (its choice is validated against the map)")
    a.add_argument("--max-hops", type=int, default=None, help="limit traversal depth")
    a.add_argument("--json", dest="json_out", action="store_true",
                   help="print the answer as JSON instead of text (for scripting)")

    args = parser.parse_args(argv)
    if args.cmd == "trace":
        return _cmd_trace(args)
    if args.cmd == "capture":
        return _cmd_capture(args)
    if args.cmd == "scrub":
        return _cmd_scrub(args)
    if args.cmd == "ask":
        return _cmd_ask(args)
    return 1


def _cmd_trace(args):
    read_gaps, blind_spots = [], []
    if args.live:
        from .ingest.azure import query_live
        resources, truncated = query_live(allow_live=args.allow_live, tenant_wide=not args.single_sub)
        graph = build_graph(resources)
    else:
        from .adapters import load_graph
        resources, truncated = [], False
        graph = load_graph(args.fixture)   # auto-detects raw export vs neutral cloudmap graph

    seeds = find_seeds(graph, args.name)
    if not seeds:
        print(f"No resource matched '{args.name}'.", file=sys.stderr)
        return 2
    if len(seeds) > 1:
        print(f"'{args.name}' is ambiguous. Matches:", file=sys.stderr)
        for s in seeds:
            print(f"  - {graph.nodes[s].name}  ({graph.nodes[s].type})", file=sys.stderr)
        return 2
    seed = seeds[0]

    # Live: Resource Graph omits config/RBAC/diagnostics, so deep-enrich web apps
    # and re-extract, then report whatever stayed invisible.
    if args.live:
        graph, read_gaps, blind_spots = _enrich_live(args, graph, seed, resources)

    # LLM-assisted extraction (opt-in): local model proposes, Resolver verifies.
    if args.llm:
        from .extract.extractors import Resolver, merge_model_edges
        from .extract.llm import llm_edges_for_seed
        resolver = Resolver(graph.nodes)
        lext, ledges = llm_edges_for_seed(graph.nodes[seed], resolver)
        for nd in lext:
            graph.nodes.setdefault(nd.id, nd)
        # model edges may only add new targets, never override deterministic ones
        graph.edges = merge_model_edges(graph.edges, ledges)

    sub = blast_radius(graph, seed, direction=args.direction, max_hops=args.max_hops)
    if args.level == "high":
        sub = collapse_high_level(sub, seed)

    meta = {"truncated": truncated, "read_gaps": read_gaps, "blind_spots": blind_spots}

    out = args.out or f"{graph.nodes[seed].name}.blast.drawio"
    _ensure_parent(out)
    with open(out, "w", encoding="utf-8") as f:
        f.write(to_drawio(sub, seed))
    if args.mermaid:
        _ensure_parent(args.mermaid)
        with open(args.mermaid, "w", encoding="utf-8") as f:
            f.write(to_mermaid(sub, seed))
    if args.json_out:
        _ensure_parent(args.json_out)
        with open(args.json_out, "w", encoding="utf-8") as f:
            f.write(to_json(sub, seed, meta=meta))
    if args.html_out:
        _ensure_parent(args.html_out)
        with open(args.html_out, "w", encoding="utf-8") as f:
            f.write(to_html(sub, seed, meta=meta))

    _print_summary(sub, seed, out, truncated=truncated, blind_spots=blind_spots)
    return 0


_WEBAPP = "microsoft.web/sites"
# Workloads whose dependencies live in free-text config, so an unresolved
# reference is worth resurfacing as an explicit external node. Container apps are
# here but not in the enrichment list: Resource Graph already returns their
# template, so there is nothing extra to fetch.
_CONFIG_WORKLOADS = (_WEBAPP, "microsoft.app/containerapps")


def _enrichment_targets(graph, seed, mode, direction):
    """Which web apps to deep-enrich, and which stay a blind spot.

    An app's config-level dependencies exist nowhere until that app is enriched,
    but enriching a whole tenant on every trace costs an `az` round-trip per app.
    So `auto` spends the calls where the answer actually needs them: a web-app
    seed's own config already yields its downward view, whereas a shared-resource
    seed (Key Vault, database, plan) can only learn its dependents from the
    config of the apps pointing at it.
    """
    apps = [n for n in graph.nodes.values() if n.type == _WEBAPP]
    seed_only = [n for n in apps if n.id == seed]
    if mode == "none":
        chosen = []
    elif mode == "all":
        chosen = apps
    elif mode == "seed" or seed_only and direction != "up":
        chosen = seed_only
    else:
        chosen = apps
    picked = {n.id for n in chosen}
    return chosen, [n for n in apps if n.id not in picked]


def _enrich_live(args, graph, seed, resources):
    """Deep-enrich web apps, re-extract, and name what stayed invisible.
    Returns (graph, read_gaps, blind_spots) - `resources` is extended in place."""
    from .extract.extractors import Resolver, _dedupe, seed_external_dependencies
    from .ingest.azure import enrich_webapps

    targets, skipped = _enrichment_targets(graph, seed, args.enrich, args.direction)
    read_gaps, blind_spots = [], []

    if targets:
        print(f"Deep-enriching {len(targets)} web app(s) - app settings, connection "
              f"strings, RBAC, diagnostics...", file=sys.stderr)
        enr = enrich_webapps([n.raw for n in targets], resolve_secrets=args.resolve_secrets)
        read_gaps = enr["errors"]
        if read_gaps:
            print("Read gaps while enriching (edges below may be INCOMPLETE):", file=sys.stderr)
            for msg in read_gaps:
                print(f"  ! could not read {msg}", file=sys.stderr)
        resources.extend(enr["role_assignments"])
        graph = build_graph(resources)            # re-extract, now that config is present
        resolver = Resolver(graph.nodes)

        # Unresolved references are resurfaced as external nodes for the seed only:
        # doing it for every enriched app would bury the map in tenant-wide noise.
        if graph.nodes[seed].type in _CONFIG_WORKLOADS:
            ext_nodes, ext_edges = seed_external_dependencies(graph.nodes[seed], resolver)
            for nd in ext_nodes:
                graph.nodes.setdefault(nd.id, nd)
            graph.edges.extend(ext_edges)

        for app_id, tid in enr["diagnostics"]:
            if app_id not in graph.nodes:
                continue
            target = resolver.by_resource_id(tid)
            if not target:
                nd = node_from_id(tid, note="diagnostic target (outside scanned scope)")
                graph.nodes.setdefault(nd.id, nd)
                target = nd.id
            graph.edges.append(Edge(app_id, target, "sends-logs-to"))

        graph.edges = _dedupe(graph.edges)

    # An un-enriched app is a known class of missing edge - say so rather than let
    # an empty upward view read as "nothing depends on this".
    if skipped and args.direction != "down":
        blind_spots.append(
            f"{len(skipped)} web app(s) in scope were not deep-enriched, so anything that "
            f"depends on this resource through app config (Key Vault references, connection "
            f"strings, backend hostnames) cannot appear as an inbound edge. "
            f"Re-run with --enrich all to close this gap."
        )
    return graph, read_gaps, blind_spots


def _cmd_capture(args):
    """Save what the cloud actually returned, before any interpretation.

    A fixture we invented can only confirm what we already believe; a captured
    export can contradict us, which is the only way a test earns trust. Scrubbed
    by default, because the interesting half of a capture is app config and app
    config is full of credentials."""
    from .ingest.azure import enrich_webapps, query_live

    resources, truncated = query_live(allow_live=args.allow_live,
                                      tenant_wide=not args.single_sub)
    if args.enrich == "all":
        apps = [r for r in resources if str(r.get("type", "")).lower() == _WEBAPP]
        print(f"Deep-enriching {len(apps)} web app(s) so the capture includes the "
              f"dependencies that only exist in app config...", file=sys.stderr)
        enr = enrich_webapps(apps, resolve_secrets=args.resolve_secrets)
        for msg in enr["errors"]:
            print(f"  ! could not read {msg}", file=sys.stderr)
        resources.extend(enr["role_assignments"])

    stats = None
    if args.no_scrub:
        print("WARNING: writing an UNSCRUBBED export - real resource names, hostnames "
              "and app settings (i.e. credentials) are about to be written to disk.\n"
              "         Do not commit or share this file; run `cloudmap scrub` on it first.",
              file=sys.stderr)
    else:
        from .scrub import scrub
        resources, stats = scrub(resources)

    _write_export(args.out, resources, scrubbed=not args.no_scrub,
                  meta={"truncated": truncated, "enriched": args.enrich == "all"})
    print(f"Captured {len(resources)} rows -> {args.out}")
    _print_scrub_stats(stats)
    return 0


def _cmd_scrub(args):
    import json as _json

    from .scrub import scrub

    with open(args.input, encoding="utf-8") as f:
        data = _json.load(f)
    resources = data.get("data", data) if isinstance(data, dict) else data
    meta = dict((data.get("meta") or {}) if isinstance(data, dict) else {})

    scrubbed, stats = scrub(resources)
    _write_export(args.out, scrubbed, scrubbed=True, meta=meta)
    print(f"Scrubbed {len(scrubbed)} rows -> {args.out}")
    _print_scrub_stats(stats)
    return 0


def _write_export(path, resources, scrubbed, meta=None):
    import json as _json

    _ensure_parent(path)
    doc = {"meta": dict(meta or {}, scrubbed=scrubbed), "data": resources}
    with open(path, "w", encoding="utf-8") as f:
        _json.dump(doc, f, indent=2)


def _print_scrub_stats(stats):
    """Counts only. The mapping itself is the re-identification key and is never
    printed or written."""
    if not stats:
        return
    print(f"Scrub: {stats['tokens']} identifier(s) pseudonymised, "
          f"{stats['redactions']} credential fragment(s) redacted.")
    if stats["short_tokens_left_alone"]:
        print(f"  ! left alone (too short to replace safely): "
              f"{', '.join(stats['short_tokens_left_alone'])}")
    print("  Review the output before committing it - a scrubber is not a proof.")


def _cmd_ask(args):
    import json as _json

    from .adapters import load_graph
    from .ask import answer, narrate

    graph = load_graph(args.map)
    result = answer(graph, args.question, allow_llm_intent=args.llm, max_hops=args.max_hops)
    if args.explain:
        result["narration"] = narrate(result)

    if args.json_out:
        print(_json.dumps(result, indent=2))
        return 0 if not result.get("error") else 2

    _print_answer(result)
    return 0 if not result.get("error") else 2


def _print_answer(result):
    if result.get("error"):
        print(result["error"], file=sys.stderr)
        print("\nQuestions this map can answer:", file=sys.stderr)
        for line in result.get("supported", []):
            print(f"  {line}", file=sys.stderr)
        return

    subject = result.get("subject_name")
    print(f"Query: {result['query']}" + (f"  ·  subject: {subject}" if subject else ""))
    print(result["headline"])

    if result.get("hint"):
        print(f"  -> {result['hint']}")
    for w in result.get("warnings", []):
        print(f"  ! {w}")

    print()
    for f in result["findings"]:
        flag = "   [GUESS]" if f["trust"] != "verified" else (
            "   [unverified target]" if f.get("external") else "")
        metric = f"  {f['metric']}" if f.get("metric") else ""
        print(f"  {f['name']}  ({f['type']}){metric}{flag}")
        if f.get("why"):
            print(f"      why unverified: {f['why']}")
        for hop in f.get("path", []):
            print(f"      {hop['source']} --{hop['kind']}--> {hop['target']}")
            if hop.get("evidence"):
                print(f"          proof: {hop['evidence']}")
        for dep in f.get("dependents", []):
            print(f"      depended on by: {dep}")

    if result.get("narration"):
        print("\nNarration (local model, from the facts above - not a source of facts):")
        print(f"  {result['narration']}")


def _ensure_parent(path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _print_summary(graph, seed, out, truncated=False, blind_spots=()):
    n = graph.nodes[seed]
    ext = sum(1 for x in graph.nodes.values() if x.external)
    print(f"Seed: {n.name} ({n.type})")
    print(f"Blast radius: {len(graph.nodes)} resources "
          f"({ext} external/unverified), {len(graph.edges)} dependencies")
    if truncated:
        print("INCOMPLETE: scan hit the pagination cap - some resources/edges are missing.")
    for spot in blind_spots:
        print(f"BLIND SPOT: {spot}")
    print(f"draw.io: {out}")
    print()
    for e in graph.edges:
        tgt = graph.nodes[e.target]
        tag = "  [external]" if tgt.external else ""
        print(f"  {graph.nodes[e.source].name}  --{e.kind}-->  {tgt.name}{tag}")
