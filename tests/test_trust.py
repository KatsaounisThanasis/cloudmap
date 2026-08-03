"""Trust-invariant tests: the properties that make cloudmap's output believable.

These lock the behaviors the tool sells: the seed never silently loses a
reference (unresolved -> explicit external node), the high-level view groups by
type, and a live read that fails is reported rather than swallowed.
"""

import json

from cloudmap.extract.extractors import Resolver, extract_edges, merge_model_edges, seed_external_dependencies
from cloudmap.graph import collapse_high_level
from cloudmap.ingest.azure import enrich_webapp
from cloudmap.model import Edge, Graph, Node
from cloudmap.render.json_out import to_json


def _web(appsettings):
    return Node(id="/sub/web", name="web", type="microsoft.web/sites",
                raw={"properties": {"siteConfig": {"appSettings": appsettings}}})


def test_seed_unresolved_ref_becomes_external_node():
    # App config points at a SQL host that is NOT in the scanned set: it must
    # surface as an explicit external node, never be dropped.
    seed = _web([{"name": "DB", "value": "Server=ghost.database.windows.net;Database=x"}])
    resolver = Resolver({seed.id: seed})            # nothing else in scope
    ext_nodes, edges = seed_external_dependencies(seed, resolver)

    assert any(n.external and n.name == "ghost.database.windows.net" for n in ext_nodes)
    assert any(e.target == "external://ghost.database.windows.net" and e.kind == "connects-to"
               for e in edges)
    assert all(n.note for n in ext_nodes)           # every external node says WHY


def test_seed_resolved_ref_is_not_external():
    # Same host, but this time it IS a scanned resource -> no external node.
    sql = Node(id="/sub/sql", name="ghost", type="microsoft.sql/servers",
               raw={"properties": {"fullyQualifiedDomainName": "ghost.database.windows.net"}})
    seed = _web([{"name": "DB", "value": "Server=ghost.database.windows.net;Database=x"}])
    resolver = Resolver({seed.id: seed, sql.id: sql})
    ext_nodes, _edges = seed_external_dependencies(seed, resolver)

    assert ext_nodes == []                          # resolved -> handled by extract_edges


def test_high_level_collapses_instances_by_type():
    seed = Node(id="/web", name="web", type="microsoft.web/sites")
    s1 = Node(id="/st1", name="stg1", type="microsoft.storage/storageaccounts")
    s2 = Node(id="/st2", name="stg2", type="microsoft.storage/storageaccounts")
    g = Graph(
        nodes={n.id: n for n in (seed, s1, s2)},
        edges=[Edge(seed.id, s1.id, "connects-to"), Edge(seed.id, s2.id, "connects-to")],
        distances={seed.id: 0, s1.id: 1, s2.id: 1},
    )
    c = collapse_high_level(g, seed.id)
    labels = {n.name for n in c.nodes.values()}

    assert "web" in labels                          # seed keeps its real name
    assert "Storage \u00d72" in labels              # two accounts -> one box, counted
    assert len(c.nodes) == 2                         # seed + one storage group


def test_high_level_keeps_the_proof_behind_a_grouped_arrow():
    # --level high is the DEFAULT, so if grouping dropped evidence then the map
    # everybody actually produces (and feeds to `ask`) would carry no proof at all.
    seed = Node(id="/web", name="web", type="microsoft.web/sites")
    s1 = Node(id="/st1", name="stg1", type="microsoft.storage/storageaccounts")
    s2 = Node(id="/st2", name="stg2", type="microsoft.storage/storageaccounts")
    g = Graph(
        nodes={n.id: n for n in (seed, s1, s2)},
        edges=[Edge(seed.id, s1.id, "connects-to", evidence="app setting BLOB_URL"),
               Edge(seed.id, s2.id, "connects-to", evidence="connection string Archive")],
        distances={seed.id: 0, s1.id: 1, s2.id: 1},
    )
    c = collapse_high_level(g, seed.id)

    assert len(c.edges) == 1                                 # one grouped arrow
    assert "app setting BLOB_URL" in c.edges[0].evidence     # both members' proofs
    assert "connection string Archive" in c.edges[0].evidence
    assert c.edges[0].kind == "connects-to"                  # kind deduped, not doubled


def test_high_level_keeps_why_an_external_group_is_unverified():
    seed = Node(id="/web", name="web", type="microsoft.web/sites")
    e1 = Node(id="external://a.vault.azure.net", name="a.vault.azure.net",
              type="external/Key Vault", external=True, note="referenced, not in scope")
    g = Graph(nodes={n.id: n for n in (seed, e1)},
              edges=[Edge(seed.id, e1.id, "reads-secret", evidence="KV reference")],
              distances={seed.id: 0, e1.id: 1})
    c = collapse_high_level(g, seed.id)
    ext = [n for n in c.nodes.values() if n.external]

    assert ext and ext[0].note == "referenced, not in scope"


def test_enrich_webapp_reports_gap_instead_of_swallowing():
    # No name/resourceGroup -> it cannot enrich; it must SAY so, not return clean.
    r = enrich_webapp({"name": "", "resourceGroup": ""})
    assert r["errors"], "an unenrichable seed must report a read gap"


def test_host_match_respects_label_boundary():
    # Seed calls myapi.*; there is also a separate app api.* whose host is a
    # suffix of it. The seed must edge to myapi only, not to api.
    caller = _web([{"name": "CB", "value": "https://myapi.azurewebsites.net/cb"}])
    api = Node(id="/api", name="api", type="microsoft.web/sites",
               raw={"properties": {"defaultHostName": "api.azurewebsites.net"}})
    myapi = Node(id="/myapi", name="myapi", type="microsoft.web/sites",
                 raw={"properties": {"defaultHostName": "myapi.azurewebsites.net"}})
    edges = extract_edges({caller.id: caller, api.id: api, myapi.id: myapi})
    tgts = {(e.source, e.target) for e in edges}

    assert (caller.id, myapi.id) in tgts        # the app it really calls
    assert (caller.id, api.id) not in tgts       # NOT the suffix-collision app


def test_json_meta_carries_incompleteness():
    seed = Node(id="/web", name="web", type="microsoft.web/sites")
    ext = Node(id="external://x.vault.azure.net", name="x.vault.azure.net",
               type="external/Key Vault", external=True, note="not in scope")
    g = Graph(nodes={seed.id: seed, ext.id: ext},
              edges=[Edge(seed.id, ext.id, "reads-secret")],
              distances={seed.id: 0, ext.id: 1})
    doc = json.loads(to_json(g, seed.id, meta={
        "truncated": True, "read_gaps": ["RBAC (az role assignment list): denied"]}))

    assert doc["meta"]["complete"] is False      # the artifact admits it is partial
    assert doc["meta"]["truncated"] is True
    assert doc["meta"]["external_unverified"] == 1
    assert doc["meta"]["read_gaps"]


def test_json_meta_complete_when_clean():
    seed = Node(id="/web", name="web", type="microsoft.web/sites")
    g = Graph(nodes={seed.id: seed}, edges=[], distances={seed.id: 0})
    doc = json.loads(to_json(g, seed.id))

    assert doc["meta"]["complete"] is True
    assert doc["meta"]["external_unverified"] == 0


def test_model_edge_never_overrides_deterministic():
    # This is the exact bug the --llm trial surfaced: the model proposed a wrong
    # RELATIONSHIP (uses-workspace) onto a real target that a rule already linked.
    det = Edge("a", "b", "connects-to", origin="extracted", evidence="app config")
    mod = Edge("a", "b", "uses-workspace", origin="model")
    out = merge_model_edges([det], [mod])

    assert len(out) == 1
    assert out[0].origin == "extracted"                 # deterministic wins
    assert "uses-workspace" not in out[0].kind          # the guess is rejected, not merged


def test_model_edge_may_add_a_new_target():
    det = Edge("a", "b", "connects-to", origin="extracted")
    mod = Edge("a", "c", "connects-to", origin="model")
    out = merge_model_edges([det], [mod])
    origin = {(e.source, e.target): e.origin for e in out}

    assert origin[("a", "b")] == "extracted"
    assert origin[("a", "c")] == "model"                # model contributes new coverage


def test_deterministic_edges_carry_origin_and_evidence():
    seed = _web([{"name": "DB",
                  "value": "Host=db1.postgres.database.azure.com;Database=x"}])
    pg = Node(id="/pg", name="db1", type="microsoft.dbforpostgresql/flexibleservers",
              raw={"properties": {"fullyQualifiedDomainName": "db1.postgres.database.azure.com"}})
    edges = extract_edges({seed.id: seed, pg.id: pg})

    assert edges and all(e.origin == "extracted" and e.evidence for e in edges)


def test_builtin_role_guids_are_named_not_shown_as_custom():
    # Resource Graph often omits roleDefinitionName, leaving only the GUID. A
    # built-in role must still be NAMED - an unnamed "custom role" edge hides
    # whether the access is an observer (Reader) or an operator (Admin). These
    # GUIDs are global Azure constants, identical in every tenant.
    from cloudmap.extract.extractors import ROLE_NAMES

    ra = {
        "id": "/ra1", "name": "ra1", "type": "microsoft.authorization/roleassignments",
        "properties": {
            "principalId": "P-OBS", "scope": "/aks",
            # AKS RBAC Reader - a built-in GUID, no roleDefinitionName supplied
            "roleDefinitionId": "/.../providers/Microsoft.Authorization/roleDefinitions/"
                                "7f6c6a51-bcf8-42ba-9220-52d62157d7db",
        },
    }
    from cloudmap.graph import build_graph
    edges = build_graph([
        {"id": "/aks", "name": "aks", "type": "microsoft.containerservice/managedclusters"},
        {"id": "/watcher", "name": "watcher", "type": "microsoft.web/sites",
         "identity": {"principalId": "P-OBS"}},
        ra,
    ]).edges
    kinds = [e.kind for e in edges if e.source == "/watcher" and e.target == "/aks"]

    assert kinds == ["role: Azure Kubernetes Service RBAC Reader"]
    assert "custom role" not in kinds[0]
    assert "7f6c6a51-bcf8-42ba-9220-52d62157d7db" in ROLE_NAMES


def test_an_unknown_guid_still_falls_through_to_custom_role():
    # A genuinely tenant-defined role we cannot name must stay honest, not guess.
    from cloudmap.graph import build_graph

    ra = {
        "id": "/ra2", "name": "ra2", "type": "microsoft.authorization/roleassignments",
        "properties": {"principalId": "P-X", "scope": "/kv",
                       "roleDefinitionId": "/.../roleDefinitions/"
                                           "00000000-dead-beef-0000-000000000000"},
    }
    edges = build_graph([
        {"id": "/kv", "name": "kv", "type": "microsoft.keyvault/vaults"},
        {"id": "/app", "name": "app", "type": "microsoft.web/sites",
         "identity": {"principalId": "P-X"}},
        ra,
    ]).edges
    kinds = [e.kind for e in edges if e.source == "/app" and e.target == "/kv"]

    assert kinds == ["role: custom role"]


def test_ai_platform_builtin_roles_are_named():
    # These were the bulk of the "custom role" edges on an AI platform's OpenAI
    # and Search resources; they are public built-in GUIDs, so name them.
    from cloudmap.extract.extractors import ROLE_NAMES

    for guid, name in [
        ("a97b65f3-24c7-4388-baec-2e87135dc908", "Cognitive Services User"),
        ("5e0bd9bd-7b93-4f28-af87-19fc36ad61bd", "Cognitive Services OpenAI User"),
        ("a001fd3d-188f-4b5d-821b-7da978bf7442", "Cognitive Services OpenAI Contributor"),
        ("7ca78c08-252a-4471-8644-bb5ff32d4ba0", "Search Service Contributor"),
    ]:
        assert ROLE_NAMES.get(guid) == name


def test_ml_workspace_maps_its_backing_platform():
    # An ML workspace / AI Hub is built on a store, a vault, telemetry and an
    # image registry - ARM ids in its properties. Before this, the type was not
    # even scanned, so a workspace could not be traced at all.
    from cloudmap.graph import build_graph

    S = "/subscriptions/s/resourceGroups/rg/providers"
    ws = {"id": f"{S}/Microsoft.MachineLearningServices/workspaces/ws", "name": "ws",
          "type": "microsoft.machinelearningservices/workspaces",
          "properties": {
              "storageAccount": f"{S}/Microsoft.Storage/storageAccounts/st",
              "keyVault": f"{S}/Microsoft.KeyVault/vaults/kv",
              "applicationInsights": f"{S}/Microsoft.Insights/components/ai",
              "containerRegistry": f"{S}/Microsoft.ContainerRegistry/registries/acr"}}
    backing = [
        {"id": f"{S}/Microsoft.Storage/storageAccounts/st", "name": "st",
         "type": "microsoft.storage/storageaccounts"},
        {"id": f"{S}/Microsoft.KeyVault/vaults/kv", "name": "kv",
         "type": "microsoft.keyvault/vaults"},
        {"id": f"{S}/Microsoft.Insights/components/ai", "name": "ai",
         "type": "microsoft.insights/components"},
        {"id": f"{S}/Microsoft.ContainerRegistry/registries/acr", "name": "acr",
         "type": "microsoft.containerregistry/registries"},
    ]
    edges = build_graph([ws, *backing]).edges
    kinds = {edg.target.rsplit("/", 1)[-1]: edg.kind for edg in edges if edg.source == ws["id"]}

    assert kinds == {"st": "connects-to", "kv": "reads-secret",
                     "ai": "sends-telemetry", "acr": "pulls-image"}
    assert all(e.evidence for e in edges)


def test_generic_arm_reference_maps_any_type():
    # A type with no hand-written rule still names other resources by ARM id deep
    # in its properties; those become deterministic 'references' edges with the
    # property path as proof. This is what lets cloudmap map anything.
    from cloudmap.graph import build_graph

    S = "/subscriptions/s/resourceGroups/rg/providers"
    thing = {"id": f"{S}/Microsoft.Some/thing/x", "name": "x", "type": "microsoft.some/thing",
             "properties": {"deep": {"nested": {"targetId": f"{S}/Microsoft.KeyVault/vaults/kv"}}}}
    kv = {"id": f"{S}/Microsoft.KeyVault/vaults/kv", "name": "kv", "type": "microsoft.keyvault/vaults"}
    edges = [e for e in build_graph([thing, kv]).edges
             if e.source == thing["id"] and e.target == kv["id"]]

    assert edges and edges[0].kind == "references"
    assert edges[0].origin == "extracted"          # deterministic: the id is really there
    assert "targetId" in edges[0].evidence          # the property path is the proof


def test_generic_pass_does_not_shadow_a_semantic_edge():
    # A rule that already named the pair (hosted-on) wins; the generic pass must
    # not append a redundant 'references'.
    from cloudmap.graph import build_graph

    S = "/subscriptions/s/resourceGroups/rg/providers"
    web = {"id": f"{S}/Microsoft.Web/sites/w", "name": "w", "type": "microsoft.web/sites",
           "properties": {"serverFarmId": f"{S}/Microsoft.Web/serverfarms/p"}}
    plan = {"id": f"{S}/Microsoft.Web/serverfarms/p", "name": "p",
            "type": "microsoft.web/serverfarms"}
    kinds = [e.kind for e in build_graph([web, plan]).edges
             if e.source == web["id"] and e.target == plan["id"]]

    assert kinds == ["hosted-on"]                   # not "hosted-on; references"


def test_monitoring_observers_are_not_generic_dependencies():
    # A Prometheus rule group / dashboard points at what it watches; that is an
    # observer, not a dependency, and must not appear as a generic edge on a
    # blast-radius map (same principle as a read-only RBAC role).
    from cloudmap.graph import build_graph

    S = "/subscriptions/s/resourceGroups/rg/providers"
    aks = {"id": f"{S}/Microsoft.ContainerService/managedClusters/c", "name": "c",
           "type": "microsoft.containerservice/managedclusters"}
    rule = {"id": f"{S}/Microsoft.AlertsManagement/prometheusRuleGroups/r", "name": "r",
            "type": "microsoft.alertsmanagement/prometheusrulegroups",
            "properties": {"scopes": [f"{S}/Microsoft.ContainerService/managedClusters/c"]}}
    edges = build_graph([aks, rule]).edges

    assert not any(e.source == rule["id"] for e in edges)   # observer contributes no edge
