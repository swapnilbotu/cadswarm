"""Project cards for picking a previous project: a picture of the shared model and what is in it.

A person choosing what to open should see *what it is*, not a database name. The picture and facts
come from `cadswarm.project_cards` (written whenever the panel shows a team's live model); this module
only draws them: the card list in the panel's "Open a previous project…" and the icons in the setup
guide's list of past projects.
"""
from PySide import QtCore, QtGui

import os

from .. import project_cards

THUMB_W, THUMB_H = 160, 120     # what a card shows (4:3, like the 3D view)
SHOT_W, SHOT_H = 480, 360       # what we draw and keep; big enough for a crisp thumbnail

_cache = {}  # (db, w, h) -> (mtime, pixmap)


def render_preview(segments, size=(SHOT_W, SHOT_H)):
    """Draw a model's outline (a list of ((x1, y1), (x2, y2)) in one flat projection) as a card picture.

    The 3D view cannot give us this: FreeCAD's `view.saveImage()` comes back blank unless the person
    is looking at that tab, and the team server saves model.FCStd without a thumbnail. Drawing the
    edges ourselves always works and looks like the model: an isometric outline on the panel's own
    dark background.
    """
    w, h = int(size[0]), int(size[1])
    pix = QtGui.QPixmap(w, h)
    pix.fill(QtGui.QColor("#151a21"))
    if not segments:
        return pix
    xs = [p[0] for seg in segments for p in seg]
    ys = [p[1] for seg in segments for p in seg]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    span = max(maxx - minx, maxy - miny, 1e-6)
    pad = 0.10 * min(w, h)
    scale = min((w - 2 * pad) / max(maxx - minx, 1e-6), (h - 2 * pad) / max(maxy - miny, 1e-6))
    scale = min(scale, (w - 2 * pad) / span if span else scale)
    ox = (w - (maxx - minx) * scale) / 2 - minx * scale
    oy = (h - (maxy - miny) * scale) / 2 - miny * scale
    painter = QtGui.QPainter(pix)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setPen(QtGui.QPen(QtGui.QColor("#7cc4ff"), 1.0))
    for (x1, y1), (x2, y2) in segments:
        painter.drawLine(QtCore.QPointF(x1 * scale + ox, y1 * scale + oy),
                         QtCore.QPointF(x2 * scale + ox, y2 * scale + oy))
    painter.end()
    return pix


def save_preview(db, segments, parts=0, when=None):
    """Keep a project's picture (drawn from its model) plus what was in it. Never raises."""
    if not db or not segments:
        return False
    try:
        import os
        os.makedirs(project_cards.preview_dir(), exist_ok=True)
        pix = render_preview(segments)
        tmp = project_cards.preview_path(db) + ".tmp.png"
        if not pix.save(tmp, "PNG"):
            return False
        os.replace(tmp, project_cards.preview_path(db))
    except Exception:  # noqa: BLE001 - a missing picture must never break the panel
        return False
    project_cards.save_facts(db, parts, when)
    _cache.clear()
    return True



def load_preview(db, size=(THUMB_W, THUMB_H)):
    """The project's last picture, fitted into `size`, or None when it has none yet."""
    path = project_cards.preview_path(db)
    key = (db, int(size[0]), int(size[1]))
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _cache.pop(key, None)
        return None
    hit = _cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    pix = QtGui.QPixmap(path)
    if pix.isNull():
        return None
    pix = pix.scaled(int(size[0]), int(size[1]), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
    _cache[key] = (mtime, pix)
    return pix


def placeholder(team, size=(THUMB_W, THUMB_H)):
    """What a project without a picture shows: its initials on CADSwarm's blue, so the card still reads
    as "this project" instead of a broken image. Drawn, so there is no asset to ship."""
    w, h = int(size[0]), int(size[1])
    pix = QtGui.QPixmap(w, h)
    pix.fill(QtGui.QColor("#20262e"))
    painter = QtGui.QPainter(pix)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setPen(QtCore.Qt.NoPen)
    painter.setBrush(QtGui.QColor("#2E86DE"))
    d = int(h * 0.60)
    painter.drawEllipse(QtCore.QRect(int(w / 2 - d / 2), int(h / 2 - d / 2), d, d))
    font = painter.font()
    font.setPointSize(max(7, int(h * 0.22)))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QtGui.QColor("#ffffff"))
    painter.drawText(pix.rect(), QtCore.Qt.AlignCenter, project_cards.initials(team.get("project")))
    painter.end()
    return pix


def icon_for(team, size=24):
    """A small icon for a button or list row: the project's picture, or its initials."""
    db = team.get("db")
    pix = load_preview(db, (size, size)) if db else None
    if pix is None or pix.isNull():
        pix = placeholder(team, (size, size))
    return QtGui.QIcon(pix)


class ProjectCard(QtGui.QFrame):
    """One project: its picture, its name, and what is inside it."""

    def __init__(self, team, parent=None):
        super().__init__(parent)
        self.team = team
        self.setObjectName("projectCard")
        self.setFrameShape(QtGui.QFrame.NoFrame)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        lay = QtGui.QHBoxLayout(self)
        lay.setContentsMargins(8, 8, 10, 8)
        lay.setSpacing(12)
        self.picture = QtGui.QLabel()
        self.picture.setFixedSize(THUMB_W, THUMB_H)
        self.picture.setAlignment(QtCore.Qt.AlignCenter)
        image = load_preview(team.get("db"))
        self.picture.setPixmap(image if image is not None and not image.isNull() else placeholder(team))
        lay.addWidget(self.picture)
        text = QtGui.QVBoxLayout()
        text.setSpacing(2)
        self.title = QtGui.QLabel(team.get("project") or "Project")
        font = self.title.font()
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        self.title.setFont(font)
        self.subtitle = QtGui.QLabel(project_cards.subtitle(team))
        self.subtitle.setStyleSheet("color:#8b949e")
        text.addStretch(1)
        text.addWidget(self.title)
        text.addWidget(self.subtitle)
        if team.get("host_data_dir"):
            host = QtGui.QLabel("Hosted here")
            host.setStyleSheet("color:#3fb950")
            text.addWidget(host)
        text.addStretch(1)
        lay.addLayout(text, 1)
        self.set_selected(False)

    def set_selected(self, on):
        self.setStyleSheet("#projectCard { border: 2px solid %s; border-radius: 8px; }"
                           % ("#2E86DE" if on else "transparent"))

    def picker(self):
        parent = self.parentWidget()
        while parent is not None and not isinstance(parent, ProjectPicker):
            parent = parent.parentWidget()
        return parent

    def mousePressEvent(self, event):  # noqa: N802 - Qt naming
        picker = self.picker()
        if picker is not None:
            picker.select(self)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802 - Qt naming
        picker = self.picker()
        if picker is not None:
            picker.select(self)
            picker.accept()
        super().mouseDoubleClickEvent(event)


class ProjectPicker(QtGui.QDialog):
    """Pick a project from its picture. `.team` is the chosen team dict once accepted."""

    def __init__(self, teams, parent=None, title="Open a previous project"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.team = None
        self.cards = []
        outer = QtGui.QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 12)
        outer.setSpacing(10)
        lead = QtGui.QLabel("Pick up where you left off:")
        lead.setStyleSheet("font-weight:600")
        outer.addWidget(lead)
        scroll = QtGui.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtGui.QFrame.NoFrame)
        inner = QtGui.QWidget()
        rows = QtGui.QVBoxLayout(inner)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(6)
        for t in teams:
            card = ProjectCard(t, inner)
            rows.addWidget(card)
            self.cards.append(card)
        rows.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        buttons = QtGui.QHBoxLayout()
        self.open_button = QtGui.QPushButton("Open")
        self.open_button.setObjectName("primary")
        self.open_button.setDefault(True)
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.accept)
        self.cancel_button = QtGui.QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.open_button)
        outer.addLayout(buttons)
        if self.cards:
            self.select(self.cards[0])
        self.resize(560, min(640, 170 + 136 * len(self.cards)))

    def select(self, card):
        for c in self.cards:
            c.set_selected(c is card)
        self.team = card.team
        self.open_button.setEnabled(True)

    def selected_index(self):
        for i, c in enumerate(self.cards):
            if c.team is self.team:
                return i
        return 0

    def keyPressEvent(self, event):  # noqa: N802 - Qt naming
        if self.cards and event.key() in (QtCore.Qt.Key_Down, QtCore.Qt.Key_Up):
            step = 1 if event.key() == QtCore.Qt.Key_Down else -1
            self.select(self.cards[(self.selected_index() + step) % len(self.cards)])
            return
        super().keyPressEvent(event)


def pick_team(teams, parent=None):
    """Ask which of `teams` to reopen. Returns the chosen team dict, or None if they cancelled."""
    if not teams:
        return None
    if len(teams) == 1:
        return teams[0]
    dlg = ProjectPicker(teams, parent)
    run = getattr(dlg, "exec", None) or dlg.exec_
    return dlg.team if run() == QtGui.QDialog.Accepted else None
