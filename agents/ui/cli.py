"""Python port of ``agents/src/ui/cli.ts`` (chalk -> colorama, inquirer -> questionary)."""

from __future__ import annotations

import sys

import questionary
from colorama import Fore, Style
from colorama import init as colorama_init

from agent_types import Plan, StepResult, Task

# Node/chalk emit UTF-8 natively; the Windows console defaults to cp1252, so
# force UTF-8 before colorama wraps the streams to keep the box-drawing glyphs.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

colorama_init()


def _c(text: str, *styles: str) -> str:
    return "".join(styles) + text + Style.RESET_ALL


def print_banner() -> None:
    print(_c("\n\u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557", Style.BRIGHT, Fore.CYAN))
    print(_c("\u2551   Joyalty Dev Agent Orchestrator      \u2551", Style.BRIGHT, Fore.CYAN))
    print(_c("\u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d\n", Style.BRIGHT, Fore.CYAN))


def print_task(task: Task) -> None:
    print(_c(f"\nTask: {task.title}", Style.BRIGHT, Fore.WHITE))
    print(_c(f"  ID: {task.id} | Priority: {task.priority}", Fore.LIGHTBLACK_EX))
    print(_c(f"  {task.description}\n", Fore.WHITE))


def print_plan(plan: Plan) -> None:
    print(_c("\n\u2550\u2550\u2550 Implementation Plan \u2550\u2550\u2550\n", Style.BRIGHT, Fore.YELLOW))
    print(_c(f"Summary: {plan.summary}\n", Fore.WHITE))

    for step in plan.steps:
        print(_c(f"  Step {step.step_number}: {step.title}", Style.BRIGHT, Fore.CYAN))
        print(_c(f"    {step.description}", Fore.WHITE))
        print(_c(f"    Files: {', '.join(step.files_to_touch)}", Fore.LIGHTBLACK_EX))
        print(_c(f"    Expected: {step.expected_outcome}", Fore.GREEN))
        print()


async def prompt_approval() -> str:
    answer = await questionary.select(
        "What would you like to do with this plan?",
        choices=[
            questionary.Choice("Approve \u2014 start implementation", value="approve"),
            questionary.Choice("Reject \u2014 skip this task", value="reject"),
            questionary.Choice("Edit \u2014 provide feedback to regenerate", value="edit"),
        ],
    ).ask_async()
    return answer


async def prompt_feedback() -> str:
    feedback = await questionary.text("Enter your feedback for the planner:").ask_async()
    return feedback or ""


async def prompt_task_selection(tasks: list[Task]) -> Task | None:
    if not tasks:
        print(_c("No pending tasks in the backlog.", Fore.YELLOW))
        return None

    choices = [
        questionary.Choice(f"[{t.priority}] {t.title} ({t.id})", value=t.id)
        for t in tasks
    ]
    choices.append(questionary.Choice("Exit", value="__exit__"))

    answer = await questionary.select("Select a task to work on:", choices=choices).ask_async()

    if answer == "__exit__" or answer is None:
        return None
    return next((t for t in tasks if t.id == answer), None)


async def prompt_continue() -> bool:
    return bool(await questionary.confirm("Continue to next task?").ask_async())


def print_step_start(step_number: int, title: str) -> None:
    print(_c(f"\n\u25b6 Step {step_number}: {title}", Style.BRIGHT, Fore.BLUE))


def print_step_result(result: StepResult) -> None:
    if result.success:
        print(_c(f"  \u2713 Step {result.step_number} completed: {result.message}", Fore.GREEN))
        if result.files_changed:
            print(_c(f"    Changed: {', '.join(result.files_changed)}", Fore.LIGHTBLACK_EX))
    else:
        print(_c(f"  \u2717 Step {result.step_number} failed: {result.message}", Fore.RED))


def print_validation_result(passed: bool, errors: list[str]) -> None:
    if passed:
        print(_c("  \u2713 Validation passed", Fore.GREEN))
    else:
        print(_c("  \u2717 Validation failed:", Fore.RED))
        for err in errors:
            print(_c(f"    {err[:200]}", Fore.RED))


def print_progress(current: int, total: int) -> None:
    pct = round((current / total) * 100) if total else 0
    filled = round(pct / 5)
    bar = "\u2588" * filled + "\u2591" * (20 - filled)
    print(_c(f"  Progress: [{bar}] {pct}% ({current}/{total})", Fore.CYAN))


def print_done() -> None:
    print(_c("\n\u2713 All steps completed successfully!\n", Style.BRIGHT, Fore.GREEN))


def print_error(message: str) -> None:
    print(_c(f"\n\u2717 Error: {message}\n", Style.BRIGHT, Fore.RED))
