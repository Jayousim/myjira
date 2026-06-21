"""Python port of ``agents/src/ui/cli.ts`` (chalk -> colorama, inquirer -> questionary)."""

from __future__ import annotations

import sys

import questionary
from colorama import Fore, Style
from colorama import init as colorama_init

from agent_types import Epic, Group, Plan, Space, StepResult, Task, Ticket

# Node/chalk emit UTF-8 natively; the Windows console defaults to cp1252, so
# force UTF-8 before colorama wraps the streams to keep the box-drawing glyphs.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

colorama_init()


def _c(text: str, *styles: str) -> str:
    return "".join(styles) + text + Style.RESET_ALL


def print_banner() -> None:
    print(_c("\n\u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557", Style.BRIGHT, Fore.CYAN))
    print(_c("\u2551   Joyalty Dev Agent Orchestrator      \u2551", Style.BRIGHT, Fore.CYAN))
    print(_c("\u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d\n", Style.BRIGHT, Fore.CYAN))


def print_task(task: Task) -> None:
    print(_c(f"\nTask: {task.title}", Style.BRIGHT, Fore.WHITE))
    print(_c(f"  ID: {task.id} | Priority: {task.priority}", Fore.LIGHTBLACK_EX))
    print(_c(f"  {task.description}\n", Fore.WHITE))


def print_plan(plan: Plan) -> None:
    print(_c("\n\u2550\u2550\u2550 Implementation Plan \u2550\u2550\u2550\n", Style.BRIGHT, Fore.YELLOW))
    print(_c(f"Summary: {plan.summary}\n", Fore.WHITE))

    for step in plan.steps:
        print(_c(f"  Step {step.step_number}: {step.title}", Style.BRIGHT, Fore.CYAN))
        print(_c(f"    {step.description}", Fore.WHITE))
        print(_c(f"    Files: {', '.join(step.files_to_touch)}", Fore.LIGHTBLACK_EX))
        print(_c(f"    Expected: {step.expected_outcome}", Fore.GREEN))
        print()


async def prompt_approval() -> str:
    answer = await questionary.select(
        "What would you like to do with this plan?",
        choices=[
            questionary.Choice("Approve \u2014 start implementation", value="approve"),
            questionary.Choice("Reject \u2014 skip this task", value="reject"),
            questionary.Choice("Edit \u2014 provide feedback to regenerate", value="edit"),
        ],
    ).ask_async()
    return answer


async def prompt_feedback() -> str:
    feedback = await questionary.text("Enter your feedback for the planner:").ask_async()
    return feedback or ""


async def prompt_task_selection(tasks: list[Task]) -> Task | str | None:
    if not tasks:
        print(_c("No pending tasks in the backlog.", Fore.YELLOW))

    choices: list[questionary.Choice | questionary.Separator] = [
        questionary.Choice(f"[{t.priority}] {t.title} ({t.id})", value=t.id)
        for t in tasks
    ]
    choices.append(questionary.Separator(" "))
    choices.append(questionary.Choice("\u002b Create a new ticket", value=CREATE_TICKET))
    choices.append(questionary.Choice("Exit", value="__exit__"))

    answer = await questionary.select("Select a task to work on:", choices=choices).ask_async()

    if answer == CREATE_TICKET:
        return CREATE_TICKET
    if answer == "__exit__" or answer is None:
        return None
    return next((t for t in tasks if t.id == answer), None)


def _group_header(group: Group) -> str:
    """A one-line label for a sprint/backlog/board, e.g. 'Sprint 5 (active) — 3 ticket(s)'."""
    badge = group.state or group.group_type
    label = f"{group.name} ({badge})" if badge else group.name
    return f"{label} \u2014 {group.ticket_count} ticket(s)"


def _ticket_label(ticket: Ticket, indent: str) -> str:
    priority = ticket.priority or "-"
    return f"{indent}[{priority}] {ticket.summary} ({ticket.key})"


def _append_epic_choices(
    epic: Epic,
    indent: str,
    by_key: dict[str, Ticket],
    choices: list,
) -> None:
    # Only show an epic header for real epics; orphan tickets render inline.
    ticket_indent = indent
    if not epic.is_orphan_bucket:
        suffix = f" ({epic.key})" if epic.key else ""
        choices.append(questionary.Separator(f"{indent}\u25c8 {epic.name}{suffix}"))
        ticket_indent = indent + "  "
    for ticket in epic.tickets:
        by_key[ticket.key] = ticket
        choices.append(
            questionary.Choice(_ticket_label(ticket, ticket_indent), value=ticket.key)
        )


async def prompt_space_selection(spaces: list[Space]) -> Space | str | None:
    """Step 1 of the browser: pick a space/project.

    Returns the chosen :class:`Space`, the :data:`CREATE_TICKET` sentinel when
    the user wants to create a new ticket, or ``None`` to exit.
    """
    if not spaces:
        print(_c("No spaces found in Jira.", Fore.YELLOW))

    choices: list[questionary.Choice | questionary.Separator] = []
    for space in spaces:
        label = (
            f"{space.space_name or space.space_key} ({space.space_key}) "
            f"\u2014 {space.ticket_count} ticket(s)"
        )
        choices.append(questionary.Choice(label, value=space.space_key))
    choices.append(questionary.Separator(" "))
    choices.append(questionary.Choice("\u002b Create a new ticket", value=CREATE_TICKET))
    choices.append(questionary.Choice("Exit", value="__exit__"))

    answer = await questionary.select("Select a space:", choices=choices).ask_async()

    if answer == CREATE_TICKET:
        return CREATE_TICKET
    if answer == "__exit__" or answer is None:
        return None
    return next((s for s in spaces if s.space_key == answer), None)


async def prompt_ticket_in_space(space: Space) -> Ticket | None:
    """Step 2 of the browser: pick a ticket within one space's tree.

    Renders the board -> group -> epic hierarchy for a single space (so the list
    stays short and headers don't scroll out of view). Returns None to go back
    to the space list.
    """
    by_key: dict[str, Ticket] = {}
    choices: list[questionary.Choice | questionary.Separator] = []

    choices.append(
        questionary.Separator(
            f"\u2550\u2550 {space.space_name or space.space_key} ({space.space_key})"
        )
    )
    for board in space.boards:
        choices.append(
            questionary.Separator(
                f"  \u2500 {board.board_name or 'Board'} ({board.board_type})"
            )
        )
        for group in board.groups:
            choices.append(questionary.Separator(f"    \u00b7 {_group_header(group)}"))
            for epic in group.epics:
                _append_epic_choices(epic, "      ", by_key, choices)
    for group in space.loose_groups:
        choices.append(questionary.Separator(f"  \u00b7 {_group_header(group)}"))
        for epic in group.epics:
            _append_epic_choices(epic, "    ", by_key, choices)

    choices.append(questionary.Separator(" "))
    choices.append(questionary.Choice("\u2190 Back to spaces", value="__back__"))

    answer = await questionary.select(
        f"Select a ticket in {space.space_key}:", choices=choices
    ).ask_async()

    if answer == "__back__" or answer is None:
        return None
    return by_key.get(answer)


async def prompt_continue() -> bool:
    return bool(await questionary.confirm("Continue to next task?").ask_async())


# The constant for the "create a new ticket" menu entry; the runner matches on
# this to branch into the create flow instead of selecting a space/ticket.
CREATE_TICKET = "__create_ticket__"


async def prompt_new_ticket(default_project_key: str | None = None) -> dict | None:
    """Collect the fields for a new Jira ticket.

    Returns a dict suitable for ``jira_source.create_task(**details)`` or
    ``None`` if the user cancelled (or omitted a required field).
    """
    project_key = await questionary.text(
        "Project key (e.g. PROJ):", default=default_project_key or ""
    ).ask_async()
    if not project_key or not project_key.strip():
        print(_c("  Cancelled \u2014 a project key is required.", Fore.YELLOW))
        return None

    summary = await questionary.text("Summary (title):").ask_async()
    if not summary or not summary.strip():
        print(_c("  Cancelled \u2014 a summary is required.", Fore.YELLOW))
        return None

    issue_type = await questionary.select(
        "Issue type:",
        choices=["Task", "Story", "Bug", "Epic"],
        default="Task",
    ).ask_async()
    if issue_type is None:
        return None

    description = await questionary.text(
        "Description (optional, Markdown \u2014 leave empty to skip):",
        multiline=True,
    ).ask_async()

    priority = await questionary.select(
        "Priority:",
        choices=[
            questionary.Choice("(leave unset)", value=""),
            "Highest",
            "High",
            "Medium",
            "Low",
            "Lowest",
        ],
        default="",
    ).ask_async()

    labels_raw = await questionary.text(
        "Labels (comma-separated, optional):"
    ).ask_async()
    labels = [label.strip() for label in (labels_raw or "").split(",") if label.strip()]

    epic_key = await questionary.text(
        "Parent epic key (optional, e.g. PROJ-1):"
    ).ask_async()
    epic_key = (epic_key or "").strip().upper() or None

    return {
        "project_key": project_key.strip().upper(),
        "summary": summary.strip(),
        "issue_type": issue_type,
        "description": (description or "").strip(),
        "priority": priority or None,
        "labels": labels,
        "epic_key": epic_key,
    }


async def prompt_confirm_create(details: dict) -> bool:
    """Show a summary of the pending ticket and ask for final confirmation."""
    print(_c("\n  New ticket", Style.BRIGHT, Fore.WHITE))
    print(_c(f"    Project:  {details['project_key']}", Fore.WHITE))
    print(_c(f"    Type:     {details['issue_type']}", Fore.WHITE))
    print(_c(f"    Summary:  {details['summary']}", Fore.WHITE))
    if details.get("priority"):
        print(_c(f"    Priority: {details['priority']}", Fore.WHITE))
    if details.get("labels"):
        print(_c(f"    Labels:   {', '.join(details['labels'])}", Fore.WHITE))
    if details.get("epic_key"):
        print(_c(f"    Epic:     {details['epic_key']}", Fore.WHITE))
    if details.get("description"):
        print(_c(f"    Description:\n      {details['description']}", Fore.LIGHTBLACK_EX))
    print()
    return bool(await questionary.confirm("Create this ticket in Jira?").ask_async())


async def prompt_implement_new_ticket() -> bool:
    return bool(
        await questionary.confirm("Plan & implement this new ticket now?").ask_async()
    )


def print_created_ticket(result: dict) -> None:
    key = result.get("key") or "(unknown key)"
    print(_c(f"\n\u2713 Created {key}", Style.BRIGHT, Fore.GREEN))
    if result.get("summary"):
        print(_c(f"  {result['summary']}", Fore.WHITE))
    if result.get("url"):
        print(_c(f"  {result['url']}", Fore.CYAN))
    print()


def print_step_start(step_number: int, title: str) -> None:
    print(_c(f"\n\u25b6 Step {step_number}: {title}", Style.BRIGHT, Fore.BLUE))


def print_step_result(result: StepResult) -> None:
    if result.success:
        print(_c(f"  \u2713 Step {result.step_number} completed: {result.message}", Fore.GREEN))
        if result.files_changed:
            print(_c(f"    Changed: {', '.join(result.files_changed)}", Fore.LIGHTBLACK_EX))
    else:
        print(_c(f"  \u2717 Step {result.step_number} failed: {result.message}", Fore.RED))


def print_validation_result(passed: bool, errors: list[str]) -> None:
    if passed:
        print(_c("  \u2713 Validation passed", Fore.GREEN))
    else:
        print(_c("  \u2717 Validation failed:", Fore.RED))
        for err in errors:
            print(_c(f"    {err[:200]}", Fore.RED))


def print_progress(current: int, total: int) -> None:
    pct = round((current / total) * 100) if total else 0
    filled = round(pct / 5)
    bar = "\u2588" * filled + "\u2591" * (20 - filled)
    print(_c(f"  Progress: [{bar}] {pct}% ({current}/{total})", Fore.CYAN))


def print_done() -> None:
    print(_c("\n\u2713 All steps completed successfully!\n", Style.BRIGHT, Fore.GREEN))


def print_error(message: str) -> None:
    print(_c(f"\n\u2717 Error: {message}\n", Style.BRIGHT, Fore.RED))
