"""SpaceHub's library and settings routes: browsing runs locally over v_component, writes are ops or
reducers, and only the reducers (not the client) decide who may change settings."""
import json
import threading
import types

import pytest

from cadswarm import mcp_server
from cadswarm.client import HubRequestError
from cadswarm.space_hub import SpaceHub

from test_space_hub import FakeSpace, member


def comp(id, name, **kw):
    row = {"id": id, "ws": 1, "name": name, "category": "other", "tags": [], "description": "", "source": "upload",
           "source_url": "", "license": "", "author": "carol", "format": "step", "hash": "h", "size": 10, "version": 1,
           "bbox": [0, 0, 0, 40, 20, 5], "volume": 1.0, "features_json": "{}", "std_json": "", "suggest_json": "[]",
           "uses": 0, "created_ms": 1, "updated_ms": 1}
    row.update(kw)
    return row


@pytest.fixture
def hub():
    tables = {
        "v_me": [member(1, "alice-claude", fence=2)],
        "v_member": [member(1, "alice-claude", fence=2), member(3, "carol", kind="human")],
        "v_object": [{"name": "NEMA17Mount1", "label": "x"}],
        "v_component": [comp("c_nema17_mount_aa11bb", "NEMA17 mount", category="motion", tags=["motor", "nema17"],
                             uses=3, features_json=json.dumps({"holes": [{"d": 3.4, "count": 4}]}))],
        "v_component_preview": [{"id": "c_nema17_mount_aa11bb", "ws": 1, "mesh": "736f6c6964"}],
        "v_setting": [{"key": "1:tol_clearance", "ws": 1, "setting": "tol_clearance", "num": 0.3, "text": "",
                       "set_by": 3, "updated_ms": 5}],
        "v_suggestion": [
            {"id": "g_1", "ws": 1, "setting": "tol_press_fit", "num": 0.0, "text": "", "reason": "printed seats",
             "component": "c_nema17_mount_aa11bb", "by": 1, "status": "pending", "created_ms": 2,
             "resolved_by": 0, "resolved_ms": 0},
            {"id": "g_0", "ws": 1, "setting": "min_wall", "num": 2.0, "text": "", "reason": "old", "component": "",
             "by": 1, "status": "dismissed", "created_ms": 1, "resolved_by": 3, "resolved_ms": 2}],
    }
    fake = FakeSpace(tables)
    h = SpaceHub({"uri": "http://x", "db": "d", "name": "alice-claude"},
                 {"participant_id": 1, "token": "t", "fence": 2}, space=fake)
    h.fake = fake
    return h


def finish_ops(fake, result):
    """Make every queued op finish with `result` (the host's job in real life)."""
    real_sql = fake.sql

    def sql(query):
        for o in fake.tables.get("v_op", []):
            o.update(status="done", result_json=json.dumps(result))
        return real_sql(query)
    fake.sql = sql


def last_op(fake):
    reducer, args = [c for c in fake.calls if c[0] == "submit_op"][-1]
    return args[2], json.loads(args[3])


def test_the_library_lists_team_parts_first_then_standard_parts(hub):
    lib = hub.get("/library")
    names = [c["name"] for c in lib["components"]]
    assert names[0] == "NEMA17 mount"
    assert "Deep groove ball bearing" in names and "motion" in lib["categories"]
    assert hub.get("/library", source="builtin")["components"][0]["source"] == "builtin"


def test_search_and_recommend_run_locally_with_the_team_settings(hub):
    r = hub.post("/library/search", {"query": "motor"})
    assert r["results"][0]["id"] == "c_nema17_mount_aa11bb" and r["results"][0]["size_mm"] == [40, 20, 5]
    rec = hub.post("/library/recommend", {"need": "fasten the lid with M3", "constraints": {"length": 8}})
    first = rec["recommendations"][0]
    assert first["size"] == "M3" and first["length"] == 8 and first["hole"]["diameter"] == 3.4
    assert not [c for c in hub.fake.calls if c[0] == "submit_op"], "browsing never queues host ops"


def test_a_team_part_shows_the_settings_it_implies_now(hub):
    got = hub.get("/library/c_nema17_mount_aa11bb")
    assert got["features"] == {"holes": [{"d": 3.4, "count": 4}]} and got["uses"] == 3
    std = hub.get("/library/c_std_ball_bearing")
    assert std["holes_for_team_settings"]["608"]["diameter"] == 21.95
    with pytest.raises(HubRequestError) as e:
        hub.get("/library/c_nothing")
    assert e.value.code == "not_found"


def test_previews_come_back_as_bytes(hub):
    assert hub.get("/library/c_nema17_mount_aa11bb/preview") == b"solid"


def test_placing_a_standard_part_queues_insert_standard_part_with_a_free_name(hub):
    finish_ops(hub.fake, {"result": {"name": "M3x12SocketHead1"}})
    r = hub.post("/library/insert", {"family": "iso4762", "size": "m3", "length": 12, "placement": {"base": [1, 2, 3]}})
    kind, args = last_op(hub.fake)
    assert kind == "insert_standard_part"
    assert args["size"] == "M3" and args["length"] == 12 and args["name"] == "M3x12SocketHead1"
    assert args["placement"] == {"base": [1, 2, 3]} and r["hole"]["diameter"] == 3.4


def test_placing_a_team_part_picks_a_name_not_already_in_the_model(hub):
    finish_ops(hub.fake, {"result": {}})
    hub.post("/library/insert", {"id": "c_nema17_mount_aa11bb"})
    kind, args = last_op(hub.fake)
    assert kind == "insert_component" and args["component"] == "c_nema17_mount_aa11bb"
    assert args["name"] == "NEMA17Mount2"  # NEMA17Mount1 is taken


def test_bad_standard_sizes_are_refused_before_anything_is_queued(hub):
    with pytest.raises(HubRequestError) as e:
        hub.post("/library/insert", {"family": "iso4762", "size": "M3", "length": 13})
    assert e.value.code == "bad_args" and "nearest is 12" in str(e.value)
    assert not [c for c in hub.fake.calls if c[0] == "submit_op"]


def test_settings_show_values_who_changed_them_and_pending_suggestions(hub):
    s = hub.get("/settings")
    by_key = {x["key"]: x for x in s["settings"]}
    assert by_key["tol_clearance"]["value"] == 0.3 and by_key["tol_clearance"]["changed_by"] == "carol"
    assert by_key["min_wall"]["is_default"] and by_key["min_wall"]["value"] == 1.2
    assert [g["id"] for g in s["suggestions"]] == ["g_1"]
    g = s["suggestions"][0]
    assert g["component_name"] == "NEMA17 mount" and g["current"] == -0.05 and g["by"] == "alice-claude"


def test_setting_and_suggesting_call_their_reducers_with_checked_values(hub):
    hub.post("/settings", {"setting": "hole_clearance_class", "value": "close"})
    assert hub.fake.calls[-1] == ("set_setting", (2, "hole_clearance_class", 0.0, "close"))
    hub.post("/settings", {"setting": "tol_clearance", "value": "0.25"})
    assert hub.fake.calls[-1] == ("set_setting", (2, "tol_clearance", 0.25, ""))
    with pytest.raises(HubRequestError):
        hub.post("/settings", {"setting": "tol_clearance", "value": 7})
    r = hub.post("/settings/suggest", {"setting": "tol_press_fit", "value": 0, "reason": "printed 608 seats",
                                       "component": "c_std_ball_bearing"})
    name, args = hub.fake.calls[-1]
    assert name == "suggest_setting" and args[0] == 2 and args[1] == r["id"]
    assert args[2:] == ("tol_press_fit", 0.0, "", "printed 608 seats", "c_std_ball_bearing")
    with pytest.raises(HubRequestError):
        hub.post("/settings/suggest", {"setting": "tol_press_fit", "value": 0, "reason": " "})
    hub.post("/settings/suggestions/g_1", {"accept": True})
    assert hub.fake.calls[-1] == ("resolve_suggestion", (2, "g_1", True))
    hub.post("/settings/reset", {"setting": "tol_clearance"})
    assert hub.fake.calls[-1] == ("reset_setting", (2, "tol_clearance"))


def test_a_spilled_library_result_is_read_from_its_blob(hub):
    hub.fake.tables["v_artifact_blob"] = [{"hash": "a" * 64, "bytes": json.dumps({"ok": 1}).encode().hex()}]
    assert hub._result(json.dumps({"blob": "a" * 64, "_components": [{"id": "c_x"}]})) == {"ok": 1}


# ------------------------------------------------------------------ MCP tools

class RecordingHub:
    def __init__(self):
        self.activity, self.posts, self.gets = [], [], []

    def log_activity(self, kind, summary, targets=None):
        self.activity.append((kind, summary, targets))

    def post(self, path, body=None):
        self.posts.append((path, body))
        return {"ok": True}

    def get(self, path, **params):
        self.gets.append(path)
        return {"path": path}


@pytest.fixture
def session(monkeypatch):
    ready = threading.Event()
    ready.set()
    s = types.SimpleNamespace(client=RecordingHub(), dead=None, doing="", cfg={"name": "a"}, ready=ready,
                              join_error=None)
    monkeypatch.setattr(mcp_server, "S", s)
    return s


def test_library_tools_reach_their_routes(session):
    mcp_server.search_components("bearing", constraints={"bore_d": 8})
    assert session.client.posts[-1][0] == "/library/search"
    mcp_server.recommend_components("an 8 mm axle")
    assert session.client.posts[-1][0] == "/library/recommend"
    mcp_server.add_standard_part("iso4762", "M3", 12, name="Bolt1")
    assert session.client.posts[-1] == ("/library/insert", {"family": "iso4762", "size": "M3", "length": 12,
                                                             "placement": None, "name": "Bolt1"})
    assert session.client.activity[-1][2] == ["Bolt1"]
    mcp_server.insert_component("c_x")
    assert session.client.posts[-1][1]["id"] == "c_x"
    mcp_server.import_component_from_url("https://a.test/x.step", license="CC-BY-4.0")
    assert session.client.posts[-1][0] == "/library/import-url"
    mcp_server.hole_size("ball_bearing", "608")
    assert session.client.posts[-1][0] == "/library/hole"
    mcp_server.suggest_setting("tol_press_fit", 0.0, "printed seats")
    assert session.client.posts[-1] == ("/settings/suggest", {"setting": "tol_press_fit", "value": 0.0,
                                                               "reason": "printed seats", "component": None})
    assert mcp_server.get_settings() == {"path": "/settings"}
    assert mcp_server.get_component("c_std_iso4762") == {"path": "/library/c_std_iso4762"}


def test_agents_are_told_to_use_the_library_and_only_suggest_settings():
    text = mcp_server.INSTRUCTIONS
    assert "recommend_components" in text and "suggest_setting" in text and "Only people change settings" in text


def test_agents_see_the_team_settings_and_library_in_their_context(hub):
    lines = hub._library_context()
    assert "tol_clearance=0.3" in lines[0] and "1 setting suggestion(s) await a person" in lines[0]
    assert "NEMA17 mount (c_nema17_mount_aa11bb)" in lines[1]


def test_the_context_still_works_against_a_module_without_the_library(hub):
    from cadswarm.space import SpaceError

    def missing(query):
        if "v_setting" in query:
            raise SpaceError(400, "no such table v_setting")
    hub.fake.on_sql = missing
    assert hub._library_context() == []


def test_prompts_about_parts_get_a_library_hint():
    from cadswarm.prompt_hook import library_hint
    assert library_hint("press-fit a 608 bearing into the base")
    assert library_hint("hold it with four M3 screws")
    assert not library_hint("make the base 5 mm taller")
