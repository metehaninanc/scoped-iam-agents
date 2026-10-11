"""Summaries, JSON and Markdown reports, and the secret scan for committed reports.

Per-run JSON stays local (`eval/reports/local/`, gitignored). Markdown reports are committed
only for milestone runs (each sprint exit and the single heldout run, `eval/reports/milestones/`).
Every report is scanned for secrets before it is written.
"""

import json
import re
from collections import Counter
from pathlib import Path

from .scoring import TicketScore

REPORT_DIR = Path(__file__).resolve().parent / "reports"
COMPONENTS = ("outcome", "tier", "route_to", "actions_exact", "first_agent", "final_agent")
META_ROWS = (
    "agent", "runner", "model", "fixture_mode", "gateway_mode", "graph_backend", "split",
    "dataset_sha256", "policy_sha256", "git_commit", "claude_version", "audit_head_start",
    "audit_head_end", "created_at",
)  # fmt: skip


class SecretInReport(Exception):
    """A secret value was found in a report that was about to be written."""


def assert_clean(text: str, secrets: list[str]) -> None:
    """Raise if any secret value appears in `text`. Short values are ignored (false matches)."""
    for s in secrets:
        if s and len(s) >= 8 and s in text:
            raise SecretInReport(
                "a gateway secret value appears in the report; nothing was written"
            )


def _rate(ok: int, n: int) -> float | None:
    return round(ok / n, 4) if n else None


def summarize(scores: list[TicketScore], tickets_by_id: dict, runs: dict) -> dict:
    n = len(scores)
    comp = {}
    for c in COMPONENTS:
        vals = [s.scored.get(c) for s in scores if s.scored.get(c) is not None]
        comp[c] = {"scored": len(vals), "ok": sum(vals), "rate": _rate(sum(vals), len(vals))}
    egress: Counter = Counter()
    for s in scores:
        egress.update(s.blocked_egress)
    violations = [{"ticket_id": s.ticket_id, **v} for s in scores for v in s.violations]

    def group(key):
        out = {}
        for s in scores:
            g = out.setdefault(str(tickets_by_id[s.ticket_id].get(key)), {"n": 0, "passed": 0})
            g["n"] += 1
            g["passed"] += int(s.passed)
        return {k: {**v, "rate": _rate(v["passed"], v["n"])} for k, v in sorted(out.items())}

    precisions = [s.precision for s in scores if s.precision is not None]
    recalls = [s.recall for s in scores if s.recall is not None]
    return {
        "tickets_run": n,
        "passed": sum(s.passed for s in scores),
        "pass_rate": _rate(sum(s.passed for s in scores), n),
        "policy_violations": len(violations),
        "violations": violations,
        "unbacked_claims": sum(len(s.unbacked) for s in scores),
        "schema_violations": sum(s.schema_violation for s in scores),
        "errors": sum(bool(s.error) for s in scores),
        "hop_exceeded": sum(bool(runs.get(s.ticket_id, {}).get("hop_exceeded")) for s in scores),
        "components": comp,
        "mean_precision": round(sum(precisions) / len(precisions), 4) if precisions else None,
        "mean_recall": round(sum(recalls) / len(recalls), 4) if recalls else None,
        "blocked_egress_by_tool": dict(sorted(egress.items())),
        "blocked_egress_total": sum(egress.values()),
        "by_class": group("class"),
        "by_split": group("split"),
        "cost_usd": round(sum(r.get("cost_usd") or 0 for r in runs.values()), 4),
        "duration_s": round(sum(r.get("duration_s") or 0 for r in runs.values()), 1),
    }


def _pct(x):
    return "n/a" if x is None else f"{100 * x:.1f}%"


def failed_components(s: TicketScore) -> list[str]:
    out = [c for c, v in s.scored.items() if v is False]
    if s.error:
        out.append("error")
    if s.schema_violation:
        out.append("schema_violation")
    if s.violations:
        out.append("POLICY VIOLATION")
    if s.unbacked:
        out.append("unbacked_claim")
    return out


def render_markdown(report: dict) -> str:
    m, s = report["meta"], report["summary"]
    lines = [f"# Eval report: {m['label']}", "", "| Item | Value |", "|---|---|"]
    lines += [f"| {k} | {m.get(k)} |" for k in META_ROWS]
    lines += ["", f"**Policy violations: {s['policy_violations']}**", ""]
    if s["violations"]:
        lines += [
            "| Ticket | Agent | Tool | Target | Rule | Decision |",
            "|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {v['ticket_id']} | {v['agent']} | {v['tool']} | {v['target']} | {v['rule_id']} | {v['decision']} |"
            for v in s["violations"]
        ]
        lines.append("")
    lines += ["## Summary", "", "| Metric | Value |", "|---|---|"]
    lines += [
        f"| tickets run | {s['tickets_run']} (skipped {len(report['skipped'])}) |",
        f"| passed | {s['passed']} ({_pct(s['pass_rate'])}) |",
        f"| unbacked claims | {s['unbacked_claims']} |",
        f"| schema violations | {s['schema_violations']} |",
        f"| run errors | {s['errors']} |",
        f"| hop limit exceeded | {s['hop_exceeded']} |",
        f"| blocked egress (all tools) | {s['blocked_egress_total']} |",
        f"| mean precision / recall of actions | {_pct(s['mean_precision'])} / {_pct(s['mean_recall'])} |",
        f"| cost (USD) / duration (s) | {s['cost_usd']} / {s['duration_s']} |",
        "",
        "## Accuracy by component",
        "",
        "| Component | Scored | Correct | Rate |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {c} | {v['scored']} | {v['ok']} | {_pct(v['rate'])} |"
        for c, v in s["components"].items()
    ]
    lines += ["", "## Blocked egress per tool", ""]
    if s["blocked_egress_by_tool"]:
        lines += ["| Tool | Blocked results |", "|---|---|"]
        lines += [f"| {t} | {n} |" for t, n in s["blocked_egress_by_tool"].items()]
    else:
        lines.append("None. A non-zero number is a bug in a tool or a false positive of the guard.")
    for key, title in (("by_class", "Pass rate by class"), ("by_split", "Pass rate by split")):
        lines += ["", f"## {title}", "", "| Group | Tickets | Passed | Rate |", "|---|---|---|---|"]
        lines += [
            f"| {g} | {v['n']} | {v['passed']} | {_pct(v['rate'])} |" for g, v in s[key].items()
        ]
    failed = [t for t in report["tickets"] if not t["score"]["passed"]]
    lines += ["", f"## Failures ({len(failed)})", ""]
    if failed:
        lines += [
            "| Ticket | Class | Expected outcome / tier | Reported | Failed components |",
            "|---|---|---|---|---|",
        ]
        for t in failed:
            rep = t.get("reported") or {}
            lines.append(
                f"| {t['ticket_id']} | {t['class']} | {t['expected']['outcome']} / {t['expected']['tier']} | "
                f"{rep.get('outcome')} / {rep.get('tier')} | {', '.join(t['failed']) or '-'} |"
            )
    else:
        lines.append("None.")
    if report["skipped"]:
        lines += ["", "## Skipped", "", "| Ticket | Reason |", "|---|---|"]
        lines += [f"| {k['ticket_id']} | {k['reason']} |" for k in report["skipped"]]
    lines.append("")
    return "\n".join(lines)


def write_reports(
    report: dict, milestone: bool, secrets: list[str], base: Path | None = None
) -> dict:
    """Write JSON (always local) and Markdown (committed folder for milestone runs only)."""
    base = base or REPORT_DIR
    label = report["meta"]["label"]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", label):
        raise ValueError("report label may only contain letters, digits, dot, dash, underscore")
    js = json.dumps(report, indent=2, ensure_ascii=False)
    assert_clean(js, secrets)
    md = render_markdown(report)
    assert_clean(md, secrets)
    (base / "local").mkdir(parents=True, exist_ok=True)
    paths = {"json": base / "local" / f"{label}.json"}
    paths["json"].write_text(js, encoding="utf-8")
    md_dir = base / ("milestones" if milestone else "local")
    md_dir.mkdir(parents=True, exist_ok=True)
    paths["markdown"] = md_dir / f"{label}.md"
    paths["markdown"].write_text(md, encoding="utf-8")
    return paths
