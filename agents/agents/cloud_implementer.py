"""Cursor cloud coding-agent implementer.

An alternative to the local ``implement_step`` loop: instead of editing files in
this repo, it runs the Cursor cloud agent against a remote target repo and lets
it open a PR automatically. Selected via ``IMPLEMENTER_MODE=cloud``.
"""

from __future__ import annotations

import asyncio

from cursor_sdk import (
    Agent,
    AgentOptions,
    CloudAgentOptions,
    CloudRepository,
    CursorAgentError,
)

from graph.config import config


def _build_prompt(ticket: dict, plan: str) -> str:
    criteria = ticket.get("acceptance_criteria") or []
    criteria_block = (
        "\n".join(f"- {c}" for c in criteria) if criteria else "(none specified)"
    )
    return (
        f"Implement Jira ticket {ticket.get('key')} ({ticket.get('issue_type')}).\n\n"
        f"Summary: {ticket.get('summary')}\n\n"
        f"Description:\n{ticket.get('description')}\n\n"
        f"Acceptance criteria:\n{criteria_block}\n\n"
        f"Approved implementation plan:\n{plan}\n\n"
        "Implement the change in this repository. Add or update tests that prove the "
        "fix/feature, run the test suite and build, and make sure they pass. "
        "Keep the change focused on this ticket. When done, open a pull request whose "
        f"description references {ticket.get('key')} and summarizes what changed and "
        "how it was verified."
    )


def _implement_sync(ticket: dict, plan: str) -> dict:
    config.require("cursor_api_key", "target_repo")

    repo = CloudRepository(
        url=config.target_repo,
        starting_ref=config.target_repo_ref,
    )
    options = AgentOptions(
        api_key=config.cursor_api_key,
        model=config.cursor_model,
        cloud=CloudAgentOptions(
            repos=[repo],
            auto_create_pr=True,
            skip_reviewer_request=True,
        ),
    )

    try:
        result = Agent.prompt(_build_prompt(ticket, plan), options)
    except CursorAgentError as err:
        # Run never started (auth / config / network).
        return {
            "ok": False,
            "stage": "startup",
            "error": str(err),
            "retryable": getattr(err, "is_retryable", None),
        }

    git = result.git
    pr_url = getattr(git, "pr_url", None) if git else None
    return {
        "ok": True,
        "run_id": result.run_id,
        "summary": result.result,
        "pr_url": pr_url,
        "git": str(git) if git else None,
    }


async def implement_ticket(ticket: dict, plan: str) -> dict:
    """Run the Cursor cloud coding agent without blocking the event loop."""
    return await asyncio.to_thread(_implement_sync, ticket, plan)
