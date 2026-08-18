"""What real Resource Graph rows look like when they are not tidy.

A tenant-wide scan returns thousands of rows written by dozens of resource
providers: `properties` can be null, optional columns come back as null rather
than absent, a row can be repeated by paging, and the set can be empty. None of
that is a reason to abort a scan halfway - the graph is the artifact people act
on, so a single ugly row must not take the whole map down with it.

Also locks seed resolution by EXACT ARM id, which is what the wizard passes so
that two resources sharing a name can never be reported as ambiguous.
"""

import pytest

from cloudmap.adapters import AzureAdapter, load_graph
from cloudmap.graph import blast_radius, build_graph, find_seeds

S = "/subscriptions/s/resourceGroups/rg/providers"


def test_null_columns_in_a_row_fall_back_to_empty_values():
    # ARG projects every requested column, so an unset one arrives as null.
    graph = build_graph([{"id": f"{S}/Microsoft.Web/sites/web", "name": None, "type": None,
                          "resourceGroup": None, "subscriptionId": None, "location": None,
                          "kind": None, "identity": None, "properties": None, "tags": None}])
    node = graph.nodes[f"{S}/Microsoft.Web/sites/web"]

    assert node.name == "web"               # falls back to the last id segment
    assert (node.type, node.resource_group, node.subscription) == ("", "", "")
    assert (node.location, node.kind, node.tags) == ("", "", {})


def test_a_row_with_null_properties_yields_no_edges_instead_of_crashing():
    graph = build_graph([
        {"id": f"{S}/Microsoft.Web/sites/web", "name": "web", "type": "microsoft.web/sites",
         "properties": None},
        {"id": f"{S}/Microsoft.KeyVault/vaults/kv", "name": "kv",
         "type": "microsoft.keyvault/vaults", "properties": None},
    ])

    assert len(graph.nodes) == 2
    assert graph.edges == []


def test_a_role_assignment_with_null_properties_contributes_no_edge():
    # Role assignments are read for their principal/scope; a null body is unusable
    # and must be ignored rather than fabricate an access path.
    graph = build_graph([
        {"id": f"{S}/Microsoft.KeyVault/vaults/kv", "name": "kv",
         "type": "microsoft.keyvault/vaults"},
        {"id": "/ra", "name": "ra", "type": "microsoft.authorization/roleassignments",
         "properties": None},
    ])

    assert graph.edges == []


def test_app_settings_with_null_names_and_values_are_skipped():
    graph = build_graph([
        {"id": f"{S}/Microsoft.Web/sites/web", "name": "web", "type": "microsoft.web/sites",
         "properties": {"siteConfig": {"appSettings": [{"name": None, "value": None},
                                                       {"name": "EMPTY"}]}}},
    ])

    assert graph.edges == []


def test_rows_without_an_id_are_skipped_rather_than_keyed_on_nothing():
    graph = build_graph([{"name": "orphan", "type": "microsoft.web/sites"},
                         {"id": f"{S}/Microsoft.Web/sites/web", "name": "web",
                          "type": "microsoft.web/sites"}])

    assert list(graph.nodes) == [f"{S}/Microsoft.Web/sites/web"]


def test_an_empty_result_set_builds_an_empty_graph():
    graph = build_graph([])

    assert (graph.nodes, graph.edges) == ({}, [])
    assert find_seeds(graph, "anything") == []


def test_a_repeated_row_does_not_duplicate_its_node_or_its_edges():
    web = {"id": f"{S}/Microsoft.Web/sites/web", "name": "web", "type": "microsoft.web/sites",
           "properties": {"serverFarmId": f"{S}/Microsoft.Web/serverfarms/plan"}}
    plan = {"id": f"{S}/Microsoft.Web/serverfarms/plan", "name": "plan",
            "type": "microsoft.web/serverfarms", "properties": {}}
    graph = build_graph([web, plan, dict(web), dict(plan)])

    assert len(graph.nodes) == 2
    assert [e.kind for e in graph.edges] == ["hosted-on"]


def test_an_export_whose_type_casing_comes_from_the_portal_is_still_recognised():
    # Raw exports carry mixed-case types ("Microsoft.Web/sites"); the adapter has
    # to claim them, and the node type is normalised to lower case.
    data = {"data": [{"id": f"{S}/Microsoft.Web/sites/web", "name": "web",
                      "type": "Microsoft.Web/sites", "properties": {}}]}

    assert AzureAdapter.matches(data) is True
    assert AzureAdapter.to_graph(data).nodes[f"{S}/Microsoft.Web/sites/web"].type \
        == "microsoft.web/sites"


def _two_apps_with_the_same_name():
    """The real situation the wizard hit: one name, two resource groups."""
    return [
        {"id": "/subscriptions/s/resourceGroups/rg1/providers/Microsoft.Web/sites/app",
         "name": "app", "type": "microsoft.web/sites", "resourceGroup": "rg1"},
        {"id": "/subscriptions/s/resourceGroups/rg2/providers/Microsoft.Web/sites/app",
         "name": "app", "type": "microsoft.web/sites", "resourceGroup": "rg2"},
    ]


def test_a_shared_name_is_reported_as_ambiguous():
    graph = build_graph(_two_apps_with_the_same_name())

    assert len(find_seeds(graph, "app")) == 2


def test_an_exact_arm_id_resolves_to_exactly_one_seed():
    # This is why the wizard passes the id and not the name: the id is unique even
    # when the name is not, so the trace can never be refused as ambiguous.
    rows = _two_apps_with_the_same_name()
    graph = build_graph(rows)

    assert find_seeds(graph, rows[1]["id"]) == [rows[1]["id"]]


def test_an_arm_id_copied_from_the_portal_matches_case_insensitively():
    rows = _two_apps_with_the_same_name()
    graph = build_graph(rows)

    assert find_seeds(graph, rows[0]["id"].upper()) == [rows[0]["id"]]


def test_a_name_substring_still_matches_when_nothing_is_exact():
    graph = build_graph([{"id": f"{S}/Microsoft.Web/sites/webapp-orders-dev",
                          "name": "webapp-orders-dev", "type": "microsoft.web/sites"}])

    assert find_seeds(graph, "orders") == [f"{S}/Microsoft.Web/sites/webapp-orders-dev"]


def test_an_exact_name_wins_over_a_longer_name_that_contains_it():
    graph = build_graph([{"id": f"{S}/Microsoft.Web/sites/api", "name": "api",
                          "type": "microsoft.web/sites"},
                         {"id": f"{S}/Microsoft.Web/sites/api-legacy", "name": "api-legacy",
                          "type": "microsoft.web/sites"}])

    assert find_seeds(graph, "api") == [f"{S}/Microsoft.Web/sites/api"]


def test_a_neutral_graph_json_is_reloaded_without_re_extracting(tmp_path):
    doc = tmp_path / "map.json"
    doc.write_text(
        '{"seed": "/web", "meta": {"truncated": true},'
        ' "nodes": [{"id": "/web", "name": "web", "type": "microsoft.web/sites", "hops": 0}],'
        ' "edges": []}', encoding="utf-8")

    graph = load_graph(str(doc))

    assert list(graph.nodes) == ["/web"]
    assert graph.meta["truncated"] is True
    assert graph.meta["seed"] == "/web"          # a reloaded map keeps its provenance


@pytest.mark.xfail(strict=True, reason="BUG: a microsoft.sql/servers/databases seed has an "
                                       "empty blast radius - no child-to-parent edge links a "
                                       "database to its server, and consumers' connection "
                                       "strings resolve to the server, so the wizard offers a "
                                       "seed type that can only ever produce an empty map")
def test_a_sql_database_seed_reaches_its_server_and_its_consumer():
    server = f"{S}/Microsoft.Sql/servers/sqlsrv"
    database = f"{server}/databases/orders"
    graph = build_graph([
        {"id": f"{S}/Microsoft.Web/sites/web", "name": "web", "type": "microsoft.web/sites",
         "properties": {"siteConfig": {"appSettings": [
             {"name": "CONN",
              "value": "Server=tcp:sqlsrv.database.windows.net,1433;Database=orders;"}]}}},
        {"id": server, "name": "sqlsrv", "type": "microsoft.sql/servers",
         "properties": {"fullyQualifiedDomainName": "sqlsrv.database.windows.net"}},
        {"id": database, "name": "orders", "type": "microsoft.sql/servers/databases",
         "properties": {}},
    ])
    reached = {n.name for n in blast_radius(graph, database).nodes.values()}

    assert "sqlsrv" in reached          # the database lives on that server
    assert "web" in reached             # and this is the app that would break
