"""PreToolUse hook on the dispatch tool: at most two reroutes per ticket.

The first dispatch is the initial route; each further dispatch is a reroute, so the 4th dispatch
(a third reroute) is denied. Early failure, not the boundary: the gateway counts distinct domain
agent identities per active ticket and refuses further calls and `close_ticket` on its own.

ASSUMPTION: one ticket per `claude -p` session. The counter is keyed on `session_id`, so a
session that handled several tickets would share one count. The eval harness (task 0.10)
starts a fresh session per ticket and asserts that.
"""

import json

from ..gateway.config import ROOT
from ..gateway.policy import Policy
from .common import deny_tool_use, read_input, safe_name, state_dir


def max_dispatches() -> int:
    policy = Policy.load(ROOT / "policy" / "policy.yaml")
    return int(policy.global_.get("reroute_limit", 2)) + 1


def decide(data: dict, limit: int, directory=None) -> tuple[bool, str]:
    session = data.get("session_id")
    if not session:
        return False, "hop_limit: session_id missing, cannot count dispatches"
    path = (directory or state_dir()) / f"hops-{safe_name(str(session))}.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"dispatches": []}
    if len(state["dispatches"]) >= limit:
        return False, (
            f"hop_limit: this ticket was already dispatched {limit} times "
            f"(initial route plus {limit - 1} reroutes). Escalate instead of rerouting."
        )
    target = (data.get("tool_input") or {}).get("subagent_type")
    state["dispatches"].append(target or "unknown")
    path.write_text(json.dumps(state), encoding="utf-8")
    return True, "ok"


def main() -> int:
    allowed, reason = decide(read_input(), max_dispatches())
    if not allowed:
        deny_tool_use(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
