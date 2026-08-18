# cloudmap

Give it the name of one Azure resource. Get back its **full dependency graph**
(the blast radius) as an **editable draw.io diagram with native Azure icons** -
plus Mermaid and JSON.

Azure Resource Graph has no "dependencies" table. The relationships that matter
- what an App Service is hosted on, which Key Vault it reads, which subnet it
integrates with, which managed identity has which role where, what a Private
Endpoint fronts, which App Gateway routes to it - are buried inside each
resource's `properties`. cloudmap reads them out, correlates them into one
graph, and draws it.

```
cloudmap trace contoso-web --from fixtures/contoso.json -o contoso-web.drawio
```

```
Seed: contoso-web (microsoft.web/sites)
Blast radius: 9 resources (0 external/unverified), 9 dependencies
draw.io: contoso-web.drawio

  contoso-web  --hosted-on-->  App Service Plan
  contoso-web  --vnet-integration-->  Virtual Network
  contoso-web  --reads-secret; role: Key Vault Secrets User-->  Key Vault
  contoso-web  --connects-to-->  SQL Server
  contoso-web  --connects-to-->  Storage
  contoso-web  --sends-telemetry-->  App Insights
  App Insights  --uses-workspace-->  Log Analytics
  App Gateway  --in-subnet-->  Virtual Network
  App Gateway  --routes-to-->  contoso-web
```

(That is the default **high-level** view - resources grouped by type. Add
`--level detail` to see every instance with its real name.)

Open the `.drawio` file in [draw.io](https://app.diagrams.net) / the desktop app
/ the VS Code extension and edit it like any hand-drawn diagram - or open the
self-contained `--html` viewer straight from disk (no server, no install) and
click a resource to focus its blast radius:

![The interactive HTML viewer: the seed at the centre, dependencies fanned out with native Azure icons, each edge carrying its relationship and proof](estate-viewer.png)

## Why

- **Live-cloud tools upload your data.** cloudmap runs locally and reads only
  what you point it at. Nothing leaves your machine.
- **Existing OSS is siloed** - Terraform-only or Kubernetes-only. cloudmap works
  from Azure's own inventory (Resource Graph) and correlates across services.
- **Impact analysis, onboarding, change reviews.** "What breaks if I touch this?"
  in one diagram instead of ten portal blades.

## Real-World Scenarios (Why you need this)

### 1. The "Safe to Delete?" Scenario (FinOps / Cloud Cost Cleanup)
A developer spots an expensive Azure SQL Database (`sql-orders-dev`) that looks orphaned in the portal. They want to delete it to save $500/month. 
By running `cloudmap trace sql-orders-dev --direction up`, the tool deep-enriches the connection strings of all Web Apps in the subscription. The map instantly reveals that a *production* Web App is mistakenly pointing to this dev database! The engineer just avoided a catastrophic outage.

### 2. The "Incident Response / Root Cause" Scenario (SRE)
At 3:00 AM, alerts fire because `payment-api` (an AKS cluster) is failing. The team is blind.
Running the CloudMap interactive wizard on `payment-api` generates an HTML dependency graph in 5 seconds. It shows the cluster depends on a Key Vault (`kv-pay`). Checking the vault reveals someone changed its firewall rules 10 minutes ago. Cloudmap provides the exact *Evidence* ("found in Kubernetes secret X"), identifying the root cause instantly.

### 3. The "Compliance & Auditor Review" Scenario (Security)
An auditor asks: *"Which systems have access to the Storage Account containing PII customer data?"*
Instead of manually clicking through 50 IAM screens in the Azure Portal, you run `cloudmap trace pii-storage --direction up --csv pii-audit.csv`. In seconds, you hand the auditor a clean spreadsheet showing exactly which Web Apps and AKS clusters have Managed Identity RBAC access to the storage, complete with the exact Role Assignments as proof.

## How it works

1. **Ingest** - a JSON fixture (default) or live `az graph query` (opt-in, guarded).
2. **Extract** - per-type rules turn `properties` into typed edges
   (`hosted-on`, `reads-secret`, `private-link-to`, `role: ...`, `routes-to`, ...).
3. **Blast radius** - walk the graph from your seed with *direction consistency*:
   from the seed it goes both ways (what it depends on **and** what depends on
   it), but once it steps in one direction it never reverses. That single rule
   keeps a shared resource (App Service Plan, VNet, Key Vault) from bridging your
   seed to unrelated apps sitting on the same thing.
4. **Render** - draw.io (Azure icons) + Mermaid + JSON + a self-contained HTML viewer.
5. **Ask** - query the saved map in plain language (`cloudmap ask`); the answers are
   computed from the graph, and a local model may only route the question or narrate
   the result.

## Usage

```
cloudmap trace <name> --from <fixture.json> [options]

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
```

Try it with the bundled synthetic estate:

```
cloudmap trace contoso-web --from fixtures/contoso.json --mermaid out.mmd --json out.json
```

### Ask a map questions

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

### Capture a real export (so the tests can be wrong)

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

### Live Azure (opt-in)

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

## Install

```
git clone https://github.com/KatsaounisThanasis/cloudmap && cd cloudmap
pip install -e .          # or just: python -m cloudmap trace ...
```

No third-party dependencies - Python 3.9+ standard library only.

Supported today: App Service / Functions, Container Apps (+ environments), AKS,
App Gateway, API Management, Key Vault, Storage, SQL / PostgreSQL / MySQL / Cosmos,
Redis, Service Bus, Event Hub, Cognitive Search, Azure OpenAI, Container Registry,
Log Analytics, App Insights, VNets, Private Endpoints and managed identities.

## Roadmap

- Slice 2: Terraform state ingestor + drift overlay (desired vs actual).
- Slice 3: AKS / Kubernetes workload correlation.
- Resource-group and application (tag-based) seeds.
- More edge extractors and Azure icon mappings (Container Apps still render as a
  labelled box - an icon is only added once its azure2 asset path is verified).

## License

MIT
