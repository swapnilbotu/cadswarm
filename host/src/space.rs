//! The only file that touches the generated SpacetimeDB bindings.

use crate::commit::Completion;
use crate::config::HostConfig;
use crate::module_bindings::*;
use anyhow::{anyhow, Result};
use spacetimedb_sdk::{DbContext, Table};
use spacetimedb_sdk::error::InternalError;
use std::sync::mpsc;
use std::time::{Duration, Instant};

type Outcome = std::result::Result<std::result::Result<(), String>, InternalError>;
const REDUCER_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, Clone)]
pub struct OpRow {
    pub id: String,
    pub by: u64,
    pub kind: String,
    pub args_json: String,
    pub allowed: Vec<String>,
    pub mutating: bool,
    pub status: String,
    pub result_json: String,
    pub error: String,
    pub created_ms: u64,
}

#[derive(Debug, Clone)]
pub struct BriefRow {
    pub id: String,
    pub text: String,
    pub board: String,
    pub status: String,
}

#[derive(Debug, Clone)]
pub struct TaskRow {
    pub id: String,
    pub status: String,
    pub objects: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct ReviewRow {
    pub round: u32,
    pub passed: bool,
}

#[derive(Debug, Clone)]
pub struct HandoffRow {
    pub id: String,
    pub context_json: String,
}

/// One row of `v_member`: enough to find the agents this host started and retire them.
#[derive(Debug, Clone)]
pub struct MemberRow {
    pub id: u64,
    pub name: String,
    pub kind: String,
    pub status: String,
}

pub struct Space {
    conn: DbConnection,
}

const QUERIES: &[&str] = &[
    "SELECT * FROM v_me",
    "SELECT * FROM v_workspace",
    "SELECT * FROM v_op",
    "SELECT * FROM v_brief",
    "SELECT * FROM v_task",
    "SELECT * FROM v_review",
    "SELECT * FROM v_member",
    "SELECT * FROM v_handoff",
    "SELECT * FROM v_param",
    "SELECT * FROM v_setting",
    "SELECT * FROM v_object",
];

/// Whether a host whose latest member row has this status should call `rejoin` by itself.
/// Only "left": the sweep retired it (e.g. the laptop slept past the orphan window). Never
/// "taken_over": a newer process replaced this one, and fighting it would flap the team.
pub fn should_rejoin(status: Option<&str>) -> bool {
    status == Some("left")
}

/// True when a reducer's answer says this host is no longer a member. The sweep can retire a host while
/// its network is away; the subscription cache then never updates, so the host keeps working as a ghost
/// (spawning agents that cannot join). The error a later call gets must be enough to rejoin.
pub fn says_retired(message: &str) -> bool {
    message.contains("left:") || message.contains("not_member:")
}

impl Space {
    /// Connect as the running host: rejoin at a new fence, superseding any older host process.
    pub fn connect(cfg: &HostConfig) -> Result<Space> {
        Self::open(cfg, true)
    }

    /// Connect alongside a running host (e.g. `cadswarm-host invite`) and reuse its fence, so the
    /// running host is not superseded.
    pub fn attach(cfg: &HostConfig) -> Result<Space> {
        Self::open(cfg, false)
    }

    fn open(cfg: &HostConfig, rejoin: bool) -> Result<Space> {
        let conn = DbConnection::builder()
            .with_uri(cfg.uri.as_str())
            .with_database_name(cfg.db.as_str())
            .with_token(Some(cfg.token.clone()))
            .on_connect(|_, identity, _| eprintln!("[host] connected as {identity:?}"))
            .on_connect_error(|_, e| eprintln!("[host] connect error: {e:?}"))
            .on_disconnect(|_, e| eprintln!("[host] disconnected: {e:?}"))
            .build()
            .map_err(|e| anyhow!("connecting to {} / {}: {e:?}", cfg.uri, cfg.db))?;
        conn.run_threaded();
        conn.subscription_builder()
            .on_error(|_, e| eprintln!("[host] subscription error: {e:?}"))
            .subscribe(QUERIES);
        let space = Space { conn };
        if !rejoin {
            // Attaching alongside a live host: it must already be a member with a fence to reuse.
            space.wait_for(Duration::from_secs(20), |s| s.me().is_some())?;
            return Ok(space);
        }
        // Wait for the subscription so `before` is the real row, but do not require it: a fresh
        // database may have no row yet, and `rejoin` creates one.
        let _ = space.wait_for(Duration::from_secs(10), |s| s.me().is_some());
        let before = space.row_key();
        space.rejoin("host")?;
        // An active row comes back with a higher fence; a retired ("left") one comes back as a new
        // row whose fence starts again at 1, so "fence went up" alone would never be seen.
        space.wait_for(Duration::from_secs(20), |s| !s.retired() && s.row_key().is_some() && s.row_key() != before)?;
        Ok(space)
    }

    pub fn wait_for(&self, timeout: Duration, ok: impl Fn(&Space) -> bool) -> Result<()> {
        let t = Instant::now();
        while t.elapsed() < timeout {
            if ok(self) {
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        Err(anyhow!("timed out waiting for SpacetimeDB"))
    }

    fn me(&self) -> Option<Member> {
        self.conn.db.v_me().iter().next()
    }

    pub fn fence(&self) -> u32 {
        self.me().map(|m| m.fence).unwrap_or(0)
    }

    /// True when the sweep has retired this host's member row, so every reducer call would fail.
    pub fn retired(&self) -> bool {
        should_rejoin(self.me().as_ref().map(|m| m.status.as_str()))
    }

    /// Rejoin as host after being retired, and wait until the new row is visible.
    pub fn revive(&self) -> Result<()> {
        self.rejoin("host")?;
        self.wait_for(Duration::from_secs(20), |s| !s.retired() && s.me().is_some())
    }

    /// This process's member row and fence; a rejoin always changes one of them.
    fn row_key(&self) -> Option<(u64, u32)> {
        self.me().map(|m| (m.id, m.fence))
    }

    pub fn bed(&self) -> [f64; 3] {
        self.conn
            .db
            .v_workspace()
            .iter()
            .next()
            .map(|w| [w.bed_x_mm, w.bed_y_mm, w.bed_z_mm])
            .unwrap_or([180.0, 180.0, 180.0])
    }

    /// The team's precision settings as `[{"setting", "num", "text"}]` (missing ones are at their default).
    /// The worker and helper read them from an op's `_settings`.
    pub fn settings_rows(&self) -> serde_json::Value {
        serde_json::Value::Array(
            self.conn
                .db
                .v_setting()
                .iter()
                .map(|r| serde_json::json!({"setting": r.setting, "num": r.num, "text": r.text}))
                .collect(),
        )
    }

    fn op_row(o: Op) -> OpRow {
        OpRow {
            id: o.id,
            by: o.by,
            kind: o.kind,
            args_json: o.args_json,
            allowed: o.allowed,
            mutating: o.mutating,
            status: o.status,
            result_json: o.result_json,
            error: o.error,
            created_ms: o.created_ms,
        }
    }

    /// Oldest first: ops run in submission order.
    pub fn queued_ops(&self) -> Vec<OpRow> {
        let mut v: Vec<OpRow> =
            self.conn.db.v_op().iter().filter(|o| o.status == "queued").map(Self::op_row).collect();
        v.sort_by_key(|o| (o.created_ms, o.id.clone()));
        v
    }

    pub fn op(&self, id: &str) -> Option<OpRow> {
        self.conn.db.v_op().iter().find(|o| o.id == id).map(Self::op_row)
    }

    pub fn briefs(&self) -> Vec<BriefRow> {
        self.conn
            .db
            .v_brief()
            .iter()
            .map(|b| BriefRow { id: b.id, text: b.text, board: b.board, status: b.status })
            .collect()
    }

    pub fn tasks_for(&self, brief: &str) -> Vec<TaskRow> {
        self.conn
            .db
            .v_task()
            .iter()
            .filter(|t| t.brief == brief)
            .map(|t| TaskRow { id: t.id, status: t.status, objects: t.objects })
            .collect()
    }

    pub fn latest_review(&self, brief: &str) -> Option<ReviewRow> {
        self.conn
            .db
            .v_review()
            .iter()
            .filter(|r| r.brief == brief)
            .max_by_key(|r| (r.round, r.at_ms))
            .map(|r| ReviewRow { round: r.round, passed: r.passed })
    }

    /// The names from `names` that exist in the model, in order and without duplicates. Task rows
    /// outlive the parts they name (a stopped task keeps its objects; a reopened one may never have
    /// been built), so an export must ask the model what is really there.
    pub fn objects_present(&self, names: &[String]) -> Vec<String> {
        let mut out: Vec<String> = Vec::new();
        for name in names {
            if !out.contains(name) && self.conn.db.v_object().iter().any(|o| &o.name == name) {
                out.push(name.clone());
            }
        }
        out
    }

    /// Handoffs a person made and nobody picked up yet. An agent that dies before accepting
    /// leaves its handoff here, so the next tick spawns a fresh one.
    pub fn open_handoffs(&self) -> Vec<HandoffRow> {
        self.conn
            .db
            .v_handoff()
            .iter()
            .filter(|h| h.status == "open")
            .map(|h| HandoffRow { id: h.id, context_json: h.context_json })
            .collect()
    }

    /// Every reducer call waits for the module's answer (the SDK applies the transaction to the
    /// cache before the callback runs). Without this the loop could see an op it just ran still
    /// `queued` in the cache and run it twice, and reducer errors would vanish.
    fn call(
        &self,
        what: &str,
        send: impl FnOnce(Box<dyn FnOnce(&ReducerEventContext, Outcome) + Send>) -> spacetimedb_sdk::Result<()>,
    ) -> Result<()> {
        let (tx, rx) = mpsc::channel();
        send(Box::new(move |_, outcome| {
            let _ = tx.send(outcome);
        }))
        .map_err(|e| anyhow!("{what}: {e:?}"))?;
        match rx.recv_timeout(REDUCER_TIMEOUT) {
            Ok(Ok(Ok(()))) => Ok(()),
            Ok(Ok(Err(msg))) => Err(anyhow!("{what}: {msg}")),
            Ok(Err(internal)) => Err(anyhow!("{what}: internal error: {internal}")),
            Err(_) => Err(anyhow!("{what}: no answer from SpacetimeDB within {}s", REDUCER_TIMEOUT.as_secs())),
        }
    }

    pub fn rejoin(&self, client: &str) -> Result<()> {
        let client = client.to_string();
        self.call("rejoin", |cb| self.conn.reducers.rejoin_then(client, cb))
    }

    /// The host's sign of life. Holding a WebSocket is not enough: `online` belongs to the identity,
    /// so any other connection with it (`cadswarm-host invite`) clears it on disconnect, and a row
    /// made by `rejoin` starts offline. Without heartbeats the sweep then retires the host.
    pub fn heartbeat(&self) -> Result<()> {
        let fence = self.fence();
        self.call("heartbeat", |cb| self.conn.reducers.heartbeat_then(fence, String::new(), cb))
    }

    pub fn start_op(&self, id: &str) -> Result<()> {
        let (fence, id) = (self.fence(), id.to_string());
        self.call("start_op", |cb| self.conn.reducers.start_op_then(fence, id, cb))
    }

    pub fn complete_op(&self, id: &str, c: &Completion) -> Result<()> {
        let (fence, id, c) = (self.fence(), id.to_string(), c.clone());
        self.call("complete_op", |cb| {
            self.conn.reducers.complete_op_then(
                fence,
                id,
                c.ok,
                c.result_json,
                c.created,
                c.modified,
                c.deleted,
                c.recomputed,
                c.broken,
                c.error_code,
                c.error,
                cb,
            )
        })
    }

    pub fn sync_objects(&self, objects_json: String, params_json: String) -> Result<()> {
        let fence = self.fence();
        self.call("sync_objects", |cb| self.conn.reducers.sync_objects_then(fence, objects_json, params_json, cb))
    }

    pub fn put_preview(&self, object: &str, mesh: Vec<u8>) -> Result<()> {
        let (fence, object) = (self.fence(), object.to_string());
        self.call("put_preview", |cb| self.conn.reducers.put_preview_then(fence, object, mesh, cb))
    }

    pub fn put_component_preview(&self, id: &str, mesh: Vec<u8>) -> Result<()> {
        let (fence, id) = (self.fence(), id.to_string());
        self.call("put_component_preview", |cb| self.conn.reducers.put_component_preview_then(fence, id, mesh, cb))
    }

    pub fn put_artifact(&self, hash: &str, name: &str, mime: &str, bytes: Vec<u8>, brief: &str) -> Result<()> {
        let fence = self.fence();
        let (hash, name, mime, brief) = (hash.to_string(), name.to_string(), mime.to_string(), brief.to_string());
        self.call("put_artifact", |cb| self.conn.reducers.put_artifact_then(fence, hash, name, mime, bytes, brief, cb))
    }

    pub fn submit_op(&self, id: &str, kind: &str, args_json: String) -> Result<()> {
        let (fence, id, kind) = (self.fence(), id.to_string(), kind.to_string());
        self.call("submit_op", |cb| self.conn.reducers.submit_op_then(fence, id, kind, args_json, String::new(), cb))
    }

    pub fn lock_objects(&self, names: Vec<String>) -> Result<()> {
        let fence = self.fence();
        self.call("lock_objects", |cb| self.conn.reducers.lock_objects_then(fence, names, cb))
    }

    pub fn release_objects(&self, names: Vec<String>) -> Result<()> {
        let fence = self.fence();
        self.call("release_objects", |cb| self.conn.reducers.release_objects_then(fence, names, false, cb))
    }

    pub fn create_invite(&self, token_hash: String, kind: &str, role: &str, ttl_secs: u64) -> Result<()> {
        let (fence, kind, role) = (self.fence(), kind.to_string(), role.to_string());
        self.call("create_invite", |cb| self.conn.reducers.create_invite_then(fence, token_hash, kind, role, ttl_secs, cb))
    }

    pub fn set_brief(&self, id: &str, status: &str, plan_json: &str) -> Result<()> {
        let fence = self.fence();
        let (id, status, plan_json) = (id.to_string(), status.to_string(), plan_json.to_string());
        self.call("set_brief", |cb| self.conn.reducers.set_brief_then(fence, id, status, plan_json, cb))
    }

    pub fn set_brief_stl(&self, id: &str, hash: &str) -> Result<()> {
        let (fence, id, hash) = (self.fence(), id.to_string(), hash.to_string());
        self.call("set_brief_stl", |cb| self.conn.reducers.set_brief_stl_then(fence, id, hash, cb))
    }

    /// (name, kind) of a member of this workspace, from the `v_member` cache.
    pub fn member_name_kind(&self, id: u64) -> (String, String) {
        self.conn
            .db
            .v_member()
            .iter()
            .find(|m| m.id == id)
            .map(|m| (m.name, m.kind))
            .unwrap_or_else(|| ("?".into(), "agent".into()))
    }

    /// Every member row, retired ones included: the host looks here for the agents it started so it can
    /// take them off the team when they are finished with their job.
    pub fn members(&self) -> Vec<MemberRow> {
        self.conn
            .db
            .v_member()
            .iter()
            .map(|m| MemberRow { id: m.id, name: m.name, kind: m.kind, status: m.status })
            .collect()
    }

    /// Take an agent off the team (host only, plus people): its locks are released and any task it still
    /// holds goes back to the team, so a finished one-job agent leaves nothing behind.
    pub fn revoke_member(&self, id: u64) -> Result<()> {
        let fence = self.fence();
        self.call("revoke_member", |cb| self.conn.reducers.revoke_member_then(fence, id, cb))
    }

    /// Who last changed each of these objects, `{name: {id, name, kind}}`, so the style memory can tell a
    /// person correcting an agent's part from an agent tidying its own.
    pub fn last_actors(&self, names: &[String]) -> serde_json::Value {
        let mut out = serde_json::Map::new();
        for o in self.conn.db.v_object().iter().filter(|o| names.contains(&o.name) && o.last_actor != 0) {
            let (name, kind) = self.member_name_kind(o.last_actor);
            out.insert(o.name.clone(), serde_json::json!({"id": o.last_actor, "name": name, "kind": kind}));
        }
        serde_json::Value::Object(out)
    }

    /// The planner's plan JSON (which may also carry the seeded board's datasheet sources).
    pub fn brief_plan(&self, id: &str) -> String {
        self.conn.db.v_brief().iter().find(|b| b.id == id).map(|b| b.plan_json).unwrap_or_default()
    }

    /// Every review round of a brief, oldest first, as JSON for the design index (plan 05).
    pub fn reviews_json(&self, brief: &str) -> serde_json::Value {
        let mut rows: Vec<_> = self.conn.db.v_review().iter().filter(|r| r.brief == brief).collect();
        rows.sort_by_key(|r| r.round);
        serde_json::Value::Array(
            rows.into_iter()
                .map(|r| serde_json::json!({"round": r.round, "passed": r.passed, "findings_json": r.findings_json}))
                .collect(),
        )
    }

    /// The shared Params as `{alias: number}`, for the design index.
    pub fn params_map(&self) -> serde_json::Value {
        serde_json::Value::Object(self.conn.db.v_param().iter().map(|p| (p.alias, serde_json::json!(p.num))).collect())
    }
}

#[cfg(test)]
mod rejoin_tests {
    use super::{says_retired, should_rejoin};

    #[test]
    fn only_a_retired_host_rejoins_by_itself() {
        assert!(should_rejoin(Some("left")));
        assert!(!should_rejoin(Some("taken_over")));
        assert!(!should_rejoin(Some("active")));
        assert!(!should_rejoin(Some("stale")));
        assert!(!should_rejoin(None));
    }

    #[test]
    fn a_retirement_is_recognised_from_the_answers_a_host_gets() {
        // After the network returns, the first reducer answers with one of these instead of a timeout.
        assert!(says_retired("heartbeat: left: This participant left the session; call rejoin"));
        assert!(says_retired("heartbeat: not_member: join with an invite first"));
        // A network that is still away, or any other refusal, is not a retirement.
        assert!(!says_retired("heartbeat: no answer from SpacetimeDB within 30s"));
        assert!(!says_retired("heartbeat: fence_stale: This process was superseded"));
    }
}
