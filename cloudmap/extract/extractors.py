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


# Property names under which a resource states its OWN address. Read nothing
# else: a host sitting under some other key is usually one this resource
# REFERENCES, and indexing that would let it claim another resource's identity.
_SELF_HOST_KEYS = re.compile(
    r"(endpoint|endpoints|fqdn|fqdns|hostname|hostnames|host|uri|url|"
    r"loginserver|dnsname|vaulturi|serviceurl)$", re.I)

# ...except the ones that name SHARED platform infrastructure rather than the
# resource. Two web apps on the same App Service scale unit report the same
# `ftpsHostName` (waws-prod-am2-735...), and reading that as identity invented a
# dependency between two applications that have nothing to do with each other.
# Found on live data; no synthetic fixture would have produced it.
_SHARED_PLATFORM_KEYS = re.compile(
    r"^(ftpshostname|ftphostname|repositorysitename|"
    r"possibleoutboundipaddresses|outboundipaddresses|inboundipaddress)$", re.I)


def _squash(text):
    """Lowercase alphanumerics only, so `kv-app` and `kvapp` compare equal."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _plausible_host(h):
    """A DNS name, not an ARM id, a path, a port or a bare IP."""
    if not h or len(h) > 253 or any(c in h for c in " /\\?#@"):
        return False
    if re.fullmatch(r"[\d.]+", h):        # bare IP: too ambiguous to own a name
        return False
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}", h))


def _self_hosts(node, depth=0, key_hint=""):
    """Every host name a resource advertises about itself, at any nesting depth.

    This is what makes hostname resolution work for a type nobody wrote a rule
    for: `properties.endpoint`, `properties.hostNames[]`, `properties.x.fqdn`
    all get indexed the same way, so a connection string naming that host
    resolves to the real resource instead of becoming an unverified external.
    """
    out = set()
    if depth > 6:
        return out
    if isinstance(node, dict):
        for k, v in node.items():
            out |= _self_hosts(v, depth + 1, k if isinstance(k, str) else key_hint)
    elif isinstance(node, list):
        for v in node:
            out |= _self_hosts(v, depth + 1, key_hint)
    elif isinstance(node, str) and _SELF_HOST_KEYS.search(key_hint or ""):
        if _SHARED_PLATFORM_KEYS.match(key_hint or ""):
            return out
        h = _host_of(node)
        if _plausible_host(h):
            out.add(h)
    return out


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
        self.sqldb_by_name = {}    # db name -> [(parent server id lower, db node_id)]
        self._generic_claims = {}   # host -> {node ids that advertise it}
        for n in nodes.values():
            self.by_id[n.id.lower()] = n.id
            if n.name:
                self.by_name.setdefault(n.name.lower(), n.id)
            self._index(n)
        # A host that two resources both advertise is not an identity, it is
        # shared platform infrastructure, and resolving it would fabricate a
        # dependency between unrelated resources. Typed branches (which read a
        # specific documented property) keep priority and are never overridden.
        for host, owners in self._generic_claims.items():
            if len(owners) == 1 and host not in self.by_host:
                self.by_host[host] = next(iter(owners))

    def _add_host(self, host, node_id):
        if host:
            self.by_host[host.lower()] = node_id

    def _index(self, n):
        p = n.raw.get("properties") or {}
        t = n.type
        name = n.name.lower()

        ident = n.raw.get("identity") or {}
        pid = ident.get("principalId")
        if pid:
            self.by_principal[pid.lower()] = n.id
        # A user-assigned identity holds its OWN principal id, and the resource
        # that attaches it acts under that principal. Without this, every RBAC
        # edge of every resource using a user-assigned identity is lost - a very
        # common enterprise pattern, and the reverse view depends on it.
        for uai in (ident.get("userAssignedIdentities") or {}).values():
            upid = (uai or {}).get("principalId") if isinstance(uai, dict) else None
            if upid:
                self.by_principal.setdefault(upid.lower(), n.id)
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
        elif t == "microsoft.sql/servers/databases":
            # a connection string names the SERVER as host and the database by
            # name; keep the pair so config edges can land on the database itself
            self.sqldb_by_name.setdefault(name, []).append(
                (n.id.lower().rsplit("/", 2)[0], n.id))
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
        elif t == "microsoft.network/publicipaddresses":
            # 54 of these in a real estate. Its FQDN is how other configs name it.
            self._add_host(_lower((p.get("dnsSettings") or {}).get("fqdn")), n.id)
        elif t == "microsoft.operationalinsights/workspaces":
            # A Container Apps environment names its workspace by customerId, not
            # by resource id - index it so that reference can still be resolved.
            cid = _lower(p.get("customerId"))
            if cid:
                self.law_by_customer_id[cid] = n.id

        # Generic sweep, AFTER the typed branches so they keep priority: index
        # every host-looking value this resource advertises about itself. The
        # branches above cover the services whose endpoint property name we know;
        # this covers every other type, so a config reference by hostname to any
        # scanned resource resolves without a per-type rule. Same principle as
        # the generic ARM-reference pass: a general rule instead of an allowlist.
        self._index_hosts_generically(n)

    def _index_hosts_generically(self, n):
        """Claim a host as this resource's identity only when it plausibly IS one.

        The signal is the first DNS label: an Azure endpoint is almost always
        `<resource name>.<service suffix>` (kv-app.vault.azure.net,
        mycosmos.documents.azure.com). Requiring that match does two jobs at
        once - it stops a resource claiming a host it merely POINTS AT under a
        key like `url`, and it rejects shared platform infrastructure, because
        `waws-prod-am2-735.ftp.azurewebsites.windows.net` does not carry the
        name of any app running on that scale unit.
        """
        mine = _squash(n.name)
        if not mine:
            return
        for host in _self_hosts(n.raw.get("properties") or {}):
            if _squash(host.split(".")[0]) == mine:
                self._generic_claims.setdefault(host, set()).add(n.id)

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
# Azure's own topology model (Network Watcher) separates CONTAINMENT from
# ASSOCIATION, and that distinction is exactly what a blast-radius map needs.
#
# Containment runs the wrong way for dependency purposes: a VNet's `subnets[]`
# enumerate what is plugged INTO it, so following those ids turns a shared VNet
# into a hub that drags in every unrelated app on the subscription. The
# dependents already point AT the fabric (vnet-integration, in-subnet), which is
# the correct direction, so the container's own listing adds nothing but noise.
#
# Association is a genuine dependency and must NOT be dropped: a VNet really
# does depend on its DDoS protection plan, an NSG rule really does reference an
# application security group, a peering really does link two VNets. An earlier
# version excluded these fabric types wholesale and silently lost all of it.
# The denylist is therefore on PROPERTY PATHS, not on types.
_CONTAINMENT_PATHS = (
    "subnets[].ipconfigurations",          # every NIC / PE plugged into a subnet
    "subnets[].privateendpoints",
    "subnets[].serviceassociationlinks",
    "subnets[].resourcenavigationlinks",
    "subnets[].applicationgatewayipconfigurations",
    "privateendpointconnections",          # the storage/vault side listing its PEs
    "privatelinkserviceconnections",
    "properties.networkinterfaces",        # an NSG listing the NICs it protects
    "properties.subnets",                  # an NSG / route table listing its subnets
    "properties.virtualnetworklinks",      # a private DNS zone listing linked VNets
    "properties.registrationvirtualnetworklinks",
    "properties.resolutionvirtualnetworklinks",
)

# Types that watch or display a resource. They ARE affected when the target goes
# away, so they belong on the map, but they never cause a failure downstream and
# they must not be traversed through. They get their own edge kind so a reader
# can tell "this breaks with me" from "this is what I depend on", and the
# high-level view collapses them into a single box per type.
_OBSERVER_TYPES = {
    "microsoft.alertsmanagement/prometheusrulegroups",
    "microsoft.alertsmanagement/smartdetectoralertrules",
    "microsoft.insights/metricalerts",
    "microsoft.insights/scheduledqueryrules",
    "microsoft.insights/activitylogalerts",
    "microsoft.portal/dashboards",
}


def _is_containment(path):
    low = path.lower()
    return any(marker in low for marker in _CONTAINMENT_PATHS)


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


def _iter_host_mentions(obj, path):
    """Yield (host, dotted_path) for every host name mentioned anywhere under
    `obj`, whatever the property is called.

    The counterpart of `_iter_arm_ids`: a dependency is just as often written as
    an endpoint as it is as an ARM id, and outside the config workloads nothing
    was reading those. The resolver is the filter here too - a host that names
    nothing we scanned yields no edge, so this cannot invent a dependency."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _iter_host_mentions(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_host_mentions(v, f"{path}[]")
    elif isinstance(obj, str) and "." in obj:
        for token in set(re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,}", obj)):
            yield token.lower(), path


def _iter_row_arm_ids(raw):
    """Every ARM id a row references, including the ones outside `properties`.

    Two ARM shapes live at the top level and were invisible while only
    `properties` was walked:
      * `managedBy` - the resource that owns this one (a disk and its VM).
      * `identity.userAssignedIdentities` - the identities a resource runs as,
        held as KEYS of that map rather than as values.
    """
    yield from _iter_arm_ids(raw.get("properties") or {}, "properties")

    managed_by = raw.get("managedBy")
    if isinstance(managed_by, str) and managed_by.lower().startswith("/subscriptions/"):
        yield managed_by, "managedBy"

    for key in ((raw.get("identity") or {}).get("userAssignedIdentities") or {}):
        if isinstance(key, str) and key.lower().startswith("/subscriptions/"):
            yield key, "identity.userAssignedIdentities"


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
        elif t == "microsoft.compute/virtualmachines":
            _vm_edges(n, p, r, add)
        elif t == "microsoft.network/networkinterfaces":
            _nic_edges(n, p, r, add)

    # Nested-child pass. An id like .../servers/sqlsrv/databases/orders is a
    # child resource: it cannot exist without its parent, so a scanned parent
    # gets a child-of edge. Without this a nested seed (a SQL database, a
    # Service Bus queue) is an island - no property on either side names the
    # other, the relationship exists only in the id's shape.
    for n in nodes.values():
        if n.type == "microsoft.authorization/roleassignments":
            continue
        if len(n.id.split("/")) > 9:                       # provider path + child segments
            parent = r.by_resource_id(n.id.rsplit("/", 2)[0])
            if parent and parent != n.id:
                add(n.id, parent, "child-of", "nested ARM resource id")

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
        observes = n.type in _OBSERVER_TYPES
        for arm_id, path in _iter_row_arm_ids(n.raw):
            tgt = r.by_resource_id(arm_id)
            if _is_containment(path):
                continue                  # what is plugged in, not what this needs
            if (not tgt or tgt == n.id or (n.id, tgt) in known or (tgt, n.id) in known
                    or nodes[tgt].type == "microsoft.authorization/roleassignments"):
                continue
            # two resources that name each other (a NIC and its VNet) would give a
            # back-and-forth pair; keep the first direction seen, drop the mirror.
            if (tgt, n.id) in generic:
                continue
            if observes:
                kind = "observes"
            elif path == "managedBy":
                kind = "managed-by"
            elif path == "identity.userAssignedIdentities":
                kind = "runs-as"
            else:
                kind = "references"
            add(n.id, tgt, kind, path)
            known.add((n.id, tgt))
            generic.add((n.id, tgt))

    # Generic host-mention pass. An endpoint written anywhere in a resource's
    # properties names a real dependency just as an ARM id does. The config
    # workloads had a rule for this; every other type had none, so a Logic App
    # or a Data Factory naming a vault or a broker by host produced no edge at
    # all. Runs for EVERY type, config workloads included: a typed rule already
    # claimed its (source, target) pairs in `known`, so its specific edge kind
    # is never shadowed, and any host its shape-specific reader missed is still
    # picked up. Resolves only against hosts a scanned resource advertises, so
    # it cannot invent a target.
    for n in nodes.values():
        if n.type == "microsoft.authorization/roleassignments":
            continue
        for host, path in _iter_host_mentions(n.raw.get("properties") or {}, "properties"):
            tgt = r.host_lookup(host)
            if not tgt or tgt == n.id or (n.id, tgt) in known or (tgt, n.id) in known:
                continue
            if _is_containment(path):
                continue
            kind = "observes" if n.type in _OBSERVER_TYPES else domain_kind(host)[0]
            add(n.id, tgt, kind, f"references host {host} at {path}")
            known.add((n.id, tgt))

    return _dedupe(edges)


def _vm_edges(n, p, r, add):
    """A virtual machine. Compute plus network is the largest category in a real
    estate, and every one of these edges used to come out as a bare `references`
    from the generic pass - technically correct, useless to read. The property
    names are the ones ARM actually returns."""
    for nic in (p.get("networkProfile") or {}).get("networkInterfaces") or []:
        add(n.id, r.by_resource_id((nic or {}).get("id")), "has-nic",
            "networkProfile.networkInterfaces[].id")

    storage = p.get("storageProfile") or {}
    os_disk = ((storage.get("osDisk") or {}).get("managedDisk") or {}).get("id")
    add(n.id, r.by_resource_id(os_disk), "uses-disk", "storageProfile.osDisk.managedDisk.id")
    for data_disk in storage.get("dataDisks") or []:
        did = ((data_disk or {}).get("managedDisk") or {}).get("id")
        add(n.id, r.by_resource_id(did), "uses-disk", "storageProfile.dataDisks[].managedDisk.id")

    # A gallery image version is the third most common type in a real estate, and
    # "which image is this VM built from" is a question people ask during patching.
    image = (storage.get("imageReference") or {}).get("id")
    add(n.id, r.by_resource_id(image), "built-from-image", "storageProfile.imageReference.id")

    # An availability set or scale set is to a VM what a plan is to a web app.
    for key in ("availabilitySet", "virtualMachineScaleSet", "host", "hostGroup"):
        add(n.id, r.by_resource_id((p.get(key) or {}).get("id")), "hosted-on", f"{key}.id")

    boot = ((p.get("diagnosticsProfile") or {}).get("bootDiagnostics") or {}).get("storageUri")
    if boot:
        host = _host_of(boot)
        add(n.id, r.host_lookup(host), "sends-diagnostics",
            f"diagnosticsProfile.bootDiagnostics.storageUri ({host})")


def _nic_edges(n, p, r, add):
    """A network interface: the hop between a VM and the network. Its own
    `virtualMachine.id` is deliberately not followed - that is the mirror of the
    VM's `has-nic` edge, and following it would state the dependency backwards."""
    for cfg in p.get("ipConfigurations") or []:
        cp = (cfg or {}).get("properties") or {}
        add(n.id, r.by_resource_id((cp.get("subnet") or {}).get("id")), "in-subnet",
            "ipConfigurations[].properties.subnet.id")
        add(n.id, r.by_resource_id((cp.get("publicIPAddress") or {}).get("id")),
            "has-public-ip", "ipConfigurations[].properties.publicIPAddress.id")

    add(n.id, r.by_resource_id((p.get("networkSecurityGroup") or {}).get("id")),
        "protected-by", "networkSecurityGroup.id")


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

    hit_hosts = {}                # resolved node id (lower) -> the host that named it
    for word in words:
        if word in r.by_host:
            hit_hosts[r.by_host[word].lower()] = word
            add(n.id, r.by_host[word], domain_kind(word)[0], f"{label} references host {word}")
        if word in r.by_ik:
            add(n.id, r.by_ik[word], "sends-telemetry", f"{label} contains instrumentation key")

    # A SQL connection string names the server as host and the database by name.
    # When that database was scanned, land the edge on the database itself (in
    # addition to the server) - it is what a database seed's upward view needs.
    # The parent-server check keeps a same-named database under an unreferenced
    # server from matching.
    for m in re.finditer(r"(?:database|initial catalog)\s*=\s*([a-z0-9._-]+)", blob):
        for srv_id, db_id in r.sqldb_by_name.get(m.group(1), []):
            if srv_id in hit_hosts:
                add(n.id, db_id, "connects-to",
                    f"{label} connection string names database {m.group(1)} "
                    f"on referenced server host {hit_hosts[srv_id]}")

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
