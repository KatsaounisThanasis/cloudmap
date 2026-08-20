"""The two text renderers (Mermaid, CSV).

Mermaid has exactly two escape rules - double quotes in labels and pipes in edge
kinds break its syntax - and the CSV is read by spreadsheets and auditors, so it
must round-trip through a real csv.reader with hostile evidence text intact.
"""

import csv
import io

from cloudmap.model import Edge, Graph, Node
from cloudmap.render.csv_export import to_csv
from cloudmap.render.mermaid import to_mermaid


def _graph():
    web = Node(id="/web", name='app "prod"', type="microsoft.web/sites")
    kv = Node(id="/kv", name="kv-1", type="microsoft.keyvault/vaults")
    ext = Node(id="/ext", name="unknown.example.com", type="external", external=True)
    return Graph(
        nodes={n.id: n for n in (web, kv, ext)},
        edges=[
            Edge("/web", "/kv", "reads-secret; role: KV | Secrets User",
                 evidence='setting "CONN", value contains , and\nnewline'),
            Edge("/web", "/ext", "connects-to", origin="model"),
        ],
        distances={"/web": 0, "/kv": 1, "/ext": 1},
    )


# --- mermaid ----------------------------------------------------------------------

def test_mermaid_escapes_the_only_two_things_that_break_it():
    text = to_mermaid(_graph(), "/web")

    assert '"' not in text.split("\n")[1].split("[")[1].replace('["', "").replace('"]', "") \
        or "app 'prod'" in text                      # quotes in names become single quotes
    assert "KV / Secrets User" in text               # pipes in kinds become slashes
    assert "-->|" in text                            # verified edge, solid
    assert '-. "connects-to (model)" .->' in text    # model edge, dashed and labelled


def test_mermaid_marks_seed_and_external_nodes():
    text = to_mermaid(_graph(), "/web")

    assert "style N0" in text                        # seed styled
    assert "stroke-dasharray" in text                # external node dashed


# --- csv --------------------------------------------------------------------------

def test_csv_round_trips_through_a_real_reader():
    rows = list(csv.reader(io.StringIO(to_csv(_graph(), "/web"))))
    header, body = rows[0], rows[1:]

    assert header[0] == "Source Name" and "Evidence" in header
    assert len(body) == 2
    by_target = {r[3]: r for r in body}
    assert by_target["kv-1"][5] == "Verified"
    assert by_target["kv-1"][6] == 'setting "CONN", value contains , and\nnewline'
    assert by_target["unknown.example.com"][5] == "GUESS (LLM) (Unverified Target)"


def test_csv_falls_back_to_the_id_tail_for_unscanned_targets():
    g = _graph()
    g.edges.append(Edge("/web", "/subscriptions/s/x/gone-resource", "references"))

    rows = list(csv.reader(io.StringIO(to_csv(g, "/web"))))

    assert any(r[3] == "gone-resource" and "Unverified Target" in r[5] for r in rows[1:])
