"""Container Apps coverage.

A Container App keeps the same dependencies a web app keeps, in different places:
an environment instead of a plan, `configuration.registries` instead of a docker
image string, `template.containers[].env` instead of appSettings. These tests
lock each of those readings, plus the one thing that is genuinely different - a
Container Apps environment names its Log Analytics workspace by customerId rather
than by resource id, so resolving it needs its own index.
"""

from cloudmap.extract.extractors import Resolver, extract_edges, seed_external_dependencies
from cloudmap.graph import build_graph, friendly_type
from cloudmap.model import Node

LAW_GUID = "9c1e7a54-0f2b-4c33-8d61-77a2b0d5e412"


def _estate():
    """A container app on an environment, pulling from ACR, reading a vault,
    talking to a database, with the environment wired to a subnet and a
    workspace."""
    return [
        {"id": "/sub/rg/law", "name": "law-1", "type": "microsoft.operationalinsights/workspaces",
         "properties": {"customerId": LAW_GUID}},
        {"id": "/sub/rg/vnet", "name": "vnet-1", "type": "microsoft.network/virtualnetworks",
         "properties": {}},
        {"id": "/sub/rg/acr", "name": "acr1", "type": "microsoft.containerregistry/registries",
         "properties": {"loginServer": "acr1.azurecr.io"}},
        {"id": "/sub/rg/kv", "name": "kv-1", "type": "microsoft.keyvault/vaults",
         "properties": {"vaultUri": "https://kv-1.vault.azure.net/"}},
        {"id": "/sub/rg/pg", "name": "pg-1",
         "type": "microsoft.dbforpostgresql/flexibleservers",
         "properties": {"fullyQualifiedDomainName": "pg-1.postgres.database.azure.com"}},
        {"id": "/sub/rg/cae", "name": "cae-1", "type": "microsoft.app/managedenvironments",
         "properties": {
             "vnetConfiguration": {"infrastructureSubnetId": "/sub/rg/vnet/subnets/infra"},
             "appLogsConfiguration": {"logAnalyticsConfiguration": {"customerId": LAW_GUID}}}},
        {"id": "/sub/rg/ca", "name": "ca-1", "type": "microsoft.app/containerapps",
         "properties": {
             "environmentId": "/sub/rg/cae",
             "configuration": {
                 "ingress": {"fqdn": "ca-1.happyfield-1234.westeurope.azurecontainerapps.io"},
                 "registries": [{"server": "acr1.azurecr.io"}],
                 "secrets": [{"name": "db", "keyVaultUrl":
                              "https://kv-1.vault.azure.net/secrets/db-password"}]},
             "template": {"containers": [{
                 "image": "acr1.azurecr.io/orders:1.4.2",
                 "env": [
                     {"name": "PG", "value":
                      "Host=pg-1.postgres.database.azure.com;Database=orders"},
                     {"name": "SECRET", "secretRef": "db"}]}]}}},
    ]


def _edges():
    g = build_graph(_estate())
    return {(g.nodes[e.source].name, g.nodes[e.target].name): e for e in g.edges}


def test_container_app_is_hosted_on_its_environment():
    e = _edges()[("ca-1", "cae-1")]

    assert e.kind == "hosted-on"
    assert "environmentId" in e.evidence


def test_container_app_pulls_from_its_registry():
    e = _edges()[("ca-1", "acr1")]

    assert "pulls-image" in e.kind
    # both readings agree, and both proofs are kept
    assert "registries[].server" in e.evidence
    assert "containers[].image" in e.evidence


def test_container_app_reads_the_vault_behind_its_secret():
    e = _edges()[("ca-1", "kv-1")]

    assert "reads-secret" in e.kind
    assert "keyVaultUrl" in e.evidence


def test_env_vars_are_read_the_way_app_settings_are():
    e = _edges()[("ca-1", "pg-1")]

    assert e.kind == "connects-to"
    assert "container env" in e.evidence


def test_environment_resolves_its_subnet_and_its_workspace_by_customer_id():
    e = _edges()
    assert e[("cae-1", "vnet-1")].kind == "vnet-integration"
    # the interesting one: no resource id anywhere, only the workspace GUID
    assert e[("cae-1", "law-1")].kind == "uses-workspace"
    assert "customerId" in e[("cae-1", "law-1")].evidence


def test_every_container_app_edge_carries_its_proof():
    g = build_graph(_estate())
    for edge in g.edges:
        assert edge.origin == "extracted"
        assert edge.evidence


def test_ingress_fqdn_makes_the_app_a_resolvable_target():
    # Another workload calling the container app by hostname must resolve to it,
    # exactly as it would for a web app's defaultHostName.
    estate = _estate() + [{
        "id": "/sub/rg/web", "name": "web-1", "type": "microsoft.web/sites",
        "properties": {"defaultHostName": "web-1.azurewebsites.net", "siteConfig": {
            "appSettings": [{"name": "ORDERS_API", "value":
                             "https://ca-1.happyfield-1234.westeurope.azurecontainerapps.io/"}]}}}]
    g = build_graph(estate)
    pairs = {(g.nodes[e.source].name, g.nodes[e.target].name): e.kind for e in g.edges}

    assert pairs[("web-1", "ca-1")] == "calls"


def test_unresolved_container_app_reference_becomes_external():
    g = build_graph(_estate())
    seed = g.nodes["/sub/rg/ca"]
    # a vault the scan never saw, named only in an env var
    seed.raw["properties"]["template"]["containers"][0]["env"].append(
        {"name": "OTHER", "value": "https://ghost-kv.vault.azure.net/secrets/x"})
    ext_nodes, edges = seed_external_dependencies(seed, Resolver(g.nodes))

    assert any(n.external and n.name == "ghost-kv.vault.azure.net" for n in ext_nodes)
    assert all(n.note for n in ext_nodes)
    assert edges


def test_the_types_have_readable_names_in_the_high_level_view():
    assert friendly_type("microsoft.app/containerapps") == "Container App"
    assert friendly_type("microsoft.app/managedenvironments") == "Container Apps Environment"


def test_live_scan_is_not_type_filtered():
    # "Map anything" means the live scan carries no type allowlist - every type
    # (Container Apps included) is scanned, so any resource can be a seed or the
    # target of a reference.
    from cloudmap.ingest.azure import RESOURCES_KQL

    assert "where type" not in RESOURCES_KQL
    assert RESOURCES_KQL.strip().startswith("resources | project") \
        or RESOURCES_KQL.strip().startswith("resources |project") \
        or "resources " in RESOURCES_KQL


def test_extract_edges_is_unchanged_for_estates_without_container_apps():
    # The refactor that made the config rules shared must not have moved anything
    # for web apps: same edges, same evidence wording.
    web = Node(id="/web", name="web", type="microsoft.web/sites",
               raw={"properties": {"siteConfig": {"appSettings": [
                   {"name": "KV", "value": "https://kv-1.vault.azure.net/"}]}}})
    kv = Node(id="/kv", name="kv-1", type="microsoft.keyvault/vaults",
              raw={"properties": {"vaultUri": "https://kv-1.vault.azure.net/"}})
    edges = extract_edges({web.id: web, kv.id: kv})

    assert len(edges) == 1
    assert edges[0].kind == "reads-secret"
    assert edges[0].evidence.startswith("app config references host")
