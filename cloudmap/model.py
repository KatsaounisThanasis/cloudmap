"""Core data model: the unified Node / Edge / Graph that every ingestor feeds
and every renderer reads. Deliberately provider-neutral so a future GCP/AWS
ingestor could reuse it unchanged."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Node:
    id: str                 # ARM resource id (original casing kept for display)
    name: str
    type: str               # lowercased ARM type, e.g. microsoft.web/sites
    resource_group: str = ""
    subscription: str = ""
    location: str = ""
    kind: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)   # original resource (properties, identity)
    external: bool = False   # referenced but not verified as an ARM resource in scope
    note: str = ""           # how it was discovered / why it's external


@dataclass
class Edge:
    source: str             # node id
    target: str             # node id
    kind: str               # relationship label, e.g. hosted-on, reads-secret
    origin: str = "extracted"  # "extracted" = a deterministic rule found proof;
                               # "model" = proposed by the LLM (a guess, marked as such)
    evidence: str = ""      # WHY this edge exists: the property/setting/rule behind it
    detail: str = ""


@dataclass
class Graph:
    nodes: dict[str, Node]             # id -> Node
    edges: list[Edge]                  # list[Edge]
    distances: dict[str, int] = field(default_factory=dict)   # id -> hop distance from seed
    # provenance of the map ITSELF (seed, complete, truncated, read_gaps) as written
    # to the JSON artifact. Carried so a reloaded map stays honest about its limits:
    # a question answered from an incomplete map gets the same warning the map has.
    meta: dict[str, Any] = field(default_factory=dict)
