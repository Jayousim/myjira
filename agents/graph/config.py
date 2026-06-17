import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Jira / Atlassian (consumed by the mcp-atlassian server)
    jira_url: str | None = os.getenv("JIRA_URL")
    jira_username: str | None = os.getenv("JIRA_USERNAME")
    jira_api_token: str | None = os.getenv("JIRA_API_TOKEN")

    # Which tickets to consider for implementation
    sprint_jql: str = os.getenv(
        "SPRINT_JQL",
        'sprint in openSprints() AND statusCategory != Done ORDER BY priority DESC',
    )

    # Planning / orchestration model (Anthropic)
    planner_model: str = os.getenv("PLANNER_MODEL", "claude-sonnet-4-6")

    # Cursor SDK (the implementer)
    cursor_api_key: str | None = os.getenv("CURSOR_API_KEY")
    cursor_model: str = os.getenv("CURSOR_MODEL", "composer-2.5")

    # Target repo the coding agent implements against (cloud runtime)
    # e.g. "github.com/your-org/your-repo"
    target_repo: str | None = os.getenv("TARGET_REPO")
    target_repo_ref: str | None = os.getenv("TARGET_REPO_REF")  # base branch, optional

    def mcp_config(self) -> dict:
        return {
            "jira": {
                "command": "uvx",
                "args": ["mcp-atlassian"],
                "transport": "stdio",
                "env": {
                    "JIRA_URL": self.jira_url,
                    "JIRA_USERNAME": self.jira_username,
                    "JIRA_API_TOKEN": self.jira_api_token,
                },
            }
        }

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise RuntimeError(
                "Missing required config (set these in .env): " + ", ".join(missing)
            )


config = Config()
