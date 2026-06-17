"""Python port of ``agents/src/agents/fixer.ts``."""

from __future__ import annotations

import asyncio

from anthropic import Anthropic

from config import CONFIG
from context.project_map import PROJECT_CONVENTIONS
from tools.file_tools import FILE_TOOL_DEFINITIONS, execute_file_tool
from tools.shell_tools import SHELL_TOOL_DEFINITION, execute_shell_tool, run_validation

ALL_TOOLS = [*FILE_TOOL_DEFINITIONS, SHELL_TOOL_DEFINITION]

FIXER_SYSTEM_PROMPT = f"""You are a senior developer fixing build/lint/type errors in the Joyalty coffee loyalty app.

{PROJECT_CONVENTIONS}

You have access to tools to read, write, edit files, and run commands.

Your job:
1. Analyze the error messages provided.
2. Read the files that have errors.
3. Fix the errors with minimal, targeted changes.
4. Do NOT change the intended functionality — only fix compilation/lint/type errors.
5. After making fixes, summarize what you changed."""


async def _execute_tool(name: str, tool_input: dict) -> str:
    if name == "run_command":
        return await execute_shell_tool(tool_input)
    return await execute_file_tool(name, tool_input)


async def fix_errors(errors: list[str], files_changed: list[str]) -> dict:
    client = Anthropic(api_key=CONFIG.anthropic_api_key)

    for attempt in range(CONFIG.max_fix_attempts):
        print(f"    Fix attempt {attempt + 1}/{CONFIG.max_fix_attempts}")

        error_block = "\n\n---\n\n".join(errors)
        user_message = f"""The following errors occurred after implementing changes to these files: {', '.join(files_changed)}

## Errors
{error_block}

Please fix these errors. Read the problematic files first, then make targeted edits."""

        messages: list[dict] = [{"role": "user", "content": user_message}]

        for _iter in range(8):
            response = await asyncio.to_thread(
                client.messages.create,
                model=CONFIG.implementer_model,
                max_tokens=4096,
                system=FIXER_SYSTEM_PROMPT,
                tools=ALL_TOOLS,
                messages=messages,
            )

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason == "tool_use":
                tool_use_blocks = [c for c in response.content if c.type == "tool_use"]
                messages.append({"role": "assistant", "content": response.content})

                tool_results: list[dict] = []
                for block in tool_use_blocks:
                    print(f"      \u2192 {block.name}({block.input.get('path', '')})")
                    try:
                        result = await _execute_tool(block.name, block.input)
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result[:50_000],
                            }
                        )
                    except Exception as error:  # noqa: BLE001 - mirror TS catch
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": f"Error: {error}",
                                "is_error": True,
                            }
                        )
                messages.append({"role": "user", "content": tool_results})

        revalidation = await run_validation()
        if revalidation["passed"]:
            print("    \u2713 Fixes verified \u2014 validation passes")
            return {"fixed": True, "remaining_errors": []}

        errors = revalidation["errors"]
        print(f"    Still {len(errors)} error(s) remaining")

    return {"fixed": False, "remaining_errors": errors}
