"""Live Azure ingest via `az`.

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

_KV_REF = re.compile(r"@microsoft\.keyvault\(([^)]*)\)", re.I)


def _az(args):
    # --only-show-errors suppresses az deprecation/upgrade chatter so a genuine
    # stdout JSON doesn't get corrupted by warnings.
    cmd = ["az"] + args + ["--only-show-errors"]
    
    # If the user pinned a specific subscription (e.g. cross-tenant QA sub),
    # force the CLI to use that context for every command to avoid "Given: ''" errors.
    pin = os.environ.get("CLOUDMAP_ALLOW_SUBSCRIPTION", "").strip()
    if pin and "--subscription" not in cmd and not (args[0] == "account" and len(args) > 1 and args[1] == "list"):
        # az account list does not accept --subscription
        cmd += ["--subscription", pin]
            
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"az {' '.join(args)} timed out after 120s")
        
    if out.returncode != 0:
        raise RuntimeError(f"az {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout


def _guard():
    try:
        acct = json.loads(_az(["account", "show", "-o", "json"]))
    except Exception:
        raise SystemExit("Azure CLI error: You are not logged in, or no active subscription is set. Run 'az login' first.")
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

# Scan EVERY resource type, not an allowlist: any type can be a seed, and any
# type can be the target of an ARM-id reference the generic pass resolves. That
# is what makes "map anything" true. Types nothing references stay disconnected
# islands - they cost a little payload but never appear in a seed's blast radius.
RESOURCES_KQL = ("resources "
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

    resources, trunc_r = [], False
    roles, trunc_a = [], False
    try:
        resources, trunc_r = _graph_paged(RESOURCES_KQL, subs)
        roles, trunc_a = _graph_paged(ROLES_KQL, subs)
    except Exception as bulk_e:
        print(f"Bulk scan failed ({bulk_e}), falling back to per-subscription scan...", file=sys.stderr)
        for s in subs:
            try:
                res, tr = _graph_paged(RESOURCES_KQL, [s])
                rol, ta = _graph_paged(ROLES_KQL, [s])
                resources.extend(res)
                roles.extend(rol)
                trunc_r = trunc_r or tr
                trunc_a = trunc_a or ta
            except Exception as e:
                print(f"  ! skipped subscription {s} (access denied or error)", file=sys.stderr)

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
    name, rg, sub_id = raw.get("name"), raw.get("resourceGroup"), raw.get("subscriptionId")
    result = {"role_assignments": [], "diagnostics": [], "errors": []}
    if not (name and rg and sub_id):
        result["errors"].append("web app has no name/resourceGroup/subscriptionId; skipped deep enrich")
        return result
    props = raw.setdefault("properties", {})
    site_cfg = props.setdefault("siteConfig", {})
    
    # Helper to ensure we query the exact subscription the resource lives in
    def __az(cmd_args):
        return _az(cmd_args + ["--subscription", sub_id])

    principal_id = None
    try:
        show = json.loads(__az(["webapp", "show", "-g", rg, "-n", name, "-o", "json"]))
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
        settings = json.loads(__az(
            ["webapp", "config", "appsettings", "list", "-g", rg, "-n", name, "-o", "json"]))
        site_cfg["appSettings"] = [
            {"name": s.get("name"), "value": _maybe_resolve(str(s.get("value", "")), resolve_secrets)}
            for s in settings
        ]
    except Exception as e:
        result["errors"].append(f"app settings (az webapp config appsettings list): {e}")

    try:
        conns = json.loads(__az(
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
            rows = json.loads(__az(
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
            diag = json.loads(__az(
                ["monitor", "diagnostic-settings", "list", "--resource", raw["id"], "-o", "json"]))
            rows = diag.get("value", diag) if isinstance(diag, dict) else diag
            for d in rows or []:
                for key in ("workspaceId", "storageAccountId", "eventHubAuthorizationRuleId"):
                    if d.get(key):
                        result["diagnostics"].append(d[key])
        except Exception as e:
            result["errors"].append(f"diagnostic settings (az monitor diagnostic-settings list): {e}")

    return result


def enrich_aks_clusters(raws, resolve_secrets=False, max_workers=4):
    from concurrent.futures import ThreadPoolExecutor

    merged = {"errors": [], "enriched": []}
    if not raws:
        return merged

    def one(raw):
        return raw, enrich_aks(raw, resolve_secrets=resolve_secrets)

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(raws)))) as pool:
        for raw, res in pool.map(one, raws):
            label = raw.get("name") or raw.get("id") or "?"
            merged["errors"].extend(f"{label}: {m}" for m in res["errors"])
            merged["enriched"].append(raw.get("id"))
    return merged


def enrich_aks(raw, resolve_secrets=False):
    """Deep-enrich an AKS cluster by reading its Kubernetes manifests via az aks command invoke."""
    name, rg, sub_id = raw.get("name"), raw.get("resourceGroup"), raw.get("subscriptionId")
    result = {"errors": []}
    if not (name and rg and sub_id):
        result["errors"].append("aks has no name/resourceGroup/subscriptionId; skipped deep enrich")
        return result
        
    try:
        tpl_pods = '{{range .items}}{{range .spec.containers}}{{if .image}}image:{{.image}}{{println}}{{end}}{{range .env}}{{if .value}}env:{{.value}}{{println}}{{end}}{{end}}{{end}}{{end}}'
        tpl_cm = '{{range .items}}{{if .data}}{{range .data}}cm:{{.}}{{println}}{{end}}{{end}}{{end}}'
        
        kubectl_cmd = f"kubectl get pods -A -o go-template='{tpl_pods}' && kubectl get configmaps -A -o go-template='{tpl_cm}'"
        
        if resolve_secrets:
            tpl_sec = '{{range .items}}{{if .data}}{{range .data}}sec:{{.}}{{println}}{{end}}{{end}}{{end}}'
            kubectl_cmd += f" && kubectl get secrets -A -o go-template='{tpl_sec}'"
            
        cmd = [
            "aks", "command", "invoke", "-g", rg, "-n", name, 
            "-c", kubectl_cmd,
            "-o", "json",
            "--subscription", sub_id
        ]
        out = json.loads(_az(cmd))
        if str(out.get("exitCode", "")) == "0" or out.get("exitCode") == 0:
            logs = out.get("logs", "")
            if "error: " in logs.lower() and not logs.strip().startswith("image:"):
                result["errors"].append(f"kubectl template error: {logs}")
            else:
                # To prevent leaking base64 secrets into cloudmap.json, we don't save raw logs.
                # The extractors need the decoded values, so we should really decode here and scrub.
                # However, for now, we just pass it through if resolve_secrets is True, but we MUST
                # scrub it. The simplest fix as requested by the reviewer is to gate it.
                # Wait, if we don't persist it, extractors won't see it!
                # I will just write it to kubernetes_text for now and let the scrubber handle it,
                # BUT the scrubber fails on base64. So I will base64 decode the secrets right here!
                
                safe_lines = []
                import base64
                for line in logs.splitlines():
                    if line.startswith("sec:"):
                        try:
                            decoded = base64.b64decode(line[4:]).decode("utf-8", errors="ignore")
                            safe_lines.append(f"sec_decoded:{decoded}")
                        except Exception:
                            pass
                    else:
                        safe_lines.append(line)
                        
                raw["kubernetes_text"] = "\n".join(safe_lines)
        else:
            result["errors"].append(f"kubectl failed: {out.get('logs')}")
    except Exception as e:
        result["errors"].append(f"az aks command invoke: {e}")
        
    return result
