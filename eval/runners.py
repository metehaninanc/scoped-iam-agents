"""Runners: how one ticket is handled by the system under test.

`ClaudeRunner` runs the real `claude -p` from a freshly rendered runtime with a scrubbed
environment (see `iamagents.launch`). `StubRunner` needs no model: it always escalates, which
exercises the whole pipeline (fixtures, binding, audit, scoring, report) over any ticket set.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from iamagents import launch
from iamagents.hooks.result_schema import check, final_json


@dataclass
class RunContext:
    ticket: dict
    agent: str
    prompt: str
    gateway: object  # iamagents.gateway.core.Gateway
    gateway_url: str | None
    workdir: Path
    model: str = "haiku"
    max_turns: int = 12
    timeout: int = 600
    use_config_dir: bool = False


@dataclass
class RunOutput:
    final_text: str | None = None
    result: dict | None = None
    session_ids: list[str] = field(default_factory=list)
    dispatched: list[str] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    cost_usd: float | None = None
    turns: int | None = None
    duration_s: float = 0.0
    error: str | None = None
    schema_violation: bool = False
    stream_path: str | None = None


AUTH_FAILURE = re.compile(
    r"Failed to authenticate|Not logged in|OAuth session|/login|credit balance", re.I
)


class RunAborted(Exception):
    """The run cannot continue (for example `claude` is not logged in); every ticket would fail alike."""


class Runner(Protocol):
    name: str

    def run(self, ctx: RunContext) -> RunOutput: ...


STUB_RESULT = {
    "outcome": "escalated_to_human",
    "tier": "NONE",
    "actions": [],
    "route_to": None,
    "reason": "stub agent: always escalates",
}


class StubRunner:
    """No model. Answers every ticket with an escalation."""

    name = "stub"

    def run(self, ctx: RunContext) -> RunOutput:
        return RunOutput(
            final_text=json.dumps(STUB_RESULT),
            result=dict(STUB_RESULT),
            session_ids=[f"stub-{ctx.ticket['ticket_id']}"],
        )


def run_process(cmd, *, cwd, env, input_text: str, timeout: int) -> subprocess.CompletedProcess:
    """Like subprocess.run, but a timeout kills the whole process tree.

    On Windows `claude` is a `.cmd` shim: killing only the shim leaves the real process holding
    the pipes, and `subprocess.run` would then wait for it. So the tree is killed.
    """
    popen = {"start_new_session": True} if sys.platform != "win32" else {}
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", **popen,
    )  # fmt: skip
    try:
        out, err = proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def parse_stream(stdout: str) -> dict:
    """Pull the facts the harness needs out of a `stream-json` run."""
    events = []
    for line in stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    sessions = sorted({e["session_id"] for e in events if e.get("session_id")})
    dispatched = [
        c["input"].get("subagent_type", "?")
        for e in events
        if e.get("type") == "assistant" and not e.get("parent_tool_use_id")
        for c in e["message"]["content"]
        if c["type"] == "tool_use" and c["name"] in ("Agent", "Task")
    ]
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    return {"events": events, "sessions": sessions, "dispatched": dispatched, "result": result}


class ClaudeRunner:
    name = "claude"

    def __init__(self, claude: str | None = None):
        self.claude = claude

    def run(self, ctx: RunContext) -> RunOutput:
        out = RunOutput()
        ctx.workdir.mkdir(parents=True, exist_ok=True)
        gateway = ctx.gateway
        tokens = gateway.issue_run_tokens()
        runtime = None
        t0 = time.time()
        try:
            runtime = launch.render_runtime(tokens, gateway_url=ctx.gateway_url)
            trace_path = ctx.workdir / "trace.jsonl"
            extra_env = {
                "IAM_STATE_DIR": str(ctx.workdir / "hookstate"),
                "IAM_TRACE_PATH": str(trace_path),
            }
            if ctx.use_config_dir:
                extra_env["CLAUDE_CONFIG_DIR"] = str(launch.CONFIG_DIR)
            cmd = launch.claude_command(
                ctx.agent, None, model=ctx.model, max_turns=ctx.max_turns, claude=self.claude
            )
            try:
                proc = run_process(
                    cmd,
                    cwd=runtime,
                    env=launch.scrubbed_env(extra=extra_env),
                    input_text=ctx.prompt,  # via stdin: the Windows cmd.exe shim cuts arguments at a newline
                    timeout=ctx.timeout,
                )
            except subprocess.TimeoutExpired:
                out.error = f"timeout after {ctx.timeout}s"
                return out
            stream = ctx.workdir / "stream.jsonl"
            stream.write_text(proc.stdout, encoding="utf-8")
            out.stream_path = str(stream)
            parsed = parse_stream(proc.stdout)
            out.session_ids = parsed["sessions"]
            out.dispatched = parsed["dispatched"]
            res = parsed["result"]
            if res is None:
                out.error = f"no result event (exit {proc.returncode}): {proc.stderr[:200]!r}"
            else:
                out.final_text = res.get("result")
                out.cost_usd = res.get("total_cost_usd")
                out.turns = res.get("num_turns")
                if res.get("is_error"):
                    text = str(res.get("result"))
                    kind = "auth" if AUTH_FAILURE.search(text) else "run failed"
                    out.error = f"{kind}: {text[:200]!r}"
            if len(out.session_ids) != 1 and not out.error:
                # the hop counter assumes one ticket per session
                out.error = f"expected exactly one session, saw {len(out.session_ids)}"
            if trace_path.exists():
                out.trace = [
                    json.loads(x) for x in trace_path.read_text("utf-8").splitlines() if x.strip()
                ]
            if out.final_text is not None and check(out.final_text) is None:
                out.result = final_json(out.final_text)
            out.schema_violation = any(t.get("event") == "schema_violation" for t in out.trace) or (
                out.result is None and not out.error
            )
            return out
        finally:
            out.duration_s = round(time.time() - t0, 1)
            if runtime is not None:
                launch.cleanup(runtime)
            gateway.revoke_run_tokens()
