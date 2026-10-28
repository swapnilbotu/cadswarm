# CADSwarm

**Build 3D designs together with your team's AI helpers: no CAD or coding experience needed.**

Describe a part, say "an enclosure for this Raspberry Pi", and a swarm of AI helpers (Claude Code,
Codex, OpenCode, or the agent CLI you already use) builds it together in one shared FreeCAD model,
live, while you and your teammates watch from FreeCAD or a phone, steer them, and edit parts yourself
when that's quicker. The whole team's state (members, locks, tasks, messages, ops) lives in one
SpacetimeDB database, the hosting computer runs the only copy of the model, and the project keeps its
own board knowledge and design memory. Before the STL is exported, the host starts one more AI helper as
the reviewer to check fit and printability.

## Get started (about 5 minutes)

**Quick install:** paste one line into a terminal. It downloads CADSwarm, finds FreeCAD (and offers to
install the official FreeCAD if you don't have it), puts CADSwarm into FreeCAD, offers a shortcut, and opens
FreeCAD.

| | Paste this |
|---|---|
| **Windows** (PowerShell) | `irm https://raw.githubusercontent.com/SachchitBalamurugan/cadswarm/main/install/quick-install.ps1 \| iex` |
| **macOS** (Terminal) | `curl -fsSL https://raw.githubusercontent.com/SachchitBalamurugan/cadswarm/main/install/quick-install.sh \| bash` |
| **Linux** (terminal) | `curl -fsSL https://raw.githubusercontent.com/SachchitBalamurugan/cadswarm/main/install/quick-install.sh \| bash` |

Or install by hand:

1. **Install FreeCAD** (free) from [freecad.org/downloads](https://www.freecad.org/downloads.php).
   Default options are fine. On Linux, use the official AppImage: the snap, Flatpak and distro packages
   have no Python of their own, so with them you can join a teammate's project but not start a team or
   connect an AI helper (the installer offers to
   fetch the official FreeCAD for you).
2. **Download CADSwarm**: on this page click the green **Code** button, then **Download ZIP**.
   Unzip it anywhere, for example your Downloads folder.
3. **Run the installer** in the unzipped folder:
   - Windows: double-click **`Install CadSwarm.cmd`**.
   - macOS: double-click **`Install CadSwarm.command`** (if macOS says it can't be opened, right-click it,
     choose **Open**, then **Open** again).
   - Linux: run `bash "Install CadSwarm.sh"` (or right-click it and choose **Run as a Program**).
4. **Follow the guide that opens in FreeCAD.**
   - *First person on the team:* choose **Start a new team project**, name it, and copy the
     **invite code** to your teammates.
   - *Everyone else:* choose **Join a teammate's project** and paste the invite code.
   - Then pick your AI helper. The guide connects it and starts it in the background, so there's no
     window to keep open and nothing to paste. You talk to it in the CADSwarm panel's **Chat**.

That's it. Next time, open the **CADSwarm** shortcut (or pick *CADSwarm* in FreeCAD's workbench
list) and you're back with your team. If you hosted the team, its server starts again when you open CADSwarm (it doesn't start on its
own after a reboot until you do).

Worked on more than one team? CADSwarm remembers the ones this computer has joined. Pick another from
**CADSwarm ▸ Get started ▸ Open a previous project…**, or from the list at the top of the guide's first
page: each project shows its last picture and how many parts it had, so you can tell them apart before
opening one. Your sign-in for each team is kept, so you don't need a new invite code.

**Good to know**
- You need an AI helper installed: [Claude Code](https://code.claude.com/docs/en/setup),
  [Codex](https://learn.chatgpt.com/docs/codex/cli), or
  [OpenCode](https://opencode.ai/docs/). The guide detects which ones
  you have and links to install instructions.
- Your AI helper must be signed in once on this computer (open `claude`, `codex` or `opencode`
  in a terminal and follow its sign-in). After that CADSwarm starts it by itself whenever FreeCAD is open, and wakes it
  when someone messages it or hands it a task. Its log is under **the Tools tab ▸ Show my AI helper's log**.
  An OpenCode helper keeps one private AI server running between messages, so a message reaches the model in
  about a second instead of paying the CLI's several-second start every time.
- An OpenCode helper (and every OpenCode agent the host starts) runs on the model configured in the person's
  own `opencode.json`; if that file names no model, the first model of the first configured provider is pinned.
  Without that pin OpenCode picks a built-in provider that was never signed in to, and every run dies at once
  with an "Incorrect API key" error.
- AI helpers always join as ordinary members. The people on the team lead it, and only a person's computer
  hosts it.
- **Whoever starts a team** makes one extra download, about 50 MB: CADSwarm's team server plus the
  [SpacetimeDB](https://spacetimedb.com/) database program (pinned to 2.13.0). The guide fetches them
  into CADSwarm's app folder (`%APPDATA%\CadSwarm` on Windows, `~/Library/Application Support/CadSwarm`
  on macOS, `~/.local/share/CadSwarm` on Linux) the first time you start a team, checks the SHA-256 of
  each file, and replaces an older download when a later release publishes a different one. The module
  the workbench ships with is used as it is, so updating the workbench never leaves an old module
  behind. Teammates who join don't need them. (A team server or SpacetimeDB already on your PATH is
  used as it is.)
- The team's database runs on **SpacetimeDB**. By default it runs **on this computer**: nothing to sign
  up for, and teammates on the same network join by reaching this computer's network address (Windows
  may ask to allow access through its firewall). Choose **Maincloud** instead to let teammates join from
  anywhere; the guide's **Sign in to SpacetimeDB…** button opens your browser so you sign in once.
- Keep the hosting computer on while your team works. The host program keeps running even if you close
  FreeCAD.
- The first setup downloads about 70 MB of helper software into CADSwarm's app folder, plus the team
  server download above if you host. Nothing is installed into FreeCAD itself. On Linux, an official FreeCAD
  the installer fetched for you is unpacked there too (`FreeCAD-<version>`, about 3 GB).
- Installer options (macOS/Linux, after `| bash -s --` or on `install/install.sh`): `--yes` (no questions;
  never downloads FreeCAD), `--get-freecad`, `--no-shortcut`, `--no-launch`. `CADSWARM_FREECAD` picks a
  specific FreeCAD; `CADSWARM_REF` makes the quick install use another branch or tag.
- When you start a team, the guide asks **where to save the project** (default
  `Documents\CadSwarm\<project name>`). If you pick a folder that already has other things in it,
  the project gets its own subfolder there, so your files are never mixed in or overwritten.

## Working with your team in FreeCAD

The **CADSwarm Team** panel on the right follows the size of the FreeCAD window (drag its edge to give it
more or less room) and scrolls when the window is short. It has three parts:

- **AI helpers**: every helper on the team in three columns: its name, the **Task** it's on (or `next: …`
  when one is waiting for it) and its status (**◐ Working**, **Ready**, or why it's away). **＋ New helper**
  starts another one on this computer: pick the AI (Claude Code, Codex or OpenCode) and, if you like, a name
  (otherwise it's called after you and its AI, like `carol-claude`). Have as many as you want. Right-click
  one of yours to **Rename** it, **Restart** it or see its log; right-click any helper (even one running on a
  teammate's computer) to **Remove from the team**. Removing releases its locks, returns the work it held and
  stops it from rejoining on its own: only a fresh invite brings it back. People are listed underneath.
- **Chat**: one box for everything.
  - `@name what to do` gives that helper a task (with the parts you have selected) and wakes it; it answers in
    the chat with what it'll do, then what it did. Typing `@` lists the helpers to pick from. Your message
    shows **⏳ not read yet**, **👀 read, working on it** or **✓ done**. `@a @b ...` gives each one the task.
  - `@team what to build` hands a whole request ("a stand for a 7 mm thick phone") to the team: the team server
    plans it into parts and starts AI helpers itself (with the agent CLI on the hosting computer). The chat
    notes where the request went and opens Tasks; that note is local, not a message to the team.
  - `@all ...` tells every helper something (pause, status...).
  - No `@` is chat between people. AI helpers never see it.
  Tasks only go to AI helpers: people pick their own work.
- **Tasks** and **Activity** fold open underneath. Tasks lists everything that needs doing, whole-team
  requests with their parts underneath; use **Start it**, **I'll do it** or **Mark done**. Activity is what
  happened (parts added, tasks finished, files saved, renames) and 🤝 the messages helpers send each other.
- **AI helpers work together.** A helper can see who is free (`list_helpers`), hand another helper part of
  a job (`delegate_task`, which wakes that helper) and ask the helper who holds a part to change it. When a
  handed-out task is finished, the helper that handed it out is woken with the result. Each helper runs at
  most 30 times an hour, so two helpers can't keep waking each other.
- **Team tools ▾** has editing a part yourself, shared sizes, clash checks, STEP export, project files,
  snapshots, reports and storage.
- **Remote** is where a phone joins from: the host mints one single-use code and shows it as a QR, with
  the same link as text. Scan it with the phone's camera (or send the link) and the phone opens the
  live build: the model, the chat and the `@` commands, from the sofa. One code works once, and only
  while the computer hosting the team is on; **New code** makes the next one.

**Changing a part yourself.** Select the part in the 3D view and choose **the Tools tab ▸ ✎ Edit a part yourself…**. While the
editor is open, the part is held by you, so no helper can change it at the same time. Type a new size
or position, or move it with FreeCAD's own **Edit ▸ Placement** / **Transform** and the editor picks up the
move. **Save to team** sends the change to everyone; **Done** hands the part back. A part held by a helper
offers **Ask … to let go** (or, if it has gone quiet, **Free it anyway**). If you move a part in the 3D view
without opening the editor, CADSwarm puts it back and explains, so your view never silently differs
from your team's.

## Your project folder

```
Phone stand/
  model.FCStd          the live shared model (only the team server writes it)
  files/               notes, plans, BOMs and scripts the AI helpers saved, in subfolders that
    parts/base/notes.md  grow as the project does
    docs/bom.csv
    fea/2026-10-23 21.05 shelf bracket/   simulation reports: report.html, report.json, plot.png
  exports/             3D files (STEP/STL), each with a new name, never overwritten or auto-deleted
  snapshots/           saved versions of the model ("2026-10-23 20.32 first draft.FCStd"), never auto-deleted
  library/             the team's components library: one folder per part (part.step + meta.json)
  AI helpers/          each helper's own workspace on this computer (holds its sign-in; not shared)
  .cadswarm-team/      the team server's data, board knowledge and design memory (knowledge.db),
                       plus older versions of files (the last 20 of each)
  README.txt           explains the above to anyone browsing the folder
```

In FreeCAD, **the CADSwarm panel's Tools tab > Project files...** lists what the team saved (version, who, when) and
lets anyone save a copy. The host also gets **Open project folder**.

### Saving is safe by design

The team server enforces these rules; it doesn't just ask the AI helpers to follow them:

- **Sandboxed.** Helpers can only write inside `files/`. Paths with `..`, absolute or drive paths,
  hidden or Windows-reserved names, and symlinks or junctions that point outside are refused. File
  types are on an allow-list, with size limits per file: 5 MB for text (notes, scripts, CSV), 512 MB
  for binary CAD and media files (STEP, STL, 3MF, FCStd, DXF, PDF, images).
- **No silent overwrites.** Saving onto an existing file is refused unless the helper asks for
  `new_name` (saves as `notes-2.md`) or `replace` with the version it last read. If someone changed
  the file since then, including a person editing it by hand, the save is refused.
- **Old versions are kept, within limits.** Replaced, moved and deleted files go to history and can be
  restored: the last 20 versions of each file are kept (and everything from the last day). A deleted
  file always keeps its latest version. Older versions beyond that are removed for good. If your file is
  replaced by a teammate, you're told.
- **Atomic writes.** A crash never leaves half a file.
- **Model scripts run in a sandbox.** When a helper runs a `run_part_script`, CADSwarm starts a separate,
  throw-away FreeCAD process for it, never the live model's own process. The script works on a read-only
  copy of the model and can hand back only a shape and some data. Anything else it does to the copy is
  discarded, so it can't change other people's parts. How tightly that process is locked down depends on
  your computer (the model server shows the active level at `/health`, under `script_sandbox`):
  - **Windows 10/11, normal case (`appcontainer`):** the script process runs in a Windows AppContainer, the
    same mechanism browsers use for web pages. It has no network, can't read or write your documents or
    profile, can't start other programs, and can only write inside a scratch folder that is deleted
    afterwards. It is also capped at about 2 GB of memory and 60 seconds. The first start gives that
    container read access to your FreeCAD folder, which can take a few seconds.
  - **Windows fallback (`lowintegrity`):** if AppContainers can't be created, scripts run with a
    low-privilege copy of your account. They still can't write anywhere but the scratch folder or start
    programs, but they **can** read your files and use the network. Use this only with people you trust.
  - **macOS / Linux (`process-limits`):** scripts run in their own process with memory, time and
    open-file limits, but with **no** file or network isolation, so they run as you. Use this only with
    people you trust.
  - **`CADSWARM_SCRIPT_SANDBOX=off`** switches all of this off for debugging and prints a loud warning.
  Settings: `CADSWARM_SCRIPT_TIMEOUT` (seconds) and `CADSWARM_SCRIPT_MEMORY_MB`. A script that runs
  too long or uses too much memory is stopped and reports an error; the model server keeps going.

### Disk space and the project limit

Big projects are welcome, but the host's computer is protected and a project can't get stuck:

- **Disk guard.** Before anything that adds data (saving a file, a snapshot, an export), the team server
  checks free space on the drive. It refuses if less than 2 GB would be left (or 10% of the drive, if
  that is smaller) and says so in plain words (`disk_full`).
- **Project limit: 20 GB by default**, counting files, older versions, snapshots and exports together.
  The host can change it in **the CADSwarm panel's Tools tab > Storage and free up space...** (0 means no project limit; the
  disk guard still applies), or with the environment variable `CADSWARM_PROJECT_LIMIT_GB` when starting the
  team's server. History retention (20 versions, 24 hours) is changed in the same place.
- **Free up space.** The same dialog shows what is using the room and lets a person trim older versions,
  delete old snapshots (always keeping the newest few) and old exports. Snapshots and exports are never
  deleted automatically, and AI helpers can look (`storage_status`) but can't delete: they're told to ask
  a person.
- Binary files up to 512 MB are accepted over HTTP; a save sent as base64 (what helpers do) briefly needs
  about 2.5 GB of RAM on the host while it is decoded. The host's own saves (lattice results) pass raw
  bytes and only need the file's size.

### Strength checks (FEA)

Ask your AI helper things like *"will this bracket hold 10 kg?"*, *"what's the natural frequency of the
arm?"* or *"how hot does the motor mount get?"*. It recognises a simulation request and asks you, in one
message, which analysis to run (it recommends **linear static**: stress, deflection and safety factor;
**modal** and **steady-state thermal** are also available), the material, which faces are held still and
the loads. If you don't know, it uses clearly marked placeholders (generic S235 steel, 1000 N on the top
face, bottom face fixed...) and the report says so in a yellow box.

The analysis runs FreeCAD's own FEM tools (Gmsh mesher + CalculiX solver, both in the standard FreeCAD
download) on a **copy** of the part. If this FreeCAD has no Gmsh (some Linux packages and snaps), the team
server installs it once by itself: the official Gmsh 4.15.2 build from PyPI, checked against a fixed SHA-256,
unpacked into `%APPDATA%\CadSwarm\tools` (`~/.local/share/CadSwarm/tools` elsewhere), never into FreeCAD or the
system. Set `CADSWARM_NO_AUTO_INSTALL=1` to turn that off. The solve runs in a separate process: the shared model, its locks and everyone else's
work are untouched, and one long solve never blocks the team. When it finishes, the helper adds a plain-words
summary and its own points of improvement to the automatic checks (safety factor, stress peaks at supports or
sharp corners, coarse mesh, no convergence check, resonance, temperature limits, placeholders...) and
publishes the report. It **pops up in every teammate's FreeCAD**, can be exported as a self-contained
**HTML** page or as **JSON** data, and is kept in `files/fea/`. In the CADSwarm panel, **Tools ▸ Simulation reports…**
lists past reports. If a helper forgets to publish, the hub publishes the automatic checks after 5 minutes.

FEA here is a design aid for linear-elastic behaviour with idealised supports, not a certification.

### Lighten without losing stiffness

**Lighten part…** (the CadSwarm panel's Tools tab, the CadSwarm menu, and the CADSwarm Optimization workbench without a
team) fills a part with a lattice that is thicker where the stress is high, keeps a solid skin and solid zones
where it is held and loaded, and checks, by simulating the real lattice, that it keeps the stiffness you ask
for (90 % by default). Pick the faces it is held by and loaded on, the material and how it is made; you get
the weight saving, the verified stiffness, a preview you can cut open, and STL, 3MF and a report in
`files/lattice/`. **Add to model** puts the lattice next to the original as a mesh; the original is never
changed. AI helpers use `lighten_part` and suggest it when a part could be lighter, including when an FEA
report shows strength to spare. Everything runs on the hosting computer in FreeCAD's own Python. It is a
design aid, not a certification. See [docs/lattice](docs/lattice/README.md).

### Components library and precision settings

The **Library** tab (Team panel in FreeCAD, and the web app) is the team's parts shelf, like Onshape's:

- **Your team's parts.** **Upload a part…** adds a STEP, FreeCAD or STL file; **Add from a link…** adds one
  from a direct download link (a vendor's CAD file, a model on GitHub). FreeCAD measures each part (size,
  volume, holes) and makes a preview. Parts live with the project on the hosting computer, in `library/`.
- **Standard parts**, built to their real dimensions: socket and button head screws (ISO 4762 / 7380), hex
  nuts, washers, heat-set inserts, ball bearings (608 and friends), dowel pins and disc magnets.
- **Place in model** puts the part in the shared model (you hold it) and offers to position it.
  **Which part should I use?** describes a need ("an 8 mm axle that spins") and suggests parts, each with the
  hole the mating part needs.
- **More ▾ › Export the library as a pack…** saves the team's parts (and settings) as a `.zip` in the project
  files; **Import a pack…** brings them into another project. A pack's settings arrive as suggestions only.

AI helpers use the same library: they check it before modelling a screw or a bearing
(`recommend_components`, `search_components`), place parts (`add_standard_part`, `insert_component`), size
mating holes with the team's fits (`hole_size`), and can add a part they found online
(`import_component_from_url`, with its license).

**Precision settings** (the panel's Tools tab, or the button in *Change the model yourself*) hold the team's
clearances and fits (general clearance, slip fit, press fit), bolt-hole class (ISO 273 close / normal /
loose), how screws hold in printed parts (heat-set inserts, tapped, self-tapping), printing values (XY
compensation, thinnest wall, layer height), mesh precision for STL exports and previews, the smallest clash
the clash check reports, and the part editor's decimals and arrow step. Fits are also shared sizes
(`Params.tol_clearance`, `Params.tol_press_fit`...), so geometry bound to them updates when they change.

Only people change settings. When a part implies a different value (a press-fit bearing seat, holes so small
a 0.2 mm mesh would facet them), an AI helper **suggests** it with a reason; the suggestion waits at the top
of the panel's Team tab (and of Settings) for someone to **Accept** or **Dismiss**.

### Learned design style

CADSwarm learns how your team designs and teaches it to every AI helper. The host notes each change to the
shared model: who made it, whether a person or a helper, and what changed. It also notes every finished
brief. From that it works out rules:

- **Shared sizes the team settles on.** For example, "Params.wall is 2.4 mm".
- **What people keep correcting** after a helper. This is the strongest signal, so it counts double.
- **Preferred parts.** For example, "M3 heat-set inserts".
- **Modelling habits.** For example, binding sizes to Params.
- **Problems reviews keep finding** for a board.

A rule backed by enough consistent, recent evidence from more than one person or day is **active**. Planners,
workers, handed-off helpers and the reviewer get it in their prompts, and every helper turn shows it as
"Team design style". Weaker rules, and rules a helper proposes (`teach_style`), wait for a person to decide.

The Team tab shows one summary line — how many things were learned, how many decisions that is, and how many
are ready to use. **Review** opens **Tools ▸ Learned design style…**, the triage inbox:

- **Rules that describe the same thing are one decision.** A shared size and the corrections people keep making
  to it are answered together; deciding one resolves all of them.
- **Decisions are grouped by kind** — shared sizes, corrections, preferred parts, habits, review problems and
  helper proposals — and each carries a **Ready**, **Look** or **Weak** badge worked out from its evidence. The
  badge is a suggestion from the evidence, never applied for you.
- **Make the ready rules** and **Dismiss the weak ones** answer a whole set in one visible action. Each opens the
  exact statements first, all selected, and any can be unticked. Every section also has **Use all** /
  **Dismiss all**.

The rest of the dialog is the undo history:

- **Confirmed rules** are always followed and never fade.
- **Dismissed rules** stay away until their evidence doubles.
- **Undecided weak rules** leave the inbox after two weeks without new evidence. They stay under **Expired or
  dismissed**, where **Make it a rule** brings one back; new evidence can also revive one on its own once it
  doubles.
- **Unused learned rules** stop guiding helpers when their evidence goes stale, then expire from the inbox.

You can also type a rule yourself. It is confirmed at once. The reviewer flags parts that break a rule as
`style:` findings, and those findings feed the next round of learning.

Rules live in the project database on the hosting computer; learning needs no account, keys or
network. `python -m cadswarm.style_memory show --project <folder>` lists them.

---

## How it works

All the AI helpers edit the *same* FreeCAD document through the team's SpacetimeDB database, and humans
watch and steer from the CADSwarm workbench. They don't overwrite each other because:

- **Object locks.** You can only change objects you've locked, and anything you create is yours.
- **Transactional rollback.** An op (including an arbitrary agent Python script) that touches
  an object you don't hold is aborted inside FreeCAD, and nothing is saved.
- **A shared `Params` sheet.** Agents bind geometry to shared dimensions with FreeCAD expressions.
  Changing one recomputes every dependent part and tells its owner.
- **Heartbeats, stale detection and takeover.** If a teammate's agent dies, its half-built parts
  stay protected until someone takes over its locks and tasks.

The coordination design is adapted from Amoeba's multi-agent runtime (fenced leases,
heartbeat-held locks, takeover by compare-and-set, delivered/consumed message receipts,
and human steering ahead of peer requests), applied to CAD objects instead of git files.

```
 alice: Claude Code ─┐  MCP (stdio)          ┌──── SpacetimeDB: the team's database ────┐
 bob:   Codex  ──────┼──► cadswarm MCP ──────► members · fences · locks · revisions ·    │
 dana:  OpenCode ────┘   + MCP tools         │ tasks · messages · briefs · stale sweep   │
                                             └───▲──────────────────▲──────────────▲────┘
 carol: FreeCAD GUI ─── CADSwarm workbench ──────┘                  │              │
        (mirror + team panel)                                       │   phone / browser: web app
                                                                    │   (pair by QR or #t= link;
                                         cadswarm-host (Rust) ──────┘    pointers, briefs, 3D view)
                                         on the host's computer: runs ops one at a time,
                                         orchestrates briefs, spawns agents and the reviewer
                                           │ JSON lines                  │ JSON lines
                                           ▼                             ▼
                          FreeCAD worker (FreeCAD's python):    helper (cadswarm/helper.py):
                          THE live .FCStd, transactions,        project files, storage limits,
                          lock-checked rollback                 board knowledge and design memory
```

## How review works

The host starts one more AI helper as the team's reviewer whenever every task of a brief is done, and
again after the owners fix what it found. It is an ordinary helper folder under
`team-model\agents\reviewer`, run with the same agent CLI as the planners and workers, and it joins the
team as a `reviewer` member.

- **What it checks:** collisions between parts, that the board actually fits (measured board dimensions,
  mounting holes, port cutouts), and printability (print bed size, closed solids, walls under 1.2 mm).
- **What it can touch:** the SpacetimeDB module refuses every write op from a `reviewer` member, so it
  cannot change geometry even if a tool slipped through.
- **Where you see its work:** every tool call and finding is written to the `activity` table and shown
  live in the app, and its verdict is stored in `review` and shown as a review card on the brief.
- **Its arrow:** it is a member like any other, so it gets its own pointer in its own colour. The round
  parks it on the brief's first part and `point_at` moves it to whichever part it is checking, in
  FreeCAD's 3D view and the web viewer alike.
- **When it cannot start:** the host checks the agent CLI before minting the reviewer's invite. If the
  reviewer still cannot start three times in a row, the brief fails with the tail of
  `team-model\agents\reviewer\agent.log` instead of retrying forever.

People can also hand work to an agent mid-design: while holding a part, "Hand off now" with a note
spawns one agent per open handoff. It calls `accept_handoff` first (which transfers the person's locks
and tasks), reads their note and recent ops, and continues instead of starting over.

## One job, then off the team

Planners, workers, the reviewer and handoff agents are one-job identities: the host starts one, it does
its job and exits. The host takes it off the team the moment its CLI exits, so a finished worker never
sits in the Team tab with "no task", and when a brief reaches `ready`, `failed` or `cancelled` the host
also deletes the folder every start left behind under `team-model\agents\`. A worker that exits while its
task is still open hands the task back to the team, and the next tick starts a fresh worker for it. The
same happens when a worker's MCP server goes quiet (the sweep marks it stale): the host takes it off the
team and its task goes back on the board, so a task no longer waits as "needs someone" until a person
notices. A host restarted mid-brief does this too, for the agents the previous host left behind.

## Run the demo

On Windows, one command brings up the whole stack on this machine: it builds the host and the web app,
serves the web app, opens a Cloudflare quick tunnel so phones can reach it, sets up the data folder
against Maincloud, runs the host with your environment (an optional `cadswarm.env` is loaded through
`scripts\with-env.ps1`, and nothing is required), and prints a single-use phone link and QR code.

```powershell
powershell -File scripts\demo-up.ps1               # first run: builds everything
powershell -File scripts\demo-up.ps1 -SkipBuild    # later runs
powershell -File scripts\demo-down.ps1             # stop everything demo-up started
```

Options: `-Data <dir>` (data folder), `-Db <name>` (Maincloud database), `-Python <exe>` (Python with
the cadswarm package), `-Agent claude|codex|opencode`, `-AgentCommand <argv>` (see below),
`-WebPort <n>` (default 4173), `-WebUrl <url>` (use a fixed public URL instead of a tunnel). Everything it starts is
recorded in `<Data>\.cadswarm-team\demo-pids.json`; `demo-down.ps1` stops only those processes, and
only after checking each one's command line. See [demo/README.md](demo/README.md) for a three-agent
demo script.

## Bring your own agent

The agents that build briefs run on whichever agent CLI the host's owner already uses, with their own
account and model provider. Pick one when you set up the host:

```powershell
host\target\release\cadswarm-host --data .\team-model init --agent codex   # claude (default) | codex | opencode
```

Or run any other CLI by setting `agent_command` in `team-model\host.json`; `{prompt}` is replaced by the
task prompt:

```json
"agent_command": ["aider", "--message", "{prompt}", "--yes"]
```

A CLI that isn't on `PATH` can be pointed to with `CADSWARM_CLAUDE`, `CADSWARM_CODEX` or
`CADSWARM_OPENCODE`. The same CLI runs the planners, the workers and the reviewer.

## For developers: command line

> **Updating the team database.** After any change to `module/` or `rules/` (new op kinds such as the
> lighten and style tools), the SpacetimeDB module must be republished, keeping the data, and the host
> restarted. Step-by-step guide, including owner sign-in, bindings, a local rehearsal and troubleshooting:
> [docs/publishing-spacetimedb.md](docs/publishing-spacetimedb.md).

The guide wraps these commands; they still work for scripting and CI.

Prereqs: FreeCAD 1.x (found automatically, or set `CADSWARM_FREECAD_PYTHON` to its
`bin/python.exe`) and Python 3.11+. When you host, the guide downloads the
[SpacetimeDB CLI](https://spacetimedb.com/install) 2.13.0 and `cadswarm-host` into
`%APPDATA%\CadSwarm` on first use, and refreshes them when a newer release changes them; the module
comes from the workbench (or that download, on an older install). From a checkout you can also build
them yourself with `cargo build --release` in `host/` and
`cargo build --release --target wasm32-unknown-unknown` in `module/`.

Nothing in the checkout needs keys or a config file. The AI helpers run on the sign-in of whichever
CLI you already use (`claude`, `codex` or `opencode`), the host starts them the same way, and
SpacetimeDB publishing and hosting use the CLI's stored login (`spacetime login`). Tests and the host
run as they are:

```powershell
python -m pytest -q
host\target\release\cadswarm-host.exe --data .\team run
```

Only the optional demo services (Elastic recall, Creao, Factory) and a machine that cannot sign in
interactively need variables. Set them in your shell, or write them to a git-ignored `cadswarm.env`
next to the checkout and let the loader set them for one command -- it never requires the file:

```powershell
powershell -File scripts\with-env.ps1 python -m pytest -q
```

`.env.example` lists the extras and the developer overrides; `-EnvFile <path>` or
`CADSWARM_ENV_FILE` picks a specific file.

```powershell
cd cadswarm
python -m venv .venv; .venv\Scripts\pip install -e ".[test]"

# 1. Host: publish the module, then write this computer's team settings and run the host.
#    The guide does all three for you; from a terminal, publish with:
#    spacetime publish --server maincloud --yes=all --delete-data=never --bin-path
#      module\target\wasm32-unknown-unknown\release\cadswarm_module.wasm  cadswarm-my-team-1a2b
host\target\release\cadswarm-host --data .\team-model init --mode maincloud --db cadswarm-my-team-1a2b --python "path\to\FreeCAD\bin\python.exe"
host\target\release\cadswarm-host --data .\team-model run

# 2. Host: mint a single-use invite for each person or agent (also prints a phone link and QR code).
host\target\release\cadswarm-host --data .\team-model invite --kind human --role lead

# 3. Each person: join with the invite and wire their own AI helper in their own folder.
.venv\Scripts\cadswarm setup --client claude --invite "cadswarm-2:..." --name alice --dir ..\alice
.venv\Scripts\cadswarm setup --client codex  --invite "cadswarm-2:..." --name bob   --dir ..\bob
#    then run `claude` / `codex` / `opencode` in that folder. (Codex: trust the project so
#    .codex/config.toml loads, then type `/hooks` in Codex and trust the CADSwarm hook.)

# 4. Install this checkout into FreeCAD as the CADSwarm add-on (same as the double-click installer).
.venv\Scripts\cadswarm install-workbench
```

Republishing an existing team's database after `module/` or `rules/` changes (owner login, keeping
the data, bindings, restarting the host): see [docs/publishing-spacetimedb.md](docs/publishing-spacetimedb.md).

### Packaging a release

The guide's first-use download needs three assets per platform. `scripts/package-release.ps1` builds
and checksums them into `dist\` (add `rustup target add wasm32-unknown-unknown` once):

```powershell
pwsh scripts\package-release.ps1                            # this computer
pwsh scripts\package-release.ps1 -Target aarch64-apple-darwin
pwsh scripts\package-release.ps1 -ModuleOnly                 # cadswarm_module.wasm only
```

Push a `v*` tag (`git tag v0.1.0; git push origin v0.1.0`) and `.github/workflows/release.yml`
builds all five host platforms plus the module on GitHub, writes `SHA256SUMS.txt`, and publishes the
release the guide resolves as `releases/latest/download`. To test the download path without a
release, point `CADSWARM_RELEASE_BASE` at a local folder (`file:///C:/path/to/dist`) holding the same
files.

## Where the team's data lives

- **The database is on SpacetimeDB.** By default it runs on the host's own computer (`spacetime start`,
  bound to the network so teammates on the same network can reach it): no account needed, works offline,
  but everyone must share a network and the host must stay on. The other choice is **Maincloud**: nothing
  to run, teammates join from anywhere, and SpacetimeDB handles TLS and sign-in (the guide signs you in
  once through your browser). A database left over from an older CADSwarm (or a manual `spacetime start`)
  that is bound to `127.0.0.1` only — so no teammate can reach it — is restarted on the network the next
  time you host or reconnect a local team.
- **The live model stays on the host's computer.** `cadswarm-host` executes ops against it and serves
  files, exports, snapshots and storage, so the host must stay on while the team works.
- **Invite codes are `cadswarm-2:` codes.** Each one works once and expires after a day. Make a new one
  for each teammate from the guide's invite page or the team panel's **Invite** button. They carry the
  database address and can be pasted as text, as a bare ticket, or as a phone link (`#t=`). The team
  panel's **Remote** tab shows that phone link as a QR; phone links point at
  `https://cadswarm.design/app` unless the host was started with its own `--web-url`. A team hosted on
  this computer hands out this computer's network address, so teammates must be on the same network
  (and a phone, reached through the public site, cannot — use FreeCAD on a networked computer).

## What `cadswarm setup` writes

| Client | Files |
|---|---|
| Claude Code | `.mcp.json` (cadswarm server), `.claude/settings.json` (UserPromptSubmit hook, `enabledMcpjsonServers`, `mcp__cadswarm` allowed), `CLAUDE.md` section |
| Codex | `.codex/config.toml` (`[mcp_servers.cadswarm]` + `[hooks] UserPromptSubmit`, in a managed block), `AGENTS.md` section. Codex skips hooks you haven't reviewed: in Codex, type `/hooks` and trust the CADSwarm hook (again after updating CADSwarm, because Codex re-checks it when it changes). Until then your helper gets no team block before prompts and falls back to calling `model_status`/`get_inbox` itself |
| OpenCode | `opencode.json` (cadswarm MCP server and the pinned `model`), `AGENTS.md` section |
| all | `.cadswarm/config.json` (uri, db, name, client, role), and after first run `.cadswarm/identity.json` (SpacetimeDB token, git-ignored) |

The prompt hook (Claude Code and Codex) injects a team block before every prompt: who holds which
parts, open tasks, what changed since your last prompt, and unread messages. It fails open: if the
team's database is unreachable, the prompt goes through without it. OpenCode gets the same context
through the MCP tools instead.

## Agent tools (MCP)

- **Awareness:** `model_status`, `get_object`, `wait_for_changes`
- **Locks:** `lock_objects`, `release_objects`
- **Modelling:** `create_primitive`, `create_extrusion`, `boolean`, `set_props`, `set_placement`,
  `delete_object`, `set_param`, `run_part_script`
- **Checks and output:** `check_interference`, `export_model` (STEP/STL), `save_snapshot`, `storage_status`
- **Simulation (FEA):** `list_faces`, `run_fea` (static / modal / thermal), `fea_status`, `publish_fea_report`
- **Lighten:** `lighten_part`, `lattice_status`, `lattice_apply`, `lattice_cancel`, `lattice_cells`, `lattice_estimate`
- **Components library:** `search_components`, `recommend_components`, `get_component`, `insert_component`,
  `add_standard_part`, `hole_size`, `import_component_from_url`, `add_component_from_file`
- **Team settings:** `get_settings`, `suggest_setting` (people accept suggestions; helpers never change settings)
- **Team design style:** `team_style`, `style_inbox`, `style_rules`, `teach_style` (a helper's rule waits for a person)
- **Project files:** `list_files`, `read_file`, `save_file`, `make_folder`, `move_file`,
  `delete_file`, `file_history`, `restore_file`
- **Teamwork:** `list_tasks`, `create_task`, `claim_task`, `report_result`, `send_message`,
  `get_inbox`, `ack_messages`, `take_over`

Board knowledge and design memory are local to the project. The host's helper process reads the board
library (`boards/boards.json`, plus datasheet text added with
`python -m cadswarm.ingest boards/sources.json --project <folder>`) and keeps finished designs and
simulation reports in `knowledge.db` inside the project's `.cadswarm-team` folder (`recall_designs`,
`lookup_board`, `board_spec`). Nothing leaves the hosting computer, and no account or key is needed.

## Rules the team enforces

1. An op may only change objects you hold. Pre-reserve a name with `lock_objects` before creating it.
2. Ops can carry `expected_rev`. If someone changed the object since you read it, you get a 409.
3. A `Params` change that recomputes someone else's part bumps its revision and messages its owner.
4. No heartbeat for 30s → **stale**. Its locks are kept, its tasks become `awaiting_takeover`, and
   everyone gets a handoff notice. `take_over` moves its locks and tasks to the caller. It is a
   compare-and-set, so it only works on stale participants (a human may force it). After 10 minutes
   stale, the locks are freed and the tasks reopened.
5. Re-joining with your stored identity resumes you at a new **fence**, so an older process using
   that identity can't act any more. A participant whose work was taken over has to join fresh.
6. Names are unique among live participants. A fresh join under a stale participant's name
   (someone who lost their identity file) inherits that participant's own work.
7. Tasks and messages can be addressed to a teammate who hasn't joined yet. They're delivered when
   that name joins.
8. A person's instructions to a helper are `steer` messages (shown as COMMAND) and always come first
   in every inbox; the sender sees sent → seen (delivered) → done (acknowledged). Team chat is `chat`,
   last in the inbox and never treated as an order. Only people can give commands; a person's
   "command" to another person is delivered as chat. Peer messages are requests, never permissions.

## Tests

```powershell
.venv\Scripts\python -m pytest        # the integration and sandbox tests drive real FreeCAD
cargo test --release --manifest-path host\Cargo.toml
cargo test --release --manifest-path rules\Cargo.toml
cd web; npm test
```

Current counts on a Windows checkout: Python 728 passed (16 skipped where FreeCAD or the platform
sandbox is unavailable), host 53, rules 26, web 107.

`tests\freecad_panel_live.py` is a manual live check, not part of pytest: it opens a real FreeCAD
window, joins a running team as a person, sends `@team ...` through the panel, waits for the host to
plan, build and review it, and exercises the CLI picker. Pass it an output folder, the joined project
folder and the host data folder; it writes `freecad-report.json` and screenshots of each step.

## Known limits (hackathon MVP)

- Hand edits go through **✎ Edit a part**: sizes of primitives (box, cylinder, sphere, cone, torus) and
  position/rotation of any part. Sketch-based or fused parts can be moved but not reshaped by hand; ask a
  helper for those.
- `run_part_script` is sandboxed (see "Model scripts run in a sandbox"), but how well depends on the
  platform: strong on Windows with AppContainers, much weaker on macOS/Linux and in the Windows fallback.
  Each script also pays about half a second to start its own FreeCAD process. The sandbox protects your
  files and network, not the model: a script can still return any shape, so review what teammates' helpers
  add. Scripts can't edit other objects; they can only create or replace one shape.
- Locks are per object. Editing a shared upstream object (e.g. a sketch used by two features)
  requires its lock; there is no sub-object locking.
- One host per data folder, and each host drives a single FreeCAD worker, so ops run one at a time.
  Agents work in parallel, but their edits queue at the host.
- `scripts\demo-up.ps1` uses a Cloudflare quick tunnel, whose URL changes on every run; mint new phone
  links after a restart, or pass `-WebUrl` for a fixed address.
- Maincloud handles TLS and sign-in, and every person has their own identity; invites are single-use
  and expire after a day. A team hosted on this computer has no TLS, so use it only with people you
  trust on your own network.
- The host's computer serves the model, files, exports and snapshots. If it goes offline, teammates can
  still read the team's state from SpacetimeDB, but the model stops changing until the host is back.

## Structural optimization

The FreeCAD workbench includes **Optimize Part…** for simulation-guided material removal from supported grid-aligned solids. It runs real CalculiX or explicitly selected SciPy analysis, preserves the original, and shares a new part only after acceptance. See [installation, usage and limits](docs/structural-optimization.md) and the [reproducible numerical demonstration](demo/structural/README.md).
