"""Hosting a team from a normal person's computer: no terminal, no Python install.

The FreeCAD setup guide uses these to (1) download the agent packages once into a private
folder with FreeCAD's own pip, (2) download the team server, database program and module once
into the app folder, (3) create the team's SpacetimeDB database and start `cadswarm-host` as a
quiet background process that survives closing FreeCAD, and (4) check on it. Stdlib only.
"""
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from urllib.parse import urlsplit

from .paths import DEPENDENCIES, LAUNCHER, ROOT, app_dir, deps_dir, project_layout

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOCAL_URI = "http://127.0.0.1:3000"
LOCAL_PORT = 3000
MAINCLOUD_URI = "https://maincloud.spacetimedb.com"
# Where a phone opens the live build. The host's own setting wins when it points somewhere a phone can reach.
DEFAULT_WEB_URL = "https://cadswarm.design/app"
# Only ever stopped or reported on when the command line names one of these (never a blanket kill).
HOST_PROCESS_NAMES = ("cadswarm-host", "spacetime")

# Download-on-first-use. `CADSWARM_RELEASE_BASE` points at a folder or server during development.
RELEASE_BASE = "https://github.com/SachchitBalamurugan/cadswarm/releases/latest/download"
RELEASE_ASSETS = {
    "windows-x86_64": "cadswarm-host-windows-x86_64.exe",
    "linux-x86_64": "cadswarm-host-linux-x86_64",
    "linux-aarch64": "cadswarm-host-linux-aarch64",
    "macos-x86_64": "cadswarm-host-macos-x86_64",
    "macos-aarch64": "cadswarm-host-macos-aarch64",
}
MODULE_ASSET = "cadswarm_module.wasm"
SUMS_ASSET = "SHA256SUMS.txt"

# Pinned with the module and host (overview Global Constraints). Hash-verified from the release.
SPACETIME_VERSION = "2.13.0"
SPACETIME_BASE = f"https://github.com/clockworklabs/SpacetimeDB/releases/download/v{SPACETIME_VERSION}"
SPACETIME_ASSETS = {
    "windows-x86_64": ("spacetime-x86_64-pc-windows-msvc.zip",
                       "94e26e3e8782e88e2cb676920a7df2faa482724f83253c399cb9e59a5c04b4d3"),
    "linux-x86_64": ("spacetime-x86_64-unknown-linux-gnu.tar.gz",
                     "034fe43ebb42e8d291c6ba18b6b3a6ac9a3bb4aa548d37743340362cb021165e"),
    "linux-aarch64": ("spacetime-aarch64-unknown-linux-gnu.tar.gz",
                      "47e4ad61f3d5830cf75a0b5b2432f5d9c115ab4e37505f757d7555f3ad1fe8f0"),
    "macos-x86_64": ("spacetime-x86_64-apple-darwin.tar.gz",
                     "12268695592b1cd6a7384cc677dad2815a9504ed135a660df75abf7e81c9e6ab"),
    "macos-aarch64": ("spacetime-aarch64-apple-darwin.tar.gz",
                      "54bb70971b88821563a1839c0d908edb94ece169e0f3bedc8ffdf8e18083c316"),
}


PROJECT_README = """This folder is a CADSwarm team project.

  model.FCStd   the shared 3D model everyone's AI helpers are building (open a copy in FreeCAD any time)
  files/        notes, plans and scripts the team saved; subfolders appear as the project grows
  exports/      3D files (STEP/STL) saved with "Save as 3D file"; never overwritten
  snapshots/    saved versions of the model
  AI helpers/   workspace folders for the AI helpers on this computer (they hold sign-ins: don't share)
  .cadswarm-team/  the team server's own data and older versions of files (the last 20 of each). Don't edit it.

Please don't edit model.FCStd directly while the team is working: your changes would be lost
the next time an AI helper changes the model. Copy it first, or work through CADSwarm.
"""


def deps_ready(python):
    try:
        r = subprocess.run([python, LAUNCHER, "check"], capture_output=True, encoding="utf-8",
                           errors="replace", timeout=120, creationflags=NO_WINDOW)
        return r.returncode == 0 and r.stdout.strip().endswith("ok")
    except (OSError, subprocess.TimeoutExpired):
        return False


def install_deps(python, on_line=None):
    """pip-install the agent packages into deps_dir() with FreeCAD's own Python. Raises on failure."""
    target = deps_dir()
    os.makedirs(target, exist_ok=True)
    cmd = [python, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--upgrade",
           "--target", target, *DEPENDENCIES]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    tail = []
    for line in proc.stdout:
        line = line.rstrip()
        tail = (tail + [line])[-15:]
        if on_line and line:
            on_line(line)
    if proc.wait() != 0:
        text = "\n".join(tail)
        hint = ("It looks like this computer is offline." if "connection" in text.lower() or "resolve" in text.lower()
                else "The download didn't finish.")
        raise RuntimeError(f"{hint} Check your internet connection and try again.\n\nDetails:\n{text}")
    if not deps_ready(python):
        raise RuntimeError("The helper software downloaded but won't start. Try again; if it keeps failing, "
                           f"delete the folder {target} and retry.")


def local_ips():
    """This computer's IPv4 addresses a teammate might reach, the default-route one (`lan_ip()`) first.

    Skips loopback and link-local (169.254.x.x) addresses.
    """
    found = [lan_ip()]
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    out = []
    for ip in found:
        if ip not in out and not ip.startswith(("127.", "169.254.")):
            out.append(ip)
    return out or [lan_ip()]


def lan_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))  # UDP connect sends nothing; it just picks the outbound interface
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "")


def invite_uri(uri):
    """The address an invite should carry. A team hosted on this computer listens on `127.0.0.1`, which
    only this computer can reach: hand out this computer's network address instead, so teammates on the
    same network can join. Maincloud's public address (and a teammate's already-network address) pass
    through unchanged."""
    try:
        host = urlsplit(uri or "").hostname or ""
    except ValueError:
        return uri
    if host in LOOPBACK_HOSTS:
        return f"http://{lan_ip()}:{LOCAL_PORT}"
    return uri


def server_label(uri):
    """A short phrase for where a team's database lives, read from its address alone (cheap, no process).
    Kept in step with the web panel's `serverLabel`."""
    host = urlsplit(uri or "").hostname or ""
    if host.endswith("spacetimedb.com"):
        return "SpacetimeDB Maincloud"
    if host in LOOPBACK_HOSTS:
        return "this computer"
    return f"this computer · {host}"


# This computer's Maincloud account: `spacetime login show` is a process, so remember what it said.
_LOGIN = {"account": "", "at": 0.0}


def cached_maincloud_login():
    """The last account we read for this computer, without running the CLI (the panel asks often)."""
    return _LOGIN["account"]


def maincloud_login(force=False, ttl=300):
    """A short id for the Maincloud account this computer is signed in as, or "" when it isn't.
    Reads `spacetime login show`, cached for `ttl`; `force` re-reads now (the sign-in button polls)."""
    now = time.time()
    if not force and _LOGIN["at"] and now - _LOGIN["at"] < ttl:
        return _LOGIN["account"]
    _LOGIN["at"] = now
    _LOGIN["account"] = _read_maincloud_login()
    return _LOGIN["account"]


def _read_maincloud_login():
    st = spacetime_bin()
    if not st:
        return ""
    try:
        r = subprocess.run([st, "login", "show"], capture_output=True, encoding="utf-8", errors="replace",
                           timeout=10, creationflags=NO_WINDOW, env=host_env())
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"logged in as\s+([0-9a-fA-F]{8,})", (r.stdout or "") + (r.stderr or ""))
    return m.group(1)[:12] if m else ""


def spacetime_login():
    """Open the browser and sign this computer in to SpacetimeDB Maincloud (`spacetime login`). The CLI
    opens the browser and waits for the person to finish there, so run it in the background; poll
    `maincloud_login(force=True)` for the result."""
    st = spacetime_bin()
    if not st:
        raise RuntimeError("CADSwarm needs the SpacetimeDB program to sign in to Maincloud, and this "
                           "computer doesn't have it yet. Start a team on this computer first.")
    os.makedirs(tools_dir(), exist_ok=True)
    log = open(os.path.join(tools_dir(), "spacetime-login.log"), "a", encoding="utf-8")
    kwargs = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "env": host_env()}
    if os.name == "nt":
        kwargs["creationflags"] = NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([st, "login"], **kwargs)


def free_port(start=8765, tries=50):
    for port in range(start, start + tries):
        # On Windows a plain wildcard bind succeeds even while something listens on 127.0.0.1:port,
        # so ask for exclusive use there (a connect probe is unreliable on loopback).
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if os.name == "nt":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError("Couldn't find a free network port on this computer.")


def port_open(port, host="127.0.0.1", timeout=0.5):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, int(port))) == 0


def local_reachable(port=LOCAL_PORT, timeout=0.4):
    """True when the local database also answers on this computer's network address, so a teammate on the
    same network can reach the team. A server bound to 127.0.0.1 answers only on loopback, so an invite
    carrying the network address would point at nothing a teammate can reach.

    Probes the same address the invite carries (`lan_ip`), not every interface: that is the one that has
    to answer."""
    return port_open(port, host=lan_ip(), timeout=timeout)


# ------------------------------------------------------------------- programs

def platform_key():
    """The OS/CPU name used in release assets, e.g. "windows-x86_64"."""
    if sys.platform.startswith("win"):
        return "windows-x86_64"  # SpacetimeDB ships x86_64; ARM Windows runs it under emulation
    machine = (platform.machine() or "").lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(machine)
    if not arch:
        raise RuntimeError(f"CADSwarm doesn't have a build for this computer ({machine or 'unknown CPU'}).")
    return ("macos-" if sys.platform == "darwin" else "linux-") + arch


def tools_dir():
    """Where downloaded programs live: the private app folder, never a project or FreeCAD itself."""
    return os.path.join(app_dir(), "bin")


def release_base():
    return (os.environ.get("CADSWARM_RELEASE_BASE") or RELEASE_BASE).rstrip("/")


def find_tool(name, env):
    """Absolute path of `name`, or None: the `env` variable, the app folder, PATH, then the usual
    install folders."""
    explicit = (os.environ.get(env) or "").strip() if env else ""
    if explicit and os.path.isfile(explicit):
        return explicit
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA", "")
    dirs = [tools_dir(), os.path.join(local, "SpacetimeDB"), os.path.join(local, "SpacetimeDB", "bin", "current"),
            os.path.join(home, ".cargo", "bin"), os.path.join(ROOT, "host", "target", "release"),
            "/usr/local/bin", os.path.join(home, ".local", "bin")]
    exts = ("", ".exe") if os.name == "nt" else ("",)
    for d in filter(None, dirs):
        for ext in exts:
            cand = os.path.join(d, name + ext)
            if os.path.isfile(cand):
                return cand
    return shutil.which(name)


def host_bin():
    return find_tool("cadswarm-host", "CADSWARM_HOST_BIN")


def spacetime_bin():
    return find_tool("spacetime", "SPACETIME_BIN")


def module_wasm():
    """The module to publish: the environment's, this checkout's build, the one shipped with the
    workbench, else a downloaded copy (which may be from an older release)."""
    explicit = os.environ.get("CADSWARM_MODULE_WASM")
    if explicit and os.path.isfile(explicit):
        return explicit
    for path in (_built_module(), shipped_module()):
        if os.path.isfile(path):
            return path
    downloaded = _managed_module()
    return downloaded if os.path.isfile(downloaded) else None


def _built_module():
    return os.path.join(ROOT, "module", "target", "wasm32-unknown-unknown", "release", MODULE_ASSET)


def shipped_module():
    """The module that ships inside this workbench (the installers copy it beside the code)."""
    return os.path.join(ROOT, "module", MODULE_ASSET)


def _managed_host():
    """The team server this guide downloaded (never one on PATH or in a checkout's build folder)."""
    return os.path.join(tools_dir(), "cadswarm-host" + (".exe" if os.name == "nt" else ""))


def _managed_module():
    """The module this guide downloaded, used only when the workbench doesn't ship one."""
    return os.path.join(app_dir(), "module", MODULE_ASSET)


def _managed_spacetime():
    return os.path.join(tools_dir(), "spacetime" + (".exe" if os.name == "nt" else ""))


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _aside_path(dest):
    return f"{dest}.old-{os.getpid()}-{int(time.time() * 1000)}"


def _set_aside_and_replace(part, dest):
    """Move `part` to `dest` after setting `dest` aside. Windows locks a running program's file
    against replacement but allows renaming it, so an update that arrives while the old host is
    still running lands now and the running copy carries on from the name we moved it to.
    Anything else (a read-only folder, say) leaves `dest` as it was and returns False."""
    if not os.path.isfile(dest):
        return False
    aside = _aside_path(dest)
    try:
        os.replace(dest, aside)
    except OSError:
        return False
    try:
        os.replace(part, dest)
    except OSError:
        try:
            os.replace(aside, dest)  # put the old one back
        except OSError:
            pass
        return False
    _unlink(aside)  # best effort: it can only go once the program using it exits
    return True


def _sweep_aside(dest):
    """Remove copies set aside by an earlier update, once nothing is running from them."""
    folder, prefix = os.path.dirname(dest), os.path.basename(dest) + ".old-"
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if name.startswith(prefix):
            _unlink(os.path.join(folder, name))


def _runs_from(path):
    """PIDs of programs running from `path` (Windows keeps their file open and locked)."""
    if os.name != "nt":
        return []
    name = os.path.basename(path).replace("'", "''")
    exe = path.replace("'", "''")
    ps = (f"Get-CimInstance Win32_Process -Filter \"Name='{name}'\" | "
          f"Where-Object {{ $_.ExecutablePath -eq '{exe}' }} | ForEach-Object {{ $_.ProcessId }}")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=20,
                             creationflags=NO_WINDOW).stdout
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    return [int(t) for t in out.split() if t.isdigit()]


def _in_place_error(dest, err):
    """Plain words for a program we couldn't put in place, naming the team still running it."""
    name = os.path.basename(dest)
    pids = _runs_from(dest)
    if pids:
        m = re.search(r'--data\s+(?:"([^"]+)"|(\S+))', _command_line(pids[0]))
        where = (m.group(1) or m.group(2)) if m else ""
        team = f"the team in “{where}”" if where else "a CADSwarm team"
        return (f"Couldn't put {name} in place: {team} is still running (pid {pids[0]}) and Windows "
                f"won't let a running program be replaced. Stop that team, then click “Try again”."
                f"\n\nDetails:\n{err}")
    return (f"Couldn't put {name} in place. If CADSwarm is running, stop the team and click “Try again”."
            f"\n\nDetails:\n{err}")


def _put_in_place(part, dest):
    """Finish a download by moving it onto `dest`, keeping a running program out of the way."""
    try:
        os.replace(part, dest)
    except OSError as e:
        if not _set_aside_and_replace(part, dest):
            _unlink(part)
            raise RuntimeError(_in_place_error(dest, e))
    if os.name != "nt":
        os.chmod(dest, 0o755)
    _sweep_aside(dest)
    return dest


def _release_sums():
    """The checksums published with the release's CADSwarm assets."""
    url = f"{release_base()}/{SUMS_ASSET}"
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            text = r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError("Couldn't check the CADSwarm downloads. Check your internet connection and try "
                           f"again.\n\nDetails:\n{e}")
    sums = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and len(parts[0]) == 64:
            sums[parts[1].lstrip("*")] = parts[0].lower()
    return sums


def _download(url, dest, expect=None, on_line=None):
    """Download `url` to `dest` atomically, verifying sha256 when `expect` is given."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    digest, got, mark = hashlib.sha256(), 0, 0
    try:
        with urllib.request.urlopen(url, timeout=120) as r, open(part, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                digest.update(chunk)
                got += len(chunk)
                if on_line and got - mark >= 8 * 1024 * 1024:
                    mark = got
                    on_line(f"Downloaded {got // (1024 * 1024)} MB…")
    except (urllib.error.URLError, OSError) as e:
        _unlink(part)
        raise RuntimeError(f"Couldn't download {os.path.basename(dest)}. Check your internet connection and "
                           f"try again.\n\nDetails:\n{e}")
    if expect and digest.hexdigest() != expect.lower():
        _unlink(part)
        raise RuntimeError(f"{os.path.basename(dest)} downloaded but was damaged, so it was thrown away. "
                           "Try again; if it keeps happening, download CADSwarm again.")
    return _put_in_place(part, dest)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stamp_path():
    return os.path.join(tools_dir(), "release.json")


def _read_stamp():
    try:
        with open(_stamp_path(), encoding="utf-8") as f:
            stamp = json.load(f)
        return stamp if isinstance(stamp, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_stamp(**values):
    """Remember what this guide installed. Best effort: a read-only folder never blocks a team."""
    stamp = _read_stamp()
    stamp.update(values)
    try:
        os.makedirs(tools_dir(), exist_ok=True)
        part = _stamp_path() + ".part"
        with open(part, "w", encoding="utf-8") as f:
            json.dump(stamp, f, indent=2, sort_keys=True)
        os.replace(part, _stamp_path())
    except OSError:
        pass


def _spacetime_needs_refresh():
    """True when this guide installed SpacetimeDB and the pinned release has moved on."""
    return (os.path.isfile(_managed_spacetime())
            and _read_stamp().get("spacetime") != SPACETIME_VERSION)


def _install_spacetime_release(log):
    """Download the pinned SpacetimeDB archive and put its programs in the app folder."""
    key = platform_key()
    entry = SPACETIME_ASSETS.get(key)
    if not entry:
        raise RuntimeError(f"CADSwarm doesn't have a database program for this computer ({key}) yet.")
    name, sha = entry
    log("Downloading the database program (about 50 MB, one time)")
    with tempfile.TemporaryDirectory(prefix="cadswarm-") as tmp:
        archive = os.path.join(tmp, name)
        _download(f"{SPACETIME_BASE}/{name}", archive, sha, on_line=log)
        _install_spacetime(archive)
    _write_stamp(spacetime=SPACETIME_VERSION)


def _install_spacetime(archive):
    """Put the CLI and its standalone server from the official archive into the app folder."""
    wanted = {"spacetimedb-cli": "spacetime", "spacetimedb-standalone": "spacetimedb-standalone"}
    suffix = ".exe" if os.name == "nt" else ""
    os.makedirs(tools_dir(), exist_ok=True)
    found = {}

    def take(name, read):
        base = os.path.basename(name)
        stem = base[:-4] if base.lower().endswith(".exe") else base
        if stem not in wanted:
            return
        out = os.path.join(tools_dir(), wanted[stem] + suffix)
        part = out + ".part"
        try:
            with read() as src, open(part, "wb") as dst:
                shutil.copyfileobj(src, dst)
        except OSError:
            _unlink(part)  # never leave half a program behind
            raise
        _put_in_place(part, out)
        found[stem] = out

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for member in z.namelist():
                take(member, lambda m=member: z.open(m))
    else:
        with tarfile.open(archive, "r:*") as t:
            for member in t.getmembers():
                if member.isfile():
                    take(member.name, lambda m=member: t.extractfile(m))
    if "spacetimedb-cli" not in found:
        raise RuntimeError("The downloaded database program is missing its main file. Try again.")
    if os.name != "nt":
        for path in found.values():
            os.chmod(path, 0o755)


def ensure_tools(on_line=None):
    """Make sure the team server, SpacetimeDB and module are here, and up to date.

    Anything missing is downloaded into the private app folder. Copies this guide installed before
    are checked against the release's published checksums and replaced when a newer release changed
    them, so an old team server can never be left talking to a new module. A module built in this
    checkout or shipped with the workbench wins over any download. When the computer is offline and
    every program is already here, the team still starts. Raises RuntimeError in plain words when a
    needed download cannot be finished.
    """
    log = on_line or (lambda _line: None)
    key = platform_key()
    host_path, wasm_path = _managed_host(), _managed_module()
    host_asset = RELEASE_ASSETS.get(key)
    need_host = not host_bin()
    need_wasm = not module_wasm()
    check_host = host_bin() == host_path and os.path.isfile(host_path)
    # A downloaded module only matters when the workbench doesn't ship one (or a dev build wins).
    check_wasm = os.path.isfile(wasm_path) and module_wasm() == wasm_path

    sums = None
    if need_host or need_wasm or check_host or check_wasm:
        try:
            sums = _release_sums()
        except RuntimeError:
            if need_host or need_wasm:
                raise
            log("Couldn't check for a newer release. Using the programs already here.")

    if need_host:
        if not host_asset:
            raise RuntimeError(f"CADSwarm doesn't have a team server for this computer ({key}) yet.")
        if not sums or host_asset not in sums:
            raise RuntimeError(f"The CADSwarm release is missing the checksum for {host_asset}. Try again later.")
        log("Downloading the team server")
        _download(f"{release_base()}/{host_asset}", host_path, sums[host_asset], on_line=log)
        _write_stamp(host=_sha256(host_path))
    elif check_host and sums and host_asset in sums and _sha256(host_path) != sums[host_asset]:
        log("Updating the team server")
        _download(f"{release_base()}/{host_asset}", host_path, sums[host_asset], on_line=log)
        _write_stamp(host=_sha256(host_path))

    if need_wasm:
        if not sums:
            sums = _release_sums()
        if MODULE_ASSET not in sums:
            raise RuntimeError(f"The CADSwarm release is missing the checksum for {MODULE_ASSET}. Try again later.")
        log("Downloading the team's database module")
        _download(f"{release_base()}/{MODULE_ASSET}", wasm_path, sums[MODULE_ASSET], on_line=log)
        _write_stamp(module=_sha256(wasm_path))
    elif check_wasm and sums and MODULE_ASSET in sums and _sha256(wasm_path) != sums[MODULE_ASSET]:
        log("Updating the team's database module")
        _download(f"{release_base()}/{MODULE_ASSET}", wasm_path, sums[MODULE_ASSET], on_line=log)
        _write_stamp(module=_sha256(wasm_path))

    if not spacetime_bin():
        _install_spacetime_release(log)
    elif _spacetime_needs_refresh():
        try:
            _install_spacetime_release(log)
        except RuntimeError:
            log("Couldn't update the database program. Using the version already here.")

    return {"host": host_bin(), "spacetime": spacetime_bin(), "wasm": module_wasm()}



def host_env():
    """What `cadswarm-host` and `spacetime` run with: this checkout on PYTHONPATH (inside FreeCAD the
    package lives in the add-on folder, not site-packages) and the CLI's folder on PATH."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    # A FreeCAD AppImage exports PYTHONHOME for its own Python; passed on, it breaks every other Python the host
    # starts (agent CLIs' tools, a system Python). FreeCAD's bundled Python finds its home without it.
    env.pop("PYTHONHOME", None)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (ROOT, env.get("PYTHONPATH")) if p)
    # A downloaded host lives in the app folder, far from this checkout's board library.
    boards = os.path.join(ROOT, "boards", "boards.json")
    if not env.get("CADSWARM_BOARDS") and os.path.isfile(boards):
        env["CADSWARM_BOARDS"] = boards
    st = spacetime_bin()
    if st:
        env["PATH"] = os.path.dirname(st) + os.pathsep + env.get("PATH", "")
    return env


def host_config(data_dir):
    """The host's own settings in <data_dir>/host.json ({} when this folder has never hosted)."""
    try:
        with open(os.path.join(data_dir, "host.json"), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def set_agent_client(data_dir, agent):
    """Record which AI helper CLI the host should start for briefs.

    The running host re-reads host.json every couple of seconds (host/src/briefs.rs), so a choice made
    after the team was created takes effect without a restart. Best effort: a folder that has never
    hosted has no host.json yet, and `start_team`'s `init` writes the choice then."""
    if not agent or not data_dir:
        return False
    path = os.path.join(data_dir, "host.json")
    data = host_config(data_dir)
    if not data.get("db"):
        return False
    if data.get("agent_client") == agent:
        return True
    data["agent_client"] = agent
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def _details(r):
    text = ((r.stderr or "") + (r.stdout or "")).strip()
    tail = [ln for ln in text.splitlines() if ln.strip()][-12:]
    return ("\n\nDetails:\n" + "\n".join(tail)) if tail else ""


def write_project_readme(root):
    readme = os.path.join(root, "README.txt")
    if not os.path.exists(readme):
        with open(readme, "w", encoding="utf-8") as f:
            f.write(PROJECT_README)


def _publish_as(uri, db, token, wasm):
    """Update `db` over HTTP as the identity `token` (the database owner)."""
    req = urllib.request.Request(f"{uri.rstrip('/')}/v1/database/{db}", data=open(wasm, "rb").read(), method="PUT",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/wasm"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError("Couldn't update your team's database on SpacetimeDB: "
                           + e.read().decode("utf-8", "replace")[:300])
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach SpacetimeDB at {uri}: {e.reason}")


def publish(db, mode="maincloud", data_dir=None):
    """Create or update the team's database with this build's module. Raises RuntimeError in plain words.

    A folder that already hosts this database re-publishes as its own host identity (host.json): only the
    database's owner may update it, and the CLI's sign-in can differ (a new `spacetime login`, or a team the
    host created with a server-issued identity)."""
    cfg = host_config(data_dir) if data_dir else {}
    wasm = module_wasm()
    if cfg.get("db") == db and cfg.get("token") and wasm:
        return _publish_as(cfg.get("uri") or (LOCAL_URI if mode == "local" else MAINCLOUD_URI), db, cfg["token"], wasm)
    st = spacetime_bin()
    if not st:
        raise RuntimeError("CADSwarm needs the SpacetimeDB program to run your team's database, and this "
                           "computer doesn't have it yet. Install it from https://spacetimedb.com/install, "
                           "then click “Try again”.")
    wasm = module_wasm()
    # Re-publishing an existing team (Reconnect) must never touch its data. --delete-data=never
    # makes that explicit, whatever the CLI's default is.
    args = [st, "publish", "--server", "local" if mode == "local" else "maincloud", "--yes=all",
            "--delete-data=never"]
    args += ["--bin-path", wasm] if wasm else ["--module-path", os.path.join(ROOT, "module")]
    args.append(db)
    try:
        r = subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace", timeout=300,
                           creationflags=NO_WINDOW, env=host_env())
    except subprocess.TimeoutExpired:
        raise RuntimeError("Creating your team's database took too long. Check your internet connection and "
                           "try again.")
    if r.returncode != 0:
        raise RuntimeError("Couldn't create your team's database on SpacetimeDB. If this computer hasn't "
                           "signed in there yet, run `spacetime login` once in a terminal." + _details(r))


def _spawn(argv, lay, log_name, env=None):
    """Start `argv` quietly in the background, appending its output to <internal>/<log_name>. Returns its PID."""
    log = open(os.path.join(lay["internal"], log_name), "a", encoding="utf-8")
    kwargs = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
              "env": host_env() if env is None else env, "cwd": lay["root"]}
    if os.name == "nt":
        kwargs["creationflags"] = NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True  # keep running after FreeCAD closes
    return subprocess.Popen(argv, **kwargs).pid


def _wait_port(port, timeout=30, pid=None, data_dir=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port):
            return True
        if pid is not None and not pid_alive(pid):
            raise RuntimeError("The local database server stopped while starting." + log_tail(data_dir))
        time.sleep(0.3)
    raise RuntimeError("The local database server is taking too long to start." + log_tail(data_dir))


def _loopback_only_db_pid():
    """PID of a SpacetimeDB on our port that was told to listen on loopback only (so only this computer
    can reach it), else None.

    Read from the process's command line, not a network probe: a probe also fails behind a firewall on a
    correctly-bound server, and we must never restart a server that is already reachable by teammates."""
    pid = pid_on_port(LOCAL_PORT)
    if not pid:
        return None
    cmd = _command_line(pid).lower()
    if "0.0.0.0" in cmd:  # told to bind every interface: leave it be
        return None
    if "127.0.0.1" in cmd or "localhost" in cmd or "::1" in cmd:
        return pid
    if not cmd and not local_reachable(LOCAL_PORT):  # no readable command line: trust a probe
        return pid
    return None


def _db_data_dir(cmd):
    """The data folder a SpacetimeDB server was started with, or "" for the default. `spacetime start`
    hands the server its own default first, so the last `--data-dir` is the one in use."""
    found = re.findall(r'--data-dir[= ](?:"([^"]+)"|(\S+))', cmd or "")
    return (found[-1][0] or found[-1][1]) if found else ""


def _stop_local_db(pid):
    """Stop a loopback-only local database. Only ever a SpacetimeDB process."""
    if not is_our_process(pid, ("spacetime",)):
        return False
    _kill(pid)
    deadline = time.time() + 20
    while time.time() < deadline:
        if not port_open(LOCAL_PORT) and not pid_alive(pid):
            return True
        time.sleep(0.3)
    if os.name != "nt" and pid_alive(pid):  # the polite stop was ignored: insist, so the new server can bind
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass
        time.sleep(1)
    return True


def ensure_local_reachable(mode, data_dir, log=None):
    """A local team's database must be bound to every interface, or teammates on the network can't join.
    If this computer already has one bound to loopback only (from before that fix, or a manual
    `spacetime start`), restart it now on every interface with the same data folder, so its databases
    come back and every client reconnects. Returns the new server's PID, or None when nothing changed.

    The new server serves whatever teams the old one did, so callers don't adopt it as theirs to stop."""
    if mode != "local" or not port_open(LOCAL_PORT):
        return None
    pid = _loopback_only_db_pid()
    if not pid:
        return None
    st = spacetime_bin()
    if not st:  # no way to start a replacement: leave the working (if unreachable) one alone
        return None
    stdb_data = _db_data_dir(_command_line(pid))
    if log:
        log("Making the team's database reachable from your network")
    if not _stop_local_db(pid):
        return None
    argv = [st, "start", "--listen-addr", f"0.0.0.0:{LOCAL_PORT}"]
    if stdb_data:
        argv += ["--data-dir", stdb_data]
    lay = project_layout(data_dir)
    os.makedirs(lay["internal"], exist_ok=True)
    new = _spawn(argv, lay, "spacetime.log")
    _wait_port(LOCAL_PORT, 30, new, data_dir)
    return new


def start_team(python, data_dir, mode="maincloud", db=None, web_url="", agent="claude", on_log=None):
    """Publish the team's database and start its host in the background.

    Returns {"host_pid", "spacetime_pid", "uri", "db", "mode"}. `db` must already be chosen; `init`
    runs only the first time this folder hosts. Raises RuntimeError with plain-language messages.
    """
    if mode not in ("local", "maincloud"):
        raise ValueError("mode must be 'local' or 'maincloud'")
    if not db:
        raise ValueError("a database name is required")
    log = on_log or (lambda _line: None)
    ensure_tools(log)  # one-time downloads into the app folder; no-ops when already available
    host = host_bin()
    if not host:
        raise RuntimeError("CADSwarm needs its team server program (cadswarm-host), and it isn't available "
                           "on this computer. Check your internet connection and click “Try again”.")
    lay = project_layout(data_dir)
    for key in ("files", "exports", "snapshots", "internal"):
        os.makedirs(lay[key], exist_ok=True)
    write_project_readme(lay["root"])
    env = host_env()
    info = {"host_pid": None, "spacetime_pid": None, "db": db, "mode": mode,
            "uri": LOCAL_URI if mode == "local" else MAINCLOUD_URI}
    try:
        if mode == "local":
            ensure_local_reachable(mode, data_dir, log)  # rebind a loopback-only database before using the port
            if not port_open(LOCAL_PORT):
                st = spacetime_bin()
                if not st:
                    raise RuntimeError("CADSwarm needs the SpacetimeDB program for a local team, and this computer "
                                       "doesn't have it yet. Install it from https://spacetimedb.com/install, then "
                                       "click “Try again”.")
                log(f"Starting the local database on port {LOCAL_PORT}")
                # Bind every interface, not just loopback: a teammate on the same network reaches the team at
                # this computer's network address. The invite carries that address (see `invite_uri`).
                info["spacetime_pid"] = _spawn([st, "start", "--listen-addr", f"0.0.0.0:{LOCAL_PORT}"],
                                               lay, "spacetime.log", env)
                _wait_port(LOCAL_PORT, 30, info["spacetime_pid"], data_dir)
        log(f"Creating your team's database “{db}” ({mode})")
        publish(db, mode, data_dir)
        if not host_config(data_dir):
            log("Setting up the team server (one time)")
            argv = [host, "--data", data_dir, "init", "--mode", mode, "--db", db, "--python", python,
                    "--agent", agent]
            if web_url:
                argv += ["--web-url", web_url]
            try:
                with open(os.path.join(lay["internal"], "host.log"), "a", encoding="utf-8") as out:
                    r = subprocess.run(argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                       env=env, cwd=lay["root"], timeout=300, creationflags=NO_WINDOW)
            except subprocess.TimeoutExpired:
                raise RuntimeError("Setting up the team server took too long." + log_tail(data_dir))
            if r.returncode != 0:
                raise RuntimeError("Couldn't set up the team server." + log_tail(data_dir))
        log("Starting the team server")
        info["host_pid"] = _spawn([host, "--data", data_dir, "run"], lay, "host.log", env)
    except Exception:
        # Never leave a database server we started running on its own (the guide's "Try again"
        # would start a second one). Only processes we started are in `info`.
        stop_team(info)
        raise
    return info


def mint_invite(data_dir, kind="human", role="member"):
    """Ask the host on this computer for a fresh single-use invite. Returns the decoded ticket."""
    host = host_bin()
    if not host:
        raise RuntimeError("CADSwarm's team server program is missing, so it can't make an invite code.")
    lay = project_layout(data_dir)
    try:
        r = subprocess.run([host, "--data", data_dir, "invite", "--kind", kind, "--role", role],
                           capture_output=True, encoding="utf-8", errors="replace", timeout=120,
                           creationflags=NO_WINDOW, env=host_env(), cwd=lay["root"])
    except subprocess.TimeoutExpired:
        raise RuntimeError("Making an invite code took too long. Try again.")
    if r.returncode != 0:
        raise RuntimeError("Couldn't make an invite code." + _details(r))
    from . import ticket
    for line in (r.stdout or "").splitlines():
        if line.startswith("ticket:"):
            return ticket.decode(line.split(":", 1)[1].strip())
    raise RuntimeError("The team server didn't print an invite code." + _details(r))


# ------------------------------------------------------------------- checking

def host_status(data_dir):
    """The status file the running host writes (plan 02 Task 5), or {} when it hasn't."""
    try:
        with open(os.path.join(project_layout(data_dir)["internal"], "host-status.json"), encoding="utf-8") as f:
            status = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    return status if isinstance(status, dict) else {}


def team_ready(data_dir):
    """True once the host wrote host-status.json and its process is still alive."""
    status = host_status(data_dir)
    return status.get("ready") is True and pid_alive(status.get("pid"))


def wait_ready(data_dir, timeout=90, pids=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if team_ready(data_dir):
            return True
        for pid in pids or []:
            if pid and not pid_alive(pid):
                raise RuntimeError("The team server stopped while starting." + log_tail(data_dir))
        time.sleep(0.7)
    raise RuntimeError("The team server is taking too long to start." + log_tail(data_dir))


def log_tail(data_dir, lines=12):
    try:
        with open(os.path.join(project_layout(data_dir)["internal"], "host.log"), encoding="utf-8",
                  errors="replace") as f:
            return "\n\nDetails:\n" + "".join(f.readlines()[-lines:])
    except (OSError, TypeError):
        return ""


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True,
                             text=True, creationflags=NO_WINDOW).stdout
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _command_line(pid):
    """The process's command line, or "" if unknown. PowerShell's CIM query because wmic is gone on new Windows."""
    try:
        if os.name == "nt":
            ps = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps]
        else:
            cmd = ["ps", "-ww", "-p", str(int(pid)), "-o", "command="]  # -ww: piped ps cuts lines at 80 columns
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW).stdout
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ""


def is_our_process(pid, names=HOST_PROCESS_NAMES):
    """True only if `pid` is one of our programs. A saved PID can be reused by an unrelated program
    after a reboot, and we must never kill that."""
    return pid_alive(pid) and any(n in _command_line(pid).lower() for n in names)


def _kill(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    else:
        os.kill(int(pid), signal.SIGTERM)


def stop_team(info):
    """Stop the host and (in local mode) the database server the guide started. Never a blanket kill."""
    stopped = False
    for key, names in (("host_pid", ("cadswarm-host",)), ("spacetime_pid", ("spacetime",))):
        pid = (info or {}).get(key)
        if pid and is_our_process(pid, names):
            _kill(int(pid))
            stopped = True
    return stopped


def pid_on_port(port):
    """PID of the SpacetimeDB server listening on `port`, or None. For a server we didn't just start."""
    try:
        port = int(port)
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=20,
                                 creationflags=NO_WINDOW).stdout
            pids = [int(cols[-1]) for cols in (ln.split() for ln in out.splitlines())
                    if len(cols) >= 5 and cols[3] == "LISTENING" and cols[1].rsplit(":", 1)[-1] == str(port)
                    and cols[-1].isdigit()]
        else:
            out = subprocess.run(["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True,
                                 timeout=20).stdout
            pids = [int(x) for x in out.split() if x.isdigit()]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return next((p for p in pids if is_our_process(p, ("spacetime",))), None)
