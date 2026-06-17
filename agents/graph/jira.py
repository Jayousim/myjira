import json
import re

from langchain_anthropic import ChatAnthropic
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

from .config import config


async def _jira_run(instruction: str) -> str:
    """Run a focused, single-purpose task against the Jira MCP tools."""
    config.require("jira_url", "jira_username", "jira_api_token")
    client = MultiServerMCPClient(config.mcp_config())
    tools = await client.get_tools()
    model = ChatAnthropic(model=config.planner_model)
    agent = create_react_agent(model, tools)
    result = await agent.ainvoke({"messages": [{"role": "user", "content": instruction}]})
    return result["messages"][-1].content


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of an LLM response."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in response:\n{text}")
    return json.loads(match.group(0))


def _extract_json_array(text: str) -> list:
    """Pull the first JSON array out of an LLM response."""
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON array found in response:\n{text}")
    return json.loads(match.group(0))


async def list_tickets(limit: int = 20) -> list[dict]:
    """Fetch a list of actionable tickets for the user to choose from."""
    instruction = (
        f"Search Jira issues with this JQL: {config.sprint_jql!r}. "
        f"Return up to {limit} issues. "
        "Return ONLY a JSON array where each element is an object with keys: "
        "key, summary, issue_type, status, priority. "
        "priority should be the priority name as a string (empty string if none). "
        "Preserve the JQL ordering. "
        "Do not include any prose outside the JSON."
    )
    raw = await _jira_run(instruction)
    return _extract_json_array(raw)


async def select_ticket(ticket_key: str | None = None) -> dict:
    """Fetch one actionable ticket. If ticket_key is given, fetch that one."""
    if ticket_key:
        instruction = (
            f"Get the Jira issue {ticket_key}. "
            "Return ONLY a JSON object with keys: "
            "key, summary, issue_type, status, description, acceptance_criteria. "
            "acceptance_criteria should be a list of strings (empty list if none). "
            "Do not include any prose outside the JSON."
        )
    else:
        instruction = (
            f"Search Jira issues with this JQL: {config.sprint_jql!r}. "
            "Pick the single highest-priority issue that is a bug or a feature/story "
            "and is not already done. "
            "Return ONLY a JSON object with keys: "
            "key, summary, issue_type, status, description, acceptance_criteria. "
            "acceptance_criteria should be a list of strings (empty list if none). "
            "Do not include any prose outside the JSON."
        )
    raw = await _jira_run(instruction)
    return _extract_json(raw)


async def report_back(ticket_key: str, comment: str, transition_to: str | None = None) -> str:
    """Post a comment on the ticket and optionally transition its status."""
    instruction = (
        f"On Jira issue {ticket_key}, add the following comment exactly:\n\n"
        f"{comment}\n\n"
    )
    if transition_to:
        instruction += (
            f"Then transition the issue to the '{transition_to}' status if such a "
            "transition is available. If it is not available, leave the status "
            "unchanged and say so."
        )
    return await _jira_run(instruction)
