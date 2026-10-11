"""Shared helpers for the runtime hooks.

Hooks run inside the client (Claude Code) and are observability and early failure, never the
security boundary: the gateway and Entra enforce everything again and trust nothing a hook did.
Each hook reads one JSON object from stdin and answers through its stdout and exit code.
"""

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

# Frontmatter `name` of each agent -> gateway identity.
AGENT_IDS = {"ds": "ds", "am": "am", "ig": "ig", "pam": "pam", "team-lead": "tl"}
DOMAIN_AGENT_TYPES = ("ds", "am", "ig", "pam")

_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def read_input() -> dict:
    """Parse stdin. Unparseable input becomes {} so each hook can fail closed on its own terms."""
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def agent_of(data: dict) -> str | None:
    """Gateway identity of the agent the hook fired in, from `agent_type`."""
    return AGENT_IDS.get(str(data.get("agent_type") or ""))


def state_dir() -> Path:
    path = Path(os.environ.get("IAM_STATE_DIR") or ROOT / "runtime" / ".state")
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_name(value: str) -> str:
    return _SAFE.sub("_", value)[:120] or "unknown"


def deny_tool_use(reason: str) -> None:
    """PreToolUse denial: exit 0 with the structured decision."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def block_stop(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}))
