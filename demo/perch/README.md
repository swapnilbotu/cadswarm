# The Perch: the live demo

> **Historical record.** This runbook describes the 2026-10-24 hackathon demo, when CADSwarm used
> Elastic for board knowledge, Factory Droid as the reviewer and Creao AI as an optional helper. None
> of those are part of the product any more: knowledge is local and the reviewer is an ordinary
> spawned helper. Keep this folder (and `docs/devpost-submission.md`) as the record of that run; the
> commands below are not expected to work against current `main`.

Two acts, one object family, one screen that changes because a team is working. The judging table runs
Act 2 for two minutes; the booth runs both acts for forty. Nothing on either screen is canned: every
part is made by a real helper against the real model, and every claim on a card is read from the module.

The design and its constraints are in
`docs/superpowers/specs/2026-10-24-perch-showcase-design.md`. This folder is the runbook.

## What is here

| File | What it is |
|---|---|
| `driver.py` | joins the team as a human presenter; runs a timeline; serves the button page |
| `table.json` | the two-minute judging run: brief, recall, tasks, refusal, first part |
| `table-safe.json` | the lid-only fallback, chosen before a pitch, never mid-pitch |
| `timeline.json` | the 40–50 minute booth run: Act 1 through Act 2 |
| `prompts.md` | every brief, steer, note and phone line to copy |
| `checklist.md` | pre-flight, the live cue sheet, the reset order, the safe-run decision |
| `rehearsal.md` | the numbers sheet, filled in by rehearsal (Task 7) |
| `runs/` | generated run logs and summaries; ignored by git |

Deferred and not built here: `pitch.md`, `table-card.html`, `testimonials/`, the 2:45 video. The demo
does not depend on any of them.

## Cast

| Who | Client | Job | How it joins |
|---|---|---|---|
| `presenter` | presenter | presses Start, sends the steers, answers judges | `driver.py` (this folder) |
| `you` | FreeCAD | builds, corrects by hand, runs Auto-constrain | the CadSwarm workbench's join/invite flow (registers `client = "freecad"`) |
| `Sam` | phone | types and sends the Act 2 brief | the host's phone invite / QR |
| `planner-claude` | claude | turns a brief into assigned tasks, then stops | host-spawned as `planner-<brief>` when the brief is submitted |
| `lead-claude` | claude | `Shoulder_Hub`, Params, coordination, takeover | manual Claude Code |
| `worker-1/2/3-claude` | claude | unassigned tasks: `Clamp_Base`, `Cradle_Base` | host-spawned worker labels |
| `arm-opencode` | opencode | arm segments, FEA, Lighten | manual OpenCode |
| `lid-creao` | creao | the lid; killed for the takeover; back for Act 2 | Creao bridge |
| `base-claude` | claude | finishes the clamp after the handoff | host-spawned at the handoff as `handoff-<id>` |
| `reviewer` | droid | review rounds 1 and 2 | host `init-reviewer` |

The host-spawned names are call-sheet labels in this runbook, not literal member names. The host names the
planner `planner-<brief>` when the brief is submitted, the workers `worker<n>-<brief>`, and the handoff
agent `handoff-<id>`. Those are the names the panel's Team tab shows. `lead-claude`, `arm-opencode` and
`lid-creao` are the manual helpers and keep the names above.

The three card claims the window makes about people are these members: `client = "claude"` is the
Anthropic card, `client = "creao"` is the Creao card, `kind = "reviewer"` is the Factory card. The
presenter's `client` is `presenter`, so it never inflates a partner's counts.

Every manual helper joins **before** the brief, so the planner can assign by name. Every manual task is
assigned by name so a free host worker cannot poach it.

## One-time setup on the host

Run from the repo root, with the venv already installed (`python -m venv .venv;
.venv\Scripts\pip install -e ".[test]"`).

```powershell
# 0. If module/ or rules/ changed since the last publish (the reviewer-lifetime fix is module code),
#    publish the module keeping data before starting. scripts\demo-up.ps1 does not publish it
#    (docs\publishing-spacetimedb.md).

# 1. The stack: builds the host and web app the first time, runs it against Maincloud, prints a phone
#    invite. After one successful build, add -SkipBuild to start it faster.
powershell -File scripts\demo-up.ps1

# 2. One human invite for the presenter (the host prints a cadswarm-2: string).
host\target\release\cadswarm-host.exe --data $env:USERPROFILE\Documents\Projects\cadswarm-demo invite --kind human --role lead

# 3. The presenter folder: joins once, then reuses its identity on every later run.
.venv\Scripts\python demo\perch\driver.py --project demo\perch\presenter --invite "cadswarm-2:..."

# 4. The manual CLI helpers, each in its own folder (invites come from the host).
.venv\Scripts\cadswarm setup --client claude   --invite "cadswarm-2:..." --name lead-claude  --dir demo\perch\lead-claude
.venv\Scripts\cadswarm setup --client opencode --invite "cadswarm-2:..." --name arm-opencode --dir demo\perch\arm-opencode
.venv\Scripts\cadswarm setup --client creao    --invite "cadswarm-2:..." --name lid-creao    --dir demo\perch\lid-creao

# 5. The reviewer member and its folder. Run once per workspace: after this the host starts the review
#    rounds by itself. The member is HTTP-only, so it reads `stale` between rounds and stays valid.
host\target\release\cadswarm-host.exe --data $env:USERPROFILE\Documents\Projects\cadswarm-demo init-reviewer
```

`you` (FreeCAD) does not use `setup`: open the CadSwarm workbench in FreeCAD and use its join/invite flow;
the panel registers `client = "freecad"`. `cadswarm setup --client` takes `claude`, `codex`, `droid`,
`opencode` or `creao`; the three helpers above are `claude`, `opencode` and `creao`.

`demo/perch/presenter/.cadswarm/` holds the presenter's SpacetimeDB token; `.cadswarm/` is already
ignored. The driver's `--project` points at that folder, **not** at the repo root.

Keys for the optional Elastic, Factory and Creao services come from the environment; the usual way is a
`cadswarm.env` next to the checkout, loaded by `scripts\with-env.ps1` (which runs fine without one). The
agent helpers themselves use their CLIs' own sign-ins. The driver never
reads a key: its probe asks Elastic through `cadswarm.knowledge` exactly as the host does, and only
checks that `droid` is on `PATH`.

## Before every pitch

Read `checklist.md` top to bottom. It is ordered: stack, helpers, Elastic tabs, `act-1-ready`, probe,
then the replay window. The driver refuses to Start unless the probe passes and the team is at the
timeline's ready state: `act-1-ready` for the table, the empty model below for the booth.

## Act 1: "The Perch" (booth, 40–50 minutes)

Brief, typed in the panel (exact text in `prompts.md`): the clip-on arm, the cradle head, the board
lookup, the owners, the 180 mm bed, "the distal arm is what we'll simulate and lighten".

| # | Beat | Time | Action | What the room sees |
|---|---|---|---|---|
| 1 | Cold open | 1′ | Sponsor window on the second screen | Booth: the five cards are in place and reading live state (the manual helpers joined at T-30; the reviewer reads `stale` between rounds), and the board is bare; Act 1 starts now. At the table the helpers are also joined by T-30 |
| 2 | Plan + board | 2′ | Submit the brief | `lookup_board`, board Params, tasks with names |
| 3 | Build | 8–12′ | Let them work | Parts in owner colours; Creao one call per turn |
| 4 | Protection | 1′ | `@lid-creao thicken Arm_Distal by 2 mm; it flexes.` | Refused; it messages the owner |
| 5 | Shared size | 1′ | Tools ▸ Change a shared size… `arm_reach = 160 mm` | Segments and head positions recompute |
| 6 | Auto-constrain | 2′ | Owners release; Tools ▸ Auto-constrain… → apply; `tol_press_fit = -0.08 mm` | Aliases, M4 pattern, bearing fit; the bore shrinks |
| 7 | FEA | 3–6′ | `@arm-opencode linear static …` | Report pops up for the team; Elastic indexes it |
| 8 | Kill + takeover | 2′ | Kill the Creao bridge; `@lead-claude take_over("lid-creao")` | Creao goes stale, its trace stops, the lock moves |
| 9 | Handoff | 2–3′ | You hold `Clamp_Base`, edit the M8 pocket, and let go; Sam takes the lock in the phone's Model tab, sends the handoff note in Team chat, arms **Continue my work if I leave**, and closes the tab | Sam goes quiet; the handoff opens; the host's handoff agent accepts, finishes, and messages back |
| 10 | Review 1 | 2–4′ | Automatic when tasks are done | Droid pointer, trace, three findings |
| 11 | Correct + style | 2–3′ | Set the lid wall to 2.4 mm; add the three rules in Learned design style | The correction is recorded; rules are active |
| 12 | Review 2 | 1–2′ | Fix tasks, automatic re-review | Factory card: round 2 · approved |
| 13 | Snapshot + Lighten | 5–10′ | Snapshot `act-1-ready`; `@arm-opencode lighten Arm_Distal to 90% stiffness, gyroid, 5 mm cells, 2.4 mm solid skin` | Verifier console, mass saved, cut-open preview |
| 14 | Export + memory | 1–2′ | Export STEP + STL; run `distil-lessons`; open Kibana | Design doc + 3 lessons + Workflow execution |
| 15 | Sponsor recap | 1′ | Leave the window up; restart Creao for Act 2 | The same five cards, each carrying the work this run gave it |

Set the snapshot name `act-1-ready` exactly: `reset_ok()` and every table run depend on the six parts
being present and no `Podium_`/`Medallion` object being left behind.

The booth starts from a blank model: `timeline.json` has `parts_any: []`, no Act 1 parts, because Act 1 is
built live. The T-15 state section of `checklist.md` is the table pre-flight only; the booth state is an
empty model, no `Clamp_`/`Shoulder_`/`Arm_`/`Cradle_`/`Podium_`/`Medallion`, no open brief, and the manual
helpers joined.

## Act 2: "The Podium"

Booth: the phone brief asks for the body, the medallion and the lid. Table: two parts, a 2.4 mm lid,
same owners, same recall. The medallion and its 608 bearing are **booth only**.

| # | Beat | Booth | Table |
|---|---|---|---|
| 1 | Phone brief | 1′ | 0:30, the button |
| 2 | Recall | 1′ | by 1:00 |
| 3 | Build | 6–10′ | from 1:15; the pitch stops claiming at 2:00 |
| 4 | Review 1 → fix → round 2 | 4–6′ | not in the two minutes |
| 5 | Export + second design memory | 1–2′ | not in the two minutes |

## The driver

```powershell
# the judging table (default timeline is table.json)
.venv\Scripts\python demo\perch\driver.py --project demo\perch\presenter

# the pre-flight choice for a slow day
.venv\Scripts\python demo\perch\driver.py --project demo\perch\presenter --timeline table-safe.json

# the booth
.venv\Scripts\python demo\perch\driver.py --project demo\perch\presenter --timeline timeline.json

# the phone tunnel is down: send the same brief from this laptop, and the page says so
.venv\Scripts\python demo\perch\driver.py --project demo\perch\presenter --timeline table-safe.json --laptop
```

The page opens at `http://127.0.0.1:8765/` (`--port` changes it, `--no-browser` stops the tab).

- **Start** — refuses unless Probe passed and the team is at the timeline's ready state (`act-1-ready`
  for the table, the empty model for the booth), with no task still `open`, `claimed` or `blocked`;
  opens the sequence and shows the brief. At the table Sam sends it from the phone; at the booth you
  type it in the panel.
- **Next** — advance now. A manual step is logged `confirmed`; a module step `skipped`. Both say why.
- **Pause / Resume** — holds the sequence while a judge talks. The build keeps running.
- **Probe** — Claude, Creao, Droid, Elastic, then every other owner in the timeline's `probe` list.
  The booth's planner joins when the brief is submitted (`planner-<brief>`), and the handoff agent
  (`handoff-<id>`, called `base-claude` here) joins at the handoff, twenty-five minutes in, so neither is
  probed at Start. Re-run Probe after touching any helper.
- **Say box** — `@lid-creao …` is a real steer; anything else is a real brief. This is how a judge's own
  idea runs live.

Every step is done when the module says so — a task row, an op, a lock, a review, a bbox, an artifact,
an event, or an activity row (the host's own recall and a refused submit make no op). Never when an
agent says it is. Every run appends to `runs/<stamp>-<timeline>.jsonl` and writes
`runs/<stamp>-<timeline>.json` with `t_recall`, `t_tasks`, `t_first_op`, `t_finish`; those summary
numbers, not the targets, are what the pitch quotes.

## What the table claims

At the table the `table.json` run claims four things: the recall returned Act 1 with its three problems,
the tasks appeared with the same named owners, the held body refused Creao, and a real part started to
build. The safe run (`table-safe.json`) claims three of those: the recall, the tasks, and a real part
started to build; it has no refusal beat. Never a finished head. The absolute gate is **the first op by
1:45 on the pitch clock — 75 s after the 0:30 press**. The presenter says the fallback line if it is
missed; the page shows it in `Runner.status`.

The build continues after 2:00 and through Q&A. The driver never stops the team.

## If something breaks

| Symptom | Do this |
|---|---|
| Probe fails on Claude | check that helper's folder is running; re-run Probe. If it stays down, use the labelled replay: a timeline will not load with a `probe` name removed from `cast`, and dropping the name from both leaves its tasks unowned. Never fake its card |
| Probe fails on Creao | check the Creao bridge; re-run Probe. The safe run's only task is the lid, owned by `lid-creao`, so taking Creao out of `cast` would leave it unowned: recover with the labelled replay, not a cast edit |
| Probe fails on Droid | run `droid` once on the host (sign-in), or let the host's reviewer autostart path read the environment; then run `cadswarm-host … init-reviewer` if the Probe still says `no reviewer has joined` (signing in does not create the member). If `init-reviewer` prints `already set up` (the reviewer folder holds an identity), check two things: the folder's `.cadswarm/config.json` `db` matches this run's `host.json` `db` (an identity from another database prints the same thing and joins nothing), and `v_member` holds no retired row (the `left`/`taken_over` row) |
| The reviewer shows `stale` | expected: `init-reviewer` mints an HTTP-only member that never heartbeats. The sweep never orphans a reviewer, so it stays on the card between rounds. Probe passes it (with `droid` on PATH) and a stale reviewer does not block Start. A stale *helper* does |
| The Factory card shows a reviewer that is `left` or `taken_over` | the web card reads every `v_member` row and picks the first reviewer, while FreeCAD and Probe read `participants()`, which hides `left`/`taken_over`. With no live reviewer, Probe says `no reviewer has joined` and Start is refused, so this is not cosmetic. Deleting the reviewer folder and re-running `init-reviewer` joins a second row (a `left` namesake is not inherited), and the web card may still show the old one. Check `v_member` **before** a round: an `active` reviewer mid-round is normal. To clear it, start a new database (`-Db`), publish the module into it, reset the presenter and reviewer folders, and re-invite the whole cast; if there is no time for that, delete the reviewer folder and re-run `init-reviewer` so Probe passes, and say the web card may still name the retired row |
| Probe fails on Elastic | check `ELASTIC_URL`, `KIBANA_URL`, `ELASTIC_API_KEY` are set in the host's environment (an optional `cadswarm.env` loaded by `scripts\with-env.ps1` is the usual way); the recall needs them |
| Nothing started by 1:45 (75 s after the press) | the presenter says the fallback line and the page shows it in `Runner.status`; point at the recall and the tasks that did land, replay the booth recording **labelled as a replay**, and say which is live |
| The phone tunnel died | restart the driver with `--laptop`; same brief, same sequence, the member is the laptop |
| A step hangs | Next; a `confirm` step is logged `confirmed`, a module step `skipped`, and the sequence moves on |
| The recall or refusal step never closes while the build moves | restart the host, every helper and the driver (no module publish): the helper logs one `recall` row per returned design (rank, request, its own problems) and the hub client logs a refused submit as `refused`, both read from `v_activity`. The panel's Activity tab shows the same rows. If the recall rows are missing, check Elastic (the Probe's Elastic line) and that `<data-dir>/host.json` holds this run's `uri`/`db`/token — a host that never joined leaves no row |
| The page is closed | reopen the URL; the run and the log are unaffected |
| Creao stalls mid-Act 1 | kill it earlier: that is the takeover beat; restart it for Act 2 |
| A host worker claims a manual part | cancel that task and re-assign by name; the module only rebalances busy or stale assignees |
| Review passes round 1 | steer a legitimate tight-fit change (thin the podium lid to 1.6 mm so the OLED bezel fits) and wait for the style finding — never invent one |

## Reset between table judges

The operative order is in `checklist.md`; this is the same order. It is for the table only.

1. Restore the `act-1-ready` snapshot; delete `Podium_*` and `Medallion*`; hide the lattice mesh; move the
   cradle back; delete or cancel Act 2's tasks and the open brief.
2. Leave Act 1's design doc and lessons in Elastic. Leave the previous podium document too: if it ranks
   first for the next recall, say so out loud — the system is returning our own earlier run.
3. Restart Creao and any helper you killed; wait for `active`.
4. Restart `driver.py`: the runner refuses a second Start and the page disables Start after a run.
5. Re-run Probe. Confirm the page says `ready — press Start`, Probe is all ✓, and the first step is
   `pending`.

At the booth there is no table reset: `timeline.json` starts from an empty model and refuses Start once
Act 1 parts exist. A second booth run needs a new database (`-Db`, with the module published into it), the
presenter and reviewer folders reset (their stored identities point at the old database), and fresh invites
for the whole cast, not a restore of `act-1-ready`.

If the table reset cannot finish before the next judge, do not fake it: start from the running Act 2, say
the recall already happened, and let the panel and `rehearsal.md` carry Act 1.
