"""Stdlib-only pieces shared by the MCP server, the prompt hook and the FreeCAD workbench
(which runs in FreeCAD's own Python, without our venv): the error every caller catches,
JSON files, and finding a set-up project folder.

A project folder that has been set up holds two files:
  .cadswarm/config.json    {uri, db, name, client, role}                  written by `cadswarm setup`
  .cadswarm/identity.json  {token, participant_id, fence, name, role}     written on join
Re-joining with the stored identity resumes the same participant at fence+1,
which makes any older process using that identity unable to act.
"""
import json
import os
import time


class HubRequestError(Exception):
    """A plain-language failure from the team (`code` is e.g. "bad_invite" or "unreachable")."""

    def __init__(self, status, body):
        self.status, self.body = status, body if isinstance(body, dict) else {"error": str(body)}
        super().__init__(self.body.get("error", f"HTTP {status}"))

    @property
    def code(self):
        return self.body.get("code")


def state_dir(project_dir):
    return os.path.join(project_dir, ".cadswarm")


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, value):
    """Write JSON so a reader never sees half a file. The temp name is unique per call: two processes
    writing the same file (a runner restart racing the previous one) used to share `<path>.tmp`, and the
    loser died on a Windows PermissionError while the winner's `os.replace` met an open temp file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02)  # a reader (or a virus scanner) has it open for a moment
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def find_project_dir(start):
    """Walk up from `start` to the folder holding .cadswarm/config.json."""
    d = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(d, ".cadswarm", "config.json")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
