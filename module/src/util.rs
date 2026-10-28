use crate::tables::*;
use cadswarm_rules::Holder;
use serde_json::Value;
use spacetimedb::{Identity, ReducerContext, Table};

pub fn now_ms(ctx: &ReducerContext) -> u64 {
    (ctx.timestamp.to_micros_since_unix_epoch() / 1000) as u64
}

/// The caller's most recent member row (re-joins after a takeover create a new row).
pub fn latest_member(ctx: &ReducerContext, who: Identity) -> Option<Member> {
    ctx.db.member().identity().filter(&who).max_by_key(|m| m.id)
}

/// The acting member, refusing superseded processes (core.py Hub.authenticate).
pub fn me(ctx: &ReducerContext, fence: u32) -> Result<Member, String> {
    let m = latest_member(ctx, ctx.sender())
        .ok_or_else(|| "not_member: join with an invite first".to_string())?;
    match m.status.as_str() {
        "taken_over" => {
            return Err(format!(
                "taken_over: Your work was taken over by {}. Call rejoin to continue as a new participant.",
                name_of(ctx, m.taken_over_by)
            ))
        }
        "left" => return Err("left: This participant left the session; call rejoin".into()),
        "removed" => return Err("removed: a person removed this helper from the team".into()),
        _ => {}
    }
    if fence != m.fence {
        return Err("fence_stale: This process was superseded by a newer one using the same identity; stop acting.".into());
    }
    Ok(m)
}

pub fn require_kind(m: &Member, kind: &str) -> Result<(), String> {
    if m.kind == kind { Ok(()) } else { Err(format!("not_allowed: only a {kind} may do this")) }
}

pub fn name_of(ctx: &ReducerContext, id: u64) -> String {
    if id == 0 {
        return "hub".into();
    }
    ctx.db.member().id().find(&id).map(|m| m.name).unwrap_or_else(|| format!("member {id}"))
}

pub fn live_by_name(ctx: &ReducerContext, ws: u64, name: &str) -> Option<Member> {
    ctx.db
        .member()
        .ws()
        .filter(&ws)
        .filter(|m| (m.status == "active" || m.status == "stale") && m.name.eq_ignore_ascii_case(name))
        .max_by_key(|m| m.id)
}

pub fn lock_key(ws: u64, obj: &str) -> String {
    format!("{ws}:{obj}")
}

pub fn holder(ctx: &ReducerContext, ws: u64, obj: &str) -> Option<Holder> {
    let l = ctx.db.cad_lock().key().find(&lock_key(ws, obj))?;
    let m = ctx.db.member().id().find(&l.holder);
    Some(Holder {
        id: l.holder,
        name: m.as_ref().map(|m| m.name.clone()).unwrap_or_default(),
        status: m.map(|m| m.status).unwrap_or_else(|| "unknown".into()),
    })
}

pub fn locks_of(ctx: &ReducerContext, member: u64) -> Vec<String> {
    let mut v: Vec<String> = ctx.db.cad_lock().holder().filter(&member).map(|l| l.object).collect();
    v.sort();
    v
}

pub fn set_lock(ctx: &ReducerContext, ws: u64, obj: &str, holder: u64) {
    let key = lock_key(ws, obj);
    let row = CadLock { key: key.clone(), ws, object: obj.to_string(), holder, acquired_ms: now_ms(ctx) };
    if ctx.db.cad_lock().key().find(&key).is_some() {
        ctx.db.cad_lock().key().update(row);
    } else {
        ctx.db.cad_lock().insert(row);
    }
}

/// (revision, name of last actor) of an object; (0, "nobody") if it does not exist.
pub fn object_rev(ctx: &ReducerContext, ws: u64, name: &str) -> (u32, String) {
    match ctx.db.cad_object().key().find(&lock_key(ws, name)) {
        Some(o) => (o.rev, name_of(ctx, o.last_actor)),
        None => (0, "nobody".into()),
    }
}

pub fn emit(ctx: &ReducerContext, ws: u64, kind: &str, actor: u64, data: Value) -> u64 {
    let seq = ctx
        .db
        .team_event()
        .insert(TeamEvent { seq: 0, ws, kind: kind.into(), actor, data_json: data.to_string(), at_ms: now_ms(ctx) })
        .seq;
    if ctx.db.event_head().ws().find(&ws).is_some() {
        ctx.db.event_head().ws().update(EventHead { ws, seq });
    } else {
        ctx.db.event_head().insert(EventHead { ws, seq });
    }
    seq
}

pub fn system_message(ctx: &ReducerContext, ws: u64, to: &str, body: String, kind: &str, task_id: &str) {
    // Messages are never deleted, so the row count makes the id unique within this timestamp.
    let id = format!("m_sys_{}_{}", now_ms(ctx), ctx.db.message().count());
    ctx.db.message().insert(Message {
        id,
        ws,
        from: 0,
        to: to.into(),
        kind: kind.into(),
        body,
        task_id: task_id.into(),
        created_ms: now_ms(ctx),
    });
}

/// The exact spot `member` is already pointing at on `object`, or empty. Server-side moves to the
/// same part keep it, so a pointer does not jump back to the part's box on every tool call.
pub fn point_on(ctx: &ReducerContext, member: u64, object: &str) -> Vec<f32> {
    ctx.db.pointer().member().find(&member).filter(|p| p.object == object).map(|p| p.point).unwrap_or_default()
}

/// Server-side pointer moves bump `seq`, so clients can drop out-of-order updates.
pub fn set_pointer(
    ctx: &ReducerContext,
    ws: u64,
    member: u64,
    object: &str,
    point: Vec<f32>,
    mode: &str,
    label: &str,
    op: &str,
) {
    let label: String = label.chars().take(60).collect();
    match ctx.db.pointer().member().find(&member) {
        Some(p) => {
            ctx.db.pointer().member().update(Pointer {
                object: object.into(),
                point,
                mode: mode.into(),
                label,
                op: op.into(),
                seq: p.seq + 1,
                at_ms: now_ms(ctx),
                ..p
            });
        }
        None => {
            ctx.db.pointer().insert(Pointer {
                member,
                ws,
                object: object.into(),
                point,
                mode: mode.into(),
                label,
                op: op.into(),
                seq: 1,
                at_ms: now_ms(ctx),
            });
        }
    }
}
