"""The background runner wakes an AI helper only for new things meant for it, and never in a loop."""
import json
import os

from cadswarm import runner
from cadswarm.space_hub import SpaceHub
from test_space_hub import FakeSpace, member


def tables():
    return {
        "v_me": [member(1, "ann-claude", fence=2)],
        "v_member": [member(1, "ann-claude", fence=2), member(2, "bob-codex"), member(3, "carol", kind="human")],
        "v_lock": [], "v_object": [], "v_param": [], "v_receipt": [], "v_event": [], "v_brief": [],
        "v_task": [
            task("t_mine", "lid", assignee="ann-claude"),
            task("t_other", "base", assignee="bob-codex"),
            task("t_gave", "standoffs", created_by=1, owner=2, status="done", result_json='{"summary": "4 posts"}'),
        ],
        "v_message": [
            msg("m_steer", 3, "member:1", "steer", "make the lid 20 mm"),
            msg("m_ask", 2, "member:1", "message", "can you widen the slot?"),
            msg("m_chat", 3, "all", "chat", "lunch?"),
            msg("m_bob", 3, "member:2", "steer", "not for ann"),
        ],
    }


def task(tid, title, assignee="", created_by=3, owner=0, status="open", result_json=""):
    return {"id": tid, "title": title, "detail": "", "objects": [], "status": status, "owner": owner,
            "created_by": created_by, "rev": 1, "result_json": result_json, "assignee": assignee, "brief": "",
            "created_ms": 1, "updated_ms": 1}


def msg(mid, frm, to, kind, body):
    return {"id": mid, "from": frm, "to": to, "kind": kind, "body": body, "task_id": "", "created_ms": 1}


def hub(t=None):
    return SpaceHub({"uri": "http://x", "db": "d", "name": "ann-claude"},
                    {"participant_id": 1, "fence": 2, "name": "ann-claude"}, space=FakeSpace(t or tables()))


def test_wakes_for_messages_tasks_and_finished_handoffs_but_not_team_chat():
    work = dict(runner.pending_work(hub(), set()))
    assert set(work) == {"m:m_steer", "m:m_ask", "t:t_mine", "d:t_gave:done"}
    assert "a person on your team" in work["m:m_steer"] and "make the lid 20 mm" in work["m:m_steer"]
    assert "another AI helper" in work["m:m_ask"]
    assert "bob-codex finished the task you handed out" in work["d:t_gave:done"] and "4 posts" in work["d:t_gave:done"]


def test_each_thing_wakes_it_once_and_handled_messages_never():
    t = tables()
    t["v_receipt"] = [{"message_id": "m_ask", "member": 1, "state": "consumed"}]
    seen = {"m:m_steer", "t:t_mine"} | runner.finished_keys(hub(t))
    assert runner.pending_work(hub(t), seen) == []


def test_a_free_helper_wakes_for_a_busy_helpers_queue_once():
    t = tables()
    t["v_task"] = [task("t_now", "base", assignee="bob-codex", owner=2, status="claimed"),
                   task("t_next", "lid", assignee="bob-codex"),
                   task("t_free", "clip", assignee="carol")]  # carol isn't busy: she'll start it herself
    work = dict(runner.pending_work(hub(t), set()))
    assert set(work) == {"m:m_steer", "m:m_ask", "s:t_next"}
    assert "bob-codex is busy" in work["s:t_next"]
    assert "s:t_next" not in dict(runner.pending_work(hub(t), {"s:t_next"}))


def test_a_helper_with_its_own_work_or_held_parts_isnt_offered_spare_work():
    t = tables()
    t["v_task"] = [task("t_now", "base", assignee="bob-codex", owner=2, status="claimed"),
                   task("t_next", "lid", assignee="bob-codex"), task("t_mine", "clip", assignee="ann-claude")]
    assert "s:t_next" not in dict(runner.pending_work(hub(t), set()))
    t["v_task"] = t["v_task"][:2]
    t["v_task"][1]["objects"] = ["Lid"]
    t["v_lock"] = [{"object": "Lid", "holder": 2}]
    assert "s:t_next" not in dict(runner.pending_work(hub(t), set()))


def make_runner(tmp_path, client="claude", t=None):
    folder = tmp_path / "helper"
    (folder / ".cadswarm").mkdir(parents=True)
    (folder / ".cadswarm" / "config.json").write_text(json.dumps(
        {"uri": "http://x", "db": "d", "name": "ann-claude", "client": client}))
    (folder / ".cadswarm" / "identity.json").write_text(json.dumps({"participant_id": 1, "fence": 2, "token": "t"}))
    clock = [1000.0]
    spawned = []

    class Child:
        returncode = 0

        def poll(self):
            return 0

        def kill(self):
            pass

    r = runner.Runner(str(folder), "Phone stand", clock=lambda: clock[0],
                      spawn=lambda argv, stdin: spawned.append((argv, stdin)) or Child())
    r.hub = hub(t)
    return r, clock, spawned


def test_an_idle_poll_is_one_cheap_read_and_news_gets_a_full_look(tmp_path, monkeypatch):
    """A poll used to be ~10 round trips, ~3 s, so a message could sit for 6 s before it was even seen."""
    monkeypatch.setattr(runner, "find_agent_cli", lambda c: f"/bin/{c}")
    calm = tables()
    calm["v_message"] = []
    calm["v_task"] = []
    calm["v_receipt"] = []
    calm["v_event"] = [{"seq": 5, "kind": "op", "actor": 2, "at_ms": 1, "data_json": "{}"}]
    calm["v_event_head"] = [{"ws": 1, "seq": 5}]
    r, clock, spawned = make_runner(tmp_path, t=calm)
    r.state["started"] = True  # don't wake it just to say hello
    r.save_state()
    queries = []
    r.hub.space.on_sql = queries.append

    r.step()  # the first poll looks properly and sets the event cursor
    assert spawned == [] and r.seq == 5
    queries.clear()

    clock[0] += 1
    r.step()  # nothing happened: only the one-row event head is read, never the history
    assert queries == ["SELECT * FROM v_event_head"], queries
    assert spawned == []

    # A message arrives (which the module records as an event): the full look wakes the helper.
    r.hub.space.tables["v_event"].append({"seq": 6, "kind": "message", "actor": 3, "at_ms": 2, "data_json": "{}"})
    r.hub.space.tables["v_event_head"] = [{"ws": 1, "seq": 6}]
    r.hub.space.tables["v_message"].append(msg("m_new", 3, "member:1", "steer", "now the base"))
    clock[0] += 1
    r.step()
    assert len(spawned) == 1 and "now the base" in spawned[0][1]
    assert r.seq == 6


def test_a_full_look_also_happens_now_and_then_in_case_work_raised_no_event(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "find_agent_cli", lambda c: f"/bin/{c}")
    calm = tables()
    calm["v_task"] = []
    calm["v_message"] = []
    calm["v_receipt"] = []
    r, clock, spawned = make_runner(tmp_path, t=calm)
    r.state["started"] = True
    r.save_state()
    queries = []
    r.hub.space.on_sql = queries.append
    r.step()
    queries.clear()
    clock[0] += runner.FULL_CHECK_SECONDS
    r.step()
    assert len(queries) > 1, "an idle poll every few seconds still reads the tables"


def test_a_step_rejoins_heartbeats_and_wakes_the_agent_with_whats_new(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "find_agent_cli", lambda c: f"/bin/{c}")
    r, clock, spawned = make_runner(tmp_path)
    assert r.step()
    calls = [c[0] for c in r.hub.space.calls]
    assert calls[:2] == ["rejoin", "heartbeat"]  # present as "ready" while no agent runs
    (argv, prompt), = spawned
    assert argv[:2] == ["/bin/claude", "-p"] and "--continue" not in argv  # the first run starts a conversation
    assert "make the lid 20 mm" in prompt and "Phone stand" in prompt
    assert "Say hello" not in prompt  # the runner greets instead, so the model spends no turn on it
    assert "never ask questions here" in prompt
    # The runner itself says hello, as a chat to "all" (peers ignore it, so it wakes nobody).
    # send_message args are (fence, id, to, body, kind, task_id).
    sends = [args for reducer, args in r.hub.space.calls if reducer == "send_message"]
    assert sends and sends[-1][2] == "all" and sends[-1][4] == "chat" and "ann-claude" in sends[-1][3]
    status = json.load(open(os.path.join(r.folder, ".cadswarm", "runner.json")))
    assert status["state"] == "working"
    # The agent exits; nothing new arrived: the runner rejoins and listens, without waking it again.
    clock[0] += 60
    assert r.step() and r.step()
    assert len(spawned) == 1
    assert json.load(open(os.path.join(r.folder, ".cadswarm", "runner.json")))["state"] == "listening"
    # Something new: a second run continues the same conversation.
    r.hub.space.tables["v_message"].append(msg("m_new", 3, "member:1", "steer", "now the base"))
    clock[0] += 60
    r.step()
    assert len(spawned) == 2 and "--continue" in spawned[1][0] and "now the base" in spawned[1][1]
    assert len([1 for reducer, _ in r.hub.space.calls if reducer == "send_message"]) == 1  # greeted only once


def test_runs_are_rate_limited_so_helpers_cant_wake_each_other_forever(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "find_agent_cli", lambda c: f"/bin/{c}")
    r, clock, spawned = make_runner(tmp_path)
    r.runs = [clock[0] - 10 * i for i in range(runner.MAX_RUNS_PER_HOUR)]
    r.state["started"] = True
    r.step()
    assert spawned == []
    assert "paused" in json.load(open(os.path.join(r.folder, ".cadswarm", "runner.json")))["state"]


def test_a_missing_cli_is_reported_not_retried_in_a_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "find_agent_cli", lambda c: None)
    r, _clock, spawned = make_runner(tmp_path, client="codex")
    assert r.step()
    assert spawned == []
    st = json.load(open(os.path.join(r.folder, ".cadswarm", "runner.json")))
    assert st["state"] == "missing" and "codex" in st["detail"]


def test_a_helper_removed_from_the_team_stops_its_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "find_agent_cli", lambda c: f"/bin/{c}")
    r, _clock, _spawned = make_runner(tmp_path)
    r.hub.space.fail["rejoin"] = "not_member: gone"
    assert r.step() is False


def test_the_stop_file_ends_the_runner(tmp_path):
    r, _clock, _spawned = make_runner(tmp_path)
    runner.stop_runner(r.folder)
    assert r.step() is False


def test_each_helper_cli_gets_a_headless_command_with_the_prompt_on_stdin_where_it_can():
    argv, stdin = runner.agent_command("claude", "claude", "/h", "do it\nnow", resume=True)
    assert argv[:2] == ["claude", "--continue"] and "-p" in argv and "mcp__cadswarm" in argv and stdin == "do it\nnow"
    argv, stdin = runner.agent_command("codex", "codex", "/h", "do it", resume=False)
    assert argv[-1] == "-" and stdin == "do it"
    argv, stdin = runner.agent_command("opencode", "opencode", "/h", "do it\nnow", resume=True)
    assert argv[:4] == ["opencode", "run", "--standalone", "--auto"] and "--continue" in argv
    assert stdin == "do it\nnow" and not any("do it" in a for a in argv)


def test_codex_gets_the_sandbox_flag_its_own_cli_documents():
    # Codex removed --full-auto: passing it exits 2 with "unexpected argument", so every helper wake died.
    # `-s workspace-write` is the sandbox --full-auto meant; older builds that predate it still need it.
    assert runner.codex_exec_flags("Usage: codex exec [OPTIONS] [PROMPT]\n  -s, --sandbox <MODE>") == \
        ["--sandbox", "workspace-write"]
    assert runner.codex_exec_flags("Usage: codex exec [OPTIONS]\n  --full-auto") == ["--full-auto"]
    assert runner.codex_exec_flags("") == ["--sandbox", "workspace-write"]  # can't ask: current spelling


def test_the_codex_command_asks_the_cli_once_for_its_flags(monkeypatch):
    calls = []

    def fake_help(exe):
        calls.append(exe)
        return "Usage: codex exec [OPTIONS]\n  -s, --sandbox <MODE>"

    monkeypatch.setattr(runner, "codex_help", fake_help)
    monkeypatch.setattr(runner, "_CODEX_FLAGS", {})
    argv, _stdin = runner.agent_command("codex", "codex", "/h", "x", resume=False)
    assert "--full-auto" not in argv and argv[argv.index("--sandbox") + 1] == "workspace-write"
    runner.agent_command("codex", "codex", "/h", "x", resume=False)
    assert calls == ["codex"]  # one probe per binary, not per wake


def test_a_failed_run_says_what_the_cli_printed(tmp_path):
    r, _clock, _spawned = make_runner(tmp_path, client="codex")
    log = os.path.join(r.folder, ".cadswarm", "helper.log")
    with open(log, "w", encoding="utf-8") as f:
        f.write("2026-10-27 10:54:55 waking the helper for 3 new thing(s)\n")

    class Failed:
        returncode = 2

        def poll(self):
            return 2

    r.child_log_at = os.path.getsize(log)
    with open(log, "a", encoding="utf-8") as f:  # a banner, then codex's reason, then tips and usage
        f.write("> build · gpt-5\n"
                "\x1b[31merror: unexpected argument '--full-auto' found\x1b[0m\n\ntip: ...\nUsage: codex exec\n")
    r.child = Failed()
    assert r.child_running() is False
    assert "exit 2" in r.last_error
    assert "unexpected argument '--full-auto' found" in r.last_error
    assert "> build" not in r.last_error and "\x1b" not in r.last_error


def test_a_codex_wake_repairs_an_old_helpers_missing_mcp_approval(tmp_path, monkeypatch):
    """Helpers set up before this fix have the server but no approval default: every call was refused."""
    monkeypatch.setattr(runner, "find_agent_cli", lambda c: f"/bin/{c}")
    r, _clock, spawned = make_runner(tmp_path, client="codex")
    cfg = os.path.join(r.folder, ".codex", "config.toml")
    os.makedirs(os.path.dirname(cfg))
    with open(cfg, "w", encoding="utf-8") as f:
        f.write("[mcp_servers.cadswarm]\ncommand = 'x'\n")
    assert r.wake([("m:x", "- hi")], first=True)
    assert spawned
    with open(cfg, encoding="utf-8") as f:
        assert "default_tools_approval_mode = 'approve'" in f.read()


def test_a_blocked_codex_run_is_reported_even_though_it_exits_zero(tmp_path):
    """Codex refusing every MCP call leaves exit 0: the panel showed "Ready" while nothing happened."""
    calm = tables()
    calm["v_message"], calm["v_task"] = [], []
    r, _clock, _spawned = make_runner(tmp_path, client="codex", t=calm)
    log = os.path.join(r.folder, ".cadswarm", "helper.log")
    with open(log, "w", encoding="utf-8") as f:
        f.write("2026-10-27 17:13:28 waking the helper for 2 new thing(s)\n")

    class Refused:
        returncode = 0

        def poll(self):
            return 0

    r.child_log_at = os.path.getsize(log)
    with open(log, "a", encoding="utf-8") as f:
        f.write("mcp: cadswarm/list_tasks started\n"
                "MCP tool call requires approval, but approval policy is never\n"
                "I could not reach the team.\n")
    r.child = Refused()
    assert r.child_running() is False
    assert "approval" in r.last_error and "exit" not in r.last_error
    r.state["started"] = True  # don't wake it just to say hello; the next step only reports the failure
    r.save_state()
    assert r.step()
    status = json.load(open(os.path.join(r.folder, ".cadswarm", "runner.json")))
    assert status["state"] == "error" and "approval" in status["detail"]


def test_a_helpers_picked_model_beats_the_environment(monkeypatch):
    monkeypatch.setenv("CADSWARM_HELPER_MODEL", "env/model")
    argv, _ = runner.agent_command("opencode", "opencode", "/h", "x", resume=False, model="anthropic/claude-sonnet-5-5")
    assert argv[argv.index("--model") + 1] == "anthropic/claude-sonnet-5-5"
    argv, _ = runner.agent_command("opencode", "opencode", "/h", "x", resume=False)
    assert argv[argv.index("--model") + 1] == "env/model"


def test_the_runner_wakes_opencode_with_the_folders_model(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "find_agent_cli", lambda c: f"/bin/{c}")
    monkeypatch.delenv("CADSWARM_HELPER_MODEL", raising=False)
    r, _clock, spawned = make_runner(tmp_path, client="opencode")
    r.cfg["model"] = "openai/gpt-5.1"
    assert r.wake([("m:x", "- hi")], first=True)
    argv = spawned[-1][0]
    assert argv[argv.index("--model") + 1] == "openai/gpt-5.1"


def test_a_newer_runner_takes_the_folder_so_the_old_one_stops(tmp_path, monkeypatch):
    """Restart clicked twice (or two starts racing) used to leave two or three runners on one helper,
    each polling the team with its own warm server. The later process takes the folder now."""
    monkeypatch.setattr(runner, "find_agent_cli", lambda c: f"/bin/{c}")
    r, clock, spawned = make_runner(tmp_path)
    assert r.step() is True
    before = len(spawned)
    runner.save_json(runner.claim_path(r.folder), {"pid": 999999, "started": clock[0] + 5})
    assert r.step() is False
    assert len(spawned) == before  # it stopped instead of waking the helper again


def test_an_older_claim_never_takes_the_folder_from_the_runner_that_has_it(tmp_path):
    """A same-instant tie (or a stale claim from an older process) is broken by pid, the same way every
    time, so two runners can never both decide they own the folder."""
    r, clock, _spawned = make_runner(tmp_path)
    assert r.step() is True
    runner.save_json(runner.claim_path(r.folder), {"pid": max(1, os.getpid() - 1), "started": clock[0]})
    assert r.step() is True
    assert json.load(open(runner.claim_path(r.folder)))["pid"] == os.getpid()


def test_runner_alive_follows_its_heartbeat_file(tmp_path):
    (tmp_path / ".cadswarm").mkdir()
    assert not runner.runner_alive(str(tmp_path))
    (tmp_path / ".cadswarm" / "runner.json").write_text(json.dumps({"beat": 1000.0, "pid": 5}))
    assert runner.runner_alive(str(tmp_path), now=1010.0)
    assert not runner.runner_alive(str(tmp_path), now=1000.0 + runner.ALIVE_SECONDS + 1)


def test_panel_snapshot_reads_each_view_once():
    t = tables()
    space = FakeSpace(t)
    seen = []
    space.on_sql = seen.append
    h = SpaceHub({"uri": "http://x", "db": "d", "name": "ann-claude"},
                 {"participant_id": 1, "fence": 2, "name": "ann-claude"}, space=space)
    snap = h.panel_snapshot()
    assert set(snap) == {"model", "tasks", "commands", "briefs", "messages", "reviews", "workspace"}
    assert len(seen) == len(set(seen))  # no view read twice in one refresh
    assert [m["id"] for m in snap["messages"]] == ["m_steer", "m_ask", "m_chat", "m_bob"]
    h.tasks()
    assert len(seen) > len(set(seen))  # outside a snapshot, reads are fresh again


def test_a_run_that_ends_with_its_own_task_claimed_gets_another_go(tmp_path, monkeypatch):
    """A timeout or a swallowed turn used to leave the task "claimed" until a person noticed: the runner
    now wakes the helper for its own held task, with a doubling backoff."""
    monkeypatch.setattr(runner, "find_agent_cli", lambda c: f"/bin/{c}")
    t = tables()
    t["v_message"], t["v_receipt"] = [], []
    t["v_task"] = [task("t_gave", "standoffs", created_by=1, owner=2, status="done",
                        result_json='{"summary": "4 posts"}'),
                   task("t_held", "prop blades", owner=1, status="claimed")]
    r, clock, spawned = make_runner(tmp_path, t=t)
    r.state["started"] = True
    r.save_state()

    clock[0] += 1
    r.step()  # nothing new and the backoff hasn't passed: no task read, no wake
    assert spawned == []

    clock[0] += runner.RESUME_AFTER
    r.step()
    (argv, prompt), = spawned
    assert "t_held" in prompt and "Finish it now" in prompt

    # The run ends and the task is still claimed: the next nudge waits longer (90 s -> 180 s), so it
    # doesn't spin, and a reported task stops the nudging altogether.
    r.step()
    clock[0] += runner.RESUME_AFTER
    r.step()
    assert len(spawned) == 1
    t["v_task"][1].update(status="done", result_json='{"summary": "blades done"}')
    clock[0] += 10 * runner.RESUME_AFTER
    r.step()
    assert len(spawned) == 1 and r.resumes == {}


def test_a_handoff_does_not_wake_a_helper_that_still_holds_its_own_task():
    """Dead workers' work is broadcast to everyone; a helper that already holds a claimed task keeps it
    instead of spending a turn on every handoff."""
    t = tables()
    t["v_message"] = [msg("m_hand", 0, "all", "handoff", "bob-codex went quiet holding [] and tasks [x]")]
    t["v_task"] = [task("t_held", "prop blades", owner=1, status="claimed")]
    assert runner.pending_work(hub(t), set()) == []
    t["v_task"] = []  # free again: now it is work this helper can take
    assert "m:m_hand" in dict(runner.pending_work(hub(t), set()))



def test_a_failed_run_is_reported_until_one_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "find_agent_cli", lambda c: f"/bin/{c}")
    r, clock, spawned = make_runner(tmp_path)

    class Failed:
        returncode = 1

        def poll(self):
            return 1
    r.spawn = lambda argv, stdin: spawned.append(argv) or Failed()
    r.step()
    clock[0] += 60
    r.step()
    st = json.load(open(os.path.join(r.folder, ".cadswarm", "runner.json")))
    assert st["state"] == "error" and "exit 1" in st["detail"], st

    class Fine:
        returncode = 0

        def poll(self):
            return 0
    r.seen.clear()  # the fake hub still holds those messages: wake once more, and this run works
    clock[0] += 60
    r.spawn = lambda argv, stdin: spawned.append(argv) or Fine()
    r.step()
    clock[0] += 60
    r.step()
    st = json.load(open(os.path.join(r.folder, ".cadswarm", "runner.json")))
    assert st["state"] == "listening" and st["detail"] == "", st


def test_background_starts_can_be_switched_off_for_a_terminal_session(tmp_path):
    (tmp_path / ".cadswarm").mkdir()
    assert runner.background_on(str(tmp_path))
    runner.set_background(str(tmp_path), False)
    assert not runner.background_on(str(tmp_path))
    runner.set_background(str(tmp_path), True)
    assert runner.background_on(str(tmp_path))


def make_warm_runner(tmp_path, t=None, ready=True, client="opencode"):
    folder = tmp_path / "helper"
    (folder / ".cadswarm").mkdir(parents=True)
    (folder / ".cadswarm" / "config.json").write_text(json.dumps(
        {"uri": "http://x", "db": "d", "name": "ann-opencode", "client": client}))
    (folder / ".cadswarm" / "identity.json").write_text(json.dumps({"participant_id": 1, "fence": 2, "token": "t"}))
    clock = [1000.0]
    spawned, killed = [], []
    envs = []
    health = [ready]

    class Server:
        pid = 4242
        returncode = None

        def poll(self):
            return None

        def kill(self):
            killed.append(self)

    class Run:
        returncode = 0

        def poll(self):
            return 0

        def kill(self):
            killed.append(self)

    def spawn(argv, stdin):
        spawned.append((argv, stdin))
        envs.append(dict(r.child_env))
        if len(argv) > 1 and argv[1] == "run":
            # The run's MCP server joins at a new fence and takes the seat (as the real one does).
            (folder / ".cadswarm" / "mcp-seat.json").write_text(json.dumps({"pid": 999, "fence": 2, "at": 1}))
            return Run()
        return Server() if len(argv) > 1 and argv[1] == "serve" else Run()

    r = runner.Runner(str(folder), "Phone stand", clock=lambda: clock[0], spawn=spawn)
    r.hub = hub(t)
    r.spawn_envs = envs
    r.state["started"] = True  # a helper that already introduced itself
    r.ready = lambda url, password: health[0]
    r.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    return r, clock, spawned, killed, health


def test_a_warm_opencode_server_starts_once_and_wakes_use_it(tmp_path, monkeypatch):
    """One private `opencode serve` per helper: the CLI, its provider connection and the team's MCP server
    start once, so each later message skips ~5 s of startup (a wake went from ~3 s to ~0.9 s measured)."""
    monkeypatch.setattr(runner, "find_agent_cli", lambda c: f"/bin/{c}")
    r, clock, spawned, _killed, _health = make_warm_runner(tmp_path)
    assert r.step()  # the server comes up and the waiting message is handled
    serves = [a for a, _ in spawned if a[1] == "serve"]
    assert len(serves) == 1 and serves[0][2:] == ["--hostname", "127.0.0.1", "--port", str(r.server_port)]
    assert r.server_url.startswith("http://127.0.0.1:")
    argv = spawned[-1][0]
    assert argv[:2] == ["/bin/opencode", "run"]
    assert argv[argv.index("--server") + 1] == r.server_url and "--standalone" not in argv
    assert r.spawn_envs[-1]["OPENCODE_PASSWORD"] == r.server_password

    # The run ended: its MCP server holds the seat, so the runner heartbeats as it and never rejoins again
    # (a rejoin would supersede the server's fence and break its tool calls).
    clock[0] += 1
    r.step()
    calls = [c[0] for c in r.hub.space.calls]
    assert calls.count("rejoin") == 1 and calls[-1] == "heartbeat"

    r.hub.space.tables["v_message"].append(msg("m_warm", 3, "member:1", "steer", "now the base"))
    clock[0] += 1
    r.step()
    argv = spawned[-1][0]
    assert argv[:2] == ["/bin/opencode", "run"]
    assert argv[argv.index("--server") + 1] == r.server_url and "--standalone" not in argv
    assert len([a for a, _ in spawned if a[1] == "serve"]) == 1  # still just the one server
    assert [c[0] for c in r.hub.space.calls].count("rejoin") == 1

    # The next run ended; the next wake is another run against the same server.
    clock[0] += 60
    r.step()
    r.hub.space.tables["v_message"].append(msg("m_more", 3, "member:1", "steer", "and the lid"))
    clock[0] += 1
    r.step()
    assert len([a for a, _ in spawned if a[1] == "serve"]) == 1
    assert "--server" in spawned[-1][0]


def test_each_wake_falls_back_to_standalone_when_the_warm_server_will_not_start(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "find_agent_cli", lambda c: f"/bin/{c}")
    monkeypatch.setattr(runner, "WARM_READY_SECONDS", 0)
    r, _clock, spawned, killed, _health = make_warm_runner(tmp_path, ready=False)
    assert r.step()
    argv = spawned[-1][0]
    assert argv[:4] == ["/bin/opencode", "run", "--standalone", "--auto"]
    assert killed, "the server that never became ready is stopped"
    assert "warm" in r.warm_error.lower() or "server" in r.warm_error.lower()


def test_stopping_ends_the_warm_server_too(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "find_agent_cli", lambda c: f"/bin/{c}")
    r, _clock, _spawned, killed, _health = make_warm_runner(tmp_path)
    r.step()
    r.stop_all()
    assert killed and r.server_url == ""
    assert not os.path.exists(runner.server_path(r.folder))


def test_an_unpinned_helper_uses_the_configured_model_at_wake_time(tmp_path, monkeypatch):
    """A helper folder from before the picker has no model; the run must not fall back to a model the
    person never signed in to."""
    monkeypatch.setattr(runner, "find_agent_cli", lambda c: f"/bin/{c}")
    monkeypatch.setattr(runner, "default_model", lambda client: "bitdeer/deepseek-ai/DeepSeek-V4.1-Flash-1017")
    monkeypatch.delenv("CADSWARM_HELPER_MODEL", raising=False)
    r, _clock, spawned, _killed, _health = make_warm_runner(tmp_path)
    assert r.wake([("m:x", "- hi")], first=True)
    argv = spawned[-1][0]
    assert argv[argv.index("--model") + 1] == "bitdeer/deepseek-ai/DeepSeek-V4.1-Flash-1017"


def test_a_finished_warm_run_reports_idle_with_the_servers_fence(tmp_path, monkeypatch):
    """The warm server keeps heartbeating: without this it would say "working on Lid" until the next run."""
    monkeypatch.setattr(runner, "find_agent_cli", lambda c: f"/bin/{c}")
    r, clock, spawned, _killed, _health = make_warm_runner(tmp_path)
    r.state["started"] = True
    r.step()  # server + a run for the message in the fixture
    assert "serve" in spawned[0][0] and "--server" in spawned[-1][0]
    clock[0] += 1
    r.step()  # the run has exited: say listening again, through the fence the MCP server stored
    calls = [c for c in r.hub.space.calls]
    assert calls[-1][0] == "heartbeat" and calls[-1][1][1] == runner.IDLE_DOING
    assert r.hub.fence == 2


def test_opencode_can_be_woken_against_a_server_instead_of_its_own(tmp_path):
    argv, stdin = runner.agent_command("opencode", "opencode", "/h", "do it", resume=False,
                                       server="http://127.0.0.1:5000")
    assert argv[:2] == ["opencode", "run"] and "--standalone" not in argv
    assert argv[argv.index("--server") + 1] == "http://127.0.0.1:5000" and stdin == "do it"
