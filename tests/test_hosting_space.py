"""The guide starts cadswarm-host (and a local SpacetimeDB) instead of the old Python hub."""
import hashlib
import json
import os
import zipfile
from types import SimpleNamespace

import pytest

from cadswarm import hosting, ticket


def ok(argv, **kw):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_find_tool_prefers_the_env_var(tmp_path, monkeypatch):
    tool = tmp_path / "cadswarm-host.exe"
    tool.write_text("")
    monkeypatch.setenv("CADSWARM_HOST_BIN", str(tool))
    monkeypatch.setattr(hosting, "app_dir", lambda: str(tmp_path / "app"))
    monkeypatch.setattr(hosting.shutil, "which", lambda name: "elsewhere/" + name)
    assert hosting.find_tool("cadswarm-host", "CADSWARM_HOST_BIN") == str(tool)


def test_find_tool_falls_back_to_the_install_folders(tmp_path, monkeypatch):
    local = tmp_path / "local"
    (local / "SpacetimeDB").mkdir(parents=True)
    exe = local / "SpacetimeDB" / ("spacetime.exe" if hosting.os.name == "nt" else "spacetime")
    exe.write_text("")
    monkeypatch.delenv("SPACETIME_BIN", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(hosting, "app_dir", lambda: str(tmp_path / "app"))
    monkeypatch.setattr(hosting.shutil, "which", lambda name: None)
    monkeypatch.setattr(hosting, "ROOT", str(tmp_path / "checkout"))
    assert hosting.find_tool("spacetime", "SPACETIME_BIN") == str(exe)


def test_platform_key_names_the_release_assets(monkeypatch):
    monkeypatch.setattr(hosting.sys, "platform", "win32")
    assert hosting.platform_key() == "windows-x86_64"
    monkeypatch.setattr(hosting.sys, "platform", "darwin")
    monkeypatch.setattr(hosting.platform, "machine", lambda: "arm64")
    assert hosting.platform_key() == "macos-aarch64"
    monkeypatch.setattr(hosting.sys, "platform", "linux")
    monkeypatch.setattr(hosting.platform, "machine", lambda: "x86_64")
    assert hosting.platform_key() == "linux-x86_64"


def test_ensure_tools_does_nothing_when_there_is_nothing_of_ours_to_update(tmp_path, monkeypatch):
    monkeypatch.setattr(hosting, "app_dir", lambda: str(tmp_path / "app"))
    monkeypatch.setattr(hosting, "host_bin", lambda: "host")
    monkeypatch.setattr(hosting, "spacetime_bin", lambda: "spacetime")
    monkeypatch.setattr(hosting, "module_wasm", lambda: "module")
    monkeypatch.setattr(hosting, "_release_sums", lambda: pytest.fail("must not check the release"))
    monkeypatch.setattr(hosting, "_download", lambda *a, **k: pytest.fail("must not download"))
    assert hosting.ensure_tools() == {"host": "host", "spacetime": "spacetime", "wasm": "module"}


def test_the_module_shipped_with_the_workbench_wins_over_a_downloaded_copy(tmp_path, monkeypatch):
    root, app = tmp_path / "addon", tmp_path / "app"
    (root / "module").mkdir(parents=True)
    shipped = root / "module" / "cadswarm_module.wasm"
    shipped.write_bytes(b"shipped")
    (app / "module").mkdir(parents=True)
    (app / "module" / "cadswarm_module.wasm").write_bytes(b"downloaded")
    monkeypatch.delenv("CADSWARM_MODULE_WASM", raising=False)
    monkeypatch.setattr(hosting, "ROOT", str(root))
    monkeypatch.setattr(hosting, "app_dir", lambda: str(app))
    assert hosting.module_wasm() == str(shipped)

    built = root / "module" / "target" / "wasm32-unknown-unknown" / "release" / "cadswarm_module.wasm"
    built.parent.mkdir(parents=True)
    built.write_bytes(b"built")
    assert hosting.module_wasm() == str(built)  # a local build still wins


def test_a_shipped_module_is_never_replaced_by_a_download(tmp_path, monkeypatch):
    root, app = tmp_path / "addon", tmp_path / "app"
    (root / "module").mkdir(parents=True)
    shipped = root / "module" / "cadswarm_module.wasm"
    shipped.write_bytes(b"shipped")
    (app / "module").mkdir(parents=True)
    (app / "module" / "cadswarm_module.wasm").write_bytes(b"older download")
    monkeypatch.delenv("CADSWARM_MODULE_WASM", raising=False)
    monkeypatch.setattr(hosting, "ROOT", str(root))
    monkeypatch.setattr(hosting, "app_dir", lambda: str(app))
    monkeypatch.setattr(hosting, "host_bin", lambda: "host")
    monkeypatch.setattr(hosting, "spacetime_bin", lambda: "spacetime")
    monkeypatch.setattr(hosting, "_release_sums", lambda: pytest.fail("must not check the release"))
    monkeypatch.setattr(hosting, "_download", lambda *a, **k: pytest.fail("must not download"))
    assert hosting.ensure_tools()["wasm"] == str(shipped)


def test_a_new_release_refreshes_the_downloaded_team_server(tmp_path, monkeypatch):
    app = tmp_path / "app"
    exe = app / "bin" / ("cadswarm-host.exe" if os.name == "nt" else "cadswarm-host")
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"old")
    asset = "cadswarm-host-windows-x86_64.exe"
    new = b"new"
    monkeypatch.setattr(hosting, "app_dir", lambda: str(app))
    monkeypatch.setattr(hosting, "host_bin", lambda: str(exe))
    monkeypatch.setattr(hosting, "spacetime_bin", lambda: "spacetime")
    monkeypatch.setattr(hosting, "module_wasm", lambda: "module")
    monkeypatch.setattr(hosting, "platform_key", lambda: "windows-x86_64")
    monkeypatch.setattr(hosting, "_release_sums", lambda: {asset: hashlib.sha256(new).hexdigest()})
    calls = []

    def download(url, dest, expect=None, on_line=None):
        calls.append((url, dest, expect))
        with open(dest, "wb") as f:
            f.write(new)
        return dest

    monkeypatch.setattr(hosting, "_download", download)
    lines = []
    info = hosting.ensure_tools(lines.append)
    assert calls == [(hosting.release_base() + "/" + asset, str(exe), hashlib.sha256(new).hexdigest())]
    assert exe.read_bytes() == new and info["host"] == str(exe)
    assert any("Updating the team server" in line for line in lines)


def test_a_new_release_refreshes_the_downloaded_module(tmp_path, monkeypatch):
    root, app = tmp_path / "addon", tmp_path / "app"
    wasm = app / "module" / "cadswarm_module.wasm"
    wasm.parent.mkdir(parents=True)
    wasm.write_bytes(b"old")
    new = b"new"
    monkeypatch.delenv("CADSWARM_MODULE_WASM", raising=False)
    monkeypatch.setattr(hosting, "ROOT", str(root))
    monkeypatch.setattr(hosting, "app_dir", lambda: str(app))
    monkeypatch.setattr(hosting, "host_bin", lambda: "host")
    monkeypatch.setattr(hosting, "spacetime_bin", lambda: "spacetime")
    monkeypatch.setattr(hosting, "platform_key", lambda: "windows-x86_64")
    monkeypatch.setattr(hosting, "_release_sums",
                        lambda: {hosting.MODULE_ASSET: hashlib.sha256(new).hexdigest()})
    calls = []

    def download(url, dest, expect=None, on_line=None):
        calls.append((url, dest, expect))
        with open(dest, "wb") as f:
            f.write(new)
        return dest

    monkeypatch.setattr(hosting, "_download", download)
    lines = []
    info = hosting.ensure_tools(lines.append)
    assert calls == [(hosting.release_base() + "/" + hosting.MODULE_ASSET, str(wasm),
                      hashlib.sha256(new).hexdigest())]
    assert wasm.read_bytes() == new and info["wasm"] == str(wasm)
    assert any("Updating the team's database module" in line for line in lines)


def test_a_moved_spacetime_version_refreshes_the_managed_cli(tmp_path, monkeypatch):
    app = tmp_path / "app"
    cli = app / "bin" / ("spacetime.exe" if os.name == "nt" else "spacetime")
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"old")
    (app / "bin" / "release.json").write_text(json.dumps({"spacetime": "0.0.1"}), encoding="utf-8")
    monkeypatch.setattr(hosting, "app_dir", lambda: str(app))
    monkeypatch.setattr(hosting, "host_bin", lambda: "host")
    monkeypatch.setattr(hosting, "spacetime_bin", lambda: str(cli))
    monkeypatch.setattr(hosting, "module_wasm", lambda: "module")
    installed = []
    monkeypatch.setattr(hosting, "_install_spacetime_release", lambda log: installed.append(True))
    hosting.ensure_tools()
    assert installed == [True]


def test_an_offline_start_uses_the_tools_already_here(tmp_path, monkeypatch):
    app = tmp_path / "app"
    exe = app / "bin" / ("cadswarm-host.exe" if os.name == "nt" else "cadswarm-host")
    wasm = app / "module" / "cadswarm_module.wasm"
    exe.parent.mkdir(parents=True)
    wasm.parent.mkdir(parents=True)
    exe.write_bytes(b"host")
    wasm.write_bytes(b"module")
    monkeypatch.delenv("CADSWARM_MODULE_WASM", raising=False)
    monkeypatch.setattr(hosting, "ROOT", str(tmp_path / "addon"))
    monkeypatch.setattr(hosting, "app_dir", lambda: str(app))
    monkeypatch.setattr(hosting, "host_bin", lambda: str(exe))
    monkeypatch.setattr(hosting, "spacetime_bin", lambda: "spacetime")

    def offline():
        raise RuntimeError("Couldn't check the CADSwarm downloads. Check your internet connection and try again.")

    monkeypatch.setattr(hosting, "_release_sums", offline)
    monkeypatch.setattr(hosting, "_download", lambda *a, **k: pytest.fail("must not download"))
    lines = []
    info = hosting.ensure_tools(lines.append)
    assert info == {"host": str(exe), "spacetime": "spacetime", "wasm": str(wasm)}
    assert any("Using the programs already here" in line for line in lines)


def test_an_offline_start_still_explains_a_missing_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(hosting, "app_dir", lambda: str(tmp_path / "app"))
    monkeypatch.setattr(hosting, "host_bin", lambda: None)
    monkeypatch.setattr(hosting, "spacetime_bin", lambda: "spacetime")
    monkeypatch.setattr(hosting, "module_wasm", lambda: "module")

    def offline():
        raise RuntimeError("Check your internet connection and try again.")

    monkeypatch.setattr(hosting, "_release_sums", offline)
    with pytest.raises(RuntimeError, match="internet"):
        hosting.ensure_tools()


def test_ensure_tools_downloads_the_team_server_into_the_app_folder(tmp_path, monkeypatch):
    app = tmp_path / "app"
    state = {"host": None}
    monkeypatch.setattr(hosting, "app_dir", lambda: str(app))
    monkeypatch.setattr(hosting, "host_bin", lambda: state["host"])
    monkeypatch.setattr(hosting, "spacetime_bin", lambda: "spacetime")
    monkeypatch.setattr(hosting, "module_wasm", lambda: "module")
    monkeypatch.setattr(hosting, "platform_key", lambda: "windows-x86_64")
    monkeypatch.setattr(hosting, "_release_sums", lambda: {"cadswarm-host-windows-x86_64.exe": "ab" * 32})
    calls = []

    def download(url, dest, expect=None, on_line=None):
        calls.append((url, dest, expect))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        open(dest, "wb").write(b"host")
        state["host"] = dest
        return dest

    monkeypatch.setattr(hosting, "_download", download)
    lines = []
    info = hosting.ensure_tools(lines.append)
    expected = app / "bin" / ("cadswarm-host.exe" if os.name == "nt" else "cadswarm-host")
    assert calls == [(hosting.release_base() + "/cadswarm-host-windows-x86_64.exe", str(expected), "ab" * 32)]
    assert info["host"] == str(expected)
    assert any("team server" in line for line in lines)


def test_ensure_tools_installs_the_database_program_from_its_archive(tmp_path, monkeypatch):
    app = tmp_path / "app"
    monkeypatch.setattr(hosting, "app_dir", lambda: str(app))
    monkeypatch.setattr(hosting, "host_bin", lambda: "host")
    monkeypatch.setattr(hosting, "spacetime_bin", lambda: None)
    monkeypatch.setattr(hosting, "module_wasm", lambda: "module")
    monkeypatch.setattr(hosting, "platform_key", lambda: "windows-x86_64")

    def download(url, dest, expect=None, on_line=None):
        assert url == hosting.SPACETIME_BASE + "/spacetime-x86_64-pc-windows-msvc.zip"
        assert expect == hosting.SPACETIME_ASSETS["windows-x86_64"][1]
        with zipfile.ZipFile(dest, "w") as z:
            z.writestr("spacetime-x86_64-pc-windows-msvc/spacetimedb-cli.exe", b"cli")
            z.writestr("spacetime-x86_64-pc-windows-msvc/spacetimedb-standalone.exe", b"server")
        return dest

    monkeypatch.setattr(hosting, "_download", download)
    hosting.ensure_tools()
    assert (app / "bin" / ("spacetime.exe" if os.name == "nt" else "spacetime")).read_bytes() == b"cli"
    assert (app / "bin" / ("spacetimedb-standalone.exe" if os.name == "nt" else "spacetimedb-standalone")
            ).read_bytes() == b"server"


def test_a_download_with_a_bad_checksum_is_thrown_away(tmp_path):
    src = tmp_path / "asset.bin"
    src.write_bytes(b"hello")
    dest = tmp_path / "downloaded.bin"
    with pytest.raises(RuntimeError, match="damaged"):
        hosting._download(src.as_uri(), str(dest), "00" * 32)
    assert not dest.exists() and not (tmp_path / "downloaded.bin.part").exists()


def test_a_running_program_is_set_aside_so_the_update_still_lands(tmp_path, monkeypatch):
    """Windows won't replace a running host's file; the old copy steps aside and the new one lands."""
    src, folder = tmp_path / "asset.exe", tmp_path / "bin"
    folder.mkdir()
    dest = folder / "cadswarm-host.exe"
    dest.write_bytes(b"old")  # the copy an old team server is running from
    leftover = folder / "cadswarm-host.exe.old-999-1"  # set aside by an earlier update
    leftover.write_bytes(b"older")
    src.write_bytes(b"new")
    real, blocked = os.replace, []

    def replace(a, b):
        if b == str(dest) and not blocked:
            blocked.append(True)
            raise OSError(32, "The process cannot access the file because it is being used by another process")
        real(a, b)

    monkeypatch.setattr(hosting.os, "replace", replace)
    hosting._download(src.as_uri(), str(dest))
    assert dest.read_bytes() == b"new"
    assert sorted(p.name for p in folder.iterdir()) == ["cadswarm-host.exe"]  # no .part, no set-aside


def test_an_unreplaceable_program_names_the_team_still_running_it(tmp_path, monkeypatch):
    src, folder = tmp_path / "asset.exe", tmp_path / "bin"
    folder.mkdir()
    dest = folder / "cadswarm-host.exe"
    dest.write_bytes(b"old")
    src.write_bytes(b"new")
    monkeypatch.setattr(hosting.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError(32, "in use")))
    monkeypatch.setattr(hosting, "_runs_from", lambda path: [4242])
    monkeypatch.setattr(hosting, "_command_line",
                        lambda pid: 'cadswarm-host.exe --data "C:\\Users\\waves\\Documents\\CadSwarm\\Racing Drone" run')
    with pytest.raises(RuntimeError) as e:
        hosting._download(src.as_uri(), str(dest))
    assert "Racing Drone" in str(e.value) and "4242" in str(e.value)
    assert dest.read_bytes() == b"old"  # the running team's program is left alone
    assert not (folder / "cadswarm-host.exe.part").exists()


def test_ensure_tools_refuses_an_asset_without_a_published_checksum(tmp_path, monkeypatch):
    monkeypatch.setattr(hosting, "app_dir", lambda: str(tmp_path / "app"))
    monkeypatch.setattr(hosting, "host_bin", lambda: None)
    monkeypatch.setattr(hosting, "spacetime_bin", lambda: "spacetime")
    monkeypatch.setattr(hosting, "module_wasm", lambda: "module")
    monkeypatch.setattr(hosting, "platform_key", lambda: "windows-x86_64")
    monkeypatch.setattr(hosting, "_release_sums", lambda: {})
    monkeypatch.setattr(hosting, "_download", lambda *a, **k: pytest.fail("must not download"))
    with pytest.raises(RuntimeError, match="checksum"):
        hosting.ensure_tools()


def test_ensure_tools_installs_everything_from_a_release_folder(tmp_path, monkeypatch):
    """The CI output (host exe, module wasm, SpacetimeDB archive, SHA256SUMS) installs end to end."""
    app, release = tmp_path / "app", tmp_path / "release"
    release.mkdir()
    host_name, wasm_name = "cadswarm-host-windows-x86_64.exe", "cadswarm_module.wasm"
    zip_name = "spacetime-fake.zip"
    (release / host_name).write_bytes(b"host")
    (release / wasm_name).write_bytes(b"wasm")
    with zipfile.ZipFile(release / zip_name, "w") as z:
        z.writestr("spacetime-x86_64-pc-windows-msvc/spacetimedb-cli.exe", b"cli")
        z.writestr("spacetime-x86_64-pc-windows-msvc/spacetimedb-standalone.exe", b"server")

    def sha(name):
        return hashlib.sha256((release / name).read_bytes()).hexdigest()

    (release / "SHA256SUMS.txt").write_text(
        f"{sha(host_name)}  {host_name}\n{sha(wasm_name)}  {wasm_name}\n", encoding="ascii")
    monkeypatch.setattr(hosting, "app_dir", lambda: str(app))
    monkeypatch.setattr(hosting, "ROOT", str(tmp_path / "checkout"))
    monkeypatch.setattr(hosting, "platform_key", lambda: "windows-x86_64")
    monkeypatch.setattr(hosting, "release_base", lambda: release.as_uri())
    monkeypatch.setattr(hosting, "SPACETIME_BASE", release.as_uri())
    monkeypatch.setattr(hosting, "SPACETIME_ASSETS", {"windows-x86_64": (zip_name, sha(zip_name))})
    monkeypatch.setattr(hosting.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # not this machine's own ~/.cargo or ~/.local tools
    monkeypatch.setattr(hosting.os.path, "isfile", lambda p, _f=os.path.isfile: _f(p) and not p.startswith("/usr/local/bin"))
    monkeypatch.delenv("CADSWARM_HOST_BIN", raising=False)
    monkeypatch.delenv("SPACETIME_BIN", raising=False)
    monkeypatch.delenv("CADSWARM_MODULE_WASM", raising=False)

    info = hosting.ensure_tools()
    exe = ".exe" if os.name == "nt" else ""
    assert info["host"] == str(app / "bin" / ("cadswarm-host" + exe))
    assert (app / "bin" / ("spacetime" + exe)).read_bytes() == b"cli"
    assert (app / "bin" / ("spacetimedb-standalone" + exe)).read_bytes() == b"server"
    assert info["wasm"] == str(app / "module" / wasm_name)
    assert (app / "module" / wasm_name).read_bytes() == b"wasm"



def test_start_team_says_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(hosting, "ensure_tools", lambda on_line=None: None)
    monkeypatch.setattr(hosting, "find_tool", lambda name, env: None)
    with pytest.raises(RuntimeError, match="cadswarm-host"):
        hosting.start_team("python", str(tmp_path / "p"), "maincloud", "cadswarm-x-0001")


def test_publish_uses_the_built_wasm_and_never_prompts(tmp_path, monkeypatch):
    wasm = tmp_path / "cadswarm_module.wasm"
    wasm.write_text("x")
    seen = []
    monkeypatch.setattr(hosting, "spacetime_bin", lambda: "spacetime")
    monkeypatch.setattr(hosting, "module_wasm", lambda: str(wasm))
    monkeypatch.setattr(hosting.subprocess, "run", lambda argv, **kw: (seen.append(argv), ok(argv))[1])
    hosting.publish("cadswarm-phone-1a2b", "maincloud")
    argv = seen[0]
    assert argv[:4] == ["spacetime", "publish", "--server", "maincloud"]
    assert "--yes=all" in argv and "--delete-data=never" in argv and "--bin-path" in argv and str(wasm) in argv
    assert argv[-1] == "cadswarm-phone-1a2b"


def test_publish_explains_a_missing_login(monkeypatch):
    monkeypatch.setattr(hosting, "spacetime_bin", lambda: "spacetime")
    monkeypatch.setattr(hosting, "module_wasm", lambda: None)
    monkeypatch.setattr(hosting.subprocess, "run",
                        lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr="not logged in"))
    with pytest.raises(RuntimeError, match="spacetime login"):
        hosting.publish("cadswarm-x-0001", "maincloud")


def test_start_team_maincloud_starts_only_the_host(tmp_path, monkeypatch):
    data = tmp_path / "proj"
    spawns, runs, published = [], [], []

    def spawn(argv, lay, log_name, env=None):
        spawns.append(argv)
        return 111

    monkeypatch.setattr(hosting, "find_tool", lambda name, env: name)
    monkeypatch.setattr(hosting, "module_wasm", lambda: "module")
    monkeypatch.setattr(hosting, "publish", lambda db, mode, data_dir=None: published.append((db, mode)))
    monkeypatch.setattr(hosting, "_spawn", spawn)
    monkeypatch.setattr(hosting.subprocess, "run", lambda argv, **kw: (runs.append(argv), ok(argv))[1])

    info = hosting.start_team("python.exe", str(data), "maincloud", "cadswarm-phone-1a2b")
    init = [a for a in runs if "init" in a]
    assert len(init) == 1 and init[0][:3] == ["cadswarm-host", "--data", str(data)]
    assert "maincloud" in init[0] and "cadswarm-phone-1a2b" in init[0]
    assert "python.exe" in init[0] and "--agent" in init[0]
    assert [a[3] for a in spawns] == ["run"]  # never `spacetime start` in maincloud mode
    assert published == [("cadswarm-phone-1a2b", "maincloud")]
    assert info["host_pid"] == 111 and info["spacetime_pid"] is None
    assert info["uri"] == hosting.MAINCLOUD_URI and info["db"] == "cadswarm-phone-1a2b"
    assert (data / "README.txt").exists() and (data / "files").is_dir()

    (data / "host.json").write_text(json.dumps({"mode": "maincloud", "db": "cadswarm-phone-1a2b"}))
    runs.clear()
    hosting.start_team("python.exe", str(data), "maincloud", "cadswarm-phone-1a2b")
    assert not [a for a in runs if "init" in a]  # an existing team is never re-initialized


def test_start_team_local_starts_spacetime_only_when_nothing_listens(tmp_path, monkeypatch):
    data = tmp_path / "proj"
    spawns = []
    monkeypatch.setattr(hosting, "find_tool", lambda name, env: name)
    monkeypatch.setattr(hosting, "module_wasm", lambda: "module")
    monkeypatch.setattr(hosting, "publish", lambda db, mode, data_dir=None: None)
    monkeypatch.setattr(hosting, "port_open", lambda port, host="127.0.0.1", timeout=0.5: False)
    monkeypatch.setattr(hosting, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(hosting.subprocess, "run", lambda argv, **kw: ok(argv))
    monkeypatch.setattr(hosting, "_spawn",
                        lambda argv, lay, log_name, env=None: (spawns.append(argv) or (7 if argv[0] == "spacetime" else 8)))

    info = hosting.start_team("python.exe", str(data), "local", "cadswarm-x-0001")
    assert spawns[0][:4] == ["spacetime", "start", "--listen-addr", "0.0.0.0:3000"]
    assert spawns[1][3] == "run"
    assert info["spacetime_pid"] == 7 and info["host_pid"] == 8 and info["uri"] == hosting.LOCAL_URI

    spawns.clear()
    monkeypatch.setattr(hosting, "port_open", lambda port, host="127.0.0.1", timeout=0.5: True)
    monkeypatch.setattr(hosting, "pid_on_port", lambda port: None)  # no server to inspect on this machine
    info = hosting.start_team("python.exe", str(data), "local", "cadswarm-x-0001")
    assert [a[3] for a in spawns] == ["run"] and info["spacetime_pid"] is None


def test_local_reachable_probes_the_address_the_invite_carries(monkeypatch):
    seen = []
    monkeypatch.setattr(hosting, "lan_ip", lambda: "192.168.1.42")
    monkeypatch.setattr(hosting, "port_open",
                        lambda port, host="127.0.0.1", timeout=0.5: (seen.append(host), host == "192.168.1.42")[1])
    assert hosting.local_reachable(3000) is True
    assert seen == ["192.168.1.42"]


def test_loopback_only_pid_reads_the_command_line(monkeypatch):
    monkeypatch.setattr(hosting, "pid_on_port", lambda port: 4242)
    monkeypatch.setattr(hosting, "_command_line", lambda pid: "... --listen-addr 127.0.0.1:3000\n")
    assert hosting._loopback_only_db_pid() == 4242
    monkeypatch.setattr(hosting, "_command_line", lambda pid: "... --listen-addr 0.0.0.0:3000\n")
    assert hosting._loopback_only_db_pid() is None  # bound everywhere already
    monkeypatch.setattr(hosting, "pid_on_port", lambda port: None)
    assert hosting._loopback_only_db_pid() is None  # not a SpacetimeDB we can place


def test_ensure_local_reachable_keeps_a_network_database(monkeypatch):
    # Already reachable (or not a local team): nothing is stopped.
    monkeypatch.setattr(hosting, "port_open", lambda port, host="127.0.0.1", timeout=0.5: True)
    monkeypatch.setattr(hosting, "pid_on_port", lambda port: None)
    monkeypatch.setattr(hosting, "_kill", lambda pid: pytest.fail("must not stop a reachable database"))
    assert hosting.ensure_local_reachable("local", "proj") is None
    assert hosting.ensure_local_reachable("maincloud", "proj") is None


def test_db_data_dir_takes_the_last_data_dir():
    # `spacetime start` passes its default first; the folder the person asked for comes last and wins.
    cmd = ('spacetimedb-standalone.exe start --data-dir C:\\Users\\a\\AppData\\Local\\SpacetimeDB\\data '
           '--jwt-key-dir C:\\x --listen-addr 127.0.0.1:3000 --data-dir C:\\scratch\\stdb-data\n')
    assert hosting._db_data_dir(cmd) == "C:\\scratch\\stdb-data"
    assert hosting._db_data_dir('spacetime start --data-dir "C:\\My Data\\stdb"') == "C:\\My Data\\stdb"
    assert hosting._db_data_dir("spacetime start --listen-addr 127.0.0.1:3000") == ""


def test_ensure_local_reachable_restarts_on_every_interface_with_the_same_data(tmp_path, monkeypatch):
    """Reconnecting to a running team rebinds its database itself: nothing else would start a new one,
    and the new one must open the old one's data folder or the team's database would be gone."""
    listening, spawns, killed = {"up": True}, [], []
    monkeypatch.setattr(hosting, "find_tool", lambda name, env: name)
    monkeypatch.setattr(hosting, "port_open", lambda port, host="127.0.0.1", timeout=0.5: listening["up"])
    monkeypatch.setattr(hosting, "pid_on_port", lambda port: 4242)
    monkeypatch.setattr(hosting, "_command_line",
                        lambda pid: "spacetime start --listen-addr 127.0.0.1:3000 --data-dir D:\\stdb\n")
    monkeypatch.setattr(hosting, "is_our_process", lambda pid, names=(): pid == 4242)
    monkeypatch.setattr(hosting, "pid_alive", lambda pid: False)
    monkeypatch.setattr(hosting, "_kill", lambda pid: (killed.append(pid), listening.update(up=False)))
    monkeypatch.setattr(hosting, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(hosting, "_spawn",
                        lambda argv, lay, log_name, env=None: (spawns.append(argv), listening.update(up=True), 9)[2])
    assert hosting.ensure_local_reachable("local", str(tmp_path / "proj")) == 9
    assert killed == [4242]
    assert spawns == [["spacetime", "start", "--listen-addr", "0.0.0.0:3000", "--data-dir", "D:\\stdb"]]


def test_start_team_replaces_a_loopback_only_local_database(tmp_path, monkeypatch):
    """A database bound to 127.0.0.1 reaches nobody else, so the invite's network address would point at
    nothing. The old server is restarted on every interface before the team uses the port."""
    data = tmp_path / "proj"
    spawns, killed = [], []
    listening = {"up": True}

    def port_open(port, host="127.0.0.1", timeout=0.5):
        return listening["up"]

    def kill(pid):
        killed.append(pid)
        listening["up"] = False  # the port frees up for the new server

    monkeypatch.setattr(hosting, "find_tool", lambda name, env: name)
    monkeypatch.setattr(hosting, "module_wasm", lambda: "module")
    monkeypatch.setattr(hosting, "publish", lambda db, mode, data_dir=None: None)
    monkeypatch.setattr(hosting, "port_open", port_open)
    monkeypatch.setattr(hosting, "pid_on_port", lambda port: 4242)
    monkeypatch.setattr(hosting, "_command_line", lambda pid: "... --listen-addr 127.0.0.1:3000\n")
    monkeypatch.setattr(hosting, "is_our_process", lambda pid, names=(): pid == 4242)
    monkeypatch.setattr(hosting, "pid_alive", lambda pid: False)
    monkeypatch.setattr(hosting, "_kill", kill)
    monkeypatch.setattr(hosting, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(hosting.subprocess, "run", lambda argv, **kw: ok(argv))
    def spawn(argv, lay, log_name, env=None):
        spawns.append(argv)
        if argv[0] == "spacetime":
            listening["up"] = True
            return 7
        return 8

    monkeypatch.setattr(hosting, "_spawn", spawn)

    info = hosting.start_team("python.exe", str(data), "local", "cadswarm-x-0001")
    assert killed == [4242]  # the loopback-only server was stopped
    assert [a[0] for a in spawns].count("spacetime") == 1  # replaced once, not started twice
    assert spawns[0][:4] == ["spacetime", "start", "--listen-addr", "0.0.0.0:3000"]
    # It still serves every team the old one did, so this team doesn't take it over to stop.
    assert info["spacetime_pid"] is None and info["host_pid"] == 8


def test_team_ready_needs_a_ready_status_and_a_live_pid(tmp_path, monkeypatch):
    data = tmp_path / "proj"
    internal = data / ".cadswarm-team"
    internal.mkdir(parents=True)
    assert hosting.team_ready(str(data)) is False
    (internal / "host-status.json").write_text(json.dumps({"ready": False, "pid": 999}))
    monkeypatch.setattr(hosting, "pid_alive", lambda pid: True)
    assert hosting.team_ready(str(data)) is False
    (internal / "host-status.json").write_text(json.dumps({"ready": True, "pid": 999}))
    assert hosting.team_ready(str(data)) is True
    monkeypatch.setattr(hosting, "pid_alive", lambda pid: False)
    assert hosting.team_ready(str(data)) is False


def test_wait_ready_reports_a_server_that_died(tmp_path, monkeypatch):
    monkeypatch.setattr(hosting, "team_ready", lambda data_dir: False)
    monkeypatch.setattr(hosting, "pid_alive", lambda pid: False)
    with pytest.raises(RuntimeError, match="stopped while starting"):
        hosting.wait_ready(str(tmp_path), timeout=5, pids=[123])


def test_stop_team_only_stops_our_programs(monkeypatch):
    killed = []
    monkeypatch.setattr(hosting, "is_our_process",
                        lambda pid, names=hosting.HOST_PROCESS_NAMES: str(pid) in ("5", "6"))
    monkeypatch.setattr(hosting, "_kill", lambda pid: killed.append(pid))
    assert hosting.stop_team({"host_pid": 5, "spacetime_pid": 6}) is True
    assert killed == [5, 6]
    assert hosting.stop_team({"host_pid": "nonsense", "spacetime_pid": 9}) is False


def test_mint_invite_reads_the_ticket_line(tmp_path, monkeypatch):
    t = {"v": 1, "uri": "http://127.0.0.1:3000", "db": "cadswarm", "token": "ab" * 32,
         "kind": "human", "role": "lead", "host_addr": ""}
    out = SimpleNamespace(returncode=0, stdout="ticket: " + ticket.encode(t) + "\nphone:  http://x/#t=y\n",
                          stderr="")
    seen = []
    monkeypatch.setattr(hosting, "find_tool", lambda name, env: "cadswarm-host")
    monkeypatch.setattr(hosting.subprocess, "run", lambda argv, **kw: (seen.append(argv), out)[1])
    assert hosting.mint_invite(str(tmp_path), "human", "lead") == t
    assert seen[0][:2] == ["cadswarm-host", "--data"] and seen[0][3:] == ["invite", "--kind", "human",
                                                                          "--role", "lead"]


def test_mint_invite_explains_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(hosting, "find_tool", lambda name, env: "cadswarm-host")
    monkeypatch.setattr(hosting.subprocess, "run",
                        lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr="no host.json"))
    with pytest.raises(RuntimeError, match="Couldn't make an invite code"):
        hosting.mint_invite(str(tmp_path))


def test_the_host_is_pointed_at_this_checkouts_board_library(monkeypatch):
    monkeypatch.delenv("CADSWARM_BOARDS", raising=False)
    assert hosting.host_env()["CADSWARM_BOARDS"] == os.path.join(hosting.ROOT, "boards", "boards.json")
    monkeypatch.setenv("CADSWARM_BOARDS", "/mine.json")  # an explicit library wins
    assert hosting.host_env()["CADSWARM_BOARDS"] == "/mine.json"


def test_an_existing_team_is_republished_as_its_own_host_identity(tmp_path, monkeypatch):
    """Only the database's owner may update it; the CLI's sign-in may be someone else by now."""
    wasm = tmp_path / "m.wasm"
    wasm.write_bytes(b"\0asm")
    (tmp_path / "host.json").write_text(json.dumps({"db": "cadswarm-x-1", "token": "owner-token",
                                                    "uri": "http://127.0.0.1:3000"}))
    monkeypatch.setattr(hosting, "module_wasm", lambda: str(wasm))
    monkeypatch.setattr(hosting.subprocess, "run", lambda *a, **k: pytest.fail("must not use the CLI's sign-in"))
    sent = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"
    monkeypatch.setattr(hosting.urllib.request, "urlopen",
                        lambda req, timeout=None: sent.append((req.get_method(), req.full_url,
                                                               req.get_header("Authorization"))) or Reply())
    hosting.publish("cadswarm-x-1", "local", str(tmp_path))
    assert sent == [("PUT", "http://127.0.0.1:3000/v1/database/cadswarm-x-1", "Bearer owner-token")]


def test_invite_uri_hands_out_the_network_address_for_a_local_team(monkeypatch):
    # A local team listens on 127.0.0.1, which only this computer can reach: the invite must carry the
    # network address instead, or teammates on the same network cannot join.
    monkeypatch.setattr(hosting, "lan_ip", lambda: "192.168.1.42")
    assert hosting.invite_uri("http://127.0.0.1:3000") == "http://192.168.1.42:3000"
    assert hosting.invite_uri("http://localhost:3000") == "http://192.168.1.42:3000"


def test_invite_uri_leaves_reachable_addresses_alone(monkeypatch):
    monkeypatch.setattr(hosting, "lan_ip", lambda: "192.168.1.42")
    assert hosting.invite_uri("https://maincloud.spacetimedb.com") == "https://maincloud.spacetimedb.com"
    # A teammate who joined a local team already has the network address: hand that on as-is.
    assert hosting.invite_uri("http://192.168.1.42:3000") == "http://192.168.1.42:3000"


def test_invite_uri_falls_back_to_loopback_when_offline(monkeypatch):
    monkeypatch.setattr(hosting, "lan_ip", lambda: "127.0.0.1")
    assert hosting.invite_uri("http://127.0.0.1:3000") == "http://127.0.0.1:3000"


def test_server_label_names_where_the_database_lives():
    assert hosting.server_label("https://maincloud.spacetimedb.com") == "SpacetimeDB Maincloud"
    assert hosting.server_label("http://127.0.0.1:3000") == "this computer"
    assert hosting.server_label("http://192.168.1.42:3000") == "this computer · 192.168.1.42"


def test_read_maincloud_login_reads_the_account_id(monkeypatch):
    monkeypatch.setattr(hosting, "spacetime_bin", lambda: "spacetime")
    monkeypatch.setattr(hosting, "host_env", lambda: {})
    monkeypatch.setattr(hosting.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="You are logged in as "
                                                        "c200641aabd1f7b96c5d79c30f05636a702117b", stderr=""))
    assert hosting._read_maincloud_login() == "c200641aabd1"


def test_read_maincloud_login_says_nothing_when_signed_out(monkeypatch):
    monkeypatch.setattr(hosting, "spacetime_bin", lambda: "spacetime")
    monkeypatch.setattr(hosting, "host_env", lambda: {})
    monkeypatch.setattr(hosting.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="You are not logged in"))
    assert hosting._read_maincloud_login() == ""
