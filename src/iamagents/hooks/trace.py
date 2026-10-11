"""PostToolUse hook: tool call trace with `agent_type` for the eval.

One JSONL line per gateway tool call. From the result it keeps only `ok`, `decision`, `rule_id`,
`tier`, `reason` and `route_to`, never `data`, so the trace holds no directory content. Other
hooks append events to the same file through `append_event` (for example `schema_violation`).
The trace follows the same retention setting as the audit log (`purge`).
"""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .common import read_input, state_dir

KEEP = ("ok", "decision", "rule_id", "tier", "reason", "route_to")
MAX_VALUE = 200


def trace_path() -> Path:
    return Path(os.environ.get("IAM_TRACE_PATH") or state_dir() / "trace.jsonl")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def append_event(record: dict, path: Path | None = None) -> None:
    path = path or trace_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _now(), **record}, ensure_ascii=False) + "\n")


def _result_object(tool_response) -> dict:
    """Find the gateway's JSON result in whatever shape the client hands over."""
    candidates = []
    if isinstance(tool_response, dict):
        candidates.append(tool_response)
        content = tool_response.get("content")
        if isinstance(content, list):
            candidates += [c.get("text") for c in content if isinstance(c, dict)]
        candidates.append(tool_response.get("text"))
    elif isinstance(tool_response, list):
        candidates += [c.get("text") if isinstance(c, dict) else c for c in tool_response]
    else:
        candidates.append(tool_response)
    for c in candidates:
        if isinstance(c, str):
            try:
                c = json.loads(c)
            except json.JSONDecodeError:
                continue
        if isinstance(c, dict) and ("ok" in c or "decision" in c):
            return c
    return {}


def _clip(value):
    if isinstance(value, str) and len(value) > MAX_VALUE:
        return value[:MAX_VALUE] + "..."
    return value


def build_record(data: dict) -> dict:
    result = _result_object(data.get("tool_response"))
    return {
        "event": "tool_call",
        "session_id": data.get("session_id"),
        "agent_type": data.get("agent_type"),
        "agent_id": data.get("agent_id"),
        "tool": data.get("tool_name"),
        "args": {k: _clip(v) for k, v in (data.get("tool_input") or {}).items()},
        "result": {k: result[k] for k in KEEP if k in result},
    }


def purge(path: Path, retention_days: int, now: datetime) -> int:
    """Drop trace lines older than the retention period. Returns how many were removed."""
    if not path.exists():
        return 0
    cutoff = now - timedelta(days=retention_days)
    kept, removed = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and datetime.fromisoformat(json.loads(line)["ts"]) < cutoff:
            removed += 1
        elif line.strip():
            kept.append(line)
    path.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
    return removed


def main() -> int:
    append_event(build_record(read_input()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
