"""Keeps one AI helper on the team without anyone opening a terminal.

    python cadswarm_launch.py runner <helper folder>

FreeCAD starts this in the background for each AI helper set up on this computer. While the helper has
nothing to do, the runner keeps it on the team as "Ready for instructions". When a person messages it,
another helper asks it for something, a task is handed to it, or a task it handed out is finished, the
runner starts the agent CLI headless in the helper's folder (`claude -p`, `codex exec`,
`opencode run`) with a
short note of what's new. The agent does the work with the cadswarm MCP tools, answers in the team chat and
exits; the runner goes back to waiting.

One runner per folder: it writes .cadswarm/runner.json every few seconds, and a second runner exits while
that is fresh. Runs are rate limited, so two helpers can't keep waking each other.

Waiting is cheap on purpose: every second it reads one row (the newest team event) and only looks at what
the news actually is when something happened, or every few seconds anyway. A person's message reaching the
helper used to take up to ~6 s of polling; it is about a second now. What remains is the agent CLI starting
(~7 s) and the model's own time, not the team.

Stdlib only: it runs with FreeCAD's Python.
"""
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

if __package__ in (None, ""):  # run as a script: make `cadswarm` importable
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cadswarm.agent_setup import default_model, ensure_codex_approval, find_agent_cli  # noqa: E402
from cadswarm.client import HubRequestError, load_json, save_json, state_dir  # noqa: E402
from cadswarm.space_hub import POLL_VIEWS, SpaceHub  # noqa: E402

POLL_SECONDS = 1            # one poll is now a single cheap read, so news is noticed within about a second
FULL_CHECK_SECONDS = 5      # ...and every few seconds look properly for anything that raised no event
HEARTBEAT_SECONDS = 10
ALIVE_SECONDS = 30          # runner.json older than this means no runner is looking after the folder
RUN_TIMEOUT = 20 * 60       # one wake-up may take this long before it is stopped
MAX_RUNS_PER_HOUR = 30
MIN_GAP_SECONDS = 5
RESUME_AFTER = 90           # a run can end with the helper's own task still claimed: nudge it again this soon
RESUME_MAX = 5              # ...at most this many times per task, doubling the wait each time
LOG_LIMIT = 2_000_000
IDLE_DOING = "listening for instructions"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
GONE = ("not_member", "left", "taken_over", "removed", "bad_token")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
RUNNER_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ")
RUN_FAILED = re.compile(r"\b(error|failed|failure|invalid|denied|forbidden|unauthorized|unexpected argument)\b"
                        r"|not\s+(been\s+)?trusted|api key", re.IGNORECASE)
# Codex's own words when it refuses an MCP call it wanted a person to approve. A background run has nobody
# to ask, so every cadswarm tool fails and the run still exits 0: without this the panel says "Ready".
MCP_BLOCKED = re.compile(r"requires approval, but approval policy is never", re.IGNORECASE)
# A warm OpenCode server: start it once, then every message connects to it instead of paying the CLI's
# startup, config load and MCP boot again (~3-9 s). Measured: a trivial wake went from 3.3 s to 0.9 s, and
# the team's MCP server starts once for many runs rather than once per run.
WARM_READY_SECONDS = 12     # how long `serve` may take to answer before that wake falls back
WARM_POLL_SECONDS = 0.1
WARM_RETRY_SECONDS = 60     # after a failed start, keep working (slowly) and try the server again later




def status_path(folder):
    return os.path.join(state_dir(folder), "runner.json")


def claim_path(folder):
    """Which runner looks after `folder`. Written once when a runner starts, so the newest one owns the
    helper and any older copy stops itself (see Runner.other_runner)."""
    return os.path.join(state_dir(folder), "runner.claim.json")


def server_path(folder):
    """Where the runner remembers the warm OpenCode server it left running (a FreeCAD restart reuses it)."""
    return os.path.join(state_dir(folder), "opencode-server.json")


def seat_path(folder):
    """Written by the MCP server when it joins: it holds the helper's seat while a warm server outlives
    the run, so the runner must not rejoin (that would supersede its fence)."""
    return os.path.join(state_dir(folder), "mcp-seat.json")


def free_port():
    """A localhost port nobody is using, for one helper's private server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def server_ready(url, password, timeout=1.5):
    """True when the OpenCode server at `url` answers its health check."""
    req = urllib.request.Request(url.rstrip("/") + "/global/health",
                                 headers={"Authorization": f"Bearer {password}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def runner_alive(folder, now=None):
    """True while a runner looks after `folder` (it rewrites runner.json every few seconds)."""
    st = load_json(status_path(folder)) or {}
    return (now or time.time()) - float(st.get("beat") or 0) < ALIVE_SECONDS


def runner_status(folder):
    """{"state": "listening" | "working" | "stopped" | ..., "detail", "beat", "pid"} as the runner last wrote it."""
    st = load_json(status_path(folder)) or {}
    if not runner_alive(folder):
        st["state"] = "stopped"
    return st


def start_runner(folder, python, launcher, project=""):
    """Start the runner for `folder` quietly in the background (no window). Returns its PID, or None when one
    is already running."""
    if runner_alive(folder):
        return None
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH")}
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
              "cwd": folder, "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True  # keeps running after FreeCAD closes
    pid = subprocess.Popen([python, launcher, "runner", folder, project], **kwargs).pid
    # Claim the folder right away so a second click (or the next panel refresh) doesn't start another.
    # `started` lets the winner be decided when two did start: the newer process keeps the folder.
    save_json(status_path(folder), {"pid": pid, "beat": time.time(), "state": "starting", "detail": "",
                                    "started": time.time()})
    return pid


def off_path(folder):
    return os.path.join(state_dir(folder), "runner.off")


def set_background(folder, on):
    """Background starts on or off for `folder`. Off while someone runs the helper in a terminal: two copies
    would keep taking the helper's seat from each other."""
    if on:
        if os.path.exists(off_path(folder)):
            os.remove(off_path(folder))
    else:
        with open(off_path(folder), "w") as f:
            f.write("The helper runs in a terminal; FreeCAD won't start it in the background.\n")


def background_on(folder):
    return not os.path.exists(off_path(folder))


def stop_runner(folder):
    """Ask the runner to stop (it checks for the stop file between polls)."""
    with open(os.path.join(state_dir(folder), "runner.stop"), "w") as f:
        f.write("stop\n")


def _json(text, default):
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def pending_work(hub, seen):
    """What's new for this helper that it hasn't been woken for yet: [(key, line)]. `key` goes in `seen`
    once the helper is woken for it, so each thing wakes it once."""
    me, my_name = hub.me_id, (hub.identity.get("name") or "").lower()
    names = hub._names()
    kinds = {m["id"]: m["kind"] for m in hub._rows("v_member")}
    tasks = hub._rows("v_task")
    free = not any((t["status"] == "claimed" and t["owner"] == me)
                   or (t["status"] == "open" and (t.get("assignee") or "").lower() == my_name) for t in tasks)
    out = []
    for m in hub._unconsumed():
        if m["kind"] == "chat" and m["to"] == "all":
            continue  # people talking among themselves
        if m["kind"] == "handoff" and not free:
            continue  # a helper that still holds its own task keeps it: dead workers' work is for whoever is free
        key = f"m:{m['id']}"
        if key in seen:
            continue
        who = names.get(m["from"], "someone")
        if m["kind"] == "steer":
            what = f"{who} (a person on your team) asks you"
        elif m["kind"] == "handoff":
            what = f"{who} handed their work to you"
        elif kinds.get(m["from"]) == "human":
            what = f"{who} (a person) says to you"
        else:
            what = f"{who} (another AI helper) asks you"
        out.append((key, f"- {what}: \"{m['body'][:600]}\" (message {m['id']})"))
    for t in tasks:
        if t["status"] == "open" and (t.get("assignee") or "").lower() == my_name:
            key = f"t:{t['id']}"
            if key not in seen:
                out.append((key, f"- Task {t['id']} is waiting for you: \"{t['title']}\""))
        elif t["created_by"] == me and t["owner"] and t["owner"] != me and t["status"] in ("done", "blocked"):
            key = f"d:{t['id']}:{t['status']}"
            if key not in seen:
                summary = (_json(t.get("result_json"), {}) or {}).get("summary") or ""
                word = "finished" if t["status"] == "done" else "got stuck on"
                out.append((key, f"- {names.get(t['owner'], 'A teammate')} {word} the task you handed out, "
                                 f"\"{t['title']}\" ({t['id']}). {summary[:300]}".rstrip()))
    if free:  # rebalancing: a busy helper's queue is work this idle one can take
        for t in hub.spare_tasks():
            key = f"s:{t['id']}"
            if key not in seen:
                out.append((key, f"- {t['assignee']} is busy and has task {t['id']} queued: \"{t['title']}\". "
                                 "You're free, so you can pick it up."))
    return out


def finished_keys(hub):
    """Keys for handed-out tasks that were already finished before this runner started (nothing to wake for)."""
    return {f"d:{t['id']}:{t['status']}" for t in hub._rows("v_task")
            if t["created_by"] == hub.me_id and t["status"] in ("done", "blocked")}


def wake_prompt(name, project, lines, first):
    news = "\n".join(lines) if lines else "- Nothing specific yet."
    # The greeting is posted by the runner itself (Runner.greet), so the model spends no turn on it.
    start = ("Look at list_tasks first: if a task is open, unassigned and fits you, claim it and build it.\n"
             if first else "")
    mine = ('0. FIRST look at list_tasks for a task you already hold: status "claimed", owner is you, no result\n'
            '   yet. If there is one, finish it in this run before anything else: build it, check_interference, then\n'
            '   report_result (done, or blocked with the reason). Never leave your own task claimed and silent, and\n'
            '   never re-claim or re-announce it. Do not introduce yourself: the runner already did. If a CAD tool\n'
            '   times out, retry with a smaller script (one part or one blade per call) instead of stopping or\n'
            '   diagnosing this computer.\n')
    return f"""You are {name}, an AI helper on the team building "{project}" in one shared FreeCAD model.
You run in the background: nobody is watching a terminal, so never ask questions here and never wait for
input. Talk to people only with send_message; they read it as a chat in FreeCAD.

What's new for you:
{news}

What to do:
{mine}{start}1. Call get_inbox. Answer each person's message like a teammate in a chat: first a short reply saying
   what you'll do (send_message to them), then do it, then a short reply with what you did, in plain words
   with sizes in mm. Then ack_messages. If you need something from them (a size, a material), ask in one
   message and stop; their answer wakes you again.
2. A request from another AI helper: help if it doesn't clash with what a person asked, and answer them
   with send_message. You may say no and explain why.
3. Tasks waiting for you: claim_task, build, check_interference, report_result. A busy helper's queued task:
   pick_up_task (no task_id) takes the next one and tells them; if it says no_spare_work, someone was quicker.
4. Work that splits well can be shared: list_helpers shows who is free, and delegate_task hands one of them
   a task (it wakes them). When they finish, you're woken with the result.
When everything above is handled, stop. Don't call wait_for_changes: you're woken when something new arrives.
"""


def codex_exec_flags(help_text):
    """The flags that put `codex exec` in the workspace-write sandbox it used to call --full-auto.
    Codex removed --full-auto (the CLI now exits 2 with "unexpected argument"), so ask the installed
    build's own help what it takes: `-s workspace-write` on current ones (the same sandbox), --full-auto
    on older ones, and the current spelling when the help can't be read at all."""
    if not help_text or "--sandbox" in help_text:
        return ["--sandbox", "workspace-write"]
    if "--full-auto" in help_text:
        return ["--full-auto"]
    return []


def codex_help(exe):
    """`codex exec --help` text, or "" when the CLI cannot be asked."""
    try:
        out = subprocess.run([exe, "exec", "--help"], capture_output=True, encoding="utf-8",
                             errors="replace", timeout=15, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out.stdout or "") + (out.stderr or "")


_CODEX_FLAGS = {}  # exe -> ((mtime, size), flags): one probe per binary, not one per wake


def codex_flags(exe):
    """Cached `codex_exec_flags` for this binary, so an upgrade is noticed but a wake costs no process."""
    try:
        st = os.stat(exe)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    hit = _CODEX_FLAGS.get(exe)
    if hit and hit[0] == stamp:
        return hit[1]
    flags = codex_exec_flags(codex_help(exe))
    _CODEX_FLAGS[exe] = (stamp, flags)
    return flags


def agent_command(client, exe, folder, prompt, resume, model=None, server=None):
    """(argv, stdin text). The prompt goes in on stdin where the CLI accepts that: a .cmd launcher would
    mangle a multi-line argument. `model` is the folder's pinned model (agent_setup.save_helper_model).
    `server` is a warm OpenCode server URL: then the run connects to it instead of starting its own."""
    if client == "claude":
        argv = [exe] + (["--continue"] if resume else []) + [
            "-p", "--max-turns", "80", "--allowedTools", "mcp__cadswarm",
            "--mcp-config", os.path.join(folder, ".mcp.json"),
            # Sonnet handles the CAD tools well at a fraction of the cost of the largest model.
            "--model", os.environ.get("CADSWARM_HELPER_MODEL") or "sonnet"]
        return argv, prompt
    if client == "codex":
        return [exe, "exec", *codex_flags(exe), "--skip-git-repo-check", "-"], prompt
    if client == "opencode":
        # --standalone: a private server per run, not whatever the person's desktop app is running.
        # The prompt goes in on stdin, like claude and codex: an npm .cmd shim would split a
        # multi-line argument.
        argv = [exe, "run"] + (["--server", server] if server else ["--standalone"]) + ["--auto"]
        if resume:
            argv.append("--continue")
        model = model or os.environ.get("CADSWARM_HELPER_MODEL")
        if model:
            argv += ["--model", model]
        return argv, prompt
    raise ValueError(f"unknown AI helper {client!r}")


class Runner:
    def __init__(self, folder, project="", clock=time.time, spawn=None):
        self.folder = os.path.abspath(folder)
        self.project = project
        self.cfg = load_json(os.path.join(state_dir(self.folder), "config.json")) or {}
        if not self.cfg.get("db"):
            raise SystemExit(f"cadswarm runner: {self.folder} isn't connected to a team")
        self.ident_path = os.path.join(state_dir(self.folder), "identity.json")
        self.hub = SpaceHub(self.cfg, load_json(self.ident_path, {}), timeout=30)
        self.clock = clock
        self.spawn = spawn or self._spawn
        self.started = self.clock()  # this process's claim on the folder (see other_runner)
        self.child = None
        self.child_started = 0.0
        self.child_log_at = 0  # where the current run's own output starts in helper.log (see child_said)
        self.state = load_json(os.path.join(state_dir(self.folder), "runner-state.json")) or {}
        self.seen = set(self.state.get("seen") or [])
        self.runs = [t for t in self.state.get("runs") or [] if clock() - t < 3600]
        self.resumes = dict(self.state.get("resumes") or {})
        # A task still claimed after a run is resumed by the runner: the helper cannot be left waiting for a
        # person to notice. `last_run_end` starts at this runner's start, so a restart also resumes it.
        self.last_run_end = float(self.state.get("last_run_end") or 0) or clock()
        self.resume_check_at = self.last_run_end + RESUME_AFTER  # no task read on every idle poll
        self.present = False
        self.last_beat = 0.0
        self.seq = None  # event cursor: one cheap read tells us whether anything at all happened since
        self.last_full = 0.0  # when pending_work last looked properly
        self.status = ("starting", "")
        self.last_error = ""  # why the last run failed, shown in FreeCAD until a run succeeds
        # A warm OpenCode server (opencode: one per helper; off with "warm": false). Its MCP server holds
        # the helper's seat between runs, so the runner must not rejoin and bump the fence itself.
        self.warm = bool(self.cfg.get("warm", self.cfg.get("client") == "opencode"))
        self.server = None
        self.server_url = ""
        self.server_password = ""
        self.server_port = 0
        self.warm_error = ""  # set when the warm start failed: runs then start fresh, one per wake
        self.warm_retry_at = 0.0
        self.greeted = False
        self.ready = server_ready  # tests replace this instead of opening a socket
        self.sleep = time.sleep
        self.child_env = {}  # extra environment for the next spawn (the warm server's password)

    # ------------------------------------------------------------------ bookkeeping

    def log(self, text):
        path = os.path.join(state_dir(self.folder), "helper.log")
        try:
            if os.path.exists(path) and os.path.getsize(path) > LOG_LIMIT:
                os.replace(path, path + ".old")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
        except OSError:
            pass

    def save_state(self):
        self.state.update({"seen": sorted(self.seen)[-2000:], "runs": self.runs,
                           "resumes": self.resumes, "last_run_end": self.last_run_end})
        save_json(os.path.join(state_dir(self.folder), "runner-state.json"), self.state)

    def write_status(self, state=None, detail=None):
        if state is not None:
            self.status = (state, detail or "")
        save_json(status_path(self.folder), {"pid": os.getpid(), "beat": self.clock(),
                                             "state": self.status[0], "detail": self.status[1],
                                             "started": self.started})

    def other_runner(self, claim=True):
        """True when a newer runner owns this folder. Clicking Restart twice used to leave two or three
        runners on one helper, each polling the team with its own warm server: the later process takes the
        folder and the older ones stop themselves. `pid` breaks a same-instant tie.

        With `claim` this also (re)takes the folder when it is ours or older: the newest runner always ends
        up owning the claim file, whatever order two racing starts wrote it in."""
        st = load_json(claim_path(self.folder)) or {}
        mine = (self.started, os.getpid())
        theirs = (float(st.get("started") or 0), int(st.get("pid") or 0))
        if theirs > mine:
            return True
        if claim and (st.get("pid") != os.getpid() or st.get("started") != self.started):
            save_json(claim_path(self.folder), {"pid": os.getpid(), "started": self.started})
        return False

    # ------------------------------------------------------------------ warm server

    def env(self):
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH")}
        env["CADSWARM_BACKGROUND"] = "1"
        env.update(self.child_env)
        return env

    def server_up(self):
        """True while this helper's warm server is (as far as we know) alive. A server adopted from a
        previous runner has no handle here: its health is checked before the next wake."""
        if not self.server_url:
            return False
        if self.server is not None and self.server.poll() is not None:
            self.log("the warm AI server exited")
            self.server, self.server_url = None, ""
            return False
        return True

    def mcp_seat(self):
        """True when the MCP server in the warm OpenCode server has joined and holds the seat. Its fence
        is the newest one, so the runner only heartbeats (see mark_idle) and never rejoins."""
        if not self.server_url:
            return False
        st = load_json(seat_path(self.folder)) or {}
        ident = load_json(self.ident_path, {}) or {}
        return bool(st.get("fence")) and st.get("fence") == ident.get("fence")

    def save_server(self):
        save_json(server_path(self.folder), {"pid": getattr(self.server, "pid", 0) or 0,
                                             "url": self.server_url, "password": self.server_password,
                                             "port": self.server_port, "beat": self.clock()})

    def adopt_server(self):
        """Reuse the warm server an earlier runner left running, so restarting FreeCAD does not cost the
        helper its first-second start again."""
        st = load_json(server_path(self.folder)) or {}
        url, password = st.get("url") or "", st.get("password") or ""
        if not url or not password or not self.ready(url, password):
            return ""
        self.server_url, self.server_password = url, password
        self.server_port = int(st.get("port") or 0)
        return url

    def ensure_server(self, check=False):
        """The helper's private OpenCode server URL, starting it if needed. "" means run this wake fresh
        (`--standalone`): the warm path is an optimisation, never a reason a helper can't work."""
        if not self.warm:
            return ""
        if self.server_up() and (not check or self.ready(self.server_url, self.server_password)):
            return self.server_url
        if self.server_url:  # up on paper but not answering (or its process died)
            self.log("the warm AI server stopped answering")
            self.stop_server()
        if not self.server_url:
            url = self.adopt_server()
            if url:
                self.log(f"reusing the warm AI server at {url}")
                return url
        now = self.clock()
        if self.warm_retry_at and now < self.warm_retry_at:
            return ""
        try:
            os.remove(seat_path(self.folder))  # a seat left by a server that is gone
        except OSError:
            pass
        exe = find_agent_cli(self.cfg.get("client") or "claude")
        if not exe:
            return ""
        if not self.state.get("started") and not self.greeted:
            # Say hello before the server's MCP server takes the seat (after that our fence is stale).
            try:
                self.be_present()
                self.greet()
            except HubRequestError:
                pass
            self.greeted = True
        port, password = free_port(), secrets.token_urlsafe(24)
        self.server_port, self.server_password = port, password
        self.server_url = f"http://127.0.0.1:{port}"
        self.child_env = {"OPENCODE_PASSWORD": password}
        try:
            self.server = self.spawn(
                [exe, "serve", "--hostname", "127.0.0.1", "--port", str(port)], None)
        except OSError as e:
            self.server = None
            self.server_url = ""
            self.warm_error = (f"the warm AI server didn't start ({e}); each message starts the AI fresh, "
                               "a few seconds slower")
            self.log(self.warm_error)
            self.warm_retry_at = now + WARM_RETRY_SECONDS
            return ""
        finally:
            self.child_env = {}
        self.save_server()
        self.log(f"starting the warm AI server at {self.server_url}")
        deadline = now + WARM_READY_SECONDS
        while self.clock() < deadline:
            if self.ready(self.server_url, self.server_password):
                self.log("the warm AI server is ready")
                return self.server_url
            if self.server.poll() is not None:
                break
            self.sleep(WARM_POLL_SECONDS)
        self.warm_error = (f"the warm AI server didn't answer within {WARM_READY_SECONDS:.0f}s; each message "
                           "starts the AI fresh, a few seconds slower")
        self.log(self.warm_error)
        self.stop_server()
        self.warm_retry_at = now + WARM_RETRY_SECONDS
        return ""

    def stop_server(self):
        server = self.server
        pid = getattr(server, "pid", 0) or (load_json(server_path(self.folder)) or {}).get("pid") or 0
        self.server = None
        self.server_url = self.server_password = ""
        self.server_port = 0
        try:
            os.remove(server_path(self.folder))
        except OSError:
            pass
        try:
            os.remove(seat_path(self.folder))
        except OSError:
            pass
        if not pid:
            return
        if server is not None and not isinstance(server, subprocess.Popen):
            try:
                server.kill()  # a test double
            except OSError:
                pass
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=NO_WINDOW)
            else:
                os.kill(pid, 15)
        except (OSError, ProcessLookupError):
            pass

    def stop_all(self):
        """End the current run and the warm server (the MCP server goes with it)."""
        if self.child is not None:
            try:
                self.child.kill()
            except OSError:
                pass
            self.child = None
        self.stop_server()

    # -------------------------------------------------------------------- presence

    def be_present(self):
        """Hold the helper's seat while no agent runs: rejoin (a new fence, so a finished agent's leftover
        MCP server can't act) and heartbeat, so the team sees it as ready. A warm server's MCP server owns
        the seat once it has joined: rejoining here would make its fence stale and break every tool call."""
        if self.server_up() and self.mcp_seat():
            return
        now = self.clock()
        if not self.present:
            self.hub.join(client=self.cfg.get("client"))
            self.present, self.last_beat = True, 0.0
        if now - self.last_beat >= HEARTBEAT_SECONDS:
            self.hub._f("heartbeat", IDLE_DOING)
            self.last_beat = now

    def greet(self):
        """Say hello for the helper the moment it first starts, so people see who joined without the model
        spending a turn on it. Sent as chat to "all": peers ignore chat in pending_work, so it wakes nobody."""
        name = self.cfg.get("name") or "an AI helper"
        try:
            self.hub.send({"to": "all", "body": f"Hi, I'm {name}, ready to help.", "kind": "chat"})
        except HubRequestError:
            pass  # a greeting is a courtesy; never let a hiccup stop the run

    # ------------------------------------------------------------------------ runs

    def allowed(self):
        now = self.clock()
        self.runs = [t for t in self.runs if now - t < 3600]
        if len(self.runs) >= MAX_RUNS_PER_HOUR:
            return False, f"paused: it already ran {MAX_RUNS_PER_HOUR} times this hour"
        if self.runs and now - self.runs[-1] < MIN_GAP_SECONDS:
            return False, ""
        return True, ""

    def _spawn(self, argv, stdin_text):
        path = os.path.join(state_dir(self.folder), "helper.log")
        try:
            self.child_log_at = os.path.getsize(path)  # this run's output starts here
        except OSError:
            self.child_log_at = 0
        log = open(path, "a", encoding="utf-8")
        kwargs = {"cwd": self.folder, "env": self.env(), "stdout": log, "stderr": subprocess.STDOUT,
                  "stdin": subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = NO_WINDOW
        child = subprocess.Popen(argv, **kwargs)
        if stdin_text is not None:
            try:
                child.stdin.write(stdin_text.encode("utf-8"))
                child.stdin.close()
            except OSError:
                pass
        return child

    def wake(self, work, first):
        client = self.cfg.get("client") or "claude"
        exe = find_agent_cli(client)
        if not exe:
            self.write_status("missing", f"{client} isn't installed on this computer")
            return False
        if client == "codex" and ensure_codex_approval(self.folder):
            self.log("let Codex approve the cadswarm tools in .codex/config.toml: a background run can't "
                     "answer its approval prompt")
        prompt = wake_prompt(self.cfg.get("name") or "an AI helper", self.project or "your team's project",
                             [line for _, line in work], first)
        model = self.cfg.get("model") or (default_model(client) if client == "opencode" else "")
        server = self.ensure_server(check=True) if self.warm else ""
        argv, stdin_text = agent_command(client, exe, self.folder, prompt, resume=bool(self.state.get("started")),
                                         model=model or None, server=server or None)
        self.log(f"waking the helper for {len(work)} new thing(s): {', '.join(k for k, _ in work) or 'first start'}"
                 + (" (warm server)" if server else ""))
        if first and not self.greeted:
            self.greet()  # the runner introduces the helper, not the model
        self.child_env = {"OPENCODE_PASSWORD": self.server_password} if server else {}
        try:
            self.child = self.spawn(argv, stdin_text)
        finally:
            self.child_env = {}
        self.child_started = self.clock()
        self.present = False  # the agent's MCP server takes the seat (a newer fence)
        self.runs.append(self.clock())
        self.seen |= {k for k, _ in work}
        self.state["started"] = True
        self.save_state()
        self.write_status("working", work[0][1][2:120] if work else "introducing itself")
        return True

    def mark_idle(self):
        """A warm server's MCP server outlives the run and would keep saying "working on X" for minutes. The
        runner shares the folder's identity, so heartbeat "listening" with the fence the MCP just stored
        (a heartbeat, not a rejoin: rejoining would supersede the server's fence)."""
        if not (self.server_up() and self.mcp_seat()):
            return
        ident = load_json(self.ident_path, {}) or {}
        if not ident.get("fence"):
            return
        self.hub.identity.update(ident)
        try:
            self.hub._f("heartbeat", IDLE_DOING)
        except HubRequestError:
            return
        self.present, self.last_beat = True, self.clock()

    def run_output(self):
        """What this run itself printed so far (the log after `child_log_at`), ANSI codes stripped."""
        path = os.path.join(state_dir(self.folder), "helper.log")
        try:
            with open(path, "rb") as f:
                f.seek(self.child_log_at)
                text = f.read(65536).decode("utf-8", "replace")
        except OSError:
            return ""
        return ANSI.sub("", text)

    def child_said(self):
        """": <first line the run printed>", e.g. "error: unexpected argument '--full-auto' found", or "".
        A CLI that dies at startup writes its reason first and tips/usage after it, so the first line names
        the cause; the whole log is one click away in the Tools tab."""
        lines = [line.strip() for line in self.run_output().splitlines() if line.strip()]
        # The runner's own bookkeeping lines are timestamped; the CLI's reason is the first other line.
        lines = [line for line in lines if not RUNNER_LINE.match(line)]
        # A CLI may print a banner first ("> build · model"), so prefer a line that names a failure.
        said = next((line for line in lines if RUN_FAILED.search(line)), lines[0] if lines else "")
        return f": {said[:200]}" if said else ""

    def child_running(self):
        if self.child is None:
            return False
        if self.child.poll() is not None:
            code = self.child.returncode
            self.log(f"the helper finished (exit {code})")
            self.last_error = "" if code == 0 else (
                f"its last run failed (exit {code})" + self.child_said())
            if MCP_BLOCKED.search(self.run_output()):
                # Exit 0, but Codex refused every call: say so instead of showing a helper that looks Ready.
                self.last_error = ("Codex refused every CADSwarm tool call: it asks for approval and a "
                                   "background run can't answer. Update CADSwarm's Codex setup, then restart "
                                   "this helper.")
            self.child = None
            self.last_run_end = self.clock()
            self.save_state()
            self.mark_idle()
            return False
        if self.clock() - self.child_started > RUN_TIMEOUT:
            self.log("the helper ran too long and was stopped")
            self.child.kill()
            self.child = None
            self.last_run_end = self.clock()
            self.save_state()
            self.mark_idle()
            return False
        return True

    def owned_claimed(self):
        """Tasks this helper holds and hasn't reported: {id: title}. A run can end (a timeout, a swallowed
        turn) with these still claimed; nobody is watching, so the runner has to pick them back up."""
        try:
            rows = self.hub._rows("v_task")
        except HubRequestError:
            return {}
        me = self.hub.me_id
        held = {}
        for t in rows:
            if t.get("status") == "claimed" and t.get("owner") == me and not (t.get("result_json") or "").strip():
                held[t["id"]] = t.get("title") or t["id"]
        for tid in list(self.resumes):
            if tid not in held:  # reported, or taken over by someone else: stop nudging it
                del self.resumes[tid]
        return held

    def resume_work(self, now):
        """One nudge line per held task whose backoff has passed, else []. Reset when the task is reported."""
        out = []
        for tid, title in self.owned_claimed().items():
            st = self.resumes.get(tid) or {"n": 0, "at": self.last_run_end or now}
            wait = RESUME_AFTER * (2 ** st["n"])
            if now - st["at"] < wait or st["n"] >= RESUME_MAX:
                continue
            st["n"] += 1
            st["at"] = now
            self.resumes[tid] = st
            out.append((f"r:{tid}:{st['n']}",
                        f"- You still hold task {tid} \"{title}\" (claimed by you, no result yet). Finish it now: "
                        "build it, check_interference, report_result. A CAD tool that timed out: retry it with a "
                        "smaller script (one part or one blade per call) instead of stopping."))
        return out

    # ------------------------------------------------------------------------ loop

    def step(self):
        """One poll. Returns False when the runner should exit."""
        if os.path.exists(os.path.join(state_dir(self.folder), "runner.stop")):
            os.remove(os.path.join(state_dir(self.folder), "runner.stop"))
            self.stop_all()
            self.log("stopped from FreeCAD")
            return False
        if self.other_runner():
            # A newer runner claimed the folder (Restart clicked twice, or two starts raced): this one
            # must not keep polling the team beside it, with its own warm server.
            self.stop_all()
            self.log("a newer runner took this folder; stopping")
            return False
        if self.child_running():
            self.write_status()
            return True
        try:
            if self.warm and not self.server_up():
                self.ensure_server()  # the server holds the seat: start it before joining ourselves
            self.be_present()
            if "primed" not in self.state:
                self.seen |= finished_keys(self.hub)
                self.state["primed"] = True
                self.save_state()
            now = self.clock()
            # One cheap read tells us whether anything happened at all; only then (and every few seconds
            # regardless, for work that raises no event) do we work out what it is. Before, every poll was
            # ~10 round trips: ~3 s of a 3 s poll, so a message could sit unread for 6 s.
            news = self.hub.latest_seq()
            if self.seq is not None and news <= self.seq and now - self.last_full < FULL_CHECK_SECONDS:
                work = []
            else:
                with self.hub.refresh(*POLL_VIEWS):
                    self.hub.identity["name"] = self.hub.me()["name"]  # a person may have renamed it
                    work = pending_work(self.hub, self.seen)
                # The cursor is where it was *before* the look, so anything that landed while we looked
                # still counts as news next time round.
                self.seq = news
                self.last_full = now
        except HubRequestError as e:
            if e.code in GONE:
                self.log(f"this helper is no longer on the team ({e.code}); stopping")
                self.write_status("gone", str(e))
                return False
            self.present = False
            self.write_status("offline", "can't reach the team right now; retrying")
            return True
        first = not self.state.get("started")
        if not work and not first and self.clock() >= self.resume_check_at and self.allowed()[0]:
            # Nobody is watching: a run that ended with the helper's own task still claimed gets another go,
            # with a backoff, instead of the task sitting "claimed" until a person notices.
            work = self.resume_work(self.clock())
            self.resume_check_at = self.clock() + RESUME_AFTER
        if work or first:
            ok, why = self.allowed()
            if ok:
                self.wake(work, first)
                return True
            if why:
                self.write_status("paused", why)
                return True
        self.write_status("error" if self.last_error else "listening", self.last_error)
        return True

    def run(self):
        if self.other_runner():
            self.log("a newer runner already looks after this folder; stopping")
            return
        self.write_status("starting")
        self.log(f"runner started for {self.cfg.get('name')} ({self.cfg.get('client')})")
        while self.step():
            time.sleep(POLL_SECONDS)
        self.stop_all()
        # A runner that stepped aside must not mark the folder stopped: the newer one is working in it.
        if not self.other_runner(claim=False) and os.path.exists(status_path(self.folder)):
            save_json(status_path(self.folder), {"pid": os.getpid(), "beat": 0, "state": "stopped", "detail": ""})


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        raise SystemExit("usage: cadswarm_launch.py runner <helper folder> [project name]")
    Runner(argv[0], argv[1] if len(argv) > 1 else "").run()


if __name__ == "__main__":
    main()
