//! A brief's life: new → planning → building → (reviewing) → ready, driven by the host.

use crate::agents::Agent;
use crate::boards::{load_boards, seed_params, Board};
use crate::config::HostConfig;
use crate::space::Space;
use crate::worker::WorkerDriver;
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::PathBuf;
use std::time::{Duration, Instant};

/// A handoff whose agent failed to start waits this long before the next try (each try mints an invite).
const HANDOFF_RETRY: Duration = Duration::from_secs(30);
/// The reviewer's verdict reaches the host through the subscription, a moment after the reviewer exits.
const VERDICT_GRACE: Duration = Duration::from_secs(10);
/// How many starts of one brief may fail in a row before the brief fails with its agent's log. A failed
/// start mints an identity and leaves a join/leave pair in the team, so it must not repeat every tick.
pub const MAX_SPAWN_FAILURES: u32 = 3;
/// Waits after the first, second and third failed start. A slow failure (a CLI that dies at once) then
/// costs a bounded number of agents instead of one per 100 ms tick.
const SPAWN_BACKOFF: [Duration; 3] = [Duration::from_secs(2), Duration::from_secs(10), Duration::from_secs(30)];

pub const MAX_WORKERS: usize = 3;
pub const MAX_REVIEW_ROUNDS: u32 = 3;
/// How often the host clears one-job agents whose MCP server is gone (see `sweep_stale_agents`).
const ORPHAN_SWEEP: Duration = Duration::from_secs(10);

#[derive(Debug, Clone, Default)]
pub struct BriefState {
    pub status: String,
    pub seeding: bool,
    pub planner_alive: bool,
    pub workers_alive: usize,
    pub open: usize,
    pub active: usize,
    pub done: usize,
    /// Stopped by a person. It never becomes done afterwards, so a brief with nothing done and
    /// every task stopped has nothing left to build.
    pub cancelled: usize,
    /// Reported blocked by a helper: it wants a person's decision. Nothing can claim it meanwhile,
    /// but its own helper may report it done after that decision, so it holds the brief open.
    pub blocked: usize,
    /// The planner has written a plan. A building brief always has one; if its task rows vanish
    /// the cache is not merely waiting for the first subscription update.
    pub planned: bool,
    pub exporting: bool,
    pub reviewer_alive: bool,
    /// The newest verdict from the subscription, with its round and whether it passed.
    pub review: Option<(u32, bool)>,
    pub reviewed_round: u32,
    /// The brief is `reviewing` but this host holds no reviewer run for it: it restarted mid-review,
    /// or a round from a previous run already left a verdict. Either way the review must be asked again.
    pub review_orphaned: bool,
}

#[derive(Debug, PartialEq)]
pub enum Action {
    Wait,
    Seed,
    Plan,
    Build(usize),
    Review,
    Export,
    Fail(String),
}

pub fn next_action(s: &BriefState) -> Action {
    match s.status.as_str() {
        "new" if !s.seeding => Action::Seed,
        "new" => Action::Wait,
        "planning" if s.seeding || s.planner_alive => Action::Wait,
        "planning" if s.open + s.active + s.done == 0 => Action::Plan,
        "planning" => Action::Build(s.open.min(MAX_WORKERS)),
        "building" if s.open + s.active == 0 => {
            if s.exporting {
                Action::Wait
            } else if s.done > 0 {
                // Finished parts are delivered even when another task is blocked: the export skips
                // parts no done task promised, and the blocked task keeps its question on the board.
                // Failing here would throw those parts away for a decision that is still open.
                Action::Review
            } else if s.cancelled > 0 && s.blocked == 0 {
                Action::Fail("every task was stopped; a person needs to steer".into())
            } else if s.planned && s.blocked == 0 {
                // The plan is written but its rows are gone: the last task was removed.
                Action::Fail("every task was removed; a person needs to steer".into())
            } else {
                // Nothing can be built right now, but this is not a stuck brief: a blocked task is
                // waiting for a person's answer and its own helper may report it done afterwards
                // (only a cancelled task is refused), and the subscription may still be delivering
                // the first task rows. The stuck task stays visible on the board meanwhile.
                Action::Wait
            }
        }
        // No live worker but open work: start a generation for it. A task whose helper went quiet is
        // `awaiting_takeover` for a few seconds until the host's sweep takes that member off the team
        // (which hands the task back), so it becomes `open` here rather than spawning a worker that
        // would find nothing to claim.
        "building" if s.open > 0 && s.workers_alive == 0 => Action::Build(s.open.min(MAX_WORKERS)),
        "reviewing" if s.reviewer_alive || s.exporting => Action::Wait,
        // No reviewer of ours is running this round: a host restarted, or the only verdict is from a
        // previous run. Judge that verdict if it is final, otherwise run the round again. (Building
        // first would spawn a worker with nothing to claim.)
        "reviewing" if s.review_orphaned => match s.review {
            Some((_, true)) => Action::Export,
            Some((round, false)) if round >= MAX_REVIEW_ROUNDS => {
                Action::Fail(format!("review failed {round} times; a person needs to steer"))
            }
            // The reviewer files its fix tasks before reporting a failed verdict. After a restart
            // those tasks are still open: build them before spending another review round.
            Some((_, false)) if s.open > 0 => Action::Build(s.open.min(MAX_WORKERS)),
            // The same fix tasks, still claimed by the workers the restart killed. The sweep hands
            // them back as open once their member goes stale, so wait for that instead of reviewing
            // a model that has not changed yet.
            Some((_, false)) if s.active > 0 => Action::Wait,
            _ => Action::Review,
        },
        "reviewing" => match s.review {
            Some((_, true)) => Action::Export,
            Some((round, false)) if round >= MAX_REVIEW_ROUNDS => {
                Action::Fail(format!("review failed {round} times; a person needs to steer"))
            }
            Some((round, false)) if round > s.reviewed_round => Action::Build(s.open.min(MAX_WORKERS).max(1)),
            _ => Action::Wait,
        },
        _ => Action::Wait,
    }
}

/// The names an export should use, or why it must not run. Parts from stopped or blocked tasks may
/// be absent on purpose; a part a done task promised but the model does not have is a broken model.
pub fn export_names(tasks: &[crate::space::TaskRow], present: &[String]) -> Result<Vec<String>, String> {
    let mut missing: Vec<String> = tasks.iter().filter(|t| t.status == "done")
        .flat_map(|t| t.objects.clone())
        .filter(|n| !present.contains(n))
        .collect();
    missing.sort();
    missing.dedup();
    if !missing.is_empty() {
        Err(format!("the model is missing {}", missing.join(", ")))
    } else if present.is_empty() {
        Err("nothing to export: no part was built".into())
    } else {
        Ok(present.to_vec())
    }
}

/// Where the board library might be: $CADSWARM_BOARDS, the project folder, the working
/// directory, then the repo the binary was built in (host/target/<profile>/cadswarm-host).
fn board_library_candidates(cfg: &HostConfig) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = std::env::var_os("CADSWARM_BOARDS").map(PathBuf::from).into_iter().collect();
    out.push(cfg.data_dir.join("boards.json"));
    if let Ok(cwd) = std::env::current_dir() {
        out.push(cwd.join("boards").join("boards.json"));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(repo) = exe.ancestors().nth(4) {
            out.push(repo.join("boards").join("boards.json"));
        }
    }
    out
}

/// The suffix a brief's one-job agents carry in their name (`b_f413f129` -> `f413f129`).
pub fn brief_suffix(brief_id: &str) -> &str {
    brief_id.strip_prefix("b_").unwrap_or(brief_id)
}

/// True for the agents the host starts for one brief: `planner-<suffix>`, `workerN-<suffix>`,
/// `reviewer-<suffix>`. They are one-job identities, not teammates: when the brief is over nobody
/// should see them sitting in the team list with nothing to do.
pub fn is_brief_agent(name: &str, brief_id: &str) -> bool {
    let Some((role, tail)) = name.rsplit_once('-') else { return false };
    if !tail.eq_ignore_ascii_case(brief_suffix(brief_id)) {
        return false;
    }
    let role = role.to_ascii_lowercase();
    role == "planner"
        || role == "reviewer"
        || role.strip_prefix("worker").is_some_and(|n| !n.is_empty() && n.chars().all(|c| c.is_ascii_digit()))
}

/// True for any one-job agent name (`planner-…`, `worker3-…`, `reviewer-…`, `handoff-…`), with no brief
/// at hand: a host that stopped mid-brief leaves these behind as live rows that no longer have a process.
pub fn is_one_job_agent(name: &str) -> bool {
    let name = name.to_ascii_lowercase();
    if name.starts_with("planner-") || name.starts_with("reviewer-") || name.starts_with("handoff-") {
        return true;
    }
    name.strip_prefix("worker").and_then(|rest| rest.split_once('-')).is_some_and(
        |(n, tail)| !tail.is_empty() && !n.is_empty() && n.chars().all(|c| c.is_ascii_digit()),
    )
}

/// A member row that no agent can act through any more: its MCP server is gone (stale), or the member
/// left / was taken over / removed. `""` means the row is not there yet (a start in progress).
fn unusable_member(status: &str) -> bool {
    !(status.is_empty() || status == "active")
}

#[derive(Default)]
struct Runtime {
    seed_ops: Vec<String>,
    seeding: bool,
    planner: Option<Agent>,
    workers: Vec<Agent>,
    export_op: Option<String>,
    reviewer: Option<Agent>,
    review_round: u32,
    reviewed_round: u32,
    reviewer_exited: Option<Instant>,
    spawned: usize,
    /// Finished tasks when the current generation (planner, workers, reviewer) started, so a run that
    /// died before touching anything is told apart from one that did its work and stopped.
    done_at_spawn: usize,
    /// Starts in a row that failed for this brief. At MAX_SPAWN_FAILURES the brief fails with the log.
    failures: u32,
    /// The brief's own work as last seen: plan written, tasks done, task rows, review round. Any
    /// change means the last generation did something, so it clears `failures` (a brief that is
    /// making progress must not inherit the count from an earlier run of bad luck).
    progress: Progress,
    /// No new agent for this brief before then (the join/leave storm fix).
    retry_at: Option<Instant>,
    /// The folder of the last agent that failed to start, for its log.
    last_failed: Option<String>,
}

/// What the brief has done so far, compared tick to tick to spot a generation worth respecting.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
struct Progress {
    planned: bool,
    done: usize,
    tasks: usize,
    review_round: u32,
}

/// Remember `now`, saying whether the brief moved on since the last tick.
fn note_progress(seen: &mut Progress, now: Progress) -> bool {
    if *seen == now {
        return false;
    }
    *seen = now;
    true
}

pub struct Orchestrator {
    cfg: HostConfig,
    boards: BTreeMap<String, Board>,
    runs: HashMap<String, Runtime>,
    handoffs: HashMap<String, Agent>,
    handoff_failed: HashMap<String, Instant>,
    /// When host.json was last re-read, so a person's new AI-helper choice takes effect without a restart.
    agent_checked: Instant,
    /// Briefs whose finished agents this host has already cleared away (once per host run).
    cleaned: HashSet<String>,
    /// When the stale one-job agents were last taken off the team (None: on the first tick, so a host
    /// that was restarted mid-brief recovers their work).
    last_sweep: Option<Instant>,
}

/// How long to wait before starting another agent for a brief after `failures` failed starts.
fn retry_delay(failures: u32) -> Duration {
    SPAWN_BACKOFF[(failures.saturating_sub(1) as usize).min(SPAWN_BACKOFF.len() - 1)]
}

/// True when a generation ended with work still waiting and nothing finished: it never got going.
fn unproductive(done_now: usize, done_at_spawn: usize, open: usize, active: usize) -> bool {
    done_now <= done_at_spawn && open + active > 0
}

fn op_id() -> String {
    format!("o_{}", &crate::ticket::new_token()[..12])
}

/// A plan JSON with the seeded board's datasheet links put back, when it has none of its own.
/// The planner overwrites `plan_json` when it saves; the app still needs `sources` (plan 05).
fn with_sources(plan_json: &str, sources: &Value) -> Option<String> {
    if !sources.as_object().is_some_and(|o| !o.is_empty()) {
        return None;
    }
    let mut plan: Value = serde_json::from_str(plan_json).ok()?;
    let obj = plan.as_object_mut()?;
    if obj.get("sources").and_then(Value::as_object).is_some_and(|o| !o.is_empty()) {
        return None;
    }
    obj.insert("sources".into(), sources.clone());
    Some(plan.to_string())
}

/// Ask the helper for a board the local library does not have (plan 05).
fn looked_up_board(
    boards: &mut BTreeMap<String, Board>,
    b: &crate::space::BriefRow,
    helper: Option<&mut WorkerDriver>,
) -> Option<Board> {
    if b.board.is_empty() {
        return None;
    }
    let reply = match helper?.call("board_spec", &json!({"board": b.board, "question": b.text}), None) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("[host] board_spec for {}: {e}", b.board);
            return None;
        }
    };
    let mut board = match serde_json::from_value::<Board>(reply["result"].clone()) {
        Ok(bd) => bd,
        Err(e) => {
            eprintln!("[host] board_spec for {} was unusable: {e}", b.board);
            return None;
        }
    };
    // The helper validated this spec (source URLs and plausible dimensions), so trust it for the
    // rest of the session instead of asking again for the next brief on this board.
    board.verified = true;
    eprintln!("[host] board_spec for {}: {} ({} x {} mm)", b.board, board.label, board.pcb[0], board.pcb[1]);
    boards.insert(b.board.clone(), board.clone());
    Some(board)
}

/// Put the board's datasheet links back into a plan the planner saved without them.
fn keep_sources(boards: &BTreeMap<String, Board>, space: &Space, b: &crate::space::BriefRow) -> anyhow::Result<()> {
    let Some(sources) = boards.get(&b.board).and_then(Board::datasheet_sources) else { return Ok(()) };
    if let Some(merged) = with_sources(&space.brief_plan(&b.id), sources) {
        space.set_brief(&b.id, "", &merged)?;
    }
    Ok(())
}

/// Index a finished brief so planners can recall it and the scheduled Workflow can learn lessons.
/// Best effort: an unreadable knowledge store must not fail the build.
fn index_design(space: &Space, helper: &mut WorkerDriver, b: &crate::space::BriefRow) {
    let plan = serde_json::from_str::<Value>(&space.brief_plan(&b.id)).unwrap_or_default();
    let args = json!({
        "brief": {"id": b.id, "text": b.text, "board": b.board, "status": "ready"},
        "plan": plan,
        "reviews": space.reviews_json(&b.id),
        "params": space.params_map(),
    });
    // The style memory learns from every finished brief.
    crate::executor::observe(space, helper, "observe_brief", &args);
    if let Err(e) = helper.call("index_design", &args, None) {
        eprintln!("[host] index_design for brief {}: {e}", b.id);
    }
}

/// The team's learned design style for this request, as a prompt paragraph ("" when nothing is learned yet).
fn style_block(helper: Option<&mut WorkerDriver>, text: &str, board: &str) -> String {
    let Some(h) = helper else { return String::new() };
    match h.call("style_context", &json!({"query": text, "board": board}), None) {
        Ok(r) => r["result"]["text"].as_str().filter(|t| !t.is_empty()).map(|t| format!("

{t}")).unwrap_or_default(),
        Err(e) => {
            eprintln!("[host] style_context: {e}");
            String::new()
        }
    }
}

impl Orchestrator {
    pub fn new(cfg: HostConfig) -> Self {
        let candidates = board_library_candidates(&cfg);
        let boards = candidates
            .iter()
            .find(|p| p.is_file())
            .and_then(|p| match load_boards(p) {
                Ok(b) => {
                    eprintln!("[host] {} boards from {}", b.len(), p.display());
                    Some(b)
                }
                Err(e) => {
                    eprintln!("[host] board library {} is unreadable: {e}", p.display());
                    None
                }
            })
            .unwrap_or_else(|| {
                eprintln!("[host] no board library found; briefs start without measured params");
                BTreeMap::new()
            });
        // The reviewer is an ordinary agent started with the host's own CLI, so every brief is reviewed.
        Orchestrator { cfg, boards, runs: HashMap::new(), handoffs: HashMap::new(), handoff_failed: HashMap::new(),
                       agent_checked: Instant::now(), cleaned: HashSet::new(), last_sweep: None }
    }

    /// The status of the newest member row with this name ("" when there is none).
    fn member_status(space: &Space, name: &str) -> String {
        space.members().into_iter()
            .filter(|m| m.name.eq_ignore_ascii_case(name))
            .max_by_key(|m| m.id)
            .map(|m| m.status)
            .unwrap_or_default()
    }

    /// True when this agent is done with the brief: its CLI exited, or its MCP server went quiet and the
    /// member is stale, so no tool call it makes can reach the team. A CLI still running without its MCP
    /// server is stopped here instead of holding a task nobody can finish (that task then goes back to
    /// the team and a fresh worker picks it up).
    fn finished(space: &Space, a: &mut Agent) -> bool {
        if !a.alive() {
            return true;
        }
        if !unusable_member(&Self::member_status(space, &a.name)) {
            return false;
        }
        a.kill();
        true
    }

    /// Take one finished agent off the team: it is a one-job identity, so once its CLI has exited it must
    /// not sit in the team list with no task. Leaving its claimed work behind would strand it, so the
    /// module hands that back to the team (`revoke_member`). Its sign-in is cleared too, so the next start
    /// (a planner retry, the next review round) joins with its fresh invite instead of a retired identity.
    fn retire_agent(cfg: &HostConfig, space: &Space, name: &str) {
        for m in space.members() {
            let live = m.status == "active" || m.status == "stale";
            if !live || m.kind == "host" || m.kind == "human" || !m.name.eq_ignore_ascii_case(name) {
                continue;
            }
            match space.revoke_member(m.id) {
                Ok(()) => eprintln!("[host] {name} finished its job; taken off the team"),
                Err(e) => eprintln!("[host] could not take {name} off the team: {e}"),
            }
        }
        crate::agents::forget_identity(cfg, name);
    }

    /// The agents one brief started: from its member rows and from the folders left by earlier hosts
    /// (a restart loses the runtimes, not the leftovers).
    fn brief_agents(cfg: &HostConfig, space: &Space, brief_id: &str) -> Vec<String> {
        let mut names: Vec<String> = crate::agents::run_dirs(cfg).into_iter()
            .filter(|n| is_brief_agent(n, brief_id))
            .collect();
        names.extend(space.members().into_iter().map(|m| m.name).filter(|n| is_brief_agent(n, brief_id)));
        names.sort();
        names.dedup();
        names
    }

    /// A brief is over: stop whatever is still running for it, take its agents off the team and delete the
    /// folders of every start. A finished brief used to leave a dozen "workers" in the team list and one
    /// log folder per start on disk.
    fn finish(&mut self, space: &Space, b: &crate::space::BriefRow) {
        let mut names: HashSet<String> = Self::brief_agents(&self.cfg, space, &b.id).into_iter().collect();
        if let Some(mut rt) = self.runs.remove(&b.id) {
            let agents: Vec<Agent> = rt.planner.take().into_iter()
                .chain(rt.workers.drain(..))
                .chain(rt.reviewer.take())
                .collect();
            for mut a in agents {
                a.kill();
                names.insert(a.name);
            }
        }
        let mut names: Vec<String> = names.into_iter().collect();
        names.sort();
        for name in names {
            Self::retire_agent(&self.cfg, space, &name);
            crate::agents::remove_run_dir(&self.cfg, &name);
        }
    }

    /// A one-job agent whose member has gone quiet (its MCP server is gone) is not a teammate: take it
    /// off the team, which also hands its task back so a fresh worker can finish it. Checked every few
    /// seconds so a host that was closed mid-brief recovers the work within half a minute of restarting.
    fn sweep_stale_agents(cfg: &HostConfig, space: &Space) {
        let stale: Vec<String> = space.members().into_iter()
            .filter(|m| m.status == "stale" && m.kind != "host" && m.kind != "human")
            .map(|m| m.name)
            .filter(|n| is_one_job_agent(n))
            .collect();
        for name in stale {
            Self::retire_agent(cfg, space, &name);
        }
    }

    /// A person can pick their AI helper in the guide after the host started: re-read host.json every
    /// couple of seconds so the next agent uses that CLI, instead of requiring a host restart.
    fn refresh_agent(&mut self) {
        if self.agent_checked.elapsed() < Duration::from_secs(2) {
            return;
        }
        self.agent_checked = Instant::now();
        if let Ok(cfg) = HostConfig::load(&self.cfg.data_dir) {
            if cfg.agent_client != self.cfg.agent_client || cfg.agent_command != self.cfg.agent_command {
                eprintln!("[host] agents now run {} (from host.json)", cfg.agent_client);
            }
            self.cfg.agent_client = cfg.agent_client;
            self.cfg.agent_command = cfg.agent_command;
        }
    }

    /// Mint an identity and start one agent, refusing to spend an invite when its CLI cannot start.
    fn start(cfg: &HostConfig, space: &Space, kind: &str, role: &str, name: &str, prompt: &str) -> anyhow::Result<Agent> {
        crate::agents::preflight(cfg)?;
        let ticket = crate::mint_invite(space, cfg, kind, role)?;
        Agent::spawn(cfg, name, &ticket, prompt)
    }

    /// Count a failed start and hold the next try back (no invite is minted while we wait).
    fn record_failure(rt: &mut Runtime, name: &str, why: &str) {
        rt.failures += 1;
        rt.last_failed = Some(name.to_string());
        if rt.failures >= MAX_SPAWN_FAILURES {
            rt.retry_at = None;  // the next tick fails the brief with this agent's log
            eprintln!("[host] {} could not start (try {} of {}): {} (giving up on this brief)",
                      name, rt.failures, MAX_SPAWN_FAILURES, why);
            return;
        }
        rt.retry_at = Some(Instant::now() + retry_delay(rt.failures));
        eprintln!("[host] {} could not start (try {} of {}): {} (next try in {}s)",
                  name, rt.failures, MAX_SPAWN_FAILURES, why, retry_delay(rt.failures).as_secs());
    }

    /// The brief cannot get an agent at all: say so once, with the agent's own log, instead of retrying.
    fn fail_unstartable(cfg: &HostConfig, space: &Space, b: &crate::space::BriefRow,
                        rt: &mut Runtime) -> anyhow::Result<()> {
        let name = rt.last_failed.clone().unwrap_or_default();
        let why = format!(
            "could not start an AI helper for this brief {MAX_SPAWN_FAILURES} times in a row \
             (last: {name}); check that {} works on this computer{}",
            if cfg.agent_command.is_empty() { cfg.agent_client.clone() } else { cfg.agent_command.join(" ") },
            crate::agents::log_tail(cfg, &name, 12)
        );
        eprintln!("[host] brief {} failed: {why}", b.id);
        space.set_brief(&b.id, "failed", &json!({"error": why}).to_string())?;
        rt.retry_at = None;
        Ok(())
    }

    pub fn tick(&mut self, space: &Space, mut helper: Option<&mut WorkerDriver>) {
        self.refresh_agent();
        // A one-job agent whose MCP server is gone can never finish its task; take it off the team, which
        // hands the task back. This is also how a host that was restarted mid-brief recovers the work.
        if self.last_sweep.map_or(true, |t| t.elapsed() >= ORPHAN_SWEEP) {
            self.last_sweep = Some(Instant::now());
            Self::sweep_stale_agents(&self.cfg, space);
        }
        // A person handed work to an agent: one agent per open handoff, until it accepts and finishes.
        // An agent that dies before accepting leaves the handoff open; dropping it here makes the
        // next tick spawn a fresh one.
        for h in space.open_handoffs() {
            if self.handoffs.contains_key(&h.id)
                || self.handoff_failed.get(&h.id).is_some_and(|t| t.elapsed() < HANDOFF_RETRY)
            {
                continue;
            }
            let name = format!("handoff-{}", h.id.trim_start_matches("h_"));
            let style = style_block(helper.as_deref_mut(), &h.context_json, "");
            let prompt = format!("{}{style}", crate::agents::handoff_prompt(&h.id, &h.context_json));
            match Self::start(&self.cfg, space, "agent", "member", &name, &prompt) {
                Ok(agent) => {
                    eprintln!("[host] handoff {}: started {name}", h.id);
                    self.handoff_failed.remove(&h.id);
                    self.handoffs.insert(h.id.clone(), agent);
                }
                Err(e) => {
                    eprintln!("[host] handoff {}: {e:#} (trying again in {}s)", h.id, HANDOFF_RETRY.as_secs());
                    self.handoff_failed.insert(h.id.clone(), Instant::now());
                }
            }
        }
        // A handoff agent is one job too: when its CLI exits (or its MCP server goes quiet), take it off
        // the team and remove the folder it left, rather than leaving a "handoff-…" row with no task.
        let mut finished: Vec<String> = Vec::new();
        self.handoffs.retain(|_, a| if Self::finished(space, a) { finished.push(a.name.clone()); false } else { true });
        for name in finished {
            Self::retire_agent(&self.cfg, space, &name);
            crate::agents::remove_run_dir(&self.cfg, &name);
        }

        for b in space.briefs() {
            if b.status == "ready" || b.status == "failed" || b.status == "cancelled" {
                if self.cleaned.insert(b.id.clone()) {
                    self.finish(space, &b);
                }
                continue;
            }
            self.cleaned.remove(&b.id);
            if let Err(e) = self.step(space, &b, helper.as_deref_mut()) {
                eprintln!("[host] brief {}: {e:#}", b.id);
            }
        }
    }

    fn step(&mut self, space: &Space, b: &crate::space::BriefRow, mut helper: Option<&mut WorkerDriver>) -> anyhow::Result<()> {
        let tasks = space.tasks_for(&b.id);
        let review = space.latest_review(&b.id).map(|r| (r.round, r.passed));
        let rt = self.runs.entry(b.id.clone()).or_default();

        // A one-job agent that exited is done: take it off the team at once (revoke also hands back any
        // task it never reported), so it does not sit in the team list as an idle "worker". Do this
        // before reading the brief's state, so the counts below only include agents that are still alive.
        let planner_was = rt.planner.is_some();
        let planner_alive = rt.planner.as_mut().map(|a| !Self::finished(space, a)).unwrap_or(false);
        if planner_was && !planner_alive {
            if let Some(a) = rt.planner.take() {
                Self::retire_agent(&self.cfg, space, &a.name);
            }
        }
        let workers_was = !rt.workers.is_empty();
        let mut still_alive: Vec<Agent> = Vec::new();
        for mut w in rt.workers.drain(..) {
            if Self::finished(space, &mut w) {
                Self::retire_agent(&self.cfg, space, &w.name);
            } else {
                still_alive.push(w);
            }
        }
        rt.workers = still_alive;
        let workers_alive = rt.workers.len();
        let reviewer_was = rt.reviewer.is_some();
        let reviewer_alive = rt.reviewer.as_mut().map(|a| !Self::finished(space, a)).unwrap_or(false);
        if reviewer_was && !reviewer_alive {
            if let Some(a) = rt.reviewer.take() {
                Self::retire_agent(&self.cfg, space, &a.name);
            }
        }

        // Seed ops finished? Release Params so agents can add their own.
        if rt.seeding
            && rt.seed_ops.iter().all(|id| space.op(id).map(|o| o.status != "queued" && o.status != "running").unwrap_or(false))
        {
            rt.seeding = false;
            space.release_objects(vec!["Params".into()])?;
        }
        // Export finished?
        if let Some(id) = rt.export_op.clone() {
            if let Some(op) = space.op(&id) {
                match op.status.as_str() {
                    "done" => {
                        let hash = serde_json::from_str::<serde_json::Value>(&op.result_json)?["hash"]
                            .as_str()
                            .unwrap_or("")
                            .to_string();
                        space.set_brief_stl(&b.id, &hash)?;
                        if let Some(h) = helper.as_deref_mut() {
                            index_design(space, h, b);
                        }
                        rt.export_op = None;
                        // `set_brief_stl` just marked the brief ready, but `b` is the row this tick
                        // read. Acting on it would submit a second export for a finished brief.
                        return Ok(());
                    }
                    "failed" | "rejected" => {
                        space.set_brief(&b.id, "failed", "")?;
                        rt.export_op = None;
                        return Ok(());
                    }
                    _ => {}
                }
            }
        }

        let state = BriefState {
            status: b.status.clone(),
            seeding: rt.seeding,
            planner_alive,
            workers_alive,
            open: tasks.iter().filter(|t| t.status == "open").count(),
            active: tasks.iter().filter(|t| t.status == "claimed" || t.status == "awaiting_takeover").count(),
            done: tasks.iter().filter(|t| t.status == "done").count(),
            cancelled: tasks.iter().filter(|t| t.status == "cancelled").count(),
            blocked: tasks.iter().filter(|t| t.status == "blocked").count(),
            planned: !space.brief_plan(&b.id).is_empty(),
            exporting: rt.export_op.is_some(),
            reviewer_alive,
            review,
            reviewed_round: rt.reviewed_round,
            review_orphaned: rt.review_round == 0 && rt.reviewer.is_none(),
        };

        // A generation that moved the brief forward (wrote the plan, opened or finished a task, took
        // the review to a new round) proves agents can start on this computer, so the failed-start
        // count is about *consecutive* dead starts, not the brief's whole life. Without this, a
        // planner that needed three tries leaves the count at the cap and the next worker generation
        // that exits without finishing a task would fail a brief that is working.
        let progress = Progress { planned: state.planned, done: state.done,
                                  tasks: state.open + state.active + state.done, review_round: rt.review_round };
        if note_progress(&mut rt.progress, progress) {
            if rt.failures > 0 {
                eprintln!("[host] brief {}: made progress (plan: {}, tasks done: {}, tasks: {}, round: {}), \
                           clearing {} failed start(s)", b.id, progress.planned, progress.done, progress.tasks,
                          progress.review_round, rt.failures);
            }
            rt.failures = 0;
            rt.retry_at = None;
        }

        // A generation that ended without finishing anything never got going (the CLI died at once, the
        // folder is untrusted, a session limit). Count it and hold the next start back instead of
        // spawning an agent every tick; three in a row means no agent can start here at all.
        if planner_was && !planner_alive {
            if state.status == "planning" && state.open + state.active + state.done == 0 {
                let name = format!("planner-{}", brief_suffix(&b.id));
                Self::record_failure(rt, &name, "it exited without planning anything");
            }
        }
        if workers_was && workers_alive == 0 {
            if unproductive(state.done, rt.done_at_spawn, state.open, state.active) && !state.exporting {
                let name = format!("worker{}-{}", rt.spawned, brief_suffix(&b.id));
                Self::record_failure(rt, &name, "it exited before finishing a task");
            } else {
                rt.failures = 0;  // that generation built something (or had nothing left to build)
                rt.retry_at = None;
            }
        }

        // A reviewer that exits without a verdict would leave the brief in `reviewing` forever.
        let no_verdict = review.map(|(r, _)| r < rt.review_round).unwrap_or(true);
        if b.status == "reviewing" && rt.review_round > 0 && !state.reviewer_alive && no_verdict
            && rt.reviewer_exited.get_or_insert_with(Instant::now).elapsed() >= VERDICT_GRACE
        {
            eprintln!("[host] brief {}: reviewer round {} exited without a verdict", b.id, rt.review_round);
            space.set_brief(&b.id, "failed", &json!({"error": "the reviewer exited without a verdict"}).to_string())?;
            return Ok(());
        }

        let wanted = next_action(&state);
        // Three starts in a row that got nowhere: waiting out the last backoff will not help, so fail
        // now, with the agent's own log, instead of leaving the brief `planning` for another 30 s.
        if matches!(wanted, Action::Plan | Action::Build(_) | Action::Review) && rt.failures >= MAX_SPAWN_FAILURES {
            return Self::fail_unstartable(&self.cfg, space, b, rt);
        }
        let now = Instant::now();
        let action = match wanted {
            Action::Plan | Action::Build(_) | Action::Review if rt.retry_at.is_some_and(|t| now < t) => Action::Wait,
            a => a,
        };
        match action {
            Action::Wait => {}
            Action::Seed => {
                let local = self.boards.get(&b.board).cloned();
                let board = match local {
                    Some(bd) if bd.verified => Some(bd),
                    other => looked_up_board(&mut self.boards, b, helper.as_deref_mut()).or(other),
                };
                let params = board.as_ref().map(seed_params).unwrap_or_default();
                if !params.is_empty() {
                    space.lock_objects(vec!["Params".into()])?;
                    for (alias, value) in &params {
                        let id = op_id();
                        space.submit_op(&id, "set_param", json!({"alias": alias, "value": value}).to_string())?;
                        rt.seed_ops.push(id);
                    }
                    rt.seeding = true;
                }
                // The app shows "from datasheet" links from the plan JSON.
                let plan = board
                    .as_ref()
                    .and_then(Board::datasheet_sources)
                    .map(|s| json!({"sources": s}).to_string())
                    .unwrap_or_default();
                space.set_brief(&b.id, "planning", &plan)?;
            }
            Action::Plan => {
                let params = self.boards.get(&b.board).map(seed_params).unwrap_or_default();
                let label = self.boards.get(&b.board).map(|x| x.label.clone()).unwrap_or_else(|| {
                    if b.board.is_empty() { "none given".into() } else { b.board.clone() }
                });
                let memory = match helper.as_deref_mut() {
                    Some(h) => match h.call("recall_designs", &json!({"query": b.text, "board": b.board}), None) {
                        Ok(r) => r["result"].to_string(),
                        Err(e) => {
                            eprintln!("[host] recall_designs for brief {}: {e}", b.id);
                            "{}".into()
                        }
                    },
                    None => "{}".into(),
                };
                let style = style_block(helper.as_deref_mut(), &b.text, &b.board);
                let prompt = format!(
                    "{}\n\nWhat earlier CadSwarm designs taught us (designs and lessons, JSON): {memory}\n\
                     Apply the lessons in your plan.{style}{}",
                    crate::agents::planner_prompt(&b.id, &b.text, &label, &params),
                    if style.is_empty() { "" } else { "\nWrite these style rules into each task's detail where they apply." }
                );
                let name = format!("planner-{}", &b.id[2..]);
                eprintln!("[host] brief {}: starting {name}", b.id);
                match Self::start(&self.cfg, space, "agent", "lead", &name, &prompt) {
                    Ok(agent) => {
                        rt.planner = Some(agent);
                        rt.done_at_spawn = state.done;
                        rt.retry_at = None;
                    }
                    Err(e) => Self::record_failure(rt, &name, &format!("{e:#}")),
                }
            }
            Action::Build(n) => {
                keep_sources(&self.boards, space, b)?;
                let style = style_block(helper.as_deref_mut(), &b.text, &b.board);
                rt.done_at_spawn = state.done;
                rt.retry_at = None;
                for _ in 0..n {
                    rt.spawned += 1;
                    let name = format!("worker{}-{}", rt.spawned, &b.id[2..]);
                    eprintln!("[host] brief {}: starting {name}", b.id);
                    let prompt = format!("{}{style}", crate::agents::worker_prompt(&b.id, rt.spawned));
                    match Self::start(&self.cfg, space, "agent", "member", &name, &prompt) {
                        Ok(agent) => rt.workers.push(agent),
                        Err(e) => {
                            Self::record_failure(rt, &name, &format!("{e:#}"));
                            break;
                        }
                    }
                }
                if let Some((round, false)) = state.review {
                    rt.reviewed_round = round;
                }
                space.set_brief(&b.id, "building", "")?;
            }
            Action::Review => {
                let round = review.map(|(r, _)| r.saturating_add(1)).unwrap_or(1);
                let label = self.boards.get(&b.board).map(|x| x.label.clone()).unwrap_or_else(|| {
                    if b.board.is_empty() { "none given".into() } else { b.board.clone() }
                });
                let style = style_block(helper.as_deref_mut(), &b.text, &b.board);
                let prompt = crate::agents::review_prompt(&b.id, &b.text, &label, space.bed(), round, &style);
                let name = format!("reviewer-{}", &b.id[2..]);
                eprintln!("[host] brief {}: starting {name} (round {round})", b.id);
                match Self::start(&self.cfg, space, "reviewer", "member", &name, &prompt) {
                    Ok(agent) => {
                        rt.reviewer = Some(agent);
                        rt.review_round = round;
                        rt.reviewer_exited = None;
                        rt.done_at_spawn = state.done;
                        rt.retry_at = None;
                        space.set_brief(&b.id, "reviewing", "")?;
                    }
                    Err(e) => Self::record_failure(rt, &name, &format!("{e:#}")),
                }
            }
            Action::Export => {
                keep_sources(&self.boards, space, b)?;
                // Task rows keep their object names after a task is stopped, and a reopened task
                // can name a part that was never built. The worker treats `names` as a whitelist
                // and fails the whole op on one missing part, so export only what exists.
                let wanted: Vec<String> = tasks.iter().flat_map(|t| t.objects.clone()).collect();
                let objects = space.objects_present(&wanted);
                match export_names(&tasks, &objects) {
                    Ok(objects) => {
                        let id = op_id();
                        space.submit_op(&id, "export", json!({"format": "stl", "names": objects, "brief": b.id}).to_string())?;
                        rt.export_op = Some(id);
                    }
                    Err(why) => {
                        space.set_brief(&b.id, "failed", &json!({"error": why}).to_string())?;
                    }
                }
            }
            Action::Fail(why) => {
                eprintln!("[host] brief {} failed: {why}", b.id);
                space.set_brief(&b.id, "failed", &json!({"error": why}).to_string())?;
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn st(status: &str) -> BriefState {
        BriefState { status: status.into(), ..Default::default() }
    }

    #[test]
    fn a_new_brief_is_seeded_once() {
        assert_eq!(next_action(&st("new")), Action::Seed);
        assert_eq!(next_action(&BriefState { seeding: true, ..st("new") }), Action::Wait);
    }

    #[test]
    fn planning_starts_the_planner_then_builds_with_at_most_three_workers() {
        assert_eq!(next_action(&st("planning")), Action::Plan);
        assert_eq!(next_action(&BriefState { planner_alive: true, ..st("planning") }), Action::Wait);
        assert_eq!(next_action(&BriefState { open: 5, ..st("planning") }), Action::Build(3));
    }

    #[test]
    fn building_restarts_workers_if_they_all_died_and_reviews_when_done() {
        assert_eq!(next_action(&BriefState { open: 2, ..st("building") }), Action::Build(2));
        assert_eq!(next_action(&BriefState { open: 2, workers_alive: 1, ..st("building") }), Action::Wait);
        // A task whose helper went quiet is `awaiting_takeover` only until the host's sweep takes that
        // member off the team and hands the task back; spawning a worker now would find nothing to
        // claim. (A teammate's helper may hold a real claimed task too.)
        assert_eq!(next_action(&BriefState { active: 2, ..st("building") }), Action::Wait);
        assert_eq!(next_action(&BriefState { done: 3, ..st("building") }), Action::Review);
        assert_eq!(next_action(&BriefState { done: 3, exporting: true, ..st("building") }), Action::Wait);
    }

    #[test]
    fn a_member_that_cannot_reach_the_team_is_not_a_live_agent() {
        // The MCP server heartbeats, so "stale" means no tool call can get through any more; a start
        // that has not joined yet has no row, and is left alone until it does.
        assert!(!unusable_member(""));
        assert!(!unusable_member("active"));
        for status in ["stale", "left", "taken_over", "removed"] {
            assert!(unusable_member(status), "{status}");
        }
    }

    #[test]
    fn a_brief_whose_every_task_was_stopped_fails_instead_of_hanging() {
        assert!(matches!(next_action(&BriefState { cancelled: 2, ..st("building") }), Action::Fail(_)));
        // A blocked task is not a dead brief: its helper can still report it done after a person
        // answers, so wait for that instead of closing the brief and throwing the answer away.
        assert_eq!(next_action(&BriefState { blocked: 2, ..st("building") }), Action::Wait);
        assert_eq!(next_action(&BriefState { planned: true, blocked: 1, ..st("building") }), Action::Wait);
        // One stopped task and one stuck one: the stuck one can still finish, so wait for it.
        assert_eq!(next_action(&BriefState { cancelled: 1, blocked: 1, ..st("building") }), Action::Wait);
        // The last task was deleted after the plan was written: no rows are left to count.
        assert!(matches!(next_action(&BriefState { planned: true, ..st("building") }), Action::Fail(_)));
        // A brief whose task rows have not arrived from the subscription yet must not be failed.
        assert_eq!(next_action(&st("building")), Action::Wait);
        // A cancelled task does not hide finished work: the build still reviews and exports.
        assert_eq!(next_action(&BriefState { done: 1, cancelled: 1, ..st("building") }), Action::Review);
        // Nor does a blocked one: its part is skipped on purpose and its question stays on the board.
        assert_eq!(next_action(&BriefState { done: 1, blocked: 1, ..st("building") }), Action::Review);
    }

    #[test]
    fn reviews_pass_to_export_fail_back_to_building_and_give_up_after_three() {
        assert_eq!(next_action(&BriefState { review: Some((1, true)), ..st("reviewing") }), Action::Export);
        assert_eq!(next_action(&BriefState { review: Some((1, false)), open: 1, ..st("reviewing") }), Action::Build(1));
        assert!(matches!(next_action(&BriefState { review: Some((3, false)), ..st("reviewing") }), Action::Fail(_)));
        assert_eq!(next_action(&BriefState { reviewer_alive: true, ..st("reviewing") }), Action::Wait);
    }

    #[test]
    fn a_review_without_a_run_of_ours_is_asked_again_instead_of_building() {
        // The host restarted mid-review: no run of ours, no verdict yet.
        assert_eq!(next_action(&BriefState { review_orphaned: true, ..st("reviewing") }), Action::Review);
        // A failed round from a previous host with no fix tasks asks for the next round.
        assert_eq!(next_action(&BriefState { review: Some((1, false)), review_orphaned: true,
            ..st("reviewing") }), Action::Review);
        // The reviewer files its fixes before its verdict: after a restart those tasks are built,
        // not skipped for another review of the unchanged model.
        assert_eq!(next_action(&BriefState { review: Some((1, false)), review_orphaned: true,
            open: 2, ..st("reviewing") }), Action::Build(2));
        // ...and while the sweep has not yet reopened them (their dead workers still hold the
        // claims) waiting is right: a review now would spend a round on the unchanged model.
        assert_eq!(next_action(&BriefState { review: Some((1, false)), review_orphaned: true,
            active: 2, ..st("reviewing") }), Action::Wait);
        // A passed round from a previous host still goes straight to export.
        assert_eq!(next_action(&BriefState { review: Some((2, true)), review_orphaned: true,
            ..st("reviewing") }), Action::Export);
        // A restart does not hand back a review budget that was already spent.
        assert!(matches!(next_action(&BriefState { review: Some((3, false)), review_orphaned: true,
            ..st("reviewing") }), Action::Fail(_)));
        // A run this host started is not orphaned, so a failed round still goes back to building.
        assert_eq!(next_action(&BriefState { review: Some((1, false)), open: 1, ..st("reviewing") }), Action::Build(1));
    }

    #[test]
    fn a_brief_that_got_something_done_is_told_apart_from_a_quiet_tick() {
        let mut seen = Progress::default();
        // A brief that has done nothing yet is not progress, or every tick would clear the count.
        assert!(!note_progress(&mut seen, Progress::default()));
        // The planner got through on its third try: the plan and its task rows count.
        assert!(note_progress(&mut seen, Progress { planned: true, tasks: 2, ..Progress::default() }));
        // A worker generation that finished a task counts; a later quiet tick does not.
        let one_done = Progress { planned: true, done: 1, tasks: 2, ..Progress::default() };
        assert!(note_progress(&mut seen, one_done));
        assert!(!note_progress(&mut seen, one_done));
        // A task moving from claimed to done changes `done` while the row count stays put.
        assert!(note_progress(&mut seen, Progress { done: 2, ..one_done }));
        // A new review round is progress even when no task moved.
        assert!(note_progress(&mut seen, Progress { review_round: 1, ..one_done }));
    }

    #[test]
    fn a_failed_start_backs_off_further_each_time_until_the_brief_gives_up() {
        // No agent every tick: 2 s, then 10 s, then 30 s (the cap holds for any further try).
        assert_eq!(retry_delay(1), Duration::from_secs(2));
        assert_eq!(retry_delay(2), Duration::from_secs(10));
        assert_eq!(retry_delay(3), Duration::from_secs(30));
        assert_eq!(retry_delay(9), Duration::from_secs(30));
        assert!(MAX_SPAWN_FAILURES >= 3, "a transient failure gets a retry before the brief fails");
    }

    #[test]
    fn a_generation_that_left_work_untouched_counts_as_a_failed_start() {
        // Three workers exited and nothing was built: the next try waits instead of spawning at once.
        assert!(unproductive(0, 0, 2, 0));
        // They were still holding a claimed task: the sweep must hand it back first, so it is a failure.
        assert!(unproductive(0, 0, 0, 2));
        // One task finished: that generation did its job; new work may start at once.
        assert!(!unproductive(1, 0, 2, 0));
        // Nothing was left to build (the brief is moving to review): not a failure.
        assert!(!unproductive(0, 0, 0, 0));
    }

    #[test]
    fn a_brief_owns_exactly_the_agents_it_started() {
        assert!(is_brief_agent("planner-f413f129", "b_f413f129"));
        assert!(is_brief_agent("worker1-f413f129", "b_f413f129"));
        assert!(is_brief_agent("worker13-F413F129", "b_f413f129"));
        assert!(is_brief_agent("reviewer-f413f129", "b_f413f129"));
        // Another brief, a person's helper, a handoff, or a name that only looks like one.
        assert!(!is_brief_agent("worker1-4e086346", "b_f413f129"));
        assert!(!is_brief_agent("lucky-opencode", "b_f413f129"));
        assert!(!is_brief_agent("handoff-h_1234", "b_f413f129"));
        assert!(!is_brief_agent("workerX-f413f129", "b_f413f129"));
        assert!(!is_brief_agent("planner-f413f1290", "b_f413f129"));
        assert!(!is_brief_agent("planner", "b_f413f129"));
    }

    #[test]
    fn a_stale_one_job_agent_is_recognised_and_a_teammate_is_not() {
        // What a host that stopped mid-brief leaves behind: these rows are swept at startup.
        for name in ["planner-f413f129", "worker2-f413f129", "reviewer-f413f129", "handoff-h_ab12"] {
            assert!(is_one_job_agent(name), "{name}");
        }
        for name in ["lucky-opencode", "ann-claude", "worker", "worker-two", "host", "carol", "worker1"] {
            assert!(!is_one_job_agent(name), "{name}");
        }
    }

    fn task(id: &str, status: &str, objects: &[&str]) -> crate::space::TaskRow {        crate::space::TaskRow {
            id: id.into(),
            status: status.into(),
            objects: objects.iter().map(|s| s.to_string()).collect(),
        }
    }

    #[test]
    fn export_skips_stopped_and_blocked_parts_but_not_a_finished_one() {
        let present = vec!["Base".to_string()];
        let tasks = vec![
            task("t1", "done", &["Base"]),
            task("t2", "cancelled", &["Lid"]),
            task("t3", "blocked", &["Clip"]),
        ];
        assert_eq!(export_names(&tasks, &present), Ok(vec!["Base".to_string()]));
        // A done task's part must exist, or the brief fails with the names.
        let broken = vec![task("t1", "done", &["Base", "Lid"])];
        assert_eq!(export_names(&broken, &present), Err("the model is missing Lid".into()));
        // Every task stopped: nothing to export.
        let stopped = vec![task("t2", "cancelled", &["Lid"])];
        assert_eq!(export_names(&stopped, &[]), Err("nothing to export: no part was built".into()));
    }

    #[test]
    fn a_planners_plan_keeps_the_seeded_datasheet_links() {
        let sources = json!({"pcb": "https://x/b.pdf", "holes": "https://x/b.pdf"});
        let plan = r#"{"parts":[{"name":"Base"}]}"#;
        let merged: serde_json::Value = serde_json::from_str(&with_sources(plan, &sources).unwrap()).unwrap();
        assert_eq!(merged["parts"][0]["name"], "Base");
        assert_eq!(merged["sources"]["holes"], "https://x/b.pdf");
        // Already sourced: no rewrite. No sources or no plan: nothing to merge.
        assert!(with_sources(&merged.to_string(), &sources).is_none());
        assert!(with_sources(plan, &json!({})).is_none());
        assert!(with_sources(plan, &json!(null)).is_none());
        assert!(with_sources("", &sources).is_none());
        assert!(with_sources("[1,2]", &sources).is_none());
    }
}
