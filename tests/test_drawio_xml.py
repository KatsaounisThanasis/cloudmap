"""The .drawio file has to be XML that draw.io can actually open.

Resource names, resource groups, locations, custom role names and the notes on
external nodes are all free text that reaches the diagram as XML ATTRIBUTES. One
unescaped `&` or `"` and the whole file fails to load - the user gets a broken
download, not a partial diagram. So these tests parse the output with an XML
parser instead of matching strings: if the parse succeeds and the values come
back byte-identical, the escaping is right whatever the renderer does internally.
"""

import os
import xml.etree.ElementTree as ET

from cloudmap.graph import build_graph, collapse_high_level
from cloudmap.ingest.fixture import load_fixture
from cloudmap.model import Edge, Graph, Node
from cloudmap.render.drawio import to_drawio

FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "estate.json")

NASTY = 'ord&rs <b>"quoted" \'single\''


def _objects(xml):
    return {o.get("label"): o for o in ET.fromstring(xml).iter("object")}


def test_a_resource_name_full_of_xml_metacharacters_still_parses_as_xml():
    seed = Node(id="/web", name=NASTY, type="microsoft.web/sites")
    g = Graph(nodes={seed.id: seed}, edges=[], distances={seed.id: 0})

    labels = _objects(to_drawio(g, seed.id))

    assert f"{NASTY} (sites)" in labels          # round-trips through the parser intact
    assert "&amp;" in to_drawio(g, seed.id)      # ... because it was escaped, not stripped


def test_metacharacters_in_every_metadata_attribute_survive_a_round_trip():
    seed = Node(id="/web&<1>", name="web", type="microsoft.web/sites",
                resource_group='rg-"a"&b', location="we<st>", subscription="s&1",
                note="referenced by 'config' & not verified", external=True)
    g = Graph(nodes={seed.id: seed}, edges=[], distances={seed.id: 0})

    obj = _objects(to_drawio(g, seed.id))["web (sites)"]

    assert obj.get("ResourceGroup") == 'rg-"a"&b'
    assert obj.get("Location") == "we<st>"
    assert obj.get("Subscription") == "s&1"
    assert obj.get("Note") == "referenced by 'config' & not verified"
    assert obj.get("ARM_ID") == "/web&<1>"


def test_an_edge_label_with_metacharacters_stays_well_formed():
    # Edge labels carry custom role names and merged kinds - tenant-authored text.
    seed = Node(id="/web", name="web", type="microsoft.web/sites")
    kv = Node(id="/kv", name="kv", type="microsoft.keyvault/vaults")
    g = Graph(nodes={n.id: n for n in (seed, kv)},
              edges=[Edge(seed.id, kv.id, 'role: R&D "secrets" <reader>')],
              distances={seed.id: 0, kv.id: 1})

    root = ET.fromstring(to_drawio(g, seed.id))
    values = [c.get("value") for c in root.iter("mxCell") if c.get("edge") == "1"]

    assert values == ['role: R&D "secrets" <reader>']


def test_a_model_proposed_edge_is_labelled_as_a_guess_in_valid_xml():
    seed = Node(id="/web", name="web", type="microsoft.web/sites")
    kv = Node(id="/kv", name="kv", type="microsoft.keyvault/vaults")
    g = Graph(nodes={n.id: n for n in (seed, kv)},
              edges=[Edge(seed.id, kv.id, "reads-secret", origin="model")],
              distances={seed.id: 0, kv.id: 1})

    root = ET.fromstring(to_drawio(g, seed.id))
    edge = next(c for c in root.iter("mxCell") if c.get("edge") == "1")

    assert "(model)" in edge.get("value")
    assert "dashed=1" in edge.get("style")


def test_an_empty_blast_radius_still_produces_a_loadable_file():
    # A seed with no discovered dependencies must yield a valid (if lonely) diagram.
    root = ET.fromstring(to_drawio(Graph(nodes={}, edges=[]), "/nothing"))

    assert root.tag == "mxfile"
    assert root.find(".//root") is not None


def test_every_node_and_edge_appears_exactly_once():
    seed = Node(id="/web", name="web", type="microsoft.web/sites")
    kv = Node(id="/kv", name="kv", type="microsoft.keyvault/vaults")
    g = Graph(nodes={n.id: n for n in (seed, kv)},
              edges=[Edge(seed.id, kv.id, "reads-secret")],
              distances={seed.id: 0, kv.id: 1})

    root = ET.fromstring(to_drawio(g, seed.id))
    cell_ids = [c.get("id") for c in root.iter("mxCell") if c.get("id")]

    assert len(list(root.iter("object"))) == 2
    assert len(cell_ids) == len(set(cell_ids))          # no duplicate mxCell ids


def test_an_edge_to_a_node_outside_the_diagram_is_dropped_not_dangled():
    # A dangling source/target would produce an mxCell pointing at a missing id,
    # which draw.io renders as a floating arrow.
    seed = Node(id="/web", name="web", type="microsoft.web/sites")
    g = Graph(nodes={seed.id: seed}, edges=[Edge(seed.id, "/not-in-the-map", "connects-to")],
              distances={seed.id: 0})

    root = ET.fromstring(to_drawio(g, seed.id))

    assert [c for c in root.iter("mxCell") if c.get("edge") == "1"] == []


def test_the_seed_is_the_only_highlighted_cell():
    seed = Node(id="/web", name="web", type="microsoft.web/sites")
    kv = Node(id="/kv", name="kv", type="microsoft.keyvault/vaults")
    g = Graph(nodes={n.id: n for n in (seed, kv)}, edges=[], distances={seed.id: 0, kv.id: 1})

    root = ET.fromstring(to_drawio(g, seed.id))
    highlighted = [o.get("label") for o in root.iter("object")
                   if "strokeWidth=3" in o.find("mxCell").get("style")]

    assert highlighted == ["web (sites)"]


def test_a_grouped_high_level_view_of_a_real_estate_is_valid_xml():
    # The default view (--level high) of a realistic estate, end to end: grouped
    # labels contain a multiplication sign and grouped notes are joined free text.
    graph = build_graph(load_fixture(FIX))
    seed = next(n.id for n in graph.nodes.values() if n.name == "nw-storefront")
    collapsed = collapse_high_level(graph, seed)

    root = ET.fromstring(to_drawio(collapsed, seed))

    assert len(list(root.iter("object"))) == len(collapsed.nodes)
    assert "nw-storefront (sites)" in {o.get("label") for o in root.iter("object")}
