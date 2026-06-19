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
class Ticket:
    """A single Jira issue (the leaf of the hierarchy)."""

    key: str
    summary: str
    issue_type: str = ""
    status: str = ""
    status_category: str = ""  # "To Do" | "In Progress" | "Done"
    priority: str = ""  # priority name; "" if none
    labels: list[str] = field(default_factory=list)
    epic_key: str | None = None
    epic_name: str | None = None

    @property
    def is_done(self) -> bool:
        category = self.status_category.strip().lower()
        if category:
            return category == "done"
        return self.status.strip().lower() in {"done", "closed", "resolved"}


@dataclass
class Epic:
    """Groups tickets that share a parent epic.

    ``key`` is ``None`` for the synthetic bucket holding tickets that have no
    epic, so unparented tickets still render under their sprint/board.
    """

    name: str
    key: str | None = None
    tickets: list["Ticket"] = field(default_factory=list)

    @property
    def is_orphan_bucket(self) -> bool:
        return self.key is None

    @property
    def ticket_count(self) -> int:
        return len(self.tickets)


@dataclass
class Group:
    """A sprint, backlog, or kanban-board container of epics/tickets.

    ``group_type`` is one of "sprint" | "backlog" | "board" | "project";
    ``state`` is the sprint state ("active"/"future"/"closed") when applicable.
    """

    name: str
    group_type: str
    state: str | None = None
    sprint_id: str | None = None
    epics: list["Epic"] = field(default_factory=list)

    @property
    def tickets(self) -> list["Ticket"]:
        return [t for epic in self.epics for t in epic.tickets]

    @property
    def ticket_count(self) -> int:
        return sum(epic.ticket_count for epic in self.epics)


@dataclass
class Board:
    """An agile board (Scrum or Kanban) under a space."""

    board_id: str
    board_name: str
    board_type: str  # "scrum" | "kanban"
    groups: list["Group"] = field(default_factory=list)

    @property
    def ticket_count(self) -> int:
        return sum(g.ticket_count for g in self.groups)


@dataclass
class Space:
    """A Jira project/space (the root of the hierarchy).

    ``boards`` holds the agile boards under this space; ``loose_groups`` holds
    tickets that live directly under the space with no board, so nothing is
    dropped from the view.
    """

    space_key: str
    space_name: str = ""
    space_id: str = ""
    boards: list["Board"] = field(default_factory=list)
    loose_groups: list["Group"] = field(default_factory=list)

    @property
    def all_groups(self) -> list["Group"]:
        return [g for b in self.boards for g in b.groups] + self.loose_groups

    @property
    def ticket_count(self) -> int:
        return sum(b.ticket_count for b in self.boards) + sum(
            g.ticket_count for g in self.loose_groups
        )


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
