"""Project cards' facts and picture files: what a person sees when picking a previous project, and the
one thing FreeCAD cannot give us from a saved model (the team server writes it without a thumbnail)."""
import json
import os
import time

from cadswarm import project_cards


def test_picture_files_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(project_cards, "app_dir", lambda: str(tmp_path))
    when = time.time() - 3600
    project_cards.save_facts("cadswarm-racingdrone22-4a9a", 118, when)
    assert project_cards.preview_path("cadswarm-racingdrone22-4a9a").endswith(".png")
    assert project_cards.load_facts("cadswarm-racingdrone22-4a9a") == {"parts": 118, "at": when}
    # No stray .tmp files: facts are swapped in whole, so a card never reads half a file.
    assert not [p.name for p in tmp_path.rglob("*.tmp*")]


def test_facts_survive_a_damaged_file(tmp_path, monkeypatch):
    monkeypatch.setattr(project_cards, "app_dir", lambda: str(tmp_path))
    path = project_cards.facts_path("team")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert project_cards.load_facts("team") == {}
    with open(path, "w", encoding="utf-8") as f:
        json.dump([1, 2, 3], f)
    assert project_cards.load_facts("team") == {}


def test_forget_drops_the_card(tmp_path, monkeypatch):
    monkeypatch.setattr(project_cards, "app_dir", lambda: str(tmp_path))
    project_cards.save_facts("team", 2)
    with open(project_cards.preview_path("team"), "wb") as f:
        f.write(b"png")
    assert project_cards.load_facts("team")["parts"] == 2
    project_cards.forget("team")
    assert project_cards.load_facts("team") == {}
    assert not os.path.exists(project_cards.preview_path("team"))
    project_cards.forget("team")  # twice is fine


def test_the_name_is_safe_for_a_file(tmp_path, monkeypatch):
    monkeypatch.setattr(project_cards, "app_dir", lambda: str(tmp_path))
    path = project_cards.preview_path("cadswarm/../weird:name?")
    filename = path.replace("/", "\\").rsplit("\\", 1)[-1]
    assert filename == "cadswarm_.._weird_name_.png", filename
    assert project_cards.preview_path("") == project_cards.preview_path("team")  # an empty db still has a card


def test_initials():
    assert project_cards.initials("RacingDrone22") == "RA"
    assert project_cards.initials("Formula1 Car") == "FC"
    assert project_cards.initials("  spaced  name ") == "SN"
    assert project_cards.initials("") == "?"
    assert project_cards.initials("!!!") == "?"


def test_how_long_ago():
    assert project_cards.ago(0) == "just now"
    assert project_cards.ago(89) == "just now"
    assert project_cards.ago(600) == "10 min ago"
    assert project_cards.ago(3600) == "1 hour ago"
    assert project_cards.ago(7200) == "2 hours ago"
    assert project_cards.ago(86400) == "1 day ago"
    assert project_cards.ago(3 * 86400) == "3 days ago"
    assert project_cards.ago(45 * 86400) == "1 month ago"
    assert project_cards.ago(70 * 86400) == "2 months ago"


def test_subtitle_says_what_the_project_is(tmp_path, monkeypatch):
    monkeypatch.setattr(project_cards, "app_dir", lambda: str(tmp_path))
    now = 1_000_000.0
    hosted = {"project": "Rocket", "db": "team-a", "host_data_dir": "C:/p/rocket", "at": now - 600}
    shared = {"project": "Car", "db": "team-b", "uri": "https://cloud", "at": now - 600}
    # Nothing seen yet: just where it lives and when it was last opened.
    assert project_cards.subtitle(hosted, now=now) == "on this computer \u00b7 opened 10 min ago"
    assert project_cards.subtitle(shared, now=now) == "shared \u00b7 opened 10 min ago"
    project_cards.save_facts("team-a", 118, now - 3600)
    project_cards.save_facts("team-b", 1, now - 60)
    assert project_cards.subtitle(hosted, now=now) == "118 parts \u00b7 on this computer \u00b7 seen 1 hour ago"
    assert project_cards.subtitle(shared, now=now) == "1 part \u00b7 shared \u00b7 seen just now"
