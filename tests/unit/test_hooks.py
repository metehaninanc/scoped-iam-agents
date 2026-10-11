"""Runtime hooks. Hooks are early failure and observability, never the security boundary."""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta

import pytest

from iamagents.gateway.config import ROOT
from iamagents.gateway.policy import Policy
from iamagents.hooks import domain_guard, hop_limit, result_schema, trace

POLICY = Policy.load(ROOT / "policy" / "policy.yaml")
RUNTIME = ROOT / "runtime"


def payload(tool_name="mcp__gw-ds__get_user", agent_type="ds", **extra):
    return {
        "session_id": "sess-1",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"ticket_id": "IAM-001", "upn": "zoe.martin@<tenant>.onmicrosoft.com"},
        "agent_type": agent_type,
        **extra,
    }


# --- domain_guard ---


@pytest.mark.parametrize(
    "agent_type,tool",
    [
        ("ds", "mcp__gw-ds__get_user"),
        ("ds", "mcp__gw-ds__reset_password"),
        ("am", "mcp__gw-am__read_signins"),
        ("ig", "mcp__gw-ig__issue_change_request"),
        ("pam", "mcp__gw-pam__prepare_package"),
        ("team-lead", "mcp__gw-tl__close_ticket"),
    ],
)
def test_domain_guard_allows_own_tools(agent_type, tool):
    assert domain_guard.decide(payload(tool, agent_type), POLICY) == (True, "ok")


@pytest.mark.parametrize(
    "agent_type,tool",
    [
        ("ds", "mcp__gw-am__read_signins"),  # another agent's server
        ("ds", "mcp__gw-am__get_user"),  # same tool name, other server
        ("am", "mcp__gw-am__reset_password"),  # right server, tool not in its list
        ("ds", "mcp__gw-ds__reset_mfa"),
        ("team-lead", "mcp__gw-ds__get_user"),
        ("pam", "mcp__gw-ig__create_user"),
        ("ds", "mcp__other__get_user"),  # not a gateway server
        ("ds", "Bash"),
        ("ds", ""),
        ("mystery", "mcp__gw-ds__get_user"),  # unknown agent type
        (None, "mcp__gw-ds__get_user"),  # missing agent type fails closed
    ],
)
def test_domain_guard_blocks_everything_else(agent_type, tool):
    data = payload(tool, agent_type or "")
    if agent_type is None:
        data.pop("agent_type")
    allowed, reason = domain_guard.decide(data, POLICY)
    assert not allowed and reason.startswith("domain_guard:")


def test_domain_guard_tool_lists_come_from_the_policy():
    for agent, hook_id in (("ds", "ds"), ("am", "am"), ("ig", "ig"), ("pam", "pam")):
        for tool in POLICY.tools_for(agent):
            assert domain_guard.decide(payload(f"mcp__gw-{hook_id}__{tool}", agent), POLICY)[0]


# --- hop_limit ---


def test_hop_limit_allows_three_dispatches_and_blocks_the_fourth(tmp_path):
    for i in range(3):
        assert hop_limit.decide(payload("Agent"), 3, tmp_path) == (True, "ok"), i
    allowed, reason = hop_limit.decide(payload("Agent"), 3, tmp_path)
    assert not allowed and "Escalate" in reason
    assert not hop_limit.decide(payload("Agent"), 3, tmp_path)[0]  # stays blocked


def test_hop_limit_counts_per_session(tmp_path):
    for _ in range(3):
        hop_limit.decide(payload("Agent"), 3, tmp_path)
    other = payload("Agent") | {"session_id": "sess-2"}
    assert hop_limit.decide(other, 3, tmp_path)[0]


def test_hop_limit_without_session_id_fails_closed(tmp_path):
    data = payload("Agent")
    data.pop("session_id")
    assert not hop_limit.decide(data, 3, tmp_path)[0]


def test_hop_limit_comes_from_policy():
    assert hop_limit.max_dispatches() == 3  # reroute_limit 2 plus the initial route


def test_hop_limit_records_dispatch_targets(tmp_path):
    hop_limit.decide(payload("Agent") | {"tool_input": {"subagent_type": "ds"}}, 3, tmp_path)
    state = json.loads((tmp_path / "hops-sess-1.json").read_text(encoding="utf-8"))
    assert state["dispatches"] == ["ds"]


# --- trace ---


def post_payload(response, **extra):
    return payload(hook_event_name="PostToolUse", agent_id="agent-abc", **extra) | {
        "tool_response": response
    }


RESULT = {
    "ok": True,
    "decision": "allowed",
    "rule_id": "READ-01",
    "tier": "T0",
    "data": {"displayName": "Zoe Martin", "note": "+49 30 5550 0000"},
}


@pytest.mark.parametrize(
    "response",
    [
        {"content": [{"type": "text", "text": json.dumps(RESULT)}]},
        [{"type": "text", "text": json.dumps(RESULT)}],
        json.dumps(RESULT),
        RESULT,
    ],
    ids=["content-dict", "list", "string", "plain-dict"],
)
def test_trace_records_agent_type_and_only_the_decision_fields(response):
    record = trace.build_record(post_payload(response))
    assert record["agent_type"] == "ds" and record["agent_id"] == "agent-abc"
    assert record["tool"] == "mcp__gw-ds__get_user"
    assert record["result"] == {
        "ok": True,
        "decision": "allowed",
        "rule_id": "READ-01",
        "tier": "T0",
    }
    assert "5550" not in json.dumps(record) and "Zoe" not in json.dumps(record)


def test_trace_survives_an_unknown_response_shape():
    assert trace.build_record(post_payload(12345))["result"] == {}
    assert trace.build_record(post_payload(None))["result"] == {}


def test_trace_clips_long_argument_values():
    data = post_payload(RESULT)
    data["tool_input"] = {"upn": "x" * 5000}
    assert len(trace.build_record(data)["args"]["upn"]) < 300


def test_trace_file_and_purge(tmp_path, monkeypatch):
    path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("IAM_TRACE_PATH", str(path))
    trace.append_event(trace.build_record(post_payload(RESULT)))
    line = json.loads(path.read_text(encoding="utf-8"))
    assert line["event"] == "tool_call" and "ts" in line
    # an old line is purged, a new one kept
    old = {"ts": (datetime.now().astimezone() - timedelta(days=100)).isoformat(), "event": "x"}
    path.write_text(json.dumps(old) + "\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    assert trace.purge(path, 90, datetime.now().astimezone()) == 1
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


# --- result_schema ---

GOOD = {
    "outcome": "executed",
    "tier": "T1",
    "actions": ["DS-02 reset_password zoe.martin"],
    "route_to": None,
    "reason": "done",
}


def stop(message, **extra):
    return {
        "session_id": "sess-1",
        "hook_event_name": "Stop",
        "agent_type": "ds",
        "last_assistant_message": message,
        **extra,
    }


@pytest.mark.parametrize(
    "message",
    [
        json.dumps(GOOD),
        "Reset done.\n" + json.dumps(GOOD),
        "Reset done.\n```json\n" + json.dumps(GOOD, indent=2) + "\n```",
        json.dumps(GOOD) + "\n",
        json.dumps(GOOD | {"route_to": "pam", "tier": "T3", "outcome": "prepared_for_human"}),
        json.dumps(GOOD | {"tier": "NONE", "outcome": "rejected_policy"}),
    ],
)
def test_result_schema_accepts_valid_final_objects(message):
    assert result_schema.check(message) is None


@pytest.mark.parametrize(
    "message",
    [
        "",
        "All done, password reset.",
        json.dumps(GOOD) + "\nHope that helps!",  # prose after the object
        json.dumps(GOOD | {"outcome": "done"}),
        json.dumps(GOOD | {"tier": "T9"}),
        json.dumps(GOOD | {"route_to": "tl"}),
        json.dumps(GOOD | {"actions": "reset"}),
        json.dumps({k: v for k, v in GOOD.items() if k != "reason"}),
        json.dumps(GOOD | {"extra": 1}),
        '{"outcome": "executed"',  # truncated
    ],
)
def test_result_schema_rejects_everything_else(message):
    assert result_schema.check(message) is not None


def test_result_schema_outcomes_match_the_dataset():
    import importlib.util

    spec = importlib.util.spec_from_file_location("v", ROOT / "data" / "validate.py")
    v = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v)
    assert set(result_schema.OUTCOMES) == set(v.OUTCOME_TIERS)


def test_result_schema_blocks_once_then_records_a_violation(tmp_path):
    data = stop("no json here")
    assert result_schema.decide(data, tmp_path)[0] == "retry"
    assert result_schema.decide(data, tmp_path)[0] == "violation"
    assert result_schema.decide(data, tmp_path)[0] == "violation"


def test_result_schema_valid_after_retry_is_ok(tmp_path):
    assert result_schema.decide(stop("oops"), tmp_path)[0] == "retry"
    assert result_schema.decide(stop(json.dumps(GOOD)), tmp_path)[0] == "ok"


def test_result_schema_stop_hook_active_never_blocks_again(tmp_path):
    assert result_schema.decide(stop("oops", stop_hook_active=True), tmp_path)[0] == "violation"


def test_result_schema_ignores_the_team_lead(tmp_path):
    assert (
        result_schema.decide(stop("whatever") | {"agent_type": "team-lead"}, tmp_path)[0]
        == "ignore"
    )
    assert result_schema.decide({"session_id": "s"}, tmp_path)[0] == "ignore"


# --- the hooks as settings.json runs them (subprocess, stdin JSON, stdout/exit code) ---


def run_hook(module, data, env_extra=None):
    env = os.environ.copy() | (env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", f"iamagents.hooks.{module}"],
        input=json.dumps(data),
        capture_output=True,
        text=True,
        cwd=RUNTIME,
        env=env,
        timeout=60,
    )


def test_domain_guard_subprocess_denies_with_structured_output():
    r = run_hook("domain_guard", payload("mcp__gw-am__read_signins", "ds"))
    assert r.returncode == 0
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
    assert "gw-am" in out["permissionDecisionReason"]


def test_domain_guard_subprocess_allows_silently():
    r = run_hook("domain_guard", payload())
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_domain_guard_subprocess_fails_closed_on_garbage_input():
    r = subprocess.run(
        [sys.executable, "-m", "iamagents.hooks.domain_guard"],
        input="not json",
        capture_output=True,
        text=True,
        cwd=RUNTIME,
    )
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hop_limit_subprocess_blocks_the_fourth_dispatch(tmp_path):
    env = {"IAM_STATE_DIR": str(tmp_path)}
    data = payload("Agent") | {"tool_input": {"subagent_type": "ds"}}
    for _ in range(3):
        assert run_hook("hop_limit", data, env).stdout.strip() == ""
    fourth = run_hook("hop_limit", data, env)
    assert json.loads(fourth.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_trace_subprocess_writes_the_trace_file(tmp_path):
    path = tmp_path / "t.jsonl"
    r = run_hook("trace", post_payload(RESULT), {"IAM_TRACE_PATH": str(path)})
    assert r.returncode == 0
    assert json.loads(path.read_text(encoding="utf-8"))["agent_type"] == "ds"


def test_result_schema_subprocess_retry_then_schema_violation_in_trace(tmp_path):
    env = {"IAM_STATE_DIR": str(tmp_path), "IAM_TRACE_PATH": str(tmp_path / "trace.jsonl")}
    first = run_hook("result_schema", stop("no json"), env)
    assert json.loads(first.stdout)["decision"] == "block"
    second = run_hook("result_schema", stop("still no json"), env)
    assert second.returncode == 0 and second.stdout.strip() == ""
    [event] = [
        json.loads(x) for x in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert event["event"] == "schema_violation" and event["agent_type"] == "ds"
    assert "no json" not in json.dumps(event)  # the message text is not logged


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_the_exact_command_from_settings_json_runs():
    settings = json.loads((RUNTIME / ".claude" / "settings.json").read_text(encoding="utf-8"))
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    r = subprocess.run(
        command,
        shell=True,
        input=json.dumps(payload("mcp__gw-am__read_signins", "ds")),
        capture_output=True,
        text=True,
        cwd=RUNTIME,
        timeout=120,
    )
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
