# The neutral graph format

cloudmap's core does not know about any specific cloud. Everything flows through
one **neutral** shape: `Node` / `Edge` / `Graph` (see `cloudmap/model.py`). An
*adapter* turns a raw cloud export into this shape; every renderer reads it.

```
raw export  --(adapter)-->  neutral Graph  --(renderer)-->  drawio | mermaid | json
```

Adding a cloud = adding one adapter. That is the whole anti-vendor-lock story:
the value (graph, blast-radius, trust, AI) lives in the cloud-agnostic core.

## Inputs cloudmap can read (`--from`, auto-detected)

The loader (`cloudmap/adapters/load_graph`) sniffs the file - no flags:

1. **Raw Azure Resource Graph** - a JSON list of resources, or the `az`
   shape `{"data": [ ... ]}`. Each item has `id`, `type`, `properties`, etc.
   Handled by the **Azure adapter**, which runs the extraction rules.
2. **A neutral cloudmap graph** - a file cloudmap itself wrote with `--json`
   (has top-level `nodes` and `edges`). Loaded straight back into a `Graph`, no
   re-extraction. This is what makes a saved map re-openable (the viewer and
   `cloudmap ask` build on it).

`seed` and `meta` survive the round-trip too (`Graph.meta`), which is what lets a
question answered from a saved map carry the same caveats the map itself carries -
an incomplete artifact cannot quietly become a confident answer.

## Output shape (`--json`)

```jsonc
{
  "seed": "<node id the trace started from>",
  "meta": {
    "complete": true,            // false if truncated OR a live read failed
    "truncated": false,          // scan hit the pagination cap -> missing data
    "read_gaps": [],             // human-readable list of things we could not read
    "external_unverified": 0,    // nodes referenced but not found in scanned scope
    "model_edges": 0             // edges proposed by the LLM (guesses), not rules
  },
  "nodes": [
    {
      "id": "<stable id>",       // ARM id for Azure; group key at high level
      "name": "webapp-orders-dev",
      "type": "microsoft.web/sites",
      "resourceGroup": "rg-orders-dev",
      "location": "westeurope",
      "hops": 0,                 // distance from the seed
      "external": false,         // true = referenced but unverified (dashed box)
      "note": ""                 // why it is external / how it was discovered
    }
  ],
  "edges": [
    {
      "source": "<node id>",
      "target": "<node id>",
      "kind": "hosted-on",       // relationship label(s), "; "-joined if merged
      "origin": "extracted",     // "extracted" = verified by a rule | "model" = LLM guess
      "evidence": "properties.serverFarmId"   // the proof behind this edge
    }
  ]
}
```

## Trust fields (the point of the format)

- **`origin`** - the single most important field. `extracted` means a
  deterministic rule found concrete proof. `model` means the local LLM proposed
  it; renderers draw it dashed so a guess never looks like a fact. A model edge
  can only ADD a new target - it never overrides an extracted edge.
- **`evidence`** - *why* the edge exists (which property / setting / rule). Lets a
  reviewer audit the map instead of trusting it.
- **`meta.complete`** - the artifact admits when it is partial (truncated scan or
  a read that failed for lack of permission).

## The adapter contract

An adapter is anything that turns raw input into a `Graph`:

```python
raw (list | dict)  ->  cloudmap.model.Graph
```

- `adapters/` — `AzureAdapter` (raw Azure Resource Graph → Graph, via the
  extraction rules) and the neutral loader (cloudmap graph JSON → Graph).
- The Azure specifics live in `ingest/azure.py` (live pull) and
  `extract/extractors.py` (property rules). A future `TerraformAdapter` /
  `AwsAdapter` slots in beside them without touching the core.
