"""Seed the tenant from `seed/tenant.yaml`.

    uv run python -m seed.seed                 # plan: reads only, shows what would change
    uv run python -m seed.seed --apply         # write (asks for the write scopes at sign-in)

Run it under the delegated sign-in of the human admin (device code); no standing app exists.
It is idempotent: a second run plans nothing. After a clean `--apply` it writes
`seed/baseline.json`, the snapshot `reset.py` repairs toward. It never touches the two admin
accounts, stops at `license_cap`, and gives every new user a random initial password that is
never shown or stored. Live runs are asked for first (CLAUDE.md rule 5).
"""

import argparse
import sys

from . import cli, engine
from .admin_graph import AdminGraphError


def main(
    argv=None, *, admin_factory=None, out=print, baseline_path=None, log_path=None, env=None
) -> int:
    p = argparse.ArgumentParser(prog="python -m seed.seed", description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--apply", action="store_true", help="write to the tenant (the default is a plan)"
    )
    p.add_argument(
        "--sku",
        help="license part number (default: LICENSE_SKU in .env, or the single Entra ID P1/P2 license)",
    )
    p.add_argument(
        "--no-wait", action="store_true", help="do not wait for dynamic group membership"
    )
    p.add_argument(
        "--no-snapshot",
        action="store_true",
        help="do not write seed/baseline.json after a clean apply",
    )
    p.add_argument(
        "--dynamic-timeout", type=float, default=1200, help="seconds to wait for dynamic groups"
    )
    a = p.parse_args(argv)
    try:
        s = cli.setup(a.apply, admin_factory, out, env)
        sku = engine.resolve_sku(s.admin, a.sku or s.env.get("LICENSE_SKU") or None)
        ctx = engine.Context(s.tenant, s.domain, s.accounts, sku.part_number)
        result = engine.run(
            s.admin, ctx, engine.desired_state(ctx),
            apply=a.apply, prune=False, wait=not a.no_wait, dynamic_timeout=a.dynamic_timeout, log=lambda line: None,
        )  # fmt: skip
        if a.apply:
            cli.log_applied(result.applied, log_path)
    except (cli.Refused, engine.SeedError, AdminGraphError) as e:
        out(f"refused: {e}")
        return 2
    cli.summarize(result, a.apply, s.tenant.licenses.cap, out)
    clean = not result.remaining and result.dynamic_ready
    if a.apply and clean and not a.no_snapshot:
        cli.write_baseline(s.admin, ctx, baseline_path)
        out("Wrote seed/baseline.json")
    elif a.apply and not clean:
        out("Not clean yet: run again (it is idempotent); no baseline was written")
    return 0 if (clean or not a.apply) else 1


if __name__ == "__main__":
    sys.exit(main())
