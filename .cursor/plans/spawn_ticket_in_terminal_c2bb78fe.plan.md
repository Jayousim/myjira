---
name: Spawn ticket in terminal
overview: Add an opt-in mode where selecting a Jira ticket spawns a new OS terminal running the existing single-ticket entrypoint (python main.py --task KEY), so the browser stays free and each ticket gets its own isolated, watchable lifecycle.
todos:
  - id: spawn-helper
    content: Add agents/spawn.py with cross-platform spawn_ticket_terminal() (Windows wt/start first, POSIX fallback), launching python main.py --task KEY detached.
    status: pending
  - id: entrypoint-flag
    content: Add --new-terminal flag to main.py _parse_args and thread it into runner.run().
    status: pending
  - id: wire-runner
    content: In runner.py run(), branch the space-browser and flat-list loops to spawn instead of awaiting run_ticket when new_terminal is set; still track handled and keep browsing.
    status: pending
  - id: local-safety-docs
    content: Document local-mode shared-working-tree hazard and guard against the spawned process re-spawning.
    status: pending
  - id: optional-worktree
    content: (Optional) Make project_root env-overridable in config.py and create a git worktree per ticket for safe parallel local runs.
    status: pending
isProject: false
---

# Spawn ticket plan/implement in a new terminal

## Verdict
Worth doing. It makes the Jira browser non-blocking and gives each ticket its own watchable window (own plan, approval prompt, logs). It's a small change because `main.py --task <KEY>` already runs the full lifecycle standalone. The one real hazard is local-mode git contention; cloud mode is parallel-safe.

## Key facts this leans on
- `[main.py](main.py)` already supports `python main.py --task <KEY> [--dry-run] [--skip-git]` and runs the whole graph for one ticket, including the human approval prompt.
- `[agents/runner.py](agents/runner.py)` currently calls `await run_ticket(...)` inline (sequential, blocking) inside both the space browser loop and the flat-list loop.
- Local implement (`implement_step` + `[agents/tools/git_tools.py](agents/tools/git_tools.py)`) edits one shared working tree at `CONFIG.project_root` and does `git checkout -b`. Parallel local runs would collide.
- `[agents/config.py](agents/config.py)` hardcodes `project_root`, so it is not env-overridable today (matters only for the optional worktree phase).

## Design
1. New helper `agents/spawn.py` with `spawn_ticket_terminal(ticket_key, dry_run, skip_git)`:
   - Builds command: `[sys.executable, <repo>/main.py, "--task", key, ...flags]`.
   - Windows-first: use `wt.exe` (new tab titled with the ticket key) when on PATH, else fall back to `cmd /c start "<key>" ...`. Include a generic POSIX fallback (`x-terminal-emulator` / `open -a Terminal`) guarded by `os.name`.
   - Launch detached via `subprocess.Popen` (no `await`), so the browser is not blocked.

2. Thread an opt-in flag through the entrypoint:
   - In `[main.py](main.py)` `_parse_args`, add `--new-terminal` (default off) and pass it into `run(...)`.
   - In `[agents/runner.py](agents/runner.py)` `run(...)`, accept `new_terminal: bool`.

3. Wire it into the loops in `[agents/runner.py](agents/runner.py)`:
   - Where it currently does `await run_ticket(selected.key, dry_run, skip_git)` (space browser ~line 231 and flat list ~line 261), branch: if `new_terminal`, call `spawn_ticket_terminal(...)`, print a "launched in new terminal" line, add the key to `handled`, and keep browsing; else keep the existing inline `await`.
   - The spawned process must NOT itself re-spawn, so it always runs `main.py --task` without `--new-terminal`.

## Local-mode safety (call out + optional phase)
- Default behavior: spawning is allowed, but document that parallel **local** runs share one working tree and will clobber each other; recommend `--new-terminal` mainly with cloud mode, or selecting one ticket at a time.
- Optional hardening (separate phase, only if local parallelism is required): make `project_root` env-overridable in `[agents/config.py](agents/config.py)`, create a `git worktree` per ticket, and pass its path to the spawned process via env so each terminal edits an isolated checkout.

## Out of scope
- No central dashboard of spawned runs; outcomes land in Jira via `report_node` as today.

## Verification
- `python main.py --task <KEY> --dry-run` still works unchanged.
- With `--new-terminal`, selecting a ticket opens a new terminal running the lifecycle while the browser immediately returns to the selection prompt; the selected key is not re-offered.