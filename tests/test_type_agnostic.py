"""Conformance: the extraction mechanisms must not depend on the resource type.

cloudmap tells people it maps any Azure resource. That claim is only honest if
the mechanisms are type-agnostic by construction, so this file takes a broad
spread of real provider types - including ones with no hand-written rule, and
deliberately invented ones - and drives each documented dependency channel
through every single one of them.

The channels a dependency can be expressed through:

  1. an ARM resource id inside properties      -> generic ARM-reference pass
  2. a host name the target advertises          -> generic host indexing
  3. the parent/child shape of the ARM id       -> nested-child pass
  4. an RBAC role assignment                    -> role extraction

What is deliberately NOT claimed here: configuration that ARM does not return
in `properties` and that needs a per-type API call (web app appSettings, AKS
manifests). Those have their own readers and their own blind-spot reporting;
see test_enrich.py.
"""

import pathlib

import pytest

from cloudmap.graph import build_graph

S = "/subscriptions/s/resourcegroups/rg/providers"

# A spread across providers: some have typed rules, some have none, and the last
# few do not exist at all. Nothing in the mechanisms may care which is which.
TYPES = [
    "microsoft.web/sites",
    "microsoft.containerservice/managedclusters",
    "microsoft.app/containerapps",
    "microsoft.logic/workflows",
    "microsoft.datafactory/factories",
    "microsoft.eventgrid/topics",
    "microsoft.signalrservice/signalr",
    "microsoft.apimanagement/service",
    "microsoft.batch/batchaccounts",
    "microsoft.streamanalytics/streamingjobs",
    "microsoft.synapse/workspaces",
    "microsoft.databricks/workspaces",
    "microsoft.hdinsight/clusters",
    "microsoft.kusto/clusters",
    "microsoft.purview/accounts",
    "microsoft.healthcareapis/services",
    "microsoft.digitaltwins/digitaltwinsinstances",
    "microsoft.iothub/iothubs",
    "microsoft.maps/accounts",
    "microsoft.media/mediaservices",
    "microsoft.relay/namespaces",
    "microsoft.notificationhubs/namespaces",
    "microsoft.powerbidedicated/capacities",
    "microsoft.timeseriesinsights/environments",
    "microsoft.automation/automationaccounts",
    "microsoft.recoveryservices/vaults",
    "microsoft.desktopvirtualization/hostpools",
    "microsoft.madeup/widgets",              # invented on purpose
    "contoso.custom/thingamajigs",           # third-party provider shape
    "microsoft.futureservice/somethingnew",  # a type that does not exist yet
]

TARGET = f"{S}/microsoft.keyvault/vaults/kv-target"
TARGET_ROW = {"id": TARGET, "name": "kv-target", "type": "microsoft.keyvault/vaults",
              "properties": {"vaultUri": "https://kv-target.vault.azure.net/"}}


def _edges_from(graph, source_id):
    return {e.target for e in graph.edges if e.source == source_id}


@pytest.mark.parametrize("rtype", TYPES)
def test_channel_1_an_arm_id_in_properties_is_mapped_for_any_type(rtype):
    src = f"{S}/{rtype}/thing"
    graph = build_graph([
        TARGET_ROW,
        {"id": src, "name": "thing", "type": rtype,
         "properties": {"someSettings": {"vaultResourceId": TARGET}}},
    ])

    assert TARGET in _edges_from(graph, src)
    assert all(e.evidence for e in graph.edges)        # never an unfalsifiable claim


@pytest.mark.parametrize("rtype", TYPES)
def test_channel_2_a_host_the_target_advertises_is_mapped_for_any_type(rtype):
    # The SOURCE is the one with no rule here: it names the target by hostname
    # in free text, exactly as a connection string would.
    src = f"{S}/{rtype}/thing"
    graph = build_graph([
        TARGET_ROW,
        {"id": src, "name": "thing", "type": rtype,
         "properties": {"siteConfig": {"appSettings": [
             {"name": "KV", "value": "https://kv-target.vault.azure.net/secrets/x"}]}}},
    ])
    # Web-app-shaped config is only read for config workloads; for every other
    # type the same host still resolves through the generic pass when it appears
    # as a plain property value.
    graph2 = build_graph([
        TARGET_ROW,
        {"id": src, "name": "thing", "type": rtype,
         "properties": {"store": {"endpointUrl": "https://kv-target.vault.azure.net/"}}},
    ])

    assert TARGET in _edges_from(graph, src) or TARGET in _edges_from(graph2, src)


@pytest.mark.parametrize("rtype", TYPES)
def test_channel_3_a_child_resource_is_linked_to_its_parent_for_any_type(rtype):
    parent = f"{S}/{rtype}/thing"
    child = f"{parent}/children/kid"
    graph = build_graph([
        {"id": parent, "name": "thing", "type": rtype, "properties": {}},
        {"id": child, "name": "kid", "type": f"{rtype}/children", "properties": {}},
    ])

    assert parent in _edges_from(graph, child)


@pytest.mark.parametrize("rtype", TYPES)
def test_channel_4_rbac_access_is_mapped_for_any_type(rtype):
    src = f"{S}/{rtype}/thing"
    graph = build_graph([
        TARGET_ROW,
        {"id": src, "name": "thing", "type": rtype,
         "identity": {"principalId": "11111111-1111-1111-1111-111111111111"},
         "properties": {}},
        {"id": "/ra1", "name": "ra1", "type": "microsoft.authorization/roleassignments",
         "properties": {"principalId": "11111111-1111-1111-1111-111111111111",
                        "scope": TARGET, "roleDefinitionName": "Key Vault Secrets User"}},
    ])

    assert TARGET in _edges_from(graph, src)


@pytest.mark.parametrize("rtype", TYPES)
def test_any_type_can_be_a_seed_and_reaches_its_dependency(rtype):
    from cloudmap.graph import blast_radius, find_seeds

    src = f"{S}/{rtype}/thing"
    graph = build_graph([
        TARGET_ROW,
        {"id": src, "name": "thing", "type": rtype,
         "properties": {"linked": {"vaultId": TARGET}}},
    ])
    seeds = find_seeds(graph, "thing")
    sub = blast_radius(graph, seeds[0])

    assert seeds == [src]
    assert TARGET in sub.nodes


def test_the_reverse_view_works_for_a_type_with_no_rule():
    # "What depends on this?" must work when the DEPENDENT is an unknown type.
    src = f"{S}/microsoft.madeup/widgets/widget"
    graph = build_graph([
        TARGET_ROW,
        {"id": src, "name": "widget", "type": "microsoft.madeup/widgets",
         "properties": {"linked": {"vaultId": TARGET}}},
    ])
    from cloudmap.graph import blast_radius
    up = blast_radius(graph, TARGET, direction="up")

    assert src in up.nodes


# --- the whole Azure catalogue ----------------------------------------------------

CATALOGUE = pathlib.Path(__file__).parent / "data" / "azure_resource_types.txt"

# Role assignments are plumbing we derive edges FROM, so they are the one type
# that never acts as a source. Fabric and observer types DO produce edges now:
# what is suppressed is the containment property path (a VNet listing what is
# plugged into it), not the type.
NOT_A_DEPENDENCY_SOURCE = {"microsoft.authorization/roleassignments"}


def _all_types():
    return [ln.strip() for ln in CATALOGUE.read_text().splitlines()
            if ln.strip() and not ln.startswith("#")]


def _arm_id(rtype, base="thing"):
    """A valid ARM id for a type at any nesting depth: ns/a/b -> .../ns/a/x/b/y."""
    ns, *segs = rtype.split("/")
    parts = [f"{S}/{ns}"]
    for i, seg in enumerate(segs):
        parts.append(f"{seg}/{base}{i if i else ''}")
    return "/".join(parts)


def test_the_catalogue_is_the_real_thing():
    types = _all_types()

    assert len(types) > 4000                                   # ~4.7k as captured
    for known in ("microsoft.compute/virtualmachines", "microsoft.cache/redis",
                  "microsoft.containerservice/managedclusters", "microsoft.web/sites",
                  "microsoft.sql/servers/databases"):
        assert known in types


@pytest.mark.parametrize("channel", ["arm-id", "hostname", "rbac", "seed"])
def test_every_azure_resource_type_is_mapped(channel):
    """Sweep all ~4700 published Azure resource types through one channel each.

    This is the evidence behind "cloudmap maps any Azure resource": the
    extraction mechanisms never branch on the type, so the catalogue is a
    fair test rather than a sample. The only permitted failures are the
    fabric and observer types, which are excluded on purpose.
    """
    from cloudmap.graph import blast_radius, find_seeds

    pid = "11111111-1111-1111-1111-111111111111"
    unexpected = []

    for rtype in _all_types():
        src = _arm_id(rtype)
        row = {"id": src, "name": src.rsplit("/", 1)[-1], "type": rtype}

        if channel == "arm-id":
            rows = [TARGET_ROW, dict(row, properties={"cfg": {"vaultResourceId": TARGET}})]
        elif channel == "hostname":
            rows = [TARGET_ROW,
                    dict(row, properties={"store": {"url": "https://kv-target.vault.azure.net/"}})]
        elif channel == "rbac":
            rows = [TARGET_ROW,
                    dict(row, identity={"principalId": pid}, properties={}),
                    {"id": "/ra", "name": "ra",
                     "type": "microsoft.authorization/roleassignments",
                     "properties": {"principalId": pid, "scope": TARGET,
                                    "roleDefinitionName": "Key Vault Secrets User"}}]
        else:
            rows = [TARGET_ROW, dict(row, properties={"cfg": {"vaultResourceId": TARGET}})]

        graph = build_graph(rows)
        if channel == "seed":
            seeds = find_seeds(graph, src)
            reached = bool(seeds) and TARGET in blast_radius(graph, seeds[0]).nodes
        else:
            reached = TARGET in _edges_from(graph, src)

        # RBAC and the child-of pass work even for the excluded source types.
        excluded = rtype in NOT_A_DEPENDENCY_SOURCE and channel != "rbac"
        if reached == excluded:
            unexpected.append(rtype)

    assert not unexpected, f"{len(unexpected)} type(s) behaved unexpectedly: {unexpected[:10]}"


def test_every_azure_resource_type_links_to_its_parent():
    unexpected = [rtype for rtype in _all_types()
                  if _arm_id(rtype) not in {
                      e.target for e in build_graph([
                          {"id": _arm_id(rtype), "name": "thing", "type": rtype, "properties": {}},
                          {"id": f"{_arm_id(rtype)}/children/kid", "name": "kid",
                           "type": f"{rtype}/children", "properties": {}},
                      ]).edges
                      if e.source == f"{_arm_id(rtype)}/children/kid"}]

    assert not unexpected, f"{len(unexpected)} type(s) lost their child link: {unexpected[:10]}"
