"""Setup pieces the FreeCAD guide relies on: invite codes, agent wiring, Codex trust, the launcher."""
import json
import os
import subprocess
import sys
import tomllib

import pytest

from cadswarm.agent_setup import codex_is_trusted, ensure_codex_approval, trust_in_codex, write_agent_config
from cadswarm.invite import InviteError, make_invite, read_invite
from cadswarm.paths import LAUNCHER


@pytest.fixture(autouse=True)
def no_network_redeem(monkeypatch):
    """Setup redeems the invite with SpacetimeDB; these tests are about the files it writes."""
    from cadswarm import agent_setup
    monkeypatch.setattr(agent_setup, "redeem", lambda t, name, client: {
        "token": "tok", "participant_id": 4, "fence": 1, "name": name, "role": t["role"], "kind": "agent"})


TICKET = {"v": 1, "uri": "http://192.168.1.20:3000", "db": "cadswarm", "token": "ab" * 32,
          "kind": "human", "role": "member", "host_addr": ""}


def test_invite_round_trip_survives_chat_app_mangling():
    code = make_invite(TICKET, "Phone stand")
    mangled = f'  "{code[:20]}\n{code[20:]}"  '  # quotes, spaces and a line break from a chat app
    assert read_invite(mangled) == {**TICKET, "project": "Phone stand"}


@pytest.mark.parametrize("text, fragment", [
    ("", "Paste the invite code"),
    ("http://192.168.1.20:8765", "doesn't look like a CADSwarm invite"),
    ("cadswarm-2:eyJ1cmkiOiJodHR0", "cut off"),
    ("cadswarm-1:eyJoIjoiaHR0", "older CADSwarm"),
])
def test_invite_errors_are_plain_language(text, fragment):
    with pytest.raises(InviteError) as e:
        read_invite(text)
    assert fragment in str(e.value)


def test_claude_config_uses_launcher_and_keeps_other_hooks(tmp_path):
    proj = tmp_path / "carol-claude"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.json").write_text(json.dumps(
        {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other-tool"}]}]}}))
    py = r"C:\FreeCAD\bin\python.exe"
    for _ in range(2):  # idempotent
        write_agent_config(str(proj), "claude", {**TICKET, "role": "lead"}, "carol-claude", python=py)
    mcp = json.loads((proj / ".mcp.json").read_text())["mcpServers"]["cadswarm"]
    assert mcp["command"] == py and mcp["args"] == [LAUNCHER, "mcp"]
    hooks = json.loads((proj / ".claude" / "settings.json").read_text())["hooks"]["UserPromptSubmit"]
    commands = [h["command"] for g in hooks for h in g["hooks"]]
    assert commands.count(f'"{py}" "{LAUNCHER}" hook') == 1 and "other-tool" in commands
    cfg = json.loads((proj / ".cadswarm" / "config.json").read_text())
    assert cfg == {"uri": TICKET["uri"], "db": "cadswarm", "name": "carol-claude", "client": "claude",
                   "role": "lead"}


def test_codex_config_parses_and_trust_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codexhome"))
    proj = tmp_path / "it's dana-codex"  # an apostrophe forces the basic-string TOML path
    write_agent_config(str(proj), "codex", TICKET, "dana-codex", python=r"C:\FC\bin\python.exe")
    cfg = tomllib.loads((proj / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert cfg["mcp_servers"]["cadswarm"]["args"] == [LAUNCHER, "mcp"]
    assert cfg["mcp_servers"]["cadswarm"]["env"]["CADSWARM_PROJECT"] == str(proj)
    # A background run can't answer Codex's approval prompt: without this every cadswarm call is refused.
    assert cfg["mcp_servers"]["cadswarm"]["default_tools_approval_mode"] == "approve"
    assert "hook" in cfg["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert not codex_is_trusted(str(proj))
    assert trust_in_codex(str(proj)) is True and trust_in_codex(str(proj)) is False
    home = tomllib.loads((tmp_path / "codexhome" / "config.toml").read_text(encoding="utf-8"))
    assert home["projects"][str(proj)]["trust_level"] == "trusted"


def test_an_old_codex_helper_gets_the_mcp_approval_added(tmp_path):
    proj = tmp_path / "old-helper"
    (proj / ".codex").mkdir(parents=True)
    cfg = proj / ".codex" / "config.toml"
    cfg.write_text("# my own note\n[mcp_servers.cadswarm]\ncommand = 'x'\nargs = ['mcp']\n\n"
                   "[mcp_servers.other]\ncommand = 'y'\ndefault_tools_approval_mode = 'prompt'\n")
    assert ensure_codex_approval(str(proj)) is True
    assert ensure_codex_approval(str(proj)) is False  # idempotent
    text = cfg.read_text()
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["cadswarm"]["default_tools_approval_mode"] == "approve"
    assert parsed["mcp_servers"]["cadswarm"]["command"] == "x"
    assert parsed["mcp_servers"]["other"]["default_tools_approval_mode"] == "prompt"  # untouched
    assert "# my own note" in text
    # A folder with no Codex config at all is left alone.
    assert ensure_codex_approval(str(tmp_path / "no-config")) is False


def test_launcher_hook_fails_open_outside_a_project(tmp_path):
    r = subprocess.run([sys.executable, LAUNCHER, "hook"], input=json.dumps({"cwd": str(tmp_path)}),
                       capture_output=True, text=True, timeout=30, cwd=tmp_path)
    assert r.returncode == 0 and r.stdout == ""


def test_launcher_check_reports_deps(tmp_path):
    r = subprocess.run([sys.executable, LAUNCHER, "check"], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "CADSWARM_DEPS": str(tmp_path / "none")})
    assert r.stdout.strip() == "ok"  # the dev venv already has the packages


def test_local_ips_default_route_first_and_filters(monkeypatch):
    from cadswarm import hosting
    monkeypatch.setattr(hosting, "lan_ip", lambda: "192.168.1.5")
    fake = [(2, 1, 6, "", (ip, 0)) for ip in ("127.0.0.1", "169.254.3.4", "100.64.1.9", "192.168.1.5", "10.0.0.2")]
    monkeypatch.setattr(hosting.socket, "getaddrinfo", lambda *a, **k: fake)
    assert hosting.local_ips() == ["192.168.1.5", "100.64.1.9", "10.0.0.2"]


def test_local_ips_falls_back_when_lookup_fails(monkeypatch):
    from cadswarm import hosting

    def boom(*a, **k):
        raise OSError("no dns")
    monkeypatch.setattr(hosting, "lan_ip", lambda: "10.1.1.1")
    monkeypatch.setattr(hosting.socket, "getaddrinfo", boom)
    assert hosting.local_ips() == ["10.1.1.1"]


def test_the_gui_helper_python_lookup_is_exported():
    # Every GUI module imports this; appending code to paths.py once swallowed the `def` by accident,
    # which only shows up inside FreeCAD because the tests never import the GUI.
    from cadswarm.paths import freecad_python
    assert callable(freecad_python)


def test_the_guides_cli_choice_is_written_to_the_hosts_settings(tmp_path):
    """The host re-reads host.json, so picking a CLI after the team started must change it in place."""
    from cadswarm import hosting
    assert hosting.set_agent_client(str(tmp_path), "codex") is False  # never hosted here: nothing to edit
    (tmp_path / "host.json").write_text(json.dumps({"mode": "local", "db": "team", "agent_client": "claude"}),
                                        encoding="utf-8")
    assert hosting.set_agent_client(str(tmp_path), "codex") is True
    data = json.loads((tmp_path / "host.json").read_text(encoding="utf-8"))
    assert data["agent_client"] == "codex" and data["db"] == "team"  # the rest of the settings are kept
    assert hosting.set_agent_client(str(tmp_path), "codex") is True  # already chosen: no error
    assert hosting.set_agent_client(str(tmp_path), "") is False      # "just watch" clears the choice in the GUI only
    assert hosting.set_agent_client("", "codex") is False            # no folder yet: never write ./host.json

