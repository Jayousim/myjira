"""Python port of ``agents/src/context/gatherer.ts``."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

from config import CONFIG


@dataclass
class ProjectContext:
    file_tree: str
    relevant_files: list[dict] = field(default_factory=list)  # [{"path", "content"}]


# Directory names and file suffixes to skip (mirrors the glob ignore list).
_IGNORE_DIRS = {
    "node_modules",
    ".git",
    "dist",
    "build",
    ".gradle",
    ".cxx",
    ".expo",
}
_IGNORE_SUFFIXES = {
    ".png",
    ".webp",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".jar",
    ".so",
    ".bin",
    ".lock",
}

_KEY_FILE_PATTERNS = [
    "app/**/*.tsx",
    "app/**/*.ts",
    "services/**/*.ts",
    "context/**/*.ts",
    "hooks/**/*.ts",
    "server/src/main/java/com/joyalty/server/**/*.java",
    "db/**/*.sql",
    "docker-compose.yml",
    "server/src/main/resources/application.properties",
]

_STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "shall", "can", "need", "must",
    "that", "this", "these", "those", "it", "its", "new", "add", "create",
    "implement", "build", "make", "update", "change", "modify",
}


def _is_ignored(rel_parts: tuple[str, ...], suffix: str) -> bool:
    if any(part in _IGNORE_DIRS for part in rel_parts):
        return True
    if suffix.lower() in _IGNORE_SUFFIXES:
        return True
    return False


def _get_file_tree(max_depth: int = 4) -> str:
    root = CONFIG.project_root
    entries: list[str] = []

    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if len(rel.parts) > max_depth:
            continue
        if _is_ignored(rel.parts, path.suffix):
            continue
        marker = "/" if path.is_dir() else ""
        entries.append(rel.as_posix() + marker)

    return "\n".join(sorted(entries))


def _extract_keywords(text: str) -> list[str]:
    keywords: list[str] = []
    for raw in re.split(r"[\s,.\-/;:()]+", text):
        if len(raw) <= 2 or raw.lower() in _STOP_WORDS:
            continue
        cleaned = re.sub(r"[^a-zA-Z0-9]", "", raw)
        if len(cleaned) > 2:
            keywords.append(cleaned)
    return keywords


def _find_relevant_files(task_description: str) -> list[str]:
    root = CONFIG.project_root
    keywords = _extract_keywords(task_description)
    candidates: set[str] = set()

    for pattern in _KEY_FILE_PATTERNS:
        for match in root.glob(pattern):
            if not match.is_file():
                continue
            rel = match.relative_to(root)
            if _is_ignored(rel.parts, match.suffix):
                continue
            candidates.add(rel.as_posix())

    if not keywords:
        return sorted(candidates)[:30]

    scored: list[tuple[str, int]] = []
    for file in candidates:
        score = 0
        lower = file.lower()
        for kw in keywords:
            if kw.lower() in lower:
                score += 2

        try:
            content = (root / file).read_text(encoding="utf-8")
            for kw in keywords:
                score += len(re.findall(kw, content, flags=re.IGNORECASE))
        except OSError:
            pass  # skip unreadable files

        scored.append((file, score))

    scored.sort(key=lambda s: s[1], reverse=True)
    return [s[0] for s in scored[:25]]


def _gather_context_sync(task_description: str) -> ProjectContext:
    file_tree = _get_file_tree()
    relevant_paths = _find_relevant_files(task_description)

    relevant_files: list[dict] = []
    for file_path in relevant_paths:
        try:
            content = (CONFIG.project_root / file_path).read_text(encoding="utf-8")
        except OSError:
            continue  # skip
        if len(content) >= 10_000:
            content = content[:10_000] + "\n... (truncated)"
        relevant_files.append({"path": file_path, "content": content})

    return ProjectContext(file_tree=file_tree, relevant_files=relevant_files)


async def gather_context(task_description: str) -> ProjectContext:
    return await asyncio.to_thread(_gather_context_sync, task_description)
