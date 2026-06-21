"""Python port of ``agents/src/config.ts``."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# This file lives at <repo>/agents/config.py.
#   parent       -> <repo>/agents   (mirrors the TS "src" root)
#   parent.parent-> <repo>          (the project being worked on)

#_HERE = Path(__file__).resolve().parent
_HERE = 'C:/Users/J.Inc/Desktop/Branches/Joyalty/agents'

@dataclass(frozen=True)
class _Config:
    # "provider:model" strings consumed by llm.build_chat_model, e.g.
    # "anthropic:claude-opus-4-8", "openai:gpt-4o", "google_genai:gemini-1.5-pro".
    planner_model: str = os.getenv("PLANNER_MODEL", "anthropic:claude-opus-4-8")
    implementer_model: str = os.getenv("IMPLEMENTER_MODEL", "anthropic:claude-opus-4-8")
    project_root: Path = _HERE.parent
    max_iterations_per_step: int = 10
    max_fix_attempts: int = 3
    backlog_path: Path = _HERE / "backlog" / "tasks.json"


CONFIG = _Config()
