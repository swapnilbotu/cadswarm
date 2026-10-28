use crate::tables::*;
use spacetimedb::{view, ViewContext};

/// The caller's workspace if they are a live member; views return nothing to anyone else.
fn caller_ws(ctx: &ViewContext) -> Option<u64> {
    ctx.db
        .member()
        .identity()
        .filter(&ctx.sender())
        .filter(|m| m.status == "active" || m.status == "stale")
        .max_by_key(|m| m.id)
        .map(|m| m.ws)
}

#[view(accessor = v_me, public)]
pub fn v_me(ctx: &ViewContext) -> Vec<Member> {
    ctx.db.member().identity().filter(&ctx.sender()).max_by_key(|m| m.id).into_iter().collect()
}

#[view(accessor = v_workspace, public)]
pub fn v_workspace(ctx: &ViewContext) -> Vec<Workspace> {
    caller_ws(ctx).and_then(|ws| ctx.db.workspace().id().find(&ws)).into_iter().collect()
}

/// The caller's newest event seq: pollers read this one row and only read v_event when it moved.
/// A team whose last event predates this module has no head row until its next event.
#[view(accessor = v_event_head, public)]
pub fn v_event_head(ctx: &ViewContext) -> Vec<EventHead> {
    caller_ws(ctx).and_then(|ws| ctx.db.event_head().ws().find(&ws)).into_iter().collect()
}

macro_rules! ws_view {
    ($name:ident, $table:ident, $row:ty) => {
        #[view(accessor = $name, public)]
        pub fn $name(ctx: &ViewContext) -> Vec<$row> {
            match caller_ws(ctx) {
                Some(ws) => ctx.db.$table().ws().filter(&ws).collect(),
                None => Vec::new(),
            }
        }
    };
}

ws_view!(v_member, member, Member);
ws_view!(v_lock, cad_lock, CadLock);
ws_view!(v_object, cad_object, CadObject);
ws_view!(v_param, param, Param);
ws_view!(v_op, op, Op);
ws_view!(v_task, task, Task);
ws_view!(v_message, message, Message);
ws_view!(v_receipt, receipt, Receipt);
ws_view!(v_event, team_event, TeamEvent);
ws_view!(v_activity, activity, Activity);
ws_view!(v_pointer, pointer, Pointer);
ws_view!(v_brief, brief, Brief);
ws_view!(v_review, review, Review);
ws_view!(v_preview, preview, Preview);
ws_view!(v_artifact, artifact, Artifact);
ws_view!(v_artifact_blob, artifact_blob, ArtifactBlob);
ws_view!(v_handoff, handoff, Handoff);
ws_view!(v_component, component, Component);
ws_view!(v_component_preview, component_preview, ComponentPreview);
ws_view!(v_setting, project_setting, ProjectSetting);
ws_view!(v_suggestion, setting_suggestion, SettingSuggestion);
