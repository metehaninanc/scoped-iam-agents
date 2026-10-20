"""Task 0.3, live: seed twice, then reset, and the diff is empty.

WRITES TO THE TENANT. Ask first (CLAUDE.md rule 5). It needs the P2 trial for the dynamic and
role assignable groups, `TENANT_DOMAIN`, `TENANT_ADMIN_UPN` and `BREAKGLASS_UPN` in `.env`, and one
device code sign-in as the admin account: the code is printed and has to be entered in a browser.

    IAM_LIVE_TENANT=yes uv run pytest tests/live/test_seed_live.py -m live -s
"""

import os

import pytest

from iamagents.gateway.config import ROOT, load_env_file
from iamagents.seedconfig import load_tenant, resolve_accounts
from seed import connect, engine

load_env_file(ROOT / ".env")

pytestmark = pytest.mark.skipif(
    os.environ.get("IAM_LIVE_TENANT") != "yes",
    reason="set IAM_LIVE_TENANT=yes (writes to the tenant)",
)


def test_seed_twice_then_reset_has_an_empty_diff():
    tenant = load_tenant()
    domain = os.environ["TENANT_DOMAIN"]
    admin = connect.connect(domain, apply=True)
    accounts = resolve_accounts(tenant, os.environ)
    sku = engine.resolve_sku(admin, os.environ.get("LICENSE_SKU") or None)
    ctx = engine.Context(tenant, domain, accounts, sku.part_number)
    admins_before = {k: admin.get_user(u) for k, u in accounts.items()}

    first = engine.run(admin, ctx, engine.desired_state(ctx), apply=True, prune=False)
    assert first.remaining == [] and first.dynamic_ready, [c.line() for c in first.remaining]

    second = engine.run(admin, ctx, engine.desired_state(ctx), apply=True, prune=False)
    assert second.planned == [], [c.line() for c in second.planned]  # idempotent

    baseline = engine.verified_state(admin, ctx)
    reset = engine.run(admin, ctx, baseline, apply=False, prune=False)
    assert reset.planned == [], [c.line() for c in reset.planned]  # reset: the diff is empty

    assert reset.seats[0] <= tenant.licenses.cap
    admins_after = {k: admin.get_user(u) for k, u in accounts.items()}
    assert admins_after == admins_before  # the two admin accounts were not touched
