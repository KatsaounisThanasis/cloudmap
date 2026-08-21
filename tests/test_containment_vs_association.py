"""Containment is not dependency, and an observer is not a dependent.

Azure's own topology model (Network Watcher) separates CONTAINMENT from
ASSOCIATION, and a blast-radius map has to respect the difference:

  * A VNet's `subnets[].ipConfigurations` enumerate what is plugged INTO it.
    Following those turns a shared VNet into a hub that drags every unrelated
    app on the subscription onto the map. The dependents already point AT the
    fabric, which is the correct direction.
  * A VNet's `ddosProtectionPlan`, an NSG rule's application security group, a
    peering's remote VNet: these are associations, and they are real
    dependencies. An earlier version suppressed the fabric TYPES wholesale and
    silently lost every one of them.

So the suppression is on property paths, not on types. These tests pin both
halves: the associations must survive, and the hub must stay dead.

Everything here is synthetic - no tenant was consulted to write it.
"""

from cloudmap.graph import blast_radius, build_graph, find_seeds

S = "/subscriptions/s/resourcegroups/rg/providers"
VNET = f"{S}/microsoft.network/virtualnetworks/vnet"


def _targets(graph, src):
    return {(graph.nodes[e.target].name, e.kind) for e in graph.edges if e.source == src}


# --- associations: real dependencies that must be on the map ----------------------

def test_a_vnet_depends_on_its_ddos_protection_plan():
    ddos = f"{S}/microsoft.network/ddosprotectionplans/ddos"
    graph = build_graph([
        {"id": ddos, "name": "ddos", "type": "microsoft.network/ddosprotectionplans",
         "properties": {}},
        {"id": VNET, "name": "vnet", "type": "microsoft.network/virtualnetworks",
         "properties": {"enableDdosProtection": True, "ddosProtectionPlan": {"id": ddos}}},
    ])

    assert ("ddos", "references") in _targets(graph, VNET)


def test_an_nsg_rule_referencing_an_application_security_group_is_an_edge():
    asg = f"{S}/microsoft.network/applicationsecuritygroups/asg"
    nsg = f"{S}/microsoft.network/networksecuritygroups/nsg"
    graph = build_graph([
        {"id": asg, "name": "asg", "type": "microsoft.network/applicationsecuritygroups",
         "properties": {}},
        {"id": nsg, "name": "nsg", "type": "microsoft.network/networksecuritygroups",
         "properties": {"securityRules": [
             {"name": "allow-web", "properties": {
                 "destinationApplicationSecurityGroups": [{"id": asg}]}}]}},
    ])

    assert ("asg", "references") in _targets(graph, nsg)


def test_a_vnet_peering_links_the_two_vnets():
    other = f"{S}/microsoft.network/virtualnetworks/vnet-b"
    graph = build_graph([
        {"id": other, "name": "vnet-b", "type": "microsoft.network/virtualnetworks",
         "properties": {}},
        {"id": VNET, "name": "vnet", "type": "microsoft.network/virtualnetworks",
         "properties": {"virtualNetworkPeerings": [
             {"name": "to-b", "properties": {"remoteVirtualNetwork": {"id": other}}}]}},
    ])

    assert ("vnet-b", "references") in _targets(graph, VNET)


# --- containment: must NOT become a dependency edge -------------------------------

def _estate_with_a_shared_vnet(subnets=3, nics_per_subnet=5):
    """One VNet, several subnets, each listing unrelated NICs - plus one app
    integrated into a single subnet. This is the shape that once ballooned a
    16-resource map into 128."""
    vnet_subnets = []
    rows = []
    for i in range(subnets):
        ipconfigs = []
        for j in range(nics_per_subnet):
            nic = f"{S}/microsoft.network/networkinterfaces/nic-{i}-{j}"
            rows.append({"id": nic, "name": f"nic-{i}-{j}",
                         "type": "microsoft.network/networkinterfaces", "properties": {}})
            ipconfigs.append({"id": f"{nic}/ipConfigurations/ipconfig1"})
        vnet_subnets.append({"name": f"sn{i}", "properties": {"ipConfigurations": ipconfigs}})

    rows.append({"id": VNET, "name": "vnet", "type": "microsoft.network/virtualnetworks",
                 "properties": {"subnets": vnet_subnets}})
    rows.append({"id": f"{S}/microsoft.web/sites/app", "name": "app",
                 "type": "microsoft.web/sites",
                 "properties": {"virtualNetworkSubnetId": f"{VNET}/subnets/sn0"}})
    return rows


def test_a_vnet_does_not_become_a_hub_through_the_things_plugged_into_it():
    graph = build_graph(_estate_with_a_shared_vnet())

    assert _targets(graph, VNET) == set()        # the VNet depends on nothing here


def test_tracing_an_app_does_not_drag_in_every_nic_on_the_shared_vnet():
    graph = build_graph(_estate_with_a_shared_vnet())
    sub = blast_radius(graph, find_seeds(graph, "app")[0])

    assert {n.name for n in sub.nodes.values()} == {"app", "vnet"}


def test_the_storage_side_of_a_private_endpoint_does_not_claim_it():
    # A storage account lists its privateEndpointConnections; the PE is what
    # depends on the storage, not the other way round.
    st = f"{S}/microsoft.storage/storageaccounts/st"
    pe = f"{S}/microsoft.network/privateendpoints/pe"
    graph = build_graph([
        {"id": pe, "name": "pe", "type": "microsoft.network/privateendpoints",
         "properties": {"privateLinkServiceConnections": [
             {"properties": {"privateLinkServiceId": st}}]}},
        {"id": st, "name": "st", "type": "microsoft.storage/storageaccounts",
         "properties": {"privateEndpointConnections": [
             {"properties": {"privateEndpoint": {"id": pe}}}]}},
    ])

    assert _targets(graph, st) == set()
    assert "pe" in {graph.nodes[e.source].name for e in graph.edges}


# --- observers: on the map, but marked, and never traversed through ---------------

def test_a_metric_alert_observes_its_target_instead_of_disappearing():
    st = f"{S}/microsoft.storage/storageaccounts/st"
    alert = f"{S}/microsoft.insights/metricalerts/alert"
    graph = build_graph([
        {"id": st, "name": "st", "type": "microsoft.storage/storageaccounts", "properties": {}},
        {"id": alert, "name": "alert", "type": "microsoft.insights/metricalerts",
         "properties": {"scopes": [st]}},
    ])

    assert ("st", "observes") in _targets(graph, alert)


def test_an_observer_shows_up_when_asking_what_breaks_if_i_touch_this():
    st = f"{S}/microsoft.storage/storageaccounts/st"
    alert = f"{S}/microsoft.portal/dashboards/board"
    graph = build_graph([
        {"id": st, "name": "st", "type": "microsoft.storage/storageaccounts", "properties": {}},
        {"id": alert, "name": "board", "type": "microsoft.portal/dashboards",
         "properties": {"lenses": {"0": {"parts": {"0": {"metadata": {"resourceId": st}}}}}},
         },
    ])
    up = blast_radius(graph, st, direction="up")

    assert "board" in {n.name for n in up.nodes.values()}


def test_an_observer_does_not_pull_its_own_dependencies_onto_the_map():
    # Direction consistency already forbids reversing, so a dashboard reached
    # upward from storage must not drag in whatever else it displays.
    st = f"{S}/microsoft.storage/storageaccounts/st"
    other = f"{S}/microsoft.documentdb/databaseaccounts/cosmos"
    board = f"{S}/microsoft.portal/dashboards/board"
    graph = build_graph([
        {"id": st, "name": "st", "type": "microsoft.storage/storageaccounts", "properties": {}},
        {"id": other, "name": "cosmos", "type": "microsoft.documentdb/databaseaccounts",
         "properties": {}},
        {"id": board, "name": "board", "type": "microsoft.portal/dashboards",
         "properties": {"watches": [st, other]}},
    ])
    up = blast_radius(graph, st, direction="up")

    assert "board" in {n.name for n in up.nodes.values()}
    assert "cosmos" not in {n.name for n in up.nodes.values()}


# --- shared platform infrastructure is not identity -------------------------------

def test_two_apps_on_the_same_scale_unit_are_not_dependencies():
    """Regression from a live trace.

    Azure reports the App Service scale unit a web app runs on in
    `ftpsHostName`, so two unrelated apps on the same unit report the SAME host.
    Reading that as identity made one app "depend on" the other and dragged its
    entire subtree onto the map: a 6-resource trace became 10.
    """
    a = f"{S}/microsoft.web/sites/app-a"
    b = f"{S}/microsoft.web/sites/app-b"
    shared = "waws-prod-am2-735.ftp.azurewebsites.windows.net"
    graph = build_graph([
        {"id": a, "name": "app-a", "type": "microsoft.web/sites",
         "properties": {"defaultHostName": "app-a.azurewebsites.net",
                        "ftpsHostName": shared}},
        {"id": b, "name": "app-b", "type": "microsoft.web/sites",
         "properties": {"defaultHostName": "app-b.azurewebsites.net",
                        "ftpsHostName": shared}},
    ])

    assert graph.edges == []


def test_a_host_two_resources_both_advertise_belongs_to_neither():
    # The general rule behind the fix: identity has to be unique. If two
    # resources claim the same endpoint it is shared infrastructure, and
    # resolving it would invent a dependency between unrelated things.
    one = f"{S}/microsoft.madeup/things/one"
    two = f"{S}/microsoft.madeup/things/two"
    consumer = f"{S}/microsoft.web/sites/app"
    graph = build_graph([
        {"id": one, "name": "one", "type": "microsoft.madeup/things",
         "properties": {"endpoint": "https://shared.example.net"}},
        {"id": two, "name": "two", "type": "microsoft.madeup/things",
         "properties": {"endpoint": "https://shared.example.net"}},
        {"id": consumer, "name": "app", "type": "microsoft.web/sites",
         "properties": {"siteConfig": {"appSettings": [
             {"name": "URL", "value": "https://shared.example.net/api"}]}}},
    ])

    assert _targets(graph, consumer) == set()


def test_a_uniquely_advertised_host_still_resolves():
    # The ambiguity rule must not break the normal case it was built for.
    target = f"{S}/microsoft.madeup/things/only-one"
    consumer = f"{S}/microsoft.web/sites/app"
    graph = build_graph([
        {"id": target, "name": "only-one", "type": "microsoft.madeup/things",
         "properties": {"endpoint": "https://only-one.example.net"}},
        {"id": consumer, "name": "app", "type": "microsoft.web/sites",
         "properties": {"store": {"url": "https://only-one.example.net"}}},
    ])

    assert ("only-one", "connects-to") in _targets(graph, consumer) or \
           ("only-one", "references") in _targets(graph, consumer)
