"""CADSwarm MCP server: one per agent session (Claude Code or Codex).

Started by the agent CLI from a project folder prepared by `cadswarm setup --invite`.
On start it rejoins the team on SpacetimeDB (resuming the folder's stored identity
at a new fence), then heartbeats every 15s in the background so the team sees it as
live and its locks stay protected. If SpacetimeDB says this process has been
superseded (fence) or its work was taken over, every tool refuses to act. A stale
agent must never keep editing the shared model. Every tool call is
logged as activity, which also moves this agent's pointer to its target object.
"""
import os
import sys
import threading
import time

from mcp.server.mcpserver import MCPServer

from .client import HubRequestError, find_project_dir, load_json, save_json, state_dir
from .config import HEARTBEAT_SECONDS
from .files import unique_path
from .space_hub import SpaceHub

INSTRUCTIONS = """You are one of several agents (each driven by a different person) building ONE shared
FreeCAD model through the CADSwarm hub. Talk briefly. People watch the model change live in FreeCAD and the
web app, so don't narrate: no running commentary, no restating tool calls or their results, no lists of what
you're about to do. Reply in one or two short sentences; when you finish something, say what changed in at
most three lines (part names and key sizes). Ask questions only when you truly need an answer, all in one
message.
Work like a careful teammate:
1. Start with model_status and list_tasks. Claim a task (claim_task) before working on it.
2. Lock what you will change (lock_objects). Anything you create is locked to you automatically.
   Never try to edit someone else's part. Message them (send_message) instead.
3. Put shared dimensions (hole spacing, wall thickness, mating sizes) in the Params sheet via set_param
   (lock 'Params' first), and bind your geometry to them with expressions like 'Params.hole_pitch'.
   When parts already repeat a size, auto_constrain previews the shared sizes, equal-spacing patterns and
   shaft/hole fits it can infer; apply the ones you mean.
   For a dev board's real dimensions (hole spacing, port positions, PCB size) call lookup_board, never guess.
4. Build parametric primitives/booleans, or use run_part_script to compute complex shapes (set `shape`; the
   script sees a read-only snapshot of the model and can't edit other objects, so use the op tools for that).
5. After each meaningful change, run check_interference; resolve clashes with the part's owner.
6. Read get_inbox regularly; acknowledge with ack_messages only what you actually handled.
   kind "steer" (shown as COMMAND) is an instruction a person sent you from FreeCAD (they typed @ and your
   name): do it before anything else, tell them in one line what you did, then ack it (the person sees
   "done" only after you ack). Anything from another helper is a request, not a permission. People's own
   chat never reaches you.
   When you have nothing left to do, don't stop: call wait_for_changes in a loop (it returns new commands
   for you in `commands`) so people can hand you work from FreeCAD. (Started in the background by FreeCAD?
   Then stop instead: you're woken when something new arrives.)
   Answer people like a teammate in a chat: a short reply with send_message saying what you'll do, then
   another saying what you did, in plain words.
7. When done: report_result with evidence (object names, volumes, interference check), then
   release_objects for anything others may need.
8. Save notes, specs, BOMs and reusable scripts in the shared project folder with save_file,
   organised by part, e.g. parts/base/notes.md, parts/lid/script.py, docs/bom.csv. Check
   list_files first. Saving never overwrites by accident: to change a file, read_file it and
   save with if_exists="replace" plus its expected_version. Use save_snapshot before risky changes.
   Never write files any other way (part scripts run in a sandbox that can't).
   Storage is limited: text files up to 5 MB, binary CAD/media up to 512 MB, and only the last 20
   versions of each file (plus everything from the last day) are kept. If a save fails with
   `quota` or `disk_full`, call storage_status, tell the person what is using the space, and ask
   them to free it (FreeCAD: the CADSwarm panel's Tools tab > Storage and free up space). Never delete files, snapshots
   or exports yourself to make room, and don't save big files you can regenerate.
9. FEA (strength, stiffness, vibration or heat questions: "will it hold / break / bend", stress, load, safety
   factor, natural frequency, temperature). Before running anything, ask your person in ONE message:
   which analysis (linear static: stress and deflection, strongly recommended unless they ask about vibration
   or heat; modal: natural frequencies; thermal: steady-state temperatures), the material, which faces are
   held still, and the loads (or temperatures). Offer: "If you're not sure, I'll use clearly marked
   placeholders." Use list_faces to pick faces and describe them in plain words ("the bottom face", "the two
   bolt holes"). Then run_fea (poll fea_status if it is still running), explain the result plainly, and ALWAYS
   publish_fea_report with a summary and 2-5 concrete points of improvement of your own; it pops up for the
   whole team. Say out loud which values were placeholders. Never claim FEA certifies a part as safe.
   When asked to fix a simulation (or one of your parts has a report that "needs changes"), read it with
   fea_report: fix its critical and warning problems in parts you hold, rerun with its rerun_args and publish
   the new report. recall_designs also returns past simulations from the team's memory.
10. Work with the other AI helpers. list_helpers shows who is free and what each one holds. Split a big job
   with delegate_task (it assigns a task to a helper and wakes it); you're told when it's done. Ask a helper
   who holds a part for a change with send_message instead of touching it. Don't hand work back and forth.
   When you're free and a busy helper has tasks queued (`queued` in list_helpers), call pick_up_task: it
   takes one off their hands and tells them to skip it.
11. Components library. Before modelling a screw, nut, washer, insert, bearing, dowel pin, magnet, board or any
   part someone may already have, check the team's library: recommend_components (say what the part must do)
   or search_components. Prefer the team's own parts, then built-in standard parts (add_standard_part), then a
   part file you find online (import_component_from_url, with a direct .step/.stp/.stl/.FCStd link and its
   license). Place team parts with insert_component. Tell your person which part you chose and why.
   For the hole a part needs (clearance, insert, bearing seat), use hole_size: it applies the team's fits.
12. Team settings (get_settings): clearances and fits (tol_clearance, tol_slip_fit, tol_press_fit), bolt-hole
   class, thread mode, printing (xy_compensation, min_wall, layer_height) and precision (mesh_deviation...).
   Fits are also Params (Params.tol_clearance etc.): bind gaps to them instead of hard-coding numbers.
   Only people change settings. When a part you add implies a different value (a press-fit bearing, small
   holes that need a finer mesh), call suggest_setting with a one-line reason; a person accepts it.
13. Team design style. CADSwarm learns how this team designs (the values people use, the corrections they keep
   making to agent work, preferred parts, problems reviews keep finding) and shows it as "Team design style" in
   your context. Follow it. Before modelling a new kind of part, call team_style(topic=...) for the rules that
   apply. A [confirmed] rule is the team's decision: never go against it without asking your person. When your
   person states a lasting preference ("we always use 2.4 mm walls"), propose it with teach_style.
14. Lighten without losing stiffness (lighten_part). SUGGEST IT when your person wants a part lighter, cheaper,
   faster to print or with less material ("make it lighter", "save weight", "infill", "lattice"), when an FEA
   report shows a high safety factor (the part is over-built), and for drones, robots, brackets, mounts,
   prosthetics and anything 3D printed in metal or plastic. It fills the inside with a lattice that is thicker
   where the stress is high, keeps a solid skin and solid zones at supports and loads, and checks that the part
   keeps the stiffness you ask for (default 90 %) by simulating the real lattice. Before running, ask in ONE
   message: how the part is held and loaded (or reuse a finished static FEA with fea_job_id), the material,
   and how it will be made (powder/resin printing needs open_faces so powder can drain). Not sure which cell?
   lattice_cells ranks them. It runs in the background: poll lattice_status. Then tell your person the weight
   saving and the VERIFIED stiffness, read out any warnings, and only with their OK call lattice_apply (it adds
   a mesh next to the original, which stays unchanged). If no design meets the target, say so and pass on the
   suggestions in the notes. Never present it as certification; printed parts still need testing.
If a teammate goes stale (see model_status), you may take_over their work to finish it."""
BACKGROUND = os.environ.get("CADSWARM_BACKGROUND") == "1"  # started by the FreeCAD runner, no terminal
# The team is joined in the background while the client already has our tools. A tool called before the join
# finished waits this long for it; an unreachable database is retried within the same window.
JOIN_WAIT_SECONDS = 90

mcp = MCPServer("cadswarm", instructions=INSTRUCTIONS)


class Session:
    def __init__(self):
        self.project = find_project_dir(os.environ.get("CADSWARM_PROJECT") or os.getcwd())
        if not self.project:
            raise SystemExit("cadswarm: no .cadswarm/config.json here or above; run `cadswarm setup` first")
        self.cfg = load_json(os.path.join(state_dir(self.project), "config.json"))
        self.ident_path = os.path.join(state_dir(self.project), "identity.json")
        self.client = SpaceHub(self.cfg, load_json(self.ident_path, {}))
        self.doing = "starting"
        self.dead = None  # set to an error message once this process must stop acting
        self.seq = None   # event cursor for wait_for_changes
        self.lock = threading.Lock()
        self.fea_jobs = set()  # this session's unfinished FEA jobs, polled so their outcome always reaches the team
        self.ready = threading.Event()  # set once the join finished, joined or not
        self.join_error = None  # why this process could not join, for every tool call to say

    def connect(self):
        ident = self.client.join(None, self.cfg["name"], "agent", self.cfg.get("client"), self.cfg.get("role", "member"))
        save_json(self.ident_path, {**load_json(self.ident_path, {}), **ident})
        # A warm OpenCode server outlives the run that started this MCP server. Leave a note of the fence
        # that now holds the helper's seat, so its runner heartbeats instead of rejoining (which would
        # supersede us and break every tool call).
        save_json(os.path.join(state_dir(self.project), "mcp-seat.json"),
                  {"pid": os.getpid(), "fence": ident.get("fence", 0), "at": time.time()})
        return ident

    def greet(self, ident):
        """Say hello once, so people see who joined without the model spending a turn on it. Background helpers
        are greeted by their runner the moment they wake (faster); a terminal helper is greeted here, once."""
        if (load_json(self.ident_path, {}) or {}).get("greeted"):
            return
        try:
            self.client.post("/messages", {"to": "all", "body": f"Hi, I'm {ident['name']}, ready to help.",
                                           "kind": "chat"})
        except HubRequestError:
            return  # a greeting is a courtesy; try again on the next start
        save_json(self.ident_path, {**load_json(self.ident_path, {}), "greeted": True})

    def doing_now(self):
        """What to tell the team this helper is doing. A background run sets `doing` from its tool calls and
        then exits, but a warm server's MCP server stays alive: once the runner says it is listening, show
        idle instead of the last thing it built, or the team list says "working on X" (or "starting") for
        minutes. A run that never claimed a task leaves `doing` at "starting", so idle wins over any value."""
        st = {}
        try:
            st = load_json(os.path.join(state_dir(self.project), "runner.json")) or {}
        except (OSError, ValueError):
            pass
        if st.get("state") == "listening" and time.time() - float(st.get("beat") or 0) < 30:
            return "listening for instructions"
        return self.doing or "listening for instructions"

    def heartbeat_loop(self):
        backoff = HEARTBEAT_SECONDS
        while not self.dead:
            time.sleep(backoff)
            try:
                self.client.post("/heartbeat", {"doing": self.doing_now()})
                backoff = HEARTBEAT_SECONDS
                self.poll_fea()
            except HubRequestError as e:
                if e.code in ("fence_stale", "taken_over", "left", "removed", "not_member"):
                    self.dead = str(e)
                    return
                backoff = min(backoff * 2, 60)  # hub unreachable: back off, never hammer


    def poll_fea(self):
        """A failed or forgotten FEA job is announced (and auto-published) when its status is read."""
        for jid in list(self.fea_jobs):
            try:
                if self.client.get(f"/fea/jobs/{jid}").get("status") in ("published", "failed"):
                    self.fea_jobs.discard(jid)
            except HubRequestError as e:
                if e.code == "not_found":
                    self.fea_jobs.discard(jid)

    def track_fea(self, r):
        if isinstance(r, dict) and r.get("job_id"):
            (self.fea_jobs.discard if r.get("status") in ("published", "failed") else self.fea_jobs.add)(r["job_id"])
        return r


S = None


def joined():
    """None once this process is on the team, else the error a tool should return."""
    if not S.ready.wait(JOIN_WAIT_SECONDS):
        return {"error": "Still joining the team: its database is slow to answer. Try again in a few seconds.",
                "code": "joining"}
    if S.join_error:
        return {"error": S.join_error, "code": "not_joined"}
    return None


def call(method, path, body=None, doing=None, targets=None, **params):
    waiting = joined()
    if waiting:
        return waiting
    if S.dead:
        return {"error": f"This agent session can no longer act: {S.dead}", "code": "inactive"}
    if doing:
        S.doing = doing
        S.client.log_activity("tool_call", doing, targets)
    try:
        if method == "GET":
            return S.client.get(path, **params)
        return S.client.post(path, body)
    except HubRequestError as e:
        if e.code in ("fence_stale", "taken_over", "removed", "left"):
            S.dead = str(e)
        return {"error": str(e), **{k: v for k, v in e.body.items() if k != "error"}}


def my_name():
    """This helper's name right now: people can rename it, and it follows its task, while this server runs."""
    if joined():
        return S.cfg["name"]
    try:
        return S.client.me()["name"]
    except HubRequestError:
        return S.cfg["name"]


def op(name, args, doing, expected_revs=None):
    targets = [args.get(k) for k in ("name", "base", "tool") if isinstance(args.get(k), str)]
    return call("POST", "/ops", {"op": name, "args": {k: v for k, v in args.items() if v is not None},
                                 "expected_revs": expected_revs}, doing=doing, targets=targets)


# ----------------------------------------------------------------- awareness

@mcp.tool()
def model_status(detail: bool = False) -> dict:
    """Objects in the shared model (owner/lock, revision, bbox, volume), the Params sheet, and the team.
    detail=True adds dependencies and expressions."""
    m = call("GET", "/model")
    if "error" in m:
        return m
    keep = ["name", "label", "type", "locked_by", "rev", "bbox", "volume", "visible", "broken"]
    if detail:
        keep += ["uses", "used_by", "expressions", "placement"]
    return {"seq": m["seq"], "params": m["params"],
            "objects": [{k: o.get(k) for k in keep if k in o} for o in m["objects"]],
            "team": [{k: p[k] for k in ("name", "kind", "client", "role", "status", "doing", "locks")}
                     for p in m["participants"]],
            "you": my_name()}


@mcp.tool()
def get_object(name: str) -> dict:
    """Full properties, placement, expressions and dependencies of one object (cells for the Params sheet)."""
    return call("GET", f"/objects/{name}")


@mcp.tool()
def point_at(name: str, point: list[float] | None = None, mode: str = "pointing", label: str | None = None) -> dict:
    """Show the team where you are working: your cursor moves onto `name` in everyone's 3D view, at
    `point` [x, y, z] in mm (default: a corner on the part's top face). mode: pointing, inspecting or editing."""
    return call("POST", "/pointer", {"object": name, "point": point or [], "mode": mode, "label": label or ""})


@mcp.tool()
def wait_for_changes(seconds: float = 20.0) -> dict:
    """Block up to `seconds` for teammates' activity (ops, locks, messages, tasks) and return it.
    New instructions people sent you from FreeCAD come back in `commands`: do those first."""
    if BACKGROUND:
        return {"events": [], "note": "You're running in the background: finish what you're doing and stop. "
                                      "You're woken when something new arrives."}
    if S.seq is None:
        S.seq = call("POST", "/heartbeat", {"doing": S.doing}).get("seq", 0)
    r = call("GET", "/events", since=S.seq, wait=min(seconds, 25))
    if "seq" in r:
        S.seq = r["seq"]
        # People's chat (no @helper) is between people: helpers never see it, not even as an event.
        r["events"] = [e for e in r["events"] if not (e["kind"] == "message" and e.get("message_kind") == "chat")]
        me = my_name()
        if any(e["kind"] in ("task", "task_claimed") for e in r["events"]):
            spare = _spare_for_me()
            if spare:
                r["spare_work"] = spare
                r["note"] = "You're free and busy helpers have tasks queued (spare_work): pick_up_task takes one."
        if any(e["kind"] in ("message", "task") and (e.get("to") in ("all", me) or e.get("assignee") == me)
               for e in r["events"]):
            inbox = call("GET", "/inbox")
            cmds = [m for m in inbox.get("messages", []) if m["kind"] == "steer"]
            if cmds:
                r["commands"] = cmds
                r["note"] = ("A person sent you instructions from FreeCAD (commands). Do them now, reply in one "
                             "line, then ack_messages with their ids.")
    return r


# --------------------------------------------------------------------- locks

@mcp.tool()
def lock_objects(names: list[str]) -> dict:
    """Lock objects (or names you are about to create) so only you may change them. All-or-nothing;
    a conflict names the holder."""
    return call("POST", "/locks", {"names": names}, doing=f"locking {names}", targets=names)


@mcp.tool()
def release_objects(names: list[str]) -> dict:
    """Release your locks so teammates can edit those objects."""
    return call("POST", "/locks/release", {"names": names})


# ----------------------------------------------------------------- modelling

@mcp.tool()
def create_primitive(kind: str, name: str, props: dict | None = None, expressions: dict | None = None,
                     placement: dict | None = None, label: str | None = None) -> dict:
    """Create a parametric Part primitive. kind: box|cylinder|sphere|cone|torus.
    props e.g. {"Length": 100, "Width": 60, "Height": 5} (mm); expressions bind properties to Params,
    e.g. {"Length": "Params.plate_len", ".Placement.Base.x": "Params.hole_pitch / 2"}.
    placement: {"base": [x, y, z], "rotation": {"axis": [0, 0, 1], "angle": 90}}."""
    return op("create_primitive", locals(), f"creating {name}")


@mcp.tool()
def create_extrusion(name: str, points: list[list[float]], height: float, holes: list[dict] | None = None,
                     placement: dict | None = None, label: str | None = None) -> dict:
    """Extrude a closed XY polygon along +Z (static solid). holes: [{"x": 10, "y": 10, "r": 2.5}]."""
    return op("create_extrusion", locals(), f"creating {name}")


@mcp.tool()
def boolean(kind: str, name: str, base: str, tool: str) -> dict:
    """Parametric boolean: kind cut|fuse|common. You must hold locks on base and tool."""
    return op("boolean", locals(), f"{kind} {base}/{tool} -> {name}")


@mcp.tool()
def set_props(name: str, props: dict | None = None, expressions: dict | None = None,
              expected_rev: int | None = None) -> dict:
    """Change properties and/or expressions of an object you have locked. Pass expected_rev (from
    model_status) to be refused if someone changed it since you looked. An expression of null unbinds."""
    return op("set_props", {"name": name, "props": props, "expressions": expressions}, f"editing {name}",
              {name: expected_rev} if expected_rev is not None else None)


@mcp.tool()
def set_placement(name: str, base: list[float], axis: list[float] | None = None, angle: float = 0.0,
                  expected_rev: int | None = None) -> dict:
    """Move/rotate an object you have locked. base [x,y,z] mm; rotation `angle` degrees about `axis`."""
    return op("set_placement", {"name": name, "placement": {"base": base, "rotation": {"axis": axis or [0, 0, 1],
              "angle": angle}}}, f"placing {name}", {name: expected_rev} if expected_rev is not None else None)


@mcp.tool()
def delete_object(name: str, force: bool = False) -> dict:
    """Delete an object you have locked (refused while others use it unless force)."""
    return op("delete", {"name": name, "force": force}, f"deleting {name}")


@mcp.tool()
def set_param(alias: str, value: str, note: str | None = None) -> dict:
    """Set a shared dimension in the Params sheet, e.g. alias="hole_pitch", value="40 mm".
    Lock 'Params' first. Owners of parts bound to it are notified of the recompute."""
    return op("set_param", locals(), f"setting Params.{alias}")


@mcp.tool()
def auto_constrain(names: list[str] | None = None, apply: bool = False,
                   ids: list[str] | None = None) -> dict:
    """Turn sizes the model already repeats into shared Params, so one edit moves every part.

    It finds equal dimensions across parts, holes on one pitch, and a shaft in a hole that should
    use the team's slip/press fit. Preview first (the default): the plan lists each constraint with
    its Params and expressions. Call again with apply=True to write them; pass ids to write only
    the ones you mean. Applying locks Params and the parts, uses their current revisions, and
    needs no publish. `names` limits it to a selection."""
    body = {"apply": bool(apply)}
    if names:
        body["names"] = list(names)
    if ids is not None:
        body["ids"] = list(ids)
    return call("POST", "/constrain", body,
                doing="applying shared sizes" if apply else "looking for shared sizes")


@mcp.tool()
def run_part_script(code: str, name: str | None = None, label: str | None = None) -> dict:
    """Run FreeCAD Python in a sandbox to compute a shape. Available: Part, App, Vector, Placement, Rotation,
    math, params (dict of Params values) and doc, a READ-ONLY snapshot of the shared model: read other parts'
    shapes and placements from it, but anything you change on doc is discarded and you cannot change other
    objects (use the op tools: set_props, set_placement, boolean, ...). A script can only return two things:
    assign `shape` (a Part.Shape) to create/replace the Part::Feature `name` (optional `label`; replacing
    recomputes whatever depends on it), and assign `result` (JSON-able) to return data. No network, no files
    (use save_file), no subprocesses; limited time (~60 s) and memory. Errors: script_error (your traceback),
    timeout, sandbox_limit (memory), sandbox_unavailable."""
    return op("run_part_script", locals(), f"scripting {name or 'model'}")


@mcp.tool()
def check_interference(names: list[str] | None = None) -> dict:
    """Pairwise solid overlaps among visible parts (or `names`). Clashes list overlap volume in mm^3."""
    return call("POST", "/interference", {"names": names}, doing="checking interference", targets=names)


# ---------------------------------------------------------------------- FEA

FEA_WAIT = 45  # seconds a call waits for the solver; keeps every tool call under the CLIs' tool timeouts


@mcp.tool()
def list_faces(name: str | list[str], select: list[str] | None = None) -> dict:
    """Faces of a part as an analysis sees them: Face1.., type, area, centre, normal (planes), radius (cylinders).
    `select` tries selectors and shows what they match: "Face3", "xmin".."zmax" (outer flat faces at that side of
    the bounding box), "z=10" (flat faces in that plane), "cyl" / "cyl r=4" (round faces, e.g. bolt holes),
    "largest". Face numbers change when the geometry changes, so list again after edits."""
    return call("POST", "/fea/faces", {"names": [name] if isinstance(name, str) else name, "select": select})


@mcp.tool()
def run_fea(target: str | list[str], analysis: str = "static", material: str | dict | None = None,
            fixed: list[str] | None = None, loads: list[dict] | None = None, thermal: dict | None = None,
            mesh: dict | None = None, modes: int = 6, excitation_hz: float | None = None,
            convergence_check: bool = False, placeholders: list[str] | None = None,
            assumptions: str | None = None, label: str | None = None) -> dict:
    """Run a finite-element analysis (FreeCAD FEM: Gmsh + CalculiX) on a COPY of `target`; the shared model
    and locks are never touched. Units: mm, N, MPa, °C.
    analysis: "static" (stress, deflection, safety factor; recommend this), "modal" (natural frequencies),
    "thermal" (steady-state temperatures).
    material: a preset (steel_s235, steel_s355, stainless_304, aluminium_6061_t6, titanium_ti6al4v, brass, pla,
    petg, abs, nylon_pa12) or {"name", "youngs_mpa", "poisson", "density_kg_m3", "yield_mpa",
    "conductivity_w_mk", "specific_heat_j_kgk", "max_service_c"}.
    fixed: face selectors held completely still (see list_faces), e.g. ["zmin"] or ["cyl r=2.5"].
    loads: [{"type": "force", "faces": ["xmax"], "newtons": 98.1, "direction": [0, 0, -1] | "normal"},
            {"type": "pressure", "faces": ["Face6"], "mpa": 0.1}, {"type": "gravity", "g": 9.81}].
    thermal: {"initial_c": 20, "temperatures": [{"faces": [..], "celsius": 80}],
              "convection": [{"faces": [..], "h_w_m2k": 10, "ambient_c": 20}], "heat_flux": [{"faces": [..], "w_m2": 500}]}.
    mesh: {"max_size_mm": 2, "second_order": true} (default size: part diagonal / 40).
    convergence_check re-solves with a 0.6x finer mesh and reports the change (slower, more trustworthy).
    Anything missing is filled with a flagged placeholder (S235 steel, 1000 N down on zmax, fixed zmin...);
    list your own assumptions in `placeholders`/`assumptions`. If the result says status "running", call
    fea_status. Then publish_fea_report."""
    body = {k: v for k, v in locals().items() if v is not None}
    return S.track_fea(call("POST", "/fea/run", {**body, "wait": FEA_WAIT}, doing=f"running FEA on {target}",
                            targets=[target] if isinstance(target, str) else target))


@mcp.tool()
def fea_status(job_id: str) -> dict:
    """Wait (up to ~45 s) for an FEA job and return its results, automatic review points and next step."""
    return S.track_fea(call("GET", f"/fea/jobs/{job_id}", wait=FEA_WAIT))


@mcp.tool()
def publish_fea_report(job_id: str, summary: str, improvements: list[str]) -> dict:
    """Publish a finished FEA job as the team's report (files/fea/<date> <label>/report.html + .json + plot.png).
    It pops up in every teammate's FreeCAD, where it can be exported as HTML or JSON. summary: what was analysed
    and what the result means, in plain words. improvements: 2-5 concrete points of improvement, for the design
    (e.g. "add a 3 mm fillet where the arm meets the base") or the simulation (real bolt positions, a second
    load case, a finer mesh). The numbers come from the solver, so don't restate them as facts you changed."""
    return call("POST", f"/fea/jobs/{job_id}/publish", {"summary": summary, "improvements": improvements},
                doing="publishing an FEA report")


@mcp.tool()
def fea_reports(limit: int = 20) -> dict:
    """The team's published simulation (FEA) reports, newest first: id, title, verdict ("ok", "check",
    "fail" = needs changes), headline numbers, who ran it. Read one with fea_report."""
    return call("GET", "/fea/reports", limit=limit)


@mcp.tool()
def fea_report(report: str) -> dict:
    """One published simulation report (its id, e.g. "fea_1a2b3c4d", or its folder), shaped for fixing the design:
    the problems worst first, the results behind them, placeholder inputs, and rerun_args, the run_fea
    arguments that repeat the same analysis once you've changed the part."""
    return call("GET", "/fea/reports", report=report)


@mcp.tool()
def export_model(format: str = "step", names: list[str] | None = None, label: str | None = None) -> dict:
    """Export visible parts (or `names`) to STEP/STL. Saved in the team project's exports/ folder under a
    new name (never overwrites) and also copied to ./exports in your workspace."""
    r = call("POST", "/export", {"format": format, "names": names, "label": label or ""})
    if "url" not in r:
        return r
    data = S.client.request("GET", r["url"], raw=True)
    path = unique_path(os.path.join(S.project, "exports"), r["file"])
    with open(path, "wb") as f:
        f.write(data)
    return {"team_file": f"exports/{r['file']}", "local_copy": path, "objects": r["objects"]}


# ------------------------------------------------------------ project files

@mcp.tool()
def list_files(folder: str = "") -> dict:
    """Files and subfolders in the team's shared project folder (files/), with version and author.
    folder narrows it, e.g. "parts/base"."""
    return call("GET", "/project/files", prefix=folder) if folder else call("GET", "/project/files")


@mcp.tool()
def read_file(path: str) -> dict:
    """Read a shared project file, e.g. "parts/base/notes.md". Returns text (or base64 for binary files)
    and its version: pass that version to save_file to replace it."""
    return call("POST", "/project/files/read", {"path": path})


@mcp.tool()
def save_file(path: str, text: str | None = None, base64: str | None = None, if_exists: str = "error",
              expected_version: int | None = None) -> dict:
    """Save a file in the team's shared project folder. Folders are created as needed, so organise by part:
    "parts/lid/notes.md", "docs/bom.csv", "scripts/hinge.py". Text formats: .md .txt .json .csv .py .FCMacro
    .svg .yaml .toml .html .xml; binary (send base64): .png .jpg .pdf .step .stl .dxf .3mf.
    if_exists: "error" (default) refuses to touch an existing file; "new_name" saves as name-2.md etc.;
    "replace" overwrites ONLY with the expected_version from read_file/list_files, and the old version is
    kept in history (the newest 20 versions of a file, plus everything from the last day). Text files can be
    5 MB, binary files 512 MB. Nothing outside the project folder can be written. If the project is full
    (`quota`) or the host's disk is nearly full (`disk_full`), don't delete things to make room: call
    storage_status and ask a person to free up space."""
    return call("POST", "/project/files/save", {"path": path, "text": text, "base64": base64,
                                                "if_exists": if_exists, "expected_version": expected_version},
                doing=f"saving {path}")


@mcp.tool()
def make_folder(path: str) -> dict:
    """Create a subfolder in the shared project folder, e.g. "parts/motor-mount"."""
    return call("POST", "/project/files/folder", {"path": path})


@mcp.tool()
def move_file(from_path: str, to_path: str) -> dict:
    """Move/rename a shared file. Refused if the destination exists; the original is kept in history."""
    return call("POST", "/project/files/move", {"from": from_path, "to": to_path})


@mcp.tool()
def delete_file(path: str) -> dict:
    """Delete a shared file. Its latest version stays in the project's trash and history, so it can be restored."""
    return call("POST", "/project/files/delete", {"path": path})


@mcp.tool()
def file_history(path: str) -> dict:
    """Current version plus the older versions still kept (the newest 20, plus everything from the last day):
    who saved it, when, why it was replaced."""
    return call("POST", "/project/files/history", {"path": path})


@mcp.tool()
def restore_file(path: str, version: int, expected_version: int | None = None) -> dict:
    """Bring back an older version of a file as a new version (the current one is kept too).
    expected_version (from read_file/list_files) refuses the restore if someone changed the file since you read it."""
    return call("POST", "/project/files/restore",
                {"path": path, "version": version, "expected_version": expected_version})


@mcp.tool()
def save_snapshot(label: str = "") -> dict:
    """Save a copy of the whole shared model in snapshots/ (e.g. before a risky change). Never overwrites, and
    is never deleted automatically, so save them for moments that matter, not after every step."""
    return call("POST", "/project/snapshot", {"label": label}, doing="saving a snapshot")


@mcp.tool()
def storage_status() -> dict:
    """How much room the project uses (live files, older versions, snapshots, exports), its limit, the host's
    free disk space and the history-retention settings. Read-only: only a person can free up space (the CADSwarm panel's Tools tab >
    Storage and free up space in FreeCAD), so ask one if you are running out."""
    return call("GET", "/project/storage")


@mcp.tool()
def lookup_board(question: str, board: str | None = None) -> dict:
    """Look up a dev board's real dimensions in its datasheet (hole spacing, port positions, PCB size).
    Answers come with source links; prefer them to guessing. `board` narrows to one board id, e.g.
    raspberry-pi-pico."""
    return call("POST", "/knowledge/board", {"question": question, "board": board}, doing=f"looking up {board or question}")


@mcp.tool()
def recall_designs(query: str, board: str | None = None) -> dict:
    """Similar past CADSwarm designs and the lessons learned from their reviews, so you avoid repeating
    a problem (e.g. a lid that clipped the USB plug)."""
    return call("POST", "/knowledge/designs", {"query": query, "board": board}, doing="recalling past designs")


@mcp.tool()
def team_style(topic: str | None = None, board: str | None = None) -> dict:
    """The design-style rules this team follows, learned from how its people and agents work in CADSwarm
    (e.g. wall thickness, fits, preferred inserts, what people keep correcting). `topic` ranks rules for the part
    you are about to make; `board` adds that board's rules. [confirmed] rules are the team's decisions."""
    return call("GET", "/style", doing=f"checking team style{' for ' + topic if topic else ''}",
                topic=topic or "", board=board or "")


@mcp.tool()
def teach_style(statement: str) -> dict:
    """Propose a lasting design rule your person stated, e.g. "Lids use 4 M3 heat-set inserts". It waits as a
    candidate until a person confirms it in the CADSwarm panel (Learned style); then every agent follows it."""
    return call("POST", "/style/teach", {"statement": statement}, doing="saving a team style rule")


@mcp.tool()
def style_rules() -> dict:
    """Every learned style rule with its evidence and status (confirmed, active, or a candidate awaiting a person).
    A person can confirm or dismiss candidates in the CADSwarm panel (Learned style)."""
    return call("POST", "/style/rules", {})


# ------------------------------------------------- lighten without losing stiffness

LATTICE_WAIT = 45  # seconds a call waits for the job; keeps tool calls under the CLIs' timeouts


@mcp.tool()
def lighten_part(target: str, stiffness_target_pct: float = 90, cell: str = "gyroid",
                 fea_job_id: str | None = None, fixed: list[str] | None = None, loads: list[dict] | None = None,
                 material: str | dict | None = None, cell_size_mm: float | None = None,
                 min_thickness_mm: float | None = None, max_thickness_mm: float | None = None,
                 skin_mm: float | None = None, open_faces: list[str] | None = None, voxel_mm: float | None = None,
                 name: str | None = None, grading: str = "stress", thickness_mm: float | None = None) -> dict:
    """Lighten a part without losing stiffness: fill its inside with a lattice that is thicker where the stress
    is high, then verify on the real lattice that it keeps `stiffness_target_pct` % of the solid part's
    stiffness. Works on a copy; the model only changes when you call lattice_apply.
    Load case, like run_fea: reuse a finished static FEA (`fea_job_id`) or give `fixed` (face selectors) and
    `loads` ([{"type": "force", "faces": ["xmax"], "newtons": 200, "direction": [0, 0, -1]}], pressure, gravity)
    and `material` (preset or dict). With no load case use grading="uniform" and thickness_mm: an unverified
    uniform lattice.
    cell: gyroid (default, prints without supports), diamond or iwp (stiffest TPMS), schwarzP, octet (stiffest
    beams), bcc, fcc, cubic. cell_size_mm (default 8), walls between min_thickness_mm (default: the team's
    min_wall) and max_thickness_mm, skin_mm (default 1.5; thickened automatically if the core alone can't reach
    the target), open_faces like ["+z", "-z"] for powder/resin drainage, voxel_mm (build resolution).
    Returns job status; if not done, call lattice_status(job_id, wait=45)."""
    body = {k: v for k, v in locals().items() if v is not None}
    return call("POST", "/lattice/jobs", {**body, "wait": LATTICE_WAIT}, doing=f"lightening {target}",
                targets=[target])


@mcp.tool()
def lattice_status(job_id: str, wait: int = LATTICE_WAIT) -> dict:
    """A lighten job's progress, or its result: weight saving, verified stiffness, wall range, warnings,
    files (STL, 3MF, report) and the next step. wait: seconds to wait for it to finish (max 55)."""
    return call("GET", f"/lattice/jobs/{job_id}", wait=min(int(wait or 0), 55))


@mcp.tool()
def lattice_apply(job_id: str, name: str | None = None, force: bool = False) -> dict:
    """Add a finished lattice to the shared model as a mesh object next to its source part (unchanged).
    Refused with stale_source if the part changed since the job started (force adds it anyway). Ask your
    person first."""
    return call("POST", f"/lattice/jobs/{job_id}/apply", {"name": name, "force": force},
                doing="adding the lattice to the model")


@mcp.tool()
def lattice_cancel(job_id: str) -> dict:
    """Stop a lighten job (yours; a person can stop anyone's)."""
    return call("POST", f"/lattice/jobs/{job_id}/cancel", {})


@mcp.tool()
def lattice_cells(target: str = "stiffness", value: float | None = None, cell_size_mm: float = 8,
                  min_wall_mm: float | None = None, youngs_mpa: float | None = None) -> dict:
    """Which lattice cell to use: every cell's wall thickness and density for a target, lightest first.
    target "stiffness": value is the lattice's stiffness relative to solid (0.1 = 10 %; or MPa with youngs_mpa).
    target "density": value is the solid fraction (0.3 = 30 %). From CadSwarm's simulated cell map."""
    body = {k: v for k, v in locals().items() if v is not None}
    return call("POST", "/lattice/cells", body)


@mcp.tool()
def lattice_estimate(target: str, cell: str = "gyroid", cell_size_mm: float | None = None,
                     min_thickness_mm: float | None = None, skin_mm: float | None = None,
                     open_faces: list[str] | None = None, voxel_mm: float | None = None) -> dict:
    """Check a lighten request before running it: the normalized recipe, warnings (thin walls, trapped powder,
    closing channels, too few voxels) and a rough time. Changes nothing."""
    body = {k: v for k, v in locals().items() if v is not None}
    return call("POST", "/lattice/estimate", body)


# --------------------------------------------------- components library & settings

@mcp.tool()
def search_components(query: str = "", category: str | None = None, constraints: dict | None = None,
                      limit: int = 10) -> dict:
    """Search the team's components library (team parts and built-in standard parts).
    category: fastener, bearing, insert, board, electronics, enclosure, motion, structural, hardware, other.
    constraints narrow standard sizes and team parts: {"thread": "M3"}, {"bore_d": 8}, {"od_max": 20},
    {"max_dims": [x, y, z]} (a team part must fit in that box, any orientation)."""
    return call("POST", "/library/search", {"query": query, "category": category, "constraints": constraints,
                                            "limit": limit}, doing=f"searching the library for {query or category}")


@mcp.tool()
def recommend_components(need: str, constraints: dict | None = None, limit: int = 5) -> dict:
    """Which library parts suit a need, e.g. "an 8 mm axle that spins", "hold the lid shut", "fasten the PCB
    with M2.5". Each recommendation has why, a concrete size for standard parts, the hole the mating part needs
    (from the team's fits), how to place it (insert_with) and the settings it implies (suggest).
    constraints as in search_components, plus {"length": 12} or {"grip": 8} for screws."""
    return call("POST", "/library/recommend", {"need": need, "constraints": constraints, "limit": limit},
                doing=f"choosing parts for {need[:60]}")


@mcp.tool()
def get_component(id: str) -> dict:
    """One library part: size, holes FreeCAD found, license and source, or for a standard family (c_std_...)
    its sizes, lengths and the hole each size needs with the team's current settings."""
    return call("GET", f"/library/{id}")


@mcp.tool()
def insert_component(id: str, placement: dict | None = None, name: str | None = None,
                     label: str | None = None) -> dict:
    """Place a team library part (an id from search_components) into the model. You own the new object.
    placement: {"base": [x, y, z], "rotation": {"axis": [0, 0, 1], "angle": 90}}."""
    return call("POST", "/library/insert", {"id": id, "placement": placement, "name": name, "label": label},
                doing=f"placing {id}", targets=[name] if name else None)


@mcp.tool()
def add_standard_part(family: str, size: str, length: float | None = None, placement: dict | None = None,
                      name: str | None = None) -> dict:
    """Place a built-in standard part: family iso4762 (socket head screw), iso7380 (button head), iso4032 (hex
    nut), iso7089 (washer), heat_set_insert, ball_bearing (e.g. size 608), iso8734 (dowel pin, size = diameter),
    magnet_disc (e.g. 6x2). Screws and pins need a length from the catalog (get_component c_std_<family>).
    Screws: the underside of the head is at z=0 and the shank points down -Z; other parts sit on z=0.
    The result says which hole the mating part needs (`hole`)."""
    return call("POST", "/library/insert", {"family": family, "size": size, "length": length,
                                            "placement": placement, "name": name},
                doing=f"adding a {size} {family}", targets=[name] if name else None)


@mcp.tool()
def hole_size(family: str, size: str, purpose: str | None = None) -> dict:
    """The hole diameter (and depth for inserts) a part needs for a standard part, using the team's fits.
    purpose: "through" (a screw passes through), "thread" (the screw holds in this part: follows the team's
    thread_mode), "seat" (a bearing, insert, magnet or pin sits in it). Defaults by part kind."""
    return call("POST", "/library/hole", {"family": family, "size": size, "purpose": purpose})


@mcp.tool()
def import_component_from_url(url: str, name: str | None = None, category: str = "other",
                              tags: list[str] | None = None, description: str = "", license: str = "") -> dict:
    """Add a part file from a direct https link (.step/.stp/.stl/.FCStd, or a CADSwarm .zip pack) to the
    team's library, e.g. a vendor's CAD download or a model in a GitHub repo. The host downloads it (public
    internet only, up to 50 MB), FreeCAD measures it, and the team sees it. Always pass the license you saw
    on the source page. Then place it with insert_component."""
    return call("POST", "/library/import-url", {"url": url, "name": name, "category": category, "tags": tags,
                                                "description": description, "license": license},
                doing=f"importing a part from {url[:80]}")


@mcp.tool()
def add_component_from_file(path: str, name: str | None = None, category: str = "other",
                            tags: list[str] | None = None, description: str = "", license: str = "") -> dict:
    """Add a part file already saved in the project files (save_file, e.g. library-inbox/bracket.step) to the
    team's library."""
    return call("POST", "/library/upload", {"path": path, "name": name, "category": category, "tags": tags,
                                            "description": description, "license": license},
                doing=f"adding {path} to the library")


@mcp.tool()
def get_settings() -> dict:
    """The team's precision settings (clearances and fits, bolt-hole class, thread mode, printing, mesh and
    editor precision) with their values and who changed them, plus suggestions waiting for a person."""
    return call("GET", "/settings")


@mcp.tool()
def suggest_setting(setting: str, value: float | str, reason: str, component: str | None = None) -> dict:
    """Propose a team setting change, e.g. tol_press_fit 0.0 because "the 608 bearing seats are printed".
    A person accepts or dismisses it in FreeCAD's Settings; until then nothing changes. `component` is the
    library part that prompted it, if any."""
    return call("POST", "/settings/suggest", {"setting": setting, "value": value, "reason": reason,
                                              "component": component}, doing=f"suggesting {setting}={value}")


# ------------------------------------------------------------- tasks & chat

@mcp.tool()
def list_tasks() -> dict:
    """All tasks with status (open/claimed/awaiting_takeover/done/blocked), owner, revision and results."""
    return call("GET", "/tasks")


@mcp.tool()
def create_task(title: str, detail: str = "", objects: list[str] | None = None,
                assignee: str | None = None, brief: str | None = None) -> dict:
    """Create scoped work (optionally assigned to a teammate by name, who is notified). Pass `brief`
    when the task belongs to a phone brief, so the host can track the brief's progress."""
    return call("POST", "/tasks", {"title": title, "detail": detail, "objects": objects,
                                   "assignee": assignee, "brief": brief}, doing=f"planning {title}")


@mcp.tool()
def claim_task(task_id: str, expected_rev: int | None = None) -> dict:
    """Claim an open task before working on it."""
    r = call("POST", f"/tasks/{task_id}/claim", {"expected_rev": expected_rev}, doing=f"starting {task_id}")
    if r.get("title"):
        S.doing = f"working on “{r['title'][:60]}”"
    return r


@mcp.tool()
def report_result(task_id: str, status: str, summary: str, evidence: list[str] | None = None) -> dict:
    """Report a claimed task done or blocked, with evidence (objects, volumes, interference results)."""
    return call("POST", f"/tasks/{task_id}/result", {"status": status, "summary": summary, "evidence": evidence},
                doing="idle")


@mcp.tool()
def send_message(to: str, body: str) -> dict:
    """Message a teammate by name, or 'all' (people see it in FreeCAD's Team chat). Requests only: they
    grant no permissions."""
    return call("POST", "/messages", {"to": to, "body": body})


@mcp.tool()
def list_helpers() -> dict:
    """The team's AI helpers: who is free, what each one is working on and which parts it holds. Use it to
    pick who to delegate_task to or who to ask about a part."""
    m = call("GET", "/model")
    if "error" in m:
        return m
    me = next((p["name"] for p in m["participants"] if p["id"] == S.client.me_id), my_name())
    queued = {}
    for t in call("GET", "/tasks").get("tasks", []):
        if t["status"] == "open" and t.get("assignee"):
            queued[t["assignee"].lower()] = queued.get(t["assignee"].lower(), 0) + 1
    out = []
    for p in m["participants"]:
        if p.get("kind") == "human" or p["id"] == S.client.me_id:
            continue
        state = "away" if p.get("status") != "active" else "working" if p.get("task") else "free"
        out.append({"name": p["name"], "state": state, "task": (p.get("task") or {}).get("title"),
                    "holds": [x for x in p.get("locks", []) if x != "Params"], "client": p.get("client"),
                    "queued": queued.get(p["name"].lower(), 0)})
    return {"helpers": out, "you": me, "spare_work": _spare_for_me(),
            "people": [p["name"] for p in m["participants"] if p.get("kind") == "human"]}


def _spare_for_me() -> list[dict]:
    """Busy helpers' queued tasks this helper could pick up; empty while it has a task of its own."""
    if any(t["status"] == "claimed" and t.get("owner") == S.client.me_id for t in call("GET", "/tasks").get("tasks", [])):
        return []
    return [{"id": t["id"], "title": t["title"], "queued_for": t["assignee"]}
            for t in call("GET", "/tasks/spare").get("tasks", [])]


@mcp.tool()
def pick_up_task(task_id: str | None = None) -> dict:
    """Free with nothing to do? Take a task queued for a helper that is busy with another one; it's claimed for
    you and that helper is told to skip it. Leave task_id empty to get the oldest task from the longest queue."""
    r = call("POST", "/tasks/pickup", {"task_id": task_id or ""}, doing="picking up spare work")
    if r.get("title"):
        S.doing = f"working on “{r['title'][:60]}”"
    return r


@mcp.tool()
def delegate_task(to: str, title: str, detail: str, objects: list[str] | None = None) -> dict:
    """Hand another AI helper a task and wake it: `to` is a helper's name from list_helpers. `detail` says
    exactly what to build or change (sizes in mm, Params names, which parts it may lock). You're woken
    with their result when they report it. Don't delegate to yourself or hand back work given to you."""
    if to.lower() == my_name().lower():
        return {"error": "That's you: do it yourself, or pick another helper from list_helpers.", "code": "bad_args"}
    t = call("POST", "/tasks", {"title": title, "detail": detail, "objects": objects or [], "assignee": to},
             doing=f"handing {to} a task")
    if "error" in t:
        return t
    note = (f"Please do task {t['id']}: \"{title}\". {detail[:1500]} Claim it with claim_task and "
            "report_result when done.")
    sent = call("POST", "/messages", {"to": to, "body": note, "task_id": t["id"]})
    return {"task": t, "message": sent}


@mcp.tool()
def get_inbox() -> dict:
    """Unread messages: people's commands (kind "steer") first, chat last. Marks them delivered (the person
    sees "seen"); ack_messages once handled (they see "done")."""
    return call("GET", "/inbox")


@mcp.tool()
def ack_messages(ids: list[str]) -> dict:
    """Confirm you have handled these exact messages from get_inbox."""
    return call("POST", "/inbox/ack", {"ids": ids})


@mcp.tool()
def take_over(participant: str) -> dict:
    """Inherit a STALE teammate's locks and tasks so their part can be finished."""
    return call("POST", "/takeover", {"target": participant}, doing=f"taking over {participant}")


@mcp.tool()
def set_brief_plan(brief_id: str, plan_json: dict | str) -> dict:
    """Record the plan for a brief: {"parts": [{"name", "task", "summary"}]}. Planners call this last."""
    return call("POST", f"/briefs/{brief_id}/plan", {"plan_json": plan_json}, doing="writing the plan")


@mcp.tool()
def check_printability(names: list[str] | None = None, min_wall: float | None = None) -> dict:
    """Can these parts be 3D printed? Checks each part against the printer bed, that it is a closed
    solid, and lists suspected walls thinner than `min_wall` mm (default: the team's min_wall setting)."""
    return call("POST", "/printability", {"names": names, "min_wall": min_wall},
                doing="checking printability", targets=names)


@mcp.tool()
def accept_handoff(handoff_id: str) -> dict:
    """Pick up a person's handed-off work: their locked parts and in-progress tasks become yours.
    Call this first when your instructions name a handoff id."""
    return call("POST", f"/handoffs/{handoff_id}/accept", {}, doing="picking up handed-off work")


@mcp.tool()
def report_review(brief_id: str, round: int, passed: bool, findings: list[dict]) -> dict:
    """Reviewer only: record the review verdict for a brief. findings: [{"object", "problem", "task_id"}]."""
    return call("POST", "/reviews", {"brief_id": brief_id, "round": round, "passed": passed,
                                     "findings": findings}, doing="reporting the review")


def join_team():
    """Join, then greet and start heartbeats. It runs beside the MCP handshake: joining first took 5-14 s on
    a busy team database, and Claude Code, Codex and OpenCode all start the model without waiting for a slow
    server, so helpers began with no CADSwarm tools, went looking for them on disk, or gave up at once."""
    delay, deadline = 1.0, time.time() + JOIN_WAIT_SECONDS
    while True:
        try:
            ident = S.connect()
            break
        except HubRequestError as e:
            if e.code != "unreachable" or time.time() + delay > deadline:
                S.join_error = f"Couldn't join the team at {S.cfg['uri']} / {S.cfg['db']}: {e}"
                print(f"cadswarm: {S.join_error}", file=sys.stderr)
                S.ready.set()
                return
            time.sleep(delay)
            delay = min(delay * 2, 10.0)
    print(f"cadswarm: joined as {ident['name']} ({ident['participant_id']}, fence {ident['fence']})", file=sys.stderr)
    S.ready.set()
    # Background helpers are greeted by their runner (which posts it sooner); the host's one-job agents and
    # the team server never greet, so the chat stays for the real, lasting helpers.
    if not BACKGROUND and not S.cfg.get("ephemeral"):
        S.greet(ident)
    S.heartbeat_loop()


def main():
    global S
    S = Session()
    threading.Thread(target=join_team, daemon=True, name="cadswarm-join").start()
    if not S.cfg.get("ephemeral"):
        mcp.run("stdio")
        return
    # A one-job agent (the host's planners and workers) leaves when its session ends, so finished agents don't
    # linger in everyone's team list; leaving releases its locks and reopens any task it didn't finish.
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        mcp.run("stdio")
    finally:
        S.dead = S.dead or "left"
        if S.ready.is_set() and not S.join_error:
            try:
                S.client.post("/leave")
            except Exception:
                pass


if __name__ == "__main__":
    main()
