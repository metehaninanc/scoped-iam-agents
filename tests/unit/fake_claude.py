"""A stand-in for the `claude` CLI, driven by a scenarios file, for harness tests.

It reads the prompt from stdin, finds the ticket id in it, and plays the scenario for that
ticket: it can call gateway tools over HTTP with the token from the rendered agent file (the
same file Claude Code would read), print a stream-json run, write trace events, hang, or
report a second session. It records what it saw in `fake_record.json` next to the trace file.
"""

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

args = sys.argv[1:]
prompt = sys.stdin.read()
cwd = Path.cwd()
record_dir = Path(os.environ["IAM_TRACE_PATH"]).parent
ticket_id = re.search(r"ticket_id: (IAM-\d+)", prompt).group(1)
scenarios = json.loads(Path(__file__).with_name("scenarios.json").read_text(encoding="utf-8"))
scenario = scenarios.get(ticket_id, {})
agent = args[args.index("--agent") + 1]
session = f"fake-session-{ticket_id}"

record_dir.mkdir(parents=True, exist_ok=True)
(record_dir / "fake_record.json").write_text(
    json.dumps(
        {
            "args": args,
            "cwd": str(cwd),
            "prompt": prompt,
            "env_names": sorted(os.environ),
            "agent_file_has_placeholder": "${GW_TOKEN"
            in (cwd / ".claude" / "agents" / f"{agent}.md").read_text(encoding="utf-8"),
        }
    ),
    encoding="utf-8",
)


def emit(obj):
    print(json.dumps(obj), flush=True)


emit({"type": "system", "subtype": "init", "session_id": session, "tools": []})
if scenario.get("hang"):
    time.sleep(60)
if scenario.get("no_output"):
    sys.exit(3)

for subagent in scenario.get("dispatch", []):
    emit(
        {
            "type": "assistant",
            "session_id": session,
            "parent_tool_use_id": None,
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Agent", "input": {"subagent_type": subagent}}
                ]
            },
        }
    )

if scenario.get("calls"):
    text = (cwd / ".claude" / "agents" / f"{agent}.md").read_text(encoding="utf-8")
    token = re.search(r"Bearer (\S+?)\"", text).group(1)
    url = re.search(r"url: (\S+)", text).group(1)
    for tool, tool_args in scenario["calls"]:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": tool_args},
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        urllib.request.urlopen(request, timeout=30).read()

if scenario.get("schema_violation"):
    trace = Path(os.environ["IAM_TRACE_PATH"])
    with trace.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": "2026-10-11T10:00:00+00:00",
                    "event": "schema_violation",
                    "agent_type": agent,
                }
            )
            + "\n"
        )

final = scenario.get("final")
text = "done" if final is None else "Done.\n```json\n" + json.dumps(final) + "\n```"
emit(
    {
        "type": "assistant",
        "session_id": session,
        "message": {"content": [{"type": "text", "text": text}]},
    }
)
if scenario.get("second_session"):
    emit(
        {
            "type": "assistant",
            "session_id": session + "-b",
            "message": {"content": [{"type": "text", "text": "x"}]},
        }
    )
emit(
    {
        "type": "result",
        "subtype": "success",
        "is_error": bool(scenario.get("is_error")),
        "session_id": session,
        "result": text,
        "total_cost_usd": 0.01,
        "num_turns": 3,
    }
)
