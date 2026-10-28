//! cadswarm-host: runs the shared FreeCAD model for a CadSwarm workspace.

mod agents;
mod boards;
mod briefs;
mod commit;
mod config;
mod executor;
mod guard;
#[allow(dead_code)]
mod module_bindings;
mod space;
mod ticket;
mod worker;

use anyhow::Result;
use clap::{Parser, Subcommand};
use config::HostConfig;
use std::collections::HashSet;
use std::path::PathBuf;
use std::time::Duration;

#[derive(Parser)]
#[command(name = "cadswarm-host")]
struct Cli {
    /// Folder for host.json, the model, exports and agent folders (git-ignored).
    #[arg(long, default_value = "host-data", global = true)]
    data: PathBuf,
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Write host.json. The module must already be published (scripts/publish-*.ps1).
    Init {
        #[arg(long, default_value = "local")]
        mode: String,
        #[arg(long, default_value = "cadswarm")]
        db: String,
        #[arg(long, default_value = "https://cadswarm.design/app")]
        web_url: String,
        /// Python with the cadswarm package installed.
        #[arg(long, default_value = "python")]
        python: PathBuf,
        /// The agent CLI that builds briefs: claude, codex or opencode.
        #[arg(long, default_value = "claude")]
        agent: String,
    },
    /// Mint an invite and print its ticket, phone link and QR code.
    Invite {
        #[arg(long, default_value = "human")]
        kind: String,
        #[arg(long, default_value = "member")]
        role: String,
    },
    /// Draw a QR code as rows of 1s and 0s (the team panel's Remote tab paints it).
    Qr {
        #[arg(long)]
        text: String,
    },
    /// Run the executor and brief orchestration until stopped.
    Run,
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Init { mode, db, web_url, python, agent } => {
            if !config::AGENT_CLIENTS.contains(&agent.as_str()) {
                anyhow::bail!("--agent must be one of {:?}", config::AGENT_CLIENTS);
            }
            let uri = if mode == "maincloud" { "https://maincloud.spacetimedb.com" } else { "http://127.0.0.1:3000" };
            std::fs::create_dir_all(&cli.data)?;
            let cfg = HostConfig {
                mode,
                uri: uri.into(),
                db,
                token: config::spacetime_token()?,
                // Not canonicalize: on Windows that yields a `\\?\` path FreeCAD and Python mishandle.
                data_dir: std::path::absolute(&cli.data).unwrap_or(cli.data.clone()),
                freecad_python: config::python_says(&python, "c.find_freecad_python()")?,
                worker_script: config::python_says(&python, "c.worker_script()")?,
                web_url,
                cadswarm_python: python,
                agent_client: agent,
                agent_command: Vec::new(),
            };
            cfg.save()?;
            println!("wrote {}", HostConfig::path(&cfg.data_dir).display());
        }
        Cmd::Invite { kind, role } => {
            let cfg = HostConfig::load(&cli.data)?;
            let space = space::Space::attach(&cfg)?;
            let t = mint_invite(&space, &cfg, &kind, &role)?;
            let url = ticket::phone_url(&cfg.web_url, &t);
            println!("ticket: {t}\nphone:  {url}\n");
            let qr = qrcode::QrCode::new(url.as_bytes())?;
            println!("{}", qr.render::<qrcode::render::unicode::Dense1x2>().quiet_zone(true).build());
        }
        Cmd::Qr { text } => println!("{}", qr_rows(&text)?),
        Cmd::Run => run(&HostConfig::load(&cli.data)?)?,
    }
    Ok(())
}

pub fn mint_invite(space: &space::Space, cfg: &HostConfig, kind: &str, role: &str) -> Result<String> {
    let token = ticket::new_token();
    space.create_invite(ticket::hash(&token), kind, role, 86_400)?;
    Ok(ticket::encode(&ticket::Ticket {
        v: 1,
        uri: cfg.uri.clone(),
        db: cfg.db.clone(),
        token,
        kind: kind.into(),
        role: role.into(),
        host_addr: String::new(),
    }))
}

fn run(cfg: &HostConfig) -> Result<()> {
    // One host per folder: a second one is refused before it touches the database.
    let _instance = guard::InstanceGuard::acquire(&cfg.data_dir)?;
    let space = space::Space::connect(cfg)?;
    let mut worker = worker::WorkerDriver::new(
        cfg.freecad_python.clone(),
        cfg.worker_script.clone(),
        cfg.doc_path(),
        Duration::from_secs(120),
    );
    // Optional: an old cadswarm package without helper.py still runs FreeCAD ops; file and
    // knowledge ops then complete as failed with `helper_unavailable`.
    let mut helper = match config::python_says(&cfg.cadswarm_python, "c.helper_script()") {
        Ok(script) => Some(
            worker::WorkerDriver::new(cfg.cadswarm_python.clone(), script, cfg.data_dir.clone(), Duration::from_secs(120))
                .with_env("CADSWARM_FREECAD_PYTHON", cfg.freecad_python.to_string_lossy()),
        ),
        Err(e) => {
            let why = e.to_string();
            eprintln!("[host] helper unavailable: {}", why.trim().lines().last().unwrap_or(""));
            None
        }
    };
    executor::refresh(&space, &mut worker, &[])?;
    executor::write_status(cfg, &mut worker)?;
    let mut orchestrator = briefs::Orchestrator::new(cfg.clone());
    println!("cadswarm-host running on {} / {} (fence {})", cfg.uri, cfg.db, space.fence());
    // Ops this process already tried: never run one twice, even if it still reads `queued`
    // (start_op refused it, or the cache lags).
    let mut tried: HashSet<String> = HashSet::new();
    // When the sweep retires this host (its laptop slept past the orphan window), every reducer
    // call fails with `left:` until it rejoins; retry at most every 10s.
    let mut last_revive: Option<std::time::Instant> = None;
    // Set when a reducer says the host is no longer a member: the subscription cache can stay silent
    // about that (it went quiet with the network), so the answer itself has to trigger the rejoin.
    let mut retired = false;
    // Heartbeat like every other member, well inside the module's 30s stale window.
    let mut last_beat: Option<std::time::Instant> = None;
    loop {
        if retired || space.retired() {
            if last_revive.map_or(true, |t| t.elapsed() >= Duration::from_secs(10)) {
                last_revive = Some(std::time::Instant::now());
                match space.revive() {
                    Ok(()) => {
                        retired = false;
                        println!("[host] rejoined after being retired (fence {})", space.fence());
                    }
                    Err(e) => eprintln!("[host] rejoin failed: {e:#}"),
                }
            }
            std::thread::sleep(Duration::from_millis(100));
            continue;
        }
        if last_beat.map_or(true, |t| t.elapsed() >= Duration::from_secs(10)) {
            last_beat = Some(std::time::Instant::now());
            if let Err(e) = space.heartbeat() {
                let msg = format!("{e:#}");
                eprintln!("[host] heartbeat failed: {msg}");
                retired = space::says_retired(&msg);
            }
        }
        if let Some(op) = space.queued_ops().into_iter().find(|o| !tried.contains(&o.id)) {
            tried.insert(op.id.clone());
            if let Err(e) = executor::run_op(&space, &mut worker, helper.as_mut(), cfg, &op) {
                eprintln!("[host] op {} failed to run: {e:#}", op.id);
            }
            continue;
        }
        orchestrator.tick(&space, helper.as_mut());
        std::thread::sleep(Duration::from_millis(100));
    }
}

/// A QR code for `text` as rows of `1` (dark) and `0` (light), quiet zone included.
///
/// The panel paints these as an image, so the Scanning flow needs no image library in Python.
/// Low error correction: the code is shown on a screen, and a smaller code means bigger modules.
fn qr_rows(text: &str) -> Result<String> {
    let qr = qrcode::QrCode::with_error_correction_level(text.as_bytes(), qrcode::EcLevel::L)?;
    Ok(qr
        .render::<char>()
        .quiet_zone(true)
        .light_color('0')
        .dark_color('1')
        .build())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn qr_rows_are_a_square_grid_with_a_quiet_border() {
        let art = qr_rows("https://cadswarm.design/app/#t=ABC").unwrap();
        let rows: Vec<&str> = art.lines().collect();
        assert!(rows.len() > 21, "a quiet zone and modules, not an empty string");
        assert!(rows.iter().all(|r| r.len() == rows.len()), "square modules");
        assert!(rows[0].chars().all(|c| c == '0'), "the top quiet zone is blank");
        assert!(rows[rows.len() - 1].chars().all(|c| c == '0'), "the bottom quiet zone is blank");
        assert!(art.contains('1'), "it actually drew something");
        assert!(art.chars().all(|c| c == '0' || c == '1' || c == '\n'));
    }
}
