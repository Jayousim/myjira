"""Python port of ``agents/src/types.ts``."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Task:
    id: str
    title: str
    description: str
    status: str  # "pending" | "in_progress" | "completed" | "failed"
    priority: int


@dataclass
class PlanStep:
    step_number: int
    title: str
    description: str
    files_to_touch: list[str] = field(default_factory=list)
    expected_outcome: str = ""


@dataclass
class Plan:
    task_id: str
    summary: str
    steps: list[PlanStep] = field(default_factory=list)


@dataclass
class StepResult:
    step_number: int
    success: bool
    message: str
    files_changed: list[str] = field(default_factory=list)
