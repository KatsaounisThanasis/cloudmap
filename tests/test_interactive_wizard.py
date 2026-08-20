"""The zero-argument wizard: what it asks, and what it hands to `trace`.

The wizard is the path most people take, and it is the only place that turns a
picked ARG row into CLI arguments. It is also optional-dependency territory
(questionary + rich), so the prompts are faked here: no terminal, no Azure CLI,
no subprocess.

Since a3e985c the wizard no longer builds its own `az` command line; it delegates
to `cloudmap.ingest.azure._az` and `._graph_paged` (imported inside
`interactive_main`, so patching them on the source module is what takes effect).
Everything is therefore asserted through those two calls and the argv the wizard
finally passes to `cloudmap.cli.main`.
"""

import contextlib
import json
import subprocess
import types

import pytest

from cloudmap import cli, interactive
from cloudmap.ingest import azure

WORKLOAD_TYPES = (
    "microsoft.web/sites",
    "microsoft.containerservice/managedclusters",
    "microsoft.app/containerapps",
    "microsoft.compute/virtualmachines",
    "microsoft.apimanagement/service",
    "microsoft.sql/servers/databases",
    "microsoft.dbforpostgresql/flexibleservers",
)


@pytest.fixture(autouse=True)
def _no_real_az(monkeypatch):
    """No test in this file may reach a real `az`. If one does, fail loudly."""

    def forbidden(*a, **k):
        raise AssertionError(f"test tried to run a real subprocess: {a!r}")

    monkeypatch.setattr(subprocess, "run", forbidden)


class FakeConsole:
    """Stand-in for rich.Console: records what the user would have been told."""

    last = None

    def __init__(self, *a, **k):
        self.messages = []
        FakeConsole.last = self

    def print(self, *a, **k):
        self.messages.append(" ".join(str(x) for x in a))

    def status(self, *a, **k):
        return contextlib.nullcontext()

    def said(self, needle):
        return [m for m in self.messages if needle.lower() in m.lower()]


class FakeChoice:
    def __init__(self, title=None, value=None, *a, **k):
        self.title, self.value = title, value


class FakePrompts:
    """Stand-in for questionary: answers each prompt from a scripted list."""

    Choice = FakeChoice

    def __init__(self, answers):
        self.answers, self.asked = list(answers), []

    def _answer(self, message, choices=()):
        self.asked.append((message, list(choices)))
        answer = self.answers.pop(0)
        return types.SimpleNamespace(ask=lambda: answer)

    def select(self, message, choices=None, **kwargs):
        return self._answer(message, choices or [])

    def confirm(self, message, **kwargs):
        return self._answer(message)

    def path(self, message, **kwargs):
        return self._answer(message)

    def choices_for(self, needle):
        return next(c for m, c in self.asked if needle.lower() in m.lower())


def _row(name, rg="rg1", rtype="microsoft.web/sites", sub="SUB-1"):
    return {"id": f"/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/sites/{name}",
            "name": name, "type": rtype, "resourceGroup": rg}


SUBS = [{"name": "dev", "id": "SUB-1", "isDefault": False, "tenantId": "t-1"},
        {"name": "sandbox", "id": "SUB-2", "isDefault": True, "tenantId": "t-2"}]

# The prompt order the wizard asks in: subscription, resource group, resource,
# enrichment mode, AI confirm, output location.
def _answers(sub="SUB-1", rg="ALL", resource=None, enrich="auto", llm=False, out="."):
    return [{"id": sub, "tenantId": "t-1"}, rg, resource, enrich, llm, out]


@pytest.fixture
def wizard(monkeypatch):
    """Install the fake UI, capture every az call and the trace argv."""
    monkeypatch.setattr(interactive, "Console", FakeConsole, raising=False)
    monkeypatch.setattr(interactive, "Panel", types.SimpleNamespace(fit=lambda *a, **k: "panel"),
                        raising=False)
    monkeypatch.setattr(interactive, "Style", lambda *a, **k: None, raising=False)
    # setenv-then-delenv so the wizard's own os.environ write is rolled back too.
    monkeypatch.setenv("CLOUDMAP_ALLOW_SUBSCRIPTION", "")
    monkeypatch.delenv("CLOUDMAP_ALLOW_SUBSCRIPTION")

    state = types.SimpleNamespace(az_args=[], graph_calls=[], argv=None, prompts=None)

    def traced(argv):
        state.argv = argv
        return 0

    monkeypatch.setattr(cli, "main", traced)

    def install(answers, rows, subs=SUBS):
        state.prompts = FakePrompts(answers)
        monkeypatch.setattr(interactive, "questionary", state.prompts)

        def fake_az(args):
            state.az_args.append(list(args))
            return json.dumps(subs)

        def fake_graph_paged(kql, subs_arg):
            state.graph_calls.append((kql, list(subs_arg)))
            return list(rows), False

        monkeypatch.setattr(azure, "_az", fake_az)
        monkeypatch.setattr(azure, "_graph_paged", fake_graph_paged)
        return state

    state.install = install
    return state


# --- the az boundary -------------------------------------------------------------

def test_a_missing_azure_cli_is_reported_and_never_raised(wizard, monkeypatch):
    wizard.install([], [])

    def no_az(args):
        raise FileNotFoundError("az")

    monkeypatch.setattr(azure, "_az", no_az)

    assert interactive.interactive_main() == 1
    assert FakeConsole.last.said("failed")


def test_an_az_error_is_shown_with_its_stderr(wizard, monkeypatch):
    wizard.install([], [])

    def failing(args):
        raise RuntimeError("az account list failed: AADSTS50076: MFA required")

    monkeypatch.setattr(azure, "_az", failing)

    assert interactive.interactive_main() == 1
    assert FakeConsole.last.said("AADSTS50076")


def test_non_json_output_is_reported_rather_than_crashing(wizard, monkeypatch):
    wizard.install([], [])
    monkeypatch.setattr(azure, "_az", lambda args: "not json")

    assert interactive.interactive_main() == 1
    assert FakeConsole.last.said("failed")


def test_the_subscription_list_is_read_through_the_shared_az_helper(wizard):
    state = wizard.install(_answers(resource={"id": _row("a")["id"], "name": "a"}), [_row("a")])

    interactive.interactive_main()

    assert state.az_args[0][:2] == ["account", "list"]


# --- subscription step ----------------------------------------------------------

def test_the_default_subscription_is_labelled_and_offered_first(wizard):
    state = wizard.install(_answers(sub="SUB-2", resource={"id": _row("a")["id"], "name": "a"}),
                           [_row("a")])

    interactive.interactive_main()
    titles = [c.title for c in state.prompts.choices_for("subscription")]

    assert "(Default)" in titles[0] and "sandbox" in titles[0]
    assert "(Default)" not in titles[1]


def test_no_subscriptions_tells_the_user_to_log_in(wizard, monkeypatch):
    wizard.install([], [])
    monkeypatch.setattr(azure, "_az", lambda args: "[]")

    assert interactive.interactive_main() == 1
    assert FakeConsole.last.said("az login")


# --- workload query -------------------------------------------------------------

def test_the_workload_query_is_scoped_to_the_chosen_subscription(wizard):
    state = wizard.install(_answers(resource={"id": _row("a")["id"], "name": "a"}), [_row("a")])

    interactive.interactive_main()
    _, subs = state.graph_calls[0]

    assert subs == ["SUB-1"]


def test_every_supported_seed_workload_type_is_queried(wizard):
    state = wizard.install(_answers(resource={"id": _row("a")["id"], "name": "a"}), [_row("a")])

    interactive.interactive_main()
    kql, _ = state.graph_calls[0]

    for rtype in WORKLOAD_TYPES:
        assert f"'{rtype}'" in kql


def test_a_subscription_with_no_workloads_exits_cleanly(wizard):
    wizard.install([{"id": "SUB-1", "tenantId": "t-1"}], [])

    assert interactive.interactive_main() == 0
    assert FakeConsole.last.said("No workloads")


def test_a_failed_workload_query_exits_non_zero(wizard, monkeypatch):
    wizard.install([{"id": "SUB-1", "tenantId": "t-1"}], [])

    def failing(kql, subs):
        raise RuntimeError("AuthorizationFailed")

    monkeypatch.setattr(azure, "_graph_paged", failing)

    assert interactive.interactive_main() == 1
    assert FakeConsole.last.said("Failed to fetch resources")


# --- resource group + resource step ---------------------------------------------

def test_resource_groups_are_offered_once_each_with_an_all_option(wizard):
    rows = [_row("a", "rg-b"), _row("b", "rg-a"), _row("c", "rg-a")]
    state = wizard.install(_answers(resource={"id": rows[0]["id"], "name": "a"}), rows)

    interactive.interactive_main()
    values = [c.value for c in state.prompts.choices_for("resource group")]

    assert values == ["ALL", "rg-a", "rg-b"]        # deduplicated and sorted


def test_choosing_a_resource_group_narrows_the_workload_list(wizard):
    rows = [_row("a", "rg-a"), _row("b", "rg-b")]
    state = wizard.install(_answers(rg="rg-b", resource={"id": rows[1]["id"], "name": "b"}), rows)

    interactive.interactive_main()
    offered = [c.value["name"] for c in state.prompts.choices_for("resource to trace")]

    assert offered == ["b"]


def test_a_resource_choice_carries_both_its_id_and_its_name(wizard):
    rows = [_row("a")]
    state = wizard.install(_answers(resource={"id": rows[0]["id"], "name": "a"}), rows)

    interactive.interactive_main()
    choice = state.prompts.choices_for("resource to trace")[0]

    assert choice.value == {"id": rows[0]["id"], "name": "a"}


def test_the_trace_is_seeded_with_the_exact_arm_id_not_the_name(wizard):
    # Two workloads share the name; only the id can identify the one picked.
    rows = [_row("app", "rg-a"), _row("app", "rg-b")]
    state = wizard.install(_answers(resource={"id": rows[1]["id"], "name": "app"}), rows)

    assert interactive.interactive_main() == 0
    assert state.argv[:2] == ["trace", rows[1]["id"]]


def test_the_trace_runs_live_against_only_the_chosen_subscription(wizard):
    rows = [_row("app")]
    state = wizard.install(_answers(resource={"id": rows[0]["id"], "name": "app"}, enrich="all"),
                           rows)

    interactive.interactive_main()

    assert "--live" in state.argv and "--allow-live" in state.argv
    assert "--single-sub" in state.argv
    assert state.argv[state.argv.index("--enrich") + 1] == "all"


def test_the_chosen_subscription_is_pinned_for_the_trace(wizard):
    import os

    rows = [_row("app")]
    state = wizard.install(_answers(resource={"id": rows[0]["id"], "name": "app"}), rows)

    interactive.interactive_main()

    assert os.environ["CLOUDMAP_ALLOW_SUBSCRIPTION"] == "SUB-1"
    assert state.argv is not None


def test_the_output_directory_is_handed_to_trace(wizard):
    rows = [_row("orders-api")]
    state = wizard.install(_answers(resource={"id": rows[0]["id"], "name": "orders-api"}), rows)

    interactive.interactive_main()

    assert "--out-dir" in state.argv
    assert state.argv[state.argv.index("--out-dir") + 1] == "."


def test_a_custom_output_path_is_asked_for_and_used(wizard):
    rows = [_row("app")]
    answers = _answers(resource={"id": rows[0]["id"], "name": "app"}, out="CUSTOM")
    answers.append("/tmp/maps")          # the questionary.path() follow-up
    state = wizard.install(answers, rows)

    assert interactive.interactive_main() == 0
    assert state.argv[state.argv.index("--out-dir") + 1] == "/tmp/maps"


def test_declining_the_ai_pass_leaves_the_llm_flag_off(wizard):
    rows = [_row("app")]
    state = wizard.install(_answers(resource={"id": rows[0]["id"], "name": "app"}, llm=False), rows)

    interactive.interactive_main()

    assert "--llm" not in state.argv


def test_accepting_the_ai_pass_adds_the_llm_flag(wizard):
    rows = [_row("app")]
    state = wizard.install(_answers(resource={"id": rows[0]["id"], "name": "app"}, llm=True), rows)

    interactive.interactive_main()

    assert "--llm" in state.argv


@pytest.mark.parametrize("answers", [
    pytest.param([None], id="subscription"),
    pytest.param([{"id": "SUB-1", "tenantId": "t-1"}, None], id="resource-group"),
    pytest.param([{"id": "SUB-1", "tenantId": "t-1"}, "ALL", None], id="resource"),
    pytest.param([{"id": "SUB-1", "tenantId": "t-1"}, "ALL", {"id": "/x", "name": "x"}, None],
                 id="enrichment-mode"),
    pytest.param([{"id": "SUB-1", "tenantId": "t-1"}, "ALL", {"id": "/x", "name": "x"}, "auto",
                  None], id="ai-confirm"),
    pytest.param([{"id": "SUB-1", "tenantId": "t-1"}, "ALL", {"id": "/x", "name": "x"}, "auto",
                  False, None], id="output-location"),
    pytest.param([{"id": "SUB-1", "tenantId": "t-1"}, "ALL", {"id": "/x", "name": "x"}, "auto",
                  False, "CUSTOM", None], id="custom-path"),
])
def test_cancelling_any_prompt_aborts_without_tracing(wizard, answers):
    state = wizard.install(answers, [_row("a")])

    assert interactive.interactive_main() == 0
    assert state.argv is None


def test_a_workload_row_without_a_resource_group_does_not_crash_the_wizard(wizard):
    rows = [_row("a", "rg-a"), dict(_row("b", "rg-b"), resourceGroup=None)]
    state = wizard.install(_answers(resource={"id": rows[0]["id"], "name": "a"}), rows)

    assert interactive.interactive_main() == 0
    assert state.argv[:2] == ["trace", rows[0]["id"]]
