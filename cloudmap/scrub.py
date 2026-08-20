"""Turn a captured raw export into a fixture that can be committed.

Why this exists: every test in this repo runs on data we wrote ourselves, which
only proves the extractors agree with our imagination. A *real* export, scrubbed,
turns "given this real input the map MUST contain these edges" into a test that
can actually fail. That is the difference between trust-by-design and
trust-by-evidence, and PLAN.md names it as the weakest link.

The hard part is that scrubbing must not destroy the very thing under test. An
app setting says `https://kv-payments.vault.azure.net/`; the vault resource is
named `kv-payments`; connecting those two IS the extractor's job. So this is a
GLOBAL, consistent substitution: one pseudonym per real token, applied to every
string in the document. Rename the vault and the app setting follows it.

Two things are deliberately preserved because the rules depend on them:
  - service domain suffixes (`.vault.azure.net`, `.database.windows.net`, ...),
    which is how `_DOMAIN_KIND` decides what an edge means;
  - instrumentation-key GUIDs, which correlate an app to its App Insights - they
    are pseudonymised consistently rather than redacted, so the link survives.

Credentials get the opposite treatment: they carry no structure worth keeping, so
password / key / SAS fragments are REDACTED in place while the host and database
around them survive, pseudonymised.

The mapping is never written to disk. It is the re-identification key; keeping it
would undo the point of the exercise.

This is a scrubber, not a proof. READ THE OUTPUT before you commit it.
"""

import json
import re

from .extract.extractors import ROLE_NAMES

# Built-in role definition GUIDs are public Azure constants, identical in every
# tenant, and the extractor resolves role NAMES from them. Pseudonymising them
# would turn "AcrPull" into "custom role" and quietly degrade the fixture.
KEEP_GUIDS = {g.lower() for g in ROLE_NAMES}

# Pseudonym prefixes: a fixture is easier to reason about when the fake name
# still says what the resource is.
ABBREV = {
    "microsoft.web/sites": "app",
    "microsoft.web/serverfarms": "plan",
    "microsoft.keyvault/vaults": "kv",
    "microsoft.storage/storageaccounts": "st",
    "microsoft.sql/servers": "sql",
    "microsoft.dbforpostgresql/flexibleservers": "pg",
    "microsoft.dbforpostgresql/servers": "pg",
    "microsoft.dbformysql/flexibleservers": "mysql",
    "microsoft.dbformysql/servers": "mysql",
    "microsoft.documentdb/databaseaccounts": "cosmos",
    "microsoft.cache/redis": "redis",
    "microsoft.servicebus/namespaces": "sb",
    "microsoft.eventhub/namespaces": "eh",
    "microsoft.search/searchservices": "srch",
    "microsoft.cognitiveservices/accounts": "ai",
    "microsoft.containerregistry/registries": "acr",
    "microsoft.containerservice/managedclusters": "aks",
    "microsoft.operationalinsights/workspaces": "law",
    "microsoft.insights/components": "appi",
    "microsoft.network/virtualnetworks": "vnet",
    "microsoft.network/privateendpoints": "pe",
    "microsoft.network/applicationgateways": "agw",
    "microsoft.apimanagement/service": "apim",
    "microsoft.managedidentity/userassignedidentities": "mi",
    "microsoft.app/containerapps": "ca",
    "microsoft.app/managedenvironments": "cae",
    "microsoft.authorization/roleassignments": "ra",
}

# Host suffixes that must survive verbatim: the extractor reads the suffix to
# decide what kind of dependency a hostname is.
AZURE_SUFFIXES = (
    "vault.azure.net", "core.windows.net", "database.windows.net",
    "postgres.database.azure.com", "mysql.database.azure.com",
    "documents.azure.com", "redis.cache.windows.net", "servicebus.windows.net",
    "search.windows.net", "openai.azure.com", "cognitiveservices.azure.com",
    "azurecr.io", "azurewebsites.net", "azure-api.net", "azureedge.net",
    "azurecontainerapps.io", "monitor.azure.com", "applicationinsights.azure.com",
)

_GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Hosts are only hunted where a host can legitimately appear. A bare
# "looks like a hostname" regex also matches `properties.serverFarmId` and
# `microsoft.web/sites`, and pseudonymising those would shred the document.
_AZURE_HOST = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:" +
                         "|".join(re.escape(s) for s in AZURE_SUFFIXES) + r")\b", re.I)
_URL_HOST = re.compile(r"(?i)\bhttps?://([a-z0-9.-]+\.[a-z]{2,})")
_CONN_HOST = re.compile(r"(?i)\b(?:server|host|hostname|data source)\s*=\s*"
                        r"([a-z0-9.-]+\.[a-z]{2,})")

# Credential shapes. `instrumentationkey` is excluded on purpose: it is a
# correlation id the extractor needs, and the GUID pass pseudonymises it.
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(password|pwd)\s*=\s*[^;,\"'\s]+"),
    re.compile(r"(?i)\b(accountkey|sharedaccesskey|primarykey|secondarykey|"
               r"apikey|api[-_]?key|client[-_]?secret|access[-_]?key)\s*=\s*[^;,\"'\s]+"),
    re.compile(r"(?i)\b(sig)=[^&;,\"'\s]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*"),
    # Long opaque blobs: base64-ish keys that carry no structure worth keeping.
    # `/` is deliberately NOT in the class even though it is a base64 character:
    # with it, `.../providers/Microsoft.ContainerService/managedClusters/x` reads
    # as one 40-char blob and the scrubber shreds ARM ids. A real key still has a
    # 40+ run between its slashes. The digit lookahead keeps long words out.
    re.compile(r"(?<![A-Za-z0-9+/])(?=[A-Za-z0-9+]*\d)[A-Za-z0-9+]{40,}={0,2}"
               r"(?![A-Za-z0-9+])"),
]

# A token shorter than this substitutes inside unrelated words more often than it
# hides anything, so it is reported instead of applied.
MIN_TOKEN = 3

# ARM path segments. A resource unluckily named "sites" would otherwise rewrite
# every `providers/Microsoft.Web/sites/...` id in the document and corrupt it.
RESERVED = {
    "sites", "servers", "vaults", "providers", "subscriptions", "resourcegroups",
    "components", "accounts", "service", "services", "registries", "namespaces",
    "workspaces", "serverfarms", "redis", "virtualnetworks", "subnets",
    "privateendpoints", "applicationgateways", "managedclusters", "databaseaccounts",
    "flexibleservers", "searchservices", "roleassignments", "containerapps",
    "userassignedidentities", "managedenvironments", "storageaccounts",
}

# Values that describe the schema rather than the tenant: substituting inside
# them can only do damage.
PROTECTED_KEYS = {"type", "location"}


def _private_ip(ip):
    """Private ranges are topology, not identity - keep them, they are the whole
    point of a network diagram."""
    p = ip.split(".")
    if len(p) != 4 or not all(x.isdigit() for x in p):
        return False
    a, b = int(p[0]), int(p[1])
    return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168) or a == 127


class _Namer:
    """Hands out stable pseudonyms, one per distinct real value. Stability is the
    whole trick: the same real token always becomes the same fake one, wherever
    it appears, which is what keeps a reference and its target correlated."""

    def __init__(self):
        self.map = {}
        self._counts = {}

    def custom(self, real, counter, template):
        key = str(real).lower()
        if key not in self.map:
            self._counts[counter] = self._counts.get(counter, 0) + 1
            self.map[key] = template.format(n=self._counts[counter])
        return self.map[key]

    def get(self, real, prefix):
        return self.custom(real, prefix, prefix + "{n}")

    def guid(self, real):
        return self.custom(real, "guid", "{n:08d}-0000-0000-0000-000000000000")


def redact_credentials(text):
    """Blank out credential fragments in place. Returns (text, n_redactions).

    Only a NAMED key keeps its head: `AccountKey=<secret>` stays readable as
    `AccountKey=REDACTED`, which is what makes a scrubbed export still legible.
    A bare blob is replaced whole - splitting it on `=` would treat base64
    PADDING as if it were a key name and write the secret back out verbatim
    (`<secret>==` -> `<secret>=REDACTED`). That bug shipped a live storage key
    into a capture; the head is now taken from the pattern's own group, never
    guessed from the text.
    """
    total = 0
    for pat in _SECRET_PATTERNS:
        named = pat.groups > 0

        def repl(m, named=named):
            return f"{m.group(1)}=REDACTED" if named else "REDACTED"

        text, n = pat.subn(repl, text)
        total += n
    return text, total


def _walk(obj, fn):
    """Apply fn to every string in a nested structure, keys included - except the
    values of PROTECTED_KEYS, which describe the schema, not the tenant."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            nk = fn(k) if isinstance(k, str) else k
            out[nk] = v if (isinstance(k, str) and k in PROTECTED_KEYS
                            and isinstance(v, str)) else _walk(v, fn)
        return out
    if isinstance(obj, list):
        return [_walk(v, fn) for v in obj]
    if isinstance(obj, str):
        return fn(obj)
    return obj


def collect_tokens(resources, namer):
    """Build the real -> pseudonym map from the structured fields, where we know
    what a value *is*, rather than guessing from free text."""
    hosts = {}
    for r in resources:
        if not isinstance(r, dict):
            continue
        rtype = str(r.get("type") or "").lower()
        prefix = ABBREV.get(rtype) or (rtype.rsplit("/", 1)[-1][:6] or "res")
        name = r.get("name")
        pseudo = namer.get(name, prefix + "-") if name else None
        if r.get("resourceGroup"):
            namer.get(r["resourceGroup"], "rg-")
        if r.get("subscriptionId"):
            namer.guid(r["subscriptionId"])

        # Host fields: keep the service suffix, replace the identifying label.
        props = r.get("properties") if isinstance(r.get("properties"), dict) else {}
        candidates = [props.get("defaultHostName"), props.get("fullyQualifiedDomainName"),
                      props.get("vaultUri"), props.get("loginServer"),
                      props.get("documentEndpoint"), props.get("hostName")]
        candidates.extend(props.get("hostNames") or [])       # custom domains
        for host in candidates:
            if not isinstance(host, str) or "." not in host:
                continue
            host = host.replace("https://", "").replace("http://", "").strip("/").lower()
            if host in hosts:
                continue
            label, suffix = host.split(".", 1)
            if suffix.endswith(AZURE_SUFFIXES) and pseudo:
                hosts[host] = f"{pseudo}.{suffix}"
            else:
                hosts[host] = namer.custom(host, "host", "host{n}.example.invalid")
    namer.map.update(hosts)
    return namer.map


def scrub(resources):
    """Pseudonymise a raw export. Returns (resources, stats).

    Order matters: redact first so a secret never enters the mapping, then map
    the identifiers that survive, then substitute everywhere at once."""
    for r in resources:
        if isinstance(r, dict) and "kubernetes_text" in r:
            del r["kubernetes_text"]
            
    text = json.dumps(resources)
    text, redactions = redact_credentials(text)
    resources = json.loads(text)

    namer = _Namer()
    collect_tokens(resources, namer)

    # Free-text sweep for identifiers the structured pass cannot see: references
    # to resources outside the scan, GUIDs, people, endpoints.
    for m in _GUID.findall(text):
        if m.lower() not in KEEP_GUIDS:
            namer.guid(m)
    for m in _EMAIL.findall(text):
        namer.custom(m, "user", "user{n}@example.invalid")
    for m in _IPV4.findall(text):
        if not _private_ip(m):                      # private ranges are topology, keep them
            namer.custom(m, "ip", "203.0.113.{n}")
    for m in _AZURE_HOST.findall(text):
        h = m.lower()
        if h not in namer.map:
            # Outside the scanned set, so no resource lends it a name - but the
            # service suffix must survive, it is what the edge kind is read from.
            namer.custom(h, "ext", "ext{n}." + h.split(".", 1)[1])
    for m in _URL_HOST.findall(text) + _CONN_HOST.findall(text):
        h = m.lower()
        if h not in namer.map and not h.endswith(AZURE_SUFFIXES):
            namer.custom(h, "host", "host{n}.example.invalid")

    mapping, skipped = {}, []
    for real, pseudo in namer.map.items():
        if len(real) < MIN_TOKEN or real in RESERVED:
            skipped.append(real)       # unsafe to replace - reported, never hidden
            continue
        mapping[real] = pseudo

    if mapping:
        # Longest first: a full hostname must win over the bare resource name
        # inside it. The lookarounds stop a name matching inside a longer word.
        alt = "|".join(re.escape(t) for t in sorted(mapping, key=len, reverse=True))
        rx = re.compile(r"(?<![A-Za-z0-9])(?:" + alt + r")(?![A-Za-z0-9])", re.I)
        resources = _walk(resources, lambda s: rx.sub(lambda m: mapping[m.group(0).lower()], s))

    stats = {
        "resources": len(resources) if isinstance(resources, list) else 0,
        "tokens": len(mapping),
        "redactions": redactions,
        "short_tokens_left_alone": sorted(set(skipped)),
    }
    return resources, stats
