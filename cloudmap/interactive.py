import os
import sys
import json
import subprocess

try:
    import questionary
    from questionary import Style
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
except ImportError:
    questionary = None

def run_az(cmd, console=None, loading_msg="Loading..."):
    """Run an az command, optionally with a rich loading spinner."""
    def _exec():
        try:
            res = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
            return json.loads(res.stdout) if res.stdout.strip() else []
        except subprocess.CalledProcessError as e:
            if console:
                console.print(f"[bold red]Azure CLI Error:[/bold red] {e.stderr}")
            return []
        except json.JSONDecodeError:
            return []

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
        sub_choices.append(questionary.Choice(title=f"{s['name']}  [dim]{s['id']}[/dim]", value=s['id']))
    
    sub_id = questionary.select(
        "Select an Azure Subscription:", 
        choices=sub_choices,
        style=custom_style,
        use_search=True,
        instruction="(Type to search, Enter to select)"
    ).ask()
    
    if not sub_id:
        return 0
    
    # 2. Get resources (Optimized ARG query)
    query = """
    Resources 
    | where type in ('microsoft.web/sites', 'microsoft.containerservice/managedclusters', 'microsoft.app/containerapps') 
    | project name, type, resourceGroup 
    | order by name asc
    """
    res = run_az(f"az graph query -q \"{query}\" --subscriptions {sub_id}", console, "Scanning for Workloads via Azure Resource Graph...")
    
    data = res.get("data", [])
    if not data:
        console.print("[bold yellow]No workloads (Web Apps / AKS / Container Apps) found in this subscription.[/bold yellow]")
        return 0
        
    res_choices = []
    for r in data:
        rtype = r['type'].split('/')[-1]
        icon = "☸️ " if "managedclusters" in r['type'].lower() else ("🌐" if "sites" in r['type'].lower() else "📦")
        # Format: Icon  Name   (Type)   [RG]
        display = f"{icon} {r['name'].ljust(30)} {rtype.ljust(18)} [dim]RG: {r['resourceGroup']}[/dim]"
        res_choices.append(questionary.Choice(title=display, value=r['name']))
    
    res_name = questionary.select(
        "Select a resource to trace:", 
        choices=res_choices, 
        style=custom_style,
        use_search=True,
        instruction="(Type to search workloads)"
    ).ask()
    
    if not res_name:
        return 0
        
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
