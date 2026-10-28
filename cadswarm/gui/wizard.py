"""Step-by-step setup inside FreeCAD, for people who have never used a terminal or a CAD tool.

    Welcome ──► Start a new team ─► name it ─► (download helper software, start the team
       │                                        server, join) ─► share invite code ─┐
       └──────► Join with an invite code ─► paste code + name ─────────────────────┤
                                                                                     ▼
        Connect your AI helper (Claude Code / Codex / OpenCode / just watch) ─► (wire it up) ─►
        Open it, paste the first message ─► "✓ your helper joined" ─► Finish

The team's database lives on SpacetimeDB: on this computer by default (no account; teammates on your
network join), or on Maincloud when you sign in; `cadswarm-host` is the program that runs the shared
model. Everything the guide does is in plain words: "team", "invite code", "AI helper". Never hub,
token or MCP. Slow work runs off the GUI thread behind a checklist; every failure says what to do and
offers Try again.
"""
import getpass
import os
import queue
import re
import secrets
import threading
import traceback

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore, QtGui

from .. import hosting
from ..agent_setup import CLIENTS, find_agent_cli, identity_matches, open_agent_terminal, redeem, \
    trust_in_codex, write_agent_config, write_launcher
from ..client import HubRequestError
from ..invite import InviteError, read_invite
from ..paths import freecad_python, projects_dir, resolve_project_folder
from ..space import SpaceError
from . import panel, theme

# CadSwarm's own dark surface, so the guide reads the same on a light FreeCAD theme (see theme.py).
STYLE = theme.DIALOG


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "me"


def member_name(text):
    """The name the team can hold for someone: 1-40 letters, digits, '-' or '_' (the module's rule)."""
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", (text or "").strip()).strip("-_")
    return name[:40] or "me"


def proposed_db(project):
    """A new team's database name: recognizable, and unique so it never collides with another team's."""
    return f"cadswarm-{slug(project)[:24].strip('-') or 'team'}-{secrets.token_hex(2)}"


def safe_folder(text):
    return re.sub(r'[<>:"/\\|?*]+', " ", text or "").strip() or "Team project"


def default_name():
    try:
        user = getpass.getuser()
    except Exception:
        return ""
    return re.split(r"[ ._-]", user)[0].capitalize()


def space_error(e):
    """A SpacetimeDB refusal or connection failure, in words a hobbyist can act on."""
    where = f" at {e.uri}" if getattr(e, "uri", "") else ""  # which database we couldn't reach, so it is clear
    return {
        "unreachable": f"Couldn't reach your team's database{where}. Check your internet connection and try "
                       "again. If the database runs on this computer, restart it from Get started ▸ Reconnect.",
        "bad_invite": "That invite code has already been used or has expired. Ask your teammate for a new one.",
        "name_in_use": "That name is already connected to this team. Pick a different one, or if it was your "
                       "own crashed session, wait a minute and try again.",
        "already_member": "This computer is already part of the team.",
        "not_allowed": "Only whoever started the team can do that.",
    }.get(getattr(e, "code", ""), str(e))


def open_folder(path):
    if os.name == "nt":
        os.startfile(path)
    else:
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))


def label(text, kind=None, wrap=True):
    lab = QtGui.QLabel(text)
    lab.setWordWrap(wrap)
    lab.setTextFormat(QtCore.Qt.RichText)
    lab.setOpenExternalLinks(True)
    if kind:
        lab.setObjectName(kind)
    return lab


def copy_to_clipboard(text, button=None, done="Copied ✓"):
    QtGui.QApplication.clipboard().setText(text)
    if button is not None:
        original = button.text()
        button.setText(done)
        QtCore.QTimer.singleShot(2000, lambda: button.setText(original))


class Checklist(QtGui.QWidget):
    """Runs steps in order, showing ○ → ⏳ → ✓/✗ for each, with a plain error and Try again on failure.

    steps: [(text, hint_or_None, fn, on_gui_thread)] where fn(log) may raise; log(str) adds detail lines.
    """

    ICONS = {"todo": "○", "run": "⏳", "done": "✓", "fail": "✗"}

    def __init__(self, on_done):
        super().__init__()
        self.on_done = on_done
        self.steps, self.rows, self.i = [], [], 0
        self.q = queue.Queue()
        self.lay = QtGui.QVBoxLayout(self)
        self.list = QtGui.QVBoxLayout()
        self.lay.addLayout(self.list)
        self.error = label("", "error")
        self.error.hide()
        self.lay.addWidget(self.error)
        row = QtGui.QHBoxLayout()
        self.retry = QtGui.QPushButton("Try again")
        self.retry.hide()
        self.retry.clicked.connect(self.resume)
        self.toggle = QtGui.QPushButton("Show details")
        self.toggle.setFlat(True)
        self.toggle.clicked.connect(lambda: (self.details.setVisible(not self.details.isVisible()),
                                             self.toggle.setText("Hide details" if self.details.isVisible()
                                                                 else "Show details")))
        row.addWidget(self.retry)
        row.addStretch(1)
        row.addWidget(self.toggle)
        self.lay.addLayout(row)
        self.details = QtGui.QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(140)
        self.details.hide()
        self.lay.addWidget(self.details)
        self.lay.addStretch(1)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.pump)

    def set_steps(self, steps):
        while self.list.count():
            item = self.list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.steps, self.rows, self.i = steps, [], 0
        for text, hint, _fn, _gui in steps:
            w = QtGui.QWidget()
            v = QtGui.QVBoxLayout(w)
            v.setContentsMargins(0, 2, 0, 2)
            main = label(f"{self.ICONS['todo']}  {text}")
            v.addWidget(main)
            h = label(hint, "hint") if hint else None
            if h:
                h.hide()
                v.addWidget(h)
            self.list.addWidget(w)
            self.rows.append((main, h, text))
        self.error.hide()
        self.retry.hide()
        self.details.clear()

    def mark(self, i, state):
        main, hint, text = self.rows[i]
        main.setText(f"{self.ICONS[state]}  {'<b>' + text + '</b>' if state == 'run' else text}")
        if hint:
            hint.setVisible(state == "run")

    def start(self):
        self.i = 0
        self.timer.start(100)
        self.run_current()

    def resume(self):
        self.error.hide()
        self.retry.hide()
        self.timer.start(100)
        self.run_current()

    def log(self, line):
        self.q.put(("log", line))

    def run_current(self):
        if self.i >= len(self.steps):
            self.timer.stop()
            QtCore.QTimer.singleShot(400, self.on_done)
            return
        self.mark(self.i, "run")
        _text, _hint, fn, on_gui = self.steps[self.i]

        def work():
            try:
                fn(self.log)
                self.q.put(("done", None))
            except HubRequestError as e:
                self.q.put(("fail", panel.friendly_error(e)))
            except SpaceError as e:
                self.q.put(("fail", space_error(e)))
            except RuntimeError as e:  # our own plain-language failures (download, server start)
                self.q.put(("fail", str(e)))
            except Exception:  # a bug: never show raw Python to a hobbyist
                self.q.put(("fail", "Something unexpected went wrong. Click “Try again”. If it keeps "
                                    "happening, click “Show details” and send what it says to your team."
                                    "\n\nDetails:\n" + traceback.format_exc()))

        if on_gui:
            QtCore.QTimer.singleShot(50, work)
        else:
            threading.Thread(target=work, daemon=True).start()

    def pump(self):
        while True:
            try:
                kind, data = self.q.get_nowait()
            except queue.Empty:
                return
            if kind == "log":
                self.details.appendPlainText(data)
            elif kind == "done":
                self.mark(self.i, "done")
                self.i += 1
                self.run_current()
            elif kind == "fail":
                self.mark(self.i, "fail")
                short, _, more = data.partition("\n\nDetails:\n")
                self.error.setText(short)
                self.error.show()
                if more:
                    self.details.appendPlainText(more)
                self.retry.show()
                self.timer.stop()


NO_PYTHON = ("This FreeCAD has no Python of its own (the snap and Flatpak versions of FreeCAD don't include "
             "one), so it can't run a team server or connect an AI helper by itself. You can still join a "
             "teammate's project from here. To host, install FreeCAD from freecad.org (the AppImage on Linux), "
             "or set CADSWARM_FREECAD_PYTHON to a FreeCAD Python before starting FreeCAD.")


class SetupWizard(QtGui.QDialog):
    @property
    def python(self):
        """FreeCAD's bundled Python; raises a plain-language error where a step needs one and there is none."""
        if self._python is None:
            try:
                self._python = freecad_python()
            except RuntimeError:
                raise RuntimeError(NO_PYTHON)
        return self._python

    def __init__(self, reason=None, start=None):
        super().__init__(Gui.getMainWindow())
        self.setWindowTitle("CADSwarm: get started")
        # Fits a laptop screen; each page scrolls when it's taller than the window.
        self.setMinimumSize(420, 360)
        panel.fit_to_screen(self, 720, 640)
        self.setStyleSheet(STYLE)
        self.reason = reason
        self.p = panel.prefs()
        self._python = None       # looked up only when hosting or connecting a helper needs it
        self.mode = None          # "host" | "join"
        self.host_mode = "local"
        self.project = self.p.GetString("ProjectName")
        self.name = self.p.GetString("Name") or default_name()
        self.db = self.p.GetString("Db")
        self.uri = self.p.GetString("Uri")
        self.client = None
        self.folder_user_set = False
        self.agent_name = None

        outer = QtGui.QVBoxLayout(self)
        self.stack = QtGui.QStackedWidget()
        outer.addWidget(self.stack, 1)
        bar = QtGui.QHBoxLayout()
        self.back = QtGui.QPushButton("‹ Back")
        self.back.clicked.connect(self.go_back)
        self.secondary = QtGui.QPushButton()
        self.secondary.setFlat(True)
        self.primary = QtGui.QPushButton("Next")
        self.primary.setObjectName("primary")
        self.primary.setDefault(True)
        bar.addWidget(self.back)
        bar.addStretch(1)
        bar.addWidget(self.secondary)
        bar.addWidget(self.primary)
        outer.addLayout(bar)
        self.pages, self.history, self.current = {}, [], None
        self.primary_action = self.secondary_action = None
        self.primary.clicked.connect(lambda: self.primary_action and self.primary_action())
        self.secondary.clicked.connect(lambda: self.secondary_action and self.secondary_action())
        self.poll = QtCore.QTimer(self)
        self.poll.timeout.connect(self.check_helper_joined)
        self.login_poll = QtCore.QTimer(self)  # waits for the sign-in finished in the browser
        self.login_poll.timeout.connect(self.check_maincloud_login)
        self.login_tries = 0

        if start == "agent" and panel.SESSION.connected:
            self.mode = "host" if panel.host_dir_for(self.db) else "join"
            self.show_page("agent")
        elif start == "reconnect":
            self.show_page("reconnect")
        else:
            self.show_page("welcome")

    # ----------------------------------------------------------- navigation

    def show_page(self, name, remember=True):
        if remember and self.stack.currentWidget() is not None and self.history[-1:] != [self.current]:
            self.history.append(self.current)
        self.current = name
        if name not in self.pages or name in ("agent", "launch", "invite"):  # rebuilt with fresh data
            old = self.pages.pop(name, None)
            if old is not None:
                self.stack.removeWidget(old)
                old.deleteLater()
            page = getattr(self, f"page_{name}")()
            holder = QtGui.QScrollArea()
            holder.setWidgetResizable(True)
            holder.setFrameShape(QtGui.QFrame.NoFrame)
            holder.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            holder.setWidget(page)
            self.pages[name] = holder
            self.stack.addWidget(holder)
        self.stack.setCurrentWidget(self.pages[name])
        self.back.setVisible(bool(self.history) and name not in ("host_work", "agent_work", "reconnect", "launch"))
        enter = getattr(self, f"enter_{name}", None)
        if enter:
            enter()

    def go_back(self):
        if self.history:
            self.show_page(self.history.pop(), remember=False)

    def buttons(self, primary=None, action=None, secondary=None, secondary_action=None):
        self.primary.setVisible(primary is not None)
        if primary:
            self.primary.setText(primary)
            self.primary.setEnabled(True)
        self.primary_action = action
        self.secondary.setVisible(secondary is not None)
        if secondary:
            self.secondary.setText(secondary)
        self.secondary_action = secondary_action

    def page(self, title, lead=None):
        w = QtGui.QWidget()
        lay = QtGui.QVBoxLayout(w)
        lay.setSpacing(10)
        lay.addWidget(label(title, "title"))
        if lead:
            lay.addWidget(label(lead, "lead"))
        return w, lay

    # --------------------------------------------------------------- welcome

    def page_welcome(self):
        w, lay = self.page("Build 3D designs with your team's AI helpers",
                           "Each teammate's AI helper (like Claude Code) builds part of one shared 3D model. "
                           "You'll watch the parts appear here and can chat with the helpers to steer them. "
                           "No CAD experience needed.")
        if self.reason:
            lay.addWidget(label(f"<b>{self.reason}</b>"))
        lay.addSpacing(8)
        teams = panel.recent_teams()
        cur_db = self.p.GetString("Db")
        if self.project and cur_db and not any(t.get("db") == cur_db for t in teams):
            teams.insert(0, {"project": self.project, "uri": self.uri, "db": cur_db,
                             "host_data_dir": self.p.GetString("HostDataDir"),
                             "host_mode": self.p.GetString("HostMode")})
        if teams:
            lay.addWidget(label("Open a project you've worked on before", "hint"))
            for t in teams[:5]:
                open_btn = QtGui.QCommandLinkButton(f"Open “{t['project']}”",
                                                    panel.project_cards.subtitle(t))
                open_btn.setIcon(panel.projects_ui.icon_for(t, 34))
                open_btn.setIconSize(QtCore.QSize(34, 34))
                open_btn.clicked.connect(lambda _c=False, t=t: self.open_team(t))
                lay.addWidget(open_btn)
            lay.addSpacing(10)
            lay.addWidget(label("Or start something new", "hint"))
        host = QtGui.QCommandLinkButton("Start a new team project",
                                        "Choose this if you're the first one. You'll get an invite code to send "
                                        "your teammates.")
        host.clicked.connect(lambda: self.choose("host"))
        join = QtGui.QCommandLinkButton("Join a teammate's project",
                                        "Choose this if someone sent you an invite code.")
        join.clicked.connect(lambda: self.choose("join"))
        watch = QtGui.QCommandLinkButton("Just exploring? Read how it works",
                                         "Opens the CADSwarm guide in your browser.")
        watch.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(
            QtCore.QUrl("https://github.com/SachchitBalamurugan/cadswarm#readme")))
        for b in (host, join, watch):
            lay.addWidget(b)
        lay.addStretch(1)
        return w

    def enter_welcome(self):
        self.buttons(secondary="Close", secondary_action=self.reject)

    def choose(self, mode):
        self.mode = mode
        self.show_page("host_name" if mode == "host" else "join")

    def open_team(self, team):
        """Reopen a team this computer joined before: its sign-in is kept per team, so no invite is needed."""
        panel.select_team(team)
        self.project, self.db, self.uri = team["project"], team["db"], team.get("uri") or ""
        self.show_page("reconnect")

    # ------------------------------------------------------------------ host

    def page_host_name(self):
        w, lay = self.page("What are you building?",
                           "Give your project a name your teammates will recognize. You can describe the "
                           "details to your AI helper later.")
        form = QtGui.QFormLayout()
        self.project_edit = QtGui.QLineEdit(self.project)
        self.project_edit.setPlaceholderText("e.g. Phone stand, Plant pot, Robot arm")
        self.host_name_edit = QtGui.QLineEdit(self.name)
        self.host_name_edit.setPlaceholderText("e.g. Sam")
        form.addRow("Project name", self.project_edit)
        form.addRow("Your name", self.host_name_edit)
        lay.addLayout(form)
        lay.addWidget(label("Your name is shown to teammates and their AI helpers. Use letters, numbers, "
                            "- or _ (spaces become -).", "hint"))
        lay.addSpacing(6)
        lay.addWidget(label("<b>Where should the project be saved?</b> The model, your team's notes and "
                            "exported 3D files all go in this folder, organised into subfolders as it grows."))
        row = QtGui.QHBoxLayout()
        self.where_label = label("", "hint")
        pick = QtGui.QPushButton("Choose folder…")
        pick.clicked.connect(self.choose_project_folder)
        row.addWidget(self.where_label, 1)
        row.addWidget(pick)
        lay.addLayout(row)
        self.chosen_parent = None  # None = the default Documents\CadSwarm\<project>
        self.project_edit.textChanged.connect(lambda _t: self.update_where())
        lay.addSpacing(6)
        lay.addWidget(label("<b>Where should your team's database live?</b> Both are SpacetimeDB; pick either.",
                            "hint"))
        self.local_box = QtGui.QRadioButton(
            "On this computer — no account, works offline. Teammates join over your network.")
        self.cloud_box = QtGui.QRadioButton(
            "On SpacetimeDB Maincloud — teammates join from anywhere. Needs a free SpacetimeDB sign-in.")
        self.local_box.setChecked(True)  # the default: nothing to sign up for, nothing to sign in to
        self.local_box.setToolTip("The database runs in the background on this computer while your team works.")
        self.cloud_box.setToolTip("SpacetimeDB's cloud hosts the database; sign in once with your browser.")
        lay.addWidget(self.local_box)
        lay.addWidget(self.cloud_box)
        login_row = QtGui.QHBoxLayout()
        self.login_button = QtGui.QPushButton("Sign in to SpacetimeDB…")
        self.login_button.setEnabled(False)  # only needed for Maincloud
        self.login_button.clicked.connect(self.log_in_maincloud)
        self.login_note = label("", "hint")
        login_row.addWidget(self.login_button)
        login_row.addWidget(self.login_note, 1)
        lay.addLayout(login_row)
        self.cloud_box.toggled.connect(self.login_button.setEnabled)
        self.local_box.toggled.connect(self.update_login_note)
        self.cloud_box.toggled.connect(self.update_login_note)
        self.host_error = label("", "error")
        lay.addWidget(self.host_error)
        lay.addStretch(1)
        return w

    def enter_host_name(self):
        self.buttons("Create my team ›", self.create_team)
        self.update_where()
        self.update_login_note()
        self.project_edit.setFocus()

    def update_login_note(self):
        """Say whether this computer is already signed in to Maincloud, so the choice is informed."""
        if not self.cloud_box.isChecked():
            self.login_note.setText("")
            return
        account = hosting.maincloud_login(force=True)
        self.login_note.setText(f"Signed in as {account}." if account else
                                "Not signed in yet. Sign in once; your browser opens and comes back.")

    def log_in_maincloud(self):
        try:
            hosting.spacetime_login()
        except Exception as e:  # the program is missing, or won't start: say so, don't get stuck
            self.login_note.setText(str(e))
            return
        self.login_tries = 0
        self.login_button.setEnabled(False)
        self.login_note.setText("Finish signing in in your browser, then come back to this window…")
        self.login_poll.start(1500)

    def check_maincloud_login(self):
        account = hosting.maincloud_login(force=True)
        if account:
            self.login_poll.stop()
            self.login_button.setEnabled(True)
            self.login_note.setText(f"Signed in as {account}. You can create your team now.")
            return
        self.login_tries += 1
        if self.login_tries >= 40:  # a minute: give up politely rather than poll forever
            self.login_poll.stop()
            self.login_button.setEnabled(True)
            self.login_note.setText("Still not signed in. Click “Sign in” again once your browser is done.")

    def planned_folder(self):
        project = self.project_edit.text().strip() or "My project"
        if self.chosen_parent:
            return resolve_project_folder(self.chosen_parent, project)
        return resolve_project_folder(os.path.join(projects_dir(), project), project)

    def update_where(self):
        folder, how = self.planned_folder()
        note = {"existing": "A CADSwarm project is already here. You'll continue it (nothing is overwritten).",
                "new": "A new folder will be created for it.",
                "inside": "That folder already has other things in it, so the project gets its own "
                          "subfolder there. Nothing in it is touched."}[how]
        self.where_label.setText(f"<b>{folder}</b><br>{note}")

    def choose_project_folder(self):
        d = QtGui.QFileDialog.getExistingDirectory(self, "Choose where to save the project",
                                                   self.chosen_parent or projects_dir())
        if d:
            self.chosen_parent = os.path.normpath(d)
            self.update_where()

    def create_team(self):
        project, name = self.project_edit.text().strip(), self.host_name_edit.text().strip()
        if not project or not name:
            self.host_error.setText("Please fill in " + " and ".join(
                n for n, v in (("a project name", project), ("your name", name)) if not v) + ".")
            return
        self.project, self.name = project, name
        self.host_mode = "maincloud" if self.cloud_box.isChecked() else "local"
        self.project_dir, self.project_how = self.planned_folder()
        self.show_page("host_work")

    def page_host_work(self):
        w, lay = self.page("Setting up your team…", "This only happens once. You can leave this window open.")
        self.host_steps = Checklist(on_done=lambda: self.show_page("invite"))
        lay.addWidget(self.host_steps, 1)
        return w

    def enter_host_work(self):
        self.buttons(secondary="Cancel", secondary_action=self.reject)
        self.host_steps.set_steps([
            ("Get the helper software (one time, about 2 minutes)",
             "Downloading the small programs that run your team. Needs an internet connection.",
             self.step_deps, False),
            ("Start your team's server",
             "Setting up your team's database and starting the program that runs the shared model. "
             "The first time, this downloads about 50 MB and can take a few minutes.",
             self.step_start_team, False),
            ("Join your team", None, self.step_host_join, True),
        ])
        self.host_steps.start()

    def step_deps(self, log):
        if hosting.deps_ready(self.python):
            log("Helper software already installed.")
            return
        hosting.install_deps(self.python, log)

    def step_start_team(self, log):
        data_dir = self.project_dir
        # Continuing a project keeps its database and mode, so helpers already connected keep working.
        stored = hosting.host_config(data_dir)
        mode = stored.get("mode") or self.host_mode
        db = stored.get("db") or proposed_db(self.project)
        agent = self.client or stored.get("agent_client") or "claude"
        log(f"Starting the team server (model saved in {data_dir})")
        info = hosting.start_team(self.python, data_dir, mode, db, agent=agent, on_log=log)
        self.remember_host(info, data_dir, mode)  # saved at once, so "Stop hosting" finds it whatever happens
        try:
            hosting.wait_ready(data_dir, timeout=120, pids=[info["host_pid"], info["spacetime_pid"]])
        except Exception:
            hosting.stop_team(info)  # else "Try again" would start a second server on the same project folder
            self.forget_pids()
            raise

    def step_host_join(self, log):
        name = member_name(self.name)
        log("Joining as the team's first member")
        t = hosting.mint_invite(self.project_dir, "human", "lead")
        ident = redeem(t, name, "freecad")
        panel.SESSION.connect({"uri": t["uri"], "db": t["db"]}, ident, name, timeout=10, project=self.project)
        self.name, self.db, self.uri = panel.prefs().GetString("Name"), t["db"], t["uri"]
        self.p.SetString("ProjectName", self.project)

    def remember_host(self, info, data_dir, mode):
        for k, v in (("HostDataDir", data_dir), ("HostMode", mode), ("Db", info["db"]), ("Uri", info["uri"]),
                     ("HostPid", str(info["host_pid"] or "")), ("SpacetimePid", str(info["spacetime_pid"] or ""))):
            self.p.SetString(k, v)
        self.db, self.uri = info["db"], info["uri"]

    def forget_pids(self):
        for k in ("HostPid", "SpacetimePid"):
            self.p.SetString(k, "")

    def page_invite(self):
        w, lay = self.page("Your team is ready! 🎉",
                           "Send this invite code to a teammate by email, Discord, text, anything. "
                           "They paste it into CADSwarm to join.")
        box = QtGui.QPlainTextEdit()
        box.setObjectName("code")
        box.setReadOnly(True)
        box.setMaximumHeight(80)
        error = label("", "error")
        lay.addWidget(box)
        lay.addWidget(error)
        copy = QtGui.QPushButton("Copy invite code")
        copy.clicked.connect(lambda: copy_to_clipboard(box.toPlainText(), copy))
        fresh = QtGui.QPushButton("Make a new code")

        def refresh_code():
            try:
                code = panel.SESSION.client.create_invite("human", "member", 86400, self.project,
                                                          uri=hosting.invite_uri(panel.SESSION.client.hub))
            except AttributeError:
                error.setText("Join your team first, then try again.")
                copy.setEnabled(False)
                return
            except (HubRequestError, SpaceError) as e:
                # Not fatal: whoever is connected can still invite from the team panel.
                error.setText(panel.friendly_error(e) if isinstance(e, HubRequestError) else space_error(e))
                copy.setEnabled(False)
                return
            box.setPlainText(code)
            error.setText("")
            copy.setEnabled(True)

        fresh.clicked.connect(refresh_code)
        refresh_code()  # a fresh single-use code every time this page opens
        row = QtGui.QHBoxLayout()
        row.addWidget(copy)
        row.addWidget(fresh)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(label("This code works once. Make a new one for each teammate. They last a day, so "
                            "there's no rush. You can also make one any time from the team panel (“Invite”).",
                            "hint"))
        folder = self.p.GetString("HostDataDir")
        if folder:
            frow = QtGui.QHBoxLayout()
            frow.addWidget(label(f"Your project is saved in <b>{folder}</b>", "hint"), 1)
            show = QtGui.QPushButton("Open folder")
            show.clicked.connect(lambda: open_folder(folder))
            frow.addWidget(show)
            lay.addLayout(frow)
        local = self.p.GetString("HostMode") == "local"
        lay.addWidget(label("<b>Good to know</b><br>" + (
            "• This team's database runs on <b>this computer</b>, so teammates must be on the same network "
            "and this computer must stay on.<br>"
            "• If Windows asks to allow access through its firewall, click <b>Allow access</b>.<br>" if local else
            "• Your team's database runs on SpacetimeDB's cloud, so teammates can join from anywhere.<br>")
            + "• Keep CADSwarm running on this computer while your team works; the host program keeps going "
              "even if you close FreeCAD.<br>"
              "• If your team ever goes offline, reopen CADSwarm ▸ Get started ▸ Reconnect.", "hint"))
        lay.addStretch(1)
        return w

    def enter_invite(self):
        self.buttons("Next: connect my AI helper ›", lambda: self.show_page("agent"),
                     "Skip, I'll just watch", self.finish)

    # ------------------------------------------------------------------ join

    def page_join(self):
        w, lay = self.page("Join your teammate's project",
                           "Paste the invite code they sent you. It starts with “cadswarm-2:”.")
        self.code_edit = QtGui.QPlainTextEdit()
        self.code_edit.setObjectName("code")
        self.code_edit.setPlaceholderText("cadswarm-2:…")
        self.code_edit.setMaximumHeight(80)
        lay.addWidget(self.code_edit)
        paste = QtGui.QPushButton("Paste from clipboard")
        paste.clicked.connect(lambda: self.code_edit.setPlainText(QtGui.QApplication.clipboard().text()))
        lay.addWidget(paste, 0, QtCore.Qt.AlignLeft)
        form = QtGui.QFormLayout()
        self.join_name_edit = QtGui.QLineEdit(self.name)
        self.join_name_edit.setPlaceholderText("e.g. Sam")
        form.addRow("Your name", self.join_name_edit)
        lay.addLayout(form)
        lay.addWidget(label("Use letters, numbers, - or _ (spaces become -). Your teammates and their AI "
                            "helpers will see this name.", "hint"))
        self.join_error = label("", "error")
        lay.addWidget(self.join_error)
        lay.addStretch(1)
        return w

    def enter_join(self):
        self.buttons("Join team ›", self.join_team)
        clip = QtGui.QApplication.clipboard().text()
        if not self.code_edit.toPlainText().strip():
            try:
                read_invite(clip)  # they just copied it: save them a step
                self.code_edit.setPlainText(clip.strip())
            except InviteError:
                pass

    def join_team(self):
        raw_name = self.join_name_edit.text().strip()
        try:
            inv = read_invite(self.code_edit.toPlainText())
        except InviteError as e:
            self.join_error.setText(str(e))
            return
        if not raw_name:
            self.join_error.setText("Please type your name.")
            return
        name = member_name(raw_name)
        self.join_error.setText("<span style='color: palette(text)'>Joining…</span>")
        QtGui.QApplication.processEvents()
        try:
            ident = redeem(inv, name, "freecad")
            panel.SESSION.connect({"uri": inv["uri"], "db": inv["db"]}, ident, name, timeout=10,
                                  project=inv["project"] or "Team project")
        except SpaceError as e:
            self.join_error.setText(space_error(e))
            return
        except HubRequestError as e:
            self.join_error.setText(panel.friendly_error(e, inv["uri"]))
            return
        except Exception as e:  # never leave the dialog stuck on "Joining…"
            App.Console.PrintError(f"[CADSwarm] Join failed: {e!r}\n")
            self.join_error.setText("Something went wrong while joining. Please try again; if it keeps "
                                    "happening, ask your teammate to send a fresh invite code.")
            return
        self.name, self.project = name, inv["project"] or "Team project"
        self.db, self.uri = inv["db"], inv["uri"]
        self.p.SetString("ProjectName", self.project)
        # Forget this computer's hosting only if its own team server is gone: someone who hosts and then joins
        # (their own invite, or another team) must still be able to "Stop hosting" the server they started.
        folder = self.p.GetString("HostDataDir")
        if folder and not hosting.team_ready(folder):
            for k in ("HostDataDir", "HostMode", "HostPid", "SpacetimePid"):
                self.p.SetString(k, "")  # this computer is not the host
        self.show_page("agent")

    # ----------------------------------------------------------------- agent

    def page_agent(self):
        w, lay = self.page("Connect your AI helper",
                           "Pick the AI helper you use. It will build parts of the model for your team.")
        self.client_buttons = {}
        group = QtGui.QButtonGroup(w)
        found_any = None
        for key, info in CLIENTS.items():
            card = QtGui.QFrame()
            card.setObjectName("card")
            row = QtGui.QHBoxLayout(card)
            rb = QtGui.QRadioButton(info["label"])
            rb.setStyleSheet("font-weight: 600;")
            group.addButton(rb)
            row.addWidget(rb)
            path = find_agent_cli(key)
            ready = bool(path)
            if info.get("description"):
                row.addWidget(label(f"— {info['description']}", "hint"))
            if ready:
                found_any = found_any or key
                row.addWidget(label("✓ Found on this computer", "ok"), 1)
            else:
                row.addWidget(label("Not installed yet", "hint"), 1)
                how = QtGui.QPushButton("How to install")
                how.clicked.connect(lambda _=False, u=info["install_url"]: QtGui.QDesktopServices.openUrl(QtCore.QUrl(u)))
                row.addWidget(how)
            rb.toggled.connect(lambda on, k=key: on and self.pick_client(k))
            self.client_buttons[key] = (rb, ready)
            lay.addWidget(card)
        none_card = QtGui.QFrame()
        none_card.setObjectName("card")
        nrow = QtGui.QHBoxLayout(none_card)
        none_rb = QtGui.QRadioButton("No AI helper: I'll just watch and chat")
        group.addButton(none_rb)
        none_rb.toggled.connect(lambda on: on and self.pick_client(None))
        nrow.addWidget(none_rb)
        lay.addWidget(none_card)
        self.client_buttons[None] = (none_rb, True)

        self.missing_note = label("", "hint")
        lay.addWidget(self.missing_note)
        refresh = QtGui.QPushButton("I installed it, check again")
        refresh.clicked.connect(lambda: self.show_page("agent", remember=False))
        lay.addWidget(refresh, 0, QtCore.Qt.AlignLeft)

        self.folder_row = QtGui.QWidget()
        fr = QtGui.QHBoxLayout(self.folder_row)
        fr.setContentsMargins(0, 0, 0, 0)
        self.folder_label = label("", "hint")
        change = QtGui.QPushButton("Change…")
        change.clicked.connect(self.change_folder)
        fr.addWidget(self.folder_label, 1)
        fr.addWidget(change)
        lay.addWidget(self.folder_row)
        self.trust_box = QtGui.QCheckBox("Let Codex use CADSwarm in this folder (recommended: Codex ignores "
                                         "folders it hasn't been told to trust)")
        self.trust_box.setChecked(True)
        lay.addWidget(self.trust_box)
        lay.addStretch(1)
        (self.client_buttons[found_any][0] if found_any else none_rb).setChecked(True)
        return w

    def enter_agent(self):
        self.pick_client(self.client)

    def default_folder(self):
        # The helper's own workspace (it holds its sign-in), next to the shared files/, never inside them.
        base = panel.host_dir_for(self.db) if self.db else ""
        base = base or os.path.join(projects_dir(), safe_folder(self.project))
        return os.path.join(base, "AI helpers", f"{slug(self.name)}-{self.client}")

    def pick_client(self, key):
        self.client = key
        if key:
            # The host spawns the planners, workers and reviewer with this same CLI. It re-reads
            # host.json every couple of seconds, so this reaches a host started before this page.
            hosting.set_agent_client(self.p.GetString("HostDataDir") or getattr(self, "project_dir", ""), key)
        if not hasattr(self, "folder_row"):
            return
        installed = self.client_buttons.get(key, (None, True))[1]
        self.folder_row.setVisible(key is not None)
        self.trust_box.setVisible(key == "codex")
        if key and not self.folder_user_set:
            self.folder = self.default_folder()
        if key:
            self.folder_label.setText(f"Your helper's workspace folder:<br><b>{getattr(self, 'folder', '')}</b>")
        if key and not installed:
            self.missing_note.setText(f"Install {CLIENTS[key]['label']} first (click “How to install”), "
                                      "then click “check again”.")
            self.buttons("Connect my AI helper ›", None)
            self.primary.setEnabled(False)
        elif key:
            self.missing_note.setText("")
            self.buttons("Connect my AI helper ›", lambda: self.show_page("agent_work"))
        else:
            self.missing_note.setText("You can add an AI helper later from the CADSwarm toolbar (“Add AI helper”).")
            self.buttons("Finish", self.finish)

    def change_folder(self):
        d = QtGui.QFileDialog.getExistingDirectory(self, "Choose your helper's workspace folder",
                                                   getattr(self, "folder", projects_dir()))
        if d:
            self.folder, self.folder_user_set = os.path.normpath(d), True
            self.pick_client(self.client)

    def page_agent_work(self):
        w, lay = self.page("Connecting your AI helper…")
        self.agent_steps = Checklist(on_done=lambda: self.show_page("launch"))
        lay.addWidget(self.agent_steps, 1)
        return w

    def enter_agent_work(self):
        self.buttons(secondary="Cancel", secondary_action=self.reject)
        label_ = CLIENTS[self.client]["label"]
        steps = [("Get the helper software (one time, about 2 minutes)",
                  "Downloading the small programs your AI helper uses to reach the team. Needs internet.",
                  self.step_deps, False),
                 (f"Connect {label_} to your team", None, self.step_write_config, False)]
        if self.client == "codex" and self.trust_box.isChecked():
            steps.append(("Let Codex use this folder", None, lambda log: trust_in_codex(self.folder), False))
        self.agent_steps.set_steps(steps)
        self.agent_steps.start()

    def agent_invite(self, role):
        """A single-use agent invite, or (when this folder already signed in) just its team details."""
        team = {"v": 1, "uri": self.uri or self.p.GetString("Uri"), "db": self.db or self.p.GetString("Db"),
                "token": "", "kind": "agent", "role": role, "host_addr": ""}
        if identity_matches(self.folder, team):  # one signed in to another team, or the old hub, starts over
            return team
        return read_invite(panel.SESSION.client.create_invite("agent", role, 86400, self.project,
                                                              uri=hosting.invite_uri(panel.SESSION.client.hub)))

    def step_write_config(self, log):
        if not panel.SESSION.connected:
            raise RuntimeError("Join your team first (CADSwarm ▸ Get started), then add the AI helper.")
        self.agent_name = f"{slug(self.name)}-{self.client}"
        # An AI helper is always a plain member: the person who started the team leads and hosts it.
        written = write_agent_config(self.folder, self.client, self.agent_invite("member"), self.agent_name,
                                     python=self.python)
        written.append(write_launcher(self.folder, self.client, self.project))
        for f in written:
            log(f"wrote {f}")
        self.p.SetString("AgentClient", self.client)
        panel.remember_helper_folder(self.folder)

    # ---------------------------------------------------------------- launch

    def page_launch(self):
        label_ = CLIENTS[self.client]["label"]
        w, lay = self.page(f"Starting {label_}",
                           "Your helper runs in the background. There's no window to keep open and nothing to type "
                           "or paste: talk to it in the CADSwarm panel's Chat and it answers there.")
        self.open_status = label("", "hint")
        lay.addWidget(self.open_status)
        if self.client == "claude":
            lay.addWidget(label("If Claude Code has never been signed in on this computer, open it once in a "
                                "terminal (type <code>claude</code>) and sign in. After that it starts by itself.",
                                "hint"))
        if self.client == "codex":
            lay.addWidget(label("If Codex has never been signed in on this computer, open it once in a terminal "
                                "(type <code>codex</code>) and sign in. After that it starts by itself.", "hint"))
        lay.addSpacing(6)
        self.helper_status = label("⏳ Waiting for your AI helper to join the team…")
        lay.addWidget(self.helper_status)
        watch = QtGui.QPushButton("Open it in a terminal instead")
        watch.setToolTip("Only if you'd like to watch it work or type to it directly")
        watch.clicked.connect(self.open_helper)
        lay.addWidget(watch, 0, QtCore.Qt.AlignLeft)
        lay.addStretch(1)
        return w

    def enter_launch(self):
        self.buttons("Finish", self.finish)
        self.poll.start(2000)
        self.start_helper()

    def start_helper(self):
        from ..paths import LAUNCHER
        from ..runner import set_background, start_runner
        try:
            set_background(self.folder, True)
            start_runner(self.folder, self.python, LAUNCHER, self.project)
            self.open_status.setText("Started in the background ✓")
        except Exception as e:
            self.open_status.setText(f"<span style='color:#e5484d'>Couldn't start it: {e}</span>"
                                     " Try “Open it in a terminal instead”.")

    def open_helper(self):
        from ..runner import runner_alive, set_background, stop_runner
        try:
            set_background(self.folder, False)  # one copy at a time: the terminal one takes over
            if runner_alive(self.folder):
                stop_runner(self.folder)
            open_agent_terminal(self.client, self.folder)
            self.open_status.setText("Opened ✓. Look for a new window on your screen. Restart the background "
                                     "helper later with the Tools tab ▸ Restart my AI helpers.")
        except Exception as e:
            hint = (f"You can also open a terminal in <b>{self.folder}</b> and type "
                    f"<code>{CLIENTS[self.client]['exe']}</code>.")
            self.open_status.setText(f"<span style='color:#e5484d'>{e}</span> {hint}")

    def check_helper_joined(self):
        if not panel.SESSION.connected or not self.agent_name:
            return
        try:
            people = panel.SESSION.client.get("/participants")["participants"]
        except HubRequestError:
            return
        if any(p["name"] == self.agent_name and p["status"] == "active" for p in people):
            self.poll.stop()
            self.helper_status.setObjectName("ok")
            self.helper_status.setStyleSheet("color: #4fb35a; font-weight: 600;")
            self.helper_status.setText(f"✓ Your AI helper “{self.agent_name}” is on the team. Tell it what to "
                                       "build in the panel's Chat and watch the parts appear in "
                                       f"“{self.project or 'the live model'}”.")
            self.primary.setText("Done, show me the model")

    # ------------------------------------------------------------- reconnect

    def page_reconnect(self):
        w, lay = self.page(f"Reconnecting to “{self.project or 'your team'}”…")
        self.re_steps = Checklist(on_done=self.finish)
        lay.addWidget(self.re_steps, 1)
        return w

    def enter_reconnect(self):
        self.buttons(secondary="Cancel", secondary_action=self.reject)
        steps = []
        if self.p.GetString("HostDataDir"):
            steps += [("Check the helper software", None, self.step_deps, False),
                      ("Start your team's server", None, self.step_restart_team, False)]
        steps.append(("Join your team", None, self.step_rejoin, True))
        self.re_steps.set_steps(steps)
        self.re_steps.start()

    def step_restart_team(self, log):
        data_dir = self.p.GetString("HostDataDir")
        if not data_dir:
            return
        stored = hosting.host_config(data_dir)
        mode = self.p.GetString("HostMode") or stored.get("mode") or "local"
        db = self.p.GetString("Db") or stored.get("db")
        if not db:
            raise RuntimeError("This computer's team settings are incomplete. Start again from CADSwarm ▸ "
                               "Get started.")
        if hosting.team_ready(data_dir):
            log("Your team's server is already running.")
            status = hosting.host_status(data_dir)
            if status.get("pid"):
                self.p.SetString("HostPid", str(status["pid"]))  # a stale PID would make "Stop hosting" do nothing
            # Never adopt whatever listens on the local port for stopping: "Stop hosting" must only stop
            # what we started. But a local database bound only to this computer serves no teammate, so
            # rebind it now (only a SpacetimeDB process on our own port is touched; the host reconnects).
            hosting.ensure_local_reachable(mode, data_dir, log)
            return
        log("Starting your team's server")
        info = hosting.start_team(self.python, data_dir, mode, db, on_log=log)
        self.remember_host(info, data_dir, mode)
        try:
            hosting.wait_ready(data_dir, timeout=120, pids=[info["host_pid"], info["spacetime_pid"]])
        except Exception:
            hosting.stop_team(info)
            self.forget_pids()
            raise

    def step_rejoin(self, log):
        db = self.p.GetString("Db")
        ident = panel.load_identity(db) if db else {}
        if not (db and ident.get("token")):
            raise RuntimeError("Your sign-in for this team isn't on this computer any more. Join again with a "
                               "new invite code from a teammate (CADSwarm ▸ Get started ▸ Join).")
        try:
            panel.SESSION.connect({"uri": self.p.GetString("Uri"), "db": db}, ident, self.p.GetString("Name"),
                                  timeout=10)
        except HubRequestError as e:
            raise RuntimeError(panel.friendly_error(e, self.p.GetString("Uri"))) from e

    # ---------------------------------------------------------------- finish

    def finish(self):
        self.poll.stop()
        self.accept()
        panel.show_panel()
        if panel.SESSION.connected:
            panel.SESSION.show_model()  # what "Done, show me the model" promises
            panel.SESSION.request_refresh()


def run_wizard(reason=None, start=None):
    """Open the setup guide. Returns True if the user ends up in a team session."""
    panel.show_panel()
    dlg = SetupWizard(reason=reason, start=start)
    run = getattr(dlg, "exec", None) or dlg.exec_
    run()
    return panel.SESSION.connected


def stop_hosting():
    p = panel.prefs()
    if not p.GetString("HostDataDir"):
        QtGui.QMessageBox.information(Gui.getMainWindow(), "CADSwarm", "This computer isn't hosting a team.")
        return
    if not panel.confirm("Stop hosting your team? Everyone's AI helpers will lose their connection until you "
                         "start it again (CADSwarm ▸ Get started ▸ Reconnect). Your project is saved."):
        return
    panel.SESSION.disconnect()
    hosting.stop_team({"host_pid": p.GetString("HostPid"), "spacetime_pid": p.GetString("SpacetimePid")})
    for k in ("HostPid", "SpacetimePid"):
        p.SetString(k, "")
    panel.SESSION.set_state("disconnected", panel.STOPPED_HOSTING)
    App.Console.PrintMessage("[CADSwarm] Stopped hosting. Your project is saved.\n")
