import asyncio

from cursor_sdk import (
    Agent,
    AgentOptions,
    CloudAgentOptions,
    CloudRepository,
    CursorAgentError,
)

from .config import config



from __future__ import annotations

from anthropic import Anthropic

from agent_types import PlanStep, StepResult
from config import CONFIG
from context.project_map import PROJECT_CONVENTIONS
from tools.file_tools import FILE_TOOL_DEFINITIONS, execute_file_tool
from tools.shell_tools import SHELL_TOOL_DEFINITION, execute_shell_tool

ALL_TOOLS = [*FILE_TOOL_DEFINITIONS, SHELL_TOOL_DEFINITION]
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


IMPLEMENTER_SYSTEM_PROMPT = f"""You are a senior full-stack developer implementing a single step of a larger plan for the Joyalty coffee loyalty app.

{PROJECT_CONVENTIONS}

You have access to tools to read, write, and edit files, list directories, search for files, and run shell commands.

Rules:
1. Implement ONLY the current step. Do not implement future steps.
2. Read existing files before editing them to understand current state.
3. Use edit_file for surgical changes to existing files. Use write_file for new files.
4. Follow the existing code style and patterns found in the codebase.
5. After making changes, verify them by reading the changed files back.
6. If you need to understand the project structure, use list_directory and search_files.
7. Do NOT run destructive commands (rm -rf, drop database, etc.).
8. When creating Java files, follow the existing package structure: com.joyalty.server.*
9. When creating TypeScript files, use the existing import patterns and types.
10. Always include proper imports in new files.

When you are done implementing the step, respond with a final message summarizing what you changed."""


async def _execute_tool(name: str, tool_input: dict) -> str:
    if name == "run_command":
        return await execute_shell_tool(tool_input)
    return await execute_file_tool(name, tool_input)


def _summarize_input(tool_input: dict) -> str:
    if tool_input.get("path"):
        return tool_input["path"]
    if tool_input.get("command"):
        return tool_input["command"][:60]
    if tool_input.get("pattern"):
        return tool_input["pattern"]
    return ", ".join(tool_input.keys())


async def implement_step(step: PlanStep, plan_summary: str) -> StepResult:
    client = Anthropic(api_key=CONFIG.anthropic_api_key)
    files_changed: list[str] = []

    user_message = f"""## Current Step ({step.step_number})
**{step.title}**

{step.description}

**Files to touch:** {', '.join(step.files_to_touch)}
**Expected outcome:** {step.expected_outcome}

**Overall plan context:** {plan_summary}

Please implement this step now. Start by reading any existing files you need to understand, then make the necessary changes."""

    messages: list[dict] = [{"role": "user", "content": user_message}]

    for iteration in range(CONFIG.max_iterations_per_step):
        print(f"    iteration {iteration + 1}/{CONFIG.max_iterations_per_step}")

        response = await asyncio.to_thread(
            client.messages.create,
            model=CONFIG.implementer_model,
            max_tokens=8192,
            system=IMPLEMENTER_SYSTEM_PROMPT,
            tools=ALL_TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            text_block = next((c for c in response.content if c.type == "text"), None)
            message = text_block.text if text_block else "Step completed."
            return StepResult(
                step_number=step.step_number,
                success=True,
                message=message,
                files_changed=list(dict.fromkeys(files_changed)),
            )

        if response.stop_reason == "tool_use":
            tool_use_blocks = [c for c in response.content if c.type == "tool_use"]
            messages.append({"role": "assistant", "content": response.content})

            tool_results: list[dict] = []
            for block in tool_use_blocks:
                print(f"      \u2192 {block.name}({_summarize_input(block.input)})")
                try:
                    result = await _execute_tool(block.name, block.input)

                    if block.name in ("write_file", "edit_file"):
                        files_changed.append(block.input["path"])

                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result[:50_000],
                        }
                    )
                except Exception as error:  # noqa: BLE001 - mirror TS catch
                    err_msg = str(error)
                    print(f"      \u2717 {block.name} error: {err_msg[:100]}")
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Error: {err_msg}",
                            "is_error": True,
                        }
                    )

            messages.append({"role": "user", "content": tool_results})

    return StepResult(
        step_number=step.step_number,
        success=False,
        message=(
            f"Reached max iterations ({CONFIG.max_iterations_per_step}) "
            "without completing the step."
        ),
        files_changed=list(dict.fromkeys(files_changed)),
    )
