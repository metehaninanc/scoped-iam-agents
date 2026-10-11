"""PreToolUse hook: an agent may only call its own gateway tools.

Early failure, not the boundary. The gateway checks the bearer token's tool list again and
trusts nothing this hook did. The tool lists come from `policy/policy.yaml`, the same file the
gateway uses, so the two cannot drift apart. Fails closed: a gateway tool call from an agent
that cannot be identified is denied.
"""

import re

from ..gateway.config import ROOT
from ..gateway.policy import Policy
from .common import agent_of, deny_tool_use, read_input

_GW_TOOL = re.compile(r"^mcp__gw-([a-z]+)__(.+)$")


def decide(data: dict, policy: Policy) -> tuple[bool, str]:
    tool_name = str(data.get("tool_name") or "")
    m = _GW_TOOL.match(tool_name)
    if m is None:
        return False, f"domain_guard: {tool_name or 'unknown tool'} is not a gateway tool"
    server, tool = m.groups()
    agent = agent_of(data)
    if agent is None:
        return False, "domain_guard: calling agent unknown (agent_type missing or unrecognised)"
    if server != agent:
        return False, f"domain_guard: agent {agent} may not call gateway server gw-{server}"
    if tool not in policy.tools_for(agent):
        return False, f"domain_guard: {tool} is not in the tool list of agent {agent}"
    return True, "ok"


def main() -> int:
    data = read_input()
    allowed, reason = decide(data, Policy.load(ROOT / "policy" / "policy.yaml"))
    if not allowed:
        deny_tool_use(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
