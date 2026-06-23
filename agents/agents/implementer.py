"""Python port of ``agents/src/agents/implementer.ts``.

Implements a single plan step locally using the Anthropic tool-use loop (read /
write / edit files, run shell commands).
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from agent_types import PlanStep, StepResult
from config import CONFIG
from context.project_map import PROJECT_CONVENTIONS
from llm import build_chat_model, message_text, to_openai_tools
from tools.file_tools import FILE_TOOL_DEFINITIONS, execute_file_tool
from tools.shell_tools import SHELL_TOOL_DEFINITION, execute_shell_tool

ALL_TOOLS = [*FILE_TOOL_DEFINITIONS, SHELL_TOOL_DEFINITION]


def build_implementer_system_prompt() -> str:
    return f"""You are a senior full-stack developer implementing a single step of a larger plan for the Joyalty coffee loyalty app.

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
11. Focus on step, Don't fix redundant imports or code that is not related to the current step.

When you are done implementing the step, respond with a final message summarizing what you changed."""


IMPLEMENTER_SYSTEM_PROMPT = build_implementer_system_prompt()


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
    model = build_chat_model(CONFIG.implementer_model, max_tokens=8192).bind_tools(
        to_openai_tools(ALL_TOOLS)
    )
    files_changed: list[str] = []

    user_message = f"""## Current Step ({step.step_number})
**{step.title}**

{step.description}

**Files to touch:** {', '.join(step.files_to_touch)}
**Expected outcome:** {step.expected_outcome}

**Overall plan context:** {plan_summary}

Please implement this step now. Start by reading any existing files you need to understand, then make the necessary changes."""

    messages: list = [
        SystemMessage(content=IMPLEMENTER_SYSTEM_PROMPT),
        HumanMessage(content=user_message),
    ]

    for iteration in range(CONFIG.max_iterations_per_step):
        print(f"    iteration {iteration + 1}/{CONFIG.max_iterations_per_step}")

        response = await model.ainvoke(messages)
        messages.append(response)

        if not response.tool_calls:
            message = message_text(response) or "Step completed."
            return StepResult(
                step_number=step.step_number,
                success=True,
                message=message,
                files_changed=list(dict.fromkeys(files_changed)),
            )

        for call in response.tool_calls:
            name = call["name"]
            args = call["args"]
            print(f"      \u2192 {name}({_summarize_input(args)})")
            try:
                result = await _execute_tool(name, args)

                if name in ("write_file", "edit_file"):
                    files_changed.append(args["path"])

                messages.append(
                    ToolMessage(content=result[:50_000], tool_call_id=call["id"])
                )
            except Exception as error:  # noqa: BLE001 - mirror TS catch
                err_msg = str(error)
                print(f"      \u2717 {name} error: {err_msg[:100]}")
                messages.append(
                    ToolMessage(
                        content=f"Error: {err_msg}",
                        tool_call_id=call["id"],
                        status="error",
                    )
                )

    return StepResult(
        step_number=step.step_number,
        success=False,
        message=(
            f"Reached max iterations ({CONFIG.max_iterations_per_step}) "
            "without completing the step."
        ),
        files_changed=list(dict.fromkeys(files_changed)),
    )
