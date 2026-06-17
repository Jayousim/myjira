"""CLI entrypoint — interactive front-end driving the langgraph orchestrator.

The graph (agents/graph/graph.py) owns the flow: select -> plan -> approval ->
implement -> review -> report. This CLI is only the presentation layer: it lists
Jira tickets, renders the plan at the approval gate, collects the human decision,
and prints the outcome. The two connect through the graph's interrupt/resume.

    python main.py [--task <JIRA-KEY>] [--dry-run] [--skip-git]
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

# The graph and its siblings (config, ui.cli, agents.*, tools.*) are written as
# top-level imports rooted at the ``agents/`` directory. Put it first on sys.path
# so those resolve and ``import agents`` finds the ``agents/agents`` subpackage.
_AGENTS_DIR = Path(__file__).resolve().parent / "agents"
if str(_AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENTS_DIR))

from colorama import Fore, Style  # noqa: E402
from colorama import init as colorama_init  # noqa: E402
from langgraph.types import Command  # noqa: E402

from agent_types import Task  # noqa: E402
from config import CONFIG  # noqa: E402
from graph.graph import graph  # noqa: E402
from jira_source import fetch_task_list  # noqa: E402
from ui.cli import (  # noqa: E402
    print_banner,
    print_error,
    print_task,
    prompt_approval,
    prompt_continue,
    prompt_feedback,
    prompt_task_selection,
)

colorama_init()

# Node/chalk emit UTF-8 natively; the Windows console defaults to cp1252, so
# force UTF-8 to keep the box-drawing glyphs in the banner/plan output.
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


def _ticket_to_task(ticket: dict) -> Task:
    return Task(
        id=ticket.get("key", ""),
        title=ticket.get("summary", ""),
        description=(ticket.get("description") or "").strip(),
        status=ticket.get("status", "pending"),
        priority=3,
    )


def _print_plan(plan_text: str) -> None:
    print(Style.BRIGHT + Fore.YELLOW + "\n\u2550\u2550\u2550 Proposed plan \u2550\u2550\u2550\n" + Style.RESET_ALL)
    print(plan_text)
    print(Style.BRIGHT + Fore.YELLOW + "\n\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\n" + Style.RESET_ALL)


def _print_outcome(state: dict) -> None:
    result = state.get("result")
    if result:
        if result.get("ok"):
            print(
                Style.BRIGHT
                + Fore.GREEN
                + f"\n\u2713 Implemented. PR: {result.get('pr_url')}  (run {result.get('run_id')})"
                + Style.RESET_ALL
            )
        else:
            print(
                Style.BRIGHT
                + Fore.RED
                + f"\n\u2717 Implementation failed at {result.get('stage')}: {result.get('error')}"
                + Style.RESET_ALL
            )

    review = state.get("review")
    if review:
        print(Style.BRIGHT + Fore.MAGENTA + "\n--- Review notes ---" + Style.RESET_ALL)
        print(review)
        print(Style.BRIGHT + Fore.MAGENTA + "--------------------" + Style.RESET_ALL)

    if state.get("report"):
        print(Fore.CYAN + f"\nJira updated:\n{state['report']}" + Style.RESET_ALL)


async def run_ticket(ticket_key: str, dry_run: bool, skip_git: bool) -> None:
    thread = {"configurable": {"thread_id": str(uuid.uuid4())}}

    # First leg: select -> plan -> (pauses at the approval interrupt).
    state = await graph.ainvoke(
        {"ticket_key": ticket_key, "skip_git": skip_git}, config=thread
    )

    interrupts = state.get("__interrupt__")
    if interrupts:
        payload = interrupts[0].value
        print_task(_ticket_to_task(payload["ticket"]))
        _print_plan(payload["plan"])

        if dry_run:
            print(Fore.CYAN + "\n  Dry run - plan only, no implementation.\n" + Style.RESET_ALL)
            return

        decision = await prompt_approval()
        approved = decision == "approve"
        feedback = ""
        if decision == "edit":
            # The graph has no in-session re-plan loop, so feedback is submitted
            # as a revision request (posted back to the ticket) rather than
            # regenerating the plan here.
            feedback = await prompt_feedback()

        # Second leg: resume -> implement -> review -> report.
        state = await graph.ainvoke(
            Command(resume={"approved": approved, "feedback": feedback}),
            config=thread,
        )

    _print_outcome(state)


async def run(task_id: str | None, dry_run: bool, skip_git: bool) -> None:
    print_banner()

    if not CONFIG.anthropic_api_key:
        print_error(
            "ANTHROPIC_API_KEY is not set. Export it as an environment variable:\n"
            "  export ANTHROPIC_API_KEY=sk-ant-..."
        )

    print(_gray(f"  Project root: {CONFIG.project_root}"))
    print(_gray(f"  Planner model: {CONFIG.planner_model}"))
    print(_gray(f"  Implementer model: {CONFIG.implementer_model}\n"))

    if dry_run:
        print(Fore.CYAN + "  Running in dry-run mode (plan only)\n" + Style.RESET_ALL)

    # A specific key skips the interactive browser and runs that ticket directly.
    if task_id:
        await run_ticket(task_id, dry_run, skip_git)
        return

    print(_gray("  Fetching tickets from Jira...\n"))
    try:
        tasks = await fetch_task_list()
    except Exception as error:  # noqa: BLE001 - surface Jira/MCP errors
        print_error(f"Could not fetch tickets from Jira: {error}")
        sys.exit(1)

    handled: set[str] = set()
    continue_loop = True
    while continue_loop:
        remaining = [t for t in tasks if t.id not in handled]
        selected = await prompt_task_selection(remaining)

        if not selected:
            break

        await run_ticket(selected.id, dry_run, skip_git)
        handled.add(selected.id)

        if len(handled) >= len(tasks):
            print(Fore.GREEN + "\n  No more tickets in the list!\n" + Style.RESET_ALL)
            break

        continue_loop = await prompt_continue()

    print(_gray("\n  Goodbye!\n"))


def main() -> None:
    task_id, dry_run, skip_git = _parse_args()
    try:
        asyncio.run(run(task_id, dry_run, skip_git))
    except Exception as error:  # noqa: BLE001 - top-level guard
        print_error(str(error))
        sys.exit(1)


if __name__ == "__main__":
    main()
