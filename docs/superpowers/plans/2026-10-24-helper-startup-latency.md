# Plan: a helper's first second, not its seventh

Date: 2026-10-24
Status: proposed (no code yet)
Branch: a fresh branch off `main` after `feat/helper-latency` (#41) lands

## Why

#41 made *the team* stop wasting time: a runner notices a message in ~1 s instead of ~6 s, because it no
longer reads ten views per poll over fresh TLS connections. What is left is not the team. It is the helper
itself, and it is now the whole delay:

| Step | Cost | Where |
| --- | --- | --- |
| `claude -p` starts, loads settings/hooks, one model round trip | **~7.0 s** | measured: `claude --version` 1.6 s, `claude -p --max-turns 1` (no MCP) 7.0 s |
| the run's MCP server boots and rejoins the team | **~1.9 s** | measured: `initialize` answered 1.93 s after spawn (Python + imports + `rejoin`) |
| the model works, calling tools | 0.2-3.5 s per turn | 69 tool schemas + the instructions block ride along on every turn |
| the run's exit is noticed and the next poll runs | ~1 s | #41 |

The observed turns in a live helper log: a "read one message and ack it" run took **18 s**; real build runs
48 s to 3.6 min. So of a short reply, roughly 9 s is startup that happens on *every* message, including the
second message of a conversation.

## The two ways to win it back

### A. Keep one helper session warm (biggest win, real risks)

Today the helper is a batch job: one CLI run per wake-up, then exit. The foreground mode already does
something else: `wait_for_changes` (MCP) long-polls `/events` and returns new commands, so an agent can sit
in a loop and act on each message with no restart, no re-join, no re-read of the system prompt.

What it would look like in the runner: on the first wake, start the CLI once with a prompt whose job is "wait
for changes, do them, keep waiting"; on each new team event, the MCP server hands the waiting agent the news
(no new process). Keep the runner as the supervisor: it still owns the seat, rate limits and the stop file,
and it restarts the CLI only when it dies.

What has to be solved first, or this is a trap:

| Problem | Why it bites | Sketch |
| --- | --- | --- |
| A stuck helper holds the seat and the conversation | Today `RUN_TIMEOUT` (20 min) kills the *run*; a warm session is supposed to be long-lived, so "stuck" and "idle" look the same | Heartbeat: the MCP server records `last_tool_call`; the runner kills the CLI if the server's own heartbeat has not moved for 5 min *while* it claims to be working |
| Context grows forever | Each wake appends to one conversation; tool results are large (breps, FEA digests) | Compact at a turn or token budget: end the CLI, start a fresh one with a one-screen "state" note (the current `context()` already builds exactly that) |
| A person must be able to stop it | `stop_runner` writes a file the runner polls; it must also kill a warm child, and the helper must leave cleanly | Keep `runner.stop`; on stop, tell the child to exit (stdin close / SIGTERM), else kill; `ephemeral` agents already have this shape |
| Crash loops | A CLI that dies on every message must not spin | `MAX_RUNS_PER_HOUR` already covers this; keep it, count restarts too |
| Cost: a warm session is billed per event anyway | No change in tokens per event; only startup goes away | - |

Payoff: ~9 s off **every** message after the first, and no per-message fence bumps (the fence churn also
makes other processes see `fence_stale` briefly).

### B. Make the per-run startup cheaper (smaller, no risk)

If A is too much for now, these are independent and safe:

1. **Fewer tool schemas per turn.** 69 MCP tools are sent on every turn; the long descriptions are the
   helper's main input cost. Group rarely-used tools behind one dispatch tool, or split into two servers
   (`cadswarm` core / `cadswarm-heavy`: FEA, lattice, library). Measure tokens and TTFT before/after.
2. **Do not resume a huge conversation by default.** `--continue` reloads the whole history each run; a
   fresh conversation with the `context()` block is cheaper and keeps the helper on-task. Try
   `resume=False` for background helpers and compare answer quality on a fixed script of tasks.
3. **Skip the join on startup.** The MCP server must rejoin to take the seat, but it currently also imports
   the whole tool stack first; ordering imports (join first, tools lazily) would overlap ~0.5-1 s of the
   boot with the CLI's own start.

## Spike first (half a day, decides everything)

1. Time `claude -p` with `--verbose` on a warm machine: how much of the 7 s is process start, settings/hooks,
   MCP handshake, auth, first token?
2. Time a warm `wait_for_changes` loop: 5 messages 20 s apart, measure wake-to-first-tool-call each time.
   Expect ~0.3 s instead of ~9 s.
3. Time a turn with 69 tools vs 20 tools: input tokens and time-to-first-tool-call (the same prompt, a tool
   that exists in both).
4. Reproduce the "stuck helper" case on purpose: a prompt that sleeps in a tool call, and confirm the
   heartbeat supervisor catches it.

## If the spike says yes

- `cadswarm/runner.py`: `warm=True` path (`WARM_IDLE_SECONDS`, `WARM_TURNS`, restart-on-death); keep the
  batch path as a fallback and leave it the default until the supervisor has test coverage.
- `cadswarm/mcp_server.py`: `wait_for_changes` returns `commands` (already does), a `last_tool_call` stamp
  the runner can read, and a clean "stop after this" tool for a person's stop.
- Settings: one line in the helper's config (`"warm": true`) so a person can turn it off; the wizard keeps
  the current behaviour until we trust it.
- Tests: a fake CLI that stays alive and prints a marker per wake; supervisor kills an idle-but-"working"
  child; stop file ends a warm child; compaction triggers at the budget.

## Not in scope

- Making the model itself faster (model choice stays a per-helper setting).
- Streaming tokens into the panel: the panel shows "working" from events, not partial text.
