"""Builds the real CadSwarm Team panel in an offscreen FreeCAD, feeds it a team, and checks the layout.

    python panel_gui_check.py <out dir>

Writes panel-<w>x<h>.png screenshots and panel-verified.json. Run by test_panel_gui.py.
"""
import json
import sys
import time
from pathlib import Path

import FreeCADGui as Gui
Gui.showMainWindow()
# FreeCAD puts its Mod folders ahead of PYTHONPATH when it starts, so an installed CadSwarm
# would shadow this checkout. Test the repository, not whatever happens to be installed.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for _name in [n for n in sys.modules if n == "cadswarm" or n.startswith("cadswarm.")]:
    del sys.modules[_name]
from PySide import QtCore, QtGui  # noqa: E402

from cadswarm.gui import panel, style_ui  # noqa: E402

out = Path(sys.argv[1])
now = time.time()

STYLE_INBOX = {
    "rules": 4, "decisions": 3, "ready": 1, "weak": 1,
    "sections": [
        {"kind": "param_convention", "label": "Shared sizes", "count": 2, "decisions": [
            {"subject": "param:wall", "kind": "param_convention", "title": "Params.wall is 2.4 mm",
             "detail": "seen 6×, 2 people, confidence 62%", "also": ["People change Params.wall from 1.6 to 2.4"],
             "ids": ["r_wall1", "r_wall2"], "confidence": 0.62, "recommendation": "confirm",
             "source": "learned", "status": "candidate", "seen_at": now},
            {"subject": "param:lid_gap", "kind": "param_convention", "title": "Params.lid_gap is 0.3 mm",
             "detail": "seen 2×, confidence 26%", "also": [], "ids": ["r_gap"], "confidence": 0.26,
             "recommendation": "look", "source": "learned", "status": "candidate", "seen_at": now}]},
        {"kind": "recurring_problem", "label": "Review problems", "count": 1, "decisions": [
            {"subject": "problem:team:lid clips the usb plug", "kind": "recurring_problem",
             "title": "Reviews keep finding: Lid clips the USB plug (in 1 design)",
             "detail": "seen 2×, confidence 21%", "also": [], "ids": ["r_usb"], "confidence": 0.21,
             "recommendation": "dismiss", "source": "learned", "status": "candidate", "seen_at": now}]}],
    "used": [{"subject": "habit:expressions", "kind": "modelling_practice",
              "title": "Bind dimensions to Params with expressions", "detail": "confidence 100%",
              "also": [], "ids": ["r_habit"], "confidence": 1.0, "recommendation": "confirm",
              "source": "learned", "status": "confirmed", "seen_at": now}],
    "history": [{"subject": "param:old", "kind": "param_convention", "title": "Params.old is 9 mm",
                 "detail": "seen 1×, confidence 15%", "also": [], "ids": ["r_old"], "confidence": 0.15,
                 "recommendation": "dismiss", "source": "learned", "status": "expired", "seen_at": now}],
}


def member(i, name, kind="agent", client="claude", status="active", doing="", task=None, locks=()):
    return {"id": i, "name": name, "kind": kind, "client": client, "role": "member", "status": status,
            "doing": doing, "last_seen": now, "locks": list(locks), "task": task, "idle_s": 5}


snap = {
    "model": {"objects": [{"name": "Base", "label": "Base", "bbox": [0, 0, 0, 1, 1, 1], "locked_by": "ann-claude"}],
              "params": {"wall": 3.0, "pcb_l": 65.0},
              "participants": [member(1, "carol", kind="human"),
                               member(2, "ann-claude", task={"id": "t_1", "title": "Base with port cutouts"},
                                      locks=["Base"]),
                               member(3, "bob-codex", client="codex", doing="listening for instructions"),
                               member(4, "dee-opencode", client="opencode", status="stale")]},
    "tasks": [{"id": "t_1", "title": "Base with port cutouts for the USB-C and HDMI connectors", "status": "claimed",
               "owner_name": "ann-claude", "assignee": "ann-claude", "brief": "b_1", "detail": ""},
              {"id": "t_2", "title": "Lid", "status": "open", "owner_name": None, "assignee": "bob-codex", "brief": "b_1",
               "detail": ""},
              {"id": "t_3", "title": "Check strength of the stand", "status": "done", "owner_name": "bob-codex",
               "assignee": "bob-codex", "brief": None, "detail": ""}],
    "commands": [{"id": "m_1", "state": "done", "by_helper": {"ann-claude": "done"}, "to": "ann-claude",
                  "body": "make the walls 3 mm", "at": now - 300, "task_id": None}],
    "briefs": [{"id": "b_1", "text": "An enclosure for a Raspberry Pi 5", "status": "building", "by": "carol",
                "at": now - 600, "tasks": 2, "done": 0}],
    "reviews": [{"id": "r_1", "brief": "b_1", "round": 2, "passed": False, "at": now - 60,
                 "findings": ["Lid clip is 0.4 mm too short.", "USB-C cutout sits 0.6 mm low."]}],
    "workspace": {"bed": [256.0, 256.0, 256.0], "bed_x_mm": 256.0, "bed_y_mm": 256.0, "bed_z_mm": 256.0},
    "messages": [{"id": "m_1", "from": "carol", "to": "ann-claude", "kind": "steer", "body": "make the walls 3 mm",
                  "at": now - 300},
                 {"id": "m_2", "from": "ann-claude", "to": "carol", "kind": "chat",
                  "body": "On it: I'll thicken the walls to 3 mm and keep the port cutouts where they are.",
                  "at": now - 290},
                 {"id": "m_3", "from": "ann-claude", "to": "carol", "kind": "chat",
                  "body": "Done. The base walls are 3 mm now; the lid still clears the posts by 0.4 mm.",
                  "at": now - 120},
                 {"id": "m_4", "from": "bob-codex", "to": "carol", "kind": "chat",
                  "body": "Refused · held by ann-claude\nNothing changed. Ask them instead.",
                  "at": now - 60}],
}


class FakeClient:
    cfg = {"db": "team-db"}
    hub = "http://x"

    def get(self, path):
        if path == "/project/files":
            return {"files": [
                {"path": "notes/plan.md", "version": 2, "size": 1840, "author": "ann-claude",
                 "updated_at": now - 600},
                {"path": "bom.csv", "version": 1, "size": 640, "author": "bob-codex",
                 "updated_at": now - 2400}],
                "folders": ["notes"]}
        raise panel.HubRequestError(404, {"code": "not_found", "error": path})

    def post(self, path, body=None):
        if path == "/project/files/history":
            return {"path": body["path"], "current": {"path": body["path"], "version": 2, "size": 1840,
                                                      "author": "ann-claude", "updated_at": now - 600},
                    "older_versions": [{"version": 1, "size": 1500, "author": "bob-codex",
                                        "saved_at": now - 9000, "reason": "replaced"}]}
        if path == "/style/inbox":
            return STYLE_INBOX
        if path == "/style/resolve":
            return {"rules": []}
        raise panel.HubRequestError(404, {"code": "not_found", "error": path})
    def create_invite(self, kind, role, ttl, project, uri=None):
        from cadswarm import invite, ticket
        return invite.make_invite(ticket.decode(ticket.encode({
            "v": 1, "uri": uri or "https://maincloud.spacetimedb.com", "db": "team-db", "token": "ab" * 32,
            "kind": kind, "role": role, "host_addr": ""})), project)


s = panel.SESSION
s.client, s.ident, s.state = FakeClient(), {"name": "carol"}, "connected"
s.mirror = lambda: type("Doc", (), {"Objects": []})()
panel.start_local_helpers = lambda session, restart=False: []
p = panel.TeamPanel(s)
s.panel = p
s.apply_snapshot(snap)
s.apply_snapshot(snap)  # an unchanged refresh must not rebuild anything
# What AI helpers suggested waits on the Team tab, in plain words, with the yes/no right there. The
# learned style backlog is one summary card (the dialog is where the decisions are grouped and made).
p.fetching = True  # FakeClient can't be read; the suggestions arrive as if read from the team
p.show_suggestions({
    "setting": [panel.setting_suggestion({"id": 7, "setting": "tol_clearance", "label": "Clearance fit", "value": 0.3,
                                          "current": 0.2, "reason": "PLA prints tight on this printer",
                                          "by": "ann-claude", "component": None},
                                         {"kind": "number", "unit": "mm"})],
    "style": [panel.style_summary(STYLE_INBOX)]})

checks = {}
for w, h in ((340, 640), (520, 900)):
    p.resize(w, h)
    p.show()
    for _ in range(5):
        QtGui.QApplication.processEvents()
    scroll = p.widget()
    inner = scroll.widget()
    checks[f"{w}x{h}"] = {
        "no_horizontal_overflow": inner.width() <= scroll.viewport().width() + 1,
        "scrolls_when_short": scroll.verticalScrollBar().maximum() > 0 if h < 800 else True,
    }
    p.grab().save(str(out / f"panel-{w}x{h}.png"))
    for name, page in (("tasks", p.tasks_page), ("tools", p.tools_page)):
        p.show_tab(page)
        QtGui.QApplication.processEvents()
        p.grab().save(str(out / f"panel-{w}x{h}-{name}.png"))
    p.show_tab(p.team_page)

# The three panes the site shows: shared sizes, the project folder with history, and refusals as cards.
p.resize(520, 900)
p.show()
for _ in range(5):
    QtGui.QApplication.processEvents()
tabs = [p.tabs.tabText(i) for i in range(p.tabs.count())]
p.show_tab(p.sizes_page)
QtGui.QApplication.processEvents()
p.grab().save(str(out / "panel-520x900-sizes.png"))
sizes = [[p.sizes_page.rows.topLevelItem(i).text(c) for c in range(2)]
         for i in range(p.sizes_page.rows.topLevelItemCount())]
size_item = p.sizes_page.rows.topLevelItem(0)
sizes_editable = bool(p.sizes_page.rows.editTriggers() & QtGui.QAbstractItemView.DoubleClicked) \
    and bool(size_item.flags() & QtCore.Qt.ItemIsEditable)
# Typing in the Value cell must go through the same set_param path as the dialog and the site.
inline = []
real_change = panel.panes.change_param
panel.panes.change_param = lambda session, alias, value, done=None, failed=None: inline.append((alias, value))
try:
    size_item.setText(1, "70 mm")
    QtGui.QApplication.processEvents()
finally:
    panel.panes.change_param = real_change
sizes_inline = inline
p.show_tab(p.files_page)
p.files_page.reload(force=True)
QtGui.QApplication.processEvents()
p.grab().save(str(out / "panel-520x900-files.png"))
file_item = p.files_page.find_path("notes/plan.md")
p.files_page.tree.setCurrentItem(file_item)
for _ in range(5):
    QtGui.QApplication.processEvents()
files = [item.data(0, QtCore.Qt.UserRole) for item in
         [p.files_page.find_path(f["path"]) for f in p.files_page.listing["files"]]]
file_sizes = [p.files_page.find_path(f["path"]).text(2) for f in p.files_page.listing["files"]]
history = [[p.files_page.history.topLevelItem(i).text(c) for c in range(4)]
           for i in range(p.files_page.history.topLevelItemCount())]
chat_text = p.chat.toPlainText()
p.show_tab(p.team_page)

wide = sorted((w.minimumSizeHint().width(), type(w).__name__, w.objectName(), getattr(w, "text", lambda: "")()[:30],
               type(w.parent()).__name__, w.parent().objectName() if w.parent() else "")
              for w in p.widget().widget().findChildren(QtGui.QWidget)
              if w.minimumSizeHint().width() > 200)
(out / "panel-wide.json").write_text(json.dumps(wide, ensure_ascii=False, indent=1), encoding="utf-8")

# Docked in the main window, the panel follows the window when it is resized.
p.hide()
mw = Gui.getMainWindow()
mw.resize(1600, 1000)
mw.show()
panel.SESSION.panel = None
panel.show_panel = panel.show_panel  # noqa: PLW0127  (the real function, as the workbench calls it)
QtGui.QApplication.processEvents()
docked = panel.TeamPanel(s)
s.panel = docked
mw.addDockWidget(QtCore.Qt.RightDockWidgetArea, docked)
mw.resizeDocks([docked], [480], QtCore.Qt.Horizontal)
docked.window_width = mw.width()
docked.share = 480 / 1600
for _ in range(5):
    QtGui.QApplication.processEvents()
wide_w = docked.width()
mw.resize(1000, 700)
for _ in range(10):
    QtGui.QApplication.processEvents()
narrow_w = docked.width()
mw.grab().save(str(out / "window-1000x700.png"))
s.panel = p

# Typing @li offers the helpers whose names start that way.
p.msg.setText("@")
p.msg.setCursorPosition(1)
p.maybe_complete()
offered_all = [p.completer.completionModel().index(i, 0).data() for i in range(p.completer.completionCount())]
p.msg.setText("@b")
p.msg.setCursorPosition(2)
p.maybe_complete()
offered_b = [p.completer.completionModel().index(i, 0).data() for i in range(p.completer.completionCount())]
p.completer.popup().hide()
p.insert_mention("bob-codex")
filled = p.msg.text()
p.completed_at = 0

# "@bob-codex ..." gives bob-codex a task and wakes it; people can't be handed tasks.
sent = []


class Recorder:
    def post(self, path, body=None):
        sent.append((path, body))
        return {"id": "t_new"} if path == "/tasks" else {}


s.ensure_connected = lambda purpose: True
s.run_async = lambda work, done=None, purpose="", failed=None: work(Recorder())


def fake_act(method, path, body=None, done=None, purpose="", failed=None):
    sent.append((path, body))
    if callable(done):
        done({})  # the real client runs the done callback once the server accepted the request
    return {}


s.act_async = fake_act
p.msg.setText("@bob-codex make the lid 20 mm tall")
p.send()
p.msg.setText("@team a stand for a 7 mm phone")
p.send()
# A brief is not a chat message, so it must not leave a "sending…" bubble waiting for an echo, and the
# chat says where the request went instead.
team_outbox = [o["body"] for o in p.outbox]
team_notice = [n["text"] for n in s.notices]
team_chat = p.chat.toPlainText()
# Handing the brief over moved the panel to Tasks (that is where the request now lives); suggestions
# stay on the Team tab, so go back to read them.
p.show_tab(p.team_page)
suggestions_shown = p.suggest_box.isVisibleTo(p) and p.suggest_lay.count() == 3  # heading + two cards
# The style card is one summary row, not a yes/no per rule: its button opens the triage dialog. Patch the
# dialog so this check stays headless, and stop the panel's own re-read racing the fake client from a thread.
opened = []
real_show_style, real_fetch = style_ui.show_style, p.fetch_suggestions
style_ui.show_style = lambda: opened.append(True)
p.fetch_suggestions = lambda: None
review = next(b for b in p.suggest_box.findChildren(QtGui.QPushButton)
              if b.text() == "Review 3 decisions")
review.click()
p.answer(p.suggestions["setting"][0], True)
style_ui.show_style, p.fetch_suggestions = real_show_style, real_fetch
p.show_suggestions({"setting": [], "style": []})
suggestions_cleared = not p.suggest_box.isVisibleTo(p)
style_opened = opened == [True]

# The chat shows the instruction with its status and the helper's replies.
chat = p.chat.toPlainText()
brief = p.task_list.topLevelItem(0)

# A long chat follows the newest message; scrolling up stops that, keeps the place as messages arrive and shows
# a "new" button that jumps back down.
p.show()
p.show_tab(p.team_page)
long_chat = [{"id": f"m_l{i}", "from": "ann-claude", "to": "carol", "kind": "chat",
              "body": f"Progress note {i}: the base walls and the lid lip are still lined up.", "at": now + i}
             for i in range(40)]


def chat_snapshot(messages):
    s.apply_snapshot({**snap, "messages": snap["messages"] + messages})
    for _ in range(10):
        QtGui.QApplication.processEvents()


chat_snapshot(long_chat)
bar = p.chat.verticalScrollBar()
followed = bar.maximum() > 0 and p.chat_at_bottom() and not p.jump.isVisible()
bar.triggerAction(QtGui.QAbstractSlider.SliderToMinimum)  # what a wheel or drag does
for _ in range(5):
    QtGui.QApplication.processEvents()
jump_shown = p.jump.isVisible() and not p.chat_follow
chat_snapshot(long_chat + [{**long_chat[0], "id": f"m_n{i}", "at": now + 100 + i} for i in range(3)])
kept_place, unseen_text = bar.value() == 0, p.jump.text()
p.jump.click()
for _ in range(5):
    QtGui.QApplication.processEvents()
back_down = p.chat_at_bottom() and p.chat_follow and not p.jump.isVisible()

# Confirmations show in the panel, not only in FreeCAD's console.
panel.say("Created task “Lid”")
QtGui.QApplication.processEvents()
toast = p.toast.text() if p.toast.isVisible() else None
p.grab().save(str(out / "panel-toast.png"))
# The Remote tab is where a phone joins from: one code, its link, and the QR that link draws.
p.resize(340, 640)
p.show_remote()
for _ in range(5):
    QtGui.QApplication.processEvents()
remote = {
    "link": p.remote_link.text(),
    "qr_pixmap": not p.remote_qr.pixmap().isNull(),
    "qr_modules": len(panel.qr.rows(p.remote_url)),
    "note": p.remote_note.text(),
    "no_horizontal_overflow": p.widget().widget().width() <= p.widget().viewport().width() + 1,
}
p.grab().save(str(out / "panel-340x640-remote.png"))
p.show_tab(p.team_page)
# The banner's Invite button hands a teammate the code they paste into FreeCAD, not the phone's QR.
shown = []
info = QtGui.QMessageBox.information
QtGui.QMessageBox.information = staticmethod(lambda parent, title, text, *a, **k: shown.append(text))
QtGui.QApplication.clipboard().clear()
p.invite_button.click()
QtGui.QApplication.processEvents()
QtGui.QMessageBox.information = info
copied = QtGui.QApplication.clipboard().text()
invite_button = {"copied_code": copied.startswith("cadswarm-2:"), "code_in_dialog": bool(copied) and copied in "".join(shown),
                 "stayed_on_team_tab": p.tabs.currentWidget() is p.team_page}
# Run a simulation: a person fills the form and gets run_fea's request, no helper in the loop.
sim = panel.fea_ui.RunSimulationDialog(s)
sim.fixed.setText("zmin, cyl r=2")
sim.newtons.setText("80")
body = sim.request()
sim.analysis.setCurrentIndex(sim.analysis.findData("modal"))
run_simulation = {"parts": sim.part.count(), "target": body["target"], "fixed": body["fixed"],
                  "newtons": body["loads"][0]["newtons"], "material": body["material"],
                  "modal_hides_load": not sim.newtons.isEnabled()}
sim.close()
# A report that needs changes offers to send it to a helper, who reads it with fea_report and fixes the part.
report = {"id": "fea_1a2b3c4d", "title": "Linear static of Base", "analysis_name": "Linear static", "by": "carol",
          "verdict": "fail", "headline": "safety factor 0.8", "inputs": {"placeholders": []}, "files": {},
          "model": {"target": ["Base"]}}
real_post, sent_fix = s.client.post, []
s.client.post = lambda path, body=None: {"text": json.dumps(report) if body["path"].endswith(".json") else "<p>x</p>"}
dlg = panel.FeaReportDialog("fea/x")
s.client.post = real_post
pick, act = QtGui.QInputDialog.getItem, s.act_async
QtGui.QInputDialog.getItem = staticmethod(lambda parent, title, label, items, *a, **k: (items[0], True))
s.act_async = lambda method, path, body=None, **k: sent_fix.append((path, body))
dlg.fix_button.click()
QtGui.QInputDialog.getItem, s.act_async = pick, act
dlg.close()
fix_request = {"to": sent_fix[0][1]["to"], "kind": sent_fix[0][1]["kind"],
               "reads_report": "fea_report('fea_1a2b3c4d')" in sent_fix[0][1]["body"]} if sent_fix else None
# The Learned design style dialog is the triage inbox: one decision per subject, grouped in sections,
# a bulk "ready" block that lists every rule it would make, and an expired history you can restore.
style_posts = []
real_act = s.act
s.act = lambda method, path, body=None, done=None, purpose="do that": (style_posts.append((path, body)), {})[1]
dlg_style = style_ui.StyleDialog()
style_summary = dlg_style.summary.text()
style_sections = [dlg_style.pending_lay.itemAt(i).widget().title()
                  for i in range(dlg_style.pending_lay.count())
                  if isinstance(dlg_style.pending_lay.itemAt(i).widget(), QtGui.QGroupBox)]
style_ready = {"shown": not dlg_style.ready.isHidden(), "toggle": dlg_style.ready.toggle.text(),
               "apply": dlg_style.ready.apply_button.text(), "ids": dlg_style.ready.ids()}
style_weak = {"shown": not dlg_style.weak.isHidden(), "toggle": dlg_style.weak.toggle.text(),
              "apply": dlg_style.weak.apply_button.text()}
style_history = {"toggle": dlg_style.history_toggle.text(), "rows": dlg_style.history_lay.count(),
                 "shown": not dlg_style.history_toggle.isHidden()}
style_used = {"shown": not dlg_style.used.isHidden(), "rows": dlg_style.used_lay.count() - 1}
dlg_style.ready.apply_button.click()
s.act = real_act
dlg_style.close()
style_inbox = {"summary": style_summary, "sections": style_sections, "ready": style_ready, "weak": style_weak,
               "history": style_history, "used": style_used, "posted": style_posts}

# Opening the workbench must not freeze FreeCAD. A busy team takes tens of seconds to answer (the live
# model alone can be 10 MB), so the rejoin runs on a background thread and the window keeps painting;
# doing it in Activated() made Windows draw FreeCAD as "not responding" until the team was read.
class SlowTeam:
    """A team whose join and first read are slow, like a hosted one under load."""
    cfg = {"db": "team-db"}
    hub = "http://127.0.0.1:1"

    def mirror_changes(self, applied):
        time.sleep(0.4)
        return {}, [], {}

    def panel_snapshot(self):
        time.sleep(0.4)
        return snap

    def post(self, path, body=None):
        return {}

    def get(self, path, **params):
        return {"events": [], "seq": 0}


class FakeDoc:
    """Stands in for the live-model document: this check has no real one to draw into."""
    Objects = []
    Label = ""

    def recompute(self):
        return None

    def getObject(self, name):
        return None


class FakePrefs:
    def __init__(self, **values):
        self.values = values

    def GetString(self, key):
        return self.values.get(key, "")

    def SetString(self, key, value):
        self.values[key] = value


def startup_check():
    real = (panel.prefs, panel.load_identity, panel.save_identity, panel.remember_team, panel.host_dir_for,
            panel.Session._join, panel.Session._start_workers)
    started = []
    panel.prefs = lambda: FakePrefs(Uri="http://127.0.0.1:1", Db="team-db", HostDataDir="", Name="carol",
                                    ProjectName="Demo")
    panel.load_identity = lambda db: {"name": "carol", "token": "t" * 32, "seq": 0}
    panel.save_identity = lambda db, ident: None
    panel.remember_team = lambda *a: None
    panel.host_dir_for = lambda db: ""
    panel.Session._join = lambda self, cfg, identity, name, timeout, host_dir="": (SlowTeam(), dict(identity))
    panel.Session._start_workers = lambda self: started.append(self.state)
    try:
        s = panel.Session()
        s.mirror = lambda: FakeDoc()  # no real document in this check
        began = time.time()
        s.try_auto_rejoin()
        blocked = time.time() - began
        during = s.state
        for _ in range(400):  # what FreeCAD's event loop does while the person waits
            QtGui.QApplication.processEvents()
            s.drain()
            if s.state == "connected":
                break
            time.sleep(0.01)
        found = {"returned_fast": blocked < 0.3, "seconds_blocked": round(blocked, 3),
                 "state_while_joining": during, "connected": s.state == "connected",
                 "parts": len(s.model.get("objects", [])), "workers_started": started == ["connected"],
                 "joining_flag_cleared": s._joining is False}
        if s.timer:
            s.timer.stop()  # this check's session is not the panel's
        return found
    finally:
        (panel.prefs, panel.load_identity, panel.save_identity, panel.remember_team, panel.host_dir_for,
         panel.Session._join, panel.Session._start_workers) = real


startup = startup_check()


class PaintView:
    """A few isometric-looking lines, as `panel.iso_outline` would hand them over (no FreeCAD needed)."""

    def segments(self):
        return [((0.0, 0.0), (10.0, 0.0)), ((10.0, 0.0), (10.0, 6.0)), ((10.0, 6.0), (0.0, 6.0)),
                ((0.0, 6.0), (0.0, 0.0)), ((0.0, 0.0), (5.0, -4.0))]


def projects_check():
    """The project list people pick from: a picture when we have one, initials when we don't, and a
    launch that starts this computer's own team server (with Try again) instead of the whole guide."""
    real = (panel.prefs, panel.load_identity, panel.save_identity, panel.remember_team, panel.host_dir_for,
            panel.Session._join, panel.Session._start_workers, panel.Session._adopted, panel.hosting.team_ready,
            panel.hosting.start_team, panel.project_cards.app_dir, panel.JOIN_RETRY_SECONDS)
    saved = {}
    started, joins = [], []
    icons = out / "previews"
    icons.mkdir(parents=True, exist_ok=True)

    def join_ok(self, cfg, identity, name, timeout, host_dir=""):
        joins.append(cfg["db"])
        return SlowTeam(), dict(identity)

    def join_down(self, cfg, identity, name, timeout, host_dir=""):
        joins.append(cfg["db"])
        raise panel.HubRequestError(0, {"code": "unreachable", "error": "no server"})

    try:
        panel.project_cards.app_dir = lambda: str(icons)
        panel.projects_ui._cache.clear()
        panel.JOIN_RETRY_SECONDS = 0
        teams = [{"project": "Rocket", "db": "team-a", "uri": "http://127.0.0.1:3000", "host_data_dir": "C:/p/rocket",
                  "at": time.time() - 7200},
                 {"project": "Formula1 Car", "db": "team-b", "uri": "https://cloud", "at": time.time() - 86400}]
        wrote = panel.projects_ui.save_preview("team-a", PaintView().segments(), 118, time.time() - 3600)

        # The picture comes from the model's own edges: two boxes give 12 lines each, and both count.
        import Part

        class Box:
            Shape = Part.makeBox(10, 20, 5)

            class ViewObject:
                Visibility = True

        class Hidden(Box):
            class ViewObject:
                Visibility = False

        class ShapeDoc:
            Objects = [Box(), Box(), Hidden(), type("Empty", (), {"Shape": Part.Shape()})()]

        box_segments, box_parts = panel.iso_outline(ShapeDoc())
        corners = [p for seg in box_segments for p in seg]
        saved["outline"] = {"parts": box_parts, "segments": len(box_segments),
                            "flat_is_2d": all(len(p) == 2 for seg in box_segments for p in seg),
                            # (10, 0, 0) in a 30 degree isometric sits at (8.66, 5) on the card
                            "projected": [[round(p[0], 2), round(p[1], 2)] for p in corners
                                          if abs(p[0] - 8.66) < 0.01 and abs(p[1] - 5.0) < 0.01][:1]}
        dlg = panel.projects_ui.ProjectPicker(teams)
        cards = dlg.cards
        picture = cards[0].picture.pixmap()
        blank = cards[1].picture.pixmap()
        img = picture.toImage()
        drawn = sum(1 for x in range(0, img.width(), 2) for y in range(0, img.height(), 2)
                    if img.pixelColor(x, y).name() not in ("#151a21", "#000000"))
        saved["cards"] = {
            "count": len(cards),
            "wrote_picture": wrote is True and panel.project_cards.preview_path("team-a").endswith(".png"),
            "picture_shown": drawn > 40,  # the drawn outline, not a blank card
            "picture_fits": picture.width() <= 160 and picture.height() <= 120,
            "placeholder_for_missing": blank.toImage().pixelColor(4, 4).name() == "#20262e",
            "subtitle": cards[0].subtitle.text(),
            "first_selected": dlg.team == teams[0] and dlg.open_button.isEnabled(),
            "hosted_badge": "Hosted here" in [w.text() for w in cards[0].findChildren(QtGui.QLabel)],
            "no_badge_for_shared": "Hosted here" not in [w.text() for w in cards[1].findChildren(QtGui.QLabel)],
        }
        dlg.select(cards[1])
        saved["picking"] = {"team": dlg.team["db"], "enabled": dlg.open_button.isEnabled(),
                            "single_team_no_dialog": panel.projects_ui.pick_team([teams[0]]) is teams[0],
                            "no_teams": panel.projects_ui.pick_team([]) is None}
        dlg.deleteLater()

        # Launch with this computer hosting the team and its server stopped: start it, then join.
        panel.prefs = lambda: FakePrefs(Uri="http://127.0.0.1:1", Db="team-a", HostDataDir="C:/p/rocket",
                                        HostMode="local", Name="carol", ProjectName="Rocket")
        panel.load_identity = lambda db: {"name": "carol", "token": "t" * 32, "seq": 0}
        panel.save_identity = lambda db, ident: None
        panel.remember_team = lambda *a: None
        panel.host_dir_for = lambda db: "C:/p/rocket"
        panel.hosting.team_ready = lambda folder: False
        panel.hosting.start_team = lambda python, folder, mode, db: started.append((folder, mode, db))
        panel.Session._join = join_ok
        panel.Session._start_workers = lambda self: None
        real_adopted = panel.Session._adopted

        def safe_adopted(self, *a):  # a bug here would abort FreeCAD (offscreen): show it instead
            try:
                return real_adopted(self, *a)
            except Exception:
                traceback.print_exc()

        panel.Session._adopted = safe_adopted
        s = panel.Session()
        s.mirror = lambda: FakeDoc()  # no real document in this check
        progress = []
        real_set_state = s.set_state
        s.set_state = lambda state, detail="": (progress.append((state, detail)), real_set_state(state, detail))
        s.try_auto_rejoin()
        for _ in range(400):
            QtGui.QApplication.processEvents()
            s.drain()
            if s.state in ("connected", "disconnected"):
                break
            time.sleep(0.01)
        saved["auto_start"] = {"started": list(started), "connected": s.state == "connected",
                               "told_the_person": [d for _st, d in progress if d][:3]}
        if s.timer:
            s.timer.stop()

        # The same launch with nothing answering: the banner offers Try again, and one click gets in.
        # (This reuses the panel that is already docked in FreeCAD: a second, never-docked dock widget
        # upsets FreeCAD's own shutdown under the offscreen platform.)
        panel.Session._join = join_down
        bad = panel.Session()
        bad.mirror = lambda: FakeDoc()  # no real document in this check (offscreen has no 3D view)
        bad.panel = p
        p.session = bad
        bad.try_auto_rejoin()
        for _ in range(600):
            QtGui.QApplication.processEvents()
            bad.drain()
            if bad.state == "disconnected" and bad.rejoin_error:
                break
            time.sleep(0.01)
        banner = {"state": bad.state, "button": p.banner_button.text(), "headline": p.banner_text.text(),
                  "error_kept": bool(bad.rejoin_error), "tries": len(joins)}
        panel.Session._join = join_ok
        p.banner_action()  # what clicking Try again does
        for _ in range(400):
            QtGui.QApplication.processEvents()
            bad.drain()
            if bad.state == "connected":
                break
            time.sleep(0.01)
        banner["after_retry"] = bad.state
        banner["button_after"] = p.banner_button.text()
        saved["retry"] = banner
        if bad.timer:
            bad.timer.stop()
        p.session = s  # the panel goes back to the team the rest of this check rendered
        bad.panel = None
        return saved
    finally:
        (panel.prefs, panel.load_identity, panel.save_identity, panel.remember_team, panel.host_dir_for,
         panel.Session._join, panel.Session._start_workers, panel.Session._adopted, panel.hosting.team_ready,
         panel.hosting.start_team, panel.project_cards.app_dir, panel.JOIN_RETRY_SECONDS) = real


projects = projects_check()
result = {
    "layout": checks,
    "startup": startup,
    "projects": projects,
    "remote": remote,
    "invite_button": invite_button,
    "run_simulation": run_simulation,
    "fix_request": fix_request,
    "follows_window": {"at_1600": wide_w, "at_1000": narrow_w},
    "chat_has_reply": "lid still clears the posts" in chat and "✓ done" in chat,
    "offered_all": offered_all,
    "offered_b": offered_b,
    "filled": filled,
    "sent": sent[:3],
    "team_outbox": team_outbox,
    "team_notice": team_notice,
    "team_chat": team_chat,
    "answered": sent[3:],
    "suggestions_shown": suggestions_shown,
    "suggestions_cleared": suggestions_cleared,
    "style_opened": style_opened,
    "style_inbox": style_inbox,
    "helpers_listed": [p.team.topLevelItem(i).data(0, QtCore.Qt.UserRole) for i in range(p.team.topLevelItemCount())],
    "helper_rows": [[p.team.topLevelItem(i).text(c).lstrip("◐◓◑◒ ") for c in range(3)]
                    for i in range(p.team.topLevelItemCount())],
    "helper_header": [p.team.headerItem().text(c) for c in range(3)],
    "people": p.people.text(),
    "tasks_grouped_under_request": brief.text(0).startswith("🧠") and brief.childCount() == 2,
    "away_tip": p.team_tip.text(),
    "review": p.review_box.text(),
    "chat_jump": {"followed": followed, "jump_shown": jump_shown, "kept_place": kept_place,
                  "unseen_text": unseen_text, "back_down": back_down},
    "toast": toast,
    "tabs": tabs,
    "sizes": sizes,
    "sizes_editable": sizes_editable,
    "sizes_inline": sizes_inline,
    "files": files,
    "file_sizes": file_sizes,
    "history": history,
    "refusal_card": {"holder": "Refused · held by ann-claude" in chat_text,
                     "nothing_changed": "Nothing changed" in chat_text},
    # The team writes "Already locked: Base by ann-claude (active)"; the card names just the holder.
    "refusal_parse": list(panel.panes.parse_refusal(
        "Already locked: Base by ann-claude (active). Message the holder")),
}
(out / "panel-verified.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
QtCore.QTimer.singleShot(0, QtGui.QApplication.quit)
sys.exit(0)
