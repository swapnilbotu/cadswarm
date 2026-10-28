"""The MCP tools log activity (which moves the agent's pointer) and stop acting once superseded."""
import json
import threading
import types

import pytest

from cadswarm import mcp_server
from cadswarm.client import HubRequestError


class RecordingHub:
    def __init__(self):
        self.activity, self.posts = [], []
        self.fail = None

    def log_activity(self, kind, summary, targets=None):
        self.activity.append((kind, summary, targets))

    def post(self, path, body=None):
        self.posts.append((path, body))
        if self.fail:
            raise self.fail
        return {"ok": True}

    def get(self, path, **params):
        return {"path": path, **params}


def fake_session(joined=True):
    ready = threading.Event()
    if joined:
        ready.set()
    return types.SimpleNamespace(client=RecordingHub(), dead=None, doing="", cfg={"name": "a"}, ready=ready,
                                 join_error=None)


@pytest.fixture
def session(monkeypatch):
    s = fake_session()
    monkeypatch.setattr(mcp_server, "S", s)
    return s


def test_a_tool_called_during_the_join_waits_for_it(monkeypatch):
    """The handshake no longer waits for the join, so a model can call a tool while it is still going."""
    s = fake_session(joined=False)
    monkeypatch.setattr(mcp_server, "S", s)
    threading.Timer(0.2, s.ready.set).start()
    assert mcp_server.lock_objects(["Lid"]) == {"ok": True}
    assert s.client.posts == [("/locks", {"names": ["Lid"]})]


def test_a_join_that_never_finishes_or_failed_says_so(monkeypatch):
    s = fake_session(joined=False)
    monkeypatch.setattr(mcp_server, "S", s)
    monkeypatch.setattr(mcp_server, "JOIN_WAIT_SECONDS", 0.05)
    assert mcp_server.lock_objects(["Lid"])["code"] == "joining"
    s.join_error = "Couldn't join the team at u / db: removed: a person removed this helper from the team"
    s.ready.set()
    r = mcp_server.lock_objects(["Lid"])
    assert r["code"] == "not_joined" and "removed" in r["error"]
    assert s.client.posts == [], "nothing reaches the team before the join"


def test_the_join_retries_an_unreachable_database_then_runs_heartbeats(monkeypatch):
    s = fake_session(joined=False)
    s.cfg = {"name": "a", "uri": "u", "db": "d", "ephemeral": True}
    tries = []

    def connect():
        tries.append(1)
        if len(tries) < 3:
            raise HubRequestError(0, {"error": "connection refused", "code": "unreachable"})
        return {"name": "a", "participant_id": 1, "fence": 2}

    s.connect, s.heartbeat_loop = connect, lambda: None
    monkeypatch.setattr(mcp_server, "S", s)
    monkeypatch.setattr(mcp_server.time, "sleep", lambda _: None)
    mcp_server.join_team()
    assert len(tries) == 3 and s.ready.is_set() and s.join_error is None


def test_a_refused_join_is_not_retried(monkeypatch):
    s = fake_session(joined=False)
    s.cfg = {"name": "a", "uri": "u", "db": "d", "ephemeral": True}
    tries = []

    def connect():
        tries.append(1)
        raise HubRequestError(403, {"error": "removed: a person removed this helper", "code": "removed"})

    s.connect = connect
    monkeypatch.setattr(mcp_server, "S", s)
    mcp_server.join_team()
    assert len(tries) == 1 and s.ready.is_set() and "removed" in s.join_error


def test_ops_log_activity_with_their_target_objects(session):
    mcp_server.boolean("cut", "Lid", "Plate", "Hole")
    assert session.client.activity == [("tool_call", "cut Plate/Hole -> Lid", ["Lid", "Plate", "Hole"])]
    path, body = session.client.posts[-1]
    assert path == "/ops" and body["op"] == "boolean" and body["args"]["base"] == "Plate"


def test_new_tools_reach_their_routes(session):
    mcp_server.create_task("lid", brief="b_1")
    assert session.client.posts[-1] == ("/tasks", {"title": "lid", "detail": "", "objects": None,
                                                    "assignee": None, "brief": "b_1"})
    mcp_server.check_printability(["Lid"])
    assert session.client.posts[-1] == ("/printability", {"names": ["Lid"], "min_wall": None})
    assert session.client.activity[-1][2] == ["Lid"]
    mcp_server.set_brief_plan("b_1", {"parts": []})
    assert session.client.posts[-1][0] == "/briefs/b_1/plan"
    mcp_server.accept_handoff("h_1")
    assert session.client.posts[-1][0] == "/handoffs/h_1/accept"
    mcp_server.report_review("b_1", 1, True, [])
    assert session.client.posts[-1] == ("/reviews", {"brief_id": "b_1", "round": 1, "passed": True, "findings": []})
    assert mcp_server.list_files("parts") == {"path": "/project/files", "prefix": "parts"}


def test_a_superseded_session_refuses_every_tool(session):
    session.client.fail = HubRequestError(409, {"error": "superseded", "code": "fence_stale"})
    r = mcp_server.lock_objects(["Lid"])
    assert r["code"] == "fence_stale"
    assert mcp_server.create_primitive("box", "Box")["code"] == "inactive"


def test_a_warm_server_shows_idle_once_its_runner_waits(tmp_path):
    """The MCP server outlives the run: without this, the team list says "working on Lid" for minutes."""
    import time

    (tmp_path / ".cadswarm").mkdir()
    runner = tmp_path / ".cadswarm" / "runner.json"
    s = types.SimpleNamespace(doing='working on "Lid"', project=str(tmp_path))
    runner.write_text(json.dumps({"state": "listening", "beat": time.time()}))
    assert mcp_server.Session.doing_now(s) == "listening for instructions"
    runner.write_text(json.dumps({"state": "working", "beat": time.time()}))
    assert mcp_server.Session.doing_now(s) == 'working on "Lid"'
    # A stale beat means no runner is looking after the folder: keep what the run was doing.
    runner.write_text(json.dumps({"state": "listening", "beat": 0}))
    assert mcp_server.Session.doing_now(s) == 'working on "Lid"'
    # A run that never claimed a task leaves "starting"; the waiting runner wins over it too.
    s.doing = "starting"
    runner.write_text(json.dumps({"state": "listening", "beat": time.time()}))
    assert mcp_server.Session.doing_now(s) == "listening for instructions"
    # A host-spawned agent has no runner at all: it just says what it is doing.
    (tmp_path / ".cadswarm" / "runner.json").unlink()
    assert mcp_server.Session.doing_now(s) == "starting"
