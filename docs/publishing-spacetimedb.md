# SpacetimeDB: publishing and updating the team database

CadSwarm keeps the shared model, members, locks, tasks and the op queue in a SpacetimeDB **module**
(`module/`). The coordination rules (`rules/`) are compiled into that module, so the database only accepts
the op kinds its module knows. **After any change to `module/` or `rules/`, republish**, or the new features
are refused with `unknown_op`.

What needs the republish right now (this branch):

| Feature | New op kinds / events the module must know |
|---|---|
| Lighten without losing stiffness | `lattice_capabilities`, `lattice_cells`, `lattice_estimate`, `lattice_start`, `lattice_status`, `lattice_list`, `lattice_cancel`, `lattice_result`, `insert_mesh`; events `lattice_started`, `lattice_progress`, `lattice_report`, `lattice_failed` |
| Learned design style | `style_context`, `style_inbox`, `list_style`, `teach_style`, `resolve_style`, `learn_style` |

The team database is **`cadswarm-calhacks-0180`** on Maincloud (the default in `scripts/demo-up.ps1`). The
steps below use PowerShell from the repository's `cadswarm/` folder.

Adding a helper client (`opencode`) needs **no republish and no new bindings**: a member's
`client` is a free-form string in the existing schema, and the new clients reuse the same MCP tools.

```powershell
$st = "$env:LOCALAPPDATA\SpacetimeDB\spacetime.exe"   # or just `spacetime` if it is on PATH
$db = "cadswarm-calhacks-0180"
```

---

## Step 1: Check you are the database's owner

Only the identity that created a database can publish to it.

```powershell
& $st login show                                                     # your identity
(Invoke-RestMethod "https://maincloud.spacetimedb.com/v1/database/$db").owner_identity   # the owner
```

- **They match:** go to step 2.
- **They differ:** sign in with the owner's account (`& $st login`, which opens a browser), or ask the
  person who owns it to do steps 2–5. If nobody can, create a new database under your own login (step 2 with
  a new name). Every teammate then needs a new invite, because invites name the database.
- **`Invalid token: InvalidSignature`** on any Maincloud command: your stored login is stale. Run
  `& $st login` again.

## Step 2: Build and publish (keeping the team's data)

```powershell
rustup target add wasm32-unknown-unknown      # once per machine
cargo build --release --target wasm32-unknown-unknown --manifest-path module\Cargo.toml
& $st publish --server maincloud --yes=all --delete-data=never `
    --bin-path module\target\wasm32-unknown-unknown\release\cadswarm_module.wasm  $db
```

- `--delete-data=never` keeps every table. If the new schema can't migrate automatically, the publish is
  **refused** and nothing is wiped. Read the message: it names the table that blocks the migration.
- **Always pass `--server`.** The CLI's default server differs between machines (`& $st server list`
  shows it); without the flag you can publish to the wrong place.
- Never use `--delete-data=always` on the team database: it erases the model, members and history.

## Step 3: Regenerate bindings (only if tables or reducers changed)

The host (Rust) and the web app (TypeScript) use generated bindings. New op kinds alone (the cases above)
don't need this; new tables, columns or reducers do.

```powershell
& $st generate --lang rust --out-dir host\src\module_bindings --module-path module
& $st generate --lang typescript --out-dir web\src\module_bindings --module-path module
git status host\src\module_bindings web\src\module_bindings   # commit any changes
```

To check without touching the repo, generate into a temporary folder and compare:
`& $st generate --lang rust --out-dir $env:TEMP\bind --module-path module --yes` then
`git diff --no-index --ignore-cr-at-eol $env:TEMP\bind host\src\module_bindings`.

## Step 4: Restart the team server (host) on the new build

The host must run code that matches the module. The demo script rebuilds and restarts everything. It picks up
an optional `cadswarm.env` (next to the checkout, outside git) when you keep demo-service keys there; without
one it runs on the helpers' own CLI sign-ins:

```powershell
powershell -File scripts\demo-down.ps1
powershell -File scripts\demo-up.ps1 -Db $db
```

Or by hand: `cargo build --release --manifest-path host\Cargo.toml`, then
`host\target\release\cadswarm-host.exe --data <data folder> run`.

## Step 5: Check it worked

```powershell
& $st sql --server maincloud $db "SELECT name, kind, status FROM v_member"
```

The host is listed as `active`. Then, in FreeCAD:

- **Tools tab › Lighten part…** opens for a selected solid, and **Check these settings** answers (not
  `unknown_op`);
- **Tools tab › Learned design style…** opens without an error;
- an AI helper's `lattice_capabilities` returns `available: true`.

Each teammate also updates their CadSwarm add-on (double-click **Install CadSwarm.cmd** again, then restart
FreeCAD), so their panel has the new commands.

---

## Optional: rehearse on a local database first

Everything above can be tried on a throwaway local SpacetimeDB without touching the team's data:

```powershell
# terminal 1
& $st start --listen-addr 127.0.0.1:3000
# terminal 2
& $st publish --server local --module-path module --delete-data=always --yes=all cadswarm-e2e
host\target\debug\cadswarm-host.exe --data $env:TEMP\cadswarm-e2e init --mode local --db cadswarm-e2e --python .venv\Scripts\python.exe
host\target\debug\cadswarm-host.exe --data $env:TEMP\cadswarm-e2e run
# terminal 3: the live tests (two agents coordinating, an op run by the host)
$env:CADSWARM_LIVE = "1"; $env:CADSWARM_DB = "cadswarm-e2e"; .venv\Scripts\python.exe -m pytest tests/test_space_live.py -q
```

`--delete-data=always` is fine here: it is a local, disposable database.

## Troubleshooting

| What you see | Cause and fix |
|---|---|
| `unknown_op: Unknown op "lattice_start"` (or another new op) | The database runs an older module: step 2. |
| `Invalid token: InvalidSignature` | Stale CLI login: `& $st login`. |
| `not authorized` / publish refused for ownership | You are not the owner: step 1. |
| Publish refused: a table can't be migrated | A schema change needs a manual migration; do not fall back to deleting data. Ask whoever changed `module/src/tables.rs`. |
| The host crashes at start with a bindings or deserialization error | Bindings and module are out of step: step 3, then rebuild the host. |
| Teammates don't see new buttons | They need the updated add-on (reinstall) and a FreeCAD restart. |
| A helper stops at once with "Incorrect API key" | The CLI it runs was never signed in: open `claude`, `codex` or `opencode` once on the host's computer and sign in. No key or env file belongs in the repo. |
