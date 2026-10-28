"""SpaceHub turns view rows and reducer calls back into the old hub's JSON shapes."""
import json

import pytest

from cadswarm.client import HubRequestError
from cadswarm.space import SpaceError
from cadswarm.space_hub import SpaceHub


class FakeSpace:
    """In-memory stand-in for SpaceClient: tables by view name, a log of reducer calls."""

    def __init__(self, tables):
        self.tables = {k: list(v) for k, v in tables.items()}
        self.calls = []
        self.fail = {}
        self.on_sql = None
        self.timeout = 30

    def call(self, reducer, *args):
        self.calls.append((reducer, args))
        if reducer in self.fail:
            raise SpaceError(530, self.fail[reducer])
        if reducer == "submit_op":
            self.tables.setdefault("v_op", []).append(
                {"id": args[1], "kind": args[2], "status": "queued", "result_json": "", "error_code": "", "error": ""})

    def sql(self, query):
        if self.on_sql:
            self.on_sql(query)
        view = query.split("FROM")[1].split()[0]
        rows = self.tables.get(view, [])
        if "WHERE seq >" in query:
            since = int(query.rsplit(">", 1)[1])
            rows = [r for r in rows if r["seq"] > since]
        if "WHERE id =" in query:
            want = query.rsplit("=", 1)[1].strip().strip("'")
            rows = [r for r in rows if r["id"] == want]
        if "WHERE hash =" in query:
            want = query.rsplit("=", 1)[1].strip().strip("'")
            rows = [r for r in rows if r["hash"] == want]
        return [dict(r) for r in rows]


def member(id, name, kind="agent", status="active", **kw):
    return {"id": id, "name": name, "kind": kind, "client": "claude", "role": "member", "status": status,
            "status_text": "", "fence": 1, "last_seen_ms": 0, "context_seq": 0, **kw}


@pytest.fixture
def world():
    tables = {
        "v_me": [member(1, "alice-claude", fence=2)],
        "v_member": [member(1, "alice-claude", fence=2), member(2, "bob-codex"), member(3, "carol", kind="human")],
        "v_lock": [{"object": "Plate", "holder": 1}, {"object": "Mount", "holder": 2}],
        "v_object": [{"name": "Plate", "label": "Plate", "type_id": "Part::Box", "rev": 2, "bbox": [0, 0, 0, 1, 1, 1],
                      "volume": 1.0, "visible": True, "broken": False, "uses": [], "used_by": [],
                      "expressions_json": "{}", "placement_json": ""}],
        "v_param": [{"alias": "pitch", "value": "40.0", "num": 40.0}],
        "v_task": [{"id": "t_1", "title": "lid", "detail": "", "objects": [], "status": "open", "owner": 0,
                    "created_by": 1, "rev": 1, "result_json": "", "assignee": "", "brief": "", "created_ms": 1, "updated_ms": 1}],
        "v_message": [
            {"id": "m_a", "from": 2, "to": "member:1", "kind": "message", "body": "widen the slot", "task_id": "", "created_ms": 1},
            {"id": "m_b", "from": 3, "to": "member:1", "kind": "steer", "body": "use M4 bolts", "task_id": "", "created_ms": 2},
            {"id": "m_c", "from": 1, "to": "all", "kind": "message", "body": "mine", "task_id": "", "created_ms": 3},
            {"id": "m_d", "from": 2, "to": "member:3", "kind": "message", "body": "for carol", "task_id": "", "created_ms": 4},
        ],
        "v_receipt": [],
        "v_event": [{"seq": 5, "kind": "op", "actor": 2, "at_ms": 1000,
                     "data_json": json.dumps({"name": "bob-codex", "op": "create_primitive", "created": ["Mount"],
                                              "modified": [], "deleted": [], "recomputed": [], "broken": []})}],
    }
    fake = FakeSpace(tables)
    hub = SpaceHub({"uri": "http://x", "db": "d", "name": "alice-claude"},
                   {"participant_id": 1, "token": "t", "fence": 2}, space=fake)
    return hub, fake


def test_model_has_the_old_shape_with_lock_holders_by_name(world):
    hub, _ = world
    m = hub.get("/model")
    plate = m["objects"][0]
    assert plate["locked_by"] == "alice-claude" and plate["rev"] == 2 and plate["type"] == "Part::Box"
    assert m["params"] == {"pitch": 40.0}
    assert {p["name"] for p in m["participants"]} == {"alice-claude", "bob-codex", "carol"}
    assert m["seq"] == 5


def test_reducers_carry_the_fence_and_errors_keep_their_code(world):
    hub, fake = world
    hub.post("/locks", {"names": ["Lid"]})
    assert fake.calls[-1] == ("lock_objects", (2, ["Lid"]))
    fake.fail["lock_objects"] = "locked: Already locked: Mount by bob-codex (active)."
    with pytest.raises(HubRequestError) as e:
        hub.post("/locks", {"names": ["Mount"]})
    assert e.value.status == 409 and e.value.code == "locked" and "bob-codex" in str(e.value)


def test_removing_a_member_revokes_any_helper_and_reports_unknown_ids(world):
    hub, fake = world
    assert hub.post("/participants/2/remove") == {"removed": "bob-codex", "id": 2}
    assert fake.calls[-1] == ("revoke_member", (2, 2))  # fence first, then the teammate's id
    before = len(fake.calls)
    with pytest.raises(HubRequestError) as e:
        hub.post("/participants/99/remove")
    assert e.value.status == 404 and e.value.code == "unknown_participant"
    assert len(fake.calls) == before  # refused before any reducer runs


def test_removed_helpers_drop_out_of_the_team_list(world):
    hub, fake = world
    fake.tables["v_member"].append(member(4, "gone-claude", status="removed"))
    assert {p["name"] for p in hub.participants()} == {"alice-claude", "bob-codex", "carol"}


def test_an_op_is_queued_then_awaited_until_the_host_finishes_it(world):
    hub, fake = world

    def host_finishes(query):
        for o in fake.tables.get("v_op", []):
            if o["status"] == "queued":
                o["status"] = "done"
                o["result_json"] = json.dumps({"created": ["Lid"], "revs": {"Lid": 1}})

    fake.on_sql = host_finishes
    r = hub.post("/ops", {"op": "create_primitive", "args": {"kind": "box", "name": "Lid"}})
    assert r["created"] == ["Lid"]
    reducer, args = fake.calls[-1]
    assert reducer == "submit_op" and args[0] == 2 and args[2] == "create_primitive"
    assert args[1].startswith("o_") and len(args[1]) == 14 and json.loads(args[3]) == {"kind": "box", "name": "Lid"}
    assert args[4] == ""


def test_a_failed_op_surfaces_its_code(world):
    hub, fake = world

    def host_rejects(query):
        for o in fake.tables.get("v_op", []):
            o.update(status="rejected", error_code="lock_violation", error="would change ['Plate']")

    fake.on_sql = host_rejects
    with pytest.raises(HubRequestError) as e:
        hub.post("/ops", {"op": "run_part_script", "args": {"code": "..."}})
    assert e.value.status == 409 and e.value.code == "lock_violation"


def test_a_refused_submit_is_logged_as_activity_before_it_raises(world):
    hub, fake = world
    fake.fail["submit_op"] = 'not_locked: Lock ["Mount"] before set_props. Current holders: bob-codex'
    with pytest.raises(HubRequestError) as e:
        hub.post("/ops", {"op": "set_props", "args": {"name": "Mount", "props": {"Length": 5}}})
    assert e.value.code == "not_locked"
    logged = [c for c in fake.calls if c[0] == "log_activity"]
    assert logged and logged[-1][1][0] == 2
    kind, summary, targets = logged[-1][1][1:]
    assert kind == "refused" and "set_props" in summary and "Mount" in summary
    assert targets == ["Mount"]
    # Only a lock refusal is the evidence the demo quotes; a bad request is not logged as one.
    fake.fail["submit_op"] = "bad_args: op_id must be new"
    with pytest.raises(HubRequestError):
        hub.post("/ops", {"op": "set_props", "args": {"name": "Mount"}})
    assert len([c for c in fake.calls if c[0] == "log_activity"]) == 1


def test_inbox_puts_human_steering_first_skips_my_own_and_others_mail(world):
    hub, fake = world
    msgs = hub.get("/inbox")["messages"]
    assert [m["id"] for m in msgs] == ["m_b", "m_a"]
    assert msgs[0]["from"] == "carol" and msgs[0]["kind"] == "steer"
    assert ("mark_delivered", (2, ["m_b", "m_a"])) in fake.calls


def test_consumed_messages_leave_the_inbox(world):
    hub, fake = world
    fake.tables["v_receipt"] = [{"message_id": "m_b", "member": 1, "state": "consumed"}]
    assert [m["id"] for m in hub.get("/inbox")["messages"]] == ["m_a"]


def test_tasks_have_names_resolved(world):
    hub, _ = world
    t = hub.get("/tasks")["tasks"][0]
    assert t["id"] == "t_1" and t["created_by_name"] == "alice-claude" and t["owner_name"] is None


def test_events_since_a_cursor(world):
    hub, _ = world
    r = hub.get("/events", since=0, wait=0)
    assert r["seq"] == 5 and r["events"][0]["created"] == ["Mount"]
    assert hub.get("/events", since=5, wait=0) == {"seq": 5, "events": []}


def test_polls_read_the_event_head_and_only_read_history_when_it_moved(world):
    """A view runs over all its rows before WHERE applies, so "anything newer?" on v_event re-read the whole
    history on every poll, every second, from every helper and panel."""
    hub, fake = world
    fake.tables["v_event_head"] = [{"ws": 1, "seq": 5}]
    reads = []
    fake.on_sql = lambda q: reads.append(q.split("FROM")[1].split()[0])
    assert hub.latest_seq() == 5
    assert hub.get("/events", since=5, wait=0) == {"seq": 5, "events": []}
    assert "v_event" not in reads, reads
    fake.tables["v_event"].append({"seq": 6, "kind": "joined", "actor": 3, "at_ms": 2000, "data_json": "{}"})
    fake.tables["v_event_head"] = [{"ws": 1, "seq": 6}]
    assert [e["seq"] for e in hub.get("/events", since=5, wait=0)["events"]] == [6]


def test_a_module_without_the_event_head_falls_back_to_the_history(world):
    hub, fake = world
    real_sql, asked = fake.sql, []

    def sql(query):
        asked.append(query)
        if "v_event_head" in query:
            raise SpaceError(400, "error: no such table: `v_event_head`. If the table exists, it may be marked private.")
        return real_sql(query)

    fake.sql = sql
    assert hub.latest_seq() == 5
    assert hub.latest_seq() == 5
    assert sum("v_event_head" in q for q in asked) == 1, "an older module is asked once, not on every poll"


def test_context_mentions_team_changes_and_unread_and_advances_the_cursor(world):
    hub, fake = world
    text = hub.get("/context")["context"]
    assert "bob-codex" in text and "created ['Mount']" in text and "use M4 bolts" in text
    assert ("set_context_seq", (2, 5)) in fake.calls


def test_join_rejoins_and_takes_the_new_fence(world):
    hub, fake = world
    fake.tables["v_me"] = [member(1, "alice-claude", fence=3)]
    ident = hub.join()
    assert fake.calls[0] == ("rejoin", ("",))
    assert ident["fence"] == 3 and hub.fence == 3 and ident["participant_id"] == 1


def test_minting_an_invite_calls_the_reducer_and_decodes_back(world):
    from cadswarm import ticket
    from cadswarm.invite import read_invite

    hub, fake = world
    code = hub.create_invite("agent", "member", 3600, "Phone stand")
    t = read_invite(code)
    assert t["uri"] == "http://x" and t["db"] == "d" and t["project"] == "Phone stand"
    assert t["kind"] == "agent" and t["role"] == "member" and len(t["token"]) == 64
    assert fake.calls[-1] == ("create_invite", (2, ticket.hash_token(t["token"]), "agent", "member", 3600))


def test_an_unknown_path_is_a_404(world):
    hub, _ = world
    with pytest.raises(HubRequestError) as e:
        hub.get("/nope")
    assert e.value.status == 404


# ---- Step 6: the routes the beginner UX and the file tools use


def finish_ops_with(fake, result):
    def host(query):
        for o in fake.tables.get("v_op", []):
            if o["status"] == "queued":
                o["status"], o["result_json"] = "done", json.dumps(result)
    fake.on_sql = host


def test_file_routes_become_helper_ops_with_the_contract_args(world):
    hub, fake = world
    finish_ops_with(fake, {"path": "notes.md", "version": 1})
    assert hub.post("/project/files/save", {"path": "notes.md", "text": "hi"})["version"] == 1
    reducer, args = fake.calls[-1]
    assert reducer == "submit_op" and args[2] == "save_file" and json.loads(args[3]) == {"path": "notes.md", "text": "hi"}
    hub.post("/project/files/move", {"from": "a.md", "to": "b.md"})
    assert fake.calls[-1][1][2] == "move_file"
    assert json.loads(fake.calls[-1][1][3]) == {"from_path": "a.md", "to_path": "b.md"}
    hub.get("/project/files", prefix="notes")
    assert fake.calls[-1][1][2] == "list_files" and json.loads(fake.calls[-1][1][3]) == {"folder": "notes"}
    for path, body, kind in [("/project/files/read", {"path": "a"}, "read_file"),
                             ("/project/files/folder", {"path": "p"}, "make_folder"),
                             ("/project/files/delete", {"path": "a"}, "delete_file"),
                             ("/project/files/history", {"path": "a"}, "file_history"),
                             ("/project/files/restore", {"path": "a", "version": 1}, "restore_file"),
                             ("/project/snapshot", {"label": "x"}, "snapshot"),
                             ("/project/storage/prune", {"keep_versions": 2}, "prune_storage")]:
        hub.post(path, body)
        assert fake.calls[-1][1][2] == kind and json.loads(fake.calls[-1][1][3]) == body
    hub.get("/project/storage")
    assert fake.calls[-1][1][2] == "storage_status"


def test_knowledge_routes_become_helper_ops(world):
    hub, fake = world
    finish_ops_with(fake, {"answers": []})
    hub.post("/knowledge/board", {"question": "pico mounting holes", "board": "raspberry-pi-pico"})
    assert fake.calls[-1][1][2] == "lookup_board"
    assert json.loads(fake.calls[-1][1][3]) == {"question": "pico mounting holes", "board": "raspberry-pi-pico"}
    hub.post("/knowledge/designs", {"query": "pico case"})
    assert fake.calls[-1][1][2] == "recall_designs"
    assert json.loads(fake.calls[-1][1][3]) == {"query": "pico case", "board": ""}


def test_large_results_are_read_from_the_blob_view(world):
    hub, fake = world
    big = {"objects": {"Plate": {"brep": "x" * 50}}}
    fake.tables["v_artifact_blob"] = [{"hash": "ab" * 32, "ws": 1, "bytes": json.dumps(big).encode().hex()}]
    finish_ops_with(fake, {"blob": "ab" * 32})
    assert hub.post("/brep", {"names": ["Plate"]}) == {"objects": {"Plate": {"brep": "x" * 50, "locked_by": "alice-claude"}}}
    assert fake.calls[-1][1][2] == "brep"


def test_a_spilled_result_that_expired_says_so(world):
    hub, fake = world
    finish_ops_with(fake, {"blob": "cd" * 32})
    with pytest.raises(HubRequestError) as e:
        hub.get("/project/files")
    assert e.value.code == "expired"


def test_raw_download_decodes_base64(world):
    hub, fake = world
    finish_ops_with(fake, {"path": "a.bin", "base64": "AAE="})
    assert hub.request("GET", "/project/files/download?path=a.bin", raw=True) == b"\x00\x01"
    assert json.loads(fake.calls[-1][1][3]) == {"path": "a.bin", "raw": True}


def test_export_downloads_read_one_blob_by_hash(world):
    hub, fake = world
    finish_ops_with(fake, {"hash": "ef" * 32, "name": "model-3.stl", "size": 2, "objects": ["Plate"]})
    r = hub.post("/export", {"format": "stl", "names": ["Plate"]})
    assert r["url"] == "/artifacts/" + "ef" * 32 and r["file"] == "model-3.stl"
    fake.tables["v_artifact_blob"] = [{"hash": "ef" * 32, "ws": 1, "bytes": "0102"}]
    assert hub.request("GET", r["url"], raw=True) == b"\x01\x02"


def test_health_is_ok_while_a_host_member_is_active(world):
    hub, fake = world
    assert hub.get("/health")["ok"] is False
    fake.tables["v_member"].append(member(9, "host", kind="host"))
    assert hub.get("/health")["ok"] is True


def test_storage_settings_are_host_only(world):
    hub, _ = world
    with pytest.raises(HubRequestError) as e:
        hub.post("/project/storage/settings", {"limit_gb": 5})
    assert e.value.code == "host_only"


# ---- the FreeCAD dashboard: commands (sent / seen / done), chat last, what each helper is doing

def test_commands_show_sent_seen_done(world):
    hub, fake = world
    fake.tables["v_me"] = [member(3, "carol", kind="human", fence=2)]
    hub.identity["participant_id"] = 3
    fake.tables["v_message"] = [
        {"id": "m_1", "from": 3, "to": "member:2", "kind": "steer", "body": "make the lid 20 mm", "task_id": "", "created_ms": 10},
        {"id": "m_2", "from": 3, "to": "all", "kind": "steer", "body": "check for clashes", "task_id": "", "created_ms": 11},
        {"id": "m_3", "from": 3, "to": "all", "kind": "chat", "body": "hi team", "task_id": "", "created_ms": 12},
    ]
    fake.tables["v_receipt"] = [{"message_id": "m_1", "member": 2, "state": "delivered"},
                                {"message_id": "m_2", "member": 2, "state": "delivered"}]
    cmds = {c["body"]: c for c in hub.get("/commands")["commands"]}
    assert set(cmds) == {"make the lid 20 mm", "check for clashes"}  # chat isn't an instruction
    assert cmds["make the lid 20 mm"]["state"] == "seen" and cmds["make the lid 20 mm"]["to"] == "bob-codex"
    assert cmds["check for clashes"]["state"] == "sent"  # alice hasn't read it yet
    assert cmds["check for clashes"]["by_helper"] == {"alice-claude": "sent", "bob-codex": "seen"}
    fake.tables["v_receipt"][0]["state"] = "consumed"
    assert {c["body"]: c for c in hub.get("/commands")["commands"]}["make the lid 20 mm"]["state"] == "done"


def test_commands_come_first_and_peoples_chat_never_reaches_agents(world):
    hub, fake = world
    fake.tables["v_message"].append({"id": "m_e", "from": 3, "to": "all", "kind": "chat", "body": "lunch at 1?",
                                     "task_id": "", "created_ms": 0})
    kinds = [m["kind"] for m in hub.get("/inbox")["messages"]]
    assert kinds[0] == "steer" and "chat" not in kinds
    ctx = hub.get("/context")["context"]
    assert "[COMMAND] carol: use M4 bolts" in ctx and "lunch at 1?" not in ctx


def test_participants_say_what_each_helper_is_working_on(world, monkeypatch):
    hub, fake = world
    fake.tables["v_task"][0].update(status="claimed", owner=2, title="Base plate")
    fake.tables["v_event"].append({"seq": 6, "kind": "op", "actor": 2, "at_ms": 100_000, "data_json": "{}"})
    monkeypatch.setattr("cadswarm.space_hub.time.time", lambda: 112.0)
    team = {p["name"]: p for p in hub.participants()}
    assert team["bob-codex"]["task"] == {"id": "t_1", "title": "Base plate"}
    assert team["bob-codex"]["idle_s"] == 12.0
    assert team["alice-claude"]["task"] is None and team["alice-claude"]["idle_s"] is None


def test_requests_to_the_whole_team_show_their_progress(world):
    hub, fake = world
    fake.tables["v_brief"] = [
        {"id": "b_old", "by": 3, "text": "a phone stand", "board": "", "status": "ready", "plan_json": "",
         "stl_hash": "ab" * 32, "created_ms": 1, "updated_ms": 2},
        {"id": "b_new", "by": 3, "text": "an enclosure for the pico", "board": "", "status": "building", "plan_json": "",
         "stl_hash": "", "created_ms": 5, "updated_ms": 6}]
    fake.tables["v_task"] = [dict(fake.tables["v_task"][0], id="t_a", brief="b_new", status="done"),
                             dict(fake.tables["v_task"][0], id="t_b", brief="b_new", status="claimed")]
    briefs = hub.get("/briefs")["briefs"]
    assert [b["id"] for b in briefs] == ["b_new", "b_old"]  # newest first
    assert (briefs[0]["tasks"], briefs[0]["done"], briefs[0]["by"]) == (2, 1, "carol")
    assert briefs[1]["stl"] == "ab" * 32  # an older brief keeps its finished model reachable
    r = hub.post("/briefs", {"text": "a lid"})
    assert fake.calls[-1][0] == "submit_brief" and fake.calls[-1][1][1] == r["brief"]


def test_the_team_server_is_not_one_of_all_helpers(world):
    hub, fake = world
    fake.tables["v_member"].append(member(9, "host", kind="host"))
    fake.tables["v_message"] = [{"id": "m_9", "from": 1, "to": "all", "kind": "steer", "body": "check clashes",
                                 "task_id": "", "created_ms": 1}]
    fake.tables["v_receipt"] = [{"message_id": "m_9", "member": 2, "state": "consumed"}]
    cmd = hub.get("/commands")["commands"][0]
    assert set(cmd["by_helper"]) == {"alice-claude", "bob-codex"}  # every AI helper, never the host


def test_style_routes_become_helper_ops(world):
    hub, fake = world
    finish_ops_with(fake, {"text": "", "rules": [], "pending": 0})
    hub.request("GET", "/style", params={"topic": "lid", "board": "pico"})
    assert fake.calls[-1][1][2] == "style_context"
    assert json.loads(fake.calls[-1][1][3]) == {"query": "lid", "board": "pico"}
    hub.post("/style/teach", {"statement": "Walls are 2.4 mm"})
    assert fake.calls[-1][1][2] == "teach_style"
    assert json.loads(fake.calls[-1][1][3]) == {"statement": "Walls are 2.4 mm", "scope": "team"}
    hub.post("/style/inbox", {})
    assert fake.calls[-1][1][2] == "style_inbox"
    hub.post("/style/resolve", {"rule_id": "r_1", "decision": "confirm"})
    assert fake.calls[-1][1][2] == "resolve_style"
    hub.post("/style/resolve", {"rule_ids": ["r_1", "r_2"], "decision": "confirm"})
    assert fake.calls[-1][1][2] == "resolve_style"
    assert json.loads(fake.calls[-1][1][3]) == {"rule_ids": ["r_1", "r_2"], "decision": "confirm"}


def test_the_context_carries_the_published_style_profile(world):
    hub, fake = world
    text = "Team design style (learned ...):\n- [confirmed] Walls are 2.4 mm"
    fake.tables["v_artifact_blob"] = [{"hash": "style-profile", "ws": 1, "bytes": text.encode().hex()}]
    assert "[confirmed] Walls are 2.4 mm" in hub.get("/context")["context"]
    fake.tables["v_artifact_blob"] = []
    assert "Team design style" not in hub.get("/context")["context"]


def test_people_see_each_others_chat(world):
    _hub, fake = world
    fake.tables["v_message"].append({"id": "m_chat", "from": 3, "to": "all", "kind": "chat", "body": "lunch?",
                                     "task_id": "", "created_ms": 9})
    fake.tables["v_member"].append(member(4, "dan", kind="human"))
    dan = SpaceHub({"uri": "http://x", "db": "d"}, {"participant_id": 4, "fence": 1}, space=fake)
    assert "m_chat" in [m["id"] for m in dan._unconsumed()]


def test_an_op_rests_the_pointer_on_the_vertex_the_worker_picked(world):
    hub, fake = world

    def host_finishes(query):
        for o in fake.tables.get("v_op", []):
            if o["status"] == "queued":
                o["status"] = "done"
                o["result_json"] = json.dumps({"name": "Lid", "pointer": {"object": "Lid", "point": [10, 6, 4]}})

    fake.on_sql = host_finishes
    hub.post("/ops", {"op": "create_primitive", "args": {"kind": "box", "name": "Lid"}})
    assert fake.calls[-1] == ("move_pointer", (2, "Lid", [10.0, 6.0, 4.0], "pointing", "create primitive"))


def test_point_at_without_a_point_uses_the_parts_anchor(world):
    hub, fake = world

    def host_finishes(query):
        for o in fake.tables.get("v_op", []):
            if o["status"] == "queued":
                o["status"] = "done"
                o["result_json"] = json.dumps({"name": "Plate", "anchor": [1, 1, 1]})

    fake.on_sql = host_finishes
    r = hub.post("/pointer", {"object": "Plate", "mode": "inspecting", "label": "checking"})
    assert r["point"] == [1, 1, 1]
    assert fake.calls[-1] == ("move_pointer", (2, "Plate", [1.0, 1.0, 1.0], "inspecting", "checking"))
    hub.post("/pointer", {"object": "Plate", "point": [0, 0.5, 1]})
    assert fake.calls[-1] == ("move_pointer", (2, "Plate", [0.0, 0.5, 1.0], "pointing", ""))
    with pytest.raises(HubRequestError):
        hub.post("/pointer", {"object": "Plate", "point": [1, 2]})


def test_a_failed_pointer_move_never_fails_the_op(world):
    hub, fake = world
    fake.fail["move_pointer"] = "boom"

    def host_finishes(query):
        for o in fake.tables.get("v_op", []):
            if o["status"] == "queued":
                o["status"] = "done"
                o["result_json"] = json.dumps({"name": "Lid", "pointer": {"object": "Lid", "point": [1, 2, 3]}})

    fake.on_sql = host_finishes
    assert hub.post("/ops", {"op": "create_primitive", "args": {"kind": "box", "name": "Lid"}})["name"] == "Lid"


def test_lighten_start_checks_the_part_and_sends_its_rev_box_and_the_team_min_wall(world):
    hub, fake = world
    fake.tables["v_setting"] = [{"setting": "min_wall", "num": 1.2, "text": "", "set_by": 3, "updated_ms": 1}]
    finish_ops_with(fake, {"job_id": "lj_ab12", "status": "queued"})
    out = hub.post("/lattice/jobs", {"target": "Plate", "cell": "octet", "stiffness_target_pct": 85})
    assert out["job_id"] == "lj_ab12"
    kind, args = fake.calls[-1][1][2], json.loads(fake.calls[-1][1][3])
    assert kind == "lattice_start" and args["revs"] == {"Plate": 2} and args["_bbox"] == [0, 0, 0, 1, 1, 1]
    assert args["min_thickness_mm"] == 1.2 and args["cell"] == "octet" and "wait" not in args
    with pytest.raises(HubRequestError) as e:
        hub.post("/lattice/jobs", {"target": "Nope"})
    assert e.value.code == "bad_args"


def test_lighten_apply_refuses_a_changed_source_and_inserts_a_mesh_otherwise(world):
    hub, fake = world
    finish_ops_with(fake, {"file": "lattice/x/result.stl", "target": "Plate", "name": "Plate_lattice",
                           "revs": {"Plate": 1}, "verdict": "passed"})
    with pytest.raises(HubRequestError) as e:
        hub.post("/lattice/jobs/lj_ab12/apply", {})
    assert e.value.code == "stale_source" and "rev 1 -> 2" in str(e.value)
    out = hub.post("/lattice/jobs/lj_ab12/apply", {"force": True})
    kind, args = fake.calls[-1][1][2], json.loads(fake.calls[-1][1][3])
    assert kind == "insert_mesh" and args == {"name": "Plate_lattice", "file": "lattice/x/result.stl",
                                              "label": "Plate_lattice", "source": "Plate", "job": "lj_ab12"}
    assert "unchanged" in out["note"]


def test_lighten_status_waits_by_polling_from_the_client(world, monkeypatch):
    hub, fake = world
    states = iter(["generating", "generating", "done"])
    monkeypatch.setattr("cadswarm.space_hub.time.sleep", lambda s: None)

    def host(query):
        for o in fake.tables.get("v_op", []):
            if o["status"] == "queued":
                o["status"], o["result_json"] = "done", json.dumps({"job_id": "lj_ab12", "status": next(states)})
    fake.on_sql = host
    assert hub.get("/lattice/jobs/lj_ab12", wait=30)["status"] == "done"
    assert [c[1][2] for c in fake.calls if c[0] == "submit_op"] == ["lattice_status"] * 3

def test_spare_work_is_a_busy_helpers_queue_and_pickup_returns_the_task_taken(world):
    hub, fake = world
    row = fake.tables["v_task"][0]
    fake.tables["v_task"] = [
        {**row, "id": "t_now", "status": "claimed", "owner": 2, "assignee": "bob-codex"},
        {**row, "id": "t_next", "assignee": "bob-codex", "created_ms": 2},
        {**row, "id": "t_held", "assignee": "bob-codex", "objects": ["Mount"], "created_ms": 3},  # bob holds Mount
        {**row, "id": "t_mine", "assignee": "alice-claude", "created_ms": 4},
    ]
    assert [t["id"] for t in hub.get("/tasks/spare")["tasks"]] == ["t_next"]
    fake.tables["v_event"].append({"seq": 6, "kind": "task_claimed", "actor": 1, "at_ms": 2000,
                                   "data_json": json.dumps({"task": "t_next", "name": "alice-claude", "from": "bob-codex"})})
    t = hub.post("/tasks/pickup", {})
    assert fake.calls[-1] == ("pick_up_task", (2, ""))
    assert t["id"] == "t_next" and t["picked_up_from"] == "bob-codex"


def test_the_mirror_catches_changes_that_made_no_event_and_retries_failed_fetches(world):
    hub, fake = world
    plate = fake.tables["v_object"][0]
    fetched = []

    def brep(path, body):
        fetched.append(body["names"])
        return {"objects": {n: {"brep": "B"} for n in body["names"]}}

    hub.post = brep
    breps, gone, applied = hub.mirror_changes({})
    assert fetched == [["Plate"]] and list(breps) == ["Plate"] and gone == [] and set(applied) == {"Plate"}
    assert hub.mirror_changes(applied) == ({}, [], applied) and len(fetched) == 1  # nothing changed: no fetch
    # The host re-synced its document: Plate grew (same rev, no event), Lid appeared, Bolt is gone.
    fake.tables["v_object"] = [{**plate, "bbox": [0, 0, 0, 2, 1, 1]}, {**plate, "name": "Lid"}]
    breps, gone, applied = hub.mirror_changes({**applied, "Bolt": (1, (), 0.0, "")})
    assert fetched[-1] == ["Lid", "Plate"] and gone == ["Bolt"] and set(applied) == {"Lid", "Plate"}

    def down(path, body):
        raise HubRequestError(504, {"error": "busy", "code": "host_timeout"})

    hub.post = down
    fake.tables["v_object"][1] = {**plate, "name": "Lid", "volume": 9.0}
    with pytest.raises(HubRequestError):
        hub.mirror_changes(applied)  # the caller keeps `applied`, so the next pass fetches Lid again


def test_the_mirror_hides_a_part_a_boolean_consumed(world):
    # A fusion hides its base and tool in the team's document without bumping their revs, bbox or volume.
    # If the mirror missed that, both stay drawn inside the fusion and their faces z-fight with it.
    hub, fake = world
    plate = fake.tables["v_object"][0]
    hub.post = lambda path, body: {"objects": {n: {"brep": "B", "visible": False} for n in body["names"]}}
    _, _, applied = hub.mirror_changes({})
    fake.tables["v_object"] = [{**plate, "visible": False}]
    breps, gone, applied = hub.mirror_changes(applied)
    assert list(breps) == ["Plate"] and breps["Plate"]["visible"] is False and gone == []
    assert hub.mirror_changes(applied)[0] == {}  # and then it stays quiet


# ---- printer bed and review rounds


def test_the_workspace_reports_the_bed_and_defaults_to_180_mm(world):
    hub, fake = world
    assert hub.get("/workspace")["bed"] == [180.0, 180.0, 180.0]
    fake.tables["v_workspace"] = [{"id": 1, "bed_x_mm": 256.0, "bed_y_mm": 210.0, "bed_z_mm": 300.0}]
    r = hub.get("/workspace")
    assert r["bed"] == [256.0, 210.0, 300.0] and r["bed_z_mm"] == 300.0


def test_setting_the_bed_calls_the_reducer_and_checks_the_size(world):
    hub, fake = world
    hub.post("/bed", {"x_mm": 256, "y_mm": 210, "z_mm": 300})
    assert fake.calls[-1] == ("set_bed", (2, 256.0, 210.0, 300.0))
    for body in ({"x_mm": 0, "y_mm": 180, "z_mm": 180}, {"x_mm": 5000, "y_mm": 180, "z_mm": 180},
                 {"x_mm": 180, "y_mm": 180}):
        before = len(fake.calls)
        with pytest.raises(HubRequestError) as e:
            hub.post("/bed", body)
        assert e.value.status == 400 and e.value.code == "bad_args"
        assert len(fake.calls) == before


def test_reviews_are_newest_first_and_findings_read_as_sentences(world):
    hub, fake = world
    fake.tables["v_review"] = [
        {"id": "r_1", "brief": "b_1", "round": 1, "passed": False, "at_ms": 1000,
         "findings_json": json.dumps({"findings": [{"message": "hole too small"}, "lid loose"]})},
        {"id": "r_2", "brief": "b_1", "round": 2, "passed": True, "at_ms": 2000, "findings_json": "[]"},
    ]
    rows = hub.reviews()
    assert [r["id"] for r in rows] == ["r_2", "r_1"]
    assert rows[1]["findings"] == ["hole too small", "lid loose"] and rows[1]["at"] == 1.0
    # The reviewer's own shape names the part and the problem; show that, never the JSON blob.
    fake.tables["v_review"][0]["findings_json"] = json.dumps(
        [{"object": "Base", "problem": "the wall is 1.1 mm", "task_id": "t_1"}])
    assert hub.reviews()[1]["findings"] == ["Base: the wall is 1.1 mm"]
    fake.tables["v_review"][1]["findings_json"] = "{not json"
    assert hub.reviews()[0]["findings"] == []


def test_panel_snapshot_carries_reviews_and_the_printer_bed(world):
    hub, fake = world
    fake.tables["v_workspace"] = [{"id": 1, "bed_x_mm": 220.0, "bed_y_mm": 220.0, "bed_z_mm": 250.0}]
    fake.tables["v_review"] = [{"id": "r_1", "brief": "b_1", "round": 1, "passed": False, "at_ms": 1000,
                                "findings_json": json.dumps([{"text": "thin wall"}])}]
    snap = hub.panel_snapshot()
    assert snap["workspace"]["bed"] == [220.0, 220.0, 250.0]
    assert snap["reviews"][0]["findings"] == ["thin wall"]


# ---- stopping and removing work


def test_a_task_can_be_stopped_or_deleted_through_the_hub(world):
    hub, fake = world
    t = hub.post("/tasks/t_1/cancel", {"reason": "duplicate"})
    assert fake.calls[-1] == ("cancel_task", (2, "t_1", "duplicate")) and t["id"] == "t_1"
    r = hub.post("/tasks/t_1/delete")
    assert fake.calls[-1] == ("delete_task", (2, "t_1")) and r == {"task": "t_1", "title": "lid", "deleted": True}
    before = len(fake.calls)
    with pytest.raises(HubRequestError) as e:
        hub.post("/tasks/t_missing/delete")
    assert e.value.status == 404 and e.value.code == "unknown_task" and len(fake.calls) == before


def test_a_request_can_be_stopped_as_a_whole(world):
    hub, fake = world
    fake.tables["v_brief"] = [{"id": "b_1", "by": 3, "text": "a phone stand", "board": "", "status": "building",
                               "plan_json": "", "stl_hash": "", "created_ms": 1, "updated_ms": 2}]
    r = hub.post("/briefs/b_1/cancel")
    assert fake.calls[-1] == ("cancel_brief", (2, "b_1")) and r == {"brief": "b_1", "status": "cancelled"}


def test_after_an_op_the_pointer_rests_on_the_vertex_the_worker_picked(world):
    hub, fake = world
    # The worker's op result as the module stores it: the pointer sits inside the op's own result.
    r = {"created": ["BBS_Wheel"], "modified": [], "revs": {"BBS_Wheel": 1},
         "result": {"name": "BBS_Wheel", "pointer": {"object": "BBS_Wheel", "point": [228.6, 0.0, 215.9]}}}
    hub._point_after("run_part_script", r)
    assert fake.calls[-1] == ("move_pointer", (2, "BBS_Wheel", [228.6, 0.0, 215.9], "pointing", "run part script"))
    fake.calls.clear()
    hub._point_after("set_param", {"modified": ["Params"], "result": {"alias": "wall"}})  # nothing to point at
    assert fake.calls == []


def test_a_helper_can_ask_the_hub_for_one_fea_report(world):
    hub, _ = world
    seen = []
    hub.run_op = lambda kind, args: seen.append((kind, args)) or {"id": args.get("report"), "_events": []}
    assert hub.get("/fea/reports", report="fea_1a2b") == {"id": "fea_1a2b"}  # the module's events stay inside
    hub.get("/fea/reports")
    assert seen == [("fea_reports", {"report": "fea_1a2b", "limit": 100}), ("fea_reports", {"limit": 100})]
