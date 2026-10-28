use crate::tables::*;
use crate::util::*;
use cadswarm_rules as rules;
use serde_json::json;
use spacetimedb::{reducer, Identity, ReducerContext, ScheduleAt, Table};
use std::time::Duration;

#[reducer(init)]
pub fn init(ctx: &ReducerContext) {
    let now = now_ms(ctx);
    ctx.db.workspace().insert(Workspace {
        id: 1,
        name: "cadswarm".into(),
        owner: ctx.sender(),
        bed_x_mm: 180.0,
        bed_y_mm: 180.0,
        bed_z_mm: 180.0,
    });
    ctx.db.member().insert(new_member(1, ctx.sender(), "host", "host", "host", "lead", "", now));
    ctx.db.sweep_schedule().insert(SweepSchedule {
        scheduled_id: 0,
        scheduled_at: ScheduleAt::Interval(Duration::from_secs(5).into()),
    });
}

fn new_member(ws: u64, identity: Identity, name: &str, kind: &str, client: &str, role: &str, endpoint_id: &str, now: u64) -> Member {
    Member {
        id: 0,
        ws,
        identity,
        name: name.into(),
        kind: kind.into(),
        client: client.into(),
        role: role.into(),
        status: "active".into(),
        status_text: String::new(),
        fence: 1,
        last_seen_ms: now,
        joined_ms: now,
        stale_since_ms: 0,
        taken_over_by: 0,
        endpoint_id: endpoint_id.into(),
        online: false,
        context_seq: 0,
    }
}

/// How old `last_seen_ms` may get before a connect or disconnect refreshes it. Heartbeats (every 15 s)
/// keep a working helper fresher than this, so its HTTP requests never write `member` at all.
const SEEN_REFRESH_MS: u64 = 20_000;

/// Every HTTP /call and /sql is a connect and a disconnect, several a second across a team. Presence goes
/// to `live_conn`, which no view reads; `member` (which every view reads) is only written when something a
/// person could see changes: a quiet member is back, or its last sign of life is getting old.
#[reducer(client_connected)]
pub fn on_connect(ctx: &ReducerContext) {
    if let Some(conn) = ctx.connection_id() {
        if ctx.db.live_conn().conn().find(&conn).is_none() {
            ctx.db.live_conn().insert(LiveConn { conn, identity: ctx.sender() });
        }
    }
    if let Some(m) = latest_member(ctx, ctx.sender()) {
        if m.status == "removed" {
            return; // a person took this helper off the team; it stays off until invited again
        }
        let now = now_ms(ctx);
        // Reconnecting is a sign of life: a quiet (stale) member is active again. Members who left
        // or were taken over stay retired; they must rejoin.
        let revived = m.status == "stale";
        if !revived && now.saturating_sub(m.last_seen_ms) < SEEN_REFRESH_MS {
            return;
        }
        let (status, stale_since_ms) = if revived { ("active".to_string(), 0) } else { (m.status.clone(), m.stale_since_ms) };
        let (ws, id, name) = (m.ws, m.id, m.name.clone());
        ctx.db.member().id().update(Member { last_seen_ms: now, status, stale_since_ms, ..m });
        // As heartbeat does: give back the tasks the sweep set aside (it will never see "stale" now).
        if revived {
            resume_tasks(ctx, id);
            emit(ctx, ws, "resumed", id, json!({"name": name}));
        }
    }
}

#[reducer(client_disconnected)]
pub fn on_disconnect(ctx: &ReducerContext) {
    if let Some(conn) = ctx.connection_id() {
        ctx.db.live_conn().conn().delete(&conn);
    }
    if let Some(m) = latest_member(ctx, ctx.sender()) {
        // The pointer is left as it is: every HTTP /call and /sql is a connect and a disconnect,
        // so resting it here would erase an agent's pointer the moment its request ends. Going
        // stale (sweep) and leaving rest it instead.
        // `online` on the row is only set by modules older than live_conn: clear it once, so a WebSocket
        // that outlived the update is counted until it closes and never after.
        let now = now_ms(ctx);
        if m.online || now.saturating_sub(m.last_seen_ms) >= SEEN_REFRESH_MS {
            ctx.db.member().id().update(Member { online: false, last_seen_ms: now, ..m });
        }
    }
}

/// True while this identity holds an open connection (live_conn), or a WebSocket opened before this
/// module recorded connections there.
pub fn is_online(ctx: &ReducerContext, m: &Member) -> bool {
    m.online || ctx.db.live_conn().identity().filter(&m.identity).next().is_some()
}

#[reducer]
pub fn create_invite(ctx: &ReducerContext, fence: u32, token_hash: String, kind: String, role: String, ttl_secs: u64) -> Result<(), String> {
    let m = me(ctx, fence)?;
    if !rules::valid_kind(&kind) || !rules::valid_role(&role) {
        return Err("bad_args: kind must be human|agent|host|reviewer and role lead|member".into());
    }
    if (kind == "host" || kind == "reviewer") && m.kind != "host" {
        return Err("not_allowed: only a host may invite a host or reviewer".into());
    }
    if token_hash.len() != 64 {
        return Err("bad_args: token_hash must be a sha256 hex digest".into());
    }
    let ttl = ttl_secs.clamp(60, rules::INVITE_MAX_TTL_SECS);
    ctx.db.invite().insert(Invite {
        token_hash,
        ws: m.ws,
        kind,
        role,
        created_by: m.id,
        expires_ms: now_ms(ctx) + ttl * 1000,
        used_by: 0,
    });
    Ok(())
}

#[reducer]
pub fn redeem_invite(ctx: &ReducerContext, token: String, name: String, client: String, endpoint_id: String) -> Result<(), String> {
    let hash = rules::hash_token(&token);
    let inv = ctx.db.invite().token_hash().find(&hash).ok_or_else(|| "bad_invite: unknown invite".to_string())?;
    if inv.used_by != 0 {
        return Err("bad_invite: this invite was already used".into());
    }
    if now_ms(ctx) > inv.expires_ms {
        return Err("bad_invite: this invite expired".into());
    }
    if let Some(m) = latest_member(ctx, ctx.sender()) {
        if m.status == "active" || m.status == "stale" {
            return Err("already_member: this identity is already a member; call rejoin".into());
        }
    }
    let member = join_as(ctx, inv.ws, &name, &inv.kind, &client, &inv.role, &endpoint_id)?;
    ctx.db.invite().token_hash().update(Invite { used_by: member.id, ..inv });
    Ok(())
}

/// Renames the calling member: a person, or an AI helper through its own sign-in (FreeCAD holds it in the
/// helper's folder). Not fenced: a name changes nothing in the model, and a helper is renamed while its agent
/// may be running. Open tasks waiting for the old name follow the member.
#[reducer]
pub fn rename_me(ctx: &ReducerContext, name: String) -> Result<(), String> {
    let m = latest_member(ctx, ctx.sender())
        .filter(|m| m.status == "active" || m.status == "stale")
        .ok_or_else(|| "not_member: join with an invite first".to_string())?;
    let name = name.trim().to_string();
    if name == m.name {
        return Ok(());
    }
    rules::check_rename(&name, live_by_name(ctx, m.ws, &name).map(|x| x.id), m.id)?;
    let old = m.name.clone();
    let waiting: Vec<Task> = ctx.db.task().ws().filter(&m.ws)
        .filter(|t| t.status != "done" && t.assignee.eq_ignore_ascii_case(&old))
        .collect();
    let now = now_ms(ctx);
    for t in waiting {
        ctx.db.task().id().update(Task { assignee: name.clone(), updated_ms: now, ..t });
    }
    let (ws, id) = (m.ws, m.id);
    ctx.db.member().id().update(Member { name: name.clone(), ..m });
    emit(ctx, ws, "renamed", id, json!({"name": name, "from": old}));
    Ok(())
}

/// Creates the member row, delivers mail queued for the name, and inherits a stale namesake's work.
fn join_as(ctx: &ReducerContext, ws: u64, name: &str, kind: &str, client: &str, role: &str, endpoint_id: &str) -> Result<Member, String> {
    rules::check_name(name)?;
    let namesake = live_by_name(ctx, ws, name);
    let decision = rules::join_decision(name, namesake.as_ref().map(|m| (m.id, m.status.as_str())))?;
    let member = ctx.db.member().insert(new_member(ws, ctx.sender(), name, kind, client, role, endpoint_id, now_ms(ctx)));
    emit(ctx, ws, "joined", member.id, json!({"name": name, "participant_kind": kind, "client": client, "role": role}));
    let queued = format!("@{}", name.to_lowercase());
    let waiting: Vec<Message> = ctx.db.message().ws().filter(&ws).filter(|m| m.to == queued).collect();
    for m in waiting {
        ctx.db.message().id().update(Message { to: format!("member:{}", member.id), ..m });
    }
    if let rules::Join::Inherit(old) = decision {
        if let Some(target) = ctx.db.member().id().find(&old) {
            take_over_inner(ctx, &member, &target);
        }
    }
    Ok(member)
}

#[reducer]
pub fn rejoin(ctx: &ReducerContext, client: String) -> Result<(), String> {
    let m = latest_member(ctx, ctx.sender()).ok_or_else(|| "not_member: join with an invite first".to_string())?;
    if m.status == "removed" {
        // A person took this helper off the team: it must not come back on its own. A fresh invite
        // creates a new identity, so re-inviting someone always works.
        return Err("removed: a person removed this helper from the team".into());
    }
    if m.status == "left" || m.status == "taken_over" {
        let (ws, name, kind, role, endpoint) = (m.ws, m.name.clone(), m.kind.clone(), m.role.clone(), m.endpoint_id.clone());
        join_as(ctx, ws, &name, &kind, &client, &role, &endpoint)?;
        return Ok(());
    }
    let was_stale = m.status == "stale";
    let (ws, id, name, fence) = (m.ws, m.id, m.name.clone(), m.fence + 1);
    let client = if client.is_empty() { m.client.clone() } else { client };
    ctx.db.member().id().update(Member {
        fence,
        status: "active".into(),
        last_seen_ms: now_ms(ctx),
        stale_since_ms: 0,
        client,
        ..m
    });
    if was_stale {
        resume_tasks(ctx, id);
    }
    emit(ctx, ws, "rejoined", id, json!({"name": name, "fence": fence}));
    Ok(())
}

pub fn resume_tasks(ctx: &ReducerContext, member: u64) {
    let now = now_ms(ctx);
    let ws = ctx.db.member().id().find(&member).map(|m| m.ws).unwrap_or(1);
    let waiting: Vec<Task> = ctx
        .db
        .task()
        .ws()
        .filter(&ws)
        .filter(|t| t.owner == member && t.status == "awaiting_takeover")
        .collect();
    for t in waiting {
        let rev = t.rev + 1;
        ctx.db.task().id().update(Task { status: "claimed".into(), rev, updated_ms: now, ..t });
    }
}

#[reducer]
pub fn heartbeat(ctx: &ReducerContext, fence: u32, status_text: String) -> Result<(), String> {
    let m = me(ctx, fence)?;
    let was_stale = m.status == "stale";
    let (ws, id, name) = (m.ws, m.id, m.name.clone());
    let status_text = if status_text.is_empty() { m.status_text.clone() } else { status_text };
    ctx.db.member().id().update(Member {
        last_seen_ms: now_ms(ctx),
        status: "active".into(),
        stale_since_ms: 0,
        status_text,
        ..m
    });
    if was_stale {
        resume_tasks(ctx, id);
        emit(ctx, ws, "resumed", id, json!({"name": name}));
    }
    Ok(())
}

#[reducer]
pub fn set_context_seq(ctx: &ReducerContext, fence: u32, seq: u64) -> Result<(), String> {
    let m = me(ctx, fence)?;
    ctx.db.member().id().update(Member { context_seq: seq, ..m });
    Ok(())
}

/// Retires a member: releases its locks, returns its claimed work to the team and parks its pointer.
/// `status` is what it becomes — "left" when it walks away, "removed" when a person takes it off the team.
pub fn leave_inner(ctx: &ReducerContext, m: &Member, status: &str) {
    let now = now_ms(ctx);
    let released = locks_of(ctx, m.id);
    for obj in &released {
        ctx.db.cad_lock().key().delete(&lock_key(m.ws, obj));
    }
    let owned: Vec<Task> = ctx
        .db
        .task()
        .ws()
        .filter(&m.ws)
        .filter(|t| t.owner == m.id && (t.status == "claimed" || t.status == "awaiting_takeover"))
        .collect();
    for t in owned {
        let rev = t.rev + 1;
        ctx.db.task().id().update(Task { status: "open".into(), owner: 0, rev, updated_ms: now, ..t });
    }
    ctx.db.member().id().update(Member { status: status.into(), ..m.clone() });
    set_pointer(ctx, m.ws, m.id, "", vec![], "idle", "", "");
    // "removed" reads differently from "left" in the panel: a person took it off the team.
    let kind = if status == "removed" { "removed" } else { "left" };
    emit(ctx, m.ws, kind, m.id, json!({"name": m.name, "released": released}));
}

#[reducer]
pub fn leave(ctx: &ReducerContext, fence: u32) -> Result<(), String> {
    let m = me(ctx, fence)?;
    leave_inner(ctx, &m, "left");
    Ok(())
}

#[reducer]
pub fn revoke_member(ctx: &ReducerContext, fence: u32, member_id: u64) -> Result<(), String> {
    let m = me(ctx, fence)?;
    if m.kind != "human" && m.kind != "host" {
        return Err("not_allowed: only a person or the host may remove a member".into());
    }
    let target = ctx
        .db
        .member()
        .id()
        .find(&member_id)
        .filter(|t| t.ws == m.ws)
        .ok_or_else(|| format!("unknown_participant: No member {member_id}"))?;
    if target.id == m.id {
        return Err("not_allowed: use leave to step away yourself".into());
    }
    if target.kind == "host" {
        return Err("not_allowed: the computer hosting the team can't be removed".into());
    }
    if target.status == "removed" {
        return Ok(()); // already off the team; removing twice is not an error
    }
    leave_inner(ctx, &target, "removed");
    Ok(())
}

pub fn take_over_inner(ctx: &ReducerContext, taker: &Member, target: &Member) -> (Vec<String>, Vec<String>) {
    let (locks, moved_ids) = transfer_work(ctx, taker, target);
    ctx.db.member().id().update(Member { status: "taken_over".into(), taken_over_by: taker.id, ..target.clone() });
    emit(
        ctx,
        target.ws,
        "taken_over",
        taker.id,
        json!({"target": target.name, "taker": taker.name, "locks": locks, "tasks": moved_ids}),
    );
    system_message(
        ctx,
        target.ws,
        "all",
        format!("{} took over {}'s work: locks {:?}, tasks {:?}.", taker.name, target.name, locks, moved_ids),
        "handoff",
        "",
    );
    (locks, moved_ids)
}

/// Move a member's locks and in-progress tasks to another member, leaving their status alone
/// (a takeover then retires the target; a deliberate handoff does not).
pub fn transfer_work(ctx: &ReducerContext, taker: &Member, target: &Member) -> (Vec<String>, Vec<String>) {
    let now = now_ms(ctx);
    let locks = locks_of(ctx, target.id);
    for obj in &locks {
        set_lock(ctx, target.ws, obj, taker.id);
    }
    let moved: Vec<Task> = ctx
        .db
        .task()
        .ws()
        .filter(&target.ws)
        .filter(|t| t.owner == target.id && (t.status == "claimed" || t.status == "awaiting_takeover"))
        .collect();
    let moved_ids: Vec<String> = moved.iter().map(|t| t.id.clone()).collect();
    for t in moved {
        let rev = t.rev + 1;
        ctx.db.task().id().update(Task { owner: taker.id, status: "claimed".into(), rev, updated_ms: now, ..t });
    }
    (locks, moved_ids)
}

#[reducer]
pub fn take_over(ctx: &ReducerContext, fence: u32, target: String, force: bool) -> Result<(), String> {
    let m = me(ctx, fence)?;
    let t = ctx
        .db
        .member()
        .ws()
        .filter(&m.ws)
        .filter(|x| x.name.eq_ignore_ascii_case(&target) && x.status != "left" && x.status != "removed")
        .max_by_key(|x| x.id)
        .ok_or_else(|| format!("unknown_participant: No participant {target:?}"))?;
    rules::check_take_over(m.id, t.id, &t.name, &t.status, &m.kind, force)?;
    take_over_inner(ctx, &m, &t);
    Ok(())
}
