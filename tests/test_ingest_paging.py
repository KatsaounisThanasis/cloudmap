"""Azure Resource Graph paging and command construction, at the `az` boundary.

Nothing here shells out: either `azure._az` or `subprocess.run` is replaced, so
these tests never need the Azure CLI, a login, or a subscription. What they lock
is the part of ingest that decides HOW MUCH of a tenant we actually read - page
size, skip-token following, the page cap - plus the honesty flag that comes with
it: `truncated` is the only thing standing between a partial scan and a map that
silently claims to be complete.
"""

import json

import pytest

from cloudmap.graph import build_graph
from cloudmap.ingest import azure


@pytest.fixture(autouse=True)
def _no_pin_and_no_real_az(monkeypatch):
    """Hermetic by construction: no inherited subscription pin, and any attempt to
    actually run a process fails loudly instead of touching a real tenant."""
    monkeypatch.delenv("CLOUDMAP_ALLOW_SUBSCRIPTION", raising=False)

    def _forbidden(*a, **k):
        raise AssertionError(f"a test tried to run a real process: {a!r}")

    monkeypatch.setattr(azure.subprocess, "run", _forbidden)


class _FakeProc:
    def __init__(self, stdout="{}", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def _record_commands(monkeypatch, stdout="{}"):
    """Replace the subprocess boundary and collect the argv of every `az` call."""
    seen = []

    def run(cmd, capture_output=True, text=True):
        seen.append(list(cmd))
        return _FakeProc(stdout)

    monkeypatch.setattr(azure.subprocess, "run", run)
    return seen


def _serve_pages(monkeypatch, pages):
    """Replace `_az` with a stub that returns the given graph pages in order.
    Collects the argv of each call so the query construction can be asserted."""
    seen, remaining = [], list(pages)

    def _az(args):
        seen.append(list(args))
        return json.dumps(remaining.pop(0) if len(remaining) > 1 else remaining[0])

    monkeypatch.setattr(azure, "_az", _az)
    return seen


def test_every_arg_page_is_requested_at_the_1000_row_limit(monkeypatch):
    seen = _serve_pages(monkeypatch, [{"data": [{"id": "/a"}]}])

    azure._graph_paged("resources", ["S1"])

    assert seen[0][:2] == ["graph", "query"]
    assert "--first" in seen[0] and seen[0][seen[0].index("--first") + 1] == "1000"


def test_pagination_follows_the_skip_token_and_concatenates_the_pages(monkeypatch):
    seen = _serve_pages(monkeypatch, [
        {"data": [{"id": "/a"}], "skip_token": "TOKEN-1"},
        {"data": [{"id": "/b"}]},
    ])

    rows, truncated = azure._graph_paged("resources", ["S1"])

    assert [r["id"] for r in rows] == ["/a", "/b"]
    assert truncated is False
    assert "--skip-token" not in seen[0]                     # first page asks for no token
    assert seen[1][seen[1].index("--skip-token") + 1] == "TOKEN-1"


def test_pagination_accepts_the_camel_case_skip_token_spelling(monkeypatch):
    # az has emitted both `skip_token` and `skipToken`; either must page.
    seen = _serve_pages(monkeypatch, [
        {"data": [{"id": "/a"}], "skipToken": "TOKEN-2"},
        {"data": [{"id": "/b"}]},
    ])

    rows, truncated = azure._graph_paged("resources", ["S1"])

    assert len(rows) == 2 and truncated is False
    assert "TOKEN-2" in seen[1]


def test_hitting_the_page_cap_reports_the_scan_as_truncated(monkeypatch):
    # A page that still hands back a token when the cap runs out means rows were
    # left unread - the caller has to be told, or the map lies about being whole.
    _serve_pages(monkeypatch, [{"data": [{"id": "/a"}], "skip_token": "MORE"}])

    rows, truncated = azure._graph_paged("resources", ["S1"])

    assert len(rows) == azure._PAGE_CAP
    assert truncated is True


def test_an_empty_result_set_is_an_empty_scan_not_a_truncated_one(monkeypatch):
    _serve_pages(monkeypatch, [{"data": [], "skip_token": None, "count": 0}])

    assert azure._graph_paged("resources", ["S1"]) == ([], False)


def test_a_response_without_a_data_key_yields_no_rows(monkeypatch):
    # az has returned bare `{"count": 0}` shapes; that is zero rows, not a crash.
    _serve_pages(monkeypatch, [{"count": 0, "totalRecords": 0}])

    assert azure._graph_paged("resources", ["S1"]) == ([], False)


def test_a_scan_with_no_subscriptions_omits_the_subscriptions_flag(monkeypatch):
    seen = _serve_pages(monkeypatch, [{"data": []}])

    azure._graph_paged("resources", [])

    assert "--subscriptions" not in seen[0]


def test_malformed_az_output_is_raised_rather_than_read_as_an_empty_tenant(monkeypatch):
    # `az` chatter leaking into stdout must never look like "this tenant is empty":
    # an empty graph is indistinguishable from a resource-free subscription.
    monkeypatch.setattr(azure, "_az", lambda args: "WARNING: az upgrade available\nnot json")

    with pytest.raises(json.JSONDecodeError):
        azure._graph_paged("resources", ["S1"])


def test_a_failing_az_call_raises_with_its_stderr_attached(monkeypatch):
    monkeypatch.setattr(azure.subprocess, "run",
                        lambda *a, **k: _FakeProc("", "AADSTS700082: token expired", 1))

    with pytest.raises(RuntimeError) as err:
        azure._az(["account", "show", "-o", "json"])

    assert "AADSTS700082" in str(err.value)
    assert "account show" in str(err.value)


def test_the_subscription_pin_is_forced_onto_every_command_except_account_list(monkeypatch):
    # The pin is what makes a cross-tenant subscription readable: `az account show`
    # must be evaluated IN the pinned context, while `az account list` rejects the
    # flag outright, so it is the one command that must not receive it.
    monkeypatch.setenv("CLOUDMAP_ALLOW_SUBSCRIPTION", "PINNED-SUB")
    seen = _record_commands(monkeypatch)

    azure._az(["account", "list", "-o", "json"])
    azure._az(["account", "show", "-o", "json"])
    azure._az(["graph", "query", "-q", "resources", "--subscriptions", "PINNED-SUB"])

    assert "--subscription" not in seen[0]
    assert seen[1][-2:] == ["--subscription", "PINNED-SUB"]
    assert seen[2][-2:] == ["--subscription", "PINNED-SUB"]


def test_an_unpinned_run_adds_no_subscription_flag(monkeypatch):
    seen = _record_commands(monkeypatch)

    azure._az(["account", "show", "-o", "json"])

    assert "--subscription" not in seen[0]


def _serve_live(monkeypatch, account, resource_pages, role_pages, subs=None):
    """Stub `_az` for a whole query_live() run: account guard, subscription list,
    then the resource and role-assignment graph queries."""
    subs = subs if subs is not None else [{"id": "S1", "state": "Enabled", "name": "dev"}]
    resource_pages, role_pages = list(resource_pages), list(role_pages)
    seen = []

    def _az(args):
        seen.append(list(args))
        if args[:2] == ["account", "show"]:
            return json.dumps(account)
        if args[:2] == ["account", "list"]:
            return json.dumps(subs)
        if args[0] == "graph":
            pages = role_pages if "authorizationresources" in args[3] else resource_pages
            return json.dumps(pages.pop(0) if len(pages) > 1 else pages[0])
        raise AssertionError(f"unexpected az call: {args!r}")

    monkeypatch.setattr(azure, "_az", _az)
    return seen


def test_a_live_query_without_allow_live_refuses_to_read_anything(monkeypatch):
    _serve_live(monkeypatch, {"id": "S1", "tenantId": "T"}, [{"data": []}], [{"data": []}])

    with pytest.raises(SystemExit):
        azure.query_live(allow_live=False)


def test_a_pin_that_is_not_the_active_subscription_aborts_the_scan(monkeypatch):
    monkeypatch.setenv("CLOUDMAP_ALLOW_SUBSCRIPTION", "SOMEONE-ELSE")
    _serve_live(monkeypatch, {"id": "S1", "name": "dev", "tenantId": "T"},
                [{"data": []}], [{"data": []}])

    with pytest.raises(SystemExit) as err:
        azure.query_live(allow_live=True)

    assert "SOMEONE-ELSE" in str(err.value)


def test_a_tenant_wide_scan_queries_only_enabled_subscriptions(monkeypatch):
    seen = _serve_live(
        monkeypatch, {"id": "S1", "tenantId": "T"}, [{"data": []}], [{"data": []}],
        subs=[{"id": "S1", "state": "Enabled"}, {"id": "S2", "state": "Enabled"},
              {"id": "S3", "state": "Disabled"}],
    )

    azure.query_live(allow_live=True)
    graph_call = next(a for a in seen if a[0] == "graph")
    subs = graph_call[graph_call.index("--subscriptions") + 1:]

    assert subs == ["S1", "S2"]                     # the disabled one is never read


def test_a_single_sub_scan_never_lists_the_tenant(monkeypatch):
    seen = _serve_live(monkeypatch, {"id": "S1", "tenantId": "T"},
                       [{"data": [{"id": "/a"}]}], [{"data": []}])

    rows, truncated = azure.query_live(allow_live=True, tenant_wide=False)

    assert (len(rows), truncated) == (1, False)
    assert not any(a[:2] == ["account", "list"] for a in seen)


def test_a_truncated_resource_scan_propagates_out_of_query_live(monkeypatch):
    _serve_live(monkeypatch, {"id": "S1", "tenantId": "T"},
                [{"data": [{"id": "/a"}], "skip_token": "MORE"}], [{"data": []}])

    _rows, truncated = azure.query_live(allow_live=True, tenant_wide=False)

    assert truncated is True


def test_role_assignments_are_read_from_their_own_table(monkeypatch):
    seen = _serve_live(monkeypatch, {"id": "S1", "tenantId": "T"},
                       [{"data": [{"id": "/a"}]}],
                       [{"data": [{"id": "/ra", "type": "microsoft.authorization/roleassignments"}]}])

    rows, _truncated = azure.query_live(allow_live=True, tenant_wide=False)
    queries = [a[3] for a in seen if a[0] == "graph"]

    assert len(rows) == 2                                   # resources + role assignments
    assert any("authorizationresources" in q for q in queries)


def test_the_same_resource_returned_by_two_pages_becomes_one_node(monkeypatch):
    # Resource Graph paging is not a snapshot: a row can legitimately come back on
    # two pages. The duplicate must collapse instead of doubling nodes or edges.
    row = {"id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Web/sites/web",
           "name": "web", "type": "microsoft.web/sites",
           "properties": {"serverFarmId":
                          "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Web/serverfarms/plan"}}
    plan = {"id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Web/serverfarms/plan",
            "name": "plan", "type": "microsoft.web/serverfarms", "properties": {}}
    _serve_pages(monkeypatch, [{"data": [row, plan], "skip_token": "MORE"}, {"data": [row]}])

    rows, _truncated = azure._graph_paged("resources", ["S1"])
    graph = build_graph(rows)

    assert len(rows) == 3                                   # the raw scan really did repeat it
    assert len(graph.nodes) == 2                            # ... and the graph collapses it
    assert [e.kind for e in graph.edges] == ["hosted-on"]   # one edge, not two
