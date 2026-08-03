"""Ask-layer tests: the answers must come from the graph, not from a model.

The point of these is not that the queries return *something* - it is that they
return the same thing with no model installed, that a conclusion resting on a
model-proposed hop is labelled a guess, and that a model can never inject a
resource that is not in the map.
"""

import json
import os

from cloudmap.adapters import graph_from_neutral
from cloudmap.ask import answer, warnings
from cloudmap.ask.intent import llm_parse, match_nodes, parse
from cloudmap.ask.narration import narrate
from cloudmap.ask.queries import depends, guesses, impact, paths, shared, summary
from cloudmap.graph import blast_radius, build_graph, collapse_high_level, find_seeds
from cloudmap.ingest.fixture import load_fixture
from cloudmap.model import Edge, Graph, Node

FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "acme_orders.json")


def _real():
    graph = build_graph(load_fixture(FIX))
    return graph, find_seeds(graph, "webapp-orders-dev")[0]


def _mixed():
    """A small map with one verified chain and one model-proposed hop on it."""
    web = Node(id="/web", name="web", type="microsoft.web/sites")
    plan = Node(id="/plan", name="plan", type="microsoft.web/serverfarms")
    vault = Node(id="/kv", name="kv", type="microsoft.keyvault/vaults")
    ghost = Node(id="external://ghost.vault.azure.net", name="ghost.vault.azure.net",
                 type="external/keyvault", external=True, note="not in scanned scope")
    return Graph(
        nodes={n.id: n for n in (web, plan, vault, ghost)},
        edges=[
            Edge("/web", "/plan", "hosted-on", origin="extracted", evidence="serverFarmId"),
            Edge("/web", "/kv", "reads-secret", origin="model"),
            Edge("/web", ghost.id, "reads-secret", origin="extracted", evidence="appSettings"),
        ],
    )


# --- the answers are computed, not generated -------------------------------------

def test_impact_is_the_upstream_dependents_of_the_subject():
    graph, _seed = _real()
    vault = find_seeds(graph, "kv-orders-dev")[0]
    res = impact(graph, vault)
    names = {f["name"] for f in res["findings"]}

    assert names == {"webapp-orders-dev"}          # the app that reads the vault
    assert res["facts"]["dependents" if "dependents" in res["facts"] else "total"] == 1
    assert all(f["trust"] == "verified" for f in res["findings"])


def test_impact_does_not_leak_the_dependents_own_dependencies():
    # Same direction-consistency rule as blast_radius: asking who depends on the
    # vault must not drag in the app's OWN downstream resources.
    graph, _seed = _real()
    vault = find_seeds(graph, "kv-orders-dev")[0]
    names = {f["name"] for f in impact(graph, vault)["findings"]}

    assert "plan-orders-dev" not in names
    assert "pg-orders-dev" not in names


def test_depends_lists_what_the_seed_needs():
    graph, seed = _real()
    names = {f["name"] for f in depends(graph, seed)["findings"]}

    assert {"plan-orders-dev", "pg-orders-dev", "stordersdev",
            "acrordersdev", "kv-orders-dev"} == names


def test_every_finding_carries_the_proof_behind_it():
    graph, seed = _real()
    for f in depends(graph, seed)["findings"]:
        assert f["path"], "a finding must show HOW it was reached"
        assert all(hop["evidence"] for hop in f["path"]), "every hop must carry its proof"


def test_findings_are_real_nodes_of_the_map_only():
    graph, seed = _real()
    ids = set(graph.nodes)
    for query in (impact(graph, seed), depends(graph, seed)):
        for f in query["findings"]:
            assert f["id"] in ids


def test_paths_are_enumerated_from_the_edges():
    graph, seed = _real()
    acr = find_seeds(graph, "acrordersdev")[0]
    res = paths(graph, seed, acr)

    assert res["findings"], "the app pulls its image from the registry"
    assert res["findings"][0]["path"][-1]["target"] == "acrordersdev"


def test_paths_reports_absence_instead_of_inventing_one():
    graph, _seed = _real()
    plan = find_seeds(graph, "plan-orders-dev")[0]
    acr = find_seeds(graph, "acrordersdev")[0]
    res = paths(graph, plan, acr)

    assert res["findings"] == []
    assert "No path" in res["headline"]


# --- trust aggregation: one weak hop makes the conclusion a guess ----------------

def test_conclusion_through_a_model_hop_is_marked_a_guess():
    graph = _mixed()
    res = impact(graph, "/kv")
    finding = res["findings"][0]

    assert finding["name"] == "web"
    assert finding["trust"] == "unverified"
    assert "model proposed" in finding["why"]


def test_an_unverified_endpoint_is_flagged_without_faking_a_guess():
    # The app really does name that host (proof: appSettings) - the edge is a fact.
    # What is unconfirmed is the resource behind it, which `external` already says.
    graph = _mixed()
    by_name = {f["name"]: f for f in depends(graph, "/web")["findings"]}

    assert by_name["plan"]["trust"] == "verified"
    assert by_name["ghost.vault.azure.net"]["trust"] == "verified"
    assert by_name["ghost.vault.azure.net"]["external"] is True
    assert by_name["kv"]["trust"] == "unverified"                 # model-proposed hop


def test_a_path_passing_through_an_unverified_node_is_a_guess():
    # Crossing something nobody confirmed exists is different from ending at it.
    web = Node(id="/web", name="web", type="microsoft.web/sites")
    ghost = Node(id="external://gw", name="gw", type="external/appgw", external=True,
                 note="not in scanned scope")
    api = Node(id="/api", name="api", type="microsoft.web/sites")
    graph = Graph(nodes={n.id: n for n in (web, ghost, api)},
                  edges=[Edge("/web", ghost.id, "routes-to", evidence="appSettings"),
                         Edge(ghost.id, "/api", "routes-to", evidence="appSettings")])
    by_name = {f["name"]: f for f in depends(graph, "/web")["findings"]}

    assert by_name["gw"]["trust"] == "verified"          # the endpoint itself
    assert by_name["api"]["trust"] == "unverified"       # reached only THROUGH gw
    assert "passes through gw" in by_name["api"]["why"]


def test_guesses_lists_exactly_what_is_not_proven():
    res = guesses(_mixed())
    names = {f["name"] for f in res["findings"]}

    assert "web --reads-secret--> kv" in names            # the model-proposed edge
    assert "ghost.vault.azure.net" in names               # the unverified reference
    assert all(f["trust"] == "unverified" for f in res["findings"])


def test_guesses_on_a_fully_verified_map_says_so():
    graph, _seed = _real()
    res = guesses(graph)

    assert res["findings"] == []
    assert "verified by a deterministic rule" in res["headline"]


def test_shared_finds_resources_several_others_depend_on():
    web1 = Node(id="/w1", name="w1", type="microsoft.web/sites")
    web2 = Node(id="/w2", name="w2", type="microsoft.web/sites")
    vault = Node(id="/kv", name="kv", type="microsoft.keyvault/vaults")
    graph = Graph(nodes={n.id: n for n in (web1, web2, vault)},
                  edges=[Edge("/w1", "/kv", "reads-secret", evidence="appSettings"),
                         Edge("/w2", "/kv", "reads-secret", evidence="appSettings")])
    res = shared(graph)

    assert [f["name"] for f in res["findings"]] == ["kv"]
    assert res["findings"][0]["metric"] == "2 dependents"
    # each dependent names the relationship it has, so "shared" is auditable too
    assert res["findings"][0]["dependents"] == ["w1 (reads-secret)", "w2 (reads-secret)"]


def test_summary_counts_verified_versus_guessed():
    facts = summary(_mixed())["facts"]

    assert facts["resources"] == 4
    assert facts["edges"] == 3
    assert facts["verified_edges"] == 2
    assert facts["model_edges"] == 1
    assert facts["external_unverified"] == 1


# --- routing: rules first, model only as a validated fallback --------------------

def test_rules_route_the_common_phrasings_without_a_model():
    graph, _seed = _real()
    cases = {
        "what breaks if I touch kv-orders-dev": "impact",
        "what does webapp-orders-dev depend on": "depends",
        "how does webapp-orders-dev reach acrordersdev": "paths",
        "what is shared in this map": "shared",
        "what should I not trust here": "guesses",
        "explain this map": "summary",
    }
    for question, expected in cases.items():
        assert parse(question, graph)["query"] == expected, question


def test_change_verbs_route_to_impact_without_a_model():
    # "I am about to rotate the secrets in kv-orders-dev" is the phrasing people
    # actually type; a rule must own it, so the model is never consulted for it.
    graph, _seed = _real()
    for question in ("I am about to rotate the secrets in kv-orders-dev, who cares",
                     "planning to restart webapp-orders-dev tonight",
                     "we will decommission plan-orders-dev"):
        assert parse(question, graph)["query"] == "impact", question


def test_empty_answer_points_at_the_other_direction():
    # The vault has no dependencies inside a map seeded from the app - saying only
    # "nothing" would read as "nothing to worry about".
    graph, _seed = _real()
    vault = find_seeds(graph, "kv-orders-dev")[0]
    res = depends(graph, vault)

    assert res["findings"] == []
    assert "do depend on kv-orders-dev" in res["hint"]


def test_shared_wins_over_paths_when_the_question_is_about_sharing():
    # PLAN.md's own example: "which paths cross a shared vault" is a sharing
    # question, not a two-endpoint path lookup.
    graph, _seed = _real()
    assert parse("which paths cross a shared vault", graph)["query"] == "shared"


def test_subject_matching_respects_the_longest_name():
    api = Node(id="/api", name="api", type="microsoft.web/sites")
    myapi = Node(id="/myapi", name="myapi", type="microsoft.web/sites")
    graph = Graph(nodes={api.id: api, myapi.id: myapi}, edges=[])

    assert match_nodes(graph, "what breaks if I touch myapi") == ["/myapi"]


def test_paths_question_keeps_the_order_the_names_appear_in():
    graph, seed = _real()
    plan = parse("how does webapp-orders-dev reach acrordersdev", graph)

    assert plan["subject"] == seed
    assert graph.nodes[plan["target"]].name == "acrordersdev"


def test_unparsed_question_is_refused_with_the_supported_forms():
    graph, _seed = _real()
    res = answer(graph, "make me a sandwich")

    assert res["error"]
    assert res["findings"] == []
    assert any("what breaks" in line for line in res["supported"])


def test_question_without_a_resource_is_refused_not_guessed():
    graph, _seed = _real()
    res = answer(graph, "what breaks if I touch it")

    assert res["error"]
    assert "Name a resource" in res["error"]


def test_llm_routing_cannot_introduce_a_resource_outside_the_map(monkeypatch):
    graph, _seed = _real()
    monkeypatch.setattr("cloudmap.ask.intent.generate_json",
                        lambda *a, **k: {"query": "impact", "subject": "totally-made-up"})
    plan = llm_parse("who cares about the made up thing", graph)

    assert plan == {"query": "impact"}       # the invented subject is dropped...
    assert answer(graph, "who cares about the made up thing",
                  allow_llm_intent=False)["error"]   # ...and the answer refuses


def test_llm_routing_refuses_an_invented_query_name(monkeypatch):
    graph, _seed = _real()
    monkeypatch.setattr("cloudmap.ask.intent.generate_json",
                        lambda *a, **k: {"query": "delete_everything", "subject": "kv"})

    assert llm_parse("do the thing", graph) is None


def test_llm_routing_result_is_still_computed_by_the_query(monkeypatch):
    graph, _seed = _real()
    monkeypatch.setattr("cloudmap.ask.intent.generate_json",
                        lambda *a, **k: {"query": "impact", "subject": "kv-orders-dev"})
    res = answer(graph, "hmm the vault, what about it", allow_llm_intent=True)

    assert res["query"] == "impact"
    assert {f["name"] for f in res["findings"]} == {"webapp-orders-dev"}


# --- degradation and honesty ----------------------------------------------------

def test_answer_works_with_no_model_available():
    # local_model.generate() returns "" when ollama is absent; the deterministic
    # answer must be complete anyway and the narration simply empty.
    graph, seed = _real()
    res = answer(graph, f"what does {graph.nodes[seed].name} depend on")

    assert not res.get("error")
    assert len(res["findings"]) == 5
    assert narrate({"query": "depends", "findings": [], "error": "x"}) == ""


def test_narration_is_skipped_when_the_local_model_is_down(monkeypatch):
    monkeypatch.setattr("cloudmap.ask.narration.generate", lambda *a, **k: "")
    graph, _seed = _real()
    res = answer(graph, "explain this map")

    assert narrate(res) == ""
    assert res["headline"]                      # the computed answer is unaffected


def test_answer_inherits_the_maps_incompleteness(tmp_path):
    doc = {
        "seed": "/web",
        "meta": {"complete": False, "truncated": True,
                 "read_gaps": ["RBAC (az role assignment list): denied"]},
        "nodes": [{"id": "/web", "name": "web", "type": "microsoft.web/sites", "hops": 0}],
        "edges": [],
    }
    path = tmp_path / "partial.json"
    path.write_text(json.dumps(doc), encoding="utf-8")

    graph = graph_from_neutral(json.loads(path.read_text(encoding="utf-8")))
    res = answer(graph, "explain this map")

    assert any("INCOMPLETE" in w for w in res["warnings"])
    assert any("read gap" in w for w in res["warnings"])
    assert res["facts"]["complete"] is False


def test_warnings_stay_quiet_on_a_clean_map():
    graph, _seed = _real()
    assert warnings(graph) == []


def test_high_level_map_explains_itself_instead_of_stonewalling():
    # The default trace writes the collapsed view, so this is the error people
    # actually hit: the vault IS on their diagram, inside a "Key Vault" box, and
    # "name a resource from this map" sends them hunting for a typo.
    graph, seed = _real()
    collapsed = collapse_high_level(blast_radius(graph, seed), seed)
    res = answer(collapsed, "what breaks if I touch kv-orders-dev")

    assert res["error"]
    assert "high-level view" in res["error"]
    assert "--level detail" in res["error"]
    assert "Key Vault" in res["error"]            # names a group they can actually ask about


def test_high_level_map_warns_that_counts_are_per_type():
    graph, seed = _real()
    collapsed = collapse_high_level(blast_radius(graph, seed), seed)

    assert any("high-level view" in w for w in warnings(collapsed))


def test_detail_map_keeps_the_plain_not_found_message():
    graph, _seed = _real()
    res = answer(graph, "what breaks if I touch something-that-does-not-exist")

    assert res["error"]
    assert "high-level" not in res["error"]
