"""Source orchestrator ``Task``s from Jira.

This bridges the TS-ported orchestrator (which originally read a JSON backlog) to
the existing ``agent/`` package, which already talks to Jira through the Atlassian
MCP server. We reuse that integration instead of duplicating it.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The ``agent`` package lives at the repo root (one level above this ``agents``
# folder). Append it so ``import agent.jira`` resolves without shadowing the
# local ``agents`` subpackage that is already on sys.path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.append(str(_REPO_ROOT))

from agent.jira import list_tickets, report_back, select_ticket  # noqa: E402

from agent_types import Task  # noqa: E402

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
