"""Stop / SubagentStop hook: a domain agent must end with the result JSON.

Expected last object: {"outcome", "tier", "actions", "route_to", "reason"}. The hook blocks the
stop once with a reason so the agent can fix its answer. If the retry is also wrong, the agent
is allowed to stop and a `schema_violation` event goes to the trace; the scoring counts that
ticket as a failure, never as a pass. Early failure, not the boundary.

Runs for Stop (a domain agent as the main agent, Sprints 1 to 4) and SubagentStop (Sprint 5).
The team lead has a different final format and is ignored here.
"""

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from .common import DOMAIN_AGENT_TYPES, block_stop, read_input, safe_name, state_dir
from .trace import append_event

OUTCOMES = (
    "executed",
    "partially_executed",
    "waiting_for_approval",
    "prepared_for_human",
    "rerouted_then_resolved",
    "rejected_policy",
    "escalated_to_human",
    "out_of_scope",
)


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal[OUTCOMES]
    tier: Literal["T0", "T1", "T2", "T3", "NONE"]
    actions: list[str]
    route_to: Literal["ds", "am", "ig", "pam"] | None
    reason: str


_FENCE = re.compile(r"\s*```\s*$")


def final_json(text: str) -> dict | None:
    """The JSON object the message ends with, or None. A closing code fence is allowed."""
    body = _FENCE.sub("", text.rstrip())
    decoder = json.JSONDecoder()
    for i, ch in enumerate(body):
        if ch != "{":
            continue
        try:
            value, end = decoder.raw_decode(body, i)
        except json.JSONDecodeError:
            continue
        if end == len(body) and isinstance(value, dict):
            return value
    return None


def check(message: str) -> str | None:
    """Return None if the message ends with a valid result object, else a short problem text."""
    obj = final_json(message or "")
    if obj is None:
        return "the message does not end with a JSON object"
    try:
        AgentResult.model_validate(obj)
    except ValidationError as e:
        fields = sorted({".".join(str(p) for p in err["loc"]) or "object" for err in e.errors()})
        return "invalid result object, check: " + ", ".join(fields)
    return None


def decide(data: dict, directory=None) -> tuple[str, str]:
    """Return (action, detail) where action is ignore | ok | retry | violation."""
    agent_type = str(data.get("agent_type") or "")
    if agent_type not in DOMAIN_AGENT_TYPES:
        return "ignore", "not a domain agent"
    problem = check(str(data.get("last_assistant_message") or ""))
    if problem is None:
        return "ok", "valid"
    who = safe_name(str(data.get("agent_id") or agent_type))
    key = f"retry-{safe_name(str(data.get('session_id')))}-{who}"
    marker = (directory or state_dir()) / f"{key}.flag"
    if marker.exists() or data.get("stop_hook_active"):
        return "violation", problem
    marker.write_text("1", encoding="utf-8")
    return "retry", problem


def main() -> int:
    data = read_input()
    action, detail = decide(data)
    if action == "retry":
        block_stop(
            "Your final message must end with one JSON object with the keys outcome, tier, "
            f"actions, route_to, reason. Problem: {detail}."
        )
    elif action == "violation":
        append_event(
            {
                "event": "schema_violation",
                "session_id": data.get("session_id"),
                "agent_type": data.get("agent_type"),
                "agent_id": data.get("agent_id"),
                "detail": detail,
            }
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
