"""Eval harness: `uv run python -m eval.harness --mode plan --agent ds --split dev`.

Per ticket: apply the fixtures, give the Graph backend a fresh directory (the tenant is reset
after every ticket), bind the ticket in the gateway, run the system under test, read what the
gateway recorded, score, and unbind. One ticket per `claude -p` session, always: the hop counter
of the `hop_limit` hook is keyed on the session id, and the runner fails a ticket that used more
than one session. Tickets run one after another because the gateway has one active ticket.

Agents: ds, am, ig, pam (isolated mode, a slice of the dataset), team-lead (integrated mode) and
stub (no model, always escalates, integrated). `--runner stub` runs any agent's slice without a
model, which tests the pipeline.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from iamagents import launch, tenant
from iamagents.gateway.audit import AuditLog
from iamagents.gateway.config import ROOT, Settings
from iamagents.gateway.core import Gateway
from iamagents.gateway.graph.fake import FakeGraphFactory
from iamagents.gateway.policy import Policy
from iamagents.gateway.tools import TOOLS
from iamagents.services.fixtures import apply_fixtures
from iamagents.services.mock import MockServices

from . import report as reporting
from .prompt import build_prompt
from .runners import ClaudeRunner, RunAborted, RunContext, Runner, StubRunner
from .scoring import WRITE_TOOLS, Observations, agent_label, score_ticket

AGENTS = ("ds", "am", "ig", "pam", "team-lead", "stub")
DATASET = ROOT / "data" / "iam-tickets-v2.jsonl"
WORK_BASE = ROOT / "state" / "eval"


class HeldoutLocked(Exception):
    """The heldout set is run once. This run would be a second one, or is not marked final."""


@dataclass
class EvalConfig:
    agent: str
    runner: str = "claude"
    split: str = "dev"
    ids: list[str] | None = None
    limit: int | None = None
    fixtures: str | None = None  # isolated | integrated; default depends on the agent
    mode: str = "plan"
    model: str = "haiku"
    max_turns: int = 12
    timeout: int = 600
    label: str | None = None
    milestone: bool = False
    heldout_final: bool = False
    dataset: Path = DATASET
    tenant_domain: str | None = None  # mapped in for the run; None keeps the placeholder
    report_base: Path = reporting.REPORT_DIR
    work_base: Path = WORK_BASE
    use_config_dir: bool = False
    claude: str | None = None
    extra_runner: Runner | None = field(default=None, repr=False)  # tests inject their own

    def __post_init__(self):
        if self.agent not in AGENTS:
            raise ValueError(f"agent must be one of {AGENTS}")
        if self.agent == "stub":
            self.runner = "stub"
        self.fixtures = self.fixtures or (
            "integrated" if self.agent in ("team-lead", "stub") else "isolated"
        )


def load_dataset(path: Path, domain: str | None = None) -> list[dict]:
    rows = [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]
    return tenant.apply(rows, domain)


def select_tickets(tickets: list[dict], cfg: EvalConfig) -> tuple[list[dict], list[dict]]:
    """Return (selected, skipped). The slice of a domain agent is the tickets that have it as
    first or final agent, which reproduces the counts in docs/SPRINTS.md (DS 24, AM 21, IG 39, PAM 7)."""
    chosen = [t for t in tickets if cfg.split == "all" or t["split"] == cfg.split]
    if cfg.ids:
        chosen = [t for t in tickets if t["ticket_id"] in set(cfg.ids)]
    if cfg.agent in ("ds", "am", "ig", "pam"):
        label = agent_label(cfg.agent)
        chosen = [
            t for t in chosen if label in (t["expected_first_agent"], t["expected_final_agent"])
        ]
    selected, skipped = [], []
    for t in chosen:
        if t.get("routing_test") and cfg.runner != "stub":
            reason = "forced first hop (routing_test): needs the Sprint 5 team lead"
            skipped.append({"ticket_id": t["ticket_id"], "reason": reason})
        else:
            selected.append(t)
    if cfg.limit:
        selected = selected[: cfg.limit]
    return selected, skipped


def check_heldout_guard(cfg: EvalConfig, selected: list[dict]) -> Path | None:
    """A model run that touches the heldout set must be marked final, is a milestone, and happens once."""
    if cfg.runner == "stub" or not any(t["split"] == "heldout" for t in selected):
        return None
    lock = cfg.report_base / "milestones" / "heldout.lock"
    if not (cfg.heldout_final and cfg.milestone):
        raise HeldoutLocked(
            "this run includes heldout tickets: pass --heldout-final and --milestone, or use --split dev"
        )
    if lock.exists():
        raise HeldoutLocked(
            f"the heldout set was already run once ({lock.read_text(encoding='utf-8').strip()[:120]})"
        )
    return lock


def env_secrets() -> list[str]:
    """Every gateway secret we know of: the .env file and the process environment."""
    values = [v for k, v in os.environ.items() if k.startswith("GW_")]
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip().startswith("GW_") and value.strip():
                values.append(value.strip())
    return values


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def build_gateway(cfg: EvalConfig, work_dir: Path) -> Gateway:
    settings = Settings(mode=cfg.mode, tokens={}, audit_path=work_dir / "audit.jsonl")
    return Gateway(
        settings,
        Policy.load(settings.policy_path),
        MockServices(),
        FakeGraphFactory(domain=cfg.tenant_domain),
        AuditLog(settings.audit_path),
    )


def make_runner(cfg: EvalConfig) -> Runner:
    if cfg.extra_runner is not None:
        return cfg.extra_runner
    return StubRunner() if cfg.runner == "stub" else ClaudeRunner(cfg.claude)


def run_eval(cfg: EvalConfig, log=print) -> dict:
    tickets = load_dataset(cfg.dataset, cfg.tenant_domain)
    by_id = {t["ticket_id"]: t for t in tickets}
    selected, skipped = select_tickets(tickets, cfg)
    lock = check_heldout_guard(cfg, selected)
    if cfg.use_config_dir and make_runner(cfg).name == "claude":
        problem = launch.config_dir_problem()
        if problem:  # an untrusted config dir would run every agent without its gateway tools
            raise RunAborted(problem)
    label = cfg.label or f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{cfg.agent}-{cfg.split}-{cfg.runner}"
    work_dir = cfg.work_base / label
    work_dir.mkdir(parents=True, exist_ok=True)

    gw = build_gateway(cfg, work_dir)
    gw.services.tickets.load_rows(tickets)
    runner = make_runner(cfg)
    server = None
    if runner.name == "claude":
        from iamagents.gateway.server import serve_in_thread

        server = serve_in_thread(gw)
    implemented = {WRITE_TOOLS[t] for t in TOOLS if t in WRITE_TOOLS}
    head_start = gw.audit.head_hash
    scores, entries, runs = [], [], {}
    try:
        for i, ticket in enumerate(selected, start=1):
            tid = ticket["ticket_id"]
            apply_fixtures(gw.services, ticket, cfg.fixtures)
            gw.set_graph_factory(
                FakeGraphFactory(domain=cfg.tenant_domain)
            )  # reset after every ticket
            gw.bind_ticket(tid)
            start = len(gw.audit.read_all())
            ctx = RunContext(
                ticket=ticket, agent=cfg.agent, prompt=build_prompt(ticket), gateway=gw,
                gateway_url=server.url if server else None, workdir=work_dir / tid, model=cfg.model,
                max_turns=cfg.max_turns, timeout=cfg.timeout, use_config_dir=cfg.use_config_dir,
            )  # fmt: skip
            out = runner.run(ctx)
            if out.error and out.error.startswith("auth:"):
                gw.unbind_ticket()
                raise RunAborted(
                    f"{out.error}. Log in again (for the dedicated config dir: run claude with "
                    "CLAUDE_CONFIG_DIR=state/claude-config) and rerun; nothing was written."
                )
            records = [r for r in gw.audit.read_all()[start:] if r.get("agent") != "harness"]
            obs = Observations(
                audit=records, dispatched=out.dispatched, schema_violation=out.schema_violation,
                error=out.error, hop_exceeded=gw.hop_exceeded, session_ids=out.session_ids,
            )  # fmt: skip
            score = score_ticket(ticket, cfg.agent, cfg.fixtures, out.result, obs, implemented)
            gw.unbind_ticket()
            scores.append(score)
            runs[tid] = {
                "cost_usd": out.cost_usd,
                "duration_s": out.duration_s,
                "hop_exceeded": gw.hop_exceeded,
            }
            entries.append(
                {
                    "ticket_id": tid,
                    "class": ticket["class"],
                    "split": ticket["split"],
                    "expected": {
                        "first_agent": ticket["expected_first_agent"],
                        "final_agent": ticket["expected_final_agent"],
                        "outcome": ticket["expected_outcome"],
                        "tier": ticket["expected_tier"],
                    },
                    "reported": out.result,
                    "score": score.to_dict(),
                    "failed": reporting.failed_components(score),
                    "run": {
                        "cost_usd": out.cost_usd, "duration_s": out.duration_s, "turns": out.turns,
                        "session_ids": out.session_ids, "dispatched": out.dispatched,
                        "domain_agents_seen_by_gateway": list(gw.ticket_agents),
                    },
                }
            )  # fmt: skip
            log(
                f"[{i:3}/{len(selected)}] {tid} {'PASS' if score.passed else 'FAIL'} {reporting.failed_components(score)}"
            )
    finally:
        if server is not None:
            server.stop()

    meta = {
        "label": label, "agent": cfg.agent, "runner": runner.name, "model": cfg.model if runner.name == "claude" else "n/a",
        "fixture_mode": cfg.fixtures, "gateway_mode": cfg.mode, "graph_backend": "fake", "split": cfg.split,
        "dataset_sha256": _sha(cfg.dataset), "policy_sha256": _sha(gw.settings.policy_path),
        "git_commit": (_git("rev-parse", "--short", "HEAD") or "unknown") + (" (dirty)" if _git("status", "--porcelain") else ""),
        "claude_version": claude_version(cfg.claude) if runner.name == "claude" else "n/a",
        "audit_head_start": head_start, "audit_head_end": gw.audit.head_hash,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }  # fmt: skip
    report = {
        "meta": meta,
        "summary": reporting.summarize(scores, by_id, runs),
        "tickets": entries,
        "skipped": skipped,
    }
    secrets = env_secrets() + list(gw.run_token_values)
    report["paths"] = {
        k: str(v)
        for k, v in reporting.write_reports(
            report, cfg.milestone, secrets, cfg.report_base, domain=cfg.tenant_domain
        ).items()
    }
    if lock is not None:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(f"{label} {meta['created_at']} {meta['git_commit']}\n", encoding="utf-8")
    return report


def claude_version(claude: str | None = None) -> str:
    exe = claude or shutil.which("claude")
    try:
        return subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
    except (OSError, TypeError, subprocess.TimeoutExpired):
        return "unknown"


def parse_args(argv: list[str] | None = None) -> EvalConfig:
    p = argparse.ArgumentParser(prog="python -m eval.harness", description=__doc__.split("\n\n")[0])
    p.add_argument("--agent", required=True, choices=AGENTS)
    p.add_argument(
        "--mode",
        choices=("plan", "live"),
        default="plan",
        help="gateway mode (live uses the fake Graph for now)",
    )
    p.add_argument("--split", choices=("dev", "heldout", "all"), default="dev")
    p.add_argument("--fixtures", choices=("isolated", "integrated"))
    p.add_argument("--runner", choices=("claude", "stub"), default="claude")
    p.add_argument("--model", default="haiku")
    p.add_argument("--ids", nargs="+")
    p.add_argument("--limit", type=int)
    p.add_argument("--label")
    p.add_argument("--max-turns", type=int, default=12)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument(
        "--milestone",
        action="store_true",
        help="write the Markdown report to the committed milestones folder",
    )
    p.add_argument("--heldout-final", action="store_true", help="this is the single heldout run")
    p.add_argument(
        "--isolated-config",
        action="store_true",
        help="use state/claude-config as CLAUDE_CONFIG_DIR",
    )
    a = p.parse_args(argv)
    return EvalConfig(
        agent=a.agent, runner=a.runner, split=a.split, ids=a.ids, limit=a.limit, fixtures=a.fixtures, mode=a.mode,
        model=a.model, max_turns=a.max_turns, timeout=a.timeout, label=a.label, milestone=a.milestone,
        heldout_final=a.heldout_final, use_config_dir=a.isolated_config,
    )  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    try:
        cfg.tenant_domain = tenant.tenant_domain()  # from .env; None when not set
        report = run_eval(cfg)
    except (HeldoutLocked, reporting.SecretInReport, tenant.TenantDomainError, RunAborted) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    s = report["summary"]
    print(
        f"\n{report['meta']['label']}: {s['passed']}/{s['tickets_run']} passed, policy violations {s['policy_violations']}"
    )
    print(f"report: {report['paths']['markdown']}")
    return 1 if s["policy_violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
