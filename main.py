"""CLI entrypoint for the dev agent (Jira-sourced).

Lists tickets from Jira, lets you pick one interactively, then runs the
orchestrator pipeline: plan -> approve -> implement -> review (validation) ->
commit + PR.

    python main.py [--task <JIRA-KEY>] [--dry-run] [--skip-git]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# The orchestrator and its siblings (config, ui.cli, agents.planner, ...) are
# written as top-level imports rooted at the ``agents/`` directory. Put it first
# on sys.path so those modules resolve and ``import agents`` finds the
# ``agents/agents`` subpackage rather than the ``agents/`` folder itself.
_AGENTS_DIR = Path(__file__).resolve().parent / "agents"
if str(_AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENTS_DIR))

from colorama import Fore, Style  # noqa: E402
from colorama import init as colorama_init  # noqa: E402

from config import CONFIG  # noqa: E402
from orchestrator import execute_task, load_task, load_tasks  # noqa: E402
from ui.cli import (  # noqa: E402
    print_banner,
    print_error,
    prompt_continue,
    prompt_task_selection,
)

colorama_init()

# Node/chalk emit UTF-8 natively; the Windows console defaults to cp1252, so
# force UTF-8 to keep the box-drawing glyphs in the banner/progress bars.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _gray(text: str) -> str:
    return Fore.LIGHTBLACK_EX + text + Style.RESET_ALL


def _parse_args() -> tuple[str | None, bool, bool]:
    args = sys.argv[1:]
    task_id: str | None = None
    dry_run = False
    skip_git = False

    i = 0
    while i < len(args):
        if args[i] == "--task" and i + 1 < len(args):
            i += 1
            task_id = args[i]
        elif args[i] == "--dry-run":
            dry_run = True
        elif args[i] == "--skip-git":
            skip_git = True
        elif args[i] == "--help":
            print(
                """
Usage: python main.py [options]

Options:
  --task <key>   Run a specific Jira ticket by key (e.g. PROJ-123)
  --dry-run      Generate plan only, no implementation
  --skip-git     Skip git branch/commit/PR operations
  --help         Show this help message
"""
            )
            sys.exit(0)
        i += 1

    return task_id, dry_run, skip_git


async def run(task_id: str | None, dry_run: bool, skip_git: bool) -> None:
    print_banner()

    if not CONFIG.anthropic_api_key:
        print_error(
            "ANTHROPIC_API_KEY is not set. Export it as an environment variable:\n"
            "  export ANTHROPIC_API_KEY=sk-ant-..."
        )
        # (continue anyway, mirroring the TS which leaves exit commented out)

    print(_gray(f"  Project root: {CONFIG.project_root}"))
    print(_gray(f"  Planner model: {CONFIG.planner_model}"))
    print(_gray(f"  Implementer model: {CONFIG.implementer_model}\n"))

    if dry_run:
        print(Fore.CYAN + "  Running in dry-run mode (plan only)\n" + Style.RESET_ALL)

    # A specific key skips the interactive browser and runs that ticket directly.
    if task_id:
        try:
            task = await load_task(task_id)
        except Exception as error:  # noqa: BLE001 - surface Jira/MCP errors
            print_error(f'Could not fetch Jira ticket "{task_id}": {error}')
            sys.exit(1)
        await execute_task(task, dry_run=dry_run, skip_git=skip_git)
        return

    print(_gray("  Fetching tickets from Jira...\n"))
    try:
        tasks = await load_tasks()
    except Exception as error:  # noqa: BLE001 - surface Jira/MCP errors
        print_error(f"Could not fetch tickets from Jira: {error}")
        sys.exit(1)

    continue_loop = True
    while continue_loop:
        remaining = [t for t in tasks if t.status == "pending"]
        selected = await prompt_task_selection(remaining)

        if not selected:
            break

        # The list view only carries the summary; fetch the full ticket now.
        try:
            task = await load_task(selected.id)
        except Exception as error:  # noqa: BLE001 - fall back to the summary
            print_error(f"Could not load ticket {selected.id}: {error}")
            task = selected

        success = await execute_task(task, dry_run=dry_run, skip_git=skip_git)
        selected.status = "completed" if success else "failed"

        still_pending = [t for t in tasks if t.status == "pending"]
        if not still_pending:
            print(Fore.GREEN + "\n  No more pending tickets!\n" + Style.RESET_ALL)
            break

        continue_loop = await prompt_continue()

    print(_gray("\n  Goodbye!\n"))


def main() -> None:
    task_id, dry_run, skip_git = _parse_args()
    try:
        asyncio.run(run(task_id, dry_run, skip_git))
    except Exception as error:  # noqa: BLE001 - top-level guard mirroring TS
        print_error(str(error))
        sys.exit(1)


if __name__ == "__main__":
    main()
