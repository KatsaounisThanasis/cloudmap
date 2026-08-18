"""Render a graph as an editable .drawio file using native Azure2 icons.

Icons are referenced as draw.io's built-in Azure2 image shapes
(`img/lib/azure2/<category>/<Icon>.svg`) - we emit the style string, draw.io
supplies the SVG, so no icon assets are shipped in this repo. Any resource type
without a mapped icon falls back to a labelled rounded box (never a broken
image). Entries marked (best-effort) may need a filename tweak if the icon
renders broken in your draw.io build.
"""

from xml.sax.saxutils import quoteattr

# Verified against jgraph/drawio src/main/webapp/img/lib/azure2/ (folder + filename).
AZURE_ICON = {
    "microsoft.web/sites": "compute/App_Services.svg",
    "microsoft.web/serverfarms": "app_services/App_Service_Plans.svg",
    "microsoft.keyvault/vaults": "security/Key_Vaults.svg",
    "microsoft.containerservice/managedclusters": "compute/Kubernetes_Services.svg",
    "microsoft.storage/storageaccounts": "storage/Storage_Accounts.svg",
    "microsoft.network/virtualnetworks": "networking/Virtual_Networks.svg",
    "microsoft.network/applicationgateways": "networking/Application_Gateways.svg",
    "microsoft.network/privateendpoints": "networking/Private_Endpoint.svg",
    "microsoft.managedidentity/userassignedidentities": "identity/Managed_Identities.svg",
    "microsoft.containerregistry/registries": "containers/Container_Registries.svg",
    "microsoft.operationalinsights/workspaces": "analytics/Log_Analytics_Workspaces.svg",
    "microsoft.sql/servers": "databases/SQL_Server.svg",
    "microsoft.insights/components": "devops/Application_Insights.svg",
}

ICON_STYLE = ("shape=image;html=1;image=img/lib/azure2/{path};"
              "verticalLabelPosition=bottom;verticalAlign=top;aspect=fixed;fontSize=11;")
BOX_STYLE = "rounded=1;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;fontSize=11;"
EXT_STYLE = ("rounded=1;whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#999999;"
             "dashed=1;fontColor=#666666;fontSize=11;")
SEED_EXTRA = "strokeColor=#d79b00;strokeWidth=3;"
EDGE_STYLE = ("edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;"
              "fontSize=10;fontColor=#555555;")
# Model-proposed edges are drawn dashed + red so a guess never looks like a fact.
EDGE_MODEL_STYLE = EDGE_STYLE + "dashed=1;strokeColor=#b85450;fontColor=#b85450;"
# RBAC edges are drawn orange/thick to highlight security access paths
EDGE_RBAC_STYLE = EDGE_STYLE + "strokeColor=#d79b00;fontColor=#b07d00;strokeWidth=2;"


def _short_type(t):
    return t.split("/")[-1] if "/" in t else t


def to_drawio(graph, seed_id):
    dist = graph.distances or {n: 0 for n in graph.nodes}

    layers = {}
    for nid in graph.nodes:
        layers.setdefault(dist.get(nid, 0), []).append(nid)

    xstep, ystep = 240, 120
    pos = {}
    
    # Calculate max height to vertically center smaller layers
    max_rows = max((len(col) for col in layers.values()), default=0)
    max_height = max_rows * ystep
    
    for layer in sorted(layers):
        col = sorted(layers[layer], key=lambda i: graph.nodes[i].name)
        layer_height = len(col) * ystep
        y_offset = (max_height - layer_height) // 2
        
        for row, nid in enumerate(col):
            pos[nid] = (60 + layer * xstep, 60 + y_offset + row * ystep)

    cells, idmap = [], {}
    for i, nid in enumerate(graph.nodes):
        cid = f"n{i}"
        idmap[nid] = cid
        n = graph.nodes[nid]
        label = f"{n.name} ({_short_type(n.type)})"
        icon = None if n.external else AZURE_ICON.get(n.type)
        if icon:
            style, w, h = ICON_STYLE.format(path=icon), 48, 48
        elif n.external:
            style, w, h = EXT_STYLE, 180, 50
        else:
            style, w, h = BOX_STYLE, 170, 50
        if nid == seed_id:
            style += SEED_EXTRA
        x, y = pos[nid]
        
        attrs = [
            f'id="{cid}"',
            f'label={quoteattr(label)}',
            f'type={quoteattr(n.type)}',
        ]
        if n.resource_group:
            attrs.append(f'ResourceGroup={quoteattr(n.resource_group)}')
        if n.location:
            attrs.append(f'Location={quoteattr(n.location)}')
        if n.subscription:
            attrs.append(f'Subscription={quoteattr(n.subscription)}')
        if n.note:
            attrs.append(f'Note={quoteattr(n.note)}')
        if not str(nid).startswith("type::") and not str(nid).startswith("ext::"):
            attrs.append(f'ARM_ID={quoteattr(str(nid))}')
            
        attr_str = " ".join(attrs)
        
        cells.append(
            f'        <object {attr_str}>\n'
            f'          <mxCell style={quoteattr(style)} vertex="1" parent="1">\n'
            f'            <mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/>\n'
            f'          </mxCell>\n'
            f'        </object>'
        )

    for j, e in enumerate(graph.edges):
        s, t = idmap.get(e.source), idmap.get(e.target)
        if not (s and t):
            continue
        model = e.origin == "model"
        rbac = "role:" in e.kind.lower()
        
        if model:
            style = EDGE_MODEL_STYLE
        elif rbac:
            style = EDGE_RBAC_STYLE
        else:
            style = EDGE_STYLE
            
        label = e.kind + ("  (model)" if model else "")
        cells.append(
            f'        <mxCell id="e{j}" value={quoteattr(label)} '
            f'style={quoteattr(style)} edge="1" parent="1" '
            f'source="{s}" target="{t}"><mxGeometry relative="1" as="geometry"/></mxCell>'
        )

    body = "\n".join(cells)
    return (
        '<mxfile host="cloudmap">\n'
        '  <diagram id="cloudmap" name="Blast radius">\n'
        '    <mxGraphModel dx="800" dy="600" grid="1" gridSize="10" guides="1" '
        'tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
        'pageWidth="1600" pageHeight="1200" math="0" shadow="0">\n'
        '      <root>\n'
        '        <mxCell id="0"/>\n'
        '        <mxCell id="1" parent="0"/>\n'
        f'{body}\n'
        '      </root>\n'
        '    </mxGraphModel>\n'
        '  </diagram>\n'
        '</mxfile>\n'
    )
