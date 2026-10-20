"""Reset the tenant to the baseline.

    uv run python -m seed.reset                  # plan: what differs from seed/baseline.json
    uv run python -m seed.reset --apply          # repair
    uv run python -m seed.reset --apply --prune  # also delete users and groups outside the seed

Drift (a changed attribute, a missing user, a wrong member, a lost license) is repaired toward the
baseline. Objects the seed does not know are reported, and deleted only with `--prune`. Under no
flag does it delete a member of RMAU-Protected (the privileged users, the PRIV groups, whatever
the live RMAU holds) or touch either admin account. The managed scope is the users with an
`employeeType` (joiners created by IG-01 carry it) and the groups named DG-, PRIV-, SG- or GG-.
"""

import argparse
import sys

from . import cli, engine
from .admin_graph import AdminGraphError


def main(
    argv=None, *, admin_factory=None, out=print, baseline_path=None, log_path=None, env=None
) -> int:
    p = argparse.ArgumentParser(prog="python -m seed.reset", description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--apply", action="store_true", help="write to the tenant (the default is a plan)"
    )
    p.add_argument(
        "--prune",
        action="store_true",
        help="delete users and groups outside the seed (never RMAU members or admin accounts)",
    )
    p.add_argument(
        "--no-wait", action="store_true", help="do not wait for dynamic group membership"
    )
    p.add_argument("--dynamic-timeout", type=float, default=1200)
    a = p.parse_args(argv)
    path = baseline_path or cli.BASELINE_PATH
    try:
        if not path.exists():
            raise cli.Refused(
                "seed/baseline.json does not exist: run `python -m seed.seed --apply` first"
            )
        baseline = engine.parse_baseline(path.read_text(encoding="utf-8"))
        licenses = {u["license"] for u in baseline["users"].values() if u.get("license")}
        if len(licenses) != 1:
            raise cli.Refused(
                f"the baseline should name exactly one license, found {sorted(licenses)}"
            )
        s = cli.setup(a.apply, admin_factory, out, env)
        ctx = engine.Context(s.tenant, s.domain, s.accounts, licenses.pop())
        result = engine.run(
            s.admin, ctx, baseline,
            apply=a.apply, prune=a.prune, wait=not a.no_wait, dynamic_timeout=a.dynamic_timeout, log=lambda line: None,
        )  # fmt: skip
        if a.apply:
            cli.log_applied(result.applied, log_path)
    except (cli.Refused, engine.SeedError, AdminGraphError) as e:
        out(f"refused: {e}")
        return 2
    cli.summarize(result, a.apply, s.tenant.licenses.cap, out)
    if not result.planned:
        out("The tenant matches the baseline.")
    return 0 if not a.apply or (not result.remaining and result.dynamic_ready) else 1


if __name__ == "__main__":
    sys.exit(main())
