"""Edge extractors: derive dependency edges from Azure resource properties.

Azure Resource Graph has no first-class "dependencies" table. Relationships are
implied by ids, hostnames and secrets buried in each resource's `properties` and
config. This module reads them out and, crucially, VERIFIES each target against
the set of resources we scanned - anything referenced but not found is surfaced
as an explicit `external` node rather than silently dropped.
"""

import re
from urllib.parse import urlparse

from ..model import Edge, Node

# Well-known role definition GUIDs -> friendly names (fallback when az/RG did not
# already give us roleDefinitionName).
# Built-in Azure role GUIDs. These are GLOBAL constants - identical in every
# tenant - so naming them here is generic, not tenant-specific. Resource Graph
# often omits roleDefinitionName, leaving only the GUID; without this table every
# such edge collapses to "custom role", which hides the one thing an RBAC edge is
# for: WHAT the access is. An AKS "RBAC Reader" (an observer) then looks identical
# to a "Cluster Admin" (an operator). Anything genuinely custom (a tenant-defined
# role) still falls through to "custom role" - honestly, because we cannot name it.
ROLE_NAMES = {
    # identity / general
    "8e3af657-a8ff-443c-a75c-2fe8c4bcb635": "Owner",
    "b24988ac-6180-42a0-ab88-20f7382dd24c": "Contributor",
    "acdd72a7-3385-48ef-bd42-f606fba81ae7": "Reader",
    "c12c1c16-33a1-487b-954d-41c89c60f349": "Reader and Data Access",
    "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9": "User Access Administrator",
    "f58310d9-a9f6-439a-9e8d-f62e7b41a168": "Role Based Access Control Administrator",
    # web / compute
    "de139f84-1756-47ae-9be6-808fbbe84772": "Website Contributor",
    # Key Vault
    "4633458b-17de-408a-b874-0445c86b69e6": "Key Vault Secrets User",
    "b86a8fe4-44ce-4948-aee5-eccb2c155cd7": "Key Vault Secrets Officer",
    "21090545-7ca7-4776-b22c-e363652d74d2": "Key Vault Reader",
    "db79e9a7-68ee-4b58-9aeb-b90e7c24fcba": "Key Vault Certificate User",
    "12338af0-0e69-4776-bea7-57ae8d297424": "Key Vault Crypto User",
    "00482a5a-887f-4fb3-b363-3b7fe8e74483": "Key Vault Administrator",
    # storage
    "ba92f5b4-2d11-453d-a403-e96b0029c9fe": "Storage Blob Data Contributor",
    "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1": "Storage Blob Data Reader",
    "b7e6dc6d-f1e8-4753-8033-0f276bb0955b": "Storage Blob Data Owner",
    "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3": "Storage Table Data Contributor",
    "69566ab7-960f-475b-8e7c-b3118f30c6bd": "Storage File Data Privileged Contributor",
    "17d1049b-9a84-46fb-8f53-869881c3d3ab": "Storage Account Contributor",
    # container registry
    "7f951dda-4ed3-4680-a7ca-43fe172d538d": "AcrPull",
    "8311e382-0749-4cb8-b61a-304f252e45ec": "AcrPush",
    # AI / Cognitive Services / Foundry (central to an AI platform - these were
    # the bulk of the "custom role" edges on OpenAI and Search resources)
    "a97b65f3-24c7-4388-baec-2e87135dc908": "Cognitive Services User",
    "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd": "Cognitive Services OpenAI User",
    "a001fd3d-188f-4b5d-821b-7da978bf7442": "Cognitive Services OpenAI Contributor",
    "b78c5d69-af96-48a3-bf8d-a8b4d589de94": "Azure AI Administrator",
    "64702f94-c441-49e6-a78b-ef80e0188fee": "Azure AI Developer",
    "3afb7f49-54cb-416e-8c09-6dc049efa503": "Azure AI Inference Deployment Operator",
    "53ca6127-db72-4b80-b1b0-d745d6d5456d": "Foundry User",
    # Cognitive Search
    "1407120a-92aa-4202-b7e9-c0e197c71c8f": "Search Index Data Reader",
    "8ebe5a00-799e-43f5-93ac-243d3dce84a7": "Search Index Data Contributor",
    "7ca78c08-252a-4471-8644-bb5ff32d4ba0": "Search Service Contributor",
    # AKS (all built-in - the reason an observer showed up as "custom role")
    "0ab0b1a8-8aac-4efd-b8c2-3ee1fb270be8": "Azure Kubernetes Service Cluster Admin Role",
    "4abbcc35-e782-43d8-92c5-2d3f1bd2253f": "Azure Kubernetes Service Cluster User Role",
    "b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b": "Azure Kubernetes Service RBAC Cluster Admin",
    "3498e952-d568-435e-9b2c-8d77e338d7f7": "Azure Kubernetes Service RBAC Admin",
    "a7ffa36f-339b-4b5c-8bdf-e2c188b2c0eb": "Azure Kubernetes Service RBAC Writer",
    "7f6c6a51-bcf8-42ba-9220-52d62157d7db": "Azure Kubernetes Service RBAC Reader",
    # networking / monitoring
    "4d97b98b-1d4f-4787-a291-c67834d212e7": "Network Contributor",
    "43d0d8ad-25c7-4714-9337-8ba259a9fe05": "Monitoring Reader",
    "73c42c96-874c-492b-b04d-ab87d138a893": "Log Analytics Reader",
}

# Service-endpoint domains we recognise -> (edge kind, friendly category).
# Order matters: first suffix match wins.
_DOMAIN_KIND = [
    ("vault.azure.net", "reads-secret", "Key Vault"),
    ("postgres.database.azure.com", "connects-to", "PostgreSQL"),
    ("mysql.database.azure.com", "connects-to", "MySQL"),
    ("database.windows.net", "connects-to", "SQL"),
    ("documents.azure.com", "connects-to", "Cosmos DB"),
    ("redis.cache.windows.net", "connects-to", "Redis"),
    ("servicebus.windows.net", "connects-to", "Service Bus"),
    ("search.windows.net", "connects-to", "Cognitive Search"),
    ("openai.azure.com", "connects-to", "Azure OpenAI"),
    ("cognitiveservices.azure.com", "connects-to", "Cognitive Services"),
    ("blob.core.windows.net", "connects-to", "Storage (blob)"),
    ("queue.core.windows.net", "connects-to", "Storage (queue)"),
    ("table.core.windows.net", "connects-to", "Storage (table)"),
    ("file.core.windows.net", "connects-to", "Storage (file)"),
    ("azurecr.io", "pulls-image", "Container Registry"),
    ("azurewebsites.net", "calls", "Web App"),
    ("azurecontainerapps.io", "calls", "Container App"),
    ("login.microsoftonline.com", "authenticates-via", "Entra ID"),
    ("sts.windows.net", "authenticates-via", "Entra ID"),
]

_HOST_RE = re.compile(
    r"([a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)*\.(?:"
    r"vault\.azure\.net|postgres\.database\.azure\.com|mysql\.database\.azure\.com|"
    r"database\.windows\.net|documents\.azure\.com|redis\.cache\.windows\.net|"
    r"servicebus\.windows\.net|search\.windows\.net|openai\.azure\.com|"
    r"cognitiveservices\.azure\.com|blob\.core\.windows\.net|queue\.core\.windows\.net|"
    r"table\.core\.windows\.net|file\.core\.windows\.net|azurecr\.io|azurewebsites\.net|"
    r"azurecontainerapps\.io"
    r"))",
    re.I,
)
_ENTRA_RE = re.compile(r"(login\.microsoftonline\.com|sts\.windows\.net)", re.I)

_KV_REF = re.compile(r"@microsoft\.keyvault\(([^)]*)\)", re.I)
_VAULT_NAME = re.compile(r"vaultname\s*=\s*([^;)\s]+)", re.I)
_SECRET_URI_HOST = re.compile(r"https://([a-z0-9\-]+)\.vault\.azure\.net", re.I)
_STORAGE_ACCOUNT = re.compile(r"accountname\s*=\s*([a-z0-9]{3,24})", re.I)


def _lower(s):
    return s.lower() if isinstance(s, str) else s


def domain_kind(host):
    h = (host or "").lower()
    for suffix, kind, cat in _DOMAIN_KIND:
        if h.endswith(suffix) or suffix in h:
            return kind, cat
    return "references", "external service"


def _vault_refs(text):
    names = set()
    for inner in _KV_REF.findall(text):
        m = _VAULT_NAME.search(inner)
        if m:
            names.add(m.group(1).lower())
        m = _SECRET_URI_HOST.search(inner)
        if m:
            names.add(m.group(1).lower())
    return names


def _host_of(url_or_host):
    s = (url_or_host or "").strip().lower()
    if "://" in s:
        return urlparse(s).hostname or ""
    return s.split("/")[0].split(":")[0]


_IDENT = set("abcdefghijklmnopqrstuvwxyz0123456789-.")


def _bounded_in(needle, blob):
    """True if `needle` occurs in `blob` as a whole hostname/identifier, i.e. not
    glued to more hostname chars on either side. Stops 'api.azurewebsites.net'
    from matching inside 'myapi.azurewebsites.net', or account 'foo' inside
    'foobar'. A boundary is anything that is not a letter/digit/dot/hyphen."""
    start = 0
    while True:
        i = blob.find(needle, start)
        if i < 0:
            return False
        before = blob[i - 1] if i > 0 else ""
        after = blob[i + len(needle)] if i + len(needle) < len(blob) else ""
        if before not in _IDENT and after not in _IDENT:
            return True
        start = i + 1


class Resolver:
    """Maps ids / subnet ids / hostnames / principals to a known node id, and
    can look up whether an endpoint host corresponds to a scanned resource."""

    def __init__(self, nodes):
        self.by_id = {}
        self.by_host = {}          # endpoint hostname -> node_id
        self.by_principal = {}
        self.by_ik = {}
        self.web_hosts = {}        # web default hostname -> node_id
        self.kv_by_name = {}
        self.acr_by_loginserver = {}
        self.storage_by_name = {}
        self.law_by_customer_id = {}   # Log Analytics workspace GUID -> node_id
        self.by_name = {}          # resource name -> node_id (for verifying LLM proposals)
        for n in nodes.values():
            self.by_id[n.id.lower()] = n.id
            if n.name:
                self.by_name.setdefault(n.name.lower(), n.id)
            self._index(n)

    def _add_host(self, host, node_id):
        if host:
            self.by_host[host.lower()] = node_id

    def _index(self, n):
        p = n.raw.get("properties") or {}
        t = n.type
        name = n.name.lower()

        pid = (n.raw.get("identity") or {}).get("principalId")
        if pid:
            self.by_principal[pid.lower()] = n.id
        # AKS pulls images with a SEPARATE kubelet identity; map it to the cluster
        # so its role assignments (e.g. AcrPull on an ACR) attribute to the cluster.
        kubelet = ((p.get("identityProfile") or {}).get("kubeletidentity") or {})
        koid = kubelet.get("objectId")
        if koid:
            self.by_principal[koid.lower()] = n.id

        if t == "microsoft.keyvault/vaults":
            self._add_host(_host_of(p.get("vaultUri")) or f"{name}.vault.azure.net", n.id)
            self.kv_by_name[name] = n.id
        elif t == "microsoft.sql/servers":
            self._add_host(p.get("fullyQualifiedDomainName") or f"{name}.database.windows.net", n.id)
        elif t.startswith("microsoft.dbforpostgresql/"):
            self._add_host(p.get("fullyQualifiedDomainName") or f"{name}.postgres.database.azure.com", n.id)
        elif t.startswith("microsoft.dbformysql/"):
            self._add_host(p.get("fullyQualifiedDomainName") or f"{name}.mysql.database.azure.com", n.id)
        elif t == "microsoft.documentdb/databaseaccounts":
            self._add_host(_host_of(p.get("documentEndpoint")) or f"{name}.documents.azure.com", n.id)
        elif t == "microsoft.cache/redis":
            self._add_host(p.get("hostName") or f"{name}.redis.cache.windows.net", n.id)
        elif t == "microsoft.servicebus/namespaces":
            self._add_host(_host_of(p.get("serviceBusEndpoint")) or f"{name}.servicebus.windows.net", n.id)
        elif t == "microsoft.search/searchservices":
            self._add_host(f"{name}.search.windows.net", n.id)
        elif t == "microsoft.cognitiveservices/accounts":
            self._add_host(_host_of(p.get("endpoint")), n.id)
            self._add_host(f"{name}.openai.azure.com", n.id)
            self._add_host(f"{name}.cognitiveservices.azure.com", n.id)
        elif t == "microsoft.storage/storageaccounts":
            for suffix in ("blob", "queue", "table", "file"):
                self._add_host(f"{name}.{suffix}.core.windows.net", n.id)
            self.storage_by_name[name] = n.id
        elif t == "microsoft.containerregistry/registries":
            login = (p.get("loginServer") or f"{name}.azurecr.io").lower()
            self._add_host(login, n.id)
            self.acr_by_loginserver[login] = n.id
        elif t == "microsoft.web/sites":
            dh = (p.get("defaultHostName") or f"{name}.azurewebsites.net").lower()
            self.web_hosts[dh] = n.id
            self._add_host(dh, n.id)
        elif t == "microsoft.app/containerapps":
            # Its ingress FQDN goes in web_hosts too, so an App Gateway backend
            # pool pointing at a container app resolves the same way as one
            # pointing at a web app.
            fqdn = _lower(((p.get("configuration") or {}).get("ingress") or {}).get("fqdn"))
            if fqdn:
                self.web_hosts[fqdn] = n.id
                self._add_host(fqdn, n.id)
        elif t == "microsoft.insights/components":
            ik = p.get("InstrumentationKey")
            if ik:
                self.by_ik[ik.lower()] = n.id
        elif t == "microsoft.operationalinsights/workspaces":
            # A Container Apps environment names its workspace by customerId, not
            # by resource id - index it so that reference can still be resolved.
            cid = _lower(p.get("customerId"))
            if cid:
                self.law_by_customer_id[cid] = n.id

    def by_resource_id(self, idstr):
        if not isinstance(idstr, str):
            return None
        key = idstr.strip().lower()
        if key in self.by_id:
            return self.by_id[key]
        marker = "/subnets/"
        if marker in key:
            vnet = key.split(marker)[0]
            if vnet in self.by_id:
                return self.by_id[vnet]
        return None

    def host_lookup(self, host):
        return self.by_host.get((host or "").lower())


# Types excluded as a SOURCE of generic reference edges. Two rationales, same
# effect - keep them off the map as edge origins:
#   - fabric: foundational network resources are depended-UPON, never depending;
#     emitting edges from them turns a shared VNet into a hub (its subnets list
#     everything plugged in). Dependents still point AT them (vnet-integration).
#   - observers: monitoring/alerting/dashboards exist to POINT AT resources and
#     watch or display them. They are not dependencies - if you change the target
#     the observer does not break, it just observes - so they are noise on a
#     blast-radius map (the same reason a read-only RBAC role is not a dependent).
_GENERIC_SOURCE_SKIP = {
    # fabric
    "microsoft.network/virtualnetworks",
    "microsoft.network/networksecuritygroups",
    "microsoft.network/routetables",
    "microsoft.network/privatednszones",
    # observers
    "microsoft.alertsmanagement/prometheusrulegroups",
    "microsoft.alertsmanagement/smartdetectoralertrules",
    "microsoft.insights/metricalerts",
    "microsoft.insights/scheduledqueryrules",
    "microsoft.insights/activitylogalerts",
    "microsoft.portal/dashboards",
}


def _iter_arm_ids(obj, path):
    """Yield (arm_id, dotted_path) for every value under `obj` that is itself a
    whole ARM resource id. The path becomes the edge's proof. Substrings inside
    URIs are not matched (the value must BE an id), and the resolver is the real
    filter - an id that names nothing we scanned yields no edge."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _iter_arm_ids(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_arm_ids(v, f"{path}[]")
    elif isinstance(obj, str):
        s = obj.strip()
        low = s.lower()
        if low.startswith("/subscriptions/") and "/providers/" in low:
            yield s, path


def extract_edges(nodes):
    """ARM-derivable edges over the whole scanned set. Targets that do not
    resolve are dropped here (they would be noise at tenant scale); the seed's
    own unresolved references are surfaced separately as external nodes."""
    r = Resolver(nodes)
    edges = []

    def add(src, dst, kind, evidence=""):
        if src and dst and src != dst:
            edges.append(Edge(source=src, target=dst, kind=kind,
                              origin="extracted", evidence=evidence))

    for n in nodes.values():
        p = n.raw.get("properties") or {}
        t = n.type

        farm = p.get("serverFarmId")
        if farm:
            add(n.id, r.by_resource_id(farm), "hosted-on", "properties.serverFarmId")

        subnet = p.get("virtualNetworkSubnetId")
        if subnet:
            add(n.id, r.by_resource_id(subnet), "vnet-integration",
                "properties.virtualNetworkSubnetId")

        for pool in p.get("agentPoolProfiles") or []:
            if pool.get("vnetSubnetID"):
                add(n.id, r.by_resource_id(pool["vnetSubnetID"]), "vnet-integration",
                    "properties.agentPoolProfiles[].vnetSubnetID")

        addon = (((p.get("addonProfiles") or {}).get("omsagent") or {}).get("config") or {})
        if addon.get("logAnalyticsWorkspaceResourceID"):
            add(n.id, r.by_resource_id(addon["logAnalyticsWorkspaceResourceID"]),
                "uses-workspace", "properties.addonProfiles.omsagent")
        if p.get("WorkspaceResourceId"):
            add(n.id, r.by_resource_id(p["WorkspaceResourceId"]), "uses-workspace",
                "properties.WorkspaceResourceId")

        # ML workspace / AI Hub: its backing platform is a set of ARM ids in
        # properties (the store, vault, telemetry and image registry it is built on).
        if t == "microsoft.machinelearningservices/workspaces":
            for key, kind in (("storageAccount", "connects-to"),
                              ("keyVault", "reads-secret"),
                              ("applicationInsights", "sends-telemetry"),
                              ("containerRegistry", "pulls-image")):
                ref = p.get(key)
                if isinstance(ref, str) and ref.startswith("/subscriptions/"):
                    add(n.id, r.by_resource_id(ref), kind, f"properties.{key}")
            for assoc in p.get("associatedWorkspaces") or []:
                add(n.id, r.by_resource_id(assoc), "associated-with",
                    "properties.associatedWorkspaces[]")

        for c in p.get("privateLinkServiceConnections") or []:
            cp = c.get("properties", c) or {}
            if cp.get("privateLinkServiceId"):
                add(n.id, r.by_resource_id(cp["privateLinkServiceId"]), "private-link-to",
                    "properties.privateLinkServiceConnections[].privateLinkServiceId")
        if (p.get("subnet") or {}).get("id"):
            add(n.id, r.by_resource_id(p["subnet"]["id"]), "in-subnet", "properties.subnet.id")

        for g in p.get("gatewayIPConfigurations") or []:
            gs = ((g.get("properties") or {}).get("subnet") or {}).get("id")
            if gs:
                add(n.id, r.by_resource_id(gs), "in-subnet",
                    "properties.gatewayIPConfigurations[].subnet")
        for pool in p.get("backendAddressPools") or []:
            for ba in (pool.get("properties") or {}).get("backendAddresses") or []:
                fqdn = _lower(ba.get("fqdn"))
                if fqdn and fqdn in r.web_hosts:
                    add(n.id, r.web_hosts[fqdn], "routes-to",
                        f"backend pool fqdn {fqdn}")

        if t == "microsoft.authorization/roleassignments":
            src = r.by_principal.get(_lower(p.get("principalId")))
            scope = r.by_resource_id(p.get("scope", ""))
            role = p.get("roleDefinitionName")
            if not role:
                rid = (p.get("roleDefinitionId") or "").rsplit("/", 1)[-1].lower()
                role = ROLE_NAMES.get(rid, "custom role")
            add(src, scope, f"role: {role}", "RBAC role assignment (principal -> scope)")

        if t == "microsoft.web/sites":
            _webapp_config_edges(n, p, r, add)
        elif t == "microsoft.app/containerapps":
            _containerapp_edges(n, p, r, add)
        elif t == "microsoft.app/managedenvironments":
            _managedenv_edges(n, p, r, add)
        elif t == "microsoft.containerservice/managedclusters":
            _aks_config_edges(n, p, r, add)

    # Generic ARM-reference pass. Any resolvable resource id sitting in a
    # resource's properties is a real dependency, whatever the type - this is
    # what lets cloudmap map resource types it has no hand-written rule for,
    # deterministically, with the property path as proof. A semantic rule that
    # already named a pair wins (kept as-is); the generic edge only fills gaps.
    known = {(e.source, e.target) for e in edges}
    generic = set()
    for n in nodes.values():
        if n.type == "microsoft.authorization/roleassignments":
            continue                      # plumbing we derive edges FROM, not a map node
        # Fabric (a VNet listing everything plugged in) and observers (a rule
        # group or dashboard watching a resource) are not dependencies - skip them
        # as generic-edge sources so they do not clutter or hub the map.
        if n.type in _GENERIC_SOURCE_SKIP:
            continue
        for arm_id, path in _iter_arm_ids(n.raw.get("properties") or {}, "properties"):
            tgt = r.by_resource_id(arm_id)
            if "privateendpointconnections" in path.lower() or "privatelinkserviceconnections" in path.lower():
                continue
            if (not tgt or tgt == n.id or (n.id, tgt) in known or (tgt, n.id) in known
                    or nodes[tgt].type == "microsoft.authorization/roleassignments"):
                continue
            # two resources that name each other (a NIC and its VNet) would give a
            # back-and-forth pair; keep the first direction seen, drop the mirror.
            if (tgt, n.id) in generic:
                continue
            add(n.id, tgt, "references", path)
            known.add((n.id, tgt))
            generic.add((n.id, tgt))

    return _dedupe(edges)


def _config_values(p):
    sc = p.get("siteConfig") or {}
    out = []
    for s in sc.get("appSettings") or []:
        out.append(str(s.get("value", "")))
    for c in (sc.get("connectionStrings") or p.get("connectionStrings") or []):
        out.append(str(c.get("connectionString", c.get("value", ""))))
    return out, sc


def _config_edges(n, values, r, add, label="app config"):
    """The rules that read a workload's configuration. Shared, because a web app's
    appSettings and a container app's env vars are the same problem wearing
    different field names: free text that happens to name other resources."""
    blob = " ".join(values).lower()

    # O(blob) extraction instead of O(hosts * blob)
    import re
    words = set(re.findall(r"[a-z0-9.-]+", blob))
    
    for word in words:
        if word in r.by_host:
            add(n.id, r.by_host[word], domain_kind(word)[0], f"{label} references host {word}")
        if word in r.by_ik:
            add(n.id, r.by_ik[word], "sends-telemetry", f"{label} contains instrumentation key")

    # Storage accounts are often in connection strings: accountname=xyz
    for m in re.finditer(r"accountname=([a-z0-9-]+)", blob):
        acct = m.group(1)
        if acct in r.storage_by_name:
            add(n.id, r.storage_by_name[acct], "connects-to", f"{label} references accountname={acct}")

    for vname in _vault_refs(blob):
        if vname in r.kv_by_name:
            add(n.id, r.kv_by_name[vname], "reads-secret",
                f"Key Vault reference to vault {vname}")


def _webapp_config_edges(n, p, r, add):
    values, sc = _config_values(p)
    _config_edges(n, values, r, add)

    fx = f"{sc.get('linuxFxVersion', '')} {sc.get('windowsFxVersion', '')}"
    m = re.search(r"docker\|([a-z0-9.\-]+\.azurecr\.io)/", fx, re.I)
    if m:
        add(n.id, r.acr_by_loginserver.get(m.group(1).lower()), "pulls-image",
            f"linuxFxVersion docker image {m.group(1).lower()}")


def _containerapp_values(p):
    """A container app's free-text config: container env vars, plus the vault URLs
    its secrets are pulled from."""
    out = []
    for c in (p.get("template") or {}).get("containers") or []:
        for e in c.get("env") or []:
            out.append(str(e.get("value", "")))
    for s in (p.get("configuration") or {}).get("secrets") or []:
        out.append(str(s.get("keyVaultUrl", "")))
    return out


def _containerapp_edges(n, p, r, add):
    """Container Apps hold the same dependencies a web app holds, in different
    places: an environment instead of a plan, `configuration.registries` instead
    of a docker image string, `template.containers[].env` instead of appSettings.

    Unlike a web app, none of this needs deep enrichment - Resource Graph returns
    `properties.template` in full, so a plain scan already sees the config.
    """
    env_id = p.get("environmentId") or p.get("managedEnvironmentId")
    if env_id:
        add(n.id, r.by_resource_id(env_id), "hosted-on", "properties.environmentId")

    cfg = p.get("configuration") or {}
    for reg in cfg.get("registries") or []:
        server = _lower(reg.get("server"))
        if server:
            add(n.id, r.acr_by_loginserver.get(server), "pulls-image",
                f"configuration.registries[].server {server}")
    for sec in cfg.get("secrets") or []:
        m = _SECRET_URI_HOST.search(str(sec.get("keyVaultUrl") or ""))
        if m:
            add(n.id, r.kv_by_name.get(m.group(1).lower()), "reads-secret",
                f"configuration.secrets[].keyVaultUrl vault {m.group(1).lower()}")

    for c in (p.get("template") or {}).get("containers") or []:
        m = re.match(r"([a-z0-9.\-]+\.azurecr\.io)/", str(c.get("image") or "").lower())
        if m:
            add(n.id, r.acr_by_loginserver.get(m.group(1)), "pulls-image",
                f"template.containers[].image {m.group(1)}")

    _config_edges(n, _containerapp_values(p), r, add, label="container env")


def _managedenv_edges(n, p, r, add):
    subnet = (p.get("vnetConfiguration") or {}).get("infrastructureSubnetId")
    if subnet:
        add(n.id, r.by_resource_id(subnet), "vnet-integration",
            "properties.vnetConfiguration.infrastructureSubnetId")

    law = ((p.get("appLogsConfiguration") or {}).get("logAnalyticsConfiguration") or {})
    cid = _lower(law.get("customerId"))
    if cid:
        add(n.id, r.law_by_customer_id.get(cid), "uses-workspace",
            "appLogsConfiguration.logAnalyticsConfiguration.customerId")


def _aks_config_edges(n, p, r, add):
    """Extract dependencies from an AKS cluster's Kubernetes manifests."""
    k8s_text = n.raw.get("kubernetes_text") or ""
    
    values = []
    
    for line in k8s_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("image:"):
            image = line[6:]
            m = re.match(r"([a-z0-9.\-]+\.azurecr\.io)/", image.lower())
            if m:
                add(n.id, r.acr_by_loginserver.get(m.group(1)), "pulls-image",
                    f"Pod container image {m.group(1)}")
        elif line.startswith("env:"):
            values.append(line[4:])
        elif line.startswith("cm:"):
            values.append(line[3:])
        elif line.startswith("sec_decoded:"):
            values.append(line[12:])
                    
    _config_edges(n, values, r, add, label="AKS workload config")


def seed_external_dependencies(seed_node, resolver):
    """Anything the seed references that did NOT resolve to a scanned resource is
    surfaced as an explicit external / unverified node - never silently dropped.
    Values are already secret-resolved by the ingest layer if the user opted in.
    """
    p = seed_node.raw.get("properties") or {}
    if seed_node.type == "microsoft.app/containerapps":
        values = _containerapp_values(p)
        self_host = _lower(((p.get("configuration") or {}).get("ingress") or {}).get("fqdn")) or ""
    else:
        values, _sc = _config_values(p)
        self_host = (p.get("defaultHostName") or f"{seed_node.name}.azurewebsites.net").lower()

    ext_nodes, edges, seen = {}, [], set()

    def add_external(host, kind, cat, why):
        nid = f"external://{host}"
        if nid not in ext_nodes:
            ext_nodes[nid] = Node(id=nid, name=host, type=f"external/{cat}",
                                  external=True, note=why)
        edges.append(Edge(seed_node.id, nid, kind))

    for val in values:
        low = val.lower()
        hosts = set(_HOST_RE.findall(low)) | set(_ENTRA_RE.findall(low))
        for host in hosts:
            if host in seen or host == self_host:
                continue
            seen.add(host)
            if resolver.host_lookup(host):
                continue                      # resolved -> edge already emitted
            kind, cat = domain_kind(host)
            add_external(host, kind, cat, "referenced in app config; not found in scanned subscriptions")
        for vname in _vault_refs(low):
            if vname not in resolver.kv_by_name and vname not in seen:
                seen.add(vname)
                add_external(f"{vname}.vault.azure.net", "reads-secret", "Key Vault",
                             "Key Vault reference; vault not found in scanned subscriptions")
        for acct in _STORAGE_ACCOUNT.findall(low):
            acct = acct.lower()
            if acct not in resolver.storage_by_name and acct not in seen:
                seen.add(acct)
                add_external(f"{acct}.blob.core.windows.net", "connects-to", "Storage",
                             "storage account referenced in app config; not found in scanned subscriptions")

    return list(ext_nodes.values()), edges


def _dedupe(edges):
    merged = {}
    for e in edges:
        key = (e.source, e.target)
        if key in merged:
            m = merged[key]
            for k in e.kind.split("; "):
                if k not in m.kind.split("; "):
                    m.kind += "; " + k
            if e.origin == "extracted":          # a verified edge on this pair wins
                m.origin = "extracted"
            if e.evidence and e.evidence not in m.evidence:
                m.evidence = f"{m.evidence}; {e.evidence}".strip("; ")
        else:
            merged[key] = Edge(source=e.source, target=e.target, kind=e.kind,
                               origin=e.origin, evidence=e.evidence)
    return list(merged.values())


def merge_model_edges(edges, model_edges):
    """Fold model-proposed edges into the deterministic set with the one rule that
    keeps the graph trustworthy: a model edge may only ADD a new (source, target)
    pair. It never modifies or relabels an edge a deterministic rule already
    produced - deterministic always wins."""
    existing = {(e.source, e.target) for e in edges}
    additions = [e for e in model_edges if (e.source, e.target) not in existing]
    return _dedupe(edges + additions)
