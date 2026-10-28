"""The Team panel in a real (offscreen) FreeCAD: fits a narrow dock without cropping, scrolls when short, shows
the helpers' replies in the chat and groups a whole-team request's tasks."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from test_structural_freecad import ROOT, runtime


@pytest.mark.skipif(os.environ.get("CADSWARM_TEST_PANEL_GUI") != "1", reason="Set CADSWARM_TEST_PANEL_GUI=1 for offscreen Qt integration")
def test_real_freecad_panel_layout_and_chat(tmp_path):
    python, env = runtime()
    env["QT_QPA_PLATFORM"] = "offscreen"
    profile = tmp_path / "freecad-profile"
    profile.mkdir()
    env["FREECAD_USER_HOME"] = env["FREECAD_USER_DATA"] = str(profile)
    proc = subprocess.run([python, str(ROOT / "tests" / "panel_gui_check.py"), str(tmp_path)], env=env,
                          capture_output=True, encoding="utf-8", errors="replace", timeout=240)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    r = json.loads(Path(tmp_path / "panel-verified.json").read_text(encoding="utf-8"))
    assert all(c["no_horizontal_overflow"] and c["scrolls_when_short"] for c in r["layout"].values()), r["layout"]
    assert r["chat_has_reply"] and r["tasks_grouped_under_request"]
    # A long chat follows the newest message; scrolled up, it keeps the place and offers a jump back down.
    assert r["chat_jump"] == {"followed": True, "jump_shown": True, "kept_place": True, "unseen_text": "↓  3 new",
                              "back_down": True}, r["chat_jump"]
    assert r["toast"] == "✓  Created task “Lid”"  # confirmations reach people who never open the console
    assert "teammate's computer" in r["away_tip"]
    # Opening the workbench keeps FreeCAD answering: the rejoin happens in the background, the panel
    # says "Joining…", and the team comes up (with its parts) once the slow team has answered.
    assert r["startup"] == {"returned_fast": True, "seconds_blocked": r["startup"]["seconds_blocked"],
                            "state_while_joining": "connecting", "connected": True, "parts": 1,
                            "workers_started": True, "joining_flag_cleared": True}, r["startup"]
    assert r["startup"]["seconds_blocked"] < 0.3, r["startup"]
    # Picking a previous project: a real picture when the panel has one, initials when it hasn't, and
    # the facts that tell projects apart ("118 parts · on this computer · seen 1 hour ago").
    proj = r["projects"]
    assert proj["cards"] == {"count": 2, "wrote_picture": True, "picture_shown": True, "picture_fits": True,
                             "placeholder_for_missing": True, "subtitle": proj["cards"]["subtitle"],
                             "first_selected": True, "hosted_badge": True, "no_badge_for_shared": True}, proj["cards"]
    assert proj["cards"]["subtitle"].startswith("118 parts \u00b7 on this computer \u00b7 seen "), proj["cards"]["subtitle"]
    assert proj["picking"] == {"team": "team-b", "enabled": True, "single_team_no_dialog": True,
                               "no_teams": True}, proj["picking"]
    assert proj["cards"]["hosted_badge"] is True, proj["cards"]
    # The card picture is the model's own edges in an isometric view: two boxes = 96 line pieces
    # (12 edges, 4 pieces each), a hidden part and an empty shape are skipped, and the projection
    # really is 30 degrees.
    assert proj["outline"] == {"parts": 2, "segments": 96, "flat_is_2d": True,
                               "projected": [[8.66, 5.0]]}, proj["outline"]
    # Opening FreeCAD with a team this computer hosts and its server stopped: the server is started and
    # joined without the setup guide, and the banner says what it is doing.
    auto = proj["auto_start"]
    assert auto["started"] == [["C:/p/rocket", "local", "team-a"]] and auto["connected"] is True, auto
    assert auto["told_the_person"][0].startswith("Starting your team"), auto["told_the_person"]
    assert any(t.startswith("Joining your team") for t in auto["told_the_person"]), auto["told_the_person"]
    # Nothing answering: three tries, a banner that says so with Try again, and one click gets in.
    retry = proj["retry"]
    assert retry["state"] == "disconnected" and retry["error_kept"] is True, retry
    assert retry["button"] == "Try again", retry
    assert "Couldn't reach your team just now." in retry["headline"], retry
    assert retry["tries"] == 4, retry  # the auto-start join + the 3 tries of the failing one
    assert retry["after_retry"] == "connected" and retry["button_after"] == "Leave", retry
    # The panes the site shows are real tabs: shared sizes, the project folder with history, refusals as cards.
    assert [t.split(" ")[0] for t in r["tabs"]] == ["Team", "Tasks", "Activity", "Sizes", "Files", "Library",
                                                     "Remote", "Tools"], r["tabs"]
    assert r["sizes"] == [["Pcb l", "65 mm"], ["Wall", "3 mm"]], r["sizes"]
    # Values edit in place, like the site's size fields; files show their size like the site's rows.
    assert r["sizes_editable"] is True, r["sizes_editable"]
    assert r["sizes_inline"] == [["pcb_l", "70 mm"]], r["sizes_inline"]
    assert sorted(r["files"]) == ["bom.csv", "notes/plan.md"], r["files"]
    assert r["file_sizes"] == ["1.8 KB", "640 B"], r["file_sizes"]
    assert r["history"][0][0] == "2 ✓" and r["history"][0][3] == "current version", r["history"]
    assert r["history"][1][0] == "1" and r["history"][1][3] == "replaced", r["history"]
    assert r["refusal_card"] == {"holder": True, "nothing_changed": True}, r["refusal_card"]
    assert r["refusal_parse"] == ["Refused · held by ann-claude", "Nothing changed. Ask them instead.",
                                  "ann-claude"], r["refusal_parse"]
    assert "Round 2 asks for changes" in r["review"] and "Lid clip is 0.4 mm too short." in r["review"]
    assert "256 x 256 x 256" in r["review"]
    # The Remote tab hands a phone one code: its link opens the public site, and the QR is that link.
    assert r["remote"]["link"].startswith("https://cadswarm.design/app/#t="), r["remote"]
    assert r["remote"]["no_horizontal_overflow"], r["remote"]
    assert r["remote"]["qr_pixmap"] == (r["remote"]["qr_modules"] > 0), r["remote"]
    assert "One code works once" in r["remote"]["note"], r["remote"]
    # The banner's Invite button copies the code a teammate pastes into FreeCAD (it does not open the QR).
    assert r["invite_button"] == {"copied_code": True, "code_in_dialog": True, "stayed_on_team_tab": True}, r["invite_button"]
    sim = r["run_simulation"]  # Tools > Run a simulation: the form becomes a run_fea request
    assert sim["parts"] >= 1 and sim["fixed"] == ["zmin", "cyl r=2"] and sim["newtons"] == 80.0, sim
    assert sim["material"] == "pla" and sim["modal_hides_load"], sim
    # A failed report: Ask a helper to fix this sends the helper a command that reads it with fea_report.
    assert r["fix_request"] == {"to": "ann-claude", "kind": "command", "reads_report": True}, r["fix_request"]
    assert r["helpers_listed"] == ["ann-claude", "bob-codex", "dee-opencode"]  # people aren't in the helper list
    assert "carol (you)" in r["people"]
    # Default names stay; the Task column says what each helper is on (or what's waiting for it).
    assert r["helper_header"] == ["Helper", "Task", "Status"]
    rows = {name.replace("🤖 ", ""): (task, status) for name, task, status in r["helper_rows"]}
    assert rows["ann-claude"] == ("Base with port cutouts", "Working")
    assert rows["bob-codex"] == ("next: Lid", "Ready")
    assert rows["dee-opencode"] == ("no task", "away")
    # @ offers every helper plus @team and @all; @b narrows it; picking one fills in "@name ".
    assert r["offered_all"][:3] == ["ann-claude", "bob-codex", "dee-opencode"] and {"team", "all"} <= set(r["offered_all"])
    assert r["offered_b"] == ["bob-codex"] and r["filled"] == "@bob-codex "
    # "@bob-codex ..." is a task for bob-codex plus the message that wakes it; "@team ..." is a whole-team request.
    (task_path, task), (msg_path, msg), (brief_path, brief) = r["sent"]
    assert task_path == "/tasks" and task["assignee"] == "bob-codex" and task["title"] == "make the lid 20 mm tall"
    assert msg_path == "/messages" and msg["to"] == "bob-codex" and msg["kind"] == "command" and msg["task_id"] == "t_new"
    assert brief_path == "/briefs" and brief["text"] == "a stand for a 7 mm phone"
    # ...and it adds no chat row: a brief is not echoed back, so an optimistic "sending…" row would
    # hang in the chat forever (and the next send would post the brief again). A local notice says where
    # the request went and that it is not a message.
    assert "a stand for a 7 mm phone" not in r["team_outbox"], r["team_outbox"]
    assert r["team_notice"] == ["Handed to the team: it plans the work and starts AI helpers. Watch the Tasks tab."]
    assert "Handed to the team: it plans the work" in r["team_chat"]
    # Suggestions sit on the Team tab; answering a setting sends it and takes it off the card. The learned
    # style backlog is one summary row whose button opens the triage dialog (it does not answer rules itself).
    assert r["suggestions_shown"] and r["suggestions_cleared"]
    assert r["style_opened"] is True, r["style_opened"]
    assert r["answered"] == [["/settings/suggestions/7", {"accept": True}]], r["answered"]
    # The dialog: one decision per subject, grouped in sections, with a bulk action that lists every rule
    # it would make, the weak set, the rules in use, and a recoverable expired history.
    inbox = r["style_inbox"]
    assert inbox["summary"] == ("4 things learned · 3 decisions · 1 ready to use, 1 weak. "
                                "Mostly 2 shared sizes, 1 review problems."), inbox["summary"]
    assert inbox["sections"] == ["Shared sizes · 2", "Review problems · 1"], inbox["sections"]
    assert inbox["ready"] == {"shown": True, "toggle": "Review the ready rules · 1",
                              "apply": "Make these rules · 1", "ids": ["r_wall1", "r_wall2"]}, inbox["ready"]
    assert inbox["weak"] == {"shown": True, "toggle": "Review the weak ones · 1",
                             "apply": "Dismiss these · 1"}, inbox["weak"]
    assert inbox["history"] == {"toggle": "Expired or dismissed · 1", "rows": 1, "shown": True}, inbox["history"]
    assert inbox["used"] == {"shown": True, "rows": 1}, inbox["used"]
    # Applying the ready block answers every rule behind the decision in one request.
    assert inbox["posted"] == [["/style/resolve", {"rule_ids": ["r_wall1", "r_wall2"],
                                                  "decision": "confirm"}]], inbox["posted"]
