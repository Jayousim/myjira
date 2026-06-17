"""Python port of ``agents/src/tools/file-tools.ts``."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from config import CONFIG

_IGNORE_DIRS = {"node_modules", ".git", "dist", "build", ".gradle"}


def _resolve_path(file_path: str) -> Path:
    p = Path(file_path)
    if p.is_absolute():
        return p
    return (CONFIG.project_root / p).resolve()


def _assert_inside_project(resolved: Path) -> None:
    root = CONFIG.project_root.resolve()
    if not str(resolved).startswith(str(root)):
        raise ValueError(f'Path "{resolved}" is outside the project root.')


async def read_file(file_path: str) -> str:
    resolved = _resolve_path(file_path)
    _assert_inside_project(resolved)
    return await asyncio.to_thread(resolved.read_text, "utf-8")


async def write_file(file_path: str, content: str) -> str:
    resolved = _resolve_path(file_path)
    _assert_inside_project(resolved)

    def _write() -> None:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")

    await asyncio.to_thread(_write)
    return f"Wrote {len(content)} chars to {file_path}"


async def edit_file(file_path: str, old_string: str, new_string: str) -> str:
    resolved = _resolve_path(file_path)
    _assert_inside_project(resolved)
    content = await asyncio.to_thread(resolved.read_text, "utf-8")

    if old_string not in content:
        raise ValueError(
            f"old_string not found in {file_path}. Make sure it matches exactly "
            "(including whitespace)."
        )
    occurrences = content.count(old_string)
    if occurrences > 1:
        raise ValueError(
            f"old_string appears {occurrences} times in {file_path}. "
            "Provide more context to make it unique."
        )

    updated = content.replace(old_string, new_string, 1)
    await asyncio.to_thread(resolved.write_text, updated, "utf-8")
    return (
        f"Edited {file_path}: replaced {len(old_string)} chars "
        f"with {len(new_string)} chars"
    )


async def list_directory(dir_path: str) -> list[str]:
    resolved = _resolve_path(dir_path)
    _assert_inside_project(resolved)

    def _list() -> list[str]:
        entries = []
        for entry in os.scandir(resolved):
            entries.append(f"{entry.name}/" if entry.is_dir() else entry.name)
        return entries

    return await asyncio.to_thread(_list)


async def search_files(pattern: str, directory: str | None = None) -> list[str]:
    cwd = _resolve_path(directory) if directory else CONFIG.project_root.resolve()
    _assert_inside_project(cwd)

    def _search() -> list[str]:
        matches: list[str] = []
        for path in cwd.glob(pattern):
            if not path.is_file():
                continue
            if any(part in _IGNORE_DIRS for part in path.parts):
                continue
            matches.append(path.relative_to(cwd).as_posix())
            if len(matches) >= 100:
                break
        return matches[:100]

    return await asyncio.to_thread(_search)


FILE_TOOL_DEFINITIONS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file. Returns the full file content as a string.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from project root, e.g. 'app/auth/index.tsx'",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Creates parent directories if needed. Overwrites existing content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path from project root"},
                "content": {
                    "type": "string",
                    "description": "The full content to write to the file",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace a unique string in a file with a new string. The old_string must appear exactly once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path from project root"},
                "old_string": {
                    "type": "string",
                    "description": "The exact string to find and replace (must be unique in the file)",
                },
                "new_string": {"type": "string", "description": "The replacement string"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and directories in a directory. Directories end with '/'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from project root, e.g. 'server/src/main/java'",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_files",
        "description": "Search for files matching a glob pattern. Returns up to 100 matching file paths.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern, e.g. '**/*.tsx' or 'server/**/*.java'",
                },
                "directory": {
                    "type": "string",
                    "description": "Optional subdirectory to search within (relative to project root)",
                },
            },
            "required": ["pattern"],
        },
    },
]


async def execute_file_tool(name: str, tool_input: dict) -> str:
    if name == "read_file":
        return await read_file(tool_input["path"])
    if name == "write_file":
        return await write_file(tool_input["path"], tool_input["content"])
    if name == "edit_file":
        return await edit_file(
            tool_input["path"], tool_input["old_string"], tool_input["new_string"]
        )
    if name == "list_directory":
        entries = await list_directory(tool_input["path"])
        return "\n".join(entries)
    if name == "search_files":
        files = await search_files(tool_input["pattern"], tool_input.get("directory"))
        return "\n".join(files) if files else "No files found."
    raise ValueError(f"Unknown file tool: {name}")
