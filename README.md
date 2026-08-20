# cloudmap

[![CI](https://github.com/KatsaounisThanasis/cloudmap/actions/workflows/ci.yml/badge.svg)](https://github.com/KatsaounisThanasis/cloudmap/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/cloudmap?color=1f7a8c)](https://pypi.org/project/cloudmap/)
[![Python](https://img.shields.io/pypi/pyversions/cloudmap?color=1f7a8c)](https://pypi.org/project/cloudmap/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Give it the name of one Azure resource. Get back its full dependency graph -
the blast radius - as an editable draw.io diagram with native Azure icons, plus
an interactive HTML viewer, Mermaid, JSON and CSV.**

Azure Resource Graph has no "dependencies" table. The relationships that matter
- what an App Service is hosted on, which Key Vault it reads, which subnet it
integrates with, which managed identity has which role where, what a Private
Endpoint fronts, which App Gateway routes to it - are buried inside each
resource's `properties`. cloudmap reads them out, correlates them into one
graph, and draws it.

![The interactive HTML viewer: the seed at the centre, dependencies fanned out with native Azure icons, each edge carrying its relationship and proof](estate-viewer.png)

**Contents** · [Install](#install) · [60-second demo](#60-second-demo) ·
[Interactive wizard](#interactive-wizard) · [Why](#why) ·
[What it maps](#what-it-maps) · [How it works](#how-it-works) ·
[Ask a map questions](#ask-a-map-questions) · [Live Azure](#live-azure-opt-in) ·
[Capture and scrub](#capture-a-real-export-so-the-tests-can-be-wrong) ·
[Usage reference](#usage-reference)

## Install

```
pip install cloudmap
```

Python 3.9+. Two runtime dependencies (`rich` and `questionary`, both for the
terminal UI). Live mode additionally needs the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
on your PATH, and the optional AI passes need a local [ollama](https://ollama.com).

To work on it instead:

```
git clone https://github.com/KatsaounisThanasis/cloudmap && cd cloudmap
pip install -e ".[dev]" && pytest
```

## 60-second demo

No Azure account needed - the repo ships a synthetic estate:

```
cloudmap trace contoso-web --from fixtures/contoso.json -o contoso-web.drawio
```

```text
Blast radius: 9 resources (0 external), 9 dependencies
╭────────────────────────────── Dependency Graph ──────────────────────────────╮
│ 🌐 contoso-web                                                               │
│ ├── --hosted-on--> 📦 App Service Plan                                       │
│ ├── --vnet-integration--> 📦 Virtual Network                                 │
│ ├── --connects-to--> 🗄️ SQL Server                                           │
│ ├── --sends-telemetry--> 📦 App Insights                                     │
│ │   └── --uses-workspace--> 📦 Log Analytics                                 │
│ ├── --reads-secret; role: Key Vault Secrets User--> 🔐 Key Vault             │
│ └── --connects-to--> 📦 Storage                                              │
╰──────────────────────────────────────────────────────────────────────────────╯
🔗 draw.io: contoso-web.drawio
```

On a real estate it goes several layers deep:

```text
Blast radius: 15 resources (0 external), 14 dependencies
╭────────────────────────────── Dependency Graph ──────────────────────────────╮
│ 🌐 app-spa-frontend                                                          │
│ ├── --calls--> 🌐 app-auth-service                                           │
│ │   ├── --reads-secret--> 🔐 kv-core-prod                                    │
│ │   └── --connects-to--> 🗄️ cosmos-auth                                      │
│ └── --calls--> 🌐 app-api-gateway                                            │
│     ├── --calls--> 📦 capp-payment-service                                   │
│     │   └── --reads-secret--> 🔐 kv-payments-prod                            │
│     ├── --calls--> 🌐 app-inventory-api                                      │
│     │   ├── --connects-to--> 🗄️ pg-inventory-prod                            │
│     │   └── --connects-to--> 📦 stinventoryprod                              │
│     ├── --connects-to--> 🗄️ redis-gateway                                    │
│     └── --calls--> 🌐 app-orders-api                                         │
│         ├── --connects-to--> 🗄️ redis-orders                                 │
│         ├── --connects-to--> 📦 sb-enterprise                                │
│         └── --connects-to--> 🗄️ sql-orders-prod                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

(That is the default **high-level** view - resources grouped by type. Add
`--level detail` to see every instance with its real name.)

### Output formats

| Flag | You get |
|---|---|
| `-o FILE` | **draw.io** diagram with native Azure icons - open it in [draw.io](https://app.diagrams.net), the desktop app or the VS Code extension and edit it like any hand-drawn diagram |
| `--html FILE` | **Interactive viewer**: one self-contained file, no server and no CDN. Dark mode, edges colour-coded by relationship type (security / data / network), resource-group filters and search, SVG + PNG export, and direct links into the Azure portal |
| `--mermaid FILE` | Mermaid source, for embedding in Markdown docs |
| `--json FILE` | The graph itself - this is what `cloudmap ask` reads |
| `--csv FILE` | Flat edge list with evidence, for a spreadsheet or an auditor |

## Interactive wizard

Run it with no arguments and it walks you through the whole thing:

```
cloudmap
```

It asks for a subscription (yours is marked as the default), then a resource
group, then the resource to trace, then how deeply to enrich, and where to write
the results. It reads live Azure, so `az login` first.

## Why

- **Live-cloud tools upload your data.** cloudmap runs locally and reads only
  what you point it at. Nothing leaves your machine.
- **Existing OSS is siloed** - Terraform-only or Kubernetes-only. cloudmap works
  from Azure's own inventory (Resource Graph) and correlates across services.
- **Impact analysis, onboarding, change reviews.** "What breaks if I touch this?"
  in one diagram instead of ten portal blades.

### Three things people use it for

**Is this safe to delete?** An Azure SQL database looks orphaned in the portal and
someone wants it gone to save the monthly bill. `cloudmap trace sql-orders-dev
--direction up` deep-enriches the connection strings of the web apps in the
subscription and shows what still points at it - including, occasionally, a
production app that was never supposed to.

**What is actually broken?** An AKS cluster starts failing at 3am. Tracing it
produces a dependency graph with the Key Vault it reads, and the map carries the
evidence for that edge ("found in Kubernetes secret X"), so the next question -
did anything change on that vault - has a place to start.

**Who has access to this?** An auditor asks which systems can reach the storage
account holding customer data. `cloudmap trace pii-storage --direction up --csv
pii-audit.csv` hands back a spreadsheet of the web apps and clusters with managed
identity RBAC on it, with the role assignments as proof.

## What it maps

App Service / Functions, Container Apps (+ environments), AKS, App Gateway, API
Management, Key Vault, Storage, SQL / PostgreSQL / MySQL / Cosmos, Redis, Service
Bus, Event Hub, Cognitive Search, Azure OpenAI, Container Registry, Log Analytics,
App Insights, VNets, Private Endpoints and managed identities.

## How it works

1. **Ingest** - a JSON fixture (default) or live `az graph query` (opt-in, guarded).
2. **Extract** - per-type rules turn `properties` into typed edges
   (`hosted-on`, `reads-secret`, `private-link-to`, `role: ...`, `routes-to`, ...).
3. **Blast radius** - walk the graph from your seed with *direction consistency*:
   from the seed it goes both ways (what it depends on **and** what depends on
   it), but once it steps in one direction it never reverses. That single rule
   keeps a shared resource (App Service Plan, VNet, Key Vault) from bridging your
   seed to unrelated apps sitting on the same thing.
4. **Render** - draw.io (Azure icons) + Mermaid + JSON + CSV + a self-contained HTML viewer.
5. **Ask** - query the saved map in plain language (`cloudmap ask`); the answers are
   computed from the graph, and a local model may only route the question or narrate
   the result.

## Ask a map questions

```
cloudmap ask <map.json> "<question>"

  --explain      also narrate the answer with a LOCAL model (ollama)
  --llm          if no built-in rule understands the phrasing, let a LOCAL model
                 pick the query (its choice is validated against the map)
  --max-hops N   limit traversal depth
  --json         print the answer as JSON (for scripting)
```

Instance names only exist in a `--level detail` map; the default high-level map
groups by type, so ask it about a group (`"what breaks if I touch Key Vault"`) or
trace with `--level detail` first:

```
$ cloudmap trace contoso-web --from fixtures/contoso.json --level detail --json out.json
$ cloudmap ask out.json "what breaks if I touch contoso-kv"
Query: impact  ·  subject: contoso-kv
2 resource(s) depend on contoso-kv - changing it can break them.

  contoso-web  (Web App)  1 hop(s)
      contoso-web --reads-secret; role: Key Vault Secrets User--> contoso-kv
          proof: app config references host contoso-kv.vault.azure.net; Key Vault
                 reference to vault contoso-kv; RBAC role assignment
  contoso-agw  (App Gateway)  2 hop(s)
      ...
```

Questions it answers: what breaks if I touch X · what does X depend on · how does
X reach Y · what is shared in this map · what should I not trust · explain this map.

**The answers are computed, not generated.** "What breaks if I touch X" is a graph
traversal, so that is how it is answered - the numbers and names come from the
edges. A local model is optional and can only do two things: pick which query an
unusual phrasing meant (`--llm`, and its pick is validated against the map), or put
the already-computed facts into prose (`--explain`). It never supplies a fact, so it
cannot promote a guess to one. Every finding shows the hops behind it and the proof
of each hop, and a finding that leans on a model-proposed edge is marked `[GUESS]`.
If the map itself says it is incomplete, every answer from it repeats that warning.

## Live Azure (opt-in)

```
cloudmap trace my-app --live --allow-live

  --single-sub          query only the active subscription (default: every enabled
                        subscription in the tenant)
  --enrich MODE         which web apps to deep-enrich for the dependencies that only
                        exist in app config. auto (default) = the seed alone when the
                        seed is a web app, every app in scope when it is not;
                        all | seed | none
  --resolve-secrets     read KV secret values in-memory to see through KV-backed
                        connection strings (never printed or written)
  --llm                 let a LOCAL model (ollama) propose extra edges, each
                        verified against scanned resources before it is trusted
```

**Why `--enrich` matters.** A Key Vault reference or a connection string lives in an
app's settings, which Resource Graph does not return - so that edge exists only once
that app has been deep-enriched. Enriching only the seed would make the graph
asymmetric: tracing an app finds the vault it reads, but tracing the vault would never
find the app. Since "what breaks if I touch this" is usually asked about shared
infrastructure, `auto` enriches every app in scope whenever the seed is *not* an app.
Anything left un-enriched is reported as a **blind spot** on the map and repeated by
every `ask` answer drawn from it, so an empty result never passes for "nothing depends
on this".

Live mode is opt-in, not sandboxed: `--allow-live` is the deliberate switch, and
cloudmap reads whatever subscription `az` is pointed at. The read is read-only, but
it is a read of live infrastructure, so point it on purpose. As an optional guard
against a stale `az` context silently redirecting a scan, pin the subscription you
mean - if set, cloudmap refuses to run against any other active subscription:

```
export CLOUDMAP_ALLOW_SUBSCRIPTION=<subscription-id>   # optional
```

If a live read fails (e.g. missing RBAC), cloudmap **reports the gap** instead of
silently dropping edges, and warns when a scan is truncated - so you know when the
picture is incomplete. Fixtures are always the default.
**Do not point this at data you are not authorized to read.**

## Capture a real export (so the tests can be wrong)

A fixture you wrote yourself can only confirm what you already believe. `capture`
saves what Azure actually returned, scrubs it, and gives you something that can
contradict the extractors.

```
cloudmap capture --allow-live --single-sub -o fixtures/captured_real.json
cloudmap scrub  raw-export.json -o fixtures/captured_real.json   # for a file you already have
```

The scrub is a **global, consistent** pseudonymisation, not a field-by-field
blanking: `kv-payments` becomes `kv-1` everywhere at once, so the app setting that
references `kv-payments.vault.azure.net` still points at the same vault
afterwards. What survives on purpose: service domain suffixes (the rules read them
to decide what an edge means), built-in role GUIDs (public Azure constants) and
private IP ranges (that is the topology). What does not: names, resource groups,
subscription and principal GUIDs, e-mails, public IPs, and any
password / key / SAS fragment, which is redacted rather than renamed.

The mapping is never written to disk - it is the re-identification key. Counts are
printed, the mapping is not. **A scrubber is not a proof: read the file before you
commit it.** `--no-scrub` exists for local debugging and writes credentials to
disk; keep those files named `*.live.json` so `.gitignore` catches them.

## Usage reference

```
cloudmap trace <name> (--from <fixture.json> | --live) [options]

  --level high|detail        high = architecture view grouped by type (default);
                             one box per type, so instance names are not in the map
                             detail = every instance with its real name
  --direction both|down|up   both = full blast radius (default)
                             down = only what it depends on
                             up   = only what depends on it
  --max-hops N               limit traversal depth
  -o FILE                    draw.io output (default: <name>.blast.drawio)
  --mermaid FILE             also write Mermaid
  --json FILE                also write the graph as JSON
  --html FILE                also write a self-contained interactive HTML viewer
  --csv FILE                 also write the edge list as CSV
  -d, --out-dir DIR          write every artifact into DIR, named after the seed
```

Other subcommands: `cloudmap capture`, `cloudmap scrub`, `cloudmap ask` (see above).

## Roadmap

- Terraform state ingestor + drift overlay (desired vs actual).
- Deeper AKS / Kubernetes workload correlation.
- Resource-group and application (tag-based) seeds.
- More edge extractors and Azure icon mappings (Container Apps still render as a
  labelled box - an icon is only added once its azure2 asset path is verified).

## License

MIT
