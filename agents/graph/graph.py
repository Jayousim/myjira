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
    result: dict[str, Any]
    review: str
    report: str


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
    return "implement" if state.get("approved") else "rejected"


async def _implement_locally(ticket: dict, plan: Plan, skip_git: bool) -> dict:
    """Run the local plan -> implement -> validate -> fix -> commit loop.

    Mirrors ``orchestrator.execute_task`` but returns the dict shape the graph's
    ``report_node`` expects.
    """
    task_id = ticket.get("key", "")
    title = ticket.get("summary", "")
    branch_name: Optional[str] = None

    try:
        if not skip_git:
            if await has_uncommitted_changes():
                return {
                    "ok": False,
                    "stage": "git",
                    "error": "Uncommitted changes present; commit or stash them first.",
                }
            branch_name = await create_branch(task_id, title)

        completed = 0
        for step in plan.steps:
            result = await implement_step(step, plan.summary)
            if not result.success:
                return {
                    "ok": False,
                    "stage": f"step {step.step_number}",
                    "error": result.message,
                    "branch": branch_name,
                }

            validation = await run_validation()
            if not validation["passed"]:
                fix = await fix_errors(validation["errors"], result.files_changed)
                if not fix["fixed"]:
                    return {
                        "ok": False,
                        "stage": f"validation after step {step.step_number}",
                        "error": "Validation failed and auto-fix was unsuccessful.",
                        "branch": branch_name,
                    }

            if not skip_git:
                await commit_step(step.step_number, step.title, result.files_changed)
            completed += 1

        pr_url = None
        if branch_name and not skip_git:
            pr_url = await create_pull_request(
                branch_name, title, plan.summary, completed
            )

        return {
            "ok": True,
            "run_id": branch_name or "(local)",
            "summary": (
                f"Implemented {completed} step(s) for {task_id}"
                + (f" on branch {branch_name}." if branch_name else " locally.")
            ),
            "pr_url": pr_url,
            "steps_completed": completed,
        }
    except Exception as error:  # noqa: BLE001 - surface anything to the report node
        return {
            "ok": False,
            "stage": "implement",
            "error": str(error),
            "branch": branch_name,
        }


async def implement_node(state: State) -> State:
    if config.implementer_mode == "cloud":
        # Cloud agent works from the prose plan and the ticket, on a remote repo.
        result = await implement_ticket(state["ticket"], state["plan"])
    else:
        result = await _implement_locally(
            state["ticket"], state["plan_obj"], state.get("skip_git", False)
        )
    return {"result": result}


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
        comment = (
            f"Automated implementation completed.\n"
            f"PR: {pr}\n"
            f"Run: {result.get('run_id')}\n\n"
            f"Summary:\n{result.get('summary')}"
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
    g.add_node("implement", implement_node)
    g.add_node("review", review_node)
    g.add_node("report", report_node)
    g.add_node("rejected", rejected_node)

    g.add_edge(START, "select")
    g.add_edge("select", "plan")
    g.add_edge("plan", "approval")
    g.add_conditional_edges(
        "approval",
        after_approval,
        {"implement": "implement", "rejected": "rejected"},
    )
    g.add_edge("implement", "review")
    g.add_edge("review", "report")
    g.add_edge("report", END)
    g.add_edge("rejected", END)

    return g.compile(checkpointer=MemorySaver())


graph = build_graph()
