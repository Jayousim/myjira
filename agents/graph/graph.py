from typing import Any, Optional, TypedDict

from langchain_anthropic import ChatAnthropic
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .config import config
from agents.implementer import implement_ticket

from .jira import report_back, select_ticket


class State(TypedDict, total=False):
    ticket_key: Optional[str]   # optional input: implement a specific ticket
    ticket: dict
    plan: str
    approved: bool
    feedback: str
    result: dict[str, Any]
    review: str
    report: str


async def select_node(state: State) -> State:
    ticket = await select_ticket(state.get("ticket_key"))
    return {"ticket": ticket}


async def plan_node(state: State) -> State:
    ticket = state["ticket"]
    model = ChatAnthropic(model=config.planner_model)
    prompt = (
        "You are a senior engineer. Draft a concise implementation plan for this "
        "Jira ticket. List the files/areas likely to change, the approach, edge "
        "cases, and how it will be tested. Do not write the code yet.\n\n"
        f"Ticket {ticket.get('key')} ({ticket.get('issue_type')}): "
        f"{ticket.get('summary')}\n\n"
        f"Description:\n{ticket.get('description')}\n\n"
        f"Acceptance criteria: {ticket.get('acceptance_criteria')}"
    )
    resp = await model.ainvoke(prompt)
    return {"plan": resp.content}


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


async def implement_node(state: State) -> State:
    result = await implement_ticket(state["ticket"], state["plan"])
    return {"result": result}


async def review_node(state: State) -> State:
    """Review the implementation against the plan and acceptance criteria."""
    result = state["result"]
    if not result.get("ok"):
        return {"review": ""}

    ticket = state["ticket"]
    model = ChatAnthropic(model=config.planner_model)
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
    return {"review": resp.content}


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
