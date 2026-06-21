import json
import re

from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

from llm import build_chat_model

from .config import config


async def _jira_run(instruction: str) -> str:
    """Run a focused, single-purpose task against the Jira MCP tools."""
    config.require("jira_url", "jira_username", "jira_api_token")
    client = MultiServerMCPClient(config.mcp_config())
    tools = await client.get_tools()
    model = build_chat_model(config.planner_model)
    agent = create_react_agent(model, tools)
    result = await agent.ainvoke({"messages": [{"role": "user", "content": instruction}]})
    return result["messages"][-1].content


async def _jira_tools() -> dict:
    """Connect to the Atlassian MCP server and return its tools keyed by name."""
    config.require("jira_url", "jira_username", "jira_api_token")
    client = MultiServerMCPClient(config.mcp_config())
    tools = await client.get_tools()
    return {tool.name: tool for tool in tools}


def _loads(value):
    """Best-effort parse of an MCP tool payload into Python objects.

    The Atlassian MCP tools wrap their JSON in a few different envelopes:

    * a FastMCP ``{"result": "<json string>"}`` wrapper, and
    * a content-block list ``[{"type": "text", "text": "<json>"}]`` (the actual
      data lives in the ``text`` field, sometimes split across blocks).

    Unwrap each shape recursively until we reach the real payload.
    """
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        data = value
    else:
        try:
            data = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return value

    # Content-block list: join the text of every block and re-parse.
    if isinstance(data, list):
        dict_items = [b for b in data if isinstance(b, dict)]
        text_blocks = [
            b["text"]
            for b in dict_items
            if b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        if dict_items and len(text_blocks) == len(dict_items):
            return _loads("".join(text_blocks))
        return data

    if isinstance(data, dict):
        # Single content block.
        if data.get("type") == "text" and isinstance(data.get("text"), str):
            return _loads(data["text"])
        # FastMCP generic result wrapper.
        if set(data.keys()) == {"result"}:
            return _loads(data["result"])
    return data


async def _call_tool(tools: dict, name: str, **kwargs):
    """Invoke a single MCP tool directly (no LLM) and parse its payload."""
    tool = tools.get(name)
    if tool is None:
        raise RuntimeError(f"Jira MCP tool '{name}' is not available")
    raw = await tool.ainvoke(kwargs)
    return _loads(raw)


def _name_of(value) -> str:
    """Pull a human name out of a Jira field that may be a dict or a string."""
    if isinstance(value, dict):
        return value.get("name") or value.get("value") or ""
    return value or ""


def _epic_of(issue: dict) -> tuple[str | None, str]:
    """Best-effort extraction of an issue's parent epic ``(key, name)``.

    Jira exposes the epic differently across project styles (``epic_link`` /
    ``epic_key`` on company-managed projects, a ``parent`` of type Epic on
    team-managed ones), so probe each shape and fall back to ``(None, "")``.
    """
    for key_field, name_field in (
        ("epic_key", "epic_name"),
        ("epicKey", "epicName"),
        ("epic_link", "epic_name"),
    ):
        value = issue.get(key_field)
        if value:
            return str(value), _name_of(issue.get(name_field)) or ""

    epic = issue.get("epic")
    if isinstance(epic, dict):
        key = str(epic.get("key") or "").strip()
        if key:
            return key, epic.get("name") or epic.get("summary") or ""
    elif isinstance(epic, str) and epic.strip():
        return epic.strip(), _name_of(issue.get("epic_name")) or ""

    parent = issue.get("parent")
    if isinstance(parent, dict):
        fields = parent.get("fields") or {}
        parent_type = _name_of(
            fields.get("issuetype")
            or parent.get("issue_type")
            or parent.get("issuetype")
        ).lower()
        parent_key = str(parent.get("key") or "").strip()
        if parent_key and parent_type == "epic":
            return parent_key, fields.get("summary") or parent.get("summary") or ""

    return None, ""


def _normalize_issue(issue: dict) -> dict:
    status = issue.get("status") or {}
    status_category = ""
    if isinstance(status, dict):
        category = status.get("statusCategory") or status.get("status_category") or {}
        status_category = _name_of(category)
    epic_key, epic_name = _epic_of(issue)
    return {
        "key": issue.get("key", ""),
        "summary": issue.get("summary", ""),
        "issue_type": _name_of(issue.get("issue_type") or issue.get("issuetype")),
        "status": _name_of(status),
        "status_category": status_category,
        "priority": _name_of(issue.get("priority")),
        "labels": issue.get("labels", []),
        "epic_key": epic_key,
        "epic_name": epic_name,
    }


def _issues_from_payload(payload) -> list[dict]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("issues") or payload.get("results") or payload.get("values") or []
    else:
        items = []
    return [_normalize_issue(i) for i in items if isinstance(i, dict)]


def _records_from_payload(payload) -> list[dict]:
    """Boards/sprints come back as a bare list or under ``values``/``boards``."""
    if isinstance(payload, dict):
        payload = payload.get("values") or payload.get("boards") or payload.get("sprints") or []
    return [r for r in payload if isinstance(r, dict)] if isinstance(payload, list) else []


def _is_done(issue: dict) -> bool:
    category = (issue.get("status_category") or "").strip().lower()
    if category:
        return category == "done"
    return (issue.get("status") or "").strip().lower() in {"done", "closed", "resolved"}


def _finalize_tickets(tickets: list[dict], include_done: bool) -> list[dict]:
    if not include_done:
        tickets = [t for t in tickets if not _is_done(t)]
    return tickets


def _is_epic(ticket: dict) -> bool:
    return (ticket.get("issue_type") or "").strip().lower() == "epic"


def _group_by_epic(tickets: list[dict]) -> list[dict]:
    """Bucket tickets by their parent epic, preserving first-seen order.

    Epic-type issues become their own header bucket (so an epic placed directly
    in a sprint still shows up, with its child issues nested beneath it). Tickets
    with no epic land in a synthetic ``{"key": None}`` bucket so they still
    render under their sprint/board.
    """
    buckets: dict[str, dict] = {}
    order: list[str] = []

    def ensure(key: str, name: str) -> dict:
        bucket_key = key or ""
        if bucket_key not in buckets:
            buckets[bucket_key] = {"key": key or None, "name": name, "tickets": []}
            order.append(bucket_key)
        return buckets[bucket_key]

    # Epics first, so children file under an already-named header.
    for ticket in tickets:
        if _is_epic(ticket) and ticket.get("key"):
            ensure(ticket["key"], ticket.get("summary") or f"Epic {ticket['key']}")

    for ticket in tickets:
        if _is_epic(ticket) and ticket.get("key"):
            continue  # represented as a header, not a selectable ticket
        epic_key = ticket.get("epic_key") or ""
        if epic_key:
            name = ticket.get("epic_name") or (
                buckets[epic_key]["name"] if epic_key in buckets else f"Epic {epic_key}"
            )
            ensure(epic_key, name)["tickets"].append(ticket)
        else:
            ensure("", "No epic")["tickets"].append(ticket)

    return [buckets[k] for k in order]


def _make_group(
    group_type: str,
    name: str,
    state: str | None,
    sprint_id: str | None,
    tickets: list[dict],
) -> dict:
    return {
        "group_type": group_type,
        "name": name,
        "state": state,
        "sprint_id": sprint_id,
        "epics": _group_by_epic(tickets),
    }


async def _sprint_groups(
    tools: dict,
    board_id: str,
    *,
    per_group_limit: int,
    board_limit: int,
    include_closed_sprints: bool,
    include_done: bool,
    include_empty: bool,
) -> list[dict]:
    """Build a group per sprint on a board. Returns [] if the board has none.

    Works for both classic ``scrum`` boards and team-managed ``simple`` boards;
    boards that don't support sprints simply yield an empty list.
    """
    states = ["active", "future"]
    if include_closed_sprints:
        states.append("closed")

    groups: list[dict] = []
    for state in states:
        try:
            sprints = _records_from_payload(
                await _call_tool(
                    tools,
                    "jira_get_sprints_from_board",
                    board_id=board_id,
                    state=state,
                    limit=board_limit,
                )
            )
        except Exception:  # noqa: BLE001 - kanban boards reject sprint queries
            return []
        for sprint in sprints:
            sprint_id = str(sprint.get("id", "")).strip()
            if not sprint_id:
                continue
            tickets = _finalize_tickets(
                _issues_from_payload(
                    await _call_tool(
                        tools,
                        "jira_get_sprint_issues",
                        sprint_id=sprint_id,
                        limit=per_group_limit,
                        fields="*all",
                    )
                ),
                include_done,
            )
            if tickets or include_empty:
                groups.append(
                    _make_group(
                        "sprint",
                        sprint.get("name", f"Sprint {sprint_id}"),
                        sprint.get("state", state),
                        sprint_id,
                        tickets,
                    )
                )
    return groups


async def _groups_for_board(
    tools: dict,
    board_id: str,
    board_name: str,
    board_type: str,
    *,
    per_group_limit: int,
    board_limit: int,
    include_closed_sprints: bool,
    include_backlog: bool,
    include_done: bool,
    include_empty: bool,
    issue_jql: str,
    backlog_jql: str,
) -> list[dict]:
    groups: list[dict] = []

    # Try sprints for anything that isn't an explicit kanban board; team-managed
    # ("simple") boards are sprint-capable and would otherwise be missed.
    if board_type != "kanban":
        groups = await _sprint_groups(
            tools,
            board_id,
            per_group_limit=per_group_limit,
            board_limit=board_limit,
            include_closed_sprints=include_closed_sprints,
            include_done=include_done,
            include_empty=include_empty,
        )

    if groups:
        if include_backlog:
            backlog = _finalize_tickets(
                _issues_from_payload(
                    await _call_tool(
                        tools,
                        "jira_get_board_issues",
                        board_id=board_id,
                        jql=backlog_jql,
                        limit=per_group_limit,
                        fields="*all",
                    )
                ),
                include_done,
            )
            if backlog or include_empty:
                groups.append(_make_group("backlog", "Backlog", None, None, backlog))
        return groups

    # No sprints: collapse the board into a single group of its issues.
    tickets = _finalize_tickets(
        _issues_from_payload(
            await _call_tool(
                tools,
                "jira_get_board_issues",
                board_id=board_id,
                jql=issue_jql,
                limit=per_group_limit,
                fields="*all",
            )
        ),
        include_done,
    )
    groups.append(_make_group("board", board_name or "Board", None, None, tickets))
    return groups


async def list_spaces(
    *,
    project_key: str | None = None,
    per_group_limit: int = 50,
    board_limit: int = 50,
    include_closed_sprints: bool = False,
    include_backlog: bool = True,
    include_done: bool = False,
    include_empty: bool = True,
) -> list[dict]:
    """Build the full ``space -> board -> group -> epic -> ticket`` hierarchy.

    Returns a list of space entries shaped as::

        [
          {
            "space_key": "JOY", "space_name": "Joyalty", "space_id": "10000",
            "boards": [
              {
                "board_id": "1001", "board_name": "Joyalty Scrum",
                "board_type": "scrum",
                "groups": [
                  {"group_type": "sprint", "sprint_id": "42",
                   "name": "Sprint 5", "state": "active",
                   "epics": [
                     {"key": "JOY-1", "name": "Checkout", "tickets": [...]},
                     {"key": None, "name": "No epic", "tickets": [...]},
                   ]},
                ],
              },
            ],
            "loose_groups": [...],  # tickets with no board
          },
        ]

    Empty sprints/boards are retained when ``include_empty`` is set, and spaces
    without any agile board surface their issues under ``loose_groups`` so
    nothing is hidden from the browser.
    """
    project_key = project_key if project_key is not None else config.jira_project_key
    issue_jql = config.grouped_issue_jql
    backlog_jql = "sprint is EMPTY"
    if not include_done:
        backlog_jql += " AND statusCategory != Done"
    backlog_jql += " ORDER BY priority DESC"

    tools = await _jira_tools()

    projects = _records_from_payload(await _call_tool(tools, "jira_get_all_projects"))
    meta: dict[str, dict] = {}
    for project in projects:
        key = str(project.get("key", "")).strip()
        if key:
            meta[key] = {"name": project.get("name", key), "id": str(project.get("id", ""))}
    if project_key:
        wanted = project_key.upper()
        meta = {k: v for k, v in meta.items() if k.upper() == wanted} or {
            project_key: {"name": project_key, "id": ""}
        }

    spaces: list[dict] = []
    for key, info in meta.items():
        boards_raw = _records_from_payload(
            await _call_tool(
                tools, "jira_get_agile_boards", project_key=key, limit=board_limit
            )
        )

        boards: list[dict] = []
        for board in boards_raw:
            board_id = str(board.get("id", "")).strip()
            if not board_id:
                continue
            board_type = (board.get("type") or "").strip().lower()
            groups = await _groups_for_board(
                tools,
                board_id,
                board.get("name", ""),
                board_type,
                per_group_limit=per_group_limit,
                board_limit=board_limit,
                include_closed_sprints=include_closed_sprints,
                include_backlog=include_backlog,
                include_done=include_done,
                include_empty=include_empty,
                issue_jql=issue_jql,
                backlog_jql=backlog_jql,
            )
            if groups or include_empty:
                boards.append(
                    {
                        "board_id": board_id,
                        "board_name": board.get("name", ""),
                        "board_type": board_type or "kanban",
                        "groups": groups,
                    }
                )

        # Spaces without a board still surface their issues directly.
        loose_groups: list[dict] = []
        if not boards:
            loose = _finalize_tickets(
                _issues_from_payload(
                    await _call_tool(
                        tools,
                        "jira_search",
                        jql=f'project = "{key}" AND {issue_jql}',
                        limit=per_group_limit,
                        fields="*all",
                    )
                ),
                include_done,
            )
            if loose or include_empty:
                loose_groups.append(
                    _make_group("project", info["name"] or key, None, None, loose)
                )

        if boards or loose_groups or include_empty:
            spaces.append(
                {
                    "space_key": key,
                    "space_name": info["name"],
                    "space_id": info["id"],
                    "boards": boards,
                    "loose_groups": loose_groups,
                }
            )

    return spaces


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


def _created_issue(payload, *, fallback_summary: str = "") -> dict:
    """Normalize the ``jira_create_issue`` payload into ``{key, url, summary}``.

    The Atlassian MCP server returns the new issue either at the top level or
    nested under an ``issue`` key, so probe both. When no browse URL is present
    we synthesize one from ``JIRA_URL`` and the issue key.
    """
    data = payload if isinstance(payload, dict) else {}
    issue = data.get("issue") if isinstance(data.get("issue"), dict) else data

    key = issue.get("key") or data.get("key") or ""
    url = (
        issue.get("url")
        or issue.get("self")
        or data.get("url")
        or (f"{(config.jira_url or '').rstrip('/')}/browse/{key}" if key else "")
    )
    return {
        "key": key,
        "url": url,
        "summary": issue.get("summary") or data.get("summary") or fallback_summary,
    }


async def create_ticket(
    *,
    project_key: str,
    summary: str,
    issue_type: str = "Task",
    description: str = "",
    priority: str | None = None,
    labels: list[str] | None = None,
    epic_key: str | None = None,
    assignee: str | None = None,
    components: str | None = None,
) -> dict:
    """Create a new Jira issue via the Atlassian MCP ``jira_create_issue`` tool.

    ``priority``, ``labels`` and ``epic_key`` are folded into the tool's
    ``additional_fields`` JSON string. The epic link is sent as ``parent`` (the
    portable form that works for team-managed projects and any issue type).
    Returns a normalized ``{key, url, summary}`` dict.
    """
    config.require("jira_url", "jira_username", "jira_api_token")
    tools = await _jira_tools()

    additional: dict = {}
    if priority:
        additional["priority"] = {"name": priority}
    if labels:
        additional["labels"] = labels
    if epic_key:
        additional["parent"] = epic_key

    kwargs: dict = {
        "project_key": project_key,
        "summary": summary,
        "issue_type": issue_type,
    }
    if description:
        kwargs["description"] = description
    if assignee:
        kwargs["assignee"] = assignee
    if components:
        kwargs["components"] = components
    if additional:
        kwargs["additional_fields"] = json.dumps(additional)

    payload = await _call_tool(tools, "jira_create_issue", **kwargs)
    return _created_issue(payload, fallback_summary=summary)


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
