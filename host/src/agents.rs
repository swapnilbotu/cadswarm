//! Headless agents the host spawns for a brief, using the agent CLI the host owner brings. Each gets its own folder and
//! cadswarm identity (via `cadswarm setup --invite`), so its actions are attributed to it.

use crate::config::HostConfig;
use anyhow::{anyhow, bail, Context, Result};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

pub const AGENT_TIMEOUT: Duration = Duration::from_secs(15 * 60);

pub struct Agent {
    pub name: String,
    child: Child,
    started: Instant,
}

/// Where `exe` might be: $CADSWARM_{EXE}, then PATH, then the usual install folders, then (for
/// `claude` only) the binary bundled with the VS Code extension (newest version first).
pub fn cli_candidates(exe: &str, env: Option<String>, path_var: &str, home: &Path, appdata: &Path) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = env.into_iter().map(PathBuf::from).collect();
    let names: Vec<String> = if cfg!(windows) { vec![format!("{exe}.exe"), format!("{exe}.cmd")] } else { vec![exe.to_string()] };
    for dir in std::env::split_paths(path_var) {
        out.extend(names.iter().map(|n| dir.join(n)));
    }
    out.extend(names.iter().map(|n| home.join(".local").join("bin").join(n)));
    if !cfg!(windows) {
        for dir in [home.join(".claude").join("local"), home.join(".npm-global").join("bin"),
                    home.join(".volta").join("bin"), home.join(".bun").join("bin"),
                    PathBuf::from("/opt/homebrew/bin"), PathBuf::from("/usr/local/bin"), PathBuf::from("/usr/bin")] {
            out.extend(names.iter().map(|n| dir.join(n)));
        }
    }
    if cfg!(windows) && !appdata.as_os_str().is_empty() {
        out.push(appdata.join("npm").join(format!("{exe}.cmd")));
    }
    if exe == "opencode" {
        out.extend(names.iter().map(|n| home.join(".opencode").join("bin").join(n)));
        // The desktop app bundles a CLI under a different name, outside PATH.
        if let Some(local) = appdata.parent() {
            out.push(local.join("Local").join("Programs").join("@opencodedesktop").join("resources").join("opencode-cli.exe"));
        }
    }
    if exe == "claude" {
        let ext = home.join(".vscode").join("extensions");
        let mut bundled: Vec<PathBuf> = std::fs::read_dir(&ext)
            .map(|rd| {
                rd.flatten()
                    .map(|e| e.path())
                    .filter(|p| p.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.starts_with("anthropic.claude-code-")))
                    .collect()
            })
            .unwrap_or_default();
        // Version-aware order: compare the dotted numbers, not the strings ("2.1.306" > "2.1.31").
        bundled.sort_by_key(|p| {
            let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("").to_string();
            name.trim_start_matches("anthropic.claude-code-")
                .split(['.', '-'])
                .map(|part| part.parse::<u64>().unwrap_or(0))
                .collect::<Vec<u64>>()
        });
        for dir in bundled.into_iter().rev() {
            out.extend(names.iter().map(|n| dir.join("resources").join("native-binary").join(n)));
        }
    }
    out
}

/// A file we can start: on Unix it must also have an execute bit (a stray non-executable copy
/// would otherwise win and fail with "permission denied").
fn is_runnable(p: &Path) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        p.metadata().map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0).unwrap_or(false)
    }
    #[cfg(not(unix))]
    {
        p.is_file()
    }
}

pub fn find_cli(exe: &str) -> Result<PathBuf> {
    let home = std::env::var_os("USERPROFILE").or_else(|| std::env::var_os("HOME")).map(PathBuf::from).unwrap_or_default();
    let appdata = std::env::var_os("APPDATA").map(PathBuf::from).unwrap_or_default();
    let path_var = std::env::var("PATH").unwrap_or_default();
    let env = std::env::var(format!("CADSWARM_{}", exe.to_uppercase())).ok();
    cli_candidates(exe, env, &path_var, &home, &appdata)
        .into_iter()
        .find(|p| is_runnable(p))
        .ok_or_else(|| anyhow!(format!("{exe} isn't installed (set CADSWARM_{} to its path)", exe.to_uppercase())))
}

pub fn agent_command(client: &str, override_cmd: &[String], dir: &Path, prompt: &str) -> Result<(String, Vec<String>)> {
    if let Some((program, rest)) = override_cmd.split_first() {
        let args = rest.iter().map(|a| if a == "{prompt}" { prompt.to_string() } else { a.clone() }).collect();
        return Ok((program.clone(), args));
    }
    let p = prompt.to_string();
    Ok(match client {
        // Headless runs don't prompt to trust the folder's .mcp.json, so name it explicitly.
        "claude" => ("claude".into(), vec!["-p".into(), p, "--max-turns".into(), "80".into(), "--allowedTools".into(),
                     "mcp__cadswarm".into(), "--mcp-config".into(), dir.join(".mcp.json").display().to_string()]),
        // Codex dropped --full-auto (it exits 2 with "unexpected argument"); -s workspace-write is the
        // sandbox that alias meant, and every version from 0.20 on takes it.
        "codex" => ("codex".into(), vec!["exec".into(), "--sandbox".into(), "workspace-write".into(),
                                          "--skip-git-repo-check".into(), p]),
        // --standalone: a private server per run, not whatever the person's desktop app is running.
        // The prompt goes in on stdin (see spawn): an npm .cmd shim would split a multi-line argument.
        "opencode" => ("opencode".into(), vec!["run".into(), "--standalone".into(), "--auto".into()]),
        other => bail!("unknown agent client {other:?}; use claude, codex or opencode, or set agent_command"),
    })
}

/// Find the CLI this host would run, before an invite is minted for an agent that cannot start.
pub fn preflight(cfg: &HostConfig) -> Result<()> {
    if !cfg.agent_command.is_empty() {
        if cfg.agent_command[0].trim().is_empty() {
            bail!("agent_command starts with an empty program name");
        }
        return Ok(());
    }
    let (program, _) = agent_command(&cfg.agent_client, &[], Path::new("."), "")?;
    find_cli(&program).map(|_| ())
}

/// The end of an agent's own log, for a failure message a person can act on.
pub fn log_tail(cfg: &HostConfig, name: &str, lines: usize) -> String {
    let path = cfg.data_dir.join("agents").join(name).join("agent.log");
    let Ok(text) = std::fs::read_to_string(&path) else { return String::new() };
    let tail: Vec<String> = text.lines().filter(|l| !l.trim().is_empty())
        .rev().take(lines).collect::<Vec<_>>().into_iter().rev()
        .map(|l| l.trim().chars().take(300).collect()).collect();
    if tail.is_empty() {
        String::new()
    } else {
        format!("\n\nIts log ended with:\n{}", tail.join("\n"))
    }
}

/// Where one start of an agent keeps its identity, config and log.
pub fn run_dir(cfg: &HostConfig, name: &str) -> PathBuf {
    cfg.data_dir.join("agents").join(name)
}

/// The agent folders on disk, so a host restart can find the leftovers of a brief it no longer holds in
/// memory (its runtimes are gone, the folders and member rows are not).
pub fn run_dirs(cfg: &HostConfig) -> Vec<String> {
    let Ok(rd) = std::fs::read_dir(cfg.data_dir.join("agents")) else { return Vec::new() };
    rd.flatten().filter(|e| e.path().is_dir())
        .filter_map(|e| e.file_name().into_string().ok())
        .collect()
}

/// Delete one finished agent's folder (its log, its sign-in). The host writes it again on the next start.
pub fn remove_run_dir(cfg: &HostConfig, name: &str) {
    let dir = run_dir(cfg, name);
    if let Err(e) = std::fs::remove_dir_all(&dir) {
        if e.kind() != std::io::ErrorKind::NotFound {
            eprintln!("[host] could not remove {}: {e}", dir.display());
        }
    }
}

/// Make the next start of this agent a fresh member. Its old identity was taken off the team, and
/// `rejoin` refuses a removed identity; the invite the next start gets is single-use, so setup must
/// actually redeem it. The log and config stay, for the failure message and the pinned model.
pub fn forget_identity(cfg: &HostConfig, name: &str) {
    let path = run_dir(cfg, name).join(".cadswarm").join("identity.json");
    if let Err(e) = std::fs::remove_file(&path) {
        if e.kind() != std::io::ErrorKind::NotFound {
            eprintln!("[host] could not clear {}: {e}", path.display());
        }
    }
}

impl Agent {
    /// `ticket` is an agent invite minted by the host.
    pub fn spawn(cfg: &HostConfig, name: &str, ticket: &str, prompt: &str) -> Result<Agent> {
        let dir: PathBuf = cfg.data_dir.join("agents").join(name);
        std::fs::create_dir_all(&dir)?;
        // PYTHONSAFEPATH, not -I: -I would also drop PYTHONPATH, which is how a guide-hosted team
        // (FreeCAD's own Python) finds the package; both keep a checkout in the cwd from being imported.
        let run_setup = |ephemeral: bool| {
            let mut args = vec!["-m", "cadswarm.cli", "setup"];
            if ephemeral {
                args.push("--ephemeral");
            }
            args.extend(["--client", cfg.agent_client.as_str(), "--invite", ticket, "--name", name, "--dir"]);
            Command::new(&cfg.cadswarm_python)
                .env("PYTHONSAFEPATH", "1")
                .args(&args)
                .arg(&dir)
                .output()
                .context("running cadswarm setup")
        };
        let mut setup = run_setup(true)?;
        // A CadSwarm package older than --ephemeral rejects the flag before redeeming the invite, so the
        // ticket is still unused: join without it (the agent then stays listed after its job).
        if !setup.status.success() && String::from_utf8_lossy(&setup.stderr).contains("unrecognized arguments: --ephemeral") {
            setup = run_setup(false)?;
        }
        if !setup.status.success() {
            bail!(
                "cadswarm setup for {name} failed: {}{}",
                String::from_utf8_lossy(&setup.stderr),
                String::from_utf8_lossy(&setup.stdout)
            );
        }
        let log = std::fs::File::create(dir.join("agent.log"))?;
        let (program, args) = agent_command(&cfg.agent_client, &cfg.agent_command, &dir, prompt)?;
        let exe = if cfg.agent_command.is_empty() { find_cli(&program)? } else { PathBuf::from(&program) };
        let mut cmd = Command::new(exe);
        cmd.args(&args)
            .current_dir(&dir)
            .stdin(if cfg.agent_client == "opencode" && cfg.agent_command.is_empty() {
                Stdio::piped()
            } else {
                Stdio::null()
            })
            .stdout(log.try_clone()?)
            .stderr(log);
        let mut child = cmd.spawn().with_context(|| format!("starting {program} for {name}"))?;
        if let Some(mut stdin) = child.stdin.take() {
            stdin.write_all(prompt.as_bytes()).context("sending the prompt to the agent")?;
        }
        Ok(Agent { name: name.into(), child, started: Instant::now() })
    }

    /// False once it exited or was killed for running past the timeout.
    pub fn alive(&mut self) -> bool {
        if self.started.elapsed() > AGENT_TIMEOUT {
            self.kill();
            return false;
        }
        matches!(self.child.try_wait(), Ok(None))
    }

    /// Stop it and everything it started. On Windows the MCP server is a grandchild process: killing
    /// only the CLI would leave it holding the helper's seat in the team.
    pub fn kill(&mut self) {
        if cfg!(windows) {
            let _ = Command::new("taskkill")
                .args(["/PID", &self.child.id().to_string(), "/T", "/F"])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

pub fn planner_prompt(brief_id: &str, text: &str, board: &str, params: &[(String, String)]) -> String {
    let list: Vec<String> = params.iter().map(|(k, v)| format!("{k} = {v}")).collect();
    format!(
        "You are the PLANNER for CadSwarm brief {brief_id}.\n\
         The person asked: \"{text}\"\n\
         Board: {board}. The shared Params sheet already holds its measured dimensions:\n{params}\n\n\
         Plan the design as separate printable parts that different agents can build in parallel. \
         For an enclosure that is usually: Base (floor plus walls, with a cutout for each port), \
         Standoffs (one cylinder per mounting hole at hole_N_x/hole_N_y, height standoff_h, bore hole_d), \
         and Lid (top plate with a lip inside the walls, clearance gap). \
         For each part call create_task with a short title, objects = the exact FreeCAD object names it will create, \
         brief = \"{brief_id}\", and a detail that states the geometry in terms of Params names \
         (e.g. inner length = pcb_l + 2*gap). Add any extra Params you need with set_param first (lock Params, then release it). \
         Gaps use the team's fits, which are Params too (tol_clearance, tol_slip_fit, tol_press_fit). \
         For screws, inserts, bearings, magnets or any bought part, call recommend_components and name the chosen part \
         (id, or standard family and size) and the hole it needs (hole_size) in the task detail. \
         Finally call set_brief_plan with brief_id \"{brief_id}\" and plan_json = a JSON object \
         {{\"parts\": [{{\"name\": ..., \"task\": ..., \"summary\": ...}}]}}. \
         Do NOT build any geometry yourself. Stop after set_brief_plan.",
        params = list.join("\n")
    )
}

pub fn worker_prompt(brief_id: &str, n: usize) -> String {
    format!(
        "You are WORKER {n} building parts for CadSwarm brief {brief_id}.\n\
         Loop: call list_tasks; pick an OPEN task whose brief is \"{brief_id}\"; claim_task it; \
         lock_objects its object names; build exactly what its detail says, binding every dimension \
         to Params with expressions (e.g. \"Params.pcb_l + 2 * Params.gap\"); place the parts the detail names \
         with add_standard_part or insert_component instead of modelling them; run check_interference \
         and fix clashes with your own parts; report_result done with evidence (object names, volumes). \
         Read get_inbox between tasks and follow any steer messages. \
         When list_tasks shows no OPEN task for brief \"{brief_id}\", stop."
    )
}

/// The reviewer is an ordinary spawned agent with a reviewer identity: the module lets it read and report
/// but refuses every write op from a `reviewer` member, so it cannot change the model it judges.
pub fn review_prompt(brief_id: &str, text: &str, board: &str, bed: [f64; 3], round: u32, style: &str) -> String {
    let style_check = if style.trim().is_empty() {
        String::new()
    } else {
        format!("\n5. Team design style: the rules below are how this team designs. Each part that breaks one is a \
                 problem; write its `problem` as \"style: <the rule> (<what the part does instead>)\".\n{style}\n")
    };
    format!(
        "You are the design REVIEWER for CadSwarm brief {brief_id} (review round {round}).\n\
         The person asked: \"{text}\". Board: {board}. Printer bed: {} x {} x {} mm.\n\
         Check, using the tools only (you cannot change geometry):\n\
         0. Your arrow shows the team which part you are on: call point_at(\"<part>\") before you check it,\n\
            and point_at again when you move to the next one. It only moves your pointer, never the model.\n\
         1. model_status: read the Params sheet (the board's measured dimensions) and the parts.\n\
         2. check_interference on all parts: any clash between separate parts is a problem.\n\
         3. Fit: the enclosure's inner size must exceed pcb_l/pcb_w by at least 2*gap; standoffs must sit at\n\
            hole_N_x/hole_N_y; each port cutout must be centred on its <port>_pos and <port>_z.\n\
         4. check_printability: every part must fit the bed, be a closed solid, and have no wall under 1.2 mm.\n\
         {style_check}\
         For each problem: create_task assigned to the part's owner (see locked_by in model_status) with a\n\
         precise fix, brief set to \"{brief_id}\", and send_message to that owner.\n\
         Finish by calling report_review(brief_id=\"{brief_id}\", round={round}, passed=<true only if no problems>,\n\
         findings=[{{\"object\", \"problem\", \"task_id\"}}]).",
        bed[0], bed[1], bed[2]
    )
}

/// The first line is `accept_handoff` on purpose: it hands over the locks and tasks atomically,
/// so the new agent owns the work before it reads anything else.
pub fn handoff_prompt(handoff_id: &str, context_json: &str) -> String {
    let ctx: serde_json::Value = serde_json::from_str(context_json).unwrap_or_default();
    let from = ctx["from"].as_str().unwrap_or("a teammate");
    let note = ctx["note"].as_str().filter(|n| !n.is_empty()).unwrap_or("(no note: infer it from their recent work)");
    format!(
        "You are picking up exactly where {from} left off in the shared CadSwarm FreeCAD model.\n\
         FIRST call accept_handoff(handoff_id=\"{handoff_id}\"): it gives you their locked parts and tasks.\n\
         Their note, which is your main instruction: {note}\n\
         Where they left off (JSON: what they were pointing at, the parts they hold, their in-progress tasks, \
         their last activity, ops and messages):\n{context_json}\n\n\
         Continue their work, do not start over: read model_status and get_object for the parts they hold, use their \
         recent ops to see what they were in the middle of, finish it binding dimensions to Params, run \
         check_interference, report_result on each of their tasks, and send_message to {from} with a short summary \
         of what you did. Stop when their tasks are done."
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_env_override_comes_first_and_the_newest_vscode_binary_before_older_ones() {
        let dir = std::env::temp_dir().join(format!("cadswarm-claude-test-{}", std::process::id()));
        let ext = dir.join(".vscode").join("extensions");
        std::fs::create_dir_all(ext.join("anthropic.claude-code-2.1.31-win32-x64")).unwrap();
        std::fs::create_dir_all(ext.join("anthropic.claude-code-2.1.306-win32-x64")).unwrap();
        let c = cli_candidates("claude", Some("X:/my/claude.exe".into()), "", &dir, Path::new("A:/appdata"));
        assert_eq!(c[0], PathBuf::from("X:/my/claude.exe"));
        let bundled: Vec<String> =
            c.iter().map(|p| p.display().to_string()).filter(|s| s.contains("native-binary")).collect();
        assert!(bundled[0].contains("2.1.306"), "newest extension first: {bundled:?}");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn each_client_gets_its_headless_command() {
        let dir = Path::new("D:/agents/w1");
        let (p, a) = agent_command("claude", &[], dir, "build it").unwrap();
        assert_eq!(p, "claude");
        assert!(a.contains(&"build it".to_string()) && a.contains(&"--mcp-config".to_string()));
        let (p, a) = agent_command("codex", &[], dir, "build it").unwrap();
        assert_eq!((p.as_str(), a[0].as_str(), a.last().unwrap().as_str()), ("codex", "exec", "build it"));
        // --full-auto was removed from the CLI: a fresh Codex answers it with exit 2, "unexpected argument".
        assert!(!a.contains(&"--full-auto".to_string()), "the removed flag would kill every agent: {a:?}");
        assert_eq!(&a[1..3], ["--sandbox", "workspace-write"], "{a:?}");
        let (p, a) = agent_command("opencode", &[], dir, "build it").unwrap();
        assert_eq!((p.as_str(), a[0].as_str(), a[1].as_str()), ("opencode", "run", "--standalone"));
        assert!(!a.contains(&"build it".to_string()), "the prompt goes in on stdin: {a:?}");
        assert!(agent_command("gpt", &[], dir, "x").is_err());
    }

    fn cfg(client: &str, command: Vec<String>, data: PathBuf) -> HostConfig {
        HostConfig {
            mode: "local".into(), uri: "http://127.0.0.1:3000".into(), db: "db".into(), token: "t".into(),
            data_dir: data, freecad_python: PathBuf::from("freecad"), worker_script: PathBuf::from("w.py"),
            web_url: String::new(), cadswarm_python: PathBuf::from("python"), agent_client: client.into(),
            agent_command: command,
        }
    }

    #[test]
    fn preflight_checks_the_command_before_a_start() {
        let dir = std::env::temp_dir().join(format!("cadswarm-preflight-{}", std::process::id()));
        // A custom command line is taken as given (a person's own wrapper script).
        assert!(preflight(&cfg("claude", vec!["my-wrapper".into(), "{prompt}".into()], dir.clone())).is_ok());
        // An empty program name or an unknown client can never start, so no invite should be spent.
        assert!(preflight(&cfg("claude", vec!["  ".into()], dir.clone())).is_err());
        assert!(preflight(&cfg("gpt", Vec::new(), dir)).is_err());
    }

    #[test]
    fn an_agents_log_tail_names_why_it_died() {
        let dir = std::env::temp_dir().join(format!("cadswarm-logtail-{}", std::process::id()));
        let agent = dir.join("agents").join("planner-1");
        std::fs::create_dir_all(&agent).unwrap();
        std::fs::write(agent.join("agent.log"), "first\n\nstarting\nworkspace has not been trusted\n").unwrap();
        let tail = log_tail(&cfg("claude", Vec::new(), dir.clone()), "planner-1", 2);
        assert!(tail.contains("trusted") && !tail.contains("first"), "{tail}");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn a_finished_agents_folder_is_deleted_and_the_others_stay() {
        // Every start of a one-job agent left a folder behind; a finished brief must not keep them.
        let dir = std::env::temp_dir().join(format!("cadswarm-runs-{}", std::process::id()));
        let c = cfg("opencode", Vec::new(), dir.clone());
        for name in ["worker1-f413f129", "worker2-f413f129"] {
            std::fs::create_dir_all(run_dir(&c, name)).unwrap();
            std::fs::write(run_dir(&c, name).join("agent.log"), "built the lid").unwrap();
        }
        assert_eq!(run_dirs(&c).len(), 2);
        remove_run_dir(&c, "worker1-f413f129");
        // Only that start is gone: the other keeps its log, and removing twice is not an error.
        assert_eq!(run_dirs(&c), vec!["worker2-f413f129".to_string()]);
        remove_run_dir(&c, "worker1-f413f129");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn a_retired_agent_forgets_its_sign_in_but_keeps_its_log() {
        // The next start must redeem its fresh invite: `rejoin` refuses an identity that was taken off
        // the team, and the invite is single-use. The log stays for the brief's failure message.
        let dir = std::env::temp_dir().join(format!("cadswarm-forget-{}", std::process::id()));
        let c = cfg("opencode", Vec::new(), dir.clone());
        let state = run_dir(&c, "planner-abc").join(".cadswarm");
        std::fs::create_dir_all(&state).unwrap();
        std::fs::write(state.join("identity.json"), "{\"token\": \"t\"}").unwrap();
        std::fs::write(run_dir(&c, "planner-abc").join("agent.log"), "workspace has not been trusted").unwrap();
        forget_identity(&c, "planner-abc");
        assert!(!state.join("identity.json").exists());
        assert!(run_dir(&c, "planner-abc").join("agent.log").exists());
        forget_identity(&c, "planner-abc");  // already gone: not an error
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn the_reviewer_prompt_checks_fit_and_reports_a_verdict() {
        let p = review_prompt("b_x", "a case for my board", "esp32", [220.0, 220.0, 250.0], 2, "");
        assert!(p.contains("check_interference") && p.contains("check_printability"));
        assert!(p.contains("report_review") && p.contains("round=2") && p.contains("esp32"));
        assert!(!p.contains("Team design style"), "no style rules learned yet");
        let with_style = review_prompt("b_x", "a case", "esp32", [220.0, 220.0, 250.0], 1, "- walls are 2.4 mm");
        assert!(with_style.contains("walls are 2.4 mm"));
    }

    #[test]
    fn an_override_runs_any_cli_with_the_prompt_substituted() {
        let cmd = vec!["aider".to_string(), "--message".to_string(), "{prompt}".to_string(), "--yes".to_string()];
        let (p, a) = agent_command("claude", &cmd, Path::new("D:/x"), "make a lid").unwrap();
        assert_eq!(p, "aider");
        assert_eq!(a, vec!["--message", "make a lid", "--yes"]);
    }

    #[test]
    fn only_claude_looks_in_the_vscode_extension() {
        let c = cli_candidates("codex", None, "", Path::new("H:/home"), Path::new("A:/appdata"));
        assert!(c.iter().all(|p| !p.display().to_string().contains("native-binary")));
        assert!(c.iter().any(|p| p.display().to_string().contains("codex")));
    }

    #[test]
    fn opencode_also_looks_in_its_own_bin_and_the_desktop_app() {
        let c = cli_candidates("opencode", None, "", Path::new("H:/home"), Path::new("A:/AppData/Roaming"));
        let paths: Vec<String> = c.iter().map(|p| p.display().to_string().replace('\\', "/")).collect();
        assert!(paths.iter().any(|p| p.contains(".opencode") && p.contains("bin/opencode")));
        assert!(paths.iter().any(|p| p.contains("@opencodedesktop") && p.ends_with("opencode-cli.exe")));
    }

    #[test]
    fn the_handoff_prompt_leads_with_accepting_and_the_persons_note() {
        let ctx = r#"{"from":"carol","note":"make the lip 1 mm thinner","pointer":{"object":"Lid"},"locks":["Lid"],"tasks":[]}"#;
        let p = handoff_prompt("h_1", ctx);
        assert!(p.find("accept_handoff").unwrap() < p.find("make the lip 1 mm thinner").unwrap());
        assert!(p.contains("carol") && p.contains("Lid") && p.contains("h_1"));
    }
}
