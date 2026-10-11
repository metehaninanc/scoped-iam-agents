"""A stub agent (plain MCP client, no LLM) against the real HTTP gateway, with the hooks in front.

The stub models what Claude Code does: before each tool call it runs the domain_guard hook,
after it the trace hook. Tests also skip the hook to show the gateway holds on its own.
"""

import json

from conftest import TOKENS, call, upn, with_session

from iamagents.gateway.config import ROOT
from iamagents.gateway.policy import Policy
from iamagents.hooks import domain_guard, trace

POLICY = Policy.load(ROOT / "policy" / "policy.yaml")
ZOE = upn("zoe.martin")


class StubAgent:
    def __init__(self, agent_type: str, url: str, trace_file, identity: str | None = None):
        self.agent_type = agent_type
        self.identity = identity or agent_type
        self.url = url
        self.trace_file = trace_file

    def call(self, server: str, tool: str, args: dict, use_hook: bool = True):
        """Returns ('blocked', reason) if the hook denies, else the gateway's JSON result."""
        name = f"mcp__gw-{server}__{tool}"
        event = {
            "session_id": "s-1",
            "agent_type": self.agent_type,
            "tool_name": name,
            "tool_input": args,
        }
        if use_hook:
            allowed, reason = domain_guard.decide(event, POLICY)
            if not allowed:
                return "blocked", reason
        result = with_session(self.url, TOKENS[self.identity], lambda s: call(s, tool, args))
        post = event | {
            "tool_response": {"content": [{"type": "text", "text": json.dumps(result)}]}
        }
        trace.append_event(trace.build_record(post), self.trace_file)
        return result

    def tools(self) -> list[str]:
        async def names(session):
            return sorted(t.name for t in (await session.list_tools()).tools)

        return with_session(self.url, TOKENS[self.identity], names)


def read_trace(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_stub_ds_calls_get_user_through_its_own_token(served, tmp_path):
    gw, url = served
    gw.bind_ticket("T-1")
    agent = StubAgent("ds", url, tmp_path / "trace.jsonl")
    assert agent.tools() == ["get_group", "get_user", "list_group_members", "reset_password"]
    result = agent.call("ds", "get_user", {"ticket_id": "T-1", "upn": ZOE})
    assert result["ok"] and result["data"]["displayName"] == "Zoe Martin"
    assert gw.audit.read_all()[-1]["agent"] == "ds"
    assert gw.graph_factory.directory.calls[-1][0] == "ds"
    [entry] = read_trace(tmp_path / "trace.jsonl")
    assert entry["agent_type"] == "ds" and entry["tool"] == "mcp__gw-ds__get_user"
    assert entry["result"]["decision"] == "allowed" and "data" not in entry["result"]


def test_domain_guard_blocks_ds_from_calling_an_am_tool(served, tmp_path):
    gw, url = served
    gw.bind_ticket("T-1")
    agent = StubAgent("ds", url, tmp_path / "trace.jsonl")
    before = len(gw.audit.read_all())
    status, reason = agent.call("am", "read_signins", {"ticket_id": "T-1", "upn": ZOE})
    assert status == "blocked" and "gw-am" in reason
    assert len(gw.audit.read_all()) == before  # the call never reached the gateway
    assert not (tmp_path / "trace.jsonl").exists()


def test_gateway_stops_the_same_call_when_the_hook_is_skipped(served, tmp_path):
    gw, url = served
    gw.bind_ticket("T-1")
    agent = StubAgent("ds", url, tmp_path / "trace.jsonl")
    result = agent.call("am", "read_signins", {"ticket_id": "T-1", "upn": ZOE}, use_hook=False)
    assert result["reason"] == "tool_not_permitted"
    entry = gw.audit.read_all()[-1]
    assert entry["agent"] == "ds" and entry["decision"] == "denied"


def test_identity_comes_from_the_token_not_the_server_alias(served, tmp_path):
    # The hook is skipped and the call is dressed as gw-am, but the token is ds's.
    gw, url = served
    gw.bind_ticket("T-1")
    agent = StubAgent("ds", url, tmp_path / "trace.jsonl")
    agent.call("am", "get_user", {"ticket_id": "T-1", "upn": ZOE}, use_hook=False)
    assert gw.audit.read_all()[-1]["agent"] == "ds"
    assert gw.graph_factory.directory.calls[-1][0] == "ds"


def test_hop_limit_is_enforced_by_the_gateway_without_the_hook(served, tmp_path):
    gw, url = served
    gw.policy.identities["pam"].tools = ["get_user"]  # let a 4th identity reach the check
    gw.policy.rule_for("ds", "get_user").agent = ["ds", "am", "ig", "pam"]
    gw.bind_ticket("T-1")
    args = {"ticket_id": "T-1", "upn": ZOE}
    results = [
        StubAgent(a, url, tmp_path / "t.jsonl").call(a, "get_user", args, use_hook=False)
        for a in ("ds", "am", "ig", "pam")
    ]
    assert [r["ok"] for r in results] == [True, True, True, False]
    assert results[3]["reason"] == "hop_limit_exceeded"
    assert gw.audit.read_all()[-1]["reason"] == "hop_limit_exceeded"
    assert gw.hop_exceeded and gw.close_allowed() == (False, "hop_limit_exceeded")
    # an agent that already acted can still call; the extra identity stays out
    assert StubAgent("ds", url, tmp_path / "t.jsonl").call("ds", "get_user", args)["ok"]
    assert not StubAgent("pam", url, tmp_path / "t.jsonl").call(
        "pam", "get_user", args, use_hook=False
    )["ok"]
