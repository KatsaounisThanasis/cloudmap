import os
import sys
import json
import subprocess

try:
    import questionary
    from questionary import Style
    from rich.console import Console
    from rich.panel import Panel
except ImportError:
    questionary = None

def run_az(cmd, console=None, loading_msg="Loading..."):
    """Run an az command, optionally with a rich loading spinner."""
    def _exec():
        try:
            res = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
            return json.loads(res.stdout) if res.stdout.strip() else None
        except FileNotFoundError:
            if console:
                console.print("[bold red]Error:[/bold red] Azure CLI ('az') is not installed or not in PATH.")
            return None
        except subprocess.CalledProcessError as e:
            if console:
                console.print(f"[bold red]Azure CLI Error:[/bold red] {e.stderr}")
            return None
        except json.JSONDecodeError:
            return None

    if console and loading_msg:
        with console.status(f"[bold cyan]{loading_msg}[/bold cyan]", spinner="dots"):
            return _exec()
    return _exec()

def interactive_main():
    if questionary is None:
        print("Interactive mode requires 'questionary' and 'rich'. Install with: pip install questionary rich", file=sys.stderr)
        return 1

    console = Console()
    
    # Beautiful custom theme for the prompts
    custom_style = Style([
        ('qmark', 'fg:#00d7ff bold'),       # Token in front of the question
        ('question', 'bold'),               # Question text
        ('answer', 'fg:#00ff00 bold'),      # Submitted answer text behind the question
        ('pointer', 'fg:#00d7ff bold'),     # Pointer used in select and checkbox prompts
        ('highlighted', 'fg:#00d7ff bold'), # Pointed-at choice in select and checkbox prompts
        ('selected', 'fg:#00d7ff'),         # Style for a selected item of a checkbox
        ('separator', 'fg:#777777'),        # Separator in lists
        ('instruction', 'fg:#777777 italic')# User instructions for select, rawselect, checkbox
    ])

    console.print(Panel.fit(
        "[bold cyan]🗺️  CloudMap Interactive Wizard[/bold cyan]\n"
        "[dim]Trace the blast radius of your Azure resources with style.[/dim]",
        border_style="cyan"
    ))
    
    # 1. Get subscriptions (Optimized query: only enabled, minimal fields)
    subs = run_az("az account list --query \"[?state=='Enabled'].{name:name, id:id, isDefault:isDefault}\" -o json", console, "Fetching Azure Subscriptions...")
    if not subs:
        console.print("[bold red]No active subscriptions found. Are you logged in? Run 'az login'.[/bold red]")
        return 1
        
    # Sort so default is at the top
    subs.sort(key=lambda x: not x.get('isDefault', False))
    
    sub_choices = []
    for s in subs:
        default_tag = " [bold green](Default)[/bold green]" if s.get('isDefault') else ""
        sub_choices.append(questionary.Choice(title=f"{s['name']}{default_tag}  [dim]{s['id']}[/dim]", value=s['id']))
    
    chosen_sub = questionary.select(
        "Select an Azure Subscription:", 
        choices=sub_choices,
        style=custom_style,
        instruction="(Use arrow keys)"
    ).ask()
    
    if not chosen_sub:
        return 0
    
    # chosen_sub is already the subscription ID because we passed it in value=
    sub_id = str(chosen_sub).strip()
    
    # 2. Get resources (Optimized ARG query)
    query = """
    Resources 
    | where type in (
        'microsoft.web/sites', 
        'microsoft.containerservice/managedclusters', 
        'microsoft.app/containerapps',
        'microsoft.compute/virtualmachines',
        'microsoft.apimanagement/service',
        'microsoft.sql/servers/databases',
        'microsoft.dbforpostgresql/flexibleservers'
    ) 
    | project id, name, type, resourceGroup 
    | order by name asc
    """
    data = []
    token = None
    with console.status("[bold cyan]Scanning for Workloads via Azure Resource Graph...[/bold cyan]", spinner="dots"):
        for _ in range(40):  # Cap at 40 pages (40,000 resources)
            cmd = f"az graph query -q \"{query}\" --first 1000 --subscriptions {sub_id}"
            if token:
                cmd += f" --skip-token \"{token}\""
            res = run_az(cmd)
            
            if res is None:
                console.print("[bold red]Failed to fetch resources from ARG.[/bold red]")
                return 1
                
            data.extend(res.get("data", []))
            token = res.get("skip_token") or res.get("skipToken")
            if not token:
                break
                
    if not data:
        console.print("[bold yellow]No workloads (Web Apps / AKS / Container Apps) found in this subscription.[/bold yellow]")
        return 0
        
    # 2.5 Select Resource Group (Cascading)
    unique_rgs = sorted(list(set(r['resourceGroup'] for r in data)))
    rg_choices = [questionary.Choice(title="🌟 [All Resource Groups]", value="ALL")]
    for rg in unique_rgs:
        rg_choices.append(questionary.Choice(title=f"📁 {rg}", value=rg))
        
    selected_rg = questionary.select(
        "Select a Resource Group:", 
        choices=rg_choices, 
        style=custom_style,
        instruction="(Use arrow keys or type to search)"
    ).ask()
    
    if not selected_rg:
        return 0
        
    if selected_rg != "ALL":
        data = [r for r in data if r['resourceGroup'] == selected_rg]
        
    # 3. Select Resource
    res_choices = []
    for r in data:
        rtype = r['type'].split('/')[-1]
        icon = "☸️ " if "managedclusters" in r['type'].lower() else ("🌐" if "sites" in r['type'].lower() else "📦")
        # Format: Icon  Name   (Type)   [RG] (only show RG dim if ALL was selected)
        rg_dim = f" [dim]RG: {r['resourceGroup']}[/dim]" if selected_rg == "ALL" else ""
        display = f"{icon} {r['name'].ljust(30)} {rtype.ljust(18)}{rg_dim}"
        res_choices.append(questionary.Choice(title=display, value={"id": r['id'], "name": r['name']}))
    
    selected_res = questionary.select(
        "Select a resource to trace:", 
        choices=res_choices, 
        style=custom_style,
        instruction="(Use arrow keys or type to search)"
    ).ask()
    
    if not selected_res:
        return 0
        
    res_id = selected_res["id"]
    res_name = selected_res["name"]
        
    # 3. Enrichment Mode
    enrich = questionary.select(
        "Deep-enrich dependencies? (Parses App Configs & K8s Manifests)",
        choices=[
            questionary.Choice([("class:highlighted", "✦ auto"), ("", "   (Deep-enrich the selected resource only - "), ("class:answer", "Fast & Recommended"), ("", ")")], "auto"),
            questionary.Choice([("class:text", "✦ all"), ("", "    (Deep-enrich ALL workloads in scope - "), ("class:warning", "Slower"), ("", ")")], "all"),
            questionary.Choice([("class:text", "✦ none"), ("", "   (ARM topology only - "), ("class:error", "Misses App Settings"), ("", ")")], "none")
        ],
        style=custom_style
    ).ask()
    
    if not enrich:
        return 0
        
    # 4. Generate trace
    console.print(f"\n[bold green]🚀 Launching Trace for [white]{res_name}[/white]...[/bold green]\n")
    
    os.environ["CLOUDMAP_ALLOW_SUBSCRIPTION"] = sub_id
    
    out_drawio = f"{res_name}.drawio"
    out_html = f"{res_name}.html"
    out_mmd = f"{res_name}.mmd"
    
    args = [
        "trace", res_id,
        "--live", "--allow-live",
        "--single-sub",
        "--enrich", enrich,
        "-o", out_drawio,
        "--html", out_html,
        "--mermaid", out_mmd
    ]
    
    from .cli import main as cli_main
    return cli_main(args)
