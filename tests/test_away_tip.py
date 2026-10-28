"""A host-started helper is on this computer, not a teammate's, and its last log line is quoted where a
person looks (the Team tab tip) whether it says "Error:" or not."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from test_structural_freecad import ROOT, runtime


@pytest.mark.skipif(os.environ.get("CADSWARM_TEST_PANEL_GUI") != "1", reason="Set CADSWARM_TEST_PANEL_GUI=1 for offscreen Qt integration")
def test_a_host_started_helper_is_not_called_a_teammates(tmp_path):
    python, env = runtime()
    env["QT_QPA_PLATFORM"] = "offscreen"
    profile = tmp_path / "freecad-profile"
    profile.mkdir()
    env["FREECAD_USER_HOME"] = env["FREECAD_USER_DATA"] = str(profile)
    proc = subprocess.run([python, str(ROOT / "tests" / "away_tip_check.py"), str(tmp_path)], env=env,
                          capture_output=True, encoding="utf-8", errors="replace", timeout=240)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    r = json.loads(Path(tmp_path / "away-tip.json").read_text(encoding="utf-8"))
    run = os.path.join("agents", "planner-abc")
    assert r["run"].endswith(run) and r["stale_prefs_run"].endswith(run), r
    # The host's own agents: this computer, and the failure to act on instead of a raw log line.
    assert "runs on this computer" in r["hosted"], r
    assert "teammate's computer" not in r["hosted"], r
    assert r["last_line"] == "Error: Incorrect API key provided: OsRkca5L********xfJk.", r
    assert "Its last start failed:" in r["hosted"] and "Incorrect API key" in r["hosted"], r
    assert "\x1b" not in r["hosted"], r
    # A CLI that never prints "Error:" still shows why it stopped (a line with "not trusted").
    assert "Its last start failed:" in r["untrusted"] and "not been trusted" in r["untrusted"], r
    # A run that ended normally shows its last line too, but is not called a failure.
    assert "Its log ends with:" in r["finished"], r
    assert "Reported task t_1 done." in r["finished"], r
    assert "failed" not in r["finished"], r
    assert "again.." not in r["hosted"] + r["untrusted"] + r["finished"], r
    # Prefs naming the team opened earlier (a dead one) must not hide the live team's host agent.
    assert r["live_db"] == r["hosted"], r
    # Another team's host, a helper with no folder here, and no live db keep the honest teammate wording.
    assert r["other_team_run"] == "", r
    assert "teammate's computer" in r["teammate"], r
    assert "teammate's computer" in r["other_team"], r
    assert "teammate's computer" in r["no_db"], r
    # A helper of this computer whose runner is gone says so and offers the fix, instead of "it answers in a
    # moment" for ever (that is what a friend saw while Codex rejected every wake with exit 2).
    assert "isn't running" in r["local_stopped"] and "Restart my AI helpers" in r["local_stopped"], r
    assert "starting" not in r["local_stopped"], r
    assert "run in a terminal" in r["local_terminal"], r
    assert "can't reach the team" in r["local_offline"], r
    assert "unexpected argument" in r["local_error"] and "--full-auto" in r["local_error"], r
    assert "Show my AI helpers' logs" in r["local_error"], r
    assert "starting" in r["local_starting"], r
