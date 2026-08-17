import os
import sys
import json
import subprocess

try:
    import questionary
except ImportError:
    questionary = None

def run_az(cmd):
    try:
        res = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
        return json.loads(res.stdout)
    except subprocess.CalledProcessError as e:
        print(f"Azure CLI error: {e.stderr}", file=sys.stderr)
        return []
    except json.JSONDecodeError:
        return []

def interactive_main():
    if questionary is None:
        print("Interactive mode requires 'questionary'. Install with: pip install questionary", file=sys.stderr)
        return 1

    print("🗺️  Welcome to CloudMap Interactive Wizard\n")
    
    # 1. Get subscriptions
    subs = run_az("az account list --all")
    if not subs:
        print("No subscriptions found. Are you logged in? Run 'az login'.", file=sys.stderr)
        return 1
        
    sub_choices = [f"{s['name']} ({s['id']})" for s in subs]
    
    chosen_sub = questionary.select(
        "Select an Azure Subscription:", 
        choices=sub_choices,
        use_search=True
    ).ask()
    
    if not chosen_sub:
        return 0
    
    sub_id = chosen_sub.split("(")[-1].strip(")")
    
    # 2. Get resources
    print("\nFetching trace-able workloads in subscription...")
    query = "Resources | where type in ('microsoft.web/sites', 'microsoft.containerservice/managedclusters', 'microsoft.app/containerapps') | project name, type, resourceGroup | order by name asc"
    res = run_az(f"az graph query -q \"{query}\" --subscriptions {sub_id}")
    
    data = res.get("data", [])
    if not data:
        print("No workloads (Web Apps / AKS / Container Apps) found in this subscription.", file=sys.stderr)
        return 0
        
    res_choices = [f"{r['name']} [{r['type'].split('/')[-1]}] (RG: {r['resourceGroup']})" for r in data]
    
    chosen_res = questionary.select(
        "Select a resource to trace:", 
        choices=res_choices, 
        use_search=True
    ).ask()
    
    if not chosen_res:
        return 0
        
    res_name = chosen_res.split(" ")[0]
    
    # 3. Enrichment Mode
    enrich = questionary.select(
        "Deep-enrich dependencies? (Parses App Configs & K8s Manifests)",
        choices=[
            questionary.Choice("auto (Deep-enrich the selected resource only - Recommended)", "auto"),
            questionary.Choice("all (Deep-enrich ALL workloads in scope - Slower)", "all"),
            questionary.Choice("none (ARM topology only)", "none")
        ]
    ).ask()
    
    if not enrich:
        return 0
        
    # 4. Generate trace
    print(f"\n🚀 Tracing {res_name}...")
    
    # Set the security guard
    os.environ["CLOUDMAP_ALLOW_SUBSCRIPTION"] = sub_id
    
    out_drawio = f"{res_name}.drawio"
    out_html = f"{res_name}.html"
    out_mmd = f"{res_name}.mmd"
    
    args = [
        "trace", res_name,
        "--live", "--allow-live",
        "--single-sub",
        "--enrich", enrich,
        "-o", out_drawio,
        "--html", out_html,
        "--mermaid", out_mmd
    ]
    
    from .cli import main as cli_main
    return cli_main(args)
