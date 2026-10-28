"""CADSwarm workbench: a human's seat at the table.

* A live, read-only MIRROR document (named after the project) shows the hub's
  shared model, each part colored by whoever holds its lock, updated as agents work.
* A TEAM PANEL shows presence, locks, tasks and the session chat, and lets the
  human steer: message agents (human messages outrank peer requests in every
  agent's inbox), lock/release parts, force a takeover, create tasks, set
  shared Params, run an interference check and export STEP/STL.

Networking runs on background threads (event long-poll, heartbeats, BREP
fetches) that only ever push into a queue; a QTimer drains it on the GUI
thread, so FreeCAD's document and Qt widgets are touched from one thread only.
"""
import html
import json
import os
import queue
import re
import threading
import time
from urllib.parse import urlencode, urlsplit

import FreeCAD as App
import FreeCADGui as Gui
import Part
from PySide import QtCore, QtGui

from .. import hosting, project_cards, qr, ticket
from ..client import HubRequestError, load_json, save_json
from ..invite import InviteError, read_invite
from ..mentions import mention_prefix, parse_mentions, strip_mentions
from ..naming import MIRROR_DOC, is_mirror_doc, mirror_label
from ..paths import app_dir
from ..space_hub import SpaceHub
from ..teams import recent_teams, remember_team
from . import fea_ui, library_ui, lighten_ui, overlay, panes, projects_ui, style_ui, theme

PARAMS_PATH = "User parameter:BaseApp/Preferences/Mod/CadSwarm"
HEARTBEAT_SECONDS = 10
REFRESH_SECONDS = 3         # the panel re-reads the team this often (in the background) even with no events
HELPER_CHECK_SECONDS = 20   # how often to make sure this computer's AI helpers are running
TOAST_SECONDS = 6           # how long a confirmation ("✓ Created task …") stays in the panel
PREVIEW_SECONDS = 60        # how often a project's card picture ("Open a previous project") is refreshed
JOIN_TRIES = 3              # a server this computer just started can take a moment to answer
JOIN_RETRY_SECONDS = 2.0
FATAL = ("taken_over", "fence_stale", "left", "removed", "bad_token", "not_member")


def prefs():
    return App.ParamGet(PARAMS_PATH)


def identity_path(db):
    """This computer's sign-in for one team, kept outside every project folder (it is a secret)."""
    return os.path.join(app_dir(), "gui", re.sub(r"[^A-Za-z0-9_.-]+", "_", db or "team") + ".json")


def load_identity(db):
    return load_json(identity_path(db), {}) or {}


def save_identity(db, ident):
    save_json(identity_path(db), ident)


def host_dir_for(db):
    """The folder this computer hosts for `db`, or "" when it hosts another team (or none)."""
    folder = prefs().GetString("HostDataDir")
    if not folder or not db:
        return ""
    try:
        with open(os.path.join(folder, "host.json"), encoding="utf-8") as f:
            return folder if (json.load(f) or {}).get("db") == db else ""
    except (OSError, ValueError, TypeError):
        return ""


LOCAL_WEB_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def web_url_for_phone():
    """Where a phone should open the build. The host's own address wins, unless only this computer can
    reach it (`localhost`), which a phone never can; then the public site is the honest answer."""
    folder = host_dir_for(prefs().GetString("Db"))
    url = ((hosting.host_config(folder) or {}).get("web_url") or "") if folder else ""
    if not url or (urlsplit(url).hostname or "") in LOCAL_WEB_HOSTS:
        return hosting.DEFAULT_WEB_URL
    return url.rstrip("/")


# Distinct, colorblind-friendlier hues (Tableau 10), handed out in join order so two
# teammates never end up with near-identical colors the way hashed hues can.
PALETTE = [(0.12, 0.47, 0.71), (1.0, 0.50, 0.05), (0.17, 0.63, 0.17), (0.84, 0.15, 0.16), (0.58, 0.40, 0.74),
           (0.55, 0.34, 0.29), (0.89, 0.47, 0.76), (0.09, 0.75, 0.81), (0.74, 0.74, 0.13), (0.50, 0.50, 0.50)]
ASSIGNED = {}


def owner_color(owner_id):
    if not owner_id:
        return (0.80, 0.80, 0.80)  # unlocked: neutral grey
    if owner_id not in ASSIGNED:
        ASSIGNED[owner_id] = PALETTE[len(ASSIGNED) % len(PALETTE)]
    return ASSIGNED[owner_id]


def say(msg):
    App.Console.PrintMessage(f"[CADSwarm] {msg}\n")
    # Most people never open FreeCAD's console: confirm it in the status bar and the panel too (GUI thread only).
    app = QtGui.QApplication.instance()
    if app is None or QtCore.QThread.currentThread() != app.thread():
        return
    try:
        Gui.getMainWindow().statusBar().showMessage(f"CADSwarm: {msg}", TOAST_SECONDS * 1000)
    except Exception:
        pass
    if SESSION.panel is not None:
        SESSION.panel.flash(msg)


def warn(msg):
    App.Console.PrintWarning(f"[CADSwarm] {msg}\n")


def friendly_error(e, hub=""):
    """What went wrong, in words a teammate can act on (never just 'not connected')."""
    code = getattr(e, "code", None)
    if code == "unreachable" or getattr(e, "status", None) == 0:
        return (f"Couldn't reach your team's server at {hub or 'that address'}.\n"
                "Check your internet connection, and that whoever is hosting the team has it running "
                "(CADSwarm ▸ Get started ▸ Reconnect).")
    if code in (404, 503) or getattr(e, "status", None) in (404, 503):
        if 503 in (code, getattr(e, "status", None)):
            return ("That project's database is paused on SpacetimeDB, so it can't answer yet.\n"
                    "On spacetimedb.com, open the database and click Start Database, then reconnect. "
                    "(Or start a new team on this computer: CADSwarm ▸ Get started.)")
        return ("That project's database isn't on this computer's team server any more — it was rebuilt "
                "from empty.\nYour files are still in the project folder, so start the team again "
                "(CADSwarm ▸ Get started) or open the project's model.FCStd.")
    return {
        "bad_invite": "That invite code has already been used or has expired. Ask your teammate for a new one.",
        "not_member": "Your saved sign-in isn't part of this team any more. Join again with a new invite code.",
        "taken_over": "A teammate took over your session, so you were signed out. Join again to keep working.",
        "fence_stale": "You joined from another FreeCAD window, so this one was signed out.",
        "left": "You left the team session.",
        "removed": "A person removed this helper from the team. It can only rejoin with a new invite.",
        "bad_token": "Your saved sign-in is no longer valid. Join again.",
        "name_in_use": "Someone with that name is already in the team. Join again with a different name "
                       "(letters, numbers, - and _ only).",
        "not_allowed": "Only whoever started the team can do that.",
        "host_only": "Only the computer hosting the team can change its storage limits.",
        "host_timeout": "The team's server didn't pick that up. Check that whoever is hosting the team has "
                        "CADSwarm running, then try again.",
    }.get(code, str(e))


def _not_there_yet(e):
    """A failure worth waiting a moment for: the server is starting, or paused, or the network blipped.
    A stale sign-in or a deleted team is not — retrying it would just fail again."""
    code = getattr(e, "code", None)
    status = getattr(e, "status", None)
    return code in ("unreachable", "host_timeout", 503) or status in (0, 503)


class Session:
    def __init__(self):
        self.client = None
        self.ident = None
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.seq = 0
        self.model = {"objects": [], "participants": [], "params": {}}
        self.tasks = []
        self.messages = []     # the team chat as this person sees it (people and AI helpers)
        self.wake = threading.Event()  # set to refresh the panel now instead of at the next tick
        self._helpers_checked = 0
        self.panel = None
        self.timer = None
        self.fea_dialogs = []  # open report popups (kept referenced so Qt doesn't drop them)
        self.commands = []     # instructions I sent to helpers, with sent/seen/done
        self.briefs = []       # requests handed to the whole team (the host plans and builds them)
        self.reviews = []      # the reviewer's rounds, newest first, findings as sentences
        # Local chat lines for things that are not messages (a brief handed to the team), so the person
        # sees what happened without a "sending..." row waiting for an echo that never comes.
        self.notices = []
        self.workspace = {"bed": [180.0, 180.0, 180.0], "bed_x_mm": 180.0, "bed_y_mm": 180.0, "bed_z_mm": 180.0}
        self.editing = {}      # part name -> its open EditPartDialog
        self.synced_pl = {}    # part name -> mirror Placement as the hub last sent it
        self.mirrored = {}     # part name -> version the live model showed at join (the mirror loop takes it on)
        self.project = ""      # the project name this session belongs to (names the live-model tab)
        self._moved_hint = 0
        self._framed = False   # the live model was fitted in view once (a refresh must not re-fit it)
        # What the panel's banner tells the user: connecting | disconnected | connected | offline | ended
        self.state, self.state_detail = "disconnected", ""
        self._joining = False    # a background rejoin is in flight (try_auto_rejoin)
        self.rejoin_error = ""   # why the last automatic rejoin failed ("Try again" in the banner)
        self._preview_at = 0.0   # when this project's card picture was last refreshed
        self._working = 0        # requests this session started and hasn't finished (holds buttons down)
        self._idle_waiters = []  # callbacks to run when the last one finishes

    # ------------------------------------------------------------ lifecycle

    @property
    def connected(self):
        return self.client is not None and not self.stop.is_set()

    def set_state(self, state, detail=""):
        self.state, self.state_detail = state, detail
        if self.panel:
            self.panel.show_state(self)

    def ensure_connected(self, purpose):
        """True if connected; otherwise walk the user through joining (purpose explains why)."""
        if self.connected:
            return True
        return connect_dialog(reason=f"To {purpose}, first join your team's session.")

    def try_auto_rejoin(self):
        """Opening the workbench should just work: first time -> the setup guide; a team this computer
        hosts -> its server is started again if it isn't running (after a reboot, say); otherwise
        quietly rejoin.

        Everything slow happens on a background thread — starting the server, joining, and reading a
        busy team (the live model alone can be 10 MB, and every view is a database round trip) — so
        FreeCAD's window keeps answering and the panel says what it is doing. Joining here used to
        freeze the window for tens of seconds, which Windows drew as "FreeCAD is not responding"."""
        p = prefs()
        uri, db = p.GetString("Uri"), p.GetString("Db")
        ident = load_identity(db) if db else {}
        if self.connected or self._joining:
            return
        from .wizard import run_wizard
        if not (uri and db and ident.get("token")):
            QtCore.QTimer.singleShot(300, run_wizard)
            return
        self._rejoin({"uri": uri, "db": db}, ident, p.GetString("Name"), p.GetString("ProjectName"))

    def _rejoin(self, cfg, identity, name, project):
        """Join and read the team off the GUI thread; drain() adopts the result (see _adopted).

        Everything here is network, files and processes: no widget and no document is touched, so the
        window keeps answering. The banner follows along ("Starting your team's server…", "Joining…")."""
        self._joining = True
        self.rejoin_error = ""
        self.set_state("connecting")
        if self.timer is None:  # nothing else starts it before a session exists: the result arrives here
            self.timer = QtCore.QTimer()
            self.timer.timeout.connect(self.drain)
        self.timer.start(250)
        events, host_dir = self.events, host_dir_for(cfg["db"])
        mode = prefs().GetString("HostMode") or "local"

        def go():
            try:
                if host_dir and not hosting.team_ready(host_dir):
                    # This computer hosts the team: start its server instead of sending the person
                    # through the setup guide (that used to be the only way to get back in).
                    events.put(("progress", "Starting your team\u2019s server\u2026"))
                    from ..paths import freecad_python
                    hosting.start_team(freecad_python(), host_dir, mode, cfg["db"])
                events.put(("progress", "Joining your team\u2026"))
                client, ident = self._join_retrying(cfg, identity, name, host_dir, events)
                events.put(("progress", "Loading the team\u2019s model\u2026"))
                breps, _gone, mirrored = client.mirror_changes({})
                snap = client.panel_snapshot()
            except Exception as e:  # a stopped server, a stale sign-in, a team that is too busy
                events.put(("rejoin", (cfg, None, None, project, None, None, None,
                                       friendly_error(e, cfg.get("uri")))))
                return
            events.put(("rejoin", (cfg, client, ident, project, breps, mirrored, snap, None)))

        threading.Thread(target=go, daemon=True, name="cadswarm-rejoin").start()

    def _join_retrying(self, cfg, identity, name, host_dir, events):
        """Join, waiting a moment for a server that is still coming up (one we just started, or one on
        the way back). Anything else (a stale sign-in, a team that does not exist) is reported at once."""
        for attempt in range(JOIN_TRIES):
            try:
                return self._join(cfg, identity, name, timeout=3, host_dir=host_dir)
            except Exception as e:  # noqa: BLE001 - only a "not there yet" failure is worth waiting for
                if attempt == JOIN_TRIES - 1 or not _not_there_yet(e):
                    raise
                events.put(("progress", "Waiting for your team\u2019s server\u2026 (try %d of %d)"
                            % (attempt + 2, JOIN_TRIES)))
                time.sleep(JOIN_RETRY_SECONDS)

    def _adopted(self, cfg, client, ident, project, breps, mirrored, snap, error):
        """The background rejoin came back: finish it here, where widgets and the document live."""
        self._joining = False
        if error:
            if self.timer:
                self.timer.stop()  # nothing to drain: no session was started
            self.rejoin_error = error  # the banner offers Try again instead of the whole setup guide
            self.set_state("disconnected", error)
            return
        self.rejoin_error = ""
        self._end_session()  # a previous session's threads must never act on this one
        self._adopt(cfg, client, ident, project)
        try:
            self.apply_breps(breps or {})
            self.mirrored = mirrored or {}  # what the model shows now: the mirror loop only fetches changes
            self.remove_objects([o.Name for o in self.mirror().Objects if o.Name not in self.mirrored])
            if snap:
                self.apply_snapshot(snap)
            self.frame_model()
        except Exception as e:
            warn(f"couldn't show the team's model: {e}")
            self.disconnect()
            return
        self._start_workers()
        # The view only draws once the event loop runs again: give it a moment, then keep the picture.
        QtCore.QTimer.singleShot(2500, lambda: self.save_project_preview(force=True))

    def _join(self, cfg, identity, name, timeout, host_dir=""):
        """The network half of joining: build the client and redeem this person's identity. It touches
        no Qt and no document, so it may run on a background thread."""
        client = SpaceHub({"uri": cfg["uri"], "db": cfg["db"], "name": name, "host_dir": host_dir},
                          dict(identity or {}), timeout=timeout)
        ident = client.join(client="freecad")  # a new fence: an older window can no longer act
        client.timeout = client.space.timeout = 30  # the quick rejoin timeout is only for the first call
        client.identity = ident
        return client, ident

    def connect(self, cfg, identity=None, name=None, timeout=30, project=None):
        """Join (or rejoin) the team in `cfg` {"uri", "db"} with this person's SpacetimeDB `identity`.

        The identity is saved under the app folder per team, so opening FreeCAD again just rejoins.
        This waits while it reads the team, so only call it where the person is already waiting on it
        (the setup guide); opening the workbench goes through try_auto_rejoin, which does it in the
        background so FreeCAD's window never stops answering."""
        p = prefs()
        name = name or (identity or {}).get("name") or p.GetString("Name")
        self._end_session()  # a previous session's threads must never act on this one
        client, ident = self._join(cfg, identity, name, timeout, host_dir_for(cfg["db"]))
        self._adopt(cfg, client, ident, project)
        try:
            self.full_refresh()
        except Exception:
            self.disconnect()  # don't stay "connected" to a session that failed to load
            raise
        self._start_workers()

    def _adopt(self, cfg, client, ident, project=None):
        """The GUI half of joining: keep the sign-in and name the live-model tab. Only the person's own
        window may see this (it writes preferences and touches the document's tab)."""
        p = prefs()
        self.ident = ident
        self.client = client
        p.SetString("Uri", cfg["uri"])
        p.SetString("Db", cfg["db"])
        p.SetString("Name", self.ident.get("name") or p.GetString("Name"))
        save_identity(cfg["db"], self.ident)
        # Note the team so it can be reopened later (this computer hosts it only if host_dir_for says so).
        hosted = host_dir_for(cfg["db"])
        self.project = project or p.GetString("ProjectName")
        remember_team(self.project, cfg["uri"], cfg["db"],
                      hosted, p.GetString("HostMode") if hosted else "")
        self.label_mirror()  # a live model left open from another team takes the new project's name
        # Fresh stop flag and queue per connection: a worker still blocked in an old long-poll can
        # only ever signal its own (already stopped) session. Workers never touch Qt; they only queue.
        self.stop, self.events = threading.Event(), queue.Queue()
        self.seq = self.ident.get("seq", 0)
        if self.timer is None:
            self.timer = QtCore.QTimer()
            self.timer.timeout.connect(self.drain)
        self.set_state("connected")

    def _start_workers(self):
        """Start the background loops once the team is on screen. They never touch Qt or the document;
        they only queue work for drain() (called by this session's timer on the GUI thread)."""
        wake, mirror_wake = threading.Event(), threading.Event()
        self.wake = wake
        threading.Thread(target=self._poll_loop, args=(self.stop, self.events, self._bg_client(), self.seq, mirror_wake),
                         daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, args=(self.stop, self.events, self._bg_client(), self.seq),
                         daemon=True).start()
        threading.Thread(target=self._refresh_loop, args=(self.stop, self.events, self._bg_client(), wake),
                         daemon=True).start()
        threading.Thread(target=self._mirror_loop,
                         args=(self.stop, self.events, self._bg_client(), mirror_wake, dict(self.mirrored)),
                         daemon=True).start()
        self.timer.start(250)
        say(f"Joined the team in {self.client.cfg['db']} as {self.ident.get('name')}")

    def participant(self, name):
        return next((p for p in self.model.get("participants", []) if p["name"] == name), None)

    def disconnect(self, reason=None):
        """Leave the session. `reason` set means it ended on us (takeover, other window)."""
        if not self.client:
            return
        client = self.client
        self._end_session()
        if reason is None:
            try:
                client.post("/leave")
            except HubRequestError:
                pass
        self.set_state("ended" if reason else "disconnected", reason or "You left the team session.")

    def _end_session(self):
        """Stop this session's workers (they exit on their own stop flag) and forget its client."""
        self.stop.set()
        self.client = None
        if self.timer:
            self.timer.stop()

    def _bg_client(self):
        return SpaceHub(dict(self.client.cfg), dict(self.client.identity), timeout=40)

    def _heartbeat_loop(self, stop, events, c, _seq):
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                c.post("/heartbeat", {"doing": "watching in FreeCAD"})
            except HubRequestError as e:
                if e.code in FATAL:
                    events.put(("fatal", friendly_error(e)))
                    return

    def _refresh_loop(self, stop, events, c, wake):
        """Re-read everything the panel shows, off the GUI thread: on every team event, after each action,
        and every few seconds anyway (a helper reading or finishing a message doesn't make an event)."""
        while not stop.is_set():
            try:
                snap = c.panel_snapshot()
            except HubRequestError as e:
                if e.code in FATAL:
                    return  # the poll and heartbeat loops report it
                snap = None
            if stop.is_set():
                return
            if snap is not None:
                events.put(("snapshot", snap))
            wake.wait(REFRESH_SECONDS)
            wake.clear()

    def request_refresh(self):
        self.wake.set()

    def notice(self, text):
        """Show a local line in the chat about something that is not a message (a brief handed to the team).

        It is never sent to the team: it only tells the person what this window just did."""
        self.notices.append({"at": time.time(), "text": text})
        del self.notices[:-NOTICES_KEPT]
        if self.panel is not None:
            self.panel.render_chat(self)

    def _mirror_loop(self, stop, events, c, wake, applied):
        """Keep the live model equal to the team's. Each pass fetches only the parts that differ from what this
        window last applied, so a change that made no event (the host re-syncs its document when it restarts) or a
        fetch that failed (host busy or offline) is caught on a later pass. Op events wake it early."""
        while True:
            wake.wait(REFRESH_SECONDS)
            wake.clear()
            if stop.is_set():
                return
            try:
                breps, gone, now = c.mirror_changes(applied)
            except HubRequestError as e:
                if e.code in FATAL:
                    return  # the poll and heartbeat loops report it
                continue
            if stop.is_set():
                return
            if breps:
                events.put(("brep", breps))
            if gone:
                events.put(("deleted", gone))
            applied = now

    def _poll_loop(self, stop, events, c, seq, mirror_wake):
        offline = False
        while not stop.is_set():
            try:
                r = c.get("/events", since=seq, wait=20)
            except HubRequestError as e:
                if e.code in FATAL:
                    events.put(("fatal", friendly_error(e)))
                    return
                if not offline:
                    offline = True
                    events.put(("offline", friendly_error(e, c.hub)))
                time.sleep(3)
                continue
            if offline:
                offline = False
                events.put(("online", None))
            if stop.is_set():
                return  # session ended while we waited: never report into a newer one
            if not r["events"]:
                continue
            seq = r["seq"]
            for ev in r["events"]:
                events.put(("event", ev))
            if any(ev["kind"] == "op" for ev in r["events"]):
                mirror_wake.set()  # the mirror loop fetches whatever changed
            events.put(("refresh", None))

    # ------------------------------------------------------- GUI-thread work

    def drain(self):
        refresh = False
        for _ in range(200):
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "brep":
                self.apply_breps(data)
            elif kind == "deleted":
                self.remove_objects(data)
            elif kind == "event":
                if self.panel:
                    self.panel.on_event(data)
                if data.get("kind") == "fea_report":
                    open_fea_report(data["folder"])
            elif kind == "refresh":
                refresh = True
            elif kind == "snapshot":
                self.apply_snapshot(data)
            elif kind == "suggestions":
                if self.panel:
                    self.panel.show_suggestions(data)
            elif kind == "done":
                self.finish_async(*data)
            elif kind == "offline":
                self.set_state("offline", data)
            elif kind == "online":
                self.set_state("connected")
                refresh = True
            elif kind == "fatal":
                warn(data)
                self.disconnect(reason=data)
            elif kind == "rejoin":
                # Adopt after this pass: _adopt swaps this queue and this dispatch's timer.
                QtCore.QTimer.singleShot(0, lambda d=data: self._adopted(*d))
            elif kind == "progress":
                if self.state == "connecting":
                    self.set_state("connecting", data)  # "Starting your team's server…", "Joining…"
        if refresh:
            self.request_refresh()
        self.guard_local_moves()
        if self.connected:
            self.save_project_preview()  # at most every PREVIEW_SECONDS; the project list shows it

    def guard_local_moves(self):
        """The live model shows the team's parts. A part moved by hand without Edit would silently differ
        from what everyone else sees, so put it back and say how to change it properly."""
        doc = mirror_document()
        if doc is None or Gui.Control.activeDialog():
            return  # a Placement/Transform dialog is open: judge the move once it's closed
        moved = []
        for name, pl in self.synced_pl.items():
            if name in self.editing:
                continue
            obj = doc.getObject(name)
            if obj is not None and not same_placement(obj.Placement, pl):
                obj.Placement = pl
                moved.append(obj.Label.split(" [")[0])
        if moved and time.time() - self._moved_hint > 20:
            self._moved_hint = time.time()
            hint(f"{', '.join(moved)} is shared with your team, so the move was undone.\n\n"
                 "To change a part yourself, select it and choose the Tools tab \u25b8 Edit a part yourself in the "
                 "CADSwarm panel "
                 "(or start the edit first, then move it). Your change is sent to everyone when you click Save.")

    def mirror(self):
        doc = mirror_document()
        if doc is None:
            doc = App.newDocument(MIRROR_DOC)
            self._framed = False
        set_if_changed(doc, "Label", mirror_label(self.project))
        return doc

    def label_mirror(self):
        """Tab name of the shared model: the project's name, not a generic product name."""
        label = mirror_label(self.project)
        for name, doc in App.listDocuments().items():
            if is_mirror_doc(name):
                set_if_changed(doc, "Label", label)

    def show_model(self):
        """Bring the team's live model to the front — what "Done, show me the model" promises."""
        self.frame_model(show=True)

    def apply_breps(self, objects):
        doc = self.mirror()
        added = False
        for name, info in objects.items():
            if info is None:
                continue
            obj = doc.getObject(name)
            if info.get("brep") is None:
                continue  # e.g. the Params spreadsheet: shown in the panel instead
            shape = Part.Shape()
            shape.importBrepFromString(info["brep"])
            if obj is None:
                obj = doc.addObject("Part::Feature", name)
                added = True
            obj.Shape = shape
            self.synced_pl[name] = obj.Placement
            owner = info.get("locked_by")
            set_if_changed(obj, "Label", f"{info.get('label') or name} [{owner or 'unlocked'}]")
            vo = getattr(obj, "ViewObject", None)
            if vo:
                set_if_changed(vo, "Visibility", bool(info.get("visible", True)))
                set_if_changed(vo, "ShapeColor", owner_color(info.get("owner_id")))
        doc.recompute()
        if added:
            self.frame_model()

    def save_project_preview(self, force=False):
        """Keep a picture of the live model for this project's card in "Open a previous project", so the
        next time it is picked the list shows what the project looks like (and how many parts it has).

        The 3D view cannot give us one (FreeCAD's saveImage comes back empty unless the person is on
        that tab, and the team server saves model.FCStd without a thumbnail), so we draw the outline:
        the model's edges in a flat isometric projection. Cheap, and it always works."""
        db = (self.client.cfg or {}).get("db") if self.client else ""
        if not db:
            return False
        now = time.time()
        if not force and now - self._preview_at < PREVIEW_SECONDS:
            return False
        doc = mirror_document()
        if doc is None:
            return False
        segments, parts = iso_outline(doc)
        if not segments:
            return False
        if projects_ui.save_preview(db, segments, parts, now):
            self._preview_at = now
            return True
        return False

    def frame_model(self, show=False):
        """Frame the live model in view. Only bring its tab to the front when asked (`show`): a refresh,
        a part an agent just added or a reconnect must not pull the person away from their own work."""
        doc = mirror_document()
        if doc is None:
            return
        try:
            gdoc = Gui.getDocument(doc.Name)
            if gdoc is None or getattr(gdoc, "ActiveView", None) is None:
                return
            active = Gui.ActiveDocument is not None and Gui.ActiveDocument.Document.Name == doc.Name
            if show and not active:
                App.setActiveDocument(doc.Name)  # bring the live model's tab to the front
                active = True
            if active and not getattr(self, "_framed", False):
                gdoc.ActiveView.viewIsometric()  # beginners shouldn't land on an edge-on model
                gdoc.ActiveView.fitAll()
                self._framed = True
        except Exception as e:
            warn(f"couldn't frame the model: {e}")

    def remove_objects(self, names):
        doc = self.mirror()
        for n in names:
            self.synced_pl.pop(n, None)
            if doc.getObject(n):
                doc.removeObject(n)
        doc.recompute()

    def full_refresh(self):
        breps, _, self.mirrored = self.client.mirror_changes({})
        self.apply_breps(breps)
        self.remove_objects([o.Name for o in self.mirror().Objects if o.Name not in self.mirrored])
        self.refresh_meta()
        self.frame_model()

    def refresh_meta(self):
        """Read everything now, on this thread. Only for joining; everything else uses request_refresh()."""
        if not self.client:
            return
        try:
            self.apply_snapshot(self.client.panel_snapshot())
        except HubRequestError as e:
            warn(f"refresh failed: {e}")

    def apply_snapshot(self, snap):
        if not self.client:
            return
        self.model, self.tasks = snap["model"], snap["tasks"]
        self.commands, self.briefs, self.messages = snap["commands"], snap["briefs"], snap["messages"]
        self.reviews = snap.get("reviews") or []
        self.workspace = snap.get("workspace") or self.workspace
        owners = {o["name"]: o.get("locked_by") for o in self.model["objects"]}
        ids = {p["name"]: p["id"] for p in self.model["participants"]}
        for p in self.model["participants"]:
            owner_color(p["id"])  # reserve colors in join order
        doc = self.mirror()
        for o in doc.Objects:  # recolor on lock moves (takeover, release) without new geometry
            owner = owners.get(o.Name)
            if getattr(o, "ViewObject", None):
                set_if_changed(o.ViewObject, "ShapeColor", owner_color(ids.get(owner)))
            editing = " \u00b7 you're editing" if o.Name in self.editing else ""
            set_if_changed(o, "Label", f"{o.Label.split(' [')[0]} [{owner or 'unlocked'}{editing}]")
        if time.time() - self._helpers_checked > HELPER_CHECK_SECONDS:
            self._helpers_checked = time.time()
            start_local_helpers(self)
        if self.panel:
            self.panel.render(self)

    # --------------------------------------------------------------- actions

    def act(self, method, path, body=None, done=None, purpose="do that"):
        """Do one thing now and return its result, for steps that need it next. Prefer act_async."""
        if not self.ensure_connected(purpose):
            return None
        QtGui.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        self._begin_work()
        try:
            r = self.client.request(method, path, body if method != "GET" else None)
        except HubRequestError as e:
            QtGui.QApplication.restoreOverrideCursor()
            QtGui.QMessageBox.warning(Gui.getMainWindow(), "CADSwarm", friendly_error(e, self.client.hub if self.client else ""))
            return None
        finally:
            self._end_work()
        QtGui.QApplication.restoreOverrideCursor()
        if done:
            say(done if isinstance(done, str) else done(r))
        self.request_refresh()
        return r

    def act_async(self, method, path, body=None, done=None, purpose="do that", failed=None):
        """Send one request without freezing FreeCAD. `done` (text, or a function of the result) runs on the GUI
        thread once it's back; `failed` runs after an error has been shown."""
        def finish(r):
            if callable(done):
                done(r)
            elif done:
                say(done)
        self.run_async(lambda c: c.request(method, path, body if method != "GET" else None), finish, purpose,
                       failed)

    def run_async(self, work, done=None, purpose="do that", failed=None):
        """Run `work(client)` on a background thread; the outcome comes back through the event queue."""
        if not self.ensure_connected(purpose):
            if failed:
                failed()
            return
        client, events = self.client, self.events
        self._begin_work()

        def go():
            try:
                events.put(("done", (done, failed, work(client), None)))
            except HubRequestError as e:
                events.put(("done", (done, failed, None, friendly_error(e, client.hub))))
            except Exception as e:  # never lose a click silently
                events.put(("done", (done, failed, None, str(e))))
        threading.Thread(target=go, daemon=True).start()

    def finish_async(self, done, failed, result, error):
        if error:
            QtGui.QMessageBox.warning(Gui.getMainWindow(), "CADSwarm", error)
            if failed:
                failed()
        elif done:
            try:
                done(result)
            except Exception as e:
                warn(f"after that step: {e}")
        self.request_refresh()
        self._end_work()

    def working(self):
        """True while a request this session started is still running (a pressed button waits on this)."""
        return self._working > 0

    def when_idle(self, cb):
        """Run `cb` on the GUI thread once no request this session started is still running."""
        if self._working <= 0:
            QtCore.QTimer.singleShot(0, cb)
        else:
            self._idle_waiters.append(cb)

    def _begin_work(self):
        self._working += 1

    def _end_work(self):
        self._working = max(0, self._working - 1)
        if self._working == 0 and self._idle_waiters:
            waiters, self._idle_waiters = self._idle_waiters, []
            for cb in waiters:
                try:
                    cb()
                except Exception as e:
                    warn(f"after background work: {e}")

    def selected_names(self):
        names = []
        for sel in Gui.Selection.getSelection():
            if is_mirror_doc(sel.Document.Name):
                names.append(sel.Name)
        return names


def mirror_document():
    """The team's live model if it is already open — even when FreeCAD gave it a suffixed name
    (CadSwarmLive1) after an earlier mirror was closed or reloaded. Reusing it is what keeps a second,
    identical "read-only mirror" tab from opening."""
    docs = App.listDocuments()
    exact = docs.get(MIRROR_DOC)
    if exact is not None:
        return exact
    return max((d for name, d in docs.items() if is_mirror_doc(name)), key=lambda d: len(d.Objects), default=None)


ISO_COS, ISO_SIN = 0.8660254037844386, 0.5   # 30 degrees: the usual isometric view
MAX_OUTLINE_SEGMENTS = 12000                 # enough for a real model, still a frame or two at most


def iso_outline(doc):
    """(segments, parts): a flat isometric drawing of what `doc` shows, as 2D lines.

    The project cards need a picture of the model, and neither the 3D view (FreeCAD's saveImage comes
    back empty unless that tab is on screen) nor the saved model.FCStd (the team server writes it
    without a thumbnail) can give one. The parts' own edges can: a few thousand short lines, drawn
    with QPainter, read as the model at card size. `parts` counts every part that shows, even when the
    outline stops at MAX_OUTLINE_SEGMENTS (a card does not need more lines than that)."""
    objects = []
    for obj in getattr(doc, "Objects", []):
        shape = getattr(obj, "Shape", None)
        if shape is None or shape.isNull():
            continue
        vo = getattr(obj, "ViewObject", None)
        if vo is not None and not getattr(vo, "Visibility", True):
            continue
        objects.append(shape)
    segments = []
    for shape in objects:
        for edge in shape.Edges:
            try:
                points = edge.discretize(5)
            except Exception:  # noqa: BLE001 - a broken edge must not cost the whole picture
                continue
            for a, b in zip(points, points[1:]):
                segments.append((_iso(a), _iso(b)))
            if len(segments) >= MAX_OUTLINE_SEGMENTS:
                return segments, len(objects)
    return segments, len(objects)


def _iso(p):
    return ((p.x - p.y) * ISO_COS, (p.x + p.y) * ISO_SIN - p.z)


def set_if_changed(obj, prop, value):
    """Only write a property that really changed: every write marks the document modified and makes
    FreeCAD redraw and re-check its panels."""
    try:
        current = getattr(obj, prop)
        if prop == "ShapeColor":
            if all(abs(a - b) < 1e-3 for a, b in zip(current[:3], value[:3])):
                return
        elif current == value:
            return
        setattr(obj, prop, value)
    except Exception:
        pass


SESSION = Session()


# ------------------------------------------------------------------ panel

class TeamPanel(QtGui.QDockWidget):
    """A header that says where you stand, then tabs: Team (suggestions waiting for you, who's here, the chat
    and the everyday actions), Tasks, Activity, Library and Tools. Type @ and a helper's name to give it a
    task (it starts at once and answers in the chat), @team to have the whole team plan and build a request,
    @all to tell every helper something; anything else is chat.

    Everything sits in a scroll area, so a small screen scrolls instead of cropping. Lists are rebuilt only
    when what they show changed, so a refresh never closes a menu, drops a selection or jumps the chat."""

    SPINNER = "◐◓◑◒"

    def __init__(self, session):
        super().__init__("CADSwarm Team")
        self.session = session
        self.setObjectName("CadSwarmTeam")
        self.spin_i = 0
        self.busy_rows = []     # (item, column, text) redrawn with a turning spinner
        self.keys = {}          # what each list last showed: skip rebuilding it when nothing changed
        self.outbox = []        # messages sent but not yet back from the team: shown greyed in the chat
        self.new_activity = 0
        self.completed_at = 0.0  # when @-completion last filled in a name (its Enter must not also send)
        self.remote_for = ""     # the team the Remote tab's code was minted for (a new team needs a new code)
        self.remote_url = ""
        self.remote_code = ""
        self.suggestions = {"setting": [], "style": []}  # what AI helpers suggested, waiting for a person
        self.answered = set()   # suggestions answered here, hidden until the team's list catches up
        self.fetching = False
        w = QtGui.QWidget()
        lay = QtGui.QVBoxLayout(w)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(10)
        # The header always answers "where do I stand, and what do I do next?"
        self.banner = QtGui.QFrame()
        self.banner.setObjectName("card")
        bl = QtGui.QVBoxLayout(self.banner)
        bl.setContentsMargins(12, 10, 12, 10)
        bl.setSpacing(4)
        top = QtGui.QHBoxLayout()
        self.title = QtGui.QLabel()
        self.title.setObjectName("title")
        self.title.setSizePolicy(QtGui.QSizePolicy.Ignored, QtGui.QSizePolicy.Preferred)
        self.status = QtGui.QLabel()
        top.addWidget(self.title, 1)
        top.addWidget(self.status)
        bl.addLayout(top)
        self.banner_text = QtGui.QLabel()
        self.banner_text.setWordWrap(True)
        self.banner_text.setSizePolicy(QtGui.QSizePolicy.Ignored, QtGui.QSizePolicy.Preferred)
        bl.addWidget(self.banner_text)
        brow = QtGui.QHBoxLayout()
        # A teammate joins from their own FreeCAD with the code; a phone's QR lives in the Remote tab.
        self.invite_button = action_button("Invite a teammate", "Copy a single-use invite code for a teammate",
                                           invite_teammates)
        self.banner_button = action_button("", "", self.banner_action)
        brow.addWidget(self.invite_button)
        brow.addWidget(self.banner_button)
        bl.addLayout(brow)
        lay.addWidget(self.banner)
        self.toast = QtGui.QLabel()  # "✓ Created task …": what say() confirms, where people are looking
        self.toast.setObjectName("toast")
        self.toast.setWordWrap(True)
        self.toast.setSizePolicy(QtGui.QSizePolicy.Ignored, QtGui.QSizePolicy.Preferred)
        self.toast.hide()
        self.toast_timer = QtCore.QTimer(self)
        self.toast_timer.setSingleShot(True)
        self.toast_timer.timeout.connect(self.toast.hide)
        lay.addWidget(self.toast)

        self.tabs = QtGui.QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setElideMode(QtCore.Qt.ElideNone)
        lay.addWidget(self.tabs, 1)

        # ---------------------------------------------------------------- Team
        page = QtGui.QWidget()
        tl = page_layout(page)
        self.suggest_box = QtGui.QFrame()
        self.suggest_box.setObjectName("suggest")
        self.suggest_lay = QtGui.QVBoxLayout(self.suggest_box)
        self.suggest_lay.setContentsMargins(12, 10, 12, 10)
        self.suggest_lay.setSpacing(8)
        self.suggest_box.hide()
        tl.addWidget(self.suggest_box)
        head = QtGui.QHBoxLayout()
        head.addWidget(caption("Who's here"), 1)
        new = QtGui.QPushButton("＋ New helper")
        new.setObjectName("quiet")
        new.setToolTip("Start another AI helper on this computer. You can have as many as you like.")
        new.clicked.connect(self.new_helper)
        head.addWidget(new)
        tl.addLayout(head)
        self.people = QtGui.QLabel("")
        self.people.setWordWrap(True)
        tl.addWidget(self.people)
        self.team = QtGui.QTreeWidget()
        self.team.setObjectName("list")
        self.team.setHeaderLabels(["Helper", "Task", "Status"])
        self.team.setHeaderHidden(True)
        team_head = self.team.header()
        team_head.setStretchLastSection(False)
        team_head.setSectionResizeMode(0, QtGui.QHeaderView.Interactive)
        team_head.setSectionResizeMode(1, QtGui.QHeaderView.Stretch)
        team_head.setSectionResizeMode(2, QtGui.QHeaderView.Interactive)
        self.team.setItemDelegateForColumn(2, PillDelegate(self.team))
        self.team.installEventFilter(self)  # name and status keep their share of the width as the panel resizes
        self.team.setRootIsDecorated(False)
        self.team.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.team.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.team.customContextMenuRequested.connect(self.helper_menu)
        self.team.itemDoubleClicked.connect(self.team_double_click)
        self.team.setToolTip("Double-click a helper to give it a task. Right-click a helper to remove it from the "
                             "team; your own also rename, restart or show their log.")
        tl.addWidget(self.team)
        self.team_tip = QtGui.QLabel("")
        self.team_tip.setWordWrap(True)
        tl.addWidget(self.team_tip)

        tl.addSpacing(4)
        tl.addWidget(caption("Team chat"))
        self.chat = QtGui.QTextBrowser()
        self.chat.setOpenLinks(False)
        self.chat.setMinimumHeight(220)
        tl.addWidget(self.chat, 1)
        # Scrolled up (reading back, or away for a while)? A button in the corner jumps to the newest message.
        self.chat_count = 0
        self.unseen = 0
        self.chat_rendering = False
        self.chat_follow = True  # pinned to the newest message (until the person scrolls up)
        self.jump = QtGui.QPushButton("↓", self.chat)
        self.jump.setObjectName("jump")
        self.jump.setToolTip("Jump to the newest message")
        self.jump.setCursor(QtCore.Qt.PointingHandCursor)
        self.jump.clicked.connect(self.jump_to_bottom)
        self.jump.hide()
        self.chat.installEventFilter(self)  # keeps the button in the bottom-right corner as the chat resizes
        self.chat.verticalScrollBar().actionTriggered.connect(lambda _a: self.chat_user_scroll())
        self.chat.verticalScrollBar().valueChanged.connect(lambda _v: self.chat_scrolled())
        self.chat.verticalScrollBar().rangeChanged.connect(lambda *_: self.chat_grew())
        row = QtGui.QHBoxLayout()
        self.msg = QtGui.QLineEdit()
        self.msg.setPlaceholderText("@helper what to do, or a message for your team")
        self.msg.returnPressed.connect(self.send)
        self.msg.textEdited.connect(self.maybe_complete)
        row.addWidget(self.msg, 1)
        send = QtGui.QPushButton("Send")
        send.setObjectName("primary")
        send.clicked.connect(self.send)
        row.addWidget(send)
        tl.addLayout(row)
        self.completer = QtGui.QCompleter(self)
        self.completer.setModel(QtCore.QStringListModel([], self.completer))
        self.completer.setCaseSensitivity(QtCore.Qt.CaseInsensitive)
        self.completer.setCompletionMode(QtGui.QCompleter.PopupCompletion)
        self.completer.setWidget(self.msg)
        try:
            self.completer.activated[str].connect(self.insert_mention)
        except (KeyError, TypeError, IndexError):
            self.completer.activated.connect(self.insert_mention)
        hint_ = QtGui.QLabel("<small style='color:#8b949e'><b>@name</b> gives a helper a task · <b>@team</b> plans "
                             "and builds a whole request · <b>@all</b> tells every helper · no @ is chat between "
                             "people</small>")
        hint_.setWordWrap(True)
        tl.addWidget(hint_)
        quick = QtGui.QGridLayout()
        quick.setSpacing(6)
        for i, (label, tip, fn) in enumerate((
                ("Add a task", "Give the team (or yourself) something to do", self.new_task),
                ("Change a shared size", "Change a size every part reads from, like the hole spacing",
                 self.set_param),
                ("Check for clashes", "Find parts that overlap", run_interference),
                ("Save as STL/STEP file", "Save the shared model as an STL (for 3D printing) or a STEP "
                                          "(editable CAD) file", run_export))):
            quick.addWidget(action_button(label, tip, fn), i // 2, i % 2)
        tl.addLayout(quick)
        self.params = QtGui.QLabel("")
        self.params.setWordWrap(True)
        tl.addWidget(self.params)
        tl.addSpacing(4)
        tl.addWidget(caption("Review and printer"))
        self.review_box = QtGui.QLabel("")
        self.review_box.setWordWrap(True)
        self.review_box.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        tl.addWidget(self.review_box)
        self.team_page = page
        self.tabs.addTab(page, "Team")

        # --------------------------------------------------------------- Tasks
        page = QtGui.QWidget()
        tl = page_layout(page)
        head = QtGui.QHBoxLayout()
        head.addWidget(caption("Tasks"), 1)
        add = QtGui.QPushButton("＋ New task")
        add.setObjectName("quiet")
        add.setToolTip("Give the team (or yourself) something to do")
        add.clicked.connect(self.new_task)
        head.addWidget(add)
        tl.addLayout(head)
        self.task_list = QtGui.QTreeWidget()
        self.task_list.setObjectName("list")
        self.task_list.setHeaderLabels(["Task", "Status"])
        th = self.task_list.header()
        th.setStretchLastSection(False)
        th.setSectionResizeMode(0, QtGui.QHeaderView.Stretch)
        th.setSectionResizeMode(1, QtGui.QHeaderView.Interactive)
        self.task_list.installEventFilter(self)  # keeps the status column at about 40% when the panel resizes
        self.task_list.itemSelectionChanged.connect(self.task_selected)
        self.task_list.itemDoubleClicked.connect(lambda *_: self.task_details())
        self.task_list.setMinimumHeight(220)
        tl.addWidget(self.task_list, 1)
        grid = QtGui.QGridLayout()
        grid.setSpacing(6)
        self.task_start = action_button("Start it", "Ask the helper it's for (or one you pick) to start it now",
                                        self.task_ask_start)
        self.task_mine = action_button("I'll do it", "Take this task yourself and change the parts by hand",
                                       self.task_take)
        self.task_done = action_button("Mark done", "Say you've finished this task", self.task_finish)
        self.task_cancel = action_button("Cancel…", "Stop this task or request; the team stops working on it",
                                         self.task_cancel_selected)
        self.task_delete = action_button("Delete", "Remove a leftover task nobody has started",
                                         self.task_delete_selected)
        self.task_stl = action_button("Save STL…", "Save this request's finished model as an STL file",
                                      self.brief_save_stl)
        details = action_button("Details", "Everything about this task, and its result", self.task_details)
        for i, b in enumerate((self.task_start, self.task_mine, self.task_done, details,
                               self.task_cancel, self.task_delete, self.task_stl)):
            grid.addWidget(b, i // 2, i % 2)
        tl.addLayout(grid)
        self.task_hint = QtGui.QLabel("")
        self.task_hint.setWordWrap(True)
        tl.addWidget(self.task_hint)
        self.tasks_page = page
        self.tabs.addTab(page, "Tasks")

        # ------------------------------------------------------------ Activity
        page = QtGui.QWidget()
        tl = page_layout(page)
        tl.addWidget(caption("Everything that happens, as it lands"))
        self.activity = QtGui.QTextBrowser()
        self.activity.setMinimumHeight(260)
        tl.addWidget(self.activity, 1)
        self.activity_page = page
        self.tabs.addTab(page, "Activity")

        # --------------------------------------------------------- Shared sizes
        self.sizes_page = panes.SharedSizesTab(session)
        self.tabs.addTab(self.sizes_page, "Sizes")

        # -------------------------------------------------------- Project files
        self.files_page = panes.FilesTab(session)
        self.tabs.addTab(self.files_page, "Files")

        # ------------------------------------------------------------- Library
        self.library = library_ui.LibraryTab(session)
        self.tabs.addTab(self.library, "Library")

        # -------------------------------------------------------------- Remote
        # The one place a phone gets in: the QR a camera scans, and the same link as text.
        page = QtGui.QWidget()
        tl = page_layout(page)
        tl.addWidget(caption("Your phone runs this build"))
        card = QtGui.QFrame()
        card.setObjectName("card")
        rl = QtGui.QVBoxLayout(card)
        rl.setContentsMargins(14, 14, 14, 14)
        rl.setSpacing(10)
        self.remote_qr = QtGui.QLabel()
        self.remote_qr.setObjectName("qr")
        self.remote_qr.setAlignment(QtCore.Qt.AlignCenter)
        self.remote_qr.setMinimumHeight(190)
        self.remote_qr.setSizePolicy(QtGui.QSizePolicy.Ignored, QtGui.QSizePolicy.Preferred)
        rl.addWidget(self.remote_qr)
        self.remote_lede = QtGui.QLabel(
            "Point your phone's camera at this code. The phone joins this team as you: you can watch the "
            "model, chat with everyone and steer the build while you're away from the desk.")
        self.remote_lede.setWordWrap(True)
        self.remote_lede.setSizePolicy(QtGui.QSizePolicy.Ignored, QtGui.QSizePolicy.Preferred)
        rl.addWidget(self.remote_lede)
        self.remote_link = QtGui.QLineEdit()
        self.remote_link.setReadOnly(True)
        self.remote_link.setPlaceholderText("the link appears here once you're in a team")
        self.remote_link.setSizePolicy(QtGui.QSizePolicy.Ignored, QtGui.QSizePolicy.Fixed)
        self.remote_link.setToolTip("The same invite as a link. Send it in a chat if a scan isn't handy.")
        rl.addWidget(self.remote_link)
        row = QtGui.QHBoxLayout()
        self.remote_copy = action_button("Copy link", "Copy the phone link", self.copy_remote_link)
        self.remote_new = action_button("New code", "Make a fresh code for the next phone", self.mint_remote)
        row.addWidget(self.remote_copy)
        row.addWidget(self.remote_new)
        rl.addLayout(row)
        tl.addWidget(card)
        self.remote_note = QtGui.QLabel("")
        self.remote_note.setWordWrap(True)
        self.remote_note.setSizePolicy(QtGui.QSizePolicy.Ignored, QtGui.QSizePolicy.Preferred)
        tl.addWidget(self.remote_note)
        tl.addStretch(1)
        self.remote_page = page
        self.tabs.addTab(page, "Remote")
        # --------------------------------------------------------------- Tools
        page = QtGui.QWidget()
        tl = page_layout(page)
        for title, actions in (
                ("Build", (("✎ Edit a part yourself…", "Change a part by hand and share the change", edit_part),
                           ("Change a shared size…", "Change a size every part reads from", self.set_param),
                           ("Auto-constrain…", "Bind the sizes and fits the model already repeats to shared "
                                               "sizes", auto_constrain),
                           ("Check for clashes", "Find parts that overlap", run_interference),
                           ("Save as STL/STEP file…", "Save the shared model as an STL (for 3D printing) or a "
                                                      "STEP (editable CAD) file", run_export),
                           ("Lighten part…", "Fill the selected part with a lattice that keeps its stiffness",
                            lighten_ui.show_lighten))),
                ("Print", (("Printer bed…", "Tell the team what printer you print on", printer_bed),
                           ("Check it fits the printer…", "Check the parts fit the bed, are closed solids and have "
                            "printable walls", check_printability))),
                ("Project", (("Open a previous project…", "Reopen another team you've joined on this computer",
                              open_previous_team),
                             ("Project files…", "Notes, plans and scripts your team saved", show_project_files),
                             ("Open project folder", "Open the folder the team saves into", open_project_folder),
                             ("Save a snapshot…", "Keep this version of the model", save_snapshot),
                             ("Run a simulation…", "Check how a part holds up under a load, without asking a helper",
                              fea_ui.show_run_simulation),
                             ("Simulation reports…", "The strength checks (FEA) your helpers ran",
                              show_fea_reports),
                             ("Storage and free up space…", "What uses the project's room", show_storage))),
                ("Team rules", (("Precision settings…", "Clearances, fits and precision for the whole team",
                                 library_ui.show_settings),
                                ("Learned design style…", "The design rules CADSwarm learned from your team",
                                 style_ui.show_style))),
                ("AI helpers", (("Set up a helper step by step…", "Connect Claude Code, Codex or OpenCode",
                                 add_helper),
                                ("Invite a teammate…", "Copy a single-use invite code", invite_teammates),
                                ("Restart my AI helpers", "Restart the helpers that run on this computer",
                                 restart_helpers),
                                ("Show my AI helpers' logs", "What your helpers wrote while working",
                                 show_helper_log))),
                ("Parts", (("Hold the selected parts", "Only you can change them until you let go", self.lock),
                           ("Let go of the selected parts", "Anyone can change them again", self.release),
                           ("Force-free the selected parts", "Free parts someone else holds", self.force_release),
                           ("Take over a teammate's work", "Take a teammate's held parts and tasks",
                            self.take_over)))):
            tl.addWidget(caption(title))
            group = QtGui.QFrame()
            group.setObjectName("card")
            gl = QtGui.QVBoxLayout(group)
            gl.setContentsMargins(4, 4, 4, 4)
            gl.setSpacing(0)
            for label, tip, fn in actions:
                b = action_button(label, tip, fn)
                b.setObjectName("tool")
                gl.addWidget(b)
            tl.addWidget(group)
            tl.addSpacing(6)
        tl.addWidget(caption("3D view"))
        arrows = QtGui.QHBoxLayout()
        self.pointers = QtGui.QCheckBox("Show helpers' arrows")
        self.pointers.setSizePolicy(QtGui.QSizePolicy.Ignored, QtGui.QSizePolicy.Fixed)
        self.pointers.setToolTip("An arrow in each helper's colour sits on the part it is working on")
        self.pointers.setChecked(overlay.pointers_enabled())
        self.pointers.toggled.connect(overlay.set_pointers)
        arrows.addWidget(self.pointers, 1)
        preview = QtGui.QPushButton("Preview")
        preview.setObjectName("quiet")
        preview.setToolTip("Open a demo model with moving arrows, to see what they look like")
        preview.clicked.connect(overlay.preview)
        arrows.addWidget(preview)
        tl.addLayout(arrows)
        tl.addSpacing(6)
        stop = action_button("Stop hosting the team", "Stop the team server on this computer (only if you started "
                             "the team)", stop_hosting)
        stop.setObjectName("danger")
        tl.addWidget(stop)
        tl.addStretch(1)
        self.tools_page = page
        self.tabs.addTab(page, "Tools")
        self.tabs.currentChanged.connect(self.tab_changed)
        self.tab_changed(0)

        w.setObjectName("cadswarmPanel")
        # Style the dock itself (self), so the sheet reaches the dock, the scroll area and the page.
        self.setStyleSheet(PANEL_STYLE)
        # A small screen scrolls instead of squashing or cropping anything.
        scroll = QtGui.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtGui.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setWidget(w)
        self.setMinimumWidth(PANEL_MIN_WIDTH)
        self.setWidget(scroll)
        self.spin_timer = QtCore.QTimer(self)
        self.spin_timer.timeout.connect(self.spin)
        self.spin_timer.start(350)
        self.suggest_timer = QtCore.QTimer(self)
        self.suggest_timer.timeout.connect(self.fetch_suggestions)
        self.suggest_timer.start(SUGGEST_SECONDS * 1000)
        # Follow the FreeCAD window: keep the share of its width people last dragged the panel to.
        self.share = PANEL_SHARE
        self.window_width = 0
        try:
            Gui.getMainWindow().installEventFilter(self)
        except Exception:
            pass
        self.task_selected()
        self.show_state(session)

    def tab_changed(self, index):
        """Size the tabs to the one showing: a hidden tab neither widens the panel nor leaves its height behind."""
        for i in range(self.tabs.count()):
            policy = QtGui.QSizePolicy.Preferred if i == index else QtGui.QSizePolicy.Ignored
            self.tabs.widget(i).setSizePolicy(policy, policy)
        self.tabs.updateGeometry()
        if self.tabs.widget(index) is self.activity_page:
            self.new_activity = 0
            self.tabs.setTabText(index, "Activity")
        if self.tabs.widget(index) is self.remote_page:
            self.refresh_remote()
        if self.tabs.widget(index) is self.files_page:
            self.files_page.refresh_if_stale()
        if self.tabs.widget(index) is self.sizes_page:
            self.sizes_page.render(self.session)

    def show_tab(self, page):
        self.tabs.setCurrentWidget(page)

    def show_tasks(self):
        self.show_tab(self.tasks_page)

    # ------------------------------------------------------------------- Remote

    def show_remote(self):
        """Open the one place a phone joins from (the QR and the same link as text)."""
        self.show_tab(self.remote_page)

    def refresh_remote(self):
        """Fill the Remote tab: a fresh code the first time, the same one after that."""
        if self.session.state != "connected":
            self.remote_for = ""
            self.remote_url = self.remote_code = ""
            self.remote_qr.clear()
            self.remote_qr.setMinimumHeight(0)
            self.remote_link.clear()
            self.remote_note.setText("Start or join a team first. Then this tab shows the code your phone scans.")
            return
        db = prefs().GetString("Db")
        if self.remote_for != db or not self.remote_url:
            self.mint_remote()
            return
        self.remote_link.setText(self.remote_url)
        self.remote_link.setCursorPosition(0)
        self.paint_remote()

    def mint_remote(self):
        """Mint one single-use invite and show it as a link (and, when we can draw it, a QR)."""
        if not self.session.ensure_connected("invite a teammate"):
            return
        try:
            code = self.session.client.create_invite("human", "member", 86400, prefs().GetString("ProjectName"),
                                                     uri=hosting.invite_uri(self.session.client.hub))
            t = read_invite(code)
            t.pop("project", None)  # the phone link carries the ticket; the project name rides in the code
            url = ticket.phone_url(web_url_for_phone(), ticket.encode(t))
        except HubRequestError as e:
            self.remote_note.setText(friendly_error(e, self.session.client.hub))
            return
        except (InviteError, ValueError, TypeError) as e:
            self.remote_note.setText(f"That code came back damaged: {e}")
            return
        self.remote_for = prefs().GetString("Db")
        self.remote_code = code
        self.remote_url = url
        self.remote_link.setText(url)
        self.remote_link.setCursorPosition(0)
        self.paint_remote()
        self.remote_note.setText("One code works once, and only while this computer is on with CadSwarm running. "
                                 "Make a new code for the next phone."
                                 + ("\nThis team's database runs on this computer, so a phone must be on the same "
                                    "network, and the public site can't reach it: use FreeCAD on a networked "
                                    "computer for now." if prefs().GetString("HostMode") == "local" else ""))

    def paint_remote(self):
        rows = qr.rows(self.remote_url) if self.remote_url else []
        if not rows:
            self.remote_qr.clear()  # no drawing program yet: the link below still gets the phone in
            self.remote_qr.setMinimumHeight(0)
            return
        self.remote_qr.setMinimumHeight(0)
        room = max(200, min(280, self.remote_qr.width() or self.width() - 40 or 220))
        self.remote_qr.setPixmap(qr_pixmap(rows, room, self.devicePixelRatioF()))

    def copy_remote_link(self):
        if not self.remote_url:
            self.refresh_remote()
        if not self.remote_url:
            return
        QtGui.QApplication.clipboard().setText(self.remote_url)
        self.flash("Phone link copied")

    # ------------------------------------------------------------------ window size

    def follow_window(self):
        mw = Gui.getMainWindow()
        if self.isFloating() or not self.isVisible() or mw.width() <= 0:
            return
        self.window_width = mw.width()
        width = int(min(max(mw.width() * self.share, PANEL_MIN_WIDTH), mw.width() * 0.6))
        if abs(width - self.width()) > 2:
            try:
                mw.resizeDocks([self], [width], QtCore.Qt.Horizontal)
            except (AttributeError, TypeError):
                pass

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        mw = Gui.getMainWindow()
        # The window didn't change size, so a person dragged the panel's edge: remember their choice.
        if not self.isFloating() and mw.width() > 0 and mw.width() == self.window_width:
            self.share = min(max(self.width() / mw.width(), 0.15), 0.6)

    def eventFilter(self, obj, ev):
        if ev.type() == QtCore.QEvent.Resize and obj is Gui.getMainWindow():
            QtCore.QTimer.singleShot(0, self.follow_window)
        if obj is getattr(self, "team", None) and ev.type() == QtCore.QEvent.Resize:
            self.team.setColumnWidth(0, max(90, int(ev.size().width() * 0.34)))
            self.team.setColumnWidth(2, max(70, self.team.sizeHintForColumn(2)))
        if obj is getattr(self, "task_list", None) and ev.type() == QtCore.QEvent.Resize:
            self.task_list.setColumnWidth(1, max(110, int(ev.size().width() * 0.4)))
        if obj is getattr(self, "chat", None) and ev.type() == QtCore.QEvent.Resize:
            self.place_jump()
        return super().eventFilter(obj, ev)

    def flash(self, text):
        """Confirm something the person did in the panel itself, for a few seconds."""
        self.toast.setText(f"✓  {text}")
        self.toast.show()
        self.toast_timer.start(TOAST_SECONDS * 1000)

    # --------------------------------------------------------------- jump to the newest message

    def chat_at_bottom(self):
        bar = self.chat.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4

    def place_jump(self):
        bar = self.chat.verticalScrollBar()
        right = self.chat.width() - (bar.width() if bar.isVisible() else 0) - 12
        self.jump.move(right - self.jump.width(), self.chat.height() - self.jump.height() - 12)

    def chat_user_scroll(self):
        """The person scrolled (wheel, drag, keys): stop following until they're back at the bottom. Only this
        decides it, because Qt also moves the scroll position itself when the chat is resized or redrawn."""
        self.chat_follow = False
        QtCore.QTimer.singleShot(0, self.chat_user_scrolled)  # the action is applied after this signal

    def chat_user_scrolled(self):
        self.chat_follow = self.chat_at_bottom()
        self.update_jump()

    def chat_scrolled(self):
        if self.chat_rendering:  # mid-redraw the chat is briefly empty, which would look like "at the bottom"
            return
        if self.chat_follow and not self.chat_at_bottom():  # Qt moved it (a resize): back to the newest
            bar = self.chat.verticalScrollBar()
            bar.setValue(bar.maximum())
        self.update_jump()

    def chat_grew(self):
        """Qt lays a long chat out a bit at a time, so the bottom keeps moving: stay pinned to it."""
        if self.chat_follow and not self.chat_rendering:
            bar = self.chat.verticalScrollBar()
            bar.setValue(bar.maximum())
        self.update_jump()

    def update_jump(self):
        """Show the button only while scrolled up, with how many messages came in meanwhile."""
        if self.chat_rendering:
            return
        if self.chat_follow or self.chat_at_bottom():
            self.unseen = 0
            self.jump.hide()
            return
        self.jump.setText(f"↓  {self.unseen} new" if self.unseen else "↓")
        self.jump.adjustSize()
        self.jump.resize(max(self.jump.width(), self.jump.height()), self.jump.height())
        self.place_jump()
        self.jump.show()
        self.jump.raise_()

    def jump_to_bottom(self):
        self.chat_follow = True
        bar = self.chat.verticalScrollBar()
        bar.setValue(bar.maximum())
        self.update_jump()

    # ------------------------------------------------------------------ state

    BANNERS = {
        # state: (status colour, status word, headline, button)
        "connecting": ("#d29922", "Joining…", "Rejoining your team…", ""),
        "disconnected": ("#d29922", "Not joined", "You haven't joined a team session yet.", "Get started…"),
        "offline": ("#f85149", "Reconnecting", "Lost contact with your team. Retrying automatically…",
                    "Retry now"),
        "ended": ("#f85149", "Signed out", "You're no longer in the team session.", "Join again…"),
        "connected": ("#3fb950", "Connected", "", "Leave"),
    }

    def show_state(self, s):
        colour, word, headline, button = self.BANNERS[s.state]
        project = s.project or prefs().GetString("ProjectName")
        self.title.setText("Team" + (f" · {html.escape(project)}" if project else ""))
        self.status.setText(f"<span style='color:{colour}'>●&nbsp;{word}</span>")
        if s.state == "connected":
            me = s.ident["name"] if s.ident else "you"
            parts = [o for o in s.model.get("objects", []) if o.get("bbox")]
            headline = (f"You're <b>{html.escape(me)}</b> · {len(parts)} part{'s' if len(parts) != 1 else ''} "
                        "in the model")
        detail = ""
        if s.state == "disconnected":
            left = s.state_detail == "You left the team session."
            stopped = s.state_detail == STOPPED_HOSTING
            if left:
                headline = "You left the team session. Your locks were released."
            elif stopped:
                headline = "You stopped hosting the team. Your project is saved."
            detail = ("Join again to keep working with your team." if left else
                      "Your team's AI helpers are offline until you start it again "
                      "(Get started ▸ Reconnect)." if stopped else
                      "Start or join a team to watch the shared model being built and work with your "
                      "team's AI helpers."
                      + (f"<br><i>{s.state_detail}</i>".replace("\n", " ") if s.state_detail else ""))
        elif s.state in ("offline", "ended"):
            detail = s.state_detail.replace("\n", "<br>")
        elif s.state == "connecting":
            detail = (s.state_detail or "Reading the team's model and tasks.") + \
                     "<br>FreeCAD stays usable while this finishes."
        if s.state == "disconnected" and s.rejoin_error:
            headline = "Couldn't reach your team just now."
        self.banner_text.setText(f"<b>{headline}</b>" + (f"<br><span style='color:#8b949e'>{detail}</span>"
                                                          if detail else "")
                                 if s.state != "connected" else f"<span style='color:#8b949e'>{headline}</span>")
        self.banner_button.setText("Try again" if (s.state == "disconnected" and s.rejoin_error) else button)
        self.banner_button.setVisible(s.state != "connecting")  # nothing to press while it joins by itself
        # Not in a team: joining is the one thing to do, so its button stands out.
        primary = "" if s.state == "connected" else "primary"
        if self.banner_button.objectName() != primary:
            self.banner_button.setObjectName(primary)
            self.banner_button.style().unpolish(self.banner_button)
            self.banner_button.style().polish(self.banner_button)
        self.invite_button.setVisible(s.state == "connected")
        if s.state != "connected" and self.remote_url:  # a code minted for the team you just left is dead weight
            self.remote_for = self.remote_url = self.remote_code = ""
            if getattr(self, "remote_page", None) is not None:
                self.remote_qr.clear()
                self.remote_link.clear()

    def banner_action(self):
        s = self.session
        if s.state == "connecting":
            return  # it is joining by itself; a second join would fight the first
        if s.state == "disconnected" and s.rejoin_error:
            return s.try_auto_rejoin()  # one click, no sign-in questions: retry what failed
        if s.state == "connected":
            if confirm("Leave the team session? Your locks will be released so others can edit those parts."):
                s.disconnect()
        elif s.state == "offline":
            s.request_refresh()
        else:
            connect_dialog()

    def changed(self, name, key):
        """True (and remembers `key`) when list `name` must be rebuilt."""
        if self.keys.get(name) == key:
            return False
        self.keys[name] = key
        return True

    def render(self, s):
        self.show_state(s)
        self.busy_rows = []
        self.render_team(s)
        self.render_tasks(s)
        self.render_chat(s)
        self.sizes_page.render(s)
        self.files_page.render(s)
        params = s.model.get("params") or {}
        self.params.setText("<small><span style='color:#8b949e'>Shared sizes</span>&nbsp; " + html.escape(" · ".join(
            f"{k.replace('_', ' ')} {v:g} mm" if isinstance(v, (int, float)) else f"{k.replace('_', ' ')} {v}"
            for k, v in params.items()) or "none yet") + "</small>")
        if not self.keys.get("suggestions_read"):
            self.keys["suggestions_read"] = True
            self.fetch_suggestions()
        self.render_review(s)

    def render_review(self, s):
        """The newest reviewer round and the team's printer bed, in the words a teammate can act on."""
        ws = s.workspace or {}
        bed = ws.get("bed") or [ws.get("bed_x_mm"), ws.get("bed_y_mm"), ws.get("bed_z_mm")]
        try:
            size = " x ".join(f"{float(v):g}" for v in bed[:3])
        except (TypeError, ValueError):
            size = "180 x 180 x 180"
        lines = [f"<span style='color:#8b949e'>Printer bed</span> {size} mm"]
        if not s.reviews:
            lines.append("<span style='color:#8b949e'>No review yet. The reviewer checks the model before it is "
                         "ready.</span>")
        else:
            r = s.reviews[0]
            passed = bool(r.get("passed"))
            colour = "#2da44e" if passed else "#d9822b"
            word = "passed" if passed else "asks for changes"
            findings = [f for f in (r.get("findings") or []) if f]
            lines.append(f"<b style='color:{colour}'>Round {int(r.get('round') or 0)} {word}</b>")
            lines.extend("· " + html.escape(str(f)) for f in findings[:3])
            if len(findings) > 3:
                lines.append(f"<span style='color:#8b949e'>and {len(findings) - 3} more</span>")
        self.review_box.setText("<br>".join(lines))

    def render_team(self, s):
        me = s.ident["name"] if s.ident else "?"
        local = local_helper_status(s)
        pending = {}
        for c in s.commands:
            for name, st in c["by_helper"].items():
                if st == "sent":
                    pending[name] = pending.get(name, 0) + 1
        rows, tips = [], []
        for p in s.model["participants"]:
            if not takes_instructions(p):
                continue
            state, task, status = helper_row(p, local.get(p["name"]), s.tasks)
            if pending.get(p["name"]):
                status += f" · {pending[p['name']]} unread"
            held = tuple(lock for lock in p["locks"] if lock != "Params")
            rows.append((p["name"], p["id"], p.get("client"), state, task, status, held, p["name"] in local))
            if state == "away":
                tips.append(away_tip(p, local.get(p["name"]), team_db(s)))
        people = [p for p in s.model["participants"] if p.get("kind") == "human"]
        self.people.setText("<br>".join(
            f"<span style='color:{dot_colour(p['id'])}; font-size:13pt'>●</span>&nbsp;{html.escape(p['name'])}"
            + (" (you)" if p["name"] == me else "")
            + f" <span style='color:#8b949e'>· {'watching' if p.get('status') == 'active' else 'away'}</span>"
            for p in people))
        self.team_tip.setText("<small>" + "<br>".join(tips) + "</small>" if tips else "")
        self.team_tip.setVisible(bool(tips))
        choices = [r[0] for r in rows] + ["team", "all"]
        if self.completer.model().stringList() != choices:  # resetting it would close an open list
            self.completer.model().setStringList(choices)
        if not self.changed("team", tuple(rows)):
            for i in range(self.team.topLevelItemCount()):  # keep the spinners turning
                item = self.team.topLevelItem(i)
                if item.data(2, QtCore.Qt.UserRole) == "working":
                    self.busy_rows.append((item, 2, item.data(2, QtCore.Qt.UserRole + 1)))
            return
        self.team.clear()
        if not rows:
            empty = QtGui.QTreeWidgetItem(["No AI helpers yet. Click ＋ New helper.", "", ""])
            empty.setFlags(QtCore.Qt.NoItemFlags)
            empty.setFirstColumnSpanned(True)
            self.team.addTopLevelItem(empty)
        for name, pid, client, state, task, status, held, mine in rows:
            item = QtGui.QTreeWidgetItem([name, task or "no task", status])
            item.setIcon(0, dot_icon(owner_color(pid)))
            item.setData(0, QtCore.Qt.UserRole, name)
            item.setData(2, QtCore.Qt.UserRole, state)
            item.setData(2, QtCore.Qt.UserRole + 1, status)
            who = who_is({"kind": "agent", "client": client}) + (" on this computer (yours)" if mine else "")
            item.setToolTip(0, who)
            item.setToolTip(1, task or "Nothing assigned. Type @" + name + " in the chat to give it a task.")
            item.setToolTip(2, status + (f"\nHolds {names(list(held))}" if held else ""))
            if mine:
                font = item.font(0)
                font.setBold(True)
                item.setFont(0, font)
            if not task:
                item.setForeground(1, QtGui.QBrush(QtGui.QColor("#8b949e")))
            colour = {"working": "#d9822b", "ready": "#2da44e", "away": "#8b949e"}.get(state)
            if colour:
                item.setForeground(2, QtGui.QBrush(QtGui.QColor(colour)))
            if state == "working":
                self.busy_rows.append((item, 2, status))
            self.team.addTopLevelItem(item)
        self.team.setColumnWidth(2, max(70, self.team.sizeHintForColumn(2)))  # the whole status pill shows
        self.fit_team_height()

    def fit_team_height(self):
        """As tall as its rows (up to about six), so there's no empty box above the chat."""
        rows = self.team.topLevelItemCount()
        row_h = max(self.team.sizeHintForRow(0), self.team.fontMetrics().height() + 10)
        self.team.setFixedHeight(min(max(rows, 1), 6) * row_h + 2 * self.team.frameWidth() + 4)

    def render_tasks(self, s):
        key = (tuple((t["id"], t["status"], t.get("owner_name"), t.get("assignee"), t["title"], t.get("brief"))
                     for t in s.tasks),
               tuple((b["id"], b["status"], b["done"], b["tasks"], b.get("stl")) for b in s.briefs))
        open_ = sum(1 for t in s.tasks if t["status"] not in ("done", "cancelled"))
        self.tabs.setTabText(self.tabs.indexOf(self.tasks_page), f"Tasks · {open_}" if open_ else "Tasks")
        if not self.changed("tasks", key):
            for item, word in getattr(self, "task_busy", []):
                self.busy_rows.append((item, 1, word))
            return
        keep = self.selected_task_id()
        self.task_list.blockSignals(True)
        self.task_list.clear()
        self.task_busy = []
        groups = {}
        for b in s.briefs:
            word = BRIEF_WORDS.get(b["status"], b["status"])
            if b["status"] in ("building", "reviewing") and b["tasks"]:
                word += f" ({b['done']}/{b['tasks']} parts)"
            item = QtGui.QTreeWidgetItem([f"🧠 {b['text']}", word])
            item.setToolTip(0, f"{b['text']}\n(asked by {b['by']})")
            item.setForeground(1, QtGui.QBrush(QtGui.QColor(
                {"ready": "#4fb35a", "failed": "#ff7b72", "cancelled": "#8b949e"}.get(b["status"], "#d9822b"))))
            item.setData(0, QtCore.Qt.UserRole, None)
            item.setData(0, QtCore.Qt.UserRole + 1, b["id"])
            self.task_list.addTopLevelItem(item)
            item.setExpanded(b["status"] not in ("ready", "cancelled"))
            groups[b["id"]] = item
        if not s.tasks and not s.briefs:
            empty = QtGui.QTreeWidgetItem(["No tasks yet. Type @ and a helper's name in the chat to give it one.",
                                           ""])
            empty.setFlags(QtCore.Qt.NoItemFlags)
            empty.setFirstColumnSpanned(True)
            self.task_list.addTopLevelItem(empty)
        for t in sorted(s.tasks, key=lambda t: TASK_ORDER.get(t["status"], 9)):
            who = t.get("owner_name") or t.get("assignee") or "anyone"
            word = TASK_WORDS.get(t["status"], t["status"])
            parent = groups.get(t.get("brief"))
            item = QtGui.QTreeWidgetItem([t["title"], f"{word} · {who}"])
            item.setData(0, QtCore.Qt.UserRole, t["id"])
            item.setToolTip(0, (t.get("detail") or t["title"])[:600])
            item.setToolTip(1, f"{word}, {who}")
            colour = {"claimed": "#d9822b", "done": "#4fb35a", "blocked": "#ff7b72",
                      "awaiting_takeover": "#e3b341", "cancelled": "#8b949e"}.get(t["status"])
            if colour:
                item.setForeground(1, QtGui.QBrush(QtGui.QColor(colour)))
            if parent is not None:
                parent.addChild(item)
            else:
                self.task_list.addTopLevelItem(item)
            if t["status"] == "claimed":
                self.busy_rows.append((item, 1, word))
                self.task_busy.append((item, word))
            if t["id"] == keep:
                item.setSelected(True)
        self.task_list.blockSignals(False)
        self.task_selected()

    def render_chat(self, s):
        me = s.ident["name"] if s.ident else None
        states = {c["id"]: c for c in s.commands}
        shown = [m for m in s.messages if m["kind"] not in ("notice", "help", "result")]
        sent = {m["body"] for m in shown if m["from"] == me}
        self.outbox = [o for o in self.outbox if o["body"] not in sent and time.time() - o["at"] < 60]
        key = (tuple((m["id"], states[m["id"]]["state"] if m["id"] in states else "") for m in shown),
               tuple(o["body"] for o in self.outbox), tuple(n["text"] for n in s.notices))
        if not self.changed("chat", key):
            return
        bar = self.chat.verticalScrollBar()
        at_bottom, was = self.chat_follow, bar.value()
        if not at_bottom:
            self.unseen += max(0, len(shown) - self.chat_count)
        self.chat_count = len(shown)
        lines = []
        for m in shown:
            mine = m["from"] == me
            sender = s.participant(m["from"])
            helper = sender is not None and sender.get("kind") != "human"
            colour = "#4fb35a" if mine else "#a371f7" if helper else "#3e9be9"
            who = "You" if mine else html.escape(m["from"] or "?")
            to = ""
            if m["to"] not in ("all", None, me) and not (mine and f"@{m['to']}".lower() in m["body"].lower()):
                to = f" <span style='color:#8b949e'>to {html.escape(m['to'])}</span>"
            tag = " <span style='color:#8b949e'>(AI helper)</span>" if helper and not mine else ""
            status = ""
            c = states.get(m["id"])
            if c:
                status = (f" <span style='color:{ {'sent': '#e3b341', 'seen': '#d9822b', 'done': '#4fb35a'}[c['state']] }'>"
                          f"{COMMAND_WORDS[c['state']]}</span>")
            if panes.is_refusal(m["body"]):
                stamp = time.strftime('%H:%M', time.localtime(m["at"]))
                lines.append(panes.refusal_html(stamp, who, colour, m["body"], to + tag + status))
            else:
                lines.append(chat_line(m["at"], who, colour, to + tag, m["body"], status))
        for o in self.outbox:
            lines.append(chat_line(o["at"], "You", "#8b949e", "", o["body"],
                                   " <span style='color:#8b949e'>sending…</span>"))
        for n in s.notices:
            stamp = time.strftime('%H:%M', time.localtime(n["at"]))
            lines.append(f"<p style='margin:0 0 8px 0'><span style='color:#8b949e'>{stamp} "
                         f"{html.escape(n['text'])}</span></p>")
        if not lines:
            lines.append("<p style='color:#8b949e'>No messages yet. Type <b>@</b> and pick a helper to give it "
                         "something to build. It answers here as it works.</p>")
        self.chat_rendering = True
        try:
            self.chat.setHtml("".join(lines))
            # setHtml scrolls to the top: follow the newest message, or stay where the person was reading.
            bar.setValue(bar.maximum() if at_bottom or len(lines) <= 1 else was)
        finally:
            self.chat_rendering = False
        self.update_jump()

    def spin(self):
        if not self.busy_rows:
            return
        self.spin_i = (self.spin_i + 1) % len(self.SPINNER)
        for item, col, text in self.busy_rows:
            try:
                current = item.text(col)
                suffix = current.split(text, 1)[1] if text in current else ""
                item.setText(col, f"{self.SPINNER[self.spin_i]} {text}{suffix}")
            except RuntimeError:  # the row was rebuilt while we spun
                pass

    def note_activity(self, line):
        self.activity.append(line)
        if self.tabs.currentWidget() is not self.activity_page:
            self.new_activity += 1
            self.tabs.setTabText(self.tabs.indexOf(self.activity_page), f"Activity · {self.new_activity}")

    def on_event(self, ev):
        """Team events go to Activity (the chat itself is redrawn from the team's messages). Messages between
        AI helpers show there too, so people can see them working together."""
        k = ev["kind"]
        me = self.session.ident["name"] if self.session.ident else None
        stamp = f"<span style='color:#8b949e'>{time.strftime('%H:%M', time.localtime(ev['ts']))}</span>"
        if k == "message":
            sender = self.session.participant(ev["name"])
            target = self.session.participant(ev.get("to"))
            if ev["name"] == me or ev.get("to") == me:
                return  # it's in the chat
            body = html.escape(ev["body"])
            if ev.get("message_kind") == "steer":
                self.note_activity(f"{stamp} <b style='color:#d9822b'>{html.escape(ev['name'])}</b> asked "
                                   f"<b>{html.escape(ev['to'])}</b>: {body}")
            elif sender and sender.get("kind") != "human" and (ev.get("to") == "all" or
                                                                (target and target.get("kind") != "human")):
                self.note_activity(f"{stamp} 🤝 <b style='color:#a371f7'>{html.escape(ev['name'])}</b> → "
                                   f"<b>{html.escape(ev['to'])}</b>: {body}")
            return
        if k == "library" and self.library.loaded:
            self.library.reload()
        if k in ("setting_suggested", "setting_changed"):
            self.fetch_suggestions()
        if k == "op_failed" and panes.is_refusal(str(ev.get("error") or "")):
            stamp = time.strftime('%H:%M', time.localtime(ev['ts']))
            self.note_activity(panes.refusal_html(stamp, html.escape(ev.get("name") or "Someone"),
                                                 "#a371f7", str(ev.get("error") or ""),
                                                 op=ev.get("op") or ""))
            return
        line = describe_event(ev)
        if line:
            self.note_activity(f"{stamp} <span style='color:#a78bfa'>{html.escape(line)}</span>")

    # ---------------------------------------------------------- suggestions for you

    def fetch_suggestions(self):
        """Re-read what AI helpers suggested, off the GUI thread; it comes back through the event queue."""
        s = self.session
        if not s.connected or self.fetching:
            return
        self.fetching = True
        client, events = s.client, s.events
        threading.Thread(target=lambda: events.put(("suggestions", read_suggestions(client))), daemon=True).start()

    def show_suggestions(self, found):
        """One card per suggestion, in plain words, with the yes/no right there."""
        self.fetching = False
        for source, rows in found.items():
            if rows is not None:  # a source that couldn't be read keeps what it showed
                self.suggestions[source] = rows
        # The style inbox is one summary card and goes first: it is the bigger backlog.
        rows = [r for r in self.suggestions["style"] + self.suggestions["setting"] if r["key"] not in self.answered]
        if not self.changed("suggestions", tuple((r["key"], r["text"]) for r in rows)):
            return
        while self.suggest_lay.count():
            w = self.suggest_lay.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.suggest_box.setVisible(bool(rows))
        if not rows:
            return
        self.suggest_lay.addWidget(caption(f"Waiting for you · {len(rows)}", colour="#d9822b"))
        for r in rows[:SUGGESTIONS_SHOWN]:
            self.suggest_lay.addWidget(self.suggestion_row(r))
        if len(rows) > SUGGESTIONS_SHOWN:
            more = QtGui.QLabel(f"<small style='color:#8b949e'>{len(rows) - SUGGESTIONS_SHOWN} more in "
                                "Tools ▸ Precision settings</small>")
            more.setWordWrap(True)
            self.suggest_lay.addWidget(more)

    def suggestion_row(self, r):
        row = QtGui.QWidget()
        rl = QtGui.QVBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        text = QtGui.QLabel(r["text"])
        text.setWordWrap(True)
        text.setTextFormat(QtCore.Qt.RichText)
        text.setSizePolicy(QtGui.QSizePolicy.Ignored, QtGui.QSizePolicy.Preferred)
        rl.addWidget(text)
        buttons = QtGui.QHBoxLayout()
        if r["kind"] == "style_inbox":
            review = action_button(r["yes"], "Open the design rules and decide", self.open_style)
            review.setObjectName("primary")
            buttons.addWidget(review)
        else:
            no = action_button("Dismiss", "Say no; the helper is told", lambda: self.answer(r, False))
            yes = action_button(r["yes"], "Say yes; it applies to the whole team", lambda: self.answer(r, True))
            yes.setObjectName("primary")
            buttons.addWidget(no)
            buttons.addWidget(yes)
        rl.addLayout(buttons)
        return row

    def open_style(self):
        """The Team tab only says how much waits; the dialog is where the decisions are made."""
        style_ui.show_style()
        self.fetch_suggestions()

    def answer(self, r, accept):
        """Accept or dismiss a setting suggestion; it leaves the card at once and the team's list catches up."""
        self.answered.add(r["key"])
        self.show_suggestions({})
        path, body = f"/settings/suggestions/{r['id']}", {"accept": accept}
        self.session.act_async("POST", path, body, purpose="answer a suggestion",
                               done=lambda _r: (say(("Accepted: " if accept else "Dismissed: ") + r["plain"]),
                                                self.fetch_suggestions()),
                               failed=lambda: (self.answered.discard(r["key"]), self.keys.pop("suggestions", None),
                                               self.show_suggestions({})))

    # --------------------------------------------------------------- the chat box

    def maybe_complete(self, _text=""):
        prefix = mention_prefix(self.msg.text()[:self.msg.cursorPosition()])
        if prefix is None:
            self.completer.popup().hide()
            return
        self.completer.setCompletionPrefix(prefix)
        if self.completer.completionCount() == 0:
            self.completer.popup().hide()
            return
        width = max(200, self.completer.popup().sizeHintForColumn(0) + 30)
        self.completer.complete(QtCore.QRect(0, 0, min(width, self.msg.width()), self.msg.height()))

    def insert_mention(self, name):
        text, pos = self.msg.text(), self.msg.cursorPosition()
        before = re.sub(r"@[A-Za-z0-9_-]*$", "@" + name + " ", text[:pos])
        self.msg.setText(before + text[pos:])
        self.msg.setCursorPosition(len(before))
        self.completed_at = time.time()

    def send(self):
        if self.completer.popup().isVisible() or time.time() - self.completed_at < 0.3:
            return  # that Enter picked a name from the list
        text = self.msg.text().strip()
        if not text:
            return
        # Not joined yet: keep what they typed, walk them through joining, then send it.
        if not self.session.ensure_connected("send your message"):
            return
        helpers, special, unknown = parse_mentions(text, [p["name"] for p in self.session.model["participants"]
                                                          if p.get("kind") != "human"])
        if unknown and not helpers and not special:
            people = {p["name"].lower() for p in self.session.model["participants"] if p.get("kind") == "human"}
            if unknown[0].lower() not in people:
                hint(f"There's no AI helper called @{unknown[0]}. Type @ to pick one from the list.")
                return
        self.msg.clear()
        what = strip_mentions(text) or text
        if "team" in special:
            # A brief is not a chat message, so nothing will ever echo this row back. Posting it in the
            # outbox left a "sending..." bubble that never resolved (and re-sent the text as another brief);
            # a local notice in the chat says where the request went instead.
            self.session.act_async("POST", "/briefs", {"text": what}, purpose="hand the request to the team",
                                   done=lambda r: self.handed_to_team())
        elif helpers:
            for h in helpers:
                self.give_task(h, text, what)
        elif "all" in special:
            self.post(text, "command", "all")
        else:
            self.post(text, "chat", "all")
        self.render_chat(self.session)

    def handed_to_team(self):
        """The brief was accepted: say so in the chat (it is not a message) and open the tracker it lives in."""
        self.session.notice(TEAM_HANDED)
        self.show_tasks()
        say(TEAM_HANDED)

    def post(self, text, kind, to):
        self.outbox.append({"body": text, "at": time.time()})
        self.session.act_async("POST", "/messages", {"to": to, "body": text, "kind": kind},
                               purpose="send your message", failed=lambda: self.drop_outbox(text))

    def give_task(self, helper, text, what):
        """@helper ...: a task for that helper, and a message that wakes it to start on it."""
        objects = self.session.selected_names()
        title = what.splitlines()[0][:90]
        self.outbox.append({"body": text, "at": time.time()})

        def work(c):
            t = c.post("/tasks", {"title": title, "detail": what, "objects": objects, "assignee": helper})
            c.post("/messages", {"to": helper, "body": text, "kind": "command", "task_id": t["id"]})
            return t
        self.session.run_async(work, purpose="give a helper a task", failed=lambda: self.drop_outbox(text))

    def drop_outbox(self, text):
        self.outbox = [o for o in self.outbox if o["body"] != text]
        if not self.msg.text().strip():
            self.msg.setText(text)  # give the words back so nothing typed is lost
        self.render_chat(self.session)

    def mention(self, name):
        """Put @name at the start of the chat box, ready to type what it should do."""
        text = re.sub(r"^(@[A-Za-z0-9_-]+\s+)+", "", self.msg.text())
        self.msg.setText(f"@{name} {text}")
        self.msg.setFocus()
        self.msg.setCursorPosition(len(self.msg.text()))

    # ------------------------------------------------------------------- helpers

    def team_double_click(self, item, _col):
        name = item.data(0, QtCore.Qt.UserRole)
        if name:
            self.mention(name)

    def helper_menu(self, pos):
        item = self.team.itemAt(pos)
        name = item.data(0, QtCore.Qt.UserRole) if item else None
        if not name:
            return
        menu = QtGui.QMenu(self)
        menu.addAction(f"Give @{name} a task").triggered.connect(lambda: self.mention(name))
        participant = next((p for p in self.session.model["participants"] if p["name"] == name), None)
        mine = local_helpers(self.session).get(name)
        if mine:
            folder = mine[0]
            menu.addSeparator()
            menu.addAction("Rename…").triggered.connect(lambda: self.rename_helper(name, folder))
            menu.addAction("Restart").triggered.connect(lambda: self.restart_helper(folder))
            menu.addAction("Show its log").triggered.connect(lambda: open_log(folder))
        if participant and takes_instructions(participant):
            menu.addSeparator()
            menu.addAction("Remove from the team…").triggered.connect(
                lambda: self.remove_helper(participant, mine[0] if mine else None))
        (getattr(menu, "exec", None) or menu.exec_)(self.team.viewport().mapToGlobal(pos))

    def new_helper(self):
        if not self.session.ensure_connected("start an AI helper"):
            return
        dlg = NewHelperDialog(self.session)
        (getattr(dlg, "exec", None) or dlg.exec_)()

    def rename_helper(self, name, folder):
        new, ok = QtGui.QInputDialog.getText(self, "Rename helper", "New name (letters, numbers, - and _):",
                                             QtGui.QLineEdit.Normal, name)
        new = re.sub(r"[^A-Za-z0-9_-]+", "-", (new or "").strip()).strip("-_")[:40]
        if not ok or not new or new == name:
            return
        hub = helper_hub(folder)
        self.session.run_async(lambda _c: hub.rename_me(new), purpose="rename a helper",
                               done=lambda _r: renamed(folder, new))

    def restart_helper(self, folder):
        from ..paths import LAUNCHER, freecad_python
        from ..runner import runner_alive, set_background, start_runner, stop_runner
        set_background(folder, True)
        if runner_alive(folder):
            stop_runner(folder)
            QtCore.QTimer.singleShot(3500, lambda: start_runner(folder, freecad_python(), LAUNCHER,
                                                                prefs().GetString("ProjectName")))
        else:
            start_runner(folder, freecad_python(), LAUNCHER, prefs().GetString("ProjectName"))

    def remove_helper(self, participant, folder=None):
        """Take any AI helper off the team, not just this computer's: the module releases its locks and returns
        the work it held, and it can't come back on its own. `folder` is set when it runs here, so we also stop
        it and forget the folder."""
        name, pid = participant["name"], participant["id"]
        if not confirm(f"Remove {name} from the team? It stops and its locks are released."
                       + (" Its folder stays on this computer." if folder else "")):
            return
        if folder:
            from ..runner import set_background, stop_runner
            set_background(folder, False)
            stop_runner(folder)
        self.session.act_async("POST", f"/participants/{pid}/remove", None, purpose="remove a helper",
                               done=(lambda _r: forget_helper_folder(folder)) if folder
                               else f"Removed {name} from the team")

    def selected_or_hint(self, verb):
        if not self.session.ensure_connected(f"{verb} parts"):
            return []
        names_ = self.session.selected_names()
        if not names_:
            where = (f"in the “{self.session.project}” document" if self.session.project
                     else "in the shared model")
            hint(f"Select one or more parts in the 3D view first ({where}), "
                 f"then click this again to {verb} them.")
        return names_

    def lock(self):
        names_ = self.selected_or_hint("lock")
        if names_:
            self.session.act_async("POST", "/locks", {"names": names_}, done=f"Locked {names_}", purpose="lock parts")

    def release(self):
        names_ = self.selected_or_hint("release")
        if names_:
            self.session.act_async("POST", "/locks/release", {"names": names_}, done=f"Released {names_}",
                                   purpose="release parts")

    def force_release(self):
        names_ = self.selected_or_hint("force-release")
        if names_ and confirm(f"Force-release {names_}? Their owners are told; any agent may then edit them."):
            self.session.act_async("POST", "/locks/release", {"names": names_, "force": True},
                                   done=f"Force-released {names_}", purpose="release parts")

    def take_over(self):
        if not self.session.ensure_connected("take over a teammate's work"):
            return
        others = [p["name"] for p in self.session.model["participants"]
                  if p["name"] != (self.session.ident or {}).get("name")]
        if not others:
            hint("There's nobody else on the team yet.")
            return
        name, ok = QtGui.QInputDialog.getItem(self, "Take over", "Whose locks and tasks do you take over?",
                                              others, 0, False)
        if ok and confirm(f"Take over {name}'s locks and tasks?"):
            self.session.act_async("POST", "/takeover", {"target": name, "force": True},
                                   done=lambda r: say(f"Took over {r['from']}: locks {r['locks']}, tasks {r['tasks']}"),
                                   purpose="take over a teammate's work")

    # tasks
    def selected_task_id(self):
        items = self.task_list.selectedItems()
        return items[0].data(0, QtCore.Qt.UserRole) if items else None

    def selected_task(self):
        tid = self.selected_task_id()
        return next((t for t in self.session.tasks if t["id"] == tid), None)

    def selected_brief(self):
        items = self.task_list.selectedItems()
        bid = items[0].data(0, QtCore.Qt.UserRole + 1) if items else None
        return next((b for b in self.session.briefs if b["id"] == bid), None) if bid else None

    def task_selected(self):
        t = self.selected_task() if hasattr(self, "task_list") else None
        b = self.selected_brief() if hasattr(self, "task_list") else None
        me = self.session.ident["name"] if self.session.ident else None
        self.task_start.setEnabled(bool(t and t["status"] in ("open", "awaiting_takeover", "blocked")))
        self.task_mine.setEnabled(bool(t and t["status"] in ("open", "awaiting_takeover")))
        self.task_done.setEnabled(bool(t and t["status"] == "claimed" and t.get("owner_name") == me))
        self.task_cancel.setEnabled(bool((t and t["status"] not in ("done", "cancelled"))
                                         or (b and b["status"] not in ("ready", "failed", "cancelled"))))
        self.task_delete.setEnabled(bool(t and t["status"] == "open"))
        self.task_stl.setEnabled(bool(b and b.get("stl")))
        if not t and b:
            text = ("This request is cancelled." if b["status"] == "cancelled"
                    else "This request is ready. Save its STL." if b["status"] == "ready"
                    else "Cancel the whole request, or click one of its tasks.")
        elif not t:
            text = "Click a task to start it, take it yourself or see its details."
        elif t["status"] == "claimed" and t.get("owner_name") != me:
            text = f"{t.get('owner_name')} is working on it."
        elif t["status"] == "done":
            text = "This task is done. Double-click it to see the result."
        elif t["status"] == "cancelled":
            text = "This task was cancelled. Its result is kept; leave it in the Cancelled group."
        else:
            text = ""
        self.task_hint.setText(f"<small style='color:#8b949e'>{text}</small>" if text else "")

    def new_task(self):
        if not self.session.ensure_connected("create a task"):
            return
        dlg = NewTaskDialog(self.session)
        (getattr(dlg, "exec", None) or dlg.exec_)()

    def task_ask_start(self):
        t = self.selected_task()
        if not t:
            return
        helper = t.get("assignee") or t.get("owner_name")
        p = self.session.participant(helper) if helper else None
        if not p or p.get("kind") == "human":
            helpers = [x["name"] for x in self.session.model["participants"] if takes_instructions(x)]
            if not helpers:
                hint("No AI helper has joined yet. Click ＋ New helper.")
                return
            helper, ok = QtGui.QInputDialog.getItem(self, "Ask a helper to start", "Which helper should do it?",
                                                    helpers, 0, False)
            if not ok:
                return
        self.session.act_async("POST", "/messages", {"to": helper, "kind": "command", "task_id": t["id"],
                                                     "body": start_instruction(t)},
                               done=f"Asked {helper} to start “{t['title']}”", purpose="start a task")

    def task_take(self):
        t = self.selected_task()
        if t and self.session.act("POST", f"/tasks/{t['id']}/claim", {}, f"You took “{t['title']}”",
                                  purpose="take a task") is not None:
            if t.get("objects"):
                doc = mirror_document()
                Gui.Selection.clearSelection()
                for n in t["objects"]:
                    if doc is not None and doc.getObject(n):
                        Gui.Selection.addSelection(doc.Name, n)
            hint(f"“{t['title']}” is yours. Select a part and use the Tools tab ▸ Edit a part yourself to change "
                 "it; click Mark done when you're finished.")

    def task_finish(self):
        t = self.selected_task()
        if not t:
            return
        summary, ok = QtGui.QInputDialog.getText(self, "Mark done", "What did you do? (one line, optional)")
        if ok:
            self.session.act_async("POST", f"/tasks/{t['id']}/result", {"status": "done", "summary": summary or "Done"},
                                   done=f"Marked “{t['title']}” done", purpose="finish a task")

    def task_cancel_selected(self):
        """Cancel a task, or the whole request when a request row is selected."""
        t, b = self.selected_task(), self.selected_brief()
        if t:
            if not confirm(f"Cancel “{t['title']}”? The team stops working on it."):
                return
            reason, ok = QtGui.QInputDialog.getText(self, "Cancel a task", "Why? (optional, the team sees it)")
            if not ok:
                return
            self.session.act_async("POST", f"/tasks/{t['id']}/cancel", {"reason": reason.strip()},
                                   done=f"Cancelled “{t['title']}”", purpose="cancel a task")
        elif b:
            if not confirm(f"Cancel “{b['text']}”? Its unstarted tasks are cancelled too."):
                return
            self.session.act_async("POST", f"/briefs/{b['id']}/cancel", {},
                                   done=f"Cancelled “{b['text']}”", purpose="cancel the request")

    def task_delete_selected(self):
        t = self.selected_task()
        if not t or not confirm(f"Delete “{t['title']}”? It was never started."):
            return
        self.session.act_async("POST", f"/tasks/{t['id']}/delete", {},
                               done=f"Deleted “{t['title']}”", purpose="delete a task")

    def brief_save_stl(self):
        """An older request keeps its finished model reachable from the Tasks tab."""
        b = self.selected_brief()
        if not b or not b.get("stl"):
            hint("That request has no finished model yet.")
            return
        path, _ = QtGui.QFileDialog.getSaveFileName(Gui.getMainWindow(), "Save STL",
                                                    f"cadswarm-{b['id']}.stl", "STL (*.stl)")
        if not path:
            return

        def got(data):
            with open(path, "wb") as f:
                f.write(data)
            say(f"Saved {path}")

        self.session.run_async(lambda c: c.request("GET", f"/artifacts/{b['stl']}", raw=True), got,
                               purpose="save the model")

    def task_details(self):
        t = self.selected_task()
        b = self.selected_brief()
        if not t and b:
            lines = [f"<b>{html.escape(b['text'])}</b>",
                     f"Status: {BRIEF_WORDS.get(b['status'], b['status'])}",
                     f"Asked by: {html.escape(b.get('by') or '?')}",
                     f"Parts: {b.get('done', 0)} of {b.get('tasks', 0)} done"]
            if b.get("stl"):
                lines.append("A finished model is ready. Save it from Save STL…")
            box = QtGui.QMessageBox(QtGui.QMessageBox.Information, "Request", "<br>".join(lines),
                                    parent=Gui.getMainWindow())
            box.setTextFormat(QtCore.Qt.RichText)
            (getattr(box, "exec", None) or box.exec_)()
            return
        if not t:
            hint("Click a task first.")
            return
        lines = [f"<b>{html.escape(t['title'])}</b>", f"Status: {TASK_WORDS.get(t['status'], t['status'])}",
                 f"Who: {html.escape(t.get('owner_name') or t.get('assignee') or 'anyone')}",
                 f"Created by: {html.escape(t.get('created_by_name') or '?')}"]
        if t.get("objects"):
            lines.append(f"Parts: {html.escape(', '.join(t['objects']))}")
        if t.get("detail") and t["detail"] != t["title"]:
            lines.append("<br>" + html.escape(t["detail"]).replace("\n", "<br>"))
        if t.get("result"):
            lines.append(f"<br><b>Result</b> ({html.escape(t['result'].get('by') or '')}): "
                         f"{html.escape(t['result'].get('summary') or '')}")
        box = QtGui.QMessageBox(QtGui.QMessageBox.Information, "Task", "<br>".join(lines), parent=Gui.getMainWindow())
        box.setTextFormat(QtCore.Qt.RichText)
        (getattr(box, "exec", None) or box.exec_)()

    def set_param(self):
        if not self.session.ensure_connected("change a shared dimension"):
            return
        text, ok = QtGui.QInputDialog.getText(self, "Change a shared size",
                                              "name = value   (e.g. hole_pitch = 40 mm)")
        if not ok or "=" not in text:
            return
        alias, value = (x.strip() for x in text.split("=", 1))

        def work(c):
            c.post("/locks", {"names": ["Params"]})
            try:
                return c.post("/ops", {"op": "set_param", "args": {"alias": alias, "value": value}})
            finally:
                c.post("/locks/release", {"names": ["Params"]})
        self.session.run_async(work, done=lambda r: say(f"Params.{alias} = {value}; recomputed {r['recomputed']}"),
                               purpose="change a shared size")


def helper_hub(folder):
    """The team as this computer's helper in `folder` (its own sign-in), for renaming or removing it."""
    cfg = load_json(os.path.join(folder, ".cadswarm", "config.json")) or {}
    return SpaceHub(cfg, load_json(os.path.join(folder, ".cadswarm", "identity.json")) or {}, timeout=30)


def renamed(folder, name):
    path = os.path.join(folder, ".cadswarm", "config.json")
    cfg = load_json(path) or {}
    cfg["name"] = name
    save_json(path, cfg)
    say(f"Your helper is now called {name}")


def open_log(folder):
    from .wizard import open_folder
    log = os.path.join(folder, ".cadswarm", "helper.log")
    open_folder(log if os.path.exists(log) else folder)


def chat_line(at, who, colour, after_name, body, status):
    stamp = time.strftime('%H:%M', time.localtime(at))
    text = html.escape(body).replace("\n", "<br>")
    return (f"<p style='margin:0 0 8px 0'><span style='color:#8b949e'>{stamp}</span> "
            f"<b style='color:{colour}'>{who}</b>{after_name}{status}<br>{text}</p>")


CLIENT_WORDS = {"claude": "Claude Code helper", "codex": "Codex helper",
                "opencode": "OpenCode helper"}
# Readable next to the 3D view, but narrow enough for a laptop screen: the panel scrolls when it's short.
PANEL_MIN_WIDTH = 300
PANEL_WIDTH = 520
PANEL_SHARE = 0.3   # of the FreeCAD window's width, until someone drags the panel wider or narrower
# CadSwarm's own dark surface, so the panel reads the same on a light FreeCAD theme (see theme.py).
PANEL_STYLE = theme.PANEL
SUGGEST_SECONDS = 20     # how often the panel re-reads what AI helpers suggested
SUGGESTIONS_SHOWN = 4    # cards on the Team tab; any more wait in Tools
NOTICES_KEPT = 20        # local chat lines kept (a brief handed to the team, and the like)
TEAM_HANDED = "Handed to the team: it plans the work and starts AI helpers. Watch the Tasks tab."


def page_layout(page):
    lay = QtGui.QVBoxLayout(page)
    lay.setContentsMargins(0, 10, 0, 0)
    lay.setSpacing(6)
    return lay


def caption(text, colour="#8b949e"):
    """A small spaced-out heading in capitals, like WHO'S HERE."""
    label = QtGui.QLabel(text.upper())
    font = label.font()
    if font.pointSizeF() > 0:
        font.setPointSizeF(max(7.0, font.pointSizeF() * 0.85))
    font.setBold(True)
    font.setLetterSpacing(QtGui.QFont.AbsoluteSpacing, 1.0)
    label.setFont(font)
    label.setStyleSheet(f"color: {colour};")
    label.setSizePolicy(QtGui.QSizePolicy.Ignored, QtGui.QSizePolicy.Preferred)
    return label


class ShrinkButton(QtGui.QPushButton):
    """A button that may get narrower than its words. FreeCAD's theme makes Qt pin every styled button to its
    full width, which would push the panel wider than its dock."""

    def minimumSizeHint(self):
        return QtCore.QSize(24, super().minimumSizeHint().height())


def action_button(label, tip, fn):
    """A button that never makes the panel wider than its dock (on a very narrow panel its words clip)."""
    return ActionButton(label, tip, fn)


# How long a pressed button stays lit when its action starts no background work: long enough that the
# press reads as a press, short enough not to get in the way.
PRESS_FLASH_MS = 400
# How fast the little loading glyph on a busy button turns.
SPIN_SPEED_MS = 120


class ActionButton(ShrinkButton):
    """A panel button that shows it was pressed and ignores a second press while its action is running.

    Every action used to fire on every click, so hammering "Auto-constrain" opened the same "nothing to
    constrain" box once per click. A press now lights the button (and holds it lit) until the work behind
    it has finished, and clicks landing in that window are dropped: one press, one action.

    A busy button also carries a small turning loading glyph, drawn before the action runs, so a slow
    action reads as "working" rather than "the panel has frozen"."""

    def __init__(self, label, tip, fn):
        super().__init__(label)
        self._fn = fn
        self._busy = False
        self._spin_i = 0
        self.setToolTip(tip)
        self.setSizePolicy(QtGui.QSizePolicy.Ignored, QtGui.QSizePolicy.Fixed)
        self.clicked.connect(self._pressed)
        self._spin = QtCore.QTimer(self)
        self._spin.setInterval(SPIN_SPEED_MS)
        self._spin.timeout.connect(self._turn)

    def _pressed(self):
        if self._busy:
            return  # this action is already running: the extra click is not a second job
        self._busy = True
        self._set_busy(True)
        self._turn()      # put the loading glyph on the button...
        self.repaint()    # ...and draw it now, before an action that blocks the event loop freezes the panel
        self._spin.start()
        try:
            self._fn()
        except Exception as e:  # a broken action must not leave the button lit and stuck
            warn(f"{self._label().rstrip('…') or 'that action'} failed: {e}")
        if SESSION.working():
            SESSION.when_idle(self._clear_busy)
        else:
            QtCore.QTimer.singleShot(PRESS_FLASH_MS, self._clear_busy)

    def _label(self):
        """The button's own words, without the loading glyph we may have prefixed to them."""
        text = self.text()
        return text[2:] if text[:1] in TeamPanel.SPINNER and text[1:2] == " " else text

    def _turn(self):
        self._spin_i = (self._spin_i + 1) % len(TeamPanel.SPINNER)
        self.setText(f"{TeamPanel.SPINNER[self._spin_i]} {self._label()}")

    def _clear_busy(self):
        self._busy = False
        self._spin.stop()
        if self._label() != self.text():
            self.setText(self._label())  # take the loading glyph back off
        self._set_busy(False)

    def _set_busy(self, on):
        self.setProperty("busy", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.repaint()  # draw the lit state now, even when the action blocks the event loop


def qr_pixmap(rows, pixels=240, ratio=1.0):
    """The Remote tab's QR: one solid block per dark module, so no hairlines break a scan."""
    count = len(rows)
    cell = max(2, min(8, int(pixels * ratio) // max(count, 1)))
    pix = QtGui.QPixmap(count * cell, count * cell)
    pix.fill(QtGui.QColor("#ffffff"))
    painter = QtGui.QPainter(pix)
    painter.setPen(QtCore.Qt.NoPen)
    painter.setBrush(QtGui.QColor("#000000"))
    for y, row in enumerate(rows):
        for x, dark in enumerate(row):
            if dark == "1":
                painter.drawRect(QtCore.QRect(x * cell, y * cell, cell, cell))
    painter.end()
    if ratio > 1:
        pix.setDevicePixelRatio(ratio)
    return pix


def dot_colour(owner_id):
    return "#%02x%02x%02x" % tuple(int(c * 255) for c in owner_color(owner_id))


def dot_icon(rgb, size=10):
    """A round dot in someone's colour, drawn at double size so it stays crisp on high-DPI screens."""
    pix = QtGui.QPixmap(size * 2, size * 2)
    pix.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pix)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setPen(QtCore.Qt.NoPen)
    painter.setBrush(QtGui.QColor(*(int(c * 255) for c in rgb)))
    painter.drawEllipse(QtCore.QRectF(size * 0.4, size * 0.4, size * 1.2, size * 1.2))
    painter.end()
    return QtGui.QIcon(pix)


class PillDelegate(QtGui.QStyledItemDelegate):
    """Draws a status cell as a small rounded pill in the status colour (the cell's text stays the same)."""

    def paint(self, painter, option, index):
        opt = QtGui.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text, opt.text = opt.text, ""
        style = opt.widget.style() if opt.widget else QtGui.QApplication.style()
        style.drawControl(QtGui.QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        if not text:
            return
        brush = index.data(QtCore.Qt.ForegroundRole)
        colour = brush.color() if isinstance(brush, QtGui.QBrush) else opt.palette.color(QtGui.QPalette.Text)
        fm = opt.fontMetrics
        room = opt.rect.adjusted(2, 3, -4, -3)
        text = fm.elidedText(text, QtCore.Qt.ElideRight, max(0, room.width() - 18))
        width = fm.horizontalAdvance(text) + 18
        pill = QtCore.QRectF(room.right() - width + 1, room.top(), width, room.height()).adjusted(0.5, 0.5, -0.5, -0.5)
        fill, edge = QtGui.QColor(colour), QtGui.QColor(colour)
        fill.setAlpha(28)
        edge.setAlpha(120)
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(QtGui.QPen(edge, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
        painter.setPen(colour)
        painter.setFont(opt.font)
        painter.drawText(pill, QtCore.Qt.AlignCenter, text)
        painter.restore()

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        spinner = option.fontMetrics.horizontalAdvance("◐ ")  # a busy row's text gains a turning spinner
        return QtCore.QSize(size.width() + 18 + spinner, size.height() + 6)


def read_suggestions(client):
    """What AI helpers suggested that waits for a person: team-setting changes and the style inbox.
    Runs off the GUI thread; a source that can't be read comes back None, so the panel keeps what it showed."""
    found = {"setting": None, "style": None}
    try:
        data = client.get("/settings")
        specs = {s["key"]: s for s in data.get("settings") or []}
        found["setting"] = [setting_suggestion(g, specs.get(g["setting"], {})) for g in data.get("suggestions") or []]
    except Exception:  # an older team server, or offline for a moment
        pass
    try:
        box = client.post("/style/inbox", {})
        found["style"] = [style_summary(box)] if box.get("decisions") else []
    except Exception:
        pass
    return found


def setting_suggestion(g, spec):
    """A helper's suggested change to a team setting, in words anyone can say yes or no to."""
    from ..library.present import setting_value_text
    new = setting_value_text(spec, g["value"])
    now = setting_value_text(spec, g["current"]) if g.get("current") is not None else ""
    why = html.escape(g.get("reason") or "")
    if g.get("component") and g.get("component_name"):
        why += f" (for {html.escape(str(g['component_name']))})"
    return {"key": f"setting:{g['id']}", "id": g["id"], "kind": "setting", "yes": "Accept",
            "plain": f"{g['label']} → {new}",
            "text": f"<b>{html.escape(g.get('by') or 'An AI helper')}</b> suggests changing "
                    f"<b>{html.escape(g['label'])}</b>" + (f" from {html.escape(now)}" if now else "")
                    + f" to <b>{html.escape(new)}</b>"
                    + (f"<br><small style='color:#8b949e'>{why}</small>" if why else "")}


def style_summary(box):
    """The one line on the Team tab; the dialog is where the decisions are grouped and made."""
    kinds = ", ".join(f"{s['count']} {html.escape(s['label'].lower())}" for s in (box.get("sections") or [])[:3])
    ready = (f" · <b>{box['ready']} ready</b>" if box.get("ready") else "")
    text = (f"<b>{box['rules']} design rules</b> are waiting: {box['decisions']} decisions{ready}."
            + (f"<br><small style='color:#8b949e'>Mostly {kinds}</small>" if kinds else ""))
    return {"key": "style:inbox", "id": None, "kind": "style_inbox",
            "yes": f"Review {box['decisions']} decisions",
            "plain": f"{box['decisions']} design rules", "text": text}


def takes_instructions(p):
    """AI helpers and the reviewer take instructions; people and the team server itself never do."""
    return p.get("kind") not in ("human", "host")


BRIEF_WORDS = {"new": "⏳ waiting for the team server", "planning": "🧠 planning the parts",
               "building": "🔨 building", "reviewing": "🔍 checking the result", "ready": "✓ done",
               "failed": "✗ stopped (see Activity)", "cancelled": "⊘ cancelled"}
COMMAND_WORDS = {"sent": "⏳ not read yet", "seen": "👀 read, working on it", "done": "✓ done"}
TASK_ORDER = {"claimed": 0, "open": 1, "awaiting_takeover": 2, "blocked": 3, "done": 4, "cancelled": 5}
TASK_KINDS = [
    ("Build a new part", "Describe the part: what it is, rough size, what it attaches to.\n"
                         "e.g. A lid for the enclosure, 3 mm walls, that sits on the four posts"),
    ("Change an existing part", "Say which part and what to change.\n"
                                "e.g. Make the base plate 10 mm longer and keep the holes centred"),
    ("Check strength (FEA)", "Say which part, what it's made of and what load it carries.\n"
                             "e.g. Will the shelf bracket hold 10 kg? It's printed in PLA"),
    ("Find and fix clashes", "e.g. Check every part for overlaps and fix the ones in your parts"),
    ("Something else", "Describe what needs doing."),
]


def start_instruction(t):
    text = f"Please start task {t['id']} now: “{t['title']}”."
    if t.get("detail") and t["detail"] != t["title"]:
        text += f" Details: {t['detail'][:1500]}"
    if t.get("objects"):
        text += f" Parts: {', '.join(t['objects'])}."
    return text + " Claim it with claim_task, tell me in one line how you'll do it, and report_result when done."


class NewTaskDialog(QtGui.QDialog):
    """A task in one step: what kind, what exactly, who does it, and whether they start right away."""

    def __init__(self, session):
        super().__init__(Gui.getMainWindow())
        theme.apply(self)
        self.session = session
        self.setWindowTitle("CADSwarm: new task")
        fit_to_screen(self, 520, 480)
        lay = QtGui.QVBoxLayout(self)
        form = QtGui.QFormLayout()
        self.kind = QtGui.QComboBox()
        for label, _ in TASK_KINDS:
            self.kind.addItem(label)
        self.kind.currentIndexChanged.connect(self.kind_changed)
        form.addRow("What kind of task?", self.kind)
        self.what = QtGui.QPlainTextEdit()
        self.what.setMinimumHeight(110)
        form.addRow("What needs doing?", self.what)
        self.parts = QtGui.QListWidget()
        self.parts.setMaximumHeight(90)
        selected = set(session.selected_names())
        for o in session.model.get("objects", []):
            if not o.get("bbox"):
                continue
            it = QtGui.QListWidgetItem(f"{o.get('label') or o['name']}"
                                       + (f"  ({o['locked_by']})" if o.get("locked_by") else ""))
            it.setData(QtCore.Qt.UserRole, o["name"])
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.Checked if o["name"] in selected else QtCore.Qt.Unchecked)
            self.parts.addItem(it)
        if self.parts.count():
            form.addRow("Parts involved (optional)", self.parts)
        self.who = QtGui.QComboBox()
        me = session.ident["name"] if session.ident else None
        helpers = [p for p in session.model.get("participants", []) if takes_instructions(p)]
        local = local_helper_status(session)
        ready = sorted(helpers, key=lambda p: {"ready": 0, "working": 1, "away": 2}.get(
            helper_state(p, local.get(p["name"]))[0], 3))
        for p in ready:
            self.who.addItem(f"🤖 {p['name']} ({helper_state(p, local.get(p['name']))[1]})", ("helper", p["name"]))
        if len(helpers) > 1:
            self.who.addItem("🤖 Whichever helper is free first", ("any", None))
        self.who.addItem("🧠 The whole team: plan it into parts and build it", ("team", None))
        self.who.addItem("🧑 Me: I'll do it by hand", ("me", me))  # teammates pick their own tasks
        self.who.currentIndexChanged.connect(self.who_changed)
        form.addRow("Who should do it?", self.who)
        self.start = QtGui.QCheckBox("Start right away (messages the helper to begin)")
        self.start.setChecked(True)
        form.addRow("", self.start)
        lay.addLayout(form)
        self.note = QtGui.QLabel("")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        row = QtGui.QHBoxLayout()
        row.addStretch(1)
        cancel = QtGui.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.ok = QtGui.QPushButton("Create task")
        self.ok.setDefault(True)
        self.ok.setStyleSheet("QPushButton { font-weight: bold; padding: 5px 14px; }")
        self.ok.clicked.connect(self.create)
        row.addWidget(cancel)
        row.addWidget(self.ok)
        lay.addLayout(row)
        self.start.toggled.connect(lambda _on: self.who_changed(0))
        self.kind_changed(0)
        self.who_changed(0)

    def kind_changed(self, i):
        self.what.setPlaceholderText(TASK_KINDS[i][1])

    def who_changed(self, _i):
        kind, name = self.who.currentData() or ("me", None)
        helper = kind in ("helper", "any")
        self.start.setVisible(helper)
        self.ok.setText("Hand it to the team" if kind == "team" else
                        "Create and start" if helper and self.start.isChecked() else "Create task")
        p = self.session.participant(name) if name else None
        local = local_helper_status(self.session)
        if kind == "helper" and p and helper_state(p, local.get(name))[0] == "away":
            self.note.setText(f"<small style='color:#e3b341'>"
                              f"{away_tip(p, local.get(name), team_db(self.session))}</small>")
        elif kind == "team":
            self.note.setText("<small>The team server plans it into parts, starts AI helpers and builds it. "
                              "Progress shows in Tasks.</small>")
        elif kind == "me":
            self.note.setText("<small>The task is marked as yours. Edit the parts with the Tools tab ▸ Edit a part "
                              "yourself, then click Mark done.</small>")
        else:
            self.note.setText("")

    def create(self):
        text = self.what.toPlainText().strip()
        if not text:
            hint("Describe what needs doing first.")
            return
        kind, name = self.who.currentData() or ("me", None)
        if kind == "team":
            self.session.act_async("POST", "/briefs", {"text": text}, purpose="hand the request to the team",
                                   done=lambda r: (self.session.notice(TEAM_HANDED), say(TEAM_HANDED)))
            self.accept()
            return
        title = text.splitlines()[0][:90]
        if TASK_KINDS[self.kind.currentIndex()][0] != "Something else" and len(title) < 60 \
                and not title.lower().startswith(("check", "build", "make", "change", "find", "will")):
            title = f"{TASK_KINDS[self.kind.currentIndex()][0]}: {title}"
        objects = [self.parts.item(i).data(QtCore.Qt.UserRole) for i in range(self.parts.count())
                   if self.parts.item(i).checkState() == QtCore.Qt.Checked]
        t = self.session.act("POST", "/tasks", {"title": title, "detail": text, "objects": objects,
                                               "assignee": name if kind == "helper" else None},
                             purpose="create a task")
        if t is None:
            return
        if kind == "me":
            self.session.act("POST", f"/tasks/{t['id']}/claim", {}, purpose="take the task")
        elif kind in ("helper", "any") and self.start.isChecked():
            self.session.act("POST", "/messages", {"to": name or "all", "kind": "command", "task_id": t["id"],
                                                   "body": start_instruction(t)}, purpose="start the task")
            if self.session.panel:
                self.session.panel.show_tasks()
        say(f"Created task “{title}”")
        self.accept()


def helper_state(p, local=None):
    """('working' | 'ready' | 'away' | 'here', words) for one participant. `local` is the runner status of a
    helper that runs on this computer (see runner.py), which knows more than the team does."""
    local = local or {}
    if local.get("state") == "working":
        return "working", f"Working: {local.get('detail') or 'on your messages'}"[:70]
    if local.get("state") == "error" and p.get("status") == "active":
        return "ready", "Ready (last run failed, see its log)"
    if p.get("status") != "active":
        if local.get("state") in ("starting", "listening"):
            return "ready", "Starting up…"
        return "away", STATUS_WORDS.get(p["status"], p["status"])
    if p.get("kind") == "human":
        return "here", "here"
    task = p.get("task")
    doing = (p.get("doing") or "").strip()
    recent = p.get("idle_s") is not None and p["idle_s"] < 60
    if task:
        return "working", f"Working on “{task['title'][:48]}”"
    if recent and doing and doing not in IDLE_WORDS:
        return "working", f"Working: {doing[:48]}"
    return "ready", "Ready for instructions"


def helper_row(p, local, tasks):
    """(state, task, status) for a helper's row: the task it's on (or the next one waiting for it) and a short
    status word."""
    state, text = helper_state(p, local)
    task = (p.get("task") or {}).get("title")
    if not task:
        waiting = [t for t in tasks if t["status"] == "open" and (t.get("assignee") or "").lower() == p["name"].lower()]
        if waiting:
            task = f"next: {waiting[0]['title']}"
    if text.startswith("Working on"):
        status = "Working"
    elif text.startswith("Working: ") and not task:
        task, status = text[len("Working: "):], "Working"
    else:
        status = {"Ready for instructions": "Ready"}.get(text, text)
    return state, task or "", status


ANSI = re.compile(r"\x1b\[[0-9;]*m")
FAILED = re.compile(r"\b(error|failed|failure|incorrect|invalid|denied|forbidden|unauthorized)\b"
                    r"|not\s+(been\s+)?trusted|session limit|api key", re.IGNORECASE)


def host_agent_run(name, db=""):
    """The run folder this computer's host started for `name`, or "" when it did not start it here. The host
    keeps one folder per start in <its data dir>/agents/, beside the people's helpers. `db` is the team the
    panel is showing: prefs can still name a team opened earlier (or a dead one)."""
    folder = host_dir_for(db or prefs().GetString("Db"))
    run = os.path.join(folder, "agents", name) if folder else ""
    return run if run and os.path.isfile(os.path.join(run, ".cadswarm", "config.json")) else ""


def last_log_line(run):
    """The last meaningful line of a run's agent.log, without colour codes, or ""."""
    try:
        with open(os.path.join(run, "agent.log"), encoding="utf-8", errors="replace") as f:
            lines = [ANSI.sub("", line).strip() for line in f if line.strip()]
    except OSError:
        return ""
    return lines[-1][:160] if lines else ""


def away_tip(p, local, db=""):
    """Why an AI helper is away, and what (if anything) to do about it. `db` is the live team, so the tip
    finds a host agent even when prefs still name the team this computer opened before."""
    n = html.escape(p["name"])
    state = (local or {}).get("state")
    if local is None:
        run = host_agent_run(p["name"], db)
        if run:
            # The host starts a planner/worker/reviewer on this computer; it is not one of the people's
            # helpers, so it has no runner of its own and stops between jobs. Never call it a teammate's.
            tip = (f"<b>{n}</b> runs on this computer: your team's host starts it for each brief. "
                   "It is not running now; the next task starts it again.")
            # Show the last line whatever it says: a CLI that dies before it can say "Error:" still names
            # its reason there ("Incorrect API key provided", "workspace has not been trusted").
            last = last_log_line(run)
            if last:
                word = "Its last start failed" if FAILED.search(last) else "Its log ends with"
                tip += f" {word}: {html.escape(last)}"
            return tip
        return (f"<b>{n}</b> runs on a teammate's computer. It answers once that computer has FreeCAD and "
                "CADSwarm open; your messages wait for it.")
    if state == "missing":
        return f"<b>{n}</b> can't start: {html.escape(local.get('detail') or 'its AI helper is not installed')}."
    if state == "paused":
        return f"<b>{n}</b> is {html.escape(local.get('detail') or 'paused')}. It carries on within the hour."
    if state == "gone":
        return f"<b>{n}</b> is no longer on the team. Start a new one with ＋ New helper."
    if state == "stopped" and (local or {}).get("off"):
        return (f"<b>{n}</b> is set to run in a terminal. Open it there, or use the Tools tab ▸ Restart my AI "
                "helpers to run it in the background again.")
    if state == "stopped":
        # The runner process is gone (an upgrade, a crash, or FreeCAD was closed): it will not come back on
        # its own, so this must not say "starting" and leave the person waiting.
        return (f"<b>{n}</b> isn't running: nothing on this computer is watching for your messages. The Tools "
                "tab ▸ Restart my AI helpers starts it again.")
    if state == "offline":
        return (f"<b>{n}</b> is running here but can't reach the team right now; it keeps retrying. Check this "
                "computer's connection to the team.")
    if state == "error":
        return (f"<b>{n}</b> is here, but {html.escape((local or {}).get('detail') or 'its last run failed')}. "
                "If its AI isn't signed in yet, open it once in a terminal; the Tools tab ▸ Show my AI "
                "helpers' logs has the rest.")
    if state not in ("starting", "listening", None, ""):
        return (f"<b>{n}</b> is reported as {html.escape(str(state))} on this computer. The Tools tab ▸ Restart "
                "my AI helpers starts it again.")
    return f"<b>{n}</b> is starting on this computer. It answers in the Chat in a moment."


def local_helper_folders():
    """Folders of the AI helpers set up on this computer (the guide adds each one it connects)."""
    p = prefs()
    folders = [f for f in p.GetString("AgentFolders").split("\n") if f]
    one = p.GetString("AgentFolder")
    if one and one not in folders:
        folders.append(one)
    return [f for f in folders if os.path.isfile(os.path.join(f, ".cadswarm", "config.json"))]


def remember_helper_folder(folder):
    folders = [f for f in prefs().GetString("AgentFolders").split("\n") if f and f != folder]
    prefs().SetString("AgentFolders", "\n".join(folders + [folder]))
    prefs().SetString("AgentFolder", folder)


def team_db(session):
    """The team the panel is showing: its live client's database, else the team this computer opened last."""
    client = getattr(session, "client", None)
    return (client.cfg.get("db") if client else None) or prefs().GetString("Db")


def local_helpers(session):
    """{name: (folder, config)} for this computer's AI helpers on the team `session` is in."""
    out = {}
    db = team_db(session)
    for folder in local_helper_folders():
        cfg = load_json(os.path.join(folder, ".cadswarm", "config.json")) or {}
        if cfg.get("db") == db and cfg.get("name"):
            out[cfg["name"]] = (folder, cfg)
    return out


def local_helper_status(session):
    from ..runner import background_on, runner_status
    return {name: dict(runner_status(folder), off=not background_on(folder))
            for name, (folder, _cfg) in local_helpers(session).items()}


def start_local_helpers(session, restart=False):
    """Keep this computer's AI helpers running in the background: no terminal to open, nothing to paste.
    A helper whose CLI isn't installed is left alone (the panel says so)."""
    from ..agent_setup import agent_ready
    from ..paths import LAUNCHER, freecad_python
    from ..runner import background_on, runner_alive, set_background, start_runner, stop_runner
    started = []
    for name, (folder, cfg) in local_helpers(session).items():
        if not agent_ready(cfg.get("client") or "claude"):
            continue
        if restart:
            set_background(folder, True)
        elif not background_on(folder):
            continue  # someone runs it in a terminal
        try:
            if restart and runner_alive(folder):
                stop_runner(folder)
                # Wait until it has really stopped (it checks for the stop file between polls): starting
                # the new one while the old still looks alive used to leave two runners on one helper.
                deadline = time.time() + 10
                while runner_alive(folder) and time.time() < deadline:
                    time.sleep(0.25)
            if start_runner(folder, freecad_python(), LAUNCHER, prefs().GetString("ProjectName")):
                started.append(name)
        except Exception as e:
            warn(f"couldn't start {name}: {e}")
    if started:
        say(f"Started {', '.join(started)} in the background")
    return started


def forget_helper_folder(folder):
    folders = [f for f in prefs().GetString("AgentFolders").split("\n") if f and f != folder]
    prefs().SetString("AgentFolders", "\n".join(folders))
    if prefs().GetString("AgentFolder") == folder:
        prefs().SetString("AgentFolder", folders[-1] if folders else "")
    say(f"Removed the helper in {folder} from the team")


def helpers_base():
    """Where this computer keeps its AI helpers' folders: next to (never inside) the shared files/."""
    from ..paths import projects_dir
    from .wizard import safe_folder
    return host_dir_for(prefs().GetString("Db")) or os.path.join(projects_dir(),
                                                                 safe_folder(prefs().GetString("ProjectName")))


def create_helper(client, name, team, model=""):
    """Set up a new AI helper folder signed in to the team as `name` (a plain member) and return the folder.
    Runs off the GUI thread: the first one may download the helper software (about 70 MB). `model` pins
    the helper's model ("" = the AI's own default)."""
    from .. import hosting
    from ..agent_setup import trust_in_codex, write_agent_config, write_launcher
    from ..invite import read_invite
    from ..paths import freecad_python
    python = freecad_python()
    if not hosting.deps_ready(python):
        hosting.install_deps(python)
    base = os.path.join(helpers_base(), "AI helpers", name)
    folder, n = base, 2
    while os.path.exists(folder):
        folder, n = f"{base}-{n}", n + 1
    project = prefs().GetString("ProjectName")
    invite = read_invite(team.create_invite("agent", "member", 86400, project))
    write_agent_config(folder, client, invite, name, python=python, model=model)
    write_launcher(folder, client, project)
    if client == "codex":
        trust_in_codex(folder)
    return folder


MODEL_PICKER_CLIENTS = ("opencode",)
MODEL_DEFAULT_TEXT = "Default (the AI's own choice)"


def list_models_for(client):
    from ..agent_setup import list_models
    return list_models(client)


class NewHelperDialog(QtGui.QDialog):
    """Start one more AI helper on this computer: pick the AI, optionally name it, done. It runs in the
    background and answers in the chat."""

    def __init__(self, session):
        super().__init__(Gui.getMainWindow())
        theme.apply(self)
        from ..agent_setup import CLIENTS, agent_ready
        self.session = session
        self.setWindowTitle("CADSwarm: new AI helper")
        fit_to_screen(self, 460, 260)
        lay = QtGui.QVBoxLayout(self)
        form = QtGui.QFormLayout()
        self.client = QtGui.QComboBox()
        installed = []
        for key, info in CLIENTS.items():
            ready = agent_ready(key)
            if ready:
                installed.append(key)
            if ready:
                label_ = info["label"] + (f" — {info['description']}" if info.get("description") else "")
                self.client.addItem(label_, key)
        try:
            self.client.activated[int].connect(lambda *_: self.picked_client())
        except (KeyError, TypeError, IndexError):
            self.client.activated.connect(lambda *_: self.picked_client())
        form.addRow("AI", self.client)
        # OpenCode reaches many models: the pick is saved in the helper's folder and sticks.
        self.model = QtGui.QComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QtGui.QComboBox.NoInsert)
        self.model.setToolTip("The model this helper always runs on. Type a model id if it isn't listed.")
        self.model_label = QtGui.QLabel("Model")
        form.addRow(self.model_label, self.model)
        self.models_for = None
        self.name = QtGui.QLineEdit()
        self.name.setPlaceholderText("Optional")
        self.name.setMaxLength(40)
        form.addRow("Name", self.name)
        lay.addLayout(form)
        self.note = QtGui.QLabel("")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        lay.addStretch(1)
        row = QtGui.QHBoxLayout()
        row.addStretch(1)
        cancel = QtGui.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.ok = QtGui.QPushButton("Start helper")
        self.ok.setDefault(True)
        self.ok.clicked.connect(self.create)
        row.addWidget(cancel)
        row.addWidget(self.ok)
        lay.addLayout(row)
        if not installed:
            links = " or ".join(f"<a href='{i['install_url']}'>{i['label']}</a>" for i in CLIENTS.values())
            self.note.setText(f"No AI helper is installed on this computer yet. Install {links}, sign in to it "
                              "once, then try again.")
            self.note.setOpenExternalLinks(True)
        self.show_models()

    def picked_client(self):
        self.show_models()

    def show_models(self):
        """The model picker, for the AIs that offer a choice. Starts on the last model picked for this AI
        and fills in the full list in the background (`opencode models` can take a few seconds)."""
        client = self.client.currentData()
        pickable = client in MODEL_PICKER_CLIENTS
        self.model.setVisible(pickable)
        self.model_label.setVisible(pickable)
        if not pickable or self.models_for == client:
            return
        self.models_for = client
        self.fill_models(client, [])

        def listed(models):
            try:
                if self.models_for == client:
                    self.fill_models(client, models or [], keep_typed=True)
            except RuntimeError:  # the dialog was closed before the list arrived
                pass
        self.session.run_async(lambda _c: list_models_for(client), done=listed, purpose="list the models")

    def fill_models(self, client, models, keep_typed=False):
        last = prefs().GetString(f"HelperModel_{client}")
        typed = self.model.currentText().strip() if keep_typed else ""
        self.model.clear()
        self.model.addItem(MODEL_DEFAULT_TEXT, "")
        for m in ([last] if last and last not in models else []) + list(models):
            self.model.addItem(m, m)
        pick = typed if typed and typed != MODEL_DEFAULT_TEXT else last
        i = self.model.findData(pick) if pick else 0
        if i >= 0:
            self.model.setCurrentIndex(i)
        else:
            self.model.setEditText(pick)

    def picked_model(self):
        if self.client.currentData() not in MODEL_PICKER_CLIENTS:
            return ""
        text = self.model.currentText().strip()
        return "" if text == MODEL_DEFAULT_TEXT else text

    def create(self):
        client = self.client.currentData()
        typed = re.sub(r"[^A-Za-z0-9_-]+", "-", self.name.text().strip()).strip("-_")[:40]
        taken = {p["name"].lower() for p in self.session.model.get("participants", [])}
        if typed and typed.lower() in taken:
            self.note.setText(f"<span style='color:#ff7b72'>Someone on the team is already called {typed}.</span>")
            return
        name = typed
        if not name:
            me = re.sub(r"[^a-z0-9]+", "-", ((self.session.ident or {}).get("name") or "me").lower()).strip("-")
            name, n = f"{me}-{client}"[:36], 2
            while name.lower() in taken:
                name, n = f"{me}-{client}-{n}"[:40], n + 1
        self.ok.setEnabled(False)
        self.note.setText("Setting it up… The first helper on a computer downloads its software (about 70 MB), "
                          "which can take a minute or two.")
        team = self.session.client
        model = self.picked_model()
        prefs().SetString(f"HelperModel_{client}", model)  # the next new helper of this AI starts on it

        def started(folder):
            from ..paths import LAUNCHER, freecad_python
            from ..runner import start_runner
            remember_helper_folder(folder)
            start_runner(folder, freecad_python(), LAUNCHER, prefs().GetString("ProjectName"))
            say(f"Started {name} in the background")
            if self.session.panel:
                self.session.panel.mention(name)
            self.accept()

        def failed():
            self.ok.setEnabled(True)
            self.note.setText("")
        self.session.run_async(lambda _c: create_helper(client, name, team, model), done=started,
                               purpose="start an AI helper", failed=failed)


def restart_helpers():
    if not SESSION.ensure_connected("restart your AI helpers"):
        return
    started = start_local_helpers(SESSION, restart=True)
    hint(f"Restarted {', '.join(started)}." if started else
         "No AI helper is set up on this computer yet. Click ＋ New helper in the CADSwarm panel.")


def show_helper_log():
    helpers = local_helpers(SESSION)
    if not helpers:
        hint("No AI helper is set up on this computer yet. Click ＋ New helper in the CADSwarm panel.")
        return
    from .wizard import open_folder
    for _name, (folder, _cfg) in helpers.items():
        log = os.path.join(folder, ".cadswarm", "helper.log")
        open_folder(log if os.path.exists(log) else folder)


IDLE_WORDS = {"idle", "starting", "watching in FreeCAD", "listening for instructions"}


class EditPartDialog(QtGui.QDialog):
    """Edit one part by hand. While it's open the part is held by you, so no helper can change it under you.
    Type new sizes and positions, or move the part in the 3D view (Edit ▸ Placement, Transform): the dialog
    picks those moves up. Save sends the change to the team; Done gives the part back."""

    def __init__(self, session, name):
        super().__init__(Gui.getMainWindow())
        theme.apply(self)
        self.session, self.name = session, name
        self.decimals, self.step = library_ui.editor_precision(session)  # the team's editing precision
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.Tool)
        self.info = session.client.get(f"/objects/{name}")
        self.was_mine = self.info_owner() == session.ident["name"]
        if not self.was_mine:
            session.client.post("/locks", {"names": [name]})
        session.editing[name] = self
        self.setWindowTitle(f"Editing {self.info.get('label') or name}")
        lay = QtGui.QVBoxLayout(self)
        head = QtGui.QLabel(f"<b>Editing {html.escape(self.info.get('label') or name)}</b><br><small>Only you can "
                            "change it while this is open. Change sizes or position, then click <b>Save to team</b>."
                            "</small>")
        head.setWordWrap(True)
        lay.addWidget(head)
        self.fields = {}
        dims = EDITABLE_DIMENSIONS.get(self.info.get("type"), [])
        bound = self.info.get("expressions") or {}
        if dims:
            box = QtGui.QGroupBox("Size")
            form = QtGui.QFormLayout(box)
            for prop, unit in dims:
                spin = self.spinbox(unit)
                spin.setValue(number(self.info["properties"].get(prop)))
                if prop in bound:
                    spin.setEnabled(False)
                    spin.setToolTip(f"Set by the shared size {bound[prop]}. Change it with Change a shared size…")
                    form.addRow(f"{prop} (uses {bound[prop]})", spin)
                else:
                    form.addRow(prop, spin)
                self.fields[prop] = spin
            lay.addWidget(box)
        else:
            note = QtGui.QLabel("<small>This part is built from other parts (or by a script), so you can move it "
                                "but its size comes from what it's made of.</small>")
            note.setWordWrap(True)
            lay.addWidget(note)
        box = QtGui.QGroupBox("Position")
        form = QtGui.QFormLayout(box)
        self.pos = {}
        for axis in "XYZ":
            self.pos[axis] = self.spinbox("mm")
            form.addRow(f"{axis}", self.pos[axis])
        self.rot = {}
        for key, label in (("yaw", "Turn around Z"), ("pitch", "Tilt around Y"), ("roll", "Roll around X")):
            self.rot[key] = self.spinbox("°", -360, 360)
            form.addRow(label, self.rot[key])
        lay.addWidget(box)
        tip = QtGui.QLabel("<small>You can also drag it: select it and use Edit ▸ Placement… or Edit ▸ Transform. "
                           "Moves you make in the 3D view show up here.</small>")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        self.state = QtGui.QLabel("")
        lay.addWidget(self.state)
        row = QtGui.QHBoxLayout()
        self.revert_b = QtGui.QPushButton("Undo my changes")
        self.revert_b.clicked.connect(self.revert)
        self.save_b = QtGui.QPushButton("Save to team")
        self.save_b.setStyleSheet("QPushButton { font-weight: bold; padding: 5px 12px; }")
        self.save_b.clicked.connect(self.save)
        done = QtGui.QPushButton("Done")
        done.clicked.connect(self.close)
        row.addWidget(self.revert_b)
        row.addStretch(1)
        row.addWidget(self.save_b)
        row.addWidget(done)
        lay.addLayout(row)
        self.load_placement(self.hub_placement())
        for w in list(self.fields.values()) + list(self.pos.values()) + list(self.rot.values()):
            w.valueChanged.connect(self.typed)
        self.watch = QtCore.QTimer(self)
        self.watch.timeout.connect(self.watch_mirror)
        self.watch.start(300)
        self.update_state()

    def spinbox(self, unit, low=-100000, high=100000):
        s = QtGui.QDoubleSpinBox()
        s.setRange(low, high)
        s.setDecimals(self.decimals)
        s.setSingleStep(self.step if unit != "°" else 1.0)
        s.setSuffix(f" {unit}")
        s.setKeyboardTracking(False)
        return s

    def info_owner(self):
        o = next((x for x in self.session.model.get("objects", []) if x["name"] == self.name), {})
        return o.get("locked_by")

    def hub_placement(self):
        pl = self.info.get("placement") or {"base": [0, 0, 0], "rotation": {"axis": [0, 0, 1], "angle": 0}}
        r = pl["rotation"]
        return App.Placement(App.Vector(*pl["base"]), App.Rotation(App.Vector(*r["axis"]), r["angle"]))

    def load_placement(self, pl):
        for w in list(self.pos.values()) + list(self.rot.values()):
            w.blockSignals(True)
        for axis, v in zip("XYZ", (pl.Base.x, pl.Base.y, pl.Base.z)):
            self.pos[axis].setValue(v)
        y, p, r = pl.Rotation.getYawPitchRoll()
        self.rot["yaw"].setValue(y)
        self.rot["pitch"].setValue(p)
        self.rot["roll"].setValue(r)
        for w in list(self.pos.values()) + list(self.rot.values()):
            w.blockSignals(False)

    def typed_placement(self):
        return App.Placement(App.Vector(*(self.pos[a].value() for a in "XYZ")),
                             App.Rotation(self.rot["yaw"].value(), self.rot["pitch"].value(), self.rot["roll"].value()))

    def mirror_obj(self):
        doc = mirror_document()
        return doc.getObject(self.name) if doc else None

    def typed(self, *_):
        # preview a typed move in the live view: same change applied to the mirror copy
        obj, base = self.mirror_obj(), self.session.synced_pl.get(self.name)
        if obj is not None and base is not None:
            delta = self.typed_placement().multiply(self.hub_placement().inverse())
            obj.Placement = delta.multiply(base)
            self._last_seen = obj.Placement
        self.update_state()

    def watch_mirror(self):
        # a move made with FreeCAD's own tools (Placement, Transform, dragging) lands here
        obj, base = self.mirror_obj(), self.session.synced_pl.get(self.name)
        if obj is None or base is None:
            return
        if getattr(self, "_last_seen", None) is not None and same_placement(obj.Placement, self._last_seen):
            return
        self._last_seen = obj.Placement
        delta = obj.Placement.multiply(base.inverse())
        self.load_placement(delta.multiply(self.hub_placement()))
        self.update_state()

    def changes(self):
        props = {}
        for prop, spin in self.fields.items():
            if spin.isEnabled() and abs(spin.value() - number(self.info["properties"].get(prop))) > 1e-6:
                props[prop] = spin.value()
        moved = not same_placement(self.typed_placement(), self.hub_placement())
        return props, moved

    def update_state(self):
        props, moved = self.changes()
        bits = ([f"size ({', '.join(props)})"] if props else []) + (["position"] if moved else [])
        self.state.setText(f"<small style='color:#d9822b'>Not saved yet: {', '.join(bits)}</small>" if bits else
                           "<small style='color:#4fb35a'>Everyone sees the same as you.</small>")
        self.save_b.setEnabled(bool(bits))
        self.revert_b.setEnabled(bool(bits))

    def save(self):
        props, moved = self.changes()
        ok = True
        if props:
            ok = self.session.act("POST", "/ops", {"op": "set_props", "args": {"name": self.name, "props": props}},
                                  purpose="save your change") is not None
        if ok and moved:
            pl = self.typed_placement()
            y, p, r = pl.Rotation.getYawPitchRoll()
            ok = self.session.act("POST", "/ops", {"op": "set_placement", "args": {"name": self.name, "placement": {
                "base": [pl.Base.x, pl.Base.y, pl.Base.z], "rotation": {"ypr": [y, p, r]}}}},
                                  purpose="save your change") is not None
        if ok:
            self.info = self.session.client.get(f"/objects/{self.name}")
            self._last_seen = None
            say(f"Saved your change to {self.name}; the team sees it now")
        self.update_state()

    def revert(self):
        obj, base = self.mirror_obj(), self.session.synced_pl.get(self.name)
        if obj is not None and base is not None:
            obj.Placement = base
            self._last_seen = obj.Placement
        for prop, spin in self.fields.items():
            spin.blockSignals(True)
            spin.setValue(number(self.info["properties"].get(prop)))
            spin.blockSignals(False)
        self.load_placement(self.hub_placement())
        self.update_state()

    def closeEvent(self, ev):
        props, moved = self.changes()
        if props or moved:
            answer = QtGui.QMessageBox.question(
                self, "CADSwarm", "Save your change to the team before you stop editing?",
                QtGui.QMessageBox.Save | QtGui.QMessageBox.Discard | QtGui.QMessageBox.Cancel)
            if answer == QtGui.QMessageBox.Cancel:
                ev.ignore()
                return
            if answer == QtGui.QMessageBox.Save:
                self.save()
            else:
                self.revert()
        self.watch.stop()
        self.session.editing.pop(self.name, None)
        if not self.was_mine and self.session.client:
            try:
                self.session.client.post("/locks/release", {"names": [self.name]})
            except HubRequestError:
                pass
        self.session.request_refresh()
        super().closeEvent(ev)


EDITABLE_DIMENSIONS = {
    "Part::Box": [("Length", "mm"), ("Width", "mm"), ("Height", "mm")],
    "Part::Cylinder": [("Radius", "mm"), ("Height", "mm"), ("Angle", "°")],
    "Part::Sphere": [("Radius", "mm")],
    "Part::Cone": [("Radius1", "mm"), ("Radius2", "mm"), ("Height", "mm")],
    "Part::Torus": [("Radius1", "mm"), ("Radius2", "mm")],
}


def number(value):
    """A property value from the hub ('120.0 mm', 120, 'Quantity(...)') as a float."""
    if isinstance(value, (int, float)):
        return float(value)
    import re
    m = re.search(r"-?\d+(?:\.\d+)?(?:e-?\d+)?", str(value or ""))
    return float(m.group()) if m else 0.0


def same_placement(a, b, tol=1e-6):
    ma, mb = a.toMatrix(), b.toMatrix()
    return all(abs(x - y) <= tol for x, y in zip(ma.A, mb.A))


def edit_part():
    if not SESSION.ensure_connected("edit a part"):
        return
    names_ = SESSION.selected_names()
    if len(names_) != 1:
        parts = [o for o in SESSION.model.get("objects", []) if o.get("bbox")]
        if not parts:
            hint("There are no parts in the shared model yet.")
            return
        labels = [f"{o.get('label') or o['name']}" + (f"  (held by {o['locked_by']})" if o.get("locked_by") else "")
                  for o in parts]
        choice, ok = QtGui.QInputDialog.getItem(Gui.getMainWindow(), "Edit a part",
                                                "Which part? (Tip: click it in the 3D view first.)", labels, 0, False)
        if not ok:
            return
        names_ = [parts[labels.index(choice)]["name"]]
    name = names_[0]
    if name in SESSION.editing:
        SESSION.editing[name].raise_()
        return
    obj = next((o for o in SESSION.model.get("objects", []) if o["name"] == name), {})
    holder, me = obj.get("locked_by"), SESSION.ident["name"]
    if holder and holder != me:
        p = SESSION.participant(holder) or {}
        box = QtGui.QMessageBox(QtGui.QMessageBox.Question, "CADSwarm",
                                f"{obj.get('label') or name} is held by {holder}, so only they can change it right now.",
                                parent=Gui.getMainWindow())
        ask = box.addButton(f"Ask {holder} to let go", QtGui.QMessageBox.AcceptRole)
        force = box.addButton("Free it anyway", QtGui.QMessageBox.DestructiveRole)
        box.addButton(QtGui.QMessageBox.Cancel)
        (getattr(box, "exec", None) or box.exec_)()
        if box.clickedButton() == ask:
            kind = "chat" if p.get("kind") == "human" else "command"
            SESSION.act("POST", "/messages", {"to": holder, "kind": kind,
                                              "body": f"Please release {name} so I can edit it by hand."},
                        f"Asked {holder} to let go of {name}", purpose="ask for a part")
            return
        if box.clickedButton() != force or not confirm(
                f"Free {name} from {holder}? They're told, and their next change to it will be refused."):
            return
        if SESSION.act("POST", "/locks/release", {"names": [name], "force": True}, purpose="free the part") is None:
            return
    try:
        dlg = EditPartDialog(SESSION, name)
    except HubRequestError as e:
        hint(friendly_error(e))
        return
    dlg.show()


STATUS_WORDS = {"active": "here", "stale": "away", "left": "left", "removed": "removed"}
TASK_WORDS = {"open": "not started", "claimed": "in progress", "awaiting_takeover": "needs someone",
              "done": "done ✓", "blocked": "stuck", "cancelled": "cancelled"}


def who_is(p):
    if p.get("kind") == "human":
        return "person"
    return CLIENT_WORDS.get(p.get("client"), "AI helper")


def names(items):
    items = [i for i in items if i != "Params"]
    return ", ".join(items[:4]) + (f" and {len(items) - 4} more" if len(items) > 4 else "")


def describe_event(ev):
    """One friendly sentence per team event (None = not worth showing)."""
    k, who = ev["kind"], ev.get("name") or ev.get("taker") or "Someone"
    if k == "op":
        bits = []
        if ev.get("created"):
            bits.append(f"added {names(ev['created'])}")
        changed = [n for n in ev.get("modified", []) if n != "Params"]
        if changed:
            bits.append(f"changed {names(changed)}")
        if ev.get("deleted"):
            bits.append(f"removed {names(ev['deleted'])}")
        if "Params" in ev.get("modified", []):
            bits.append("changed a shared size")
        if ev.get("recomputed"):
            bits.append(f"which updated {names(ev['recomputed'])}")
        return f"{who} {', '.join(bits)}" if bits else None
    if k == "op_failed":
        return f"{who}'s {ev.get('op') or 'change'} failed: {ev.get('error') or ev.get('code') or 'unknown'}"
    if k == "joined":
        kind = "person" if ev.get("participant_kind") == "human" else CLIENT_WORDS.get(ev.get("client"), "AI helper")
        return f"{who} ({kind}) joined the team"
    if k == "left":
        return f"{who} left the team"
    if k == "removed":
        return f"{who} was removed from the team"
    if k == "stale":
        held = names(ev.get("locks") or [])
        return f"{who} went quiet" + (f" while working on {held}" if held else "") + \
            ". A teammate can take over their work (the Tools tab ▸ Take over)"
    if k == "renamed":
        return f"{ev.get('from')} is now called {ev.get('name')}"
    if k == "resumed":
        return f"{who} is back"
    if k == "taken_over":
        return f"{ev.get('taker', who)} took over {ev.get('target')}'s work"
    if k == "task":
        return f"New task: “{ev.get('title')}”" + (f" for {ev['assignee']}" if ev.get("assignee") else "")
    if k == "task_claimed":
        return f"{who} started a task"
    if k == "task_reported":
        return f"{who} finished a task ✓" if ev.get("status") == "done" else f"{who} is stuck on a task"
    if k == "file_saved":
        return f"{who} {'updated' if ev.get('replaced') else 'saved'} files/{ev.get('path')}"
    if k == "file_moved":
        return f"{who} moved files/{ev.get('from')} to files/{ev.get('to')}"
    if k == "file_deleted":
        return f"{who} deleted files/{ev.get('path')} (it can be restored)"
    if k == "snapshot":
        return f"{who} saved a snapshot of the model ({ev.get('file')})"
    if k == "exported":
        return f"{who} saved a 3D file ({ev.get('file')})"
    if k == "fea_started":
        what = {"static": "a strength check", "modal": "a vibration check", "thermal": "a heat check"}
        return f"{who} started {what.get(ev.get('analysis'), 'a simulation')} of {names(ev.get('target') or [])}…"
    if k == "fea_report":
        return f"{who} published a simulation report: {ev.get('title')}. {ev.get('headline')} " \
               "(the Tools tab ▸ Simulation reports)"
    if k == "fea_progress":
        return f"{who}'s simulation: {ev.get('text')}"
    if k == "fea_failed":
        return f"{who}'s simulation of {names(ev.get('target') or [])} failed: {ev.get('error')}"
    if k == "library":
        bits = []
        if ev.get("added"):
            bits.append(f"added {names(ev['added'])} to the components library")
        if ev.get("removed"):
            bits.append(f"removed {names(ev['removed'])} from the library")
        return f"{who} {' and '.join(bits)}" if bits else None
    if k == "setting_changed":
        return f"{who} {'reset' if ev.get('reset') else 'set'} the team setting {ev.get('setting')} to " \
               f"{ev.get('value')}{(' ' + ev['unit']) if ev.get('unit') else ''}"
    if k == "setting_suggested":
        return f"{who} suggests {ev.get('setting')} = {ev.get('value')}: {ev.get('reason')} " \
               "(accept or dismiss it in Precision settings)"
    return None


def hint(text):
    QtGui.QMessageBox.information(Gui.getMainWindow(), "CADSwarm", text)


def confirm(text):
    return QtGui.QMessageBox.question(Gui.getMainWindow(), "CADSwarm", text,
                                      QtGui.QMessageBox.Yes | QtGui.QMessageBox.No) == QtGui.QMessageBox.Yes


def screen_size(widget=None):
    """The usable part of the screen FreeCAD is on (without the taskbar)."""
    try:
        screen = (widget or Gui.getMainWindow()).screen()
    except AttributeError:
        screen = QtGui.QApplication.primaryScreen()
    return screen.availableGeometry().size()


def fit_to_screen(widget, width, height):
    """Open `widget` at width x height, or smaller where the screen is smaller (its contents scroll)."""
    avail = screen_size()
    widget.resize(min(width, int(avail.width() * 0.9)), min(height, int(avail.height() * 0.85)))


def show_panel():
    if SESSION.panel is None:
        SESSION.panel = TeamPanel(SESSION)
        mw = Gui.getMainWindow()
        mw.addDockWidget(QtCore.Qt.RightDockWidgetArea, SESSION.panel)
        try:  # about a third of the window: readable, and the 3D view keeps most of the room
            width = max(PANEL_MIN_WIDTH, min(PANEL_WIDTH, int(mw.width() * PANEL_SHARE)))
            SESSION.panel.share = width / max(mw.width(), 1)
            mw.resizeDocks([SESSION.panel], [width], QtCore.Qt.Horizontal)
            SESSION.panel.window_width = mw.width()
        except (AttributeError, TypeError):
            pass
    SESSION.panel.show()
    tidy_task_view()
    if SESSION.connected:
        SESSION.request_refresh()


def tidy_task_view():
    """FreeCAD 1.1 puts its Tasks panel in an overlay on the right edge of the 3D view, right beside our
    panel, where it pops up and vanishes as the mouse crosses it. Dock it as a tab next to the Model tree
    instead; FreeCAD still brings it forward by itself whenever a real task (Placement, Transform) opens."""
    mw = Gui.getMainWindow()
    docks = {d.windowTitle(): d for d in mw.findChildren(QtGui.QDockWidget)}
    tasks, model = docks.get("Tasks"), docks.get("Model") or docks.get("Combo View")
    if tasks is None or model is None or tasks is model:
        return
    if mw.dockWidgetArea(tasks) == QtCore.Qt.LeftDockWidgetArea and tasks.parent() is mw:
        return  # already docked on the left
    try:
        mw.addDockWidget(QtCore.Qt.LeftDockWidgetArea, tasks)
        mw.tabifyDockWidget(model, tasks)
        model.raise_()
    except Exception as e:
        warn(f"couldn't move the Tasks panel: {e}")


def auto_constrain():
    from . import constraints_ui
    constraints_ui.show()


def run_interference():
    r = SESSION.act("POST", "/interference", {}, purpose="check for clashes")
    if r is None:
        return
    Gui.Selection.clearSelection()
    if not r["clashes"]:
        say(f"No interference among {len(r['checked'])} parts")
        QtGui.QMessageBox.information(Gui.getMainWindow(), "CADSwarm", "No interference found.")
        return
    lines = []
    doc = mirror_document()
    for c in r["clashes"]:
        lines.append(f"{c['a']} ∩ {c['b']}: {c['volume']} mm³")
        for n in (c["a"], c["b"]):
            if doc is not None and doc.getObject(n):
                Gui.Selection.addSelection(doc.Name, n)
    QtGui.QMessageBox.warning(Gui.getMainWindow(), "CADSwarm: interference", "\n".join(lines))


BED_PRESETS = (("Bambu Lab A1 mini", 180.0, 180.0, 180.0),
               ("Bambu Lab P1S / X1C", 256.0, 256.0, 256.0),
               ("Prusa MK4", 250.0, 210.0, 220.0),
               ("Creality Ender 3", 220.0, 220.0, 250.0))


class BedDialog(QtGui.QDialog):
    """The printer the team prints on. The reviewer and the printability check both use its bed size."""

    def __init__(self, bed):
        super().__init__(Gui.getMainWindow())
        theme.apply(self)
        self.setWindowTitle("CADSwarm: printer bed")
        fit_to_screen(self, 430, 260)
        lay = QtGui.QVBoxLayout(self)
        lay.addWidget(QtGui.QLabel("How much room does your printer have? The team checks parts against this."))
        row = QtGui.QHBoxLayout()
        row.addWidget(QtGui.QLabel("Common printer"))
        self.preset = QtGui.QComboBox()
        self.preset.addItem("Choose one, or type the sizes below", None)
        for label, x, y, z in BED_PRESETS:
            self.preset.addItem(label, (x, y, z))
        self.preset.currentIndexChanged.connect(self.pick_preset)
        row.addWidget(self.preset, 1)
        lay.addLayout(row)
        grid = QtGui.QGridLayout()
        self.spins = {}
        for i, (key, label) in enumerate((("x_mm", "Width (X)"), ("y_mm", "Depth (Y)"),
                                          ("z_mm", "Height (Z)"))):
            spin = QtGui.QDoubleSpinBox()
            spin.setRange(20.0, 2000.0)
            spin.setDecimals(1)
            spin.setSingleStep(10.0)
            spin.setSuffix(" mm")
            try:
                spin.setValue(float(bed[i]))
            except (IndexError, TypeError, ValueError):
                spin.setValue(180.0)
            self.spins[key] = spin
            grid.addWidget(QtGui.QLabel(label), i, 0)
            grid.addWidget(spin, i, 1)
        lay.addLayout(grid)
        lay.addStretch(1)
        self.buttons = QtGui.QDialogButtonBox(QtGui.QDialogButtonBox.Save | QtGui.QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

    def pick_preset(self, index):
        data = self.preset.itemData(index)
        if not data:
            return
        for key, value in zip(("x_mm", "y_mm", "z_mm"), data):
            self.spins[key].setValue(float(value))

    def save(self):
        x, y, z = (round(self.spins[k].value(), 1) for k in ("x_mm", "y_mm", "z_mm"))
        if not 20.0 <= min(x, y, z) or max(x, y, z) > 2000.0:
            warn("A printer bed between 20 and 2000 mm on each side, please.")
            return
        self.buttons.setEnabled(False)

        def saved(_r):
            self.accept()
            hint(f"Printer bed set to {x:g} x {y:g} x {z:g} mm.")

        def failed():
            self.buttons.setEnabled(True)

        SESSION.act_async("POST", "/bed", {"x_mm": x, "y_mm": y, "z_mm": z}, done=saved, failed=failed,
                          purpose="set the printer bed")


def printer_bed():
    if not SESSION.ensure_connected("set the printer bed"):
        return
    bed = (SESSION.workspace or {}).get("bed") or [180.0, 180.0, 180.0]
    dlg = BedDialog(bed)
    (getattr(dlg, "exec", None) or dlg.exec_)()


def show_printability(r):
    objects = (r or {}).get("objects") or []
    if not objects:
        hint("There are no solid parts to check yet. Build something first.")
        return
    lines = []
    for o in objects:
        name = o.get("name") or "part"
        try:
            size = " x ".join(f"{float(v):g}" for v in (o.get("size") or [])[:3])
        except (TypeError, ValueError):
            size = "unknown size"
        issues = []
        if not o.get("fits_bed"):
            issues.append("too big for the bed")
        if not o.get("closed_solid"):
            issues.append("not a closed solid")
        thin = o.get("thin_wall_suspects") or []
        if thin:
            closest = min((float(t.get("distance") or 0) for t in thin if isinstance(t, dict)), default=0.0)
            issues.append(f"{len(thin)} thin wall{'s' if len(thin) != 1 else ''}"
                          + (f" (closest {closest:g} mm)" if closest else ""))
        lines.append(f"{name}: {size} mm" + (" - " + "; ".join(issues) if issues else " - fits"))
    text = "\n".join(lines)
    if r.get("ok"):
        QtGui.QMessageBox.information(Gui.getMainWindow(), "CADSwarm: fits the printer", text)
    else:
        QtGui.QMessageBox.warning(Gui.getMainWindow(), "CADSwarm: not printable yet", text)


def check_printability():
    if not SESSION.ensure_connected("check it fits the printer"):
        return
    selected = SESSION.selected_names()
    SESSION.act_async("POST", "/printability", {"names": selected} if selected else {},
                      done=show_printability, purpose="check it fits the printer")


EXPORT_FILTERS = "STEP - editable CAD (*.step *.stp);;STL - for 3D printing (*.stl)"


def export_target(path, selected_filter):
    """(format, path) from the save dialog: a typed extension wins, otherwise the file type on show."""
    fmt = "stl" if "STL" in (selected_filter or "") else "step"
    ext = os.path.splitext(path)[1].lower()
    if ext in (".step", ".stp"):
        fmt = "step"
    elif ext == ".stl":
        fmt = "stl"
    else:  # no extension (or an unknown one): use the file type the dialog was showing
        path += ".stl" if fmt == "stl" else ".step"
    return fmt, path


def run_export():
    """Save the shared model as a 3D file. The save dialog offers STEP (editable CAD) and STL (for
    printing) side by side; the file type you pick decides which one is written."""
    default = safe_name(prefs().GetString("ProjectName") or "model") + ".step"
    path, selected = QtGui.QFileDialog.getSaveFileName(Gui.getMainWindow(), "Save the 3D model (STEP or STL)",
                                                       default, EXPORT_FILTERS)
    if not path:
        return
    fmt, path = export_target(path, selected)
    r = SESSION.act("POST", "/export", {"format": fmt}, purpose=f"export the model as {fmt.upper()}")
    if r is None:
        return
    with open(path, "wb") as f:
        f.write(SESSION.client.request("GET", r["url"], raw=True))
    count = len(r.get("objects") or [])
    say(f"Saved {count} part{'s' if count != 1 else ''} as {fmt.upper()} to {path}")


# --------------------------------------------------------------- commands

class Cmd:
    """Toolbar/menu command. Always clickable: if you're not in a session yet, clicking
    explains that and offers to join, instead of being mysteriously greyed out."""

    def __init__(self, text, tip, fn, needs_connection=True):
        self.text, self.tip, self.fn, self.needs = text, tip, fn, needs_connection

    def GetResources(self):
        return {"MenuText": self.text, "ToolTip": self.tip}

    def Activated(self):
        if self.needs and not SESSION.ensure_connected(self.text.rstrip("…").lower()):
            return
        self.fn()

    def IsActive(self):
        return True


def connect_dialog(reason=None):
    """Join (or set up) a team through the step-by-step guide. Returns True once joined."""
    from .wizard import run_wizard
    return run_wizard(reason=reason)


def select_team(team):
    """Point the app at a recent team without joining yet (the guide's reconnect step does the joining).
    A team server this computer is still running is never forgotten, so "Stop hosting" keeps working."""
    p = prefs()
    p.SetString("ProjectName", team["project"])
    p.SetString("Db", team["db"])
    p.SetString("Uri", team.get("uri") or "")
    folder = team.get("host_data_dir") or ""
    if folder:
        p.SetString("HostDataDir", folder)
        p.SetString("HostMode", team.get("host_mode") or "")
        return
    current = p.GetString("HostDataDir")
    if not (current and hosting.team_ready(current)):  # this computer hosts the picked team, or nobody
        p.SetString("HostDataDir", "")
        p.SetString("HostMode", "")


def open_previous_team():
    """Reopen a team this computer joined before, without a new invite code (a per-team sign-in is kept).

    The list shows each project's last picture and how big it was, so "RacingDrone22 (cadswarm-…)" is
    not the only thing to go on when picking one."""
    from .wizard import run_wizard
    teams = recent_teams()
    if not teams:
        hint("You haven't joined a team on this computer yet. Start or join one from CADSwarm ▸ Get started.")
        return
    chosen = projects_ui.pick_team(teams, Gui.getMainWindow())
    if chosen is None:
        return
    select_team(chosen)
    run_wizard(start="reconnect")


def add_helper():
    from .wizard import run_wizard
    run_wizard(start="agent")


def invite_teammates():
    """Mint a fresh single-use code for one teammate (a used code can never be handed on)."""
    if not SESSION.ensure_connected("invite teammates"):
        return
    try:
        code = SESSION.client.create_invite("human", "member", 86400, prefs().GetString("ProjectName"),
                                            uri=hosting.invite_uri(SESSION.client.hub))
    except HubRequestError as e:
        QtGui.QMessageBox.warning(Gui.getMainWindow(), "CADSwarm", friendly_error(e, SESSION.client.hub))
        return
    QtGui.QApplication.clipboard().setText(code)
    QtGui.QMessageBox.information(
        Gui.getMainWindow(), "Invite teammates",
        "The invite code is copied. Paste it to one teammate (email, Discord, text...).\n\n"
        f"{code}\n\nThis code works once. Make a new one for each teammate.\n\nThey open FreeCAD, pick "
        "CADSwarm, click Get started, then \u201cJoin a teammate\u2019s project\u201d.")


STOPPED_HOSTING = "You stopped hosting the team."


def stop_hosting():
    from .wizard import stop_hosting as stop
    stop()


def hosted_project_folder():
    """The project folder this computer hosts for the team it is in now, or None."""
    folder = host_dir_for(prefs().GetString("Db"))
    return folder if folder and os.path.isdir(folder) else None


def open_project_folder():
    folder = hosted_project_folder()
    if not folder:
        hint("The project folder is on the computer of the teammate who started the team. "
             "Use the Tools tab ▸ Project files to see and copy what's in it.")
        return
    from .wizard import open_folder
    open_folder(folder)


def save_snapshot():
    label, ok = QtGui.QInputDialog.getText(Gui.getMainWindow(), "Save a snapshot",
                                           "Name this version of the model (optional):")
    if ok:
        r = SESSION.act("POST", "/project/snapshot", {"label": label.strip()}, purpose="save a snapshot")
        if r:
            hint(f"Saved {r['file']} in the project folder. Snapshots are never overwritten.")


class ProjectFilesDialog(QtGui.QDialog):
    """What the team has saved: browse, see versions and authors, save a copy."""

    def __init__(self):
        super().__init__(Gui.getMainWindow())
        theme.apply(self)
        self.setWindowTitle("CADSwarm: project files")
        fit_to_screen(self, 640, 460)
        lay = QtGui.QVBoxLayout(self)
        where = hosted_project_folder()
        lay.addWidget(QtGui.QLabel(
            "Notes, plans and scripts your team's AI helpers saved. The last 20 versions of each file are kept."
            + (f"<br><small>On this computer: {where}\\files</small>" if where else
               "<br><small>They live on the computer of the teammate who started the team.</small>")))
        self.tree = QtGui.QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Version", "Saved by", "When"])
        self.tree.setColumnWidth(0, 280)
        lay.addWidget(self.tree, 1)
        row = QtGui.QHBoxLayout()
        copy = QtGui.QPushButton("Save a copy…")
        copy.clicked.connect(self.save_copy)
        row.addWidget(copy)
        if where:
            folder = QtGui.QPushButton("Open project folder")
            folder.clicked.connect(open_project_folder)
            row.addWidget(folder)
        row.addStretch(1)
        close = QtGui.QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        self.load()

    def load(self):
        try:
            listing = SESSION.client.get("/project/files")
        except HubRequestError as e:
            hint(friendly_error(e))
            return
        nodes = {"": self.tree.invisibleRootItem()}

        def folder_node(path):
            if path in nodes:
                return nodes[path]
            parent, _, name = path.rpartition("/")
            item = QtGui.QTreeWidgetItem(folder_node(parent), [f"📁 {name}", "", "", ""])
            item.setExpanded(True)
            nodes[path] = item
            return item

        for f in sorted(listing.get("folders", [])):
            folder_node(f)
        for f in listing.get("files", []):
            parent, _, name = f["path"].rpartition("/")
            when = time.strftime("%b %d %H:%M", time.localtime(f["updated_at"]))
            item = QtGui.QTreeWidgetItem(folder_node(parent), [name, str(f["version"]), f["author"] or "", when])
            item.setData(0, QtCore.Qt.UserRole, f["path"])
        if not listing.get("files"):
            QtGui.QTreeWidgetItem(self.tree, ["(nothing saved yet)", "", "", ""])

    def save_copy(self):
        item = self.tree.currentItem()
        path = item.data(0, QtCore.Qt.UserRole) if item else None
        if not path:
            hint("Click a file first, then “Save a copy”.")
            return
        dest, _ = QtGui.QFileDialog.getSaveFileName(self, "Save a copy", path.split("/")[-1])
        if not dest:
            return  # the system dialog already asked before replacing an existing file
        try:
            data = SESSION.client.request("GET", "/project/files/download?" + urlencode({"path": path}), raw=True)
        except HubRequestError as e:
            hint(friendly_error(e))
            return
        with open(dest, "wb") as f:
            f.write(data)
        say(f"Saved a copy of {path} to {dest}")


def show_project_files():
    if not SESSION.ensure_connected("see the project's files"):
        return
    dlg = ProjectFilesDialog()
    (getattr(dlg, "exec", None) or dlg.exec_)()


def size_text(n):
    """Bytes the way people say them: '320 MB', '1.5 GB'."""
    for unit, size in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= size:
            v = n / size
            return f"{v:.1f} {unit}" if v < 10 else f"{v:.0f} {unit}"
    return f"{n} bytes"


class StorageDialog(QtGui.QDialog):
    """What is using the project's room, and a careful way to free some. Everything runs on the GUI
    thread through the same blocking client calls the other dialogs use (they are quick)."""

    def __init__(self):
        super().__init__(Gui.getMainWindow())
        theme.apply(self)
        self.setWindowTitle("CADSwarm: storage")
        fit_to_screen(self, 560, 520)
        self.is_host = hosted_project_folder() is not None
        lay = QtGui.QVBoxLayout(self)
        self.summary = QtGui.QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(QtCore.Qt.RichText)
        lay.addWidget(self.summary)

        box = QtGui.QGroupBox("Free up space (this deletes things for good)")
        form = QtGui.QFormLayout(box)
        self.versions = self.spin(1, 1000, 5, " newest")
        self.snap_days = self.spin(1, 3650, 30, " days")
        self.snap_keep = self.spin(0, 1000, 3, " newest")
        self.exp_days = self.spin(1, 3650, 30, " days")
        self.do_versions = QtGui.QCheckBox("Trim older versions of files, keeping only the")
        self.do_snaps = QtGui.QCheckBox("Delete model snapshots older than")
        self.do_exports = QtGui.QCheckBox("Delete exported 3D files older than")
        for check in (self.do_versions, self.do_snaps, self.do_exports):
            check.setChecked(True)
        form.addRow(self.do_versions, self.versions)
        form.addRow(self.do_snaps, self.snap_days)
        form.addRow("   …but always keep the", self.snap_keep)
        form.addRow(self.do_exports, self.exp_days)
        free = QtGui.QPushButton("Free up space…")
        free.clicked.connect(self.free_up)
        form.addRow(free)
        lay.addWidget(box)

        if self.is_host:  # only the person running the team server sets its limits
            sbox = QtGui.QGroupBox("Limits (host only)")
            sform = QtGui.QFormLayout(sbox)
            self.limit = QtGui.QDoubleSpinBox()
            self.limit.setRange(0, 100000)
            self.limit.setDecimals(1)
            self.limit.setSuffix(" GB")
            self.limit.setSpecialValueText("No project limit")
            self.keep = self.spin(1, 1000, 20, " versions per file")
            self.hours = self.spin(0, 24 * 365, 24, " hours")
            sform.addRow("Project size limit", self.limit)
            sform.addRow("Keep older versions of each file:", self.keep)
            sform.addRow("…and everything saved in the last", self.hours)
            save = QtGui.QPushButton("Save limits")
            save.clicked.connect(self.save_limits)
            sform.addRow(save)
            lay.addWidget(sbox)
        row = QtGui.QHBoxLayout()
        row.addStretch(1)
        close = QtGui.QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        self.load()

    @staticmethod
    def spin(low, high, value, suffix):
        box = QtGui.QSpinBox()
        box.setRange(low, high)
        box.setValue(value)
        box.setSuffix(suffix)
        return box

    def load(self):
        try:
            st = SESSION.client.get("/project/storage")
        except HubRequestError as e:
            hint(friendly_error(e))
            return
        u, s = st["usage"], st["settings"]
        limit, free = st["limit_bytes"], st["free_disk_bytes"]
        lines = [f"<b>The project uses {size_text(u['total'])}</b>"
                 + (f" of its {size_text(limit)} limit" if limit else " (no project limit set)"),
                 f"&bull; Your team's files: {size_text(u['files'])}",
                 f"&bull; Older versions of those files: {size_text(u['old_versions'])}",
                 f"&bull; Model snapshots: {size_text(u['snapshots'])}",
                 f"&bull; Exported 3D files: {size_text(u['exports'])}",
                 f"&bull; The live model: {size_text(u['model'])} (not counted)"]
        if free is not None:
            lines.append(f"<br>The host's drive has {size_text(free)} free; CADSwarm stops saving when less than "
                         f"{size_text(st['reserve_bytes'])} would be left.")
        lines.append(f"<small>Older versions: the newest {s['history_keep']} of each file are kept, plus everything from "
                     f"the last {s['history_hours']} hours. Snapshots and exports are only deleted here, by a person.</small>")
        self.summary.setText("<br>".join(lines))
        if self.is_host:
            self.limit.setValue(s["project_limit_gb"])
            self.keep.setValue(s["history_keep"])
            self.hours.setValue(int(s["history_hours"]))
            if st.get("limit_source") == "env":
                self.limit.setEnabled(False)
                self.limit.setToolTip("Set by the CADSWARM_PROJECT_LIMIT_GB environment variable.")

    def options(self):
        o = {}
        if self.do_versions.isChecked():
            o["keep_versions"] = self.versions.value()
        if self.do_snaps.isChecked():
            o["snapshots_older_than_days"] = self.snap_days.value()
            o["keep_newest_snapshots"] = self.snap_keep.value()
        if self.do_exports.isChecked():
            o["exports_older_than_days"] = self.exp_days.value()
        return o

    def free_up(self):
        o = self.options()
        if not o:
            hint("Tick at least one thing to clean up.")
            return
        will = []
        if "keep_versions" in o:
            will.append(f"- older versions of files: all but the newest {o['keep_versions']} of each file")
        if "snapshots_older_than_days" in o:
            will.append(f"- model snapshots older than {o['snapshots_older_than_days']} days "
                        f"(the newest {o['keep_newest_snapshots']} stay)")
        if "exports_older_than_days" in o:
            will.append(f"- exported 3D files older than {o['exports_older_than_days']} days")
        if not confirm("This will delete, for good:\n\n" + "\n".join(will) +
                       "\n\nYour current files and the live model are not touched. Continue?"):
            return
        try:
            r = SESSION.client.post("/project/storage/prune", o)
        except HubRequestError as e:
            hint(friendly_error(e))
            return
        f = r["freed"]
        say(f"Freed {size_text(f['total'])} of project storage")
        hint(f"Freed {size_text(f['total'])}: older versions {size_text(f['old_versions'])}, "
             f"snapshots {size_text(f['snapshots'])}, exports {size_text(f['exports'])}.")
        self.load()

    def save_limits(self):
        try:
            SESSION.client.post("/project/storage/settings", {
                "project_limit_gb": self.limit.value(), "history_keep": self.keep.value(),
                "history_hours": self.hours.value()})
        except HubRequestError as e:
            hint(friendly_error(e))
            return
        hint("Saved. The new limits apply to the next save.")
        self.load()


def show_storage():
    if not SESSION.ensure_connected("see the project's storage"):
        return
    dlg = StorageDialog()
    (getattr(dlg, "exec", None) or dlg.exec_)()


VERDICT_WORDS = {"ok": ("#1a7f37", "Looks OK"), "check": ("#9a6700", "Check the warnings"),
                 "fail": ("#cf222e", "Needs changes")}


class FeaReportDialog(QtGui.QDialog):
    """One simulation report, as it pops up for everyone on the team. Non-modal, so work can go on."""

    def __init__(self, folder):
        super().__init__(Gui.getMainWindow())
        theme.apply(self)
        self.folder = folder
        self.report = json.loads(SESSION.client.post("/project/files/read", {"path": f"{folder}/report.json"})["text"])
        page = SESSION.client.post("/project/files/read", {"path": f"{folder}/report.html"})["text"]
        self.png = None
        if self.report["files"].get("plot"):
            try:
                self.png = SESSION.client.request(
                    "GET", "/project/files/download?" + urlencode({"path": self.report["files"]["plot"]}), raw=True)
            except HubRequestError:
                pass
        r = self.report
        self.setWindowTitle(f"CADSwarm: simulation report: {r['title']}")
        fit_to_screen(self, 780, 720)
        lay = QtGui.QVBoxLayout(self)
        color, word = VERDICT_WORDS.get(r["verdict"], VERDICT_WORDS["check"])
        head = QtGui.QLabel(f"<b style='font-size:13pt'>{html.escape(r['title'])}</b><br>"
                            f"<span style='color:#8b949e'>{html.escape(r['analysis_name'])} by {html.escape(r['by'])}"
                            f"</span><br><span style='background:{color};color:white;font-weight:bold'>&nbsp;{word}&nbsp;"
                            f"</span>&nbsp; {html.escape(r['headline'])}"
                            + ("<br><span style='color:#e3b341'><b>Uses placeholder values</b>: see the yellow box"
                               "</span>" if r["inputs"]["placeholders"] else ""))
        head.setTextFormat(QtCore.Qt.RichText)
        lay.addWidget(head)
        self.view = QtGui.QTextBrowser()
        self.view.setOpenExternalLinks(True)
        self.view.setStyleSheet("QTextBrowser { background: white; color: #1f2328; }")  # paper, even in dark themes
        if self.png:
            img = QtGui.QImage.fromData(self.png)
            self.view.document().addResource(QtGui.QTextDocument.ImageResource, QtCore.QUrl("plot.png"), img)
        self.view.setHtml(page)
        lay.addWidget(self.view, 1)
        row = QtGui.QHBoxLayout()
        if r["verdict"] != "ok":  # an AI helper reads the report (fea_report), fixes its parts and reruns it
            self.fix_button = QtGui.QPushButton("Ask a helper to fix this…")
            self.fix_button.setToolTip("A helper reads this report, fixes the problems in the parts and runs it again")
            self.fix_button.clicked.connect(self.ask_fix)
            row.addWidget(self.fix_button)
        for label, fn in (("Export HTML…", self.export_html), ("Export JSON…", self.export_json)):
            b = QtGui.QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addWidget(QtGui.QLabel(f"<small style='color:#8b949e'>Saved in the project: files/{html.escape(folder)}"
                                   "</small>"))
        row.addStretch(1)
        close = QtGui.QPushButton("Close")
        close.clicked.connect(self.close)
        row.addWidget(close)
        lay.addLayout(row)

    def ask_fix(self):
        helpers = [x["name"] for x in SESSION.model["participants"] if takes_instructions(x)]
        if not helpers:
            hint("No AI helper has joined yet. Click ＋ New helper.")
            return
        helper, ok = QtGui.QInputDialog.getItem(self, "Ask a helper to fix this", "Which helper should fix it?",
                                                helpers, 0, False)
        if not ok:
            return
        r = self.report
        body = (f"The simulation report “{r['title']}” ({r['id']}) says {VERDICT_WORDS.get(r['verdict'], ('', ''))[1].lower()}: "
                f"{r['headline']}. Read it with fea_report('{r['id']}'), fix its critical and warning problems in "
                f"{', '.join(r['model']['target'])}, then rerun it with its rerun_args and publish the new report.")
        SESSION.act_async("POST", "/messages", {"to": helper, "kind": "command", "body": body},
                          done=f"Asked {helper} to fix “{r['title']}”", purpose="ask a helper to fix a simulation")

    def _save(self, title, ext, pattern, data):
        name = f"{safe_name(self.report['title'])} report.{ext}"
        dest, _ = QtGui.QFileDialog.getSaveFileName(self, title, name, pattern)
        if not dest:
            return
        with open(dest, "wb") as f:
            f.write(data)
        say(f"Saved the simulation report to {dest}")

    def export_html(self):
        from ..fea import standalone_html
        self._save("Export report as HTML", "html", "Web page (*.html)",
                   standalone_html(self.report, self.png).encode("utf-8"))

    def export_json(self):
        self._save("Export report data as JSON", "json", "JSON (*.json)",
                   json.dumps(self.report, indent=2).encode("utf-8"))

    def closeEvent(self, ev):
        if self in SESSION.fea_dialogs:
            SESSION.fea_dialogs.remove(self)
        super().closeEvent(ev)


def safe_name(text):
    return "".join(c if c.isalnum() or c in " -_." else " " for c in text).strip()[:60] or "simulation"


def open_fea_report(folder):
    """Pop up a report (from the team's event stream or the reports list). At most 5 stay open."""
    for d in SESSION.fea_dialogs:
        if d.folder == folder:
            d.raise_()
            d.activateWindow()
            return
    try:
        dlg = FeaReportDialog(folder)
    except (HubRequestError, ValueError, KeyError) as e:
        warn(f"Couldn't open the simulation report in {folder}: {e}")
        return
    while len(SESSION.fea_dialogs) >= 5:
        SESSION.fea_dialogs.pop(0).close()
    SESSION.fea_dialogs.append(dlg)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()


class FeaReportsDialog(QtGui.QDialog):
    """Every simulation report the team has published; double-click to open one."""

    def __init__(self):
        super().__init__(Gui.getMainWindow())
        theme.apply(self)
        self.setWindowTitle("CADSwarm: simulation reports")
        fit_to_screen(self, 720, 400)
        lay = QtGui.QVBoxLayout(self)
        lay.addWidget(QtGui.QLabel("Simulations (FEA) your team's AI helpers ran. Double-click one to open it."))
        self.tree = QtGui.QTreeWidget()
        self.tree.setHeaderLabels(["When", "Report", "Result", "Verdict", "By"])
        self.tree.setColumnWidth(0, 110)
        self.tree.setColumnWidth(1, 220)
        self.tree.setColumnWidth(2, 220)
        self.tree.itemDoubleClicked.connect(lambda item, _col: self.open(item))
        lay.addWidget(self.tree, 1)
        row = QtGui.QHBoxLayout()
        b = QtGui.QPushButton("Open")
        b.clicked.connect(lambda: self.open(self.tree.currentItem()))
        row.addWidget(b)
        row.addStretch(1)
        close = QtGui.QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        try:
            reports = SESSION.client.get("/fea/reports")["reports"]
        except HubRequestError as e:
            hint(friendly_error(e))
            reports = []
        for r in reports:
            when = time.strftime("%b %d %H:%M", time.localtime(r["published_at"]))
            item = QtGui.QTreeWidgetItem([when, r["title"], r["headline"],
                                          VERDICT_WORDS.get(r["verdict"], ("", r["verdict"]))[1]
                                          + (" (placeholders)" if r["placeholders"] else ""), r["by"]])
            item.setData(0, QtCore.Qt.UserRole, r["folder"])
            self.tree.addTopLevelItem(item)
        if not reports:
            QtGui.QTreeWidgetItem(self.tree, ["", "(no simulations yet. Ask an AI helper \"will this hold 5 kg?\")"])

    def open(self, item):
        folder = item.data(0, QtCore.Qt.UserRole) if item else None
        if folder:
            open_fea_report(folder)


def show_fea_reports():
    if not SESSION.ensure_connected("see the team's simulation reports"):
        return
    dlg = FeaReportsDialog()
    (getattr(dlg, "exec", None) or dlg.exec_)()


def lock_selected():
    show_panel()
    SESSION.panel.lock()


def release_selected():
    show_panel()
    SESSION.panel.release()


def register_commands():
    Gui.addCommand("CadSwarm_Connect", Cmd("Get started…", "Start or join a team, step by step",
                                           lambda: connect_dialog(), False))
    Gui.addCommand("CadSwarm_AddHelper", Cmd("Add AI helper…", "Connect an AI helper (Claude Code, Codex "
                                                              "or OpenCode) to your team", add_helper))
    Gui.addCommand("CadSwarm_Invite", Cmd("Invite…", "Copy a new invite code for a teammate", invite_teammates))
    Gui.addCommand("CadSwarm_StopHosting", Cmd("Stop hosting", "Stop the team server on this computer",
                                               stop_hosting, False))
    Gui.addCommand("CadSwarm_Panel", Cmd("Team panel", "Show presence, locks, tasks and chat", show_panel, False))
    Gui.addCommand("CadSwarm_Lock", Cmd("Lock selected", "Lock the selected mirror parts to you", lock_selected))
    Gui.addCommand("CadSwarm_Release", Cmd("Release selected", "Release your locks on the selected parts", release_selected))
    Gui.addCommand("CadSwarm_Interference", Cmd("Check for clashes", "Find parts that overlap", run_interference))
    Gui.addCommand("CadSwarm_Export", Cmd("Save as STL/STEP file…", "Save the shared model as an STL (for 3D printing) "
                                                                    "or a STEP (editable CAD) file", run_export))
    Gui.addCommand("CadSwarm_FeaReports", Cmd("Simulation reports…", "Open the team's FEA (simulation) reports",
                                              show_fea_reports))
    Gui.addCommand("CadSwarm_RunFea", Cmd("Run a simulation…", "Check how a part holds up under a load, no helper "
                                          "needed", fea_ui.show_run_simulation))
    Gui.addCommand("CadSwarm_NewTask", Cmd("New task…", "Give the team (or yourself) something to do", lambda: (
        show_panel(), SESSION.panel.new_task())))
    Gui.addCommand("CadSwarm_EditPart", Cmd("Edit a part…", "Change a part yourself and share the change", edit_part))
    Gui.addCommand("CadSwarm_Library", Cmd("Components library…", "Place screws, bearings, inserts or your team's own "
                                                                "parts, or upload a STEP file", library_ui.show_library))
    Gui.addCommand("CadSwarm_Settings", Cmd("Precision settings…", "The team's clearances, fits and precision, and "
                                                                 "what AI helpers suggest", library_ui.show_settings))
    Gui.addCommand("CadSwarm_Style", Cmd("Learned design style…", "The design rules CADSwarm learned from how your "
                                                                 "team works; confirm, dismiss or teach one",
                                         style_ui.show_style))
    Gui.addCommand("CadSwarm_TellHelper", Cmd("Chat with helpers", "Tell an AI helper what to do; it answers in the "
                                                                   "Chat", lambda: (
        show_panel(), SESSION.panel.msg.setText("@"), SESSION.panel.msg.setFocus(), SESSION.panel.maybe_complete())))
    Gui.addCommand("CadSwarm_Disconnect", Cmd("Leave team", "Leave the session (releases your locks)",
                                              lambda: SESSION.disconnect() if SESSION.connected else None, False))
    Gui.addCommand("CadSwarm_Constrain", Cmd("Auto-constrain…", "Bind the sizes and fits the model already "
                                                                "repeats to shared sizes", auto_constrain))
