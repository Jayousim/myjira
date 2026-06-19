"""Interactive CLI runner that drives the langgraph orchestrator.

The graph (graph/graph.py) owns the flow; this Runner is the presentation layer
around it: it lists Jira tickets, renders the plan at the approval gate, collects
the human decision, and prints the outcome. The two connect through the graph's
interrupt/resume.
"""

from __future__ import annotations

import sys
import uuid

from colorama import Fore, Style
from colorama import init as colorama_init
from langgraph.types import Command

from agent_types import Board, Epic, Group, Space, Task
from config import CONFIG
from graph.graph import graph
from jira_source import fetch_spaces, fetch_task_list

from ui.cli import (
    print_banner,
    print_error,
    print_task,
    prompt_approval,
    prompt_continue,
    prompt_feedback,
    prompt_space_selection,
    prompt_task_selection,
    prompt_ticket_in_space,
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


def _filter_groups(groups: list[Group], handled: set[str]) -> list[Group]:
    """Drop already-handled tickets from each group, keeping empty containers."""
    result: list[Group] = []
    for group in groups:
        epics: list[Epic] = []
        for epic in group.epics:
            tickets = [t for t in epic.tickets if t.key not in handled]
            # Keep real epics as headers even when empty; drop a spent orphan bucket.
            if tickets or not epic.is_orphan_bucket:
                epics.append(Epic(name=epic.name, key=epic.key, tickets=tickets))
        result.append(
            Group(
                name=group.name,
                group_type=group.group_type,
                state=group.state,
                sprint_id=group.sprint_id,
                epics=epics,
            )
        )
    return result


def _without_handled(spaces: list[Space], handled: set[str]) -> list[Space]:
    """Rebuild the space tree without handled tickets (structure preserved)."""
    result: list[Space] = []
    for space in spaces:
        boards = [
            Board(
                board_id=board.board_id,
                board_name=board.board_name,
                board_type=board.board_type,
                groups=_filter_groups(board.groups, handled),
            )
            for board in space.boards
        ]
        result.append(
            Space(
                space_key=space.space_key,
                space_name=space.space_name,
                space_id=space.space_id,
                boards=boards,
                loose_groups=_filter_groups(space.loose_groups, handled),
            )
        )
    return result


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

    print(_gray("  Fetching tickets from Jira (space / board / sprint / epic)...\n"))
    spaces: list[Space] = []
    try:
        spaces = await fetch_spaces()
    except Exception as error:  # noqa: BLE001 - surface Jira/MCP errors
        print(_gray(f"  Hierarchical query unavailable ({error}); falling back to flat list.\n"))

    total = sum(s.ticket_count for s in spaces)
    if spaces and total:
        handled: set[str] = set()
        while True:
            space = await prompt_space_selection(_without_handled(spaces, handled))
            if space is None:
                break  # exit the browser

            # Drill into the chosen space until the user backs out or it empties.
            while True:
                current = next(
                    (
                        s
                        for s in _without_handled(spaces, handled)
                        if s.space_key == space.space_key
                    ),
                    None,
                )
                if current is None or current.ticket_count == 0:
                    print(_gray("  No more tickets in this space.\n"))
                    break

                selected = await prompt_ticket_in_space(current)
                if selected is None:
                    break  # back to the space list

                await run_ticket(selected.key, dry_run, skip_git)
                handled.add(selected.key)

                if len(handled) >= total:
                    print(Fore.GREEN + "\n  No more tickets in the list!\n" + Style.RESET_ALL)
                    print(_gray("\n  Goodbye!\n"))
                    return

                if not await prompt_continue():
                    print(_gray("\n  Goodbye!\n"))
                    return

        print(_gray("\n  Goodbye!\n"))
        return

    try:
        tasks = await fetch_task_list()
    except Exception as error:  # noqa: BLE001 - surface Jira/MCP errors
        print_error(f"Could not fetch tickets from Jira: {error}")
        sys.exit(1)

    handled = set()
    continue_loop = True
    while continue_loop:
        remaining_tasks = [t for t in tasks if t.id not in handled]
        selected = await prompt_task_selection(remaining_tasks)

        if not selected:
            break

        await run_ticket(selected.id, dry_run, skip_git)
        handled.add(selected.id)

        if len(handled) >= len(tasks):
            print(Fore.GREEN + "\n  No more tickets in the list!\n" + Style.RESET_ALL)
            break

        continue_loop = await prompt_continue()

    print(_gray("\n  Goodbye!\n"))
