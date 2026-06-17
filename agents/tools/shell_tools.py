"""Python port of ``agents/src/tools/shell-tools.ts``."""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from config import CONFIG


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int


ALLOWED_COMMANDS = {
    "npm", "npx", "node", "tsc", "mvn", "mvnw",
    "git", "gh", "java", "javac",
    "ls", "dir", "cat", "head", "tail", "find", "grep",
    "echo", "pwd", "which", "where",
}

_BLOCKED_PATTERNS = [
    re.compile(r"rm\s+-rf\s+/"),
    re.compile(r"format\s+[a-z]:", re.IGNORECASE),
    re.compile(r"del\s+/s", re.IGNORECASE),
    re.compile(r"rmdir\s+/s", re.IGNORECASE),
]


def _validate_command(command: str) -> None:
    for pattern in _BLOCKED_PATTERNS:
        if pattern.search(command):
            raise ValueError(f"Blocked dangerous command pattern: {command}")


async def run_command(
    command: str,
    cwd: str | None = None,
    timeout_ms: int = 60_000,
) -> CommandResult:
    _validate_command(command)

    work_dir = (
        Path(CONFIG.project_root / cwd).resolve() if cwd else CONFIG.project_root
    )

    def _run() -> CommandResult:
        is_windows = sys.platform == "win32"
        shell = "cmd.exe" if is_windows else "/bin/sh"
        shell_flag = "/c" if is_windows else "-c"
        try:
            proc = subprocess.run(
                [shell, shell_flag, command],
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                timeout=timeout_ms / 1000,
            )
            return CommandResult(
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
                exit_code=proc.returncode,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                stdout=exc.stdout or "" if isinstance(exc.stdout, str) else "",
                stderr=f"Command timed out after {timeout_ms}ms",
                exit_code=1,
            )

    return await asyncio.to_thread(_run)


async def run_validation() -> dict:
    errors: list[str] = []

    typecheck = await run_command("npm run typecheck", timeout_ms=30_000)
    if typecheck.exit_code != 0:
        errors.append(f"TypeScript errors:\n{typecheck.stdout}\n{typecheck.stderr}")

    lint = await run_command("npm run lint", timeout_ms=30_000)
    if lint.exit_code != 0:
        errors.append(f"Lint errors:\n{lint.stdout}\n{lint.stderr}")

    mvn_command = (
        "mvnw.cmd compile -q" if sys.platform == "win32" else "./mvnw compile -q"
    )
    mvn_compile = await run_command(mvn_command, cwd="server", timeout_ms=120_000)
    if mvn_compile.exit_code != 0:
        errors.append(
            f"Maven compile errors:\n{mvn_compile.stdout}\n{mvn_compile.stderr}"
        )

    return {"passed": len(errors) == 0, "errors": errors}


SHELL_TOOL_DEFINITION = {
    "name": "run_command",
    "description": (
        "Run a shell command in the project. Use for checking types, running tests, "
        "listing processes, etc. Commands that delete files or access the network "
        "(besides localhost) are blocked."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to run, e.g. 'npm run typecheck'",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory relative to project root. Defaults to project root.",
            },
        },
        "required": ["command"],
    },
}


async def execute_shell_tool(tool_input: dict) -> str:
    result = await run_command(tool_input["command"], cwd=tool_input.get("cwd"))
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return f"Exit code: {result.exit_code}\n{output}".strip()
