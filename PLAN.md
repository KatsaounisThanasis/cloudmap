# cloudmap - build plan

## What this is

Give it the name of one cloud resource, get back a **trustworthy** dependency map
(the blast radius) that a developer can open, click, and ask questions about.

Two non-negotiable principles:

1. **Local-first** - nothing leaves the machine. Non-negotiable in a regulated
   enterprise (banking, healthcare, public sector), where uploading a cloud
   inventory to a SaaS vendor is simply not an option.
2. **Trust** - every edge is either *verified* (a deterministic rule found proof)
   or clearly *marked as a guess* (a model proposed it). A guess never gets to
   look like a fact.

## Two users, one pipeline

- **Producer** (you): has cloud access, runs the scan, produces the map artifact.
- **Consumers** (devs): no cloud access, no install - they open the shared map in
  a browser and ask "what breaks if I touch this?".

The producer/consumer split *is* the architecture: produce a trustworthy
artifact, let others consume it. AI reads the already-verified map and explains
it - read-only, so it can never turn a guess into a fact.

## Three layers (why we are not vendor-locked)

```
adapters/           per-cloud, thin, swappable   raw export -> neutral graph
  azure.py            (Azure today; terraform/aws later)
core/               cloud-agnostic - ALL the value lives here
  model, graph, blast-radius, trust/provenance
ask/                cloud-agnostic questions over a verified map
  queries (computed), intent (routing), narration (prose)
render/             cloud-agnostic outputs
  drawio, mermaid, json, html viewer
local_model.py      the ONE place a model is called, and it is localhost
```

`model.py` is already cloud-neutral. The adapter is the only part that knows a
specific cloud. "Not vendor-locked" is a promise of the *seams*, not a day-one
feature: we keep the seams clean so a second cloud is possible without a rewrite,
and build it only when someone actually needs it.

## How we know the output is trustworthy

Trust does not come from more tests against data we wrote ourselves (that is
circular). It comes from three things:

1. **Golden data from real tenants.** A scrubbed, real-shaped export becomes a
   test: "given this real input, the map MUST contain these edges." The gold
   standard is a *captured raw export*. `cloudmap capture` + `cloudmap scrub`
   now produce one (scrub is proven not to change the graph, only the identities,
   by `tests/test_scrub.py`), and `test_captured_real_export_is_believable`
   activates the moment `fixtures/captured_real.json` exists. Until someone runs
   a capture against a real tenant, the standing fixture is one reconstructed
   from a hand-verified real deployment (clearly labelled).
2. **Every edge carries its proof.** Not just "web -> storage" but *why*: which
   setting / property / rule produced it. Provenance, not faith.
3. **Honest about blind spots.** Read failures and truncated scans are reported;
   the JSON says `complete: false` when it is. (Done.)

## Phases (each ships something useful on its own)

### Phase 0 - Foundation: trust + seams  ← DONE
- [x] Provenance on every edge: `origin` (extracted vs model) + `evidence` (proof).
- [x] Model-proposed edges never override a deterministic edge (deterministic wins).
- [x] Renderers show provenance: model edges dashed; JSON carries origin+evidence.
- [x] Real-shaped golden test (reconstructed from the verified orders deploy).
- [x] Document the neutral graph format (FORMAT.md).
- [x] Name the Azure ingest+extract as the first *adapter* (`adapters/`), with
      auto-detect (raw export vs neutral cloudmap graph) and neutral round-trip.

### Phase 1 - Sharing  ← DONE
- [x] Self-contained HTML viewer (`render/html.py`, `--html`): open in a browser,
      no install/CDN; pan/zoom, search, click a resource to focus its blast radius;
      side panel shows each edge's relationship + trust + evidence; model edges are
      dashed/animated; nodes coloured & iconed by category; seed pulses.

### Phase 2 - Ask  ← DONE
- [x] `cloudmap ask <map.json> "<question>"` over the *verified* graph, read-only.
      Six queries, each **computed** by traversal, never generated: `impact` (what
      breaks if I touch X), `depends`, `paths` (how X reaches Y), `shared` (what
      several resources depend on), `guesses` (what in this map is not proven),
      `summary`.
- [x] The model is confined to two roles it cannot lie from: routing an unusual
      phrasing to one of the six queries (`--llm`, and the query name plus the
      resource it picked are validated against the graph and printed), and narrating
      the already-computed facts (`--explain`). No ollama = same answer, no prose.
- [x] Trust travels with the answer: every finding shows its hops and each hop's
      proof; one model-proposed hop marks the whole finding `[GUESS]`; passing
      *through* an unverified node does the same, while merely *ending* at one is
      flagged as an unverified target instead (the reference itself is proven).
- [x] An answer inherits the map's own caveats - `meta.complete: false`, read gaps
      and truncation are repeated on every answer drawn from that artifact.

### Phase 2.5 - UX & Core Stability  ← DONE
- [x] Interactive CLI Wizard (`interactive.py`): Zero-config start with cascading resource group menus and graceful Azure CLI error handling.
- [x] Azure Resource Graph (ARG) Query enhancements: Subscription pinning, 1000-resource pagination warnings, and cross-tenant resolution.
- [x] Enterprise-grade Python strict type hinting across core files (`model.py`, `graph.py`).
- [x] Extensive test coverage (185+ passing tests) securing the interactive UI and ARG extraction layers.

### Phase 3 - Extend  ← DONE
- [x] Coverage: model proposes edges for resource types with no hand-written rule;
      verifier checks; results marked as model-derived.
- [ ] Second adapter (Terraform state, then a second cloud) when there is a real need.

## What we keep vs change (no throwaway rewrite)

- **Keep:** `model.py`, `graph.py`, `render/` - already the right shape.
- **Refactor into an adapter:** `ingest/azure.py` + `extract/extractors.py`.
- **Add:** neutral-format doc, edge provenance, import auto-detect, real-data golden test.

A literal rewrite would lose the real-tenant validation, the guards, and the
extractors that already work - to arrive back where we are, with less. We design
clean and build on top.
