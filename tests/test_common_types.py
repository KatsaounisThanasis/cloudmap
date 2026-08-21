"""The types people actually trace, wired the way Azure actually wires them.

The catalogue sweep in test_type_agnostic.py proves the MECHANISMS do not care
about the type, but it feeds them a stub property. This file is the other half:
a realistic estate built from the resource types that dominate a real enterprise
subscription, each one carrying the property names Azure genuinely returns
(`serverFarmId`, `fullyQualifiedDomainName`, `identityProfile.kubeletidentity`,
`managedBy`, `identity.userAssignedIdentities`, ...).

Type selection is empirical rather than a guess: the shape below mirrors the
top-40 type distribution of a real enterprise tenant (web apps, storage, redis,
Cognitive Services, SQL databases, App Insights, NSGs, managed identities, key
vaults, VNets, plans, ML workspaces, VMs, registries) crossed with the services
industry surveys report as most used (VMs, Cosmos DB, SQL, Kubernetes,
Functions, storage).

Everything here is synthetic. No tenant data, no real names.
"""

import pytest

from cloudmap.graph import blast_radius, build_graph, find_seeds

S = "/subscriptions/11111111-1111-1111-1111-111111111111/resourcegroups/rg-app/providers"


def _id(t, n):
    return f"{S}/{t}/{n}"


# --- the estate -------------------------------------------------------------------

PLAN = _id("microsoft.web/serverfarms", "plan-app")
APP = _id("microsoft.web/sites", "app-orders")
FUNC = _id("microsoft.web/sites", "func-jobs")
VNET = _id("microsoft.network/virtualnetworks", "vnet-app")
NSG = _id("microsoft.network/networksecuritygroups", "nsg-app")
KV = _id("microsoft.keyvault/vaults", "kv-app")
ST = _id("microsoft.storage/storageaccounts", "stappdata")
SQLSRV = _id("microsoft.sql/servers", "sql-app")
SQLDB = f"{SQLSRV}/databases/db-orders"
REDIS = _id("microsoft.cache/redis", "redis-app")
COSMOS = _id("microsoft.documentdb/databaseaccounts", "cosmos-app")
OPENAI = _id("microsoft.cognitiveservices/accounts", "oai-app")
AI = _id("microsoft.insights/components", "appi-app")
LAW = _id("microsoft.operationalinsights/workspaces", "law-app")
ACR = _id("microsoft.containerregistry/registries", "acrapp")
AKS = _id("microsoft.containerservice/managedclusters", "aks-app")
UAI = _id("microsoft.managedidentity/userassignedidentities", "uai-app")
VM = _id("microsoft.compute/virtualmachines", "vm-build")
NIC = _id("microsoft.network/networkinterfaces", "nic-build")
DISK = _id("microsoft.compute/disks", "vm-build-osdisk")
PE = _id("microsoft.network/privateendpoints", "pe-sql")
AGW = _id("microsoft.network/applicationgateways", "agw-app")
ML = _id("microsoft.machinelearningservices/workspaces", "mlw-app")
SB = _id("microsoft.servicebus/namespaces", "sb-app")
PIP = _id("microsoft.network/publicipaddresses", "pip-build")
IMG = _id("microsoft.compute/galleries", "gal-app") + "/images/img-ubuntu/versions/1.2.3"
AVSET = _id("microsoft.compute/availabilitysets", "avset-build")
VMEXT = _id("microsoft.compute/virtualmachines", "vm-build") + "/extensions/AzureMonitorLinuxAgent"


def _estate():
    return [
        # --- platform targets, each advertising its real endpoint property -------
        {"id": PLAN, "name": "plan-app", "type": "microsoft.web/serverfarms",
         "properties": {"numberOfSites": 2}},
        {"id": KV, "name": "kv-app", "type": "microsoft.keyvault/vaults",
         "properties": {"vaultUri": "https://kv-app.vault.azure.net/"}},
        {"id": ST, "name": "stappdata", "type": "microsoft.storage/storageaccounts",
         "properties": {"primaryEndpoints": {
             "blob": "https://stappdata.blob.core.windows.net/",
             "queue": "https://stappdata.queue.core.windows.net/"}}},
        {"id": SQLSRV, "name": "sql-app", "type": "microsoft.sql/servers",
         "properties": {"fullyQualifiedDomainName": "sql-app.database.windows.net"}},
        {"id": SQLDB, "name": "db-orders", "type": "microsoft.sql/servers/databases",
         "properties": {"status": "Online"}},
        {"id": REDIS, "name": "redis-app", "type": "microsoft.cache/redis",
         "properties": {"hostName": "redis-app.redis.cache.windows.net", "sslPort": 6380}},
        {"id": COSMOS, "name": "cosmos-app", "type": "microsoft.documentdb/databaseaccounts",
         "properties": {"documentEndpoint": "https://cosmos-app.documents.azure.com:443/"}},
        {"id": OPENAI, "name": "oai-app", "type": "microsoft.cognitiveservices/accounts",
         "kind": "OpenAI",
         "properties": {"endpoint": "https://oai-app.openai.azure.com/"}},
        {"id": SB, "name": "sb-app", "type": "microsoft.servicebus/namespaces",
         "properties": {"serviceBusEndpoint": "https://sb-app.servicebus.windows.net:443/"}},
        {"id": ACR, "name": "acrapp", "type": "microsoft.containerregistry/registries",
         "properties": {"loginServer": "acrapp.azurecr.io"}},
        {"id": LAW, "name": "law-app", "type": "microsoft.operationalinsights/workspaces",
         "properties": {"customerId": "22222222-2222-2222-2222-222222222222"}},
        {"id": AI, "name": "appi-app", "type": "microsoft.insights/components",
         "properties": {"InstrumentationKey": "33333333-3333-3333-3333-333333333333",
                        "WorkspaceResourceId": LAW}},
        {"id": UAI, "name": "uai-app",
         "type": "microsoft.managedidentity/userassignedidentities",
         "properties": {"principalId": "aaaaaaaa-0000-0000-0000-000000000001",
                        "clientId": "bbbbbbbb-0000-0000-0000-000000000001"}},

        # --- network fabric -----------------------------------------------------
        {"id": VNET, "name": "vnet-app", "type": "microsoft.network/virtualnetworks",
         "properties": {"subnets": [
             {"name": "snet-apps", "properties": {
                 "networkSecurityGroup": {"id": NSG},
                 "ipConfigurations": [{"id": f"{NIC}/ipConfigurations/ipconfig1"}]}}]}},
        {"id": NSG, "name": "nsg-app", "type": "microsoft.network/networksecuritygroups",
         "properties": {"securityRules": []}},

        # --- a web app: the single most common seed ------------------------------
        {"id": APP, "name": "app-orders", "type": "microsoft.web/sites", "kind": "app,linux",
         "identity": {"type": "UserAssigned",
                      "userAssignedIdentities": {UAI: {
                          "principalId": "aaaaaaaa-0000-0000-0000-000000000001"}}},
         "properties": {
             "serverFarmId": PLAN,
             "defaultHostName": "app-orders.azurewebsites.net",
             "virtualNetworkSubnetId": f"{VNET}/subnets/snet-apps",
             "siteConfig": {
                 "linuxFxVersion": "DOCKER|acrapp.azurecr.io/orders:1.4.2",
                 "appSettings": [
                     {"name": "APPINSIGHTS_INSTRUMENTATIONKEY",
                      "value": "33333333-3333-3333-3333-333333333333"},
                     {"name": "KV_URI", "value": "https://kv-app.vault.azure.net/"},
                     {"name": "REDIS_HOST", "value": "redis-app.redis.cache.windows.net:6380"},
                     {"name": "COSMOS_URI", "value": "https://cosmos-app.documents.azure.com:443/"},
                     {"name": "OPENAI_ENDPOINT", "value": "https://oai-app.openai.azure.com/"},
                     {"name": "SB_NS", "value": "sb-app.servicebus.windows.net"},
                     {"name": "SQL_CONN",
                      "value": "Server=tcp:sql-app.database.windows.net,1433;"
                               "Database=db-orders;Authentication=Active Directory Default;"},
                     {"name": "BLOB", "value": "https://stappdata.blob.core.windows.net/uploads"},
                 ]}}},

        # --- a function app on the same plan ------------------------------------
        {"id": FUNC, "name": "func-jobs", "type": "microsoft.web/sites",
         "kind": "functionapp,linux",
         "properties": {"serverFarmId": PLAN,
                        "defaultHostName": "func-jobs.azurewebsites.net",
                        "siteConfig": {"appSettings": [
                            {"name": "AzureWebJobsStorage",
                             "value": "DefaultEndpointsProtocol=https;AccountName=stappdata;"
                                      "EndpointSuffix=core.windows.net"}]}}},

        # --- AKS with its kubelet identity pulling from the registry -------------
        {"id": AKS, "name": "aks-app", "type": "microsoft.containerservice/managedclusters",
         "identity": {"type": "SystemAssigned",
                      "principalId": "aaaaaaaa-0000-0000-0000-000000000002"},
         "properties": {
             "kubernetesVersion": "1.30.4",
             "nodeResourceGroup": "MC_rg-app_aks-app_westeurope",
             "agentPoolProfiles": [{"name": "sys",
                                    "vnetSubnetID": f"{VNET}/subnets/snet-apps"}],
             "identityProfile": {"kubeletidentity": {
                 "objectId": "aaaaaaaa-0000-0000-0000-000000000003"}}}},

        # --- a VM, its NIC and its managed disk ---------------------------------
        {"id": VM, "name": "vm-build", "type": "microsoft.compute/virtualmachines",
         "properties": {"networkProfile": {"networkInterfaces": [{"id": NIC}]},
                        "availabilitySet": {"id": AVSET},
                        "storageProfile": {"osDisk": {"managedDisk": {"id": DISK}},
                                           "imageReference": {"id": IMG}},
                        "diagnosticsProfile": {"bootDiagnostics": {
                            "enabled": True,
                            "storageUri": "https://stappdata.blob.core.windows.net/"}}}},
        {"id": AVSET, "name": "avset-build", "type": "microsoft.compute/availabilitysets",
         "properties": {"platformFaultDomainCount": 2}},
        {"id": IMG, "name": "1.2.3",
         "type": "microsoft.compute/galleries/images/versions",
         "properties": {"provisioningState": "Succeeded"}},
        {"id": VMEXT, "name": "AzureMonitorLinuxAgent",
         "type": "microsoft.compute/virtualmachines/extensions",
         "properties": {"publisher": "Microsoft.Azure.Monitor"}},
        {"id": PIP, "name": "pip-build", "type": "microsoft.network/publicipaddresses",
         "properties": {"ipAddress": "20.50.1.7",
                        "dnsSettings": {"fqdn": "vm-build.westeurope.cloudapp.azure.com"}}},
        {"id": NIC, "name": "nic-build", "type": "microsoft.network/networkinterfaces",
         "properties": {"ipConfigurations": [
             {"name": "ipconfig1",
              "properties": {"subnet": {"id": f"{VNET}/subnets/snet-apps"},
                             "publicIPAddress": {"id": PIP}}}],
             "networkSecurityGroup": {"id": NSG}}},
        {"id": DISK, "name": "vm-build-osdisk", "type": "microsoft.compute/disks",
         "managedBy": VM, "properties": {"diskSizeGB": 128}},

        # --- private endpoint fronting the SQL server ---------------------------
        {"id": PE, "name": "pe-sql", "type": "microsoft.network/privateendpoints",
         "properties": {"subnet": {"id": f"{VNET}/subnets/snet-apps"},
                        "privateLinkServiceConnections": [
                            {"properties": {"privateLinkServiceId": SQLSRV}}]}},

        # --- app gateway routing to the web app ---------------------------------
        {"id": AGW, "name": "agw-app", "type": "microsoft.network/applicationgateways",
         "properties": {
             "gatewayIPConfigurations": [
                 {"properties": {"subnet": {"id": f"{VNET}/subnets/snet-apps"}}}],
             "backendAddressPools": [
                 {"properties": {"backendAddresses": [
                     {"fqdn": "app-orders.azurewebsites.net"}]}}]}},

        # --- ML workspace and its backing platform ------------------------------
        {"id": ML, "name": "mlw-app", "type": "microsoft.machinelearningservices/workspaces",
         "properties": {"storageAccount": ST, "keyVault": KV,
                        "applicationInsights": AI, "containerRegistry": ACR}},

        # --- RBAC: the app's user-assigned identity reads the vault -------------
        {"id": f"{S}/microsoft.authorization/roleassignments/ra-1", "name": "ra-1",
         "type": "microsoft.authorization/roleassignments",
         "properties": {"principalId": "aaaaaaaa-0000-0000-0000-000000000001",
                        "scope": KV, "roleDefinitionId":
                            "/providers/Microsoft.Authorization/roleDefinitions/"
                            "4633458b-17de-408a-b874-0445c86b69e6"}},
        # --- RBAC: the AKS kubelet pulls from the registry ----------------------
        {"id": f"{S}/microsoft.authorization/roleassignments/ra-2", "name": "ra-2",
         "type": "microsoft.authorization/roleassignments",
         "properties": {"principalId": "aaaaaaaa-0000-0000-0000-000000000003",
                        "scope": ACR, "roleDefinitionId":
                            "/providers/Microsoft.Authorization/roleDefinitions/"
                            "7f951dda-4ed3-4680-a7ca-43fe172d538d"}},
    ]


@pytest.fixture(scope="module")
def graph():
    return build_graph(_estate())


def _pairs(graph):
    return {(graph.nodes[e.source].name, graph.nodes[e.target].name) for e in graph.edges}


def _kinds(graph, src, tgt):
    return {e.kind for e in graph.edges
            if graph.nodes[e.source].name == src and graph.nodes[e.target].name == tgt}


# --- the edges a real trace has to produce ----------------------------------------

@pytest.mark.parametrize("src,tgt,why", [
    ("app-orders", "plan-app", "serverFarmId"),
    ("app-orders", "vnet-app", "virtualNetworkSubnetId"),
    ("app-orders", "kv-app", "Key Vault URI in an app setting"),
    ("app-orders", "redis-app", "redis hostName in an app setting"),
    ("app-orders", "cosmos-app", "documentEndpoint in an app setting"),
    ("app-orders", "oai-app", "Azure OpenAI endpoint in an app setting"),
    ("app-orders", "sb-app", "Service Bus namespace host in an app setting"),
    ("app-orders", "sql-app", "SQL FQDN in a connection string"),
    ("app-orders", "db-orders", "Database= names the scanned database"),
    ("app-orders", "stappdata", "blob endpoint in an app setting"),
    ("app-orders", "appi-app", "instrumentation key in an app setting"),
    ("app-orders", "acrapp", "DOCKER| image in linuxFxVersion"),
    ("app-orders", "uai-app", "identity.userAssignedIdentities"),
    ("func-jobs", "plan-app", "the same plan as the web app"),
    ("func-jobs", "stappdata", "AccountName= in AzureWebJobsStorage"),
    ("appi-app", "law-app", "WorkspaceResourceId"),
    ("aks-app", "vnet-app", "agentPoolProfiles[].vnetSubnetID"),
    ("aks-app", "acrapp", "kubelet identity holds AcrPull"),
    ("vm-build", "nic-build", "networkProfile.networkInterfaces"),
    ("vm-build", "vm-build-osdisk", "storageProfile.osDisk.managedDisk"),
    ("vm-build", "1.2.3", "storageProfile.imageReference - which image it was built from"),
    ("vm-build", "avset-build", "availabilitySet"),
    ("vm-build", "stappdata", "boot diagnostics storage"),
    ("nic-build", "vnet-app", "ipConfigurations[].subnet"),
    ("nic-build", "nsg-app", "networkSecurityGroup"),
    ("nic-build", "pip-build", "ipConfigurations[].publicIPAddress"),
    ("AzureMonitorLinuxAgent", "vm-build", "an extension is a child of its VM"),
    ("pe-sql", "sql-app", "privateLinkServiceConnections"),
    ("pe-sql", "vnet-app", "subnet"),
    ("agw-app", "app-orders", "backend pool FQDN"),
    ("agw-app", "vnet-app", "gatewayIPConfigurations[].subnet"),
    ("mlw-app", "stappdata", "workspace storageAccount"),
    ("mlw-app", "kv-app", "workspace keyVault"),
    ("mlw-app", "appi-app", "workspace applicationInsights"),
    ("mlw-app", "acrapp", "workspace containerRegistry"),
    ("db-orders", "sql-app", "nested child of the server"),
])
def test_the_estate_produces_the_edge(graph, src, tgt, why):
    assert (src, tgt) in _pairs(graph), f"missing {src} -> {tgt} ({why})"


@pytest.mark.parametrize("src,tgt,kind", [
    ("vm-build", "nic-build", "has-nic"),
    ("vm-build", "vm-build-osdisk", "uses-disk"),
    ("vm-build", "1.2.3", "built-from-image"),
    ("vm-build", "avset-build", "hosted-on"),
    ("nic-build", "vnet-app", "in-subnet"),
    ("nic-build", "nsg-app", "protected-by"),
    ("nic-build", "pip-build", "has-public-ip"),
])
def test_compute_and_network_edges_are_named_not_generic(graph, src, tgt, kind):
    """Compute plus network is the largest category in a real estate. Every one
    of these edges used to read `references`, which is true and unhelpful."""
    assert kind in _kinds(graph, src, tgt)


def test_a_nic_does_not_claim_its_vm_as_a_dependency(graph):
    # The NIC's own virtualMachine.id is the mirror of the VM's has-nic edge.
    assert ("nic-build", "vm-build") not in _pairs(graph)


def test_rbac_role_names_are_resolved_from_their_guids(graph):
    assert any("Key Vault Secrets User" in k for k in _kinds(graph, "app-orders", "kv-app"))
    assert any("AcrPull" in k for k in _kinds(graph, "aks-app", "acrapp"))


def test_every_edge_carries_its_evidence(graph):
    assert all(e.evidence for e in graph.edges)


def test_nothing_in_a_realistic_estate_is_an_unverified_external(graph):
    assert [n.name for n in graph.nodes.values() if n.external] == []


# --- seeds people actually type ---------------------------------------------------

@pytest.mark.parametrize("seed,expected", [
    ("app-orders", {"plan-app", "kv-app", "sql-app", "redis-app", "cosmos-app",
                    "oai-app", "acrapp", "stappdata", "appi-app", "uai-app"}),
    ("aks-app", {"acrapp", "vnet-app"}),
    ("vm-build", {"nic-build", "vm-build-osdisk", "pip-build", "vnet-app", "nsg-app",
                  "avset-build", "1.2.3", "stappdata"}),
    ("mlw-app", {"stappdata", "kv-app", "appi-app", "acrapp"}),
])
def test_a_common_seed_reaches_its_dependencies(graph, seed, expected):
    sub = blast_radius(graph, find_seeds(graph, seed)[0], direction="down")
    reached = {n.name for n in sub.nodes.values()}

    assert expected <= reached, f"{seed} missed {expected - reached}"


@pytest.mark.parametrize("seed,expected", [
    ("plan-app", {"app-orders", "func-jobs"}),          # who sits on this plan
    ("kv-app", {"app-orders", "mlw-app"}),              # who reads this vault
    ("acrapp", {"app-orders", "aks-app", "mlw-app"}),   # who pulls from this registry
    ("stappdata", {"app-orders", "func-jobs", "mlw-app"}),
    ("vnet-app", {"app-orders", "aks-app", "nic-build", "pe-sql", "agw-app"}),
    ("sql-app", {"app-orders", "pe-sql"}),
    ("uai-app", {"app-orders"}),
])
def test_shared_infrastructure_reports_its_dependents(graph, seed, expected):
    """The reverse view is the reason to trace shared infrastructure at all."""
    sub = blast_radius(graph, find_seeds(graph, seed)[0], direction="up")
    reached = {n.name for n in sub.nodes.values()}

    assert expected <= reached, f"{seed} missed {expected - reached}"


def test_a_shared_plan_does_not_bridge_two_apps(graph):
    # func-jobs and app-orders share plan-app. Tracing one must not reach the
    # other's dependencies through it.
    sub = blast_radius(graph, find_seeds(graph, "app-orders")[0])

    assert "func-jobs" not in {n.name for n in sub.nodes.values()}


def test_a_shared_vnet_does_not_bridge_the_app_to_the_build_vm(graph):
    sub = blast_radius(graph, find_seeds(graph, "app-orders")[0])

    assert "vm-build" not in {n.name for n in sub.nodes.values()}
