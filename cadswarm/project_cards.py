"""What a person needs to recognise a project before opening it: a picture and a line of facts.

Every team this computer has joined keeps its sign-in under the app folder (`panel.identity_path`);
this is the same idea for what the project looked like — the last picture the panel drew of the
shared model, and how many parts it had when we last saw it. Nothing is written into the project
folder (the team server owns that), and nothing here is team data the server has to know about.

Stdlib only: the picture itself is drawn with Qt by `cadswarm.gui.projects_ui`, from outlines the
panel takes off the live model, so this module stays testable without FreeCAD or Qt.
"""
import json
import os
import re
import time

from .paths import app_dir


def preview_dir():
    return os.path.join(app_dir(), "gui", "previews")


def _safe(db):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", db or "team")


def preview_path(db):
    return os.path.join(preview_dir(), _safe(db) + ".png")


def facts_path(db):
    return os.path.join(preview_dir(), _safe(db) + ".json")


def load_facts(db):
    try:
        with open(facts_path(db), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_facts(db, parts=0, when=None):
    data = {"parts": int(parts or 0), "at": float(when if when is not None else time.time())}
    try:
        os.makedirs(preview_dir(), exist_ok=True)
        tmp = facts_path(db) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, facts_path(db))
    except OSError:
        pass
    return data


def forget(db):
    """Drop a project's picture and facts (a team left for good, or a card that no longer matches)."""
    for path in (preview_path(db), facts_path(db)):
        try:
            os.remove(path)
        except OSError:
            pass


def initials(name):
    words = [w for w in "".join(c if c.isalnum() else " " for c in (name or "")).split() if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return "%d min ago" % (seconds // 60)
    if seconds < 86400:
        hours = seconds // 3600
        return "%d hour%s ago" % (hours, "" if hours == 1 else "s")
    days = seconds // 86400
    if days < 30:
        return "%d day%s ago" % (days, "" if days == 1 else "s")
    months = days // 30
    return "%d month%s ago" % (months, "" if months == 1 else "s")


def subtitle(team, now=None):
    """One line under the project name: what is in it, where it lives, and when we last saw it."""
    now = time.time() if now is None else now
    facts = load_facts(team.get("db"))
    bits = []
    if facts.get("parts"):
        parts = int(facts["parts"])
        bits.append("%d part%s" % (parts, "" if parts == 1 else "s"))
    bits.append("on this computer" if team.get("host_data_dir") else "shared")
    if facts.get("at"):
        bits.append("seen " + ago(now - float(facts["at"])))
    elif team.get("at"):
        bits.append("opened " + ago(now - float(team["at"])))
    return " \u00b7 ".join(bits)
