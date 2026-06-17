"""Python port of ``agents/src/orchestrator.ts`` (Jira-sourced).

This drives the plan/approval loop and executes a plan step-by-step with
validation, automatic error fixing, git commits and PR creation. Tasks are
sourced from Jira (via ``jira_source``) rather than the original JSON backlog.

It relies on sibling modules that mirror the rest of the TS project:
    config            -> CONFIG
    jira_source       -> fetch_task_list() / fetch_task_detail(key)
    agents.planner    -> generate_plan(task) -> Plan
    agents.implementer-> implement_step(step, summary) -> StepResult
    agents.fixer      -> fix_errors(errors, files_changed) -> {"fixed": bool}
    tools.shell_tools -> run_validation() -> {"passed": bool, "errors": list[str]}
    tools.git_tools   -> create_branch / commit_step / create_pull_request /
                         get_current_branch / switch_branch / has_uncommitted_changes
    ui.cli            -> print_* / prompt_* helpers
"""

from __future__ import annotations

import dataclasses
from typing import Optional

from config import CONFIG
from agents.planner import generate_plan
from agents.implementer import implement_step
from agents.fixer import fix_errors
from tools.shell_tools import run_validation
from tools.git_tools import (
    create_branch,
    commit_step,
    create_pull_request,
    get_current_branch,
    switch_branch,  # noqa: F401  (kept to mirror the TS import surface)
    has_uncommitted_changes,
)
from ui.cli import (
    print_task,
    print_plan,
    prompt_approval,
    prompt_feedback,
    print_step_start,
    print_step_result,
    print_validation_result,
    print_progress,
    print_done,
    print_error,
)
from agent_types import Task, Plan
from jira_source import fetch_task_detail, fetch_task_list


async def load_tasks() -> list[Task]:
    """Source the actionable tickets from Jira (was a JSON backlog in the TS port)."""
    return await fetch_task_list()


async def load_task(ticket_key: str) -> Task:
    """Fetch a single Jira ticket with full description + acceptance criteria."""
    return await fetch_task_detail(ticket_key)


async def plan_with_approval(task: Task) -> Optional[Plan]:
    feedback: Optional[str] = None

    for _attempt in range(5):
        print("\n  Generating plan...")

        plan = await generate_plan(task)
        print_plan(plan)

        decision = await prompt_approval()

        if decision == "approve":
            return plan
        if decision == "reject":
            return None

        feedback = await prompt_feedback()
        task = dataclasses.replace(
            task,
            description=f"{task.description}\n\nAdditional feedback: {feedback}",
        )

    print_error("Too many plan iterations. Skipping task.")
    return None


async def execute_task(
    task: Task,
    dry_run: bool = False,
    skip_git: bool = False,
) -> bool:
    print_task(task)

    plan = await plan_with_approval(task)
    if not plan:
        print("  Task skipped.\n")
        return False

    if dry_run:
        print("\n  Dry run - no changes made.\n")
        return True

    branch_name: Optional[str] = None
    _original_branch = await get_current_branch()

    if not skip_git:
        if await has_uncommitted_changes():
            print_error(
                "You have uncommitted changes. Please commit or stash them "
                "before running the agent."
            )
            return False
        branch_name = await create_branch(task.id, task.title)

    completed_steps = 0

    try:
        for step in plan.steps:
            print_step_start(step.step_number, step.title)
            print_progress(completed_steps, len(plan.steps))

            result = await implement_step(step, plan.summary)
            print_step_result(result)

            if not result.success:
                print_error(f"Step {step.step_number} failed. Stopping.")
                break

            validation = await run_validation()
            print_validation_result(validation["passed"], validation["errors"])

            if not validation["passed"]:
                print("  Attempting automatic fix...")
                fix = await fix_errors(validation["errors"], result.files_changed)

                if not fix["fixed"]:
                    print_error(
                        "Could not fix validation errors after "
                        f"{CONFIG.max_fix_attempts} attempts. Stopping."
                    )
                    break

            if not skip_git:
                await commit_step(step.step_number, step.title, result.files_changed)

            completed_steps += 1
            print_progress(completed_steps, len(plan.steps))
    except Exception as error:  # noqa: BLE001 - mirror TS catch-all
        print_error(f"Unexpected error: {error}")

    if completed_steps == len(plan.steps):
        print_done()

        if branch_name and not skip_git:
            await create_pull_request(
                branch_name,
                task.title,
                plan.summary,
                completed_steps,
            )

        return True

    print(
        f"\n  Completed {completed_steps}/{len(plan.steps)} steps on branch: "
        f"{branch_name or 'current'}\n"
    )

    return False
