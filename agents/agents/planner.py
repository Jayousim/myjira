"""Python port of ``agents/src/agents/planner.ts``."""

from __future__ import annotations

import asyncio
import json
import re

from anthropic import Anthropic

from agent_types import Plan, PlanStep, Task
from config import CONFIG
from context.gatherer import gather_context
from context.project_map import PROJECT_CONVENTIONS

PLANNER_SYSTEM_PROMPT = f"""You are a senior full-stack architect planning implementation steps for the Joyalty coffee loyalty app.

{PROJECT_CONVENTIONS}

Your job is to take a feature/bug description and produce a detailed, ordered implementation plan.

Rules:
1. Each step should be atomic — it touches a small number of files and can be validated independently.
2. Order steps so that dependencies come first (e.g., entity before repository before service before controller).
3. For full-stack features, follow this order: DB migration → Backend entity → Repository → Service → Controller/DTO → Frontend API service → Frontend screen/component.
4. Be specific about file paths, class names, method signatures, and field names.
5. Include any configuration changes needed (application.properties, app.json, etc.).
6. Each step must have a clear expected outcome that can be verified.

Respond with ONLY valid JSON matching this schema:
{{
  "summary": "Brief description of the overall change",
  "steps": [
    {{
      "stepNumber": 1,
      "title": "Short title for the step",
      "description": "Detailed description of what to implement, including specific code patterns to follow",
      "filesToTouch": ["path/to/file1.ts", "path/to/file2.java"],
      "expectedOutcome": "What should be true after this step is complete"
    }}
  ]
}}"""


def _to_plan_step(raw: dict) -> PlanStep:
    return PlanStep(
        step_number=raw.get("stepNumber", 0),
        title=raw.get("title", ""),
        description=raw.get("description", ""),
        files_to_touch=raw.get("filesToTouch", []) or [],
        expected_outcome=raw.get("expectedOutcome", ""),
    )


async def generate_plan(task: Task) -> Plan:
    client = Anthropic(api_key=CONFIG.anthropic_api_key)
    context = await gather_context(task.description)

    context_block = "\n\n".join(
        f"### {f['path']}\n```\n{f['content']}\n```" for f in context.relevant_files
    )

    user_message = f"""## Task
**{task.title}** (ID: {task.id})

{task.description}

## Project File Tree
```
{context.file_tree}
```

## Relevant Existing Files
{context_block}

Please produce a step-by-step implementation plan as JSON."""

    response = await asyncio.to_thread(
        client.messages.create,
        model=CONFIG.planner_model,
        max_tokens=4096,
        system=PLANNER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    text_content = next((c for c in response.content if c.type == "text"), None)
    if text_content is None:
        raise RuntimeError("Planner returned no text content")

    json_str = text_content.text.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", json_str)
    if fence_match:
        json_str = fence_match.group(1).strip()

    parsed = json.loads(json_str)

    return Plan(
        task_id=task.id,
        summary=parsed["summary"],
        steps=[_to_plan_step(s) for s in parsed.get("steps", [])],
    )
