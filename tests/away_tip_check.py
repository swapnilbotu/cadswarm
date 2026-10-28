"""Real (offscreen) FreeCAD: a planner/worker the host started on this computer is not called a
teammate's helper, and the last line of its log is shown whether or not it says "Error:". Run by
tests/test_away_tip.py with CADSWARM_TEST_PANEL_GUI=1."""
import json
import os
import sys
from pathlib import Path


def run_folder(host, name, log):
    run = host / "agents" / name
    (run / ".cadswarm").mkdir(parents=True)
    (run / ".cadswarm" / "config.json").write_text("{}", encoding="utf-8")
    (run / "agent.log").write_text(log, encoding="utf-8")
    return run


def main(out):
    import FreeCADGui  # noqa: F401  (the panel module needs FreeCAD's GUI modules)

    from cadswarm.gui import panel

    out = Path(out)
    host = out / "Host"
    key_error = run_folder(host, "planner-abc",
                           "\x1b[91m\x1b[1mError: \x1b[0mIncorrect API key provided: OsRkca5L********xfJk.\n")
    run_folder(host, "worker-trust",
               "> build \u00b7 deepseek-ai/DeepSeek-V4.1-Flash-1017\n"
               "This workspace has not been trusted. Open it once to approve it.\n")
    run_folder(host, "worker-done", "> build \u00b7 deepseek-ai/DeepSeek-V4.1-Flash-1017\nReported task t_1 done.\n")
    (host / "host.json").write_text(json.dumps({"db": "db-test"}), encoding="utf-8")

    prefs = panel.prefs()
    prefs.SetString("HostDataDir", str(host))
    prefs.SetString("Db", "db-test")

    def agent(name):
        return {"name": name, "kind": "agent", "client": "opencode", "status": "stale", "locks": []}

    helper = {"name": "swapnil-codex", "kind": "agent", "client": "codex", "status": "stale", "locks": []}
    failed = ("its last run failed (exit 2): error: unexpected argument '--full-auto' found")

    result = {
        "run": panel.host_agent_run("planner-abc", "db-test"),
        "hosted": panel.away_tip(agent("planner-abc"), None, "db-test"),
        "teammate": panel.away_tip(agent("elsewhere"), None, "db-test"),
        "last_line": panel.last_log_line(str(key_error)),
        "untrusted": panel.away_tip(agent("worker-trust"), None, "db-test"),
        "finished": panel.away_tip(agent("worker-done"), None, "db-test"),
        # A helper of this computer: the runner's own state decides the tip, and a dead runner must not
        # read as "starting" (it never comes back by itself).
        "local_stopped": panel.away_tip(helper, {"state": "stopped", "off": False}, "db-test"),
        "local_terminal": panel.away_tip(helper, {"state": "stopped", "off": True}, "db-test"),
        "local_offline": panel.away_tip(helper, {"state": "offline"}, "db-test"),
        "local_error": panel.away_tip(helper, {"state": "error", "detail": failed}, "db-test"),
        "local_starting": panel.away_tip(helper, {"state": "starting"}, "db-test"),
    }
    # Prefs still name the team this computer opened earlier (here: a dead one). The live team's db is
    # what the panel shows, and it must still find the agent the host started for it.
    prefs.SetString("Db", "dead-team")
    result["stale_prefs_run"] = panel.host_agent_run("planner-abc", "db-test")
    result["live_db"] = panel.away_tip(agent("planner-abc"), None, "db-test")
    result["other_team"] = panel.away_tip(agent("planner-abc"), None, "another-team")
    result["other_team_run"] = panel.host_agent_run("planner-abc", "another-team")
    result["no_db"] = panel.away_tip(agent("planner-abc"), None)
    Path(out, "away-tip.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main(sys.argv[1])
