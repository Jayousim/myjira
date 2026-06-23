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
from graph.config import config as jira_config
from graph.graph import graph
from jira_source import create_task, fetch_spaces, fetch_task_list
from llm import missing_provider_keys

from ui.cli import (
    CREATE_TICKET,
    print_banner,
    print_created_ticket,
    print_error,
    print_step_review,
    print_task,
    prompt_approval,
    prompt_confirm_create,
    prompt_continue,
    prompt_feedback,
    prompt_implement_new_ticket,
    prompt_new_ticket,
    prompt_space_selection,
    prompt_step_review,
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
            warnings = result.get("warnings") or []
            if warnings:
                print(
                    Style.BRIGHT
                    + Fore.YELLOW
                    + f"\n\u26a0 {len(warnings)} step(s) finished with unresolved "
                    "validation errors (left for you to fix manually):"
                    + Style.RESET_ALL
                )
                for warning in warnings:
                    print(
                        Fore.YELLOW
                        + f"\n[{warning.get('stage')}] {warning.get('step_title', '')}".rstrip()
                        + Style.RESET_ALL
                    )
                    for error in warning.get("errors", []):
                        print(error)
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


async def _resume_value_for(payload: dict, dry_run: bool) -> dict | None:
    """Translate a graph interrupt into the human's resume decision.

    Returns the dict to feed back via ``Command(resume=...)``, or ``None`` to
    abort the run without resuming (e.g. a dry-run that only wanted the plan).
    """
    kind = payload.get("type")

    if kind == "step_review":
        print_step_review(payload)
        action = await prompt_step_review(bool(payload.get("step", {}).get("is_last")))
        return {"action": action}

    # Default / plan_approval: show the plan and collect approve/reject/edit.
    print_task(_ticket_to_task(payload["ticket"]))
    _print_plan(payload["plan"])

    if dry_run:
        print(Fore.CYAN + "\n  Dry run - plan only, no implementation.\n" + Style.RESET_ALL)
        return None

    decision = await prompt_approval()
    feedback = ""
    if decision == "edit":
        # The graph has no in-session re-plan loop, so feedback is submitted as a
        # revision request (posted back to the ticket) rather than regenerating
        # the plan here.
        feedback = await prompt_feedback()
    return {"approved": decision == "approve", "feedback": feedback}


async def run_ticket(
    ticket_key: str, dry_run: bool, skip_git: bool, review_steps: bool = False
) -> None:
    thread = {"configurable": {"thread_id": str(uuid.uuid4())}}

    # First leg: select -> plan -> (pauses at the approval interrupt).
    state = await graph.ainvoke(
        {
            "ticket_key": ticket_key,
            "skip_git": skip_git,
            "review_steps": review_steps,
        },
        config=thread,
    )

    # The graph can now pause more than once: first at plan approval, then after
    # each implemented step when --review-steps is on. Keep resuming until it
    # runs to completion (no more interrupts).
    while True:
        interrupts = state.get("__interrupt__")
        if not interrupts:
            break

        resume_value = await _resume_value_for(interrupts[0].value, dry_run)
        if resume_value is None:
            return  # dry-run: stop after showing the plan

        state = await graph.ainvoke(Command(resume=resume_value), config=thread)

    _print_outcome(state)


async def create_ticket_flow(
    dry_run: bool, skip_git: bool, review_steps: bool = False
) -> str | None:
    """Collect details, create a Jira ticket, and optionally run it immediately.

    Returns the created issue key (so the browser can mark it handled) or
    ``None`` if the user cancelled or creation failed.
    """
    details = await prompt_new_ticket(jira_config.jira_project_key)
    if not details:
        return None

    if not await prompt_confirm_create(details):
        print(_gray("  Ticket creation cancelled.\n"))
        return None

    try:
        result = await create_task(**details)
    except Exception as error:  # noqa: BLE001 - surface Jira/MCP errors
        print_error(f"Could not create the ticket in Jira: {error}")
        return None

    print_created_ticket(result)

    key = result.get("key")
    if key and await prompt_implement_new_ticket():
        await run_ticket(key, dry_run, skip_git, review_steps)
    return key


async def run(
    task_id: str | None,
    dry_run: bool,
    skip_git: bool,
    create: bool = False,
    review_steps: bool = False,
) -> None:
    print_banner()

    missing_keys = missing_provider_keys(
        CONFIG.planner_model, CONFIG.implementer_model, jira_config.planner_model
    )
    if missing_keys:
        print_error(
            "Missing API key(s) for the configured models: "
            + ", ".join(missing_keys)
            + ".\n  Set them as environment variables (e.g. in your .env), one per "
            "provider you use:\n"
            "    ANTHROPIC_API_KEY=sk-ant-...\n"
            "    OPENAI_API_KEY=sk-...\n"
            "    GOOGLE_API_KEY=..."
        )

    print(_gray(f"  Project root: {CONFIG.project_root}"))
    print(_gray(f"  Planner model: {CONFIG.planner_model}"))
    print(_gray(f"  Implementer model: {CONFIG.implementer_model}\n"))

    if dry_run:
        print(Fore.CYAN + "  Running in dry-run mode (plan only)\n" + Style.RESET_ALL)

    # A specific key skips the interactive browser and runs that ticket directly.
    if task_id:
        await run_ticket(task_id, dry_run, skip_git, review_steps)
        return

    # --create jumps straight into the new-ticket flow, no browsing.
    if create:
        await create_ticket_flow(dry_run, skip_git, review_steps)
        print(_gray("\n  Goodbye!\n"))
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
            if space == CREATE_TICKET:
                key = await create_ticket_flow(dry_run, skip_git, review_steps)
                if key:
                    handled.add(key)
                continue  # back to the space list

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

                await run_ticket(selected.key, dry_run, skip_git, review_steps)
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

        if selected == CREATE_TICKET:
            await create_ticket_flow(dry_run, skip_git, review_steps)
            continue_loop = await prompt_continue()
            continue

        await run_ticket(selected.id, dry_run, skip_git, review_steps)
        handled.add(selected.id)

        if len(handled) >= len(tasks):
            print(Fore.GREEN + "\n  No more tickets in the list!\n" + Style.RESET_ALL)
            break

        continue_loop = await prompt_continue()

    print(_gray("\n  Goodbye!\n"))
