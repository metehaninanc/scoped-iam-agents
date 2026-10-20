"""Shared setup and output of `seed.py` and `reset.py`.

Both scripts plan by default and write only with `--apply`. Output uses user prefixes and group
names, never the tenant domain or an address, and nothing here can print a password. A local log
of applied changes goes to `state/seed-log.jsonl` (gitignored).
"""

import json
import os
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from iamagents import tenant as tenant_mod
from iamagents.gateway.config import ROOT, load_env_file
from iamagents.seedconfig import Tenant, assert_valid, load_tenant, resolve_accounts

from . import engine
from .admin_graph import AdminGraph

BASELINE_PATH = ROOT / "seed" / "baseline.json"
TENANT_PATH = ROOT / "seed" / "tenant.yaml"
LOG_PATH = ROOT / "state" / "seed-log.jsonl"
MAX_LINES = 80

AdminFactory = Callable[[str, bool, Callable[[str], None]], AdminGraph]


class Refused(Exception):
    """A setup problem to report in one line (missing variable, invalid config, wrong tenant)."""


@dataclass
class Setup:
    tenant: Tenant
    domain: str
    accounts: dict[str, str]
    admin: AdminGraph
    env: Mapping[str, str]


def setup(
    apply: bool,
    admin_factory: AdminFactory | None,
    out: Callable[[str], None],
    env: Mapping[str, str] | None = None,
) -> Setup:
    if env is None:  # the real run: .env, then the process environment
        load_env_file(ROOT / ".env")
        env = os.environ
    raw = (env.get("TENANT_DOMAIN") or "").strip()
    if not raw:
        raise Refused("TENANT_DOMAIN is not set: put the tenant domain in .env")
    try:
        domain = tenant_mod.validate(raw)
    except tenant_mod.TenantDomainError as e:
        raise Refused(str(e)) from e
    try:
        cfg = load_tenant(TENANT_PATH)
        assert_valid(cfg)
    except ValueError as e:
        raise Refused(str(e)) from e
    try:
        accounts = resolve_accounts(cfg, env)
    except KeyError as e:
        raise Refused(str(e.args[0])) from e
    if admin_factory is None:
        from .connect import connect as admin_factory
    return Setup(cfg, domain, accounts, admin_factory(domain, apply, out), env)


def log_applied(changes: list[engine.Change], path: Path | None = None) -> None:
    path = path or LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as f:
        for c in changes:
            f.write(json.dumps({"ts": now, "op": c.op, "key": c.key}) + "\n")


def summarize(result: engine.Result, apply: bool, cap: int, out: Callable[[str], None]) -> None:
    counts = Counter(c.op for c in result.planned)
    out(f"{'Applied' if apply else 'Plan'}: {len(result.planned)} change(s)")
    for op, n in sorted(counts.items()):
        out(f"  {op:<20} {n}")
    for c in result.planned[:MAX_LINES]:
        out("    " + c.line())
    if len(result.planned) > MAX_LINES:
        out(f"    ... {len(result.planned) - MAX_LINES} more")
    now, after = result.seats
    out(f"Licenses in use: {now} now, {after} after (cap {cap})")
    for c in result.skipped:
        out(f"  not applied without --prune: {c.line()}")
    for c in result.planned:
        if c.op == "unrepairable":
            out(f"  NEEDS A HUMAN: {c.line()}")
    if apply:
        out(f"Dynamic groups ready: {result.dynamic_ready}")
        out(f"Still different after applying: {len(result.remaining)}")
        for c in result.remaining:
            out("    " + c.line())


def write_baseline(admin: AdminGraph, ctx: engine.Context, path: Path | None = None) -> str:
    text = engine.render_baseline(
        engine.verified_state(admin, ctx), engine.config_hash(TENANT_PATH)
    )
    tenant_mod.assert_absent(text, ctx.domain)  # a baseline is committed: never the real domain
    (path or BASELINE_PATH).write_text(text, encoding="utf-8", newline="\n")
    return text
