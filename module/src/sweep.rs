use crate::membership::leave_inner;
use crate::tables::*;
use crate::util::*;
use cadswarm_rules as rules;
use serde_json::json;
use spacetimedb::{reducer, ReducerContext, Table};

/// Mark quiet members stale, free the work of long-gone ones (the reviewer is never freed: the host
/// needs it between rounds), expire invites, prune activity
/// (core.py Hub.sweep). Runs every 5 s from `sweep_schedule`.
#[reducer]
pub fn sweep(ctx: &ReducerContext, _row: SweepSchedule) -> Result<(), String> {
    let now = now_ms(ctx);
    let members: Vec<Member> = ctx.db.member().iter().collect();
    let presence: Vec<rules::Presence> = members
        .iter()
        .map(|m| rules::Presence {
            id: m.id,
            kind: m.kind.clone(),
            status: m.status.clone(),
            last_seen_ms: m.last_seen_ms,
            stale_since_ms: m.stale_since_ms,
            online: crate::membership::is_online(ctx, m),
        })
        .collect();
    let plan = rules::plan_sweep(now, &presence);

    for id in plan.to_stale {
        let Some(m) = ctx.db.member().id().find(&id) else { continue };
        ctx.db.member().id().update(Member { status: "stale".into(), stale_since_ms: now, ..m.clone() });
        let claimed: Vec<Task> =
            ctx.db.task().ws().filter(&m.ws).filter(|t| t.owner == id && t.status == "claimed").collect();
        let titles: Vec<String> = claimed.iter().map(|t| t.title.clone()).collect();
        for t in claimed {
            let rev = t.rev + 1;
            ctx.db.task().id().update(Task { status: "awaiting_takeover".into(), rev, updated_ms: now, ..t });
        }
        let locks = locks_of(ctx, id);
        // A person who armed "if I leave, let an AI continue" gets their handoff opened now,
        // while their pointer still says where they were.
        crate::handoff::open_waiting_for(ctx, &m);
        if let Some(p) = ctx.db.pointer().member().find(&id) {
            set_pointer(ctx, m.ws, id, &p.object, p.point.clone(), "idle", &p.label, "");
        }
        emit(ctx, m.ws, "stale", id, json!({"name": m.name, "locks": locks, "tasks": titles}));
        if !locks.is_empty() || !titles.is_empty() {
            system_message(
                ctx,
                m.ws,
                "all",
                format!(
                    "{} went quiet holding {:?} and tasks {:?}. Its parts stay locked; call take_over('{}') to continue its work.",
                    m.name, locks, titles, m.name
                ),
                "handoff",
                "",
            );
        }
    }
    for id in plan.to_orphan {
        if let Some(m) = ctx.db.member().id().find(&id) {
            leave_inner(ctx, &m, "left");
        }
    }

    let expired: Vec<Invite> = ctx.db.invite().iter().filter(|i| i.used_by == 0 && i.expires_ms < now).collect();
    for i in expired {
        ctx.db.invite().token_hash().delete(&i.token_hash);
    }

    for m in &members {
        let mut rows: Vec<Activity> = ctx.db.activity().member().filter(&m.id).collect();
        if rows.len() > rules::ACTIVITY_KEEP {
            rows.sort_by_key(|a| std::cmp::Reverse(a.id));
            for a in rows.into_iter().skip(rules::ACTIVITY_KEEP) {
                ctx.db.activity().id().delete(&a.id);
            }
        }
    }

    // Parked large op results (see rules::op_result_expired) are dropped after ten minutes.
    let expired_results: Vec<Artifact> = ctx
        .db
        .artifact()
        .iter()
        .filter(|a| rules::op_result_expired(&a.name, a.created_ms, now))
        .collect();
    for a in expired_results {
        ctx.db.artifact_blob().hash().delete(&a.hash);
        ctx.db.artifact().hash().delete(&a.hash);
    }
    Ok(())
}
