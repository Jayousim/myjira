"""CLI entrypoint.

Thin bootstrap: sets up the import path, parses args, and hands off to the
Runner (agents/runner.py), which drives the langgraph orchestrator.

    python main.py [--task <JIRA-KEY>] [--dry-run] [--skip-git]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# The runner and its siblings (config, graph, ui.cli, agents.*, tools.*) are
# written as top-level imports rooted at the ``agents/`` directory. Put it first
# on sys.path so those resolve and ``import agents`` finds the ``agents/agents``
# subpackage rather than the ``agents/`` folder itself.
_AGENTS_DIR = Path(__file__).resolve().parent / "agents"
if str(_AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENTS_DIR))

from runner import run  # noqa: E402
from ui.cli import print_error  # noqa: E402


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


def main() -> None:
    task_id, dry_run, skip_git = _parse_args()
    try:
        asyncio.run(run(task_id, dry_run, skip_git))
    except Exception as error:  # noqa: BLE001 - top-level guard
        print_error(str(error))
        sys.exit(1)


if __name__ == "__main__":
    main()
