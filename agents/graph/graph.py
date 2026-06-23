from typing import Any, Optional, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from llm import build_chat_model, message_text

from .config import config
from .jira import report_back, select_ticket

# Local coding agents (the same ones the orchestrator uses) live in the
# ``agents`` subpackage, which is on sys.path when the app runs from the
# ``agents/`` root. The graph delegates the actual coding work to them instead
# of the Cursor cloud agent.
from agent_types import Plan, Task
from agents.planner import generate_plan
from agents.implementer import implement_step
from agents.cloud_implementer import implement_ticket
from agents.fixer import fix_errors
from tools.shell_tools import run_validation
from tools.git_tools import (
    create_branch,
    commit_step,
    create_pull_request,
    get_step_diff,
    has_uncommitted_changes,
)


class State(TypedDict, total=False):
    ticket_key: Optional[str]   # optional input: implement a specific ticket
    ticket: dict
    plan: str                   # human-readable plan (shown at the approval gate)
    plan_obj: Plan              # structured plan the implementer actually executes
    approved: bool
    feedback: str
    skip_git: bool
    review_steps: bool          # opt-in: pause for human review after each step
    result: dict[str, Any]
    review: str
    report: str

    # --- per-step loop bookkeeping (checkpointed so it survives interrupts) ---
    current_step_index: int     # index into plan_obj.steps of the NEXT step to run
    completed: int              # steps fully implemented so far
    warnings: list[dict]        # unresolved validation errors, per step
    branch_name: Optional[str]
    git_enabled: bool
    git_skipped_reason: Optional[str]
    last_step: dict             # summary + diff of the most recently finished step
    stop_requested: bool        # set when the human chooses "stop" at a step review


def _ticket_to_task(ticket: dict) -> Task:
    description = (ticket.get("description") or "").strip()
    criteria = ticket.get("acceptance_criteria") or []
    if criteria:
        criteria_block = "\n".join(f"- {c}" for c in criteria)
        description = f"{description}\n\nAcceptance criteria:\n{criteria_block}".strip()
    return Task(
        id=ticket.get("key", ""),
        title=ticket.get("summary", ""),
        description=description,
        status="pending",
        priority=3,
    )


def _render_plan(plan: Plan) -> str:
    lines = [f"Summary: {plan.summary}", ""]
    for step in plan.steps:
        lines.append(f"Step {step.step_number}: {step.title}")
        lines.append(f"  {step.description}")
        if step.files_to_touch:
            lines.append(f"  Files: {', '.join(step.files_to_touch)}")
        lines.append(f"  Expected: {step.expected_outcome}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_warnings(warnings: Optional[list[dict]]) -> str:
    """Render unresolved validation errors into a readable report block."""
    if not warnings:
        return ""
    # Cap each error blob so a noisy compiler dump doesn't overwhelm the report.
    max_chars = 2_000
    lines = ["", "", "Unresolved validation errors (auto-fix could not handle these):"]
    for warning in warnings:
        stage = warning.get("stage", "validation")
        lines.append("")
        lines.append(f"[{stage}] {warning.get('step_title', '')}".rstrip())
        for error in warning.get("errors", []):
            text = error if len(error) <= max_chars else f"{error[:max_chars]}\n... (truncated)"
            lines.append(text)
    return "\n".join(lines)


async def select_node(state: State) -> State:
    ticket = await select_ticket(state.get("ticket_key"))
    return {"ticket": ticket}


async def plan_node(state: State) -> State:
    """Produce a structured plan with the local planner agent."""
    task = _ticket_to_task(state["ticket"])
    plan = await generate_plan(task)
    return {"plan_obj": plan, "plan": _render_plan(plan)}


async def approval_node(state: State) -> State:
    """Pause and wait for a human decision before any code is written."""
    decision = interrupt(
        {
            "type": "plan_approval",
            "ticket": state["ticket"],
            "plan": state["plan"],
            "question": "Approve this plan for implementation? (approve / reject)",
        }
    )
    if isinstance(decision, dict):
        approved = bool(decision.get("approved"))
        feedback = decision.get("feedback", "")
    else:
        approved = str(decision).strip().lower() in {"approve", "approved", "y", "yes"}
        feedback = ""
    return {"approved": approved, "feedback": feedback}


def after_approval(state: State) -> str:
    if not state.get("approved"):
        return "rejected"
    if config.implementer_mode == "cloud":
        return "implement_cloud"
    return "prepare"


async def implement_cloud_node(state: State) -> State:
    """Cloud agent works from the prose plan and the ticket, on a remote repo."""
    result = await implement_ticket(state["ticket"], state["plan"])
    return {"result": result}


async def prepare_node(state: State) -> State:
    """Set up git and initialise the per-step loop counters.

    Splitting this off from the step loop means the implement/review nodes stay
    small and idempotent, which is what makes the per-step ``interrupt()`` safe:
    on resume only the (side-effect-free) review node replays, never the
    already-committed implementation work.
    """
    ticket = state["ticket"]
    task_id = ticket.get("key", "")
    title = ticket.get("summary", "")

    # When git automation is unavailable (e.g. the working tree already has
    # uncommitted changes we shouldn't touch), we don't abort. Instead we fall
    # back to "local only" mode: the implementation still runs, but we skip
    # branch/commit/PR and leave everything uncommitted for the user to review
    # and commit manually.
    git_enabled = not state.get("skip_git", False)
    git_skipped_reason: Optional[str] = None
    branch_name: Optional[str] = None

    if git_enabled and await has_uncommitted_changes():
        git_enabled = False
        git_skipped_reason = (
            "Working tree had uncommitted changes, so git automation was "
            "skipped. New changes were left uncommitted for you to review and "
            "commit manually."
        )

    if git_enabled:
        branch_name = await create_branch(task_id, title)

    return {
        "current_step_index": 0,
        "completed": 0,
        "warnings": [],
        "branch_name": branch_name,
        "git_enabled": git_enabled,
        "git_skipped_reason": git_skipped_reason,
        "stop_requested": False,
    }


async def implement_step_node(state: State) -> State:
    """Implement the single step at ``current_step_index`` and advance the index."""
    plan: Plan = state["plan_obj"]
    idx = state["current_step_index"]
    step = plan.steps[idx]
    git_enabled = state.get("git_enabled", False)

    try:
        result = await implement_step(step, plan.summary)
    except Exception as error:  # noqa: BLE001 - surface anything to the report node
        return {
            "result": {
                "ok": False,
                "stage": f"step {step.step_number}",
                "error": str(error),
                "branch": state.get("branch_name"),
            }
        }

    if not result.success:
        # A step that can't be implemented at all is still fatal — there's
        # nothing downstream can build on. Surface it and stop the run.
        return {
            "result": {
                "ok": False,
                "stage": f"step {step.step_number}",
                "error": result.message,
                "branch": state.get("branch_name"),
            }
        }

    # Validation failures that auto-fix can't resolve are NOT fatal: record them
    # and keep going so a human can finish the (usually small) fix manually.
    warnings = list(state.get("warnings", []))
    validation = await run_validation()
    if not validation["passed"]:
        fix = await fix_errors(validation["errors"], result.files_changed)
        if not fix["fixed"]:
            warnings.append(
                {
                    "stage": f"validation after step {step.step_number}",
                    "step_number": step.step_number,
                    "step_title": step.title,
                    "errors": fix.get("remaining_errors") or validation["errors"],
                }
            )

    if git_enabled:
        await commit_step(step.step_number, step.title, result.files_changed)

    diff = ""
    if state.get("review_steps"):
        diff = await get_step_diff(result.files_changed, committed=git_enabled)

    return {
        "current_step_index": idx + 1,
        "completed": state.get("completed", 0) + 1,
        "warnings": warnings,
        "last_step": {
            "step_number": step.step_number,
            "title": step.title,
            "message": result.message,
            "files_changed": result.files_changed,
            "diff": diff,
            "is_last": idx + 1 >= len(plan.steps),
            "total": len(plan.steps),
        },
    }


def after_implement_step(state: State) -> str:
    # A fatal step failure already wrote a result dict — go straight to review.
    if state.get("result"):
        return "review"
    if state.get("review_steps"):
        return "step_review"
    if state["current_step_index"] < len(state["plan_obj"].steps):
        return "implement_step"
    return "finalize"


async def step_review_node(state: State) -> State:
    """Pause after a step so the human can inspect the changes and decide.

    This node is intentionally side-effect free: the diff was computed upstream
    in ``implement_step_node`` and only read here, so replaying this node on
    resume (which LangGraph does) does no real work beyond returning the human's
    decision.
    """
    last = state.get("last_step", {})
    decision = interrupt(
        {
            "type": "step_review",
            "ticket": state["ticket"],
            "step": last,
            "completed": state.get("completed", 0),
            "question": "Review this step. Continue to the next step or stop here?",
        }
    )
    if isinstance(decision, dict):
        action = str(decision.get("action", "continue"))
    else:
        action = str(decision).strip().lower()
    return {"stop_requested": action == "stop"}


def after_step_review(state: State) -> str:
    if state.get("stop_requested"):
        return "finalize"
    if state["current_step_index"] < len(state["plan_obj"].steps):
        return "implement_step"
    return "finalize"


async def finalize_node(state: State) -> State:
    """Build the final result dict (PR + summary) once the loop is done."""
    ticket = state["ticket"]
    task_id = ticket.get("key", "")
    title = ticket.get("summary", "")
    branch_name = state.get("branch_name")
    git_enabled = state.get("git_enabled", False)
    completed = state.get("completed", 0)
    warnings = state.get("warnings", [])
    stopped_early = state.get("stop_requested", False) and state[
        "current_step_index"
    ] < len(state["plan_obj"].steps)

    pr_url = None
    if branch_name and git_enabled:
        pr_url = await create_pull_request(
            branch_name, title, state["plan_obj"].summary, completed
        )

    summary = f"Implemented {completed} step(s) for {task_id}"
    if branch_name:
        summary += f" on branch {branch_name}."
    else:
        summary += " locally (changes left uncommitted)."
    if stopped_early:
        summary += (
            f"\n\nNote: stopped early at your request after {completed} step(s); "
            "the remaining steps were not implemented."
        )
    if state.get("git_skipped_reason"):
        summary += f"\n\nNote: {state['git_skipped_reason']}"
    if warnings:
        summary += (
            f"\n\nNote: {len(warnings)} step(s) finished with unresolved "
            "validation errors that auto-fix could not handle. The changes "
            "were left in place for you to finish manually — see the details "
            "below."
        )

    return {
        "result": {
            "ok": True,
            "run_id": branch_name or "(local)",
            "summary": summary,
            "pr_url": pr_url,
            "steps_completed": completed,
            "git_skipped": state.get("git_skipped_reason") is not None,
            "manual_commit_required": branch_name is None,
            "stopped_early": stopped_early,
            "warnings": warnings,
        }
    }


async def review_node(state: State) -> State:
    """Review the implementation against the plan and acceptance criteria."""
    result = state["result"]
    if not result.get("ok"):
        return {"review": ""}

    ticket = state["ticket"]
    model = build_chat_model(config.planner_model)
    prompt = (
        "You are a senior reviewer. Given the approved plan and the implementation "
        "summary below, write a brief code review. Call out risks, anything missing "
        "(especially tests), and whether it appears to meet the acceptance criteria. "
        "Keep it concise (a few bullet points).\n\n"
        f"Ticket {ticket.get('key')}: {ticket.get('summary')}\n"
        f"Acceptance criteria: {ticket.get('acceptance_criteria')}\n\n"
        f"Approved plan:\n{state['plan']}\n\n"
        f"Implementation summary:\n{result.get('summary')}"
    )
    resp = await model.ainvoke(prompt)
    return {"review": message_text(resp)}


async def report_node(state: State) -> State:
    ticket = state["ticket"]
    result = state["result"]
    if result.get("ok"):
        pr = result.get("pr_url") or "(see run output)"
        review = state.get("review")
        review_block = f"\n\nReview notes:\n{review}" if review else ""
        warnings_block = _format_warnings(result.get("warnings"))
        comment = (
            f"Automated implementation completed.\n"
            f"PR: {pr}\n"
            f"Run: {result.get('run_id')}\n\n"
            f"Summary:\n{result.get('summary')}"
            f"{warnings_block}"
            f"{review_block}"
        )
        transition = "In Review"
    else:
        comment = (
            "Automated implementation did not start.\n"
            f"Stage: {result.get('stage')}\n"
            f"Error: {result.get('error')}"
        )
        transition = None
    report = await report_back(ticket["key"], comment, transition_to=transition)
    return {"report": report}


async def rejected_node(state: State) -> State:
    ticket = state["ticket"]
    feedback = state.get("feedback") or "No feedback provided."
    report = await report_back(
        ticket["key"],
        f"Implementation plan was not approved. Feedback: {feedback}",
    )
    return {"report": report}


def build_graph():
    g = StateGraph(State)
    g.add_node("select", select_node)
    g.add_node("plan", plan_node)
    g.add_node("approval", approval_node)
    g.add_node("implement_cloud", implement_cloud_node)
    g.add_node("prepare", prepare_node)
    g.add_node("implement_step", implement_step_node)
    g.add_node("step_review", step_review_node)
    g.add_node("finalize", finalize_node)
    g.add_node("review", review_node)
    g.add_node("report", report_node)
    g.add_node("rejected", rejected_node)

    g.add_edge(START, "select")
    g.add_edge("select", "plan")
    g.add_edge("plan", "approval")
    g.add_conditional_edges(
        "approval",
        after_approval,
        {
            "implement_cloud": "implement_cloud",
            "prepare": "prepare",
            "rejected": "rejected",
        },
    )
    g.add_edge("implement_cloud", "review")

    # Local per-step loop: prepare -> implement_step -> (step_review) -> ... -> finalize
    g.add_edge("prepare", "implement_step")
    g.add_conditional_edges(
        "implement_step",
        after_implement_step,
        {
            "implement_step": "implement_step",
            "step_review": "step_review",
            "finalize": "finalize",
            "review": "review",
        },
    )
    g.add_conditional_edges(
        "step_review",
        after_step_review,
        {"implement_step": "implement_step", "finalize": "finalize"},
    )
    g.add_edge("finalize", "review")

    g.add_edge("review", "report")
    g.add_edge("report", END)
    g.add_edge("rejected", END)

    return g.compile(checkpointer=MemorySaver())


graph = build_graph()
