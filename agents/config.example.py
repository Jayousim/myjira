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
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    planner_model: str = "claude-opus-4-8"
    implementer_model: str = "claude-opus-4-8"
    project_root: Path = _HERE.parent
    max_iterations_per_step: int = 10
    max_fix_attempts: int = 3
    backlog_path: Path = _HERE / "backlog" / "tasks.json"


CONFIG = _Config()
