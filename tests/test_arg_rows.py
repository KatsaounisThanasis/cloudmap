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

S = "/subscriptions/s/resourcegroups/rg/providers"


def test_null_columns_in_a_row_fall_back_to_empty_values():
    # ARG projects every requested column, so an unset one arrives as null.
    graph = build_graph([{"id": f"{S}/microsoft.web/sites/web", "name": None, "type": None,
                          "resourceGroup": None, "subscriptionId": None, "location": None,
                          "kind": None, "identity": None, "properties": None, "tags": None}])
    node = graph.nodes[f"{S}/microsoft.web/sites/web"]

    assert node.name == "web"               # falls back to the last id segment
    assert (node.type, node.resource_group, node.subscription) == ("", "", "")
    assert (node.location, node.kind, node.tags) == ("", "", {})


def test_a_row_with_null_properties_yields_no_edges_instead_of_crashing():
    graph = build_graph([
        {"id": f"{S}/microsoft.web/sites/web", "name": "web", "type": "microsoft.web/sites",
         "properties": None},
        {"id": f"{S}/microsoft.keyvault/vaults/kv", "name": "kv",
         "type": "microsoft.keyvault/vaults", "properties": None},
    ])

    assert len(graph.nodes) == 2
    assert graph.edges == []


def test_a_role_assignment_with_null_properties_contributes_no_edge():
    # Role assignments are read for their principal/scope; a null body is unusable
    # and must be ignored rather than fabricate an access path.
    graph = build_graph([
        {"id": f"{S}/microsoft.keyvault/vaults/kv", "name": "kv",
         "type": "microsoft.keyvault/vaults"},
        {"id": "/ra", "name": "ra", "type": "microsoft.authorization/roleassignments",
         "properties": None},
    ])

    assert graph.edges == []


def test_app_settings_with_null_names_and_values_are_skipped():
    graph = build_graph([
        {"id": f"{S}/microsoft.web/sites/web", "name": "web", "type": "microsoft.web/sites",
         "properties": {"siteConfig": {"appSettings": [{"name": None, "value": None},
                                                       {"name": "EMPTY"}]}}},
    ])

    assert graph.edges == []


def test_rows_without_an_id_are_skipped_rather_than_keyed_on_nothing():
    graph = build_graph([{"name": "orphan", "type": "microsoft.web/sites"},
                         {"id": f"{S}/microsoft.web/sites/web", "name": "web",
                          "type": "microsoft.web/sites"}])

    assert list(graph.nodes) == [f"{S}/microsoft.web/sites/web"]


def test_an_empty_result_set_builds_an_empty_graph():
    graph = build_graph([])

    assert (graph.nodes, graph.edges) == ({}, [])
    assert find_seeds(graph, "anything") == []


def test_a_repeated_row_does_not_duplicate_its_node_or_its_edges():
    web = {"id": f"{S}/microsoft.web/sites/web", "name": "web", "type": "microsoft.web/sites",
           "properties": {"serverFarmId": f"{S}/microsoft.web/serverfarms/plan"}}
    plan = {"id": f"{S}/microsoft.web/serverfarms/plan", "name": "plan",
            "type": "microsoft.web/serverfarms", "properties": {}}
    graph = build_graph([web, plan, dict(web), dict(plan)])

    assert len(graph.nodes) == 2
    assert [e.kind for e in graph.edges] == ["hosted-on"]


def test_an_export_whose_type_casing_comes_from_the_portal_is_still_recognised():
    # Raw exports carry mixed-case types ("microsoft.web/sites"); the adapter has
    # to claim them, and the node type is normalised to lower case.
    data = {"data": [{"id": f"{S}/microsoft.web/sites/web", "name": "web",
                      "type": "microsoft.web/sites", "properties": {}}]}

    assert AzureAdapter.matches(data) is True
    assert AzureAdapter.to_graph(data).nodes[f"{S}/microsoft.web/sites/web"].type \
        == "microsoft.web/sites"


def _two_apps_with_the_same_name():
    """The real situation the wizard hit: one name, two resource groups."""
    return [
        {"id": "/subscriptions/s/resourcegroups/rg1/providers/microsoft.web/sites/app",
         "name": "app", "type": "microsoft.web/sites", "resourceGroup": "rg1"},
        {"id": "/subscriptions/s/resourcegroups/rg2/providers/microsoft.web/sites/app",
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
    graph = build_graph([{"id": f"{S}/microsoft.web/sites/webapp-orders-dev",
                          "name": "webapp-orders-dev", "type": "microsoft.web/sites"}])

    assert find_seeds(graph, "orders") == [f"{S}/microsoft.web/sites/webapp-orders-dev"]


def test_an_exact_name_wins_over_a_longer_name_that_contains_it():
    graph = build_graph([{"id": f"{S}/microsoft.web/sites/api", "name": "api",
                          "type": "microsoft.web/sites"},
                         {"id": f"{S}/microsoft.web/sites/api-legacy", "name": "api-legacy",
                          "type": "microsoft.web/sites"}])

    assert find_seeds(graph, "api") == [f"{S}/microsoft.web/sites/api"]


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


def test_a_sql_database_seed_reaches_its_server_and_its_consumer():
    # Regression for a real bug: a database seed used to produce an empty map -
    # no child-of edge linked it to its server, and consumers' connection strings
    # resolved only to the server. Fixed by the nested-child pass + landing
    # config edges on the scanned database itself.
    server = f"{S}/microsoft.sql/servers/sqlsrv"
    database = f"{server}/databases/orders"
    graph = build_graph([
        {"id": f"{S}/microsoft.web/sites/web", "name": "web", "type": "microsoft.web/sites",
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


def test_a_type_with_no_rule_is_resolved_by_the_host_it_advertises():
    # A provider nobody wrote a rule for, which states its own endpoint. The app
    # names that host in a setting. Before generic host indexing the Resolver
    # only indexed hosts for ~16 known types, so this edge did not exist and the
    # target surfaced as an unverified external instead of the real resource.
    graph = build_graph([
        {"id": f"{S}/microsoft.web/sites/web", "name": "web", "type": "microsoft.web/sites",
         "properties": {"siteConfig": {"appSettings": [
             {"name": "FEED_URL", "value": "https://analytics.contoso-feeds.io/v1/push"}]}}},
        {"id": f"{S}/microsoft.madeup/feeds/analytics", "name": "analytics",
         "type": "microsoft.madeup/feeds",
         "properties": {"endpoint": "https://analytics.contoso-feeds.io"}},
    ])
    pairs = {(g.nodes[e.source].name, g.nodes[e.target].name)
             for g in [graph] for e in graph.edges}

    assert ("web", "analytics") in pairs
    assert all(not n.external for n in graph.nodes.values())   # verified, not a guess


def test_a_nested_fqdn_is_indexed_too():
    graph = build_graph([
        {"id": f"{S}/microsoft.web/sites/web", "name": "web", "type": "microsoft.web/sites",
         "properties": {"siteConfig": {"appSettings": [
             {"name": "BROKER", "value": "amqps://broker.example-bus.net:5671"}]}}},
        {"id": f"{S}/microsoft.madeup/brokers/broker", "name": "broker",
         "type": "microsoft.madeup/brokers",
         "properties": {"networking": {"public": {"fqdn": "broker.example-bus.net"}}}},
    ])
    pairs = {(graph.nodes[e.source].name, graph.nodes[e.target].name) for e in graph.edges}

    assert ("web", "broker") in pairs


def test_a_resource_does_not_claim_a_host_it_merely_references():
    # kv-real owns the vault host. The made-up resource only POINTS at it, under
    # a key that is not self-describing, so it must not steal the mapping.
    graph = build_graph([
        {"id": f"{S}/microsoft.keyvault/vaults/kv-real", "name": "kv-real",
         "type": "microsoft.keyvault/vaults",
         "properties": {"vaultUri": "https://kv-real.vault.azure.net/"}},
        {"id": f"{S}/microsoft.madeup/things/thing", "name": "thing",
         "type": "microsoft.madeup/things",
         "properties": {"secretStore": "https://kv-real.vault.azure.net/"}},
        {"id": f"{S}/microsoft.web/sites/web", "name": "web", "type": "microsoft.web/sites",
         "properties": {"siteConfig": {"appSettings": [
             {"name": "KV", "value": "https://kv-real.vault.azure.net/secrets/x"}]}}},
    ])
    targets = {graph.nodes[e.target].name for e in graph.edges
               if graph.nodes[e.source].name == "web"}

    assert "kv-real" in targets
    assert "thing" not in targets


def test_generic_host_indexing_requires_the_host_to_carry_the_resource_name():
    """A deliberate limit, chosen after a live trace produced a false edge.

    The generic fallback claims a host only when its first DNS label is the
    resource's own name, which is how Azure endpoints are almost always shaped.
    The alternative - trusting any host under a key called `url` or `endpoint` -
    let one resource claim an address it merely POINTED AT, and let two web apps
    on the same App Service scale unit look like dependencies of each other.
    Types whose endpoint does not carry their name get a typed branch instead
    (public IP FQDNs, for example, are indexed that way).
    """
    graph = build_graph([
        {"id": f"{S}/microsoft.madeup/things/thing", "name": "thing",
         "type": "microsoft.madeup/things",
         "properties": {"endpoint": "https://something-else.example.net"}},
        {"id": f"{S}/microsoft.web/sites/web", "name": "web", "type": "microsoft.web/sites",
         "properties": {"siteConfig": {"appSettings": [
             {"name": "URL", "value": "https://something-else.example.net"}]}}},
    ])

    assert graph.edges == []          # a miss, not an invented dependency
