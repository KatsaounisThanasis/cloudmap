"""End-to-end CLI behaviour for the artifacts a trace produces.

`trace` is the whole tool from the outside: pick a seed, write the files. These
tests drive `cloudmap.cli.main` with real argv and read the files back off disk -
the draw.io export is parsed as XML, the JSON artifact is parsed as JSON - so a
refactor of the export path cannot quietly stop writing one of them or drop the
provenance (truncated / read gaps / blind spots) that makes the artifact honest.

The live path is exercised too, with `query_live` replaced: no `az`, no network,
no subscription. A guard fixture makes any real subprocess call a test failure.
"""

import json
import os
import subprocess
import xml.etree.ElementTree as ET

import pytest

from cloudmap import cli
from cloudmap.ingest import azure

FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "contoso.json")
SEED_ID = ("/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/"
           "rg-contoso-app/providers/Microsoft.Web/sites/contoso-web")


@pytest.fixture(autouse=True)
def _no_process_and_no_pin(monkeypatch):
    def _forbidden(*a, **k):
        raise AssertionError(f"a test tried to run a real process: {a!r}")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setenv("CLOUDMAP_ALLOW_SUBSCRIPTION", "")
    monkeypatch.delenv("CLOUDMAP_ALLOW_SUBSCRIPTION")


def test_a_trace_writes_every_artifact_that_was_asked_for(tmp_path):
    rc = cli.main(["trace", SEED_ID, "--from", FIX, "--level", "detail",
                   "-o", str(tmp_path / "map.drawio"),
                   "--json", str(tmp_path / "map.json"),
                   "--mermaid", str(tmp_path / "map.mmd"),
                   "--html", str(tmp_path / "map.html")])

    assert rc == 0
    assert ET.parse(tmp_path / "map.drawio").getroot().tag == "mxfile"
    assert json.loads((tmp_path / "map.json").read_text(encoding="utf-8"))["seed"] == SEED_ID.lower()
    assert (tmp_path / "map.mmd").read_text(encoding="utf-8").startswith("graph LR")
    assert "<html" in (tmp_path / "map.html").read_text(encoding="utf-8").lower()


def test_only_the_requested_artifacts_are_written(tmp_path):
    cli.main(["trace", SEED_ID, "--from", FIX, "-o", str(tmp_path / "map.drawio")])

    assert sorted(p.name for p in tmp_path.iterdir()) == ["map.drawio"]


def test_the_default_output_name_is_derived_from_the_seed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    cli.main(["trace", SEED_ID, "--from", os.path.abspath(FIX)])

    assert (tmp_path / "contoso-web.blast.drawio").exists()


def test_a_nested_output_path_gets_its_directory_created(tmp_path):
    out = tmp_path / "deep" / "nested" / "map.drawio"

    cli.main(["trace", SEED_ID, "--from", FIX, "-o", str(out)])

    assert out.exists()


def test_an_unmatched_seed_writes_nothing_and_reports_failure(tmp_path):
    rc = cli.main(["trace", "no-such-resource", "--from", FIX,
                   "-o", str(tmp_path / "map.drawio")])

    assert rc == 2
    assert list(tmp_path.iterdir()) == []


def test_an_ambiguous_name_is_refused_before_any_file_is_written(tmp_path):
    # "contoso" matches every resource in the fixture: refuse, do not guess.
    rc = cli.main(["trace", "contoso", "--from", FIX, "-o", str(tmp_path / "map.drawio")])

    assert rc == 2
    assert list(tmp_path.iterdir()) == []


def _live(monkeypatch, resources, truncated):
    monkeypatch.setattr(azure, "query_live",
                        lambda allow_live=False, tenant_wide=True: (list(resources), truncated))


def _webapp_and_vault():
    S = "/subscriptions/s/resourceGroups/rg/providers"
    return [
        {"id": f"{S}/Microsoft.Web/sites/web", "name": "web", "type": "microsoft.web/sites",
         "resourceGroup": "rg", "properties": {"siteConfig": {"appSettings": [
             {"name": "KV", "value": "https://kv-1.vault.azure.net/secrets/db"}]}}},
        {"id": f"{S}/Microsoft.KeyVault/vaults/kv", "name": "kv-1",
         "type": "microsoft.keyvault/vaults", "resourceGroup": "rg",
         "properties": {"vaultUri": "https://kv-1.vault.azure.net/"}},
    ]


def test_a_truncated_live_scan_is_recorded_in_the_json_artifact(tmp_path, monkeypatch):
    rows = _webapp_and_vault()
    _live(monkeypatch, rows, True)

    rc = cli.main(["trace", rows[0]["id"], "--live", "--allow-live", "--enrich", "none",
                   "--level", "detail", "-o", str(tmp_path / "map.drawio"),
                   "--json", str(tmp_path / "map.json")])
    meta = json.loads((tmp_path / "map.json").read_text(encoding="utf-8"))["meta"]

    assert rc == 0
    assert meta["truncated"] is True
    assert meta["complete"] is False        # a partial scan never claims completeness


def test_an_un_enriched_workload_is_declared_as_a_blind_spot(tmp_path, monkeypatch):
    # --enrich none means config edges were never looked for; the artifact has to
    # say so, otherwise an empty upward view reads as "nothing depends on this".
    rows = _webapp_and_vault()
    _live(monkeypatch, rows, False)

    cli.main(["trace", rows[1]["id"], "--live", "--allow-live", "--enrich", "none",
              "--level", "detail", "-o", str(tmp_path / "map.drawio"),
              "--json", str(tmp_path / "map.json")])
    meta = json.loads((tmp_path / "map.json").read_text(encoding="utf-8"))["meta"]

    assert meta["blind_spots"]
    assert meta["complete"] is False


def test_a_live_trace_seeded_by_arm_id_renders_valid_xml(tmp_path, monkeypatch):
    rows = _webapp_and_vault()
    _live(monkeypatch, rows, False)

    cli.main(["trace", rows[0]["id"], "--live", "--allow-live", "--enrich", "none",
              "-o", str(tmp_path / "map.drawio")])
    labels = {o.get("label") for o in ET.parse(tmp_path / "map.drawio").getroot().iter("object")}

    assert "web (sites)" in labels
    assert "Key Vault (vaults)" in labels           # default view groups by type


def _capture_stubs(monkeypatch, rows, truncated=False):
    """Replace the live read and both deep-enrichers; record what they were given."""
    seen = {"webapps": None, "aks": None}
    _live(monkeypatch, rows, truncated)

    def webapps(raws, resolve_secrets=False):
        seen["webapps"] = [r["name"] for r in raws]
        return {"role_assignments": [{"id": "/ra", "name": "ra",
                                      "type": "microsoft.authorization/roleassignments",
                                      "properties": {}}],
                "diagnostics": [], "errors": ["web: RBAC denied"], "enriched": []}

    def aks(raws, resolve_secrets=False):
        seen["aks"] = [r["name"] for r in raws]
        return {"errors": [], "enriched": []}

    monkeypatch.setattr(azure, "enrich_webapps", webapps)
    monkeypatch.setattr(azure, "enrich_aks_clusters", aks)
    return seen


def _web_and_aks():
    S = "/subscriptions/s/resourceGroups/rg/providers"
    return [
        {"id": f"{S}/Microsoft.Web/sites/web", "name": "web", "type": "microsoft.web/sites",
         "resourceGroup": "rg", "properties": {}},
        {"id": f"{S}/Microsoft.ContainerService/managedClusters/aks", "name": "aks",
         "type": "microsoft.containerservice/managedclusters", "resourceGroup": "rg",
         "properties": {}},
    ]


def test_a_capture_deep_enriches_web_apps_and_aks_clusters(tmp_path, monkeypatch):
    # AKS config (Kubernetes manifests) is invisible to Resource Graph exactly the
    # way app settings are, so a capture that skips it silently loses those edges.
    seen = _capture_stubs(monkeypatch, _web_and_aks())

    rc = cli.main(["capture", "-o", str(tmp_path / "cap.json"), "--allow-live"])
    doc = json.loads((tmp_path / "cap.json").read_text(encoding="utf-8"))

    assert rc == 0
    assert seen["webapps"] == ["web"]
    assert seen["aks"] == ["aks"]
    assert doc["meta"]["enriched"] is True
    assert doc["meta"]["scrubbed"] is True          # scrubbed unless --no-scrub is asked for
    assert len(doc["data"]) == 3                    # both resources + the derived assignment


def test_a_capture_with_enrichment_off_reads_no_config(tmp_path, monkeypatch):
    seen = _capture_stubs(monkeypatch, _web_and_aks())

    cli.main(["capture", "-o", str(tmp_path / "cap.json"), "--allow-live", "--enrich", "none"])

    assert (seen["webapps"], seen["aks"]) == (None, None)


def test_a_capture_records_that_the_scan_was_truncated(tmp_path, monkeypatch):
    _capture_stubs(monkeypatch, _web_and_aks(), truncated=True)

    cli.main(["capture", "-o", str(tmp_path / "cap.json"), "--allow-live", "--enrich", "none"])
    doc = json.loads((tmp_path / "cap.json").read_text(encoding="utf-8"))

    assert doc["meta"]["truncated"] is True


def test_an_unscrubbed_capture_is_flagged_as_unscrubbed(tmp_path, monkeypatch):
    _capture_stubs(monkeypatch, _web_and_aks())

    cli.main(["capture", "-o", str(tmp_path / "cap.json"), "--allow-live", "--enrich", "none",
              "--no-scrub"])
    doc = json.loads((tmp_path / "cap.json").read_text(encoding="utf-8"))

    assert doc["meta"]["scrubbed"] is False


def test_no_subcommand_is_rejected_rather_than_falling_into_the_wizard():
    # argparse owns this: an unknown/absent subcommand must not start an
    # interactive session in a non-interactive context (CI, pipes).
    with pytest.raises(SystemExit):
        cli.main(["--nonsense"])


def test_a_truncated_capture_stays_truncated_when_retraced(tmp_path):
    # The capture said "I did not see everything". Re-tracing that file offline
    # must not launder the warning into complete:true.
    capture = tmp_path / "capture.json"
    capture.write_text(json.dumps({
        "data": [{"id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Web/sites/web",
                  "name": "web", "type": "microsoft.web/sites", "properties": {}}],
        "meta": {"truncated": True},
        "scrubbed": True,
    }), encoding="utf-8")

    out = tmp_path / "map.json"
    rc = cli.main(["trace", "web", "--from", str(capture), "--json", str(out)])
    meta = json.loads(out.read_text())["meta"]

    assert rc == 0
    assert meta["truncated"] is True
    assert meta["complete"] is False


def test_bare_cloudmap_without_a_terminal_refuses_instead_of_prompting(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": staticmethod(lambda: False)})())

    rc = cli.main([])

    assert rc == 2
    assert "terminal" in capsys.readouterr().err


def _summary_of(rows, seed, tmp_path, capsys, extra=()):
    fixture = tmp_path / "f.json"
    fixture.write_text(json.dumps({"data": rows}), encoding="utf-8")
    cli.main(["trace", seed, "--from", str(fixture), "--level", "detail",
              "-o", str(tmp_path / "m.drawio"), *extra])
    return capsys.readouterr().out


def test_the_terminal_shows_what_depends_on_the_seed_not_just_what_it_needs(tmp_path, capsys):
    # Shared infrastructure puts its whole story in the upward direction. A
    # single downward tree used to print two branches under a headline that
    # counted seven resources, hiding the dependents entirely.
    S = "/subscriptions/s/resourcegroups/rg/providers"
    vnet = f"{S}/microsoft.network/virtualnetworks/vnet-core"
    rows = [{"id": vnet, "name": "vnet-core", "type": "microsoft.network/virtualnetworks",
             "properties": {"ddosProtectionPlan": {"id": f"{S}/microsoft.network/ddosprotectionplans/ddos"}}},
            {"id": f"{S}/microsoft.network/ddosprotectionplans/ddos", "name": "ddos",
             "type": "microsoft.network/ddosprotectionplans", "properties": {}}]
    for i in range(3):
        rows.append({"id": f"{S}/microsoft.web/sites/app-{i}", "name": f"app-{i}",
                     "type": "microsoft.web/sites",
                     "properties": {"virtualNetworkSubnetId": f"{vnet}/subnets/sn"}})

    out = _summary_of(rows, "vnet-core", tmp_path, capsys)

    assert "Depends on" in out and "What depends on it" in out
    for i in range(3):
        assert f"app-{i}" in out                  # the dependents are visible now
    assert "ddos" in out


def test_an_upward_edge_is_drawn_pointing_back_at_the_seed(tmp_path, capsys):
    # Printing an inbound edge like an outbound one would state the dependency
    # backwards ("vnet depends on app"), so the arrow is reversed.
    S = "/subscriptions/s/resourcegroups/rg/providers"
    vnet = f"{S}/microsoft.network/virtualnetworks/vnet"
    rows = [{"id": vnet, "name": "vnet", "type": "microsoft.network/virtualnetworks",
             "properties": {}},
            {"id": f"{S}/microsoft.web/sites/app", "name": "app", "type": "microsoft.web/sites",
             "properties": {"virtualNetworkSubnetId": f"{vnet}/subnets/sn"}}]

    out = _summary_of(rows, "vnet", tmp_path, capsys)

    assert "<--vnet-integration--" in out


def test_a_seed_with_no_dependents_still_prints_its_dependencies(tmp_path, capsys):
    S = "/subscriptions/s/resourcegroups/rg/providers"
    rows = [{"id": f"{S}/microsoft.web/sites/app", "name": "app", "type": "microsoft.web/sites",
             "properties": {"serverFarmId": f"{S}/microsoft.web/serverfarms/plan"}},
            {"id": f"{S}/microsoft.web/serverfarms/plan", "name": "plan",
             "type": "microsoft.web/serverfarms", "properties": {}}]

    out = _summary_of(rows, "app", tmp_path, capsys)

    assert "Depends on" in out and "plan" in out
    assert "What depends on it" not in out


def test_an_observer_is_labelled_as_observing_rather_than_depending(tmp_path, capsys):
    S = "/subscriptions/s/resourcegroups/rg/providers"
    st = f"{S}/microsoft.storage/storageaccounts/st"
    rows = [{"id": st, "name": "st", "type": "microsoft.storage/storageaccounts",
             "properties": {}},
            {"id": f"{S}/microsoft.insights/metricalerts/alert", "name": "alert",
             "type": "microsoft.insights/metricalerts", "properties": {"scopes": [st]}}]

    out = _summary_of(rows, "st", tmp_path, capsys)

    assert "observes" in out and "alert" in out
