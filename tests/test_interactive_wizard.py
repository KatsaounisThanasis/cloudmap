"""The zero-argument wizard: what it asks, and what it hands to `trace`.

The wizard is the path most people take, and it is the only place that builds an
`az` command line by string concatenation and turns a picked row into CLI
arguments. It is also optional-dependency territory (questionary + rich), so the
prompts are faked here: no terminal, no Azure CLI, no subprocess. Everything is
asserted through `run_az` (the single command boundary) and the argv the wizard
finally passes to `cloudmap.cli.main`.
"""

import contextlib
import json
import subprocess
import types

import pytest

from cloudmap import cli, interactive

WORKLOAD_TYPES = (
    "microsoft.web/sites",
    "microsoft.containerservice/managedclusters",
    "microsoft.app/containerapps",
    "microsoft.compute/virtualmachines",
    "microsoft.apimanagement/service",
    "microsoft.sql/servers/databases",
    "microsoft.dbforpostgresql/flexibleservers",
)


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
    """Stand-in for questionary: answers each select() from a scripted list."""

    Choice = FakeChoice

    def __init__(self, answers):
        self.answers, self.asked = list(answers), []

    def select(self, message, choices=None, **kwargs):
        self.asked.append((message, list(choices or [])))
        answer = self.answers.pop(0)
        return types.SimpleNamespace(ask=lambda: answer)

    def confirm(self, message, **kwargs):
        self.asked.append((message, []))
        answer = self.answers.pop(0)
        return types.SimpleNamespace(ask=lambda: answer)

    def choices_for(self, needle):
        return next(c for m, c in self.asked if needle.lower() in m.lower())


def _row(name, rg="rg1", rtype="microsoft.web/sites", sub="SUB-1"):
    return {"id": f"/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/sites/{name}",
            "name": name, "type": rtype, "resourceGroup": rg}


SUBS = [{"name": "dev", "id": "SUB-1", "isDefault": False},
        {"name": "sandbox", "id": "SUB-2", "isDefault": True}]


@pytest.fixture
def wizard(monkeypatch):
    """Install the fake UI, capture every az command and the trace argv."""
    monkeypatch.setattr(interactive, "Console", FakeConsole, raising=False)
    monkeypatch.setattr(interactive, "Panel", types.SimpleNamespace(fit=lambda *a, **k: "panel"),
                        raising=False)
    monkeypatch.setattr(interactive, "Style", lambda *a, **k: None, raising=False)
    # setenv-then-delenv so the wizard's own os.environ write is rolled back too.
    monkeypatch.setenv("CLOUDMAP_ALLOW_SUBSCRIPTION", "")
    monkeypatch.delenv("CLOUDMAP_ALLOW_SUBSCRIPTION")

    state = types.SimpleNamespace(commands=[], argv=None, prompts=None)

    def traced(argv):
        state.argv = argv
        return 0

    monkeypatch.setattr(cli, "main", traced)

    def install(answers, rows, subs=SUBS):
        state.prompts = FakePrompts(answers)
        monkeypatch.setattr(interactive, "questionary", state.prompts)

        def run_az(cmd, console=None, loading_msg="Loading..."):
            state.commands.append(cmd)
            return subs if "account list" in cmd else {"data": rows}

        monkeypatch.setattr(interactive, "run_az", run_az)
        return state

    state.install = install
    return state


# --- the single command boundary -------------------------------------------------

def test_a_missing_azure_cli_is_reported_and_never_raised(monkeypatch):
    def no_az(*a, **k):
        raise FileNotFoundError("az")

    monkeypatch.setattr(subprocess, "run", no_az)
    console = FakeConsole()

    assert interactive.run_az("az account list", console, None) is None
    assert console.said("not installed")


def test_an_az_error_is_shown_with_its_stderr(monkeypatch):
    def failing(*a, **k):
        raise subprocess.CalledProcessError(1, "az", stderr="AADSTS50076: MFA required")

    monkeypatch.setattr(subprocess, "run", failing)
    console = FakeConsole()

    assert interactive.run_az("az account list", console, None) is None
    assert console.said("AADSTS50076")


def test_non_json_output_is_treated_as_no_result(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout="not json", stderr=""))

    assert interactive.run_az("az graph query -q x", FakeConsole(), None) is None


def test_empty_output_is_treated_as_no_result(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout="   ", stderr=""))

    assert interactive.run_az("az graph query -q x", None, None) is None


def test_json_output_is_returned_parsed(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout=json.dumps({"data": [1]}),
                                                              stderr=""))

    assert interactive.run_az("az graph query -q x", None, None) == {"data": [1]}


# --- subscription step ----------------------------------------------------------

def test_the_default_subscription_is_labelled_and_offered_first(wizard):
    state = wizard.install(["SUB-2", "ALL", {"id": _row("a")["id"], "name": "a"}, "auto", False, "."],
                           [_row("a")])

    interactive.interactive_main()
    titles = [c.title for c in state.prompts.choices_for("subscription")]

    assert "(Default)" in titles[0] and "sandbox" in titles[0]
    assert "(Default)" not in titles[1]


def test_no_subscriptions_tells_the_user_to_log_in(wizard, monkeypatch):
    wizard.install([], [])
    monkeypatch.setattr(interactive, "run_az",
                        lambda cmd, console=None, loading_msg="Loading...": None)

    assert interactive.interactive_main() == 1
    assert FakeConsole.last.said("az login")


# --- workload query -------------------------------------------------------------

def test_the_workload_query_asks_for_a_thousand_rows_in_one_subscription(wizard):
    state = wizard.install(["SUB-1", "ALL", {"id": _row("a")["id"], "name": "a"}, "auto", False, "."],
                           [_row("a")])

    interactive.interactive_main()
    graph_cmd = next(c for c in state.commands if "graph query" in c)

    assert "--first 1000" in graph_cmd
    assert "--subscriptions SUB-1" in graph_cmd
    # `--subscriptions` already scopes the query; the duplicate singular flag that
    # az rejects in this position must not come back.
    assert "--subscription SUB-1" not in graph_cmd.replace("--subscriptions SUB-1", "")


def test_every_supported_seed_workload_type_is_queried(wizard):
    state = wizard.install(["SUB-1", "ALL", {"id": _row("a")["id"], "name": "a"}, "auto", False, "."],
                           [_row("a")])

    interactive.interactive_main()
    graph_cmd = next(c for c in state.commands if "graph query" in c)

    for rtype in WORKLOAD_TYPES:
        assert f"'{rtype}'" in graph_cmd


def test_pagination_fetches_multiple_pages_if_skip_token_is_present(wizard, monkeypatch):
    rows = [_row("app1"), _row("app2")]
    wizard.install(["SUB-1", "ALL", {"id": rows[0]["id"], "name": rows[0]["name"]}, "auto", False, "."], [])
    
    call_count = 0
    def paged_run_az(cmd, console=None, loading_msg="Loading..."):
        nonlocal call_count
        if "account list" in cmd:
            return SUBS
        
        # Mock ARG query responses
        call_count += 1
        if call_count == 1:
            return {"data": [rows[0]], "skipToken": "TOKEN1"}
        elif call_count == 2:
            return {"data": [rows[1]]} # No token, loop should stop
            
    monkeypatch.setattr(interactive, "run_az", paged_run_az)

    interactive.interactive_main()

    # The wizard should have gathered both rows and presented them
    offered = [c.value["name"] for c in wizard.prompts.choices_for("resource to trace")]
    assert offered == ["app1", "app2"]
    assert call_count == 2


def test_a_subscription_with_no_workloads_exits_cleanly(wizard):
    wizard.install(["SUB-1"], [])

    assert interactive.interactive_main() == 0
    assert FakeConsole.last.said("No workloads")


def test_a_failed_workload_query_exits_non_zero(wizard, monkeypatch):
    wizard.install(["SUB-1"], [])
    monkeypatch.setattr(interactive, "run_az",
                        lambda cmd, console=None, loading_msg="Loading...":
                        SUBS if "account list" in cmd else None)

    assert interactive.interactive_main() == 1
    assert FakeConsole.last.said("Failed to fetch resources")


# --- resource group + resource step ---------------------------------------------

def test_resource_groups_are_offered_once_each_with_an_all_option(wizard):
    rows = [_row("a", "rg-b"), _row("b", "rg-a"), _row("c", "rg-a")]
    state = wizard.install(["SUB-1", "ALL", {"id": rows[0]["id"], "name": "a"}, "auto", False, "."], rows)

    interactive.interactive_main()
    values = [c.value for c in state.prompts.choices_for("resource group")]

    assert values == ["ALL", "rg-a", "rg-b"]        # deduplicated and sorted


def test_choosing_a_resource_group_narrows_the_workload_list(wizard):
    rows = [_row("a", "rg-a"), _row("b", "rg-b")]
    state = wizard.install(["SUB-1", "rg-b", {"id": rows[1]["id"], "name": "b"}, "auto", False, "."], rows)

    interactive.interactive_main()
    offered = [c.value["name"] for c in state.prompts.choices_for("resource to trace")]

    assert offered == ["b"]


def test_a_resource_choice_carries_both_its_id_and_its_name(wizard):
    rows = [_row("a")]
    state = wizard.install(["SUB-1", "ALL", {"id": rows[0]["id"], "name": "a"}, "auto", False, "."], rows)

    interactive.interactive_main()
    choice = state.prompts.choices_for("resource to trace")[0]

    assert choice.value == {"id": rows[0]["id"], "name": "a"}


def test_the_trace_is_seeded_with_the_exact_arm_id_not_the_name(wizard):
    # Two workloads share the name; only the id can identify the one picked.
    rows = [_row("app", "rg-a"), _row("app", "rg-b")]
    state = wizard.install(["SUB-1", "ALL", {"id": rows[1]["id"], "name": "app"}, "auto", False, "."], rows)

    assert interactive.interactive_main() == 0
    assert state.argv[:2] == ["trace", rows[1]["id"]]


def test_the_trace_runs_live_against_only_the_chosen_subscription(wizard, monkeypatch):
    rows = [_row("app")]
    state = wizard.install(["SUB-1", "ALL", {"id": rows[0]["id"], "name": "app"}, "all", False, "."], rows)

    interactive.interactive_main()

    assert "--live" in state.argv and "--allow-live" in state.argv
    assert "--single-sub" in state.argv
    assert state.argv[state.argv.index("--enrich") + 1] == "all"


def test_the_chosen_subscription_is_pinned_for_the_trace(wizard, monkeypatch):
    import os

    rows = [_row("app")]
    state = wizard.install(["SUB-1", "ALL", {"id": rows[0]["id"], "name": "app"}, "auto", False, "."], rows)

    interactive.interactive_main()

    assert os.environ["CLOUDMAP_ALLOW_SUBSCRIPTION"] == "SUB-1"
    assert state.argv is not None


def test_all_three_artifacts_are_named_after_the_resource(wizard):
    rows = [_row("orders-api")]
    state = wizard.install(["SUB-1", "ALL", {"id": rows[0]["id"], "name": "orders-api"}, "auto", False, "."],
                           rows)

    interactive.interactive_main()

    assert "--out-dir" in state.argv
    assert state.argv[state.argv.index("--out-dir") + 1] == "."


@pytest.mark.parametrize("answers", [
    [None],                                                     # subscription
    ["SUB-1", None],                                            # resource group
    ["SUB-1", "ALL", None],                                     # resource
    ["SUB-1", "ALL", {"id": "/x", "name": "x"}, None],          # enrichment mode
])
def test_cancelling_any_prompt_aborts_without_tracing(wizard, answers):
    state = wizard.install(answers, [_row("a")])

    assert interactive.interactive_main() == 0
    assert state.argv is None


def test_a_workload_row_without_a_resource_group_does_not_crash_the_wizard(wizard):
    rows = [_row("a", "rg-a"), dict(_row("b", "rg-b"), resourceGroup=None)]
    state = wizard.install(["SUB-1", "ALL", {"id": rows[0]["id"], "name": "a"}, "auto", False, "."], rows)

    assert interactive.interactive_main() == 0
    assert state.argv[:2] == ["trace", rows[0]["id"]]
