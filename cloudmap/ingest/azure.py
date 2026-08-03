"""Live Azure ingest via `az`.

Guarded on purpose:
  1. Production is hard-blocked by subscription-name hint, always.
  2. Live mode requires confirming the EXACT active subscription id via the
     CLOUDMAP_ALLOW_SUBSCRIPTION env var (so it can never run against the wrong
     context by accident).
  3. Tenant-wide scans exclude any subscription whose name looks like prod.

Secrets: web-app app settings / Key Vault secret values can contain credentials.
They are read in-process ONLY to derive dependency endpoints, and are never
printed and never written to any output file.
"""

import json
import os
import re
import subprocess
import sys

# Tenants to refuse outright, by id. Empty by default; a generic denylist hook
# for anyone who wants to hard-block a specific tenant. Live mode is otherwise
# ungated beyond the explicit --allow-live flag: cloudmap reads whatever
# subscription you point it at, production included. The read is read-only, but
# it IS a read of live infrastructure - --allow-live is the deliberate opt-in,
# and CLOUDMAP_ALLOW_SUBSCRIPTION (below) is an optional extra pin.
DENY_TENANTS = set()

# Types worth scanning for dependency resolution (keeps tenant-wide payload sane).
RELEVANT_TYPES = [
    "microsoft.web/sites", "microsoft.web/serverfarms",
    "microsoft.keyvault/vaults", "microsoft.storage/storageaccounts",
    "microsoft.sql/servers",
    "microsoft.dbforpostgresql/flexibleservers", "microsoft.dbforpostgresql/servers",
    "microsoft.dbformysql/flexibleservers", "microsoft.dbformysql/servers",
    "microsoft.documentdb/databaseaccounts", "microsoft.cache/redis",
    "microsoft.servicebus/namespaces", "microsoft.eventhub/namespaces",
    "microsoft.search/searchservices", "microsoft.cognitiveservices/accounts",
    "microsoft.containerregistry/registries", "microsoft.containerservice/managedclusters",
    "microsoft.app/containerapps", "microsoft.app/managedenvironments",
    "microsoft.machinelearningservices/workspaces",
    "microsoft.operationalinsights/workspaces", "microsoft.insights/components",
    "microsoft.network/virtualnetworks", "microsoft.network/privateendpoints",
    "microsoft.network/applicationgateways", "microsoft.apimanagement/service",
    "microsoft.managedidentity/userassignedidentities",
]

_KV_REF = re.compile(r"@microsoft\.keyvault\(([^)]*)\)", re.I)


def _az(args):
    # --only-show-errors suppresses az deprecation/upgrade chatter so a genuine
    # failure (the message we surface as a read-gap) is not buried in noise.
    out = subprocess.run(["az"] + args + ["--only-show-errors"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"az {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout


def _guard():
    acct = json.loads(_az(["account", "show", "-o", "json"]))
    sub_id = acct.get("id") or ""
    tenant = (acct.get("tenantId") or "").lower()

    if tenant in DENY_TENANTS:
        raise SystemExit(f"Refusing to query denied tenant {tenant}.")
    # Optional extra pin: if CLOUDMAP_ALLOW_SUBSCRIPTION is set, it must equal the
    # active subscription, so a stale `az account set` cannot silently redirect a
    # scan to the wrong place. Unset means no pin - --allow-live already said yes.
    allowed = os.environ.get("CLOUDMAP_ALLOW_SUBSCRIPTION", "").strip()
    if allowed and allowed.lower() != sub_id.lower():
        raise SystemExit(
            f"CLOUDMAP_ALLOW_SUBSCRIPTION is set to {allowed} but the active "
            f"subscription is {sub_id} ({acct.get('name')}). Refusing the mismatch."
        )
    return acct


def _target_subscriptions(active_id, tenant_wide):
    if not tenant_wide:
        return [active_id]
    subs = json.loads(_az(["account", "list", "-o", "json"]))
    out = [s["id"] for s in subs if s.get("state") == "Enabled"]
    return out or [active_id]


_PAGE_CAP = 40

RESOURCES_KQL = ("resources | where type in~ ('" + "','".join(RELEVANT_TYPES) + "') "
                 "| project id,name,type,resourceGroup,subscriptionId,location,kind,identity,properties,tags")
# Role assignments live in a separate table; pulling them tenant-wide lets us
# answer "what has access to this resource" (reverse / incident-response view).
ROLES_KQL = ("authorizationresources | where type =~ 'microsoft.authorization/roleassignments' "
             "| project id,name,type,properties")


def _graph_paged(kql, subs):
    """Run a KQL query paged via skip_token. Returns (rows, truncated) where
    truncated=True means we hit the page cap and the result is INCOMPLETE."""
    data, token = [], None
    for _ in range(_PAGE_CAP):
        args = ["graph", "query", "-q", kql, "--first", "1000"]
        if subs:
            args += ["--subscriptions"] + subs
        if token:
            args += ["--skip-token", token]
        raw = json.loads(_az(args))
        data += raw.get("data", [])
        token = raw.get("skip_token") or raw.get("skipToken")
        if not token:
            break
    return data, bool(token)


def query_live(allow_live=False, tenant_wide=True):
    if not allow_live:
        raise SystemExit("Live query requires --allow-live (fixtures are the default).")
    acct = _guard()
    subs = _target_subscriptions(acct.get("id"), tenant_wide)

    resources, trunc_r = _graph_paged(RESOURCES_KQL, subs)
    roles, trunc_a = _graph_paged(ROLES_KQL, subs)

    print(f"Scanned {len(resources)} resources + {len(roles)} role assignments "
          f"across {len(subs)} subscription(s).", file=sys.stderr)
    truncated = trunc_r or trunc_a
    if truncated:
        print(f"WARNING: pagination cap ({_PAGE_CAP} pages) reached - the graph is "
              f"INCOMPLETE. Narrow the scope (e.g. --single-sub) to see everything.",
              file=sys.stderr)
    return resources + roles, truncated


def _resolve_secret(ref_inner):
    """ref_inner is the text inside @Microsoft.KeyVault(...). Return the secret
    value (read in-process only) or "" on any failure."""
    vault = secret = None
    m = re.search(r"vaultname\s*=\s*([^;)\s]+)", ref_inner, re.I)
    if m:
        vault = m.group(1)
    m = re.search(r"secretname\s*=\s*([^;)\s]+)", ref_inner, re.I)
    if m:
        secret = m.group(1)
    m = re.search(r"secreturi\s*=\s*https://([a-z0-9\-]+)\.vault\.azure\.net/secrets/([^/;)\s]+)",
                  ref_inner, re.I)
    if m:
        vault, secret = m.group(1), m.group(2)
    if not (vault and secret):
        return ""
    try:
        return _az(["keyvault", "secret", "show", "--vault-name", vault,
                    "--name", secret, "--query", "value", "-o", "tsv"]).strip()
    except Exception:
        return ""


def _maybe_resolve(value, resolve_secrets):
    if not resolve_secrets or "@microsoft.keyvault(" not in value.lower():
        return value
    def repl(m):
        return _resolve_secret(m.group(1)) or m.group(0)
    return _KV_REF.sub(repl, value)


def enrich_webapps(raws, resolve_secrets=False, max_workers=8):
    """Deep-enrich MANY web apps concurrently, folding each app's config into its
    own `raw` dict so a later build_graph() sees it.

    Why this exists: a dependency that lives in app config (a Key Vault
    reference, a connection string, a backend hostname) appears nowhere in the
    ARM topology, so enriching only the seed makes the graph asymmetric -
    tracing an app finds the vault it reads, but tracing that vault never finds
    the app. "What breaks if I touch this" is usually asked about exactly such a
    shared resource, so the upward view is the one that needs every app enriched.

    Returns {"role_assignments": [...], "diagnostics": [(app_id, target_id)],
             "errors": [...], "enriched": [app_id]}. Errors are prefixed with the
    app name so a read gap stays attributable to one resource.

    Nothing is cached to disk: app settings and resolved secrets are credentials,
    and this tool does not write them anywhere.
    """
    from concurrent.futures import ThreadPoolExecutor

    merged = {"role_assignments": [], "diagnostics": [], "errors": [], "enriched": []}
    if not raws:
        return merged

    def one(raw):
        return raw, enrich_webapp(raw, resolve_secrets=resolve_secrets)

    # az shells out per call, so these are IO-bound: threads are enough, and the
    # cap keeps a tenant-wide pass from opening hundreds of processes at once.
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(raws)))) as pool:
        for raw, res in pool.map(one, raws):
            label = raw.get("name") or raw.get("id") or "?"
            merged["role_assignments"].extend(res["role_assignments"])
            merged["diagnostics"].extend((raw.get("id"), t) for t in res["diagnostics"])
            merged["errors"].extend(f"{label}: {m}" for m in res["errors"])
            merged["enriched"].append(raw.get("id"))
    return merged


def enrich_webapp(raw, resolve_secrets=False):
    """Fetch the parts of a web app that Resource Graph omits and fold them into
    `raw` so the extractors can read them. Returns a dict with synthetic
    role-assignment resources and diagnostic-target ids for the caller to merge.

    With resolve_secrets=True, Key Vault references in app settings are replaced
    in-memory with their resolved values so dependencies hidden behind secrets
    (DB / Storage connection strings) become visible. Values are never printed
    or written out.
    """
    name, rg = raw.get("name"), raw.get("resourceGroup")
    result = {"role_assignments": [], "diagnostics": [], "errors": []}
    if not (name and rg):
        result["errors"].append("web app has no name/resourceGroup; skipped deep enrich")
        return result
    props = raw.setdefault("properties", {})
    site_cfg = props.setdefault("siteConfig", {})

    principal_id = None
    try:
        show = json.loads(_az(["webapp", "show", "-g", rg, "-n", name, "-o", "json"]))
        ident = show.get("identity") or {}
        principal_id = ident.get("principalId")
        if principal_id:
            raw["identity"] = {"type": ident.get("type"), "principalId": principal_id}
        if show.get("virtualNetworkSubnetId"):
            props["virtualNetworkSubnetId"] = show["virtualNetworkSubnetId"]
        fx = (show.get("siteConfig") or {}).get("linuxFxVersion")
        if fx:
            site_cfg["linuxFxVersion"] = fx
    except Exception as e:
        result["errors"].append(f"identity/vnet/runtime (az webapp show): {e}")

    try:
        settings = json.loads(_az(
            ["webapp", "config", "appsettings", "list", "-g", rg, "-n", name, "-o", "json"]))
        site_cfg["appSettings"] = [
            {"name": s.get("name"), "value": _maybe_resolve(str(s.get("value", "")), resolve_secrets)}
            for s in settings
        ]
    except Exception as e:
        result["errors"].append(f"app settings (az webapp config appsettings list): {e}")

    try:
        conns = json.loads(_az(
            ["webapp", "config", "connection-string", "list", "-g", rg, "-n", name, "-o", "json"]))
        cs = []
        items = conns.items() if isinstance(conns, dict) else [(v.get("name"), v) for v in conns]
        for k, v in items:
            raw_val = (v or {}).get("value", "") if isinstance(v, dict) else ""
            cs.append({"name": k, "connectionString": _maybe_resolve(str(raw_val), resolve_secrets)})
        props["connectionStrings"] = cs
    except Exception as e:
        result["errors"].append(f"connection strings (az webapp config connection-string list): {e}")

    if principal_id:
        try:
            rows = json.loads(_az(
                ["role", "assignment", "list", "--assignee", principal_id, "--all", "-o", "json"]))
            for i, r in enumerate(rows):
                result["role_assignments"].append({
                    "id": r.get("id") or f"ra-{name}-{i}",
                    "name": r.get("name") or f"ra-{name}-{i}",
                    "type": "microsoft.authorization/roleassignments",
                    "properties": {
                        "principalId": principal_id,
                        "roleDefinitionName": r.get("roleDefinitionName"),
                        "roleDefinitionId": r.get("roleDefinitionId"),
                        "scope": r.get("scope"),
                    },
                })
        except Exception as e:
            result["errors"].append(f"RBAC role assignments (az role assignment list): {e}")

    if raw.get("id"):
        try:
            diag = json.loads(_az(
                ["monitor", "diagnostic-settings", "list", "--resource", raw["id"], "-o", "json"]))
            rows = diag.get("value", diag) if isinstance(diag, dict) else diag
            for d in rows or []:
                for key in ("workspaceId", "storageAccountId", "eventHubAuthorizationRuleId"):
                    if d.get(key):
                        result["diagnostics"].append(d[key])
        except Exception as e:
            result["errors"].append(f"diagnostic settings (az monitor diagnostic-settings list): {e}")

    return result
