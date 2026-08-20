"""Render a graph as ONE self-contained, interactive HTML file.

No server, no CDN, no install: the data, the official Azure icons and the whole
viewer (SVG + vanilla JS + CSS) are inlined, so a developer can open the .html
straight from disk (file://). That is also what keeps it local-first - nothing is
fetched at view time.

Styled like an architecture diagram, not a web app: white canvas with a light
grid, each resource is its real Azure icon (the same azure2 set draw.io uses,
embedded via render/azure_icons.py) with the name beneath it, edges are thin and
grey. Interactivity on top: pan/zoom, search, click a resource to focus its blast
radius; a side panel shows each dependency with its relationship, whether it is
verified or a model guess, and the evidence behind it. Model-proposed edges are
dashed and red so a guess never looks like a fact.
"""

import json

from .azure_icons import icon_for


def to_html(graph, seed_id, meta=None):
    meta = meta or {}
    dist = graph.distances or {}
    nodes = [
        {
            "id": n.id,
            "name": n.name,
            "type": n.type,
            "rg": n.resource_group,
            "location": n.location,
            "hops": dist.get(n.id, 0) or 0,
            "external": bool(n.external),
            "note": n.note,
            "seed": n.id == seed_id,
        }
        for n in graph.nodes.values()
    ]
    edges = [
        {"source": e.source, "target": e.target, "kind": e.kind,
         "origin": e.origin, "evidence": e.evidence}
        for e in graph.edges
    ]
    # only the icons of types present in THIS map are embedded
    icons = {}
    for n in graph.nodes.values():
        if not n.external and n.type not in icons:
            svg = icon_for(n.type)
            if svg:
                icons[n.type] = svg
    seed = graph.nodes.get(seed_id)
    data = {
        "seed": seed_id,
        "seedName": seed.name if seed else seed_id,
        "meta": {
            "complete": not (meta.get("truncated") or meta.get("read_gaps")
                             or meta.get("blind_spots")),
            "truncated": bool(meta.get("truncated")),
            "read_gaps": list(meta.get("read_gaps") or []),
            "blind_spots": list(meta.get("blind_spots") or []),
            "external": sum(1 for n in graph.nodes.values() if n.external),
            "model_edges": sum(1 for e in graph.edges if e.origin == "model"),
            "nodes": len(graph.nodes),
            "edges": len(graph.edges),
        },
        "nodes": nodes,
        "edges": edges,
        "icons": icons,
    }
    # "<\/" guards against a literal </script> ever appearing inside the data.
    data_json = json.dumps(data).replace("</", "<\\/")
    return _TEMPLATE.replace("/*__DATA__*/null", data_json)


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cloudmap</title>
<style>
  :root{
    --bg: #f3f6f9;
    --card: #ffffff;
    --ink: #1f2328;
    --muted: #57606a;
    --line: #d0d7de;
    --edge: #8c959f;
    --elabel: #424a53;
    --model: #cf222e;
    --accent: #0969da;
    --seed: #bf8700;
  }
  body.dark {
    --bg: #0d1117;
    --card: #161b22;
    --ink: #e6edf3;
    --muted: #7d8590;
    --line: #30363d;
    --edge: #8b949e;
    --elabel: #c9d1d9;
    --model: #ff7b72;
    --accent: #2f81f7;
    --seed: #e3b341;
  }
  * {box-sizing:border-box}
  body{margin:0;padding:0;background:var(--bg);color:var(--ink);font:13px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;height:100vh;display:flex;flex-direction:column;overflow:hidden}
  header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:10px 16px;
         background:var(--card);border-bottom:1px solid var(--line);position:relative;z-index:10}
  header h1{font-size:15px;margin:0;font-weight:600}
  header h1 b{color:var(--accent);font-weight:700}
  header h1 span{color:var(--muted);font-weight:500}
  .search input{background:var(--card);border:1px solid var(--line);color:var(--ink);
     border-radius:6px;padding:5px 10px;width:200px;outline:none;font-size:13px}
  .search input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(9,105,218,.15)}
  .badges{display:flex;gap:7px;flex-wrap:wrap;align-items:center;margin-left:auto}
  .badge{font-size:11.5px;padding:3px 9px;border-radius:999px;background:#f6f8fa;
         color:var(--muted);border:1px solid var(--line)}
  .badge.warn{background:var(--card)8f0;color:#bc4c00;border-color:#f3c795}
  .badge.ok{background:#f0fff4;color:#1a7f37;border-color:#a5d9b3}
  .toggles{display:flex;gap:12px;align-items:center;font-size:12px;color:var(--muted)}
  .toggles label{display:flex;gap:5px;align-items:center;cursor:pointer;user-select:none}
  #stage{display:flex;height:calc(100vh - 47px)}
  #wrap{flex:1;position:relative;overflow:hidden;
        background-image:radial-gradient(var(--grid) 1px,transparent 1px);
        background-size:16px 16px}
  #graph{width:100%;height:100%;cursor:grab;touch-action:none}
  #graph.grabbing{cursor:grabbing}
  .zoom{position:absolute;left:14px;bottom:14px;display:flex;flex-direction:column;
        gap:6px;z-index:6}
  .zoom button{width:32px;height:32px;border-radius:6px;border:1px solid var(--line);
        background:var(--card);color:var(--ink);font-size:16px;cursor:pointer;line-height:1}
  .zoom button:hover{border-color:var(--accent);color:var(--accent)}
  .legend{position:absolute;right:14px;bottom:14px;display:flex;gap:14px;
        background:var(--card);border:1px solid var(--line);border-radius:8px;
        padding:7px 12px;font-size:11.5px;color:var(--muted);z-index:6}
  .lg{display:inline-flex;gap:6px;align-items:center}
  .sw{width:22px;height:0;border-top:1.6px solid var(--edge)}
  .sw.model{border-top:2px dashed var(--model)}
  .sw.box{width:12px;height:12px;border:2px solid var(--seed);border-radius:3px}
  aside{width:340px;background:var(--card);border-left:1px solid var(--line);background:var(--card);
        padding:18px;overflow:auto}
  aside.empty .detail{display:none}
  aside .hint{color:var(--muted)}
  aside h2{font-size:15px;margin:0 0 3px;display:flex;align-items:center;gap:8px}
  aside h2 .hico{width:22px;height:22px;flex:none}
  aside .sub{color:var(--muted);font-size:12px;margin-bottom:14px;word-break:break-all}
  .chip{font-size:10.5px;padding:2px 8px;border-radius:999px;color:#fff;font-weight:600}
  .kv{display:flex;gap:8px;font-size:12.5px;margin:3px 0}
  .kv b{color:var(--muted);font-weight:600;min-width:66px}
  .deps{margin-top:16px}
  .deps h3{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 9px}
  .dep{border:1px solid var(--line);border-radius:8px;padding:9px 11px;margin-bottom:9px;background:var(--card)}
  .dep:hover{border-color:#9aa4af}
  .dep .top{display:flex;justify-content:space-between;gap:8px;align-items:center}
  .dep .name{font-weight:600}
  .dep .kind{color:#454f59;font-size:12px;margin-top:2px}
  .dep .ev{color:var(--muted);font-size:11px;margin-top:5px;font-style:italic}
  .arrow{color:var(--muted);font-size:11px}
  .tag{font-size:10px;padding:1px 8px;border-radius:999px;white-space:nowrap;font-weight:600}
  .tag.verified{background:#ddf4ff;color:#0969da}
  .tag.model{background:#ffebe9;color:#cf222e}
  /* graph */
  .edge path{fill:none;stroke:var(--edge);stroke-width:1.4}
  .edge .elabel{fill:var(--elabel);font-size:10.5px;paint-order:stroke;
        stroke:var(--bg);stroke-width:3.5px;font-weight:500}
        
  .edge.k-readssecret path { stroke: #d18f00; }
  .edge.k-readssecret .elabel { fill: #b57a00; }
  body.dark .edge.k-readssecret .elabel { fill: #eab446; }
  
  .edge.k-connectsto path { stroke: #1a7f37; }
  .edge.k-connectsto .elabel { fill: #1a7f37; }
  body.dark .edge.k-connectsto .elabel { fill: #3fb950; }
  
  .edge.k-authenticatesvia path { stroke: #8250df; }
  .edge.k-authenticatesvia .elabel { fill: #8250df; }
  body.dark .edge.k-authenticatesvia .elabel { fill: #bc8cff; }
  
  .edge.k-calls path { stroke: #0969da; }
  .edge.k-calls .elabel { fill: #0969da; }
  body.dark .edge.k-calls .elabel { fill: #58a6ff; }
  
  .edge.k-pullsimage path { stroke: #bc4c00; }
  .edge.k-pullsimage .elabel { fill: #bc4c00; }
  body.dark .edge.k-pullsimage .elabel { fill: #f78166; }

  .edge.model path{stroke:var(--model) !important;stroke-dasharray:6 5}
  .edge.model .elabel{fill:var(--model) !important}
  .node{cursor:pointer}
  .node .hit{fill:transparent;stroke:none;rx:8}
  .node:hover .hit{fill:rgba(9,105,218,.06)}
  .node .ring{fill:none;stroke:none;rx:8}
  .node.seed .ring{stroke:var(--seed);stroke-width:2.5}
  .node.sel .ring{stroke:var(--accent);stroke-width:2.5}
  .node .nname{fill:var(--ink);font-weight:600;font-size:12.5px}
  .node .ntype{fill:var(--muted);font-size:10.5px}
  .node .fbox{fill:#dae8fc;stroke:#6c8ebf;stroke-width:1.4;rx:6}
  .node.external .fbox{fill:#f6f8fa;stroke:#8c959f;stroke-dasharray:5 4}
  .node .fbtext{fill:#1f3b57;font-weight:600;font-size:12px}
  .node.external .fbtext{fill:#57606a}
  .dim{opacity:.13 !important}
  .hidden{display:none}
</style>
</head>
<body>
<header>
  <h1><b>cloudmap</b> <span id="seedName"></span></h1>
  <div class="search"><input id="q" type="search" placeholder="search resources..."></div>
  <select id="rgFilter" style="background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 8px;"><option value="">All Resource Groups</option></select>
  <div class="toggles">
    <label><input type="checkbox" id="tModel" checked> model</label>
    <label><input type="checkbox" id="tExt" checked> external</label>
  </div>
  <button id="btnDark" style="margin-left:auto;background:var(--card);color:var(--ink);border:1px solid var(--line);padding:5px 10px;border-radius:6px;cursor:pointer;">🌙 Dark Mode</button>
  <button id="btnExportPNG" style="margin-left:8px;background:var(--accent);color:#fff;border:none;padding:5px 10px;border-radius:6px;cursor:pointer;font-weight:600;">⬇️ PNG</button>
  <button id="btnExportSVG" style="background:var(--accent);color:#fff;border:none;padding:5px 10px;border-radius:6px;cursor:pointer;font-weight:600;">⬇️ SVG</button>
  <div class="badges" id="badges"></div>
</header>
<div id="stage">
  <div id="wrap">
    <svg id="graph" xmlns="http://www.w3.org/2000/svg"><g id="vp"></g></svg>
    <div class="zoom">
      <button id="zin" title="zoom in">+</button>
      <button id="zout" title="zoom out">&minus;</button>
      <button id="zfit" title="fit">&#9634;</button>
    </div>
    <div class="legend">
      <span class="lg"><span class="sw"></span> verified</span>
      <span class="lg"><span class="sw model"></span> model guess</span>
      <span class="lg"><span class="sw box"></span> seed</span>
    </div>
  </div>
  <aside id="panel" class="empty">
    <div class="hint">Click a resource to focus its blast radius. Scroll to zoom, drag to pan.</div>
    <div class="detail"></div>
  </aside>
</div>
<script>
const DATA = /*__DATA__*/null;
const NS="http://www.w3.org/2000/svg";
const svg=document.getElementById("graph"), vp=document.getElementById("vp");
const panel=document.getElementById("panel");
const byId={}; DATA.nodes.forEach(n=>byId[n.id]=n);
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const shortType=t=>String(t||"").split("/").pop();
function el(t,a){const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);return e;}
const parser=new DOMParser();
function iconEl(type,x,y,size){
  const markup=DATA.icons[type];if(!markup)return null;
  const doc=parser.parseFromString(markup,"image/svg+xml");
  const ic=document.importNode(doc.documentElement,true);
  ic.setAttribute("x",x);ic.setAttribute("y",y);
  ic.setAttribute("width",size);ic.setAttribute("height",size);
  return ic;
}

function catInfo(type){
  const t=(type||"").toLowerCase();
  const M=[
    [/sites|serverfarms|managedclusters|containerinstance|functions/,"compute","#2f6feb"],
    [/keyvault|managedidentity|userassigned/,"security","#b58907"],
    [/virtualnetworks|privateendpoints|applicationgateways|apimanagement|networkinterfaces/,"network","#8250df"],
    [/operationalinsights|insights\/components|workspaces/,"monitor","#bf3989"],
    [/servicebus|eventhub|searchservices|cognitiveservices/,"integration","#1b7c83"],
    [/containerregistry|registries/,"registry","#bc4c00"],
    [/sql|postgres|mysql|documentdb|cosmos|cache|redis|storageaccounts/,"data","#1a7f37"],
  ];
  for(const [re,cat,color] of M) if(re.test(t)) return {cat,color};
  return {cat:"other",color:"#6e7781"};
}

document.getElementById("seedName").textContent="— "+DATA.seedName;
(function(){const m=DATA.meta,b=document.getElementById("badges");
  const add=(txt,cls,title)=>{const s=document.createElement("span");s.className="badge "+(cls||"");s.textContent=txt;if(title)s.title=title;b.appendChild(s);};
  add(m.nodes+" resources");add(m.edges+" dependencies");
  if(m.external)add(m.external+" external","warn");
  if(m.model_edges)add(m.model_edges+" model","warn");
  // why it is incomplete travels with the badge, so the reader never has to guess
  const why=[].concat(m.truncated?["scan hit the pagination cap"]:[],m.read_gaps||[],m.blind_spots||[]);
  add(m.complete?"complete":"INCOMPLETE",m.complete?"ok":"warn",why.join("\n\n"));
})();

// ---- layout: radial blast ----
// The seed IS the centre of the blast, so draw it that way: dependencies fan out
// to the RIGHT, dependents to the LEFT, one ring per hop. Angular spans follow a
// tidy radial tree (each node centred over its subtree), so edges rarely cross,
// and every edge leaves the seed at its own angle - no bundling, no label pile-up.
const NW=168,NH=104,ICON=56,PAD=70,RSTEP=270,MINCHORD=190;
const adjD={},adjU={};
DATA.edges.forEach(e=>{(adjD[e.source]=adjD[e.source]||[]).push(e.target);
                       (adjU[e.target]=adjU[e.target]||[]).push(e.source);});
// BFS tree from the seed with the same direction-consistency the engine uses.
const info={};info[DATA.seed]={hops:0,side:"seed",children:[]};
const bfs=[DATA.seed];
while(bfs.length){
  const id=bfs.shift(),inf=info[id],steps=[];
  if(inf.side!=="up")(adjD[id]||[]).forEach(t=>steps.push([t,"down"]));
  if(inf.side!=="down")(adjU[id]||[]).forEach(s=>steps.push([s,"up"]));
  steps.forEach(([nb,d])=>{if(info[nb]||!byId[nb])return;
    info[nb]={hops:inf.hops+1,side:d,children:[]};inf.children.push(nb);bfs.push(nb);});
}
DATA.nodes.forEach(n=>{if(!info[n.id]){                      // safety net: never drop
  info[n.id]={hops:1,side:"down",children:[]};info[DATA.seed].children.push(n.id);}});
const leafN={};
(function count(id){const c=info[id].children;
  return leafN[id]=c.length?c.reduce((s,k)=>s+count(k),0):1;})(DATA.seed);
function spread(kids,a0,a1){
  const tot=kids.reduce((s,k)=>s+leafN[k],0)||1;let a=a0;
  kids.forEach(k=>{const w=(a1-a0)*leafN[k]/tot;
    info[k].angle=a+w/2;spread(info[k].children,a,a+w);a+=w;});
}
spread(info[DATA.seed].children.filter(k=>info[k].side==="down"),-76,76);
spread(info[DATA.seed].children.filter(k=>info[k].side==="up"),104,256);
// ring radii: grow until neighbours on the ring cannot collide
const radius={0:0};let rr=0;
[...new Set(Object.values(info).map(i=>i.hops))].filter(h=>h>0).sort((a,b)=>a-b).forEach(h=>{
  const as=Object.values(info).filter(i=>i.hops===h).map(i=>i.angle).sort((a,b)=>a-b);
  let need=RSTEP;
  for(let i=1;i<as.length;i++){const d=(as[i]-as[i-1])*Math.PI/180;
    if(d>1e-4)need=Math.max(need,MINCHORD/(2*Math.sin(Math.min(d,Math.PI)/2)));}
  rr=Math.max(rr+RSTEP*.85,need);radius[h]=rr;
});
// rings are ellipses (wider than tall): labels sit under the icons, screens are
// wide, and the stretch keeps the fan from becoming a tall oval hugging the seed.
const STRETCH=1.55;
const pos={};let mnx=1e9,mny=1e9,mxx=-1e9,mxy=-1e9;
DATA.nodes.forEach(n=>{const inf=info[n.id];let x=0,y=0;
  if(inf.hops>0){const a=inf.angle*Math.PI/180;
    x=radius[inf.hops]*STRETCH*Math.cos(a);y=radius[inf.hops]*Math.sin(a);}
  pos[n.id]={cx:x,cy:y};
  mnx=Math.min(mnx,x-NW/2);mxx=Math.max(mxx,x+NW/2);
  mny=Math.min(mny,y-NH/2);mxy=Math.max(mxy,y+NH/2);});
const W=mxx-mnx+PAD*2,H=mxy-mny+PAD*2;
DATA.nodes.forEach(n=>{const p=pos[n.id];
  p.cx+=PAD-mnx;p.cy+=PAD-mny;p.x=p.cx-NW/2;p.y=p.cy-NH/2;});

const defs=el("defs");
[["arrow","#8b939c"],["arrow-model","#cf222e"]].forEach(([id,col])=>{
  const m=el("marker",{id,viewBox:"0 0 10 10",refX:9,refY:5,markerWidth:7,markerHeight:7,orient:"auto-start-reverse"});
  m.appendChild(el("path",{d:"M0,0 L10,5 L0,10 z",fill:col}));defs.appendChild(m);});
vp.appendChild(defs);
const gE=el("g"),gN=el("g");vp.appendChild(gE);vp.appendChild(gN);

function anchor(from,to){                 // where the line meets the node's box
  const dx=to.cx-from.cx,dy=to.cy-from.cy;
  const t=1/Math.max(Math.abs(dx)/(NW/2-14),Math.abs(dy)/(NH/2+4),1e-6);
  return {x:from.cx+dx*Math.min(t,1),y:from.cy+dy*Math.min(t,1)};
}
function edgeGeom(a,b){
  const s=anchor(a,b),t=anchor(b,a);
  const dx=t.x-s.x,dy=t.y-s.y,len=Math.hypot(dx,dy)||1;
  const bow=Math.min(26,len*.07),px=-dy/len*bow,py=dx/len*bow;   // gentle arc
  const mx=(s.x+t.x)/2+px,my=(s.y+t.y)/2+py;
  let ang=Math.atan2(dy,dx)*180/Math.PI;
  if(ang>90||ang<-90)ang+=180;            // keep the label readable, never upside down
  return {d:`M${s.x},${s.y} Q${mx},${my} ${t.x},${t.y}`,mx:mx-px/2,my:my-py/2,ang};
}

const incident={},edgeG=[];
DATA.edges.forEach((e,i)=>{
  (incident[e.source]=incident[e.source]||[]).push(i);
  (incident[e.target]=incident[e.target]||[]).push(i);
  const a=pos[e.source],b=pos[e.target];if(!a||!b){edgeG[i]=null;return;}
  const model=e.origin==="model",gm=edgeGeom(a,b);
  const kindCls = e.kind ? " k-"+e.kind.split(" ")[0].toLowerCase().replace(/[^a-z0-9]/g,"") : "";
  const g=el("g",{class:"edge"+(model?" model":"")+kindCls});
  g.appendChild(el("path",{d:gm.d,"marker-end":model?"url(#arrow-model)":"url(#arrow)"}));
  // the label rides its own edge (rotated along it), so labels fan out with the
  // edges instead of piling on one axis; long kinds are trimmed - full text on hover
  const kind=e.kind+(model?" (model)":"");
  const t=el("text",{y:-5,"text-anchor":"middle",class:"elabel",
    transform:`translate(${gm.mx},${gm.my}) rotate(${gm.ang})`});
  t.textContent=kind.length>32?kind.slice(0,30)+"…":kind;
  const tip=el("title");tip.textContent=e.kind+(e.evidence?"\n"+e.evidence:"");
  g.appendChild(tip);g.appendChild(t);
  gE.appendChild(g);edgeG[i]=g;
});

const nodeG={};
DATA.nodes.forEach(n=>{
  const p=pos[n.id];if(!p)return;
  const g=el("g",{class:"node"+(n.seed?" seed":"")+(n.external?" external":""),
                  transform:`translate(${p.x},${p.y})`});
  g.appendChild(el("rect",{class:"hit",width:NW,height:NH,rx:8}));
  const icon=n.external?null:iconEl(n.type,(NW-ICON)/2,4,ICON);
  if(icon){
    g.appendChild(icon);
    const t1=el("text",{x:NW/2,y:ICON+22,class:"nname","text-anchor":"middle"});
    t1.textContent=n.name;g.appendChild(t1);
    const t2=el("text",{x:NW/2,y:ICON+37,class:"ntype","text-anchor":"middle"});
    t2.textContent=shortType(n.type);g.appendChild(t2);
  }else{
    // no icon for this type: a plain diagram box (blue = azure resource,
    // dashed grey = external/unverified reference), never a broken image
    g.appendChild(el("rect",{class:"fbox",x:9,y:22,width:NW-18,height:44,rx:6}));
    const t1=el("text",{x:NW/2,y:48,class:"fbtext","text-anchor":"middle"});
    t1.textContent=n.name.length>22?n.name.slice(0,21)+"…":n.name;g.appendChild(t1);
    const t2=el("text",{x:NW/2,y:80,class:"ntype","text-anchor":"middle"});
    t2.textContent=n.external?"external":shortType(n.type);g.appendChild(t2);
  }
  g.appendChild(el("rect",{class:"ring",x:2,y:2,width:NW-4,height:NH-4,rx:8}));
  const tip=el("title");tip.textContent=n.name+"\n"+n.type;g.appendChild(tip);
  g.addEventListener("pointerdown",ev=>ev.stopPropagation());
  g.addEventListener("click",ev=>{ev.stopPropagation();focus(n.id);});
  gN.appendChild(g);nodeG[n.id]=g;
});

// ---- zoom & pan ----
let scale=1,tx=0,ty=0;
function apply(){vp.setAttribute("transform",`translate(${tx},${ty}) scale(${scale})`);}
function fit(){const r=svg.getBoundingClientRect();
  const s=Math.min(1.2,(r.width-60)/W,(r.height-60)/H)||1;
  scale=s>0?s:1;tx=(r.width-W*scale)/2;ty=Math.max(20,(r.height-H*scale)/2);apply();}
function zoomAt(mx,my,f){const ns=Math.min(3,Math.max(.2,scale*f));
  tx=mx-(mx-tx)*(ns/scale);ty=my-(my-ty)*(ns/scale);scale=ns;apply();}
svg.addEventListener("wheel",e=>{e.preventDefault();const r=svg.getBoundingClientRect();
  zoomAt(e.clientX-r.left,e.clientY-r.top,e.deltaY<0?1.12:1/1.12);},{passive:false});
let drag=false,moved=false,x0,y0,tx0,ty0;
svg.addEventListener("pointerdown",e=>{drag=true;moved=false;x0=e.clientX;y0=e.clientY;tx0=tx;ty0=ty;svg.classList.add("grabbing");svg.setPointerCapture(e.pointerId);});
svg.addEventListener("pointermove",e=>{if(!drag)return;const dx=e.clientX-x0,dy=e.clientY-y0;if(Math.abs(dx)+Math.abs(dy)>3)moved=true;tx=tx0+dx;ty=ty0+dy;apply();});
svg.addEventListener("pointerup",e=>{drag=false;svg.classList.remove("grabbing");if(!moved)clearFocus();});
document.getElementById("zin").onclick=()=>{const r=svg.getBoundingClientRect();zoomAt(r.width/2,r.height/2,1.2);};
document.getElementById("zout").onclick=()=>{const r=svg.getBoundingClientRect();zoomAt(r.width/2,r.height/2,1/1.2);};
document.getElementById("zfit").onclick=fit;
window.addEventListener("resize",fit);

// ---- focus / panel ----
function focus(id){
  const keepN=new Set([id]),keepE=new Set();
  (incident[id]||[]).forEach(i=>{keepE.add(i);keepN.add(DATA.edges[i].source);keepN.add(DATA.edges[i].target);});
  DATA.nodes.forEach(n=>nodeG[n.id]&&nodeG[n.id].classList.toggle("dim",!keepN.has(n.id)));
  DATA.edges.forEach((e,i)=>edgeG[i]&&edgeG[i].classList.toggle("dim",!keepE.has(i)));
  Object.values(nodeG).forEach(g=>g.classList.remove("sel"));
  nodeG[id]&&nodeG[id].classList.add("sel");
  showPanel(id);
}
function clearFocus(){
  document.querySelectorAll(".dim").forEach(x=>x.classList.remove("dim"));
  Object.values(nodeG).forEach(g=>g.classList.remove("sel"));
  panel.className="empty";panel.querySelector(".detail").innerHTML="";
}
function showPanel(id){
  const n=byId[id],ci=catInfo(n.type),rows=[];
  (incident[id]||[]).forEach(i=>{
    const e=DATA.edges[i],out=e.source===id,other=byId[out?e.target:e.source]||{name:(out?e.target:e.source)};
    const model=e.origin==="model";
    rows.push(`<div class="dep"><div class="top">
        <span><span class="arrow">${out?"depends on →":"← used by"}</span> <span class="name">${esc(other.name)}</span></span>
        <span class="tag ${model?"model":"verified"}">${model?"model":"verified"}</span></div>
      <div class="kind">${esc(e.kind)}</div>
      ${e.evidence?`<div class="ev">${esc(e.evidence)}</div>`:""}</div>`);
  });
  panel.className="";
  const hico=DATA.icons[n.type]
    ?`<span class="hico">${DATA.icons[n.type].replace("<svg ",'<svg width="22" height="22" ')}</span>`:"";
  const portalLink = n.id.startsWith("/subscriptions/") 
    ? `<a href="https://portal.azure.com/#resource${n.id}" target="_blank" style="display:inline-block;margin:12px 0 4px;background:#0078d4;color:#fff;padding:6px 12px;border-radius:4px;text-decoration:none;font-weight:600;font-size:12.5px;">🔗 Open in Azure Portal</a>` 
    : "";
  panel.querySelector(".detail").innerHTML=`
    <h2>${hico}${esc(n.name)}${n.seed?" (seed)":""}</h2>
    <div class="sub">${esc(n.type)} <span class="chip" style="background:${ci.color}">${ci.cat}</span></div>
    ${n.rg?`<div class="kv"><b>group</b><span>${esc(n.rg)}</span></div>`:""}
    ${n.location?`<div class="kv"><b>location</b><span>${esc(n.location)}</span></div>`:""}
    ${n.external?`<div class="kv"><b>status</b><span>external / unverified</span></div>`:""}
    ${n.note?`<div class="kv"><b>note</b><span>${esc(n.note)}</span></div>`:""}
    ${portalLink}
    <div class="deps"><h3>${rows.length} connection${rows.length===1?"":"s"}</h3>${rows.join("")||'<div class="hint">no edges</div>'}</div>`;
}
svg.addEventListener("click",e=>{if(e.target===svg||e.target===vp)clearFocus();});

// ---- search + toggles + new features ----
document.getElementById("q").addEventListener("input",e=>{
  const q=e.target.value.trim().toLowerCase();
  DATA.nodes.forEach(n=>{const hit=!q||n.name.toLowerCase().includes(q)||(n.type||"").toLowerCase().includes(q);
    nodeG[n.id]&&nodeG[n.id].classList.toggle("dim",!hit);});
});
const rgs = new Set(DATA.nodes.map(n=>n.rg).filter(Boolean));
rgs.forEach(rg => {
  const opt = document.createElement("option");
  opt.value = opt.textContent = rg;
  document.getElementById("rgFilter").appendChild(opt);
});
function applyToggles(){
  const sm=document.getElementById("tModel").checked,se=document.getElementById("tExt").checked;
  const srg = document.getElementById("rgFilter").value;
  DATA.nodes.forEach(n=>{if(!nodeG[n.id])return;
    const hideExt = n.external && !se;
    const hideRg = srg && n.rg && n.rg !== srg;
    nodeG[n.id].classList.toggle("hidden", hideExt || hideRg);
  });
  DATA.edges.forEach((e,i)=>{if(!edgeG[i])return;
    const eh=!se&&((byId[e.source]&&byId[e.source].external)||(byId[e.target]&&byId[e.target].external));
    const er=!srg ? false : ((byId[e.source]&&byId[e.source].rg&&byId[e.source].rg!==srg)||(byId[e.target]&&byId[e.target].rg&&byId[e.target].rg!==srg));
    edgeG[i].classList.toggle("hidden",eh||er||(!sm&&e.origin==="model"));});
}
document.getElementById("tModel").addEventListener("change",applyToggles);
document.getElementById("tExt").addEventListener("change",applyToggles);
document.getElementById("rgFilter").addEventListener("change",applyToggles);

// Dark Mode
document.getElementById("btnDark").onclick = () => {
  document.body.classList.toggle("dark");
  document.getElementById("btnDark").textContent = document.body.classList.contains("dark") ? "☀️ Light Mode" : "🌙 Dark Mode";
};

// Export logic (PNG & SVG)
function getExportSVG() {
  const clone = svg.cloneNode(true);
  const bbox = vp.getBBox();
  const w = bbox.width + 100, h = Math.max(bbox.height + 100, 300);
  clone.setAttribute("width", w);
  clone.setAttribute("height", h);
  clone.setAttribute("viewBox", `${bbox.x - 50} ${bbox.y - 50} ${w} ${h}`);
  
  // reset transform so it's not exported with current pan/zoom
  clone.querySelector("#vp").setAttribute("transform", "");
  
  const style = document.createElement("style");
  style.textContent = `
    .edge path{fill:none;stroke:#8c959f;stroke-width:1.4}
    .edge .elabel{fill:#424a53;font-size:10.5px;font-weight:500;stroke:${document.body.classList.contains('dark') ? '#0d1117' : '#ffffff'};stroke-width:3.5px}
    .edge.model path{stroke:#cf222e;stroke-dasharray:6 5}
    .edge.model .elabel{fill:#cf222e}
    .node .hit{fill:transparent;stroke:none;rx:8}
    .node .ring{fill:none;stroke:none;rx:8}
    .node.seed .ring{stroke:#bf8700;stroke-width:2.5}
    .node .nname{fill:${document.body.classList.contains('dark') ? '#e6edf3' : '#1f2328'};font-weight:600;font-size:12.5px;font-family:sans-serif}
    .node .ntype{fill:#57606a;font-size:10.5px;font-family:sans-serif}
    .node .fbox{fill:#dae8fc;stroke:#6c8ebf;stroke-width:1.4;rx:6}
    .node.external .fbox{fill:#f6f8fa;stroke:#8c959f;stroke-dasharray:5 4}
    .node .fbtext{fill:#1f3b57;font-weight:600;font-size:12px;font-family:sans-serif}
    .node.external .fbtext{fill:#57606a}
    .hidden, .dim {display:none}
  `;
  clone.insertBefore(style, clone.firstChild);
  return { clone, w, h };
}

document.getElementById("btnExportSVG").onclick = () => {
  const { clone } = getExportSVG();
  const svgData = new XMLSerializer().serializeToString(clone);
  const blob = new Blob([svgData], {type: "image/svg+xml;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "cloudmap_export.svg";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
};

document.getElementById("btnExportPNG").onclick = () => {
  const { clone, w, h } = getExportSVG();
  const svgData = new XMLSerializer().serializeToString(clone);
  const svg64 = btoa(unescape(encodeURIComponent(svgData)));
  
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d");
  const img = new Image();
  img.onload = function() {
    canvas.width = w * 2; // retina 2x resolution
    canvas.height = h * 2;
    ctx.scale(2, 2);
    ctx.fillStyle = document.body.classList.contains("dark") ? "#0d1117" : "#f3f6f9";
    ctx.fillRect(0, 0, w, h);
    ctx.drawImage(img, 0, 0);
    const a = document.createElement("a");
    a.download = "cloudmap_export.png";
    a.href = canvas.toDataURL("image/png");
    a.click();
  };
  img.src = "data:image/svg+xml;base64," + svg64;
};

fit();
</script>
</body>
</html>
"""
