"""Source orchestrator ``Task``s from Jira.

This bridges the orchestrator's ``Task`` model to the ``graph`` package, which
talks to Jira through the Atlassian MCP server. We reuse that integration
instead of duplicating it.
"""

from __future__ import annotations

from graph.jira import (
    create_ticket,
    list_spaces,
    list_tickets,
    report_back,
    select_ticket,
)

from agent_types import Board, Epic, Group, Space, Task, Ticket

# Jira priority names -> the orchestrator's integer priority (lower = higher).
_PRIORITY_MAP = {"highest": 1, "high": 2, "medium": 3, "low": 4, "lowest": 5}


def _priority_to_int(name: object) -> int:
    if not name:
        return 3
    return _PRIORITY_MAP.get(str(name).strip().lower(), 3)


def _format_description(ticket: dict) -> str:
    description = (ticket.get("description") or "").strip()
    criteria = ticket.get("acceptance_criteria") or []
    if criteria:
        criteria_block = "\n".join(f"- {c}" for c in criteria)
        description = f"{description}\n\nAcceptance criteria:\n{criteria_block}".strip()
    return description


async def fetch_task_list(limit: int = 20) -> list[Task]:
    """Return a lightweight list of tickets for the interactive selection menu."""
    tickets = await list_tickets(limit)
    return [
        Task(
            id=t.get("key"),
            title=t.get("summary", ""),
            description=t.get("summary", ""),  # enriched on selection
            status="pending",
            priority=_priority_to_int(t.get("priority")),
        )
        for t in tickets
        if t.get("key")
    ]


def _ticket_from_dict(ticket: dict) -> Ticket:
    return Ticket(
        key=ticket.get("key", ""),
        summary=ticket.get("summary", ""),
        issue_type=ticket.get("issue_type", ""),
        status=ticket.get("status", ""),
        status_category=ticket.get("status_category", ""),
        priority=ticket.get("priority", ""),
        labels=ticket.get("labels", []) or [],
        epic_key=ticket.get("epic_key"),
        epic_name=ticket.get("epic_name"),
    )


def _epic_from_dict(epic: dict) -> Epic:
    return Epic(
        name=epic.get("name", ""),
        key=epic.get("key"),
        tickets=[_ticket_from_dict(t) for t in epic.get("tickets", []) if t.get("key")],
    )


def _group_from_dict(group: dict) -> Group:
    return Group(
        name=group.get("name", ""),
        group_type=group.get("group_type", "board"),
        state=group.get("state"),
        sprint_id=group.get("sprint_id"),
        epics=[_epic_from_dict(e) for e in group.get("epics", [])],
    )


def _board_from_dict(board: dict) -> Board:
    return Board(
        board_id=board.get("board_id", ""),
        board_name=board.get("board_name", ""),
        board_type=board.get("board_type", ""),
        groups=[_group_from_dict(g) for g in board.get("groups", [])],
    )


async def fetch_spaces(
    per_group_limit: int = 50,
    project_key: str | None = None,
    include_closed_sprints: bool = False,
) -> list[Space]:
    """Return the full ``space -> board -> group -> epic -> ticket`` hierarchy.

    Maps the nested dicts from :func:`list_spaces` into the typed
    :class:`Space` model, keeping empty containers so the browser can show the
    complete structure.
    """
    raw_spaces = await list_spaces(
        per_group_limit=per_group_limit,
        project_key=project_key,
        include_closed_sprints=include_closed_sprints,
    )

    return [
        Space(
            space_key=space.get("space_key", ""),
            space_name=space.get("space_name", ""),
            space_id=space.get("space_id", ""),
            boards=[_board_from_dict(b) for b in space.get("boards", [])],
            loose_groups=[_group_from_dict(g) for g in space.get("loose_groups", [])],
        )
        for space in raw_spaces
    ]


async def fetch_task_detail(ticket_key: str) -> Task:
    """Fetch a single ticket with its full description and acceptance criteria."""
    ticket = await select_ticket(ticket_key)
    return Task(
        id=ticket.get("key", ticket_key),
        title=ticket.get("summary", ""),
        description=_format_description(ticket),
        status="pending",
        priority=_priority_to_int(ticket.get("priority")),
    )


async def report_status(
    ticket_key: str, comment: str, transition_to: str | None = None
) -> str:
    """Post a comment (and optionally transition) back to the Jira ticket."""
    return await report_back(ticket_key, comment, transition_to=transition_to)


async def create_task(
    *,
    project_key: str,
    summary: str,
    issue_type: str = "Task",
    description: str = "",
    priority: str | None = None,
    labels: list[str] | None = None,
    epic_key: str | None = None,
) -> dict:
    """Create a new Jira ticket and return ``{key, url, summary}``."""
    return await create_ticket(
        project_key=project_key,
        summary=summary,
        issue_type=issue_type,
        description=description,
        priority=priority,
        labels=labels,
        epic_key=epic_key,
    )
