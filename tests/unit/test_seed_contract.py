"""Contract: the engine behaves the same through the in-memory fake and through the real REST
client talking to the Graph emulator (small pages, throttling, delayed visibility)."""

import copy
import json

import httpx
import pytest
from fake_graph_server import GraphEmulator
from test_seed_engine import ADMIN, BREAKGLASS, DOMAIN, TENANT, drift, upn

from iamagents import seedconfig as sc
from iamagents.gateway.graph.base import Secret
from seed import engine
from seed.engine import Context, LicenseCapError, ProtectedObjectError
from seed.fake_admin import FakeAdminGraph
from seed.rest_admin import RestAdminGraph


@pytest.fixture(params=["fake", "rest"])
def backend(request):
    fake = FakeAdminGraph(DOMAIN, ADMIN, BREAKGLASS)
    if request.param == "fake":
        return fake, fake
    emu = GraphEmulator(fake, page_size=7, throttle_first=4, lag_404=1)
    rest = RestAdminGraph(
        lambda: "tok-" + "x" * 20, httpx.Client(transport=emu.transport), sleep=lambda s: None
    )
    return rest, fake


def ctx_for():
    accounts = sc.resolve_accounts(
        TENANT, {"TENANT_ADMIN_UPN": ADMIN, "BREAKGLASS_UPN": BREAKGLASS}
    )
    return Context(TENANT, DOMAIN, accounts, sku_part="AAD_PREMIUM_P2")


def run(admin, ctx, target, **kw):
    kw = {"apply": True, "prune": False, "wait": True, "sleep": lambda s: None} | kw
    return engine.run(admin, ctx, target, **kw)


def test_seed_from_nothing_then_again(backend):
    admin, fake = backend
    ctx = ctx_for()
    target = engine.desired_state(ctx)
    plan = run(admin, ctx, target, apply=False)
    assert fake.writes == [] and plan.seats == (1, 23)
    first = run(admin, ctx, target)
    assert first.remaining == [] and first.dynamic_ready
    assert len([u for u in fake.users.values() if u.employee_type]) == 22 and len(fake.groups) == 21
    writes = len(fake.writes)
    again = run(admin, ctx, target)
    assert again.planned == [] and len(fake.writes) == writes
    assert fake.passwords_supplied == 22


def test_the_seeded_world_is_the_world_the_tickets_were_labelled_against(backend):
    admin, fake = backend
    ctx = ctx_for()
    run(admin, ctx, engine.desired_state(ctx))
    for name, (_, owner, members) in sc.load_reference().GROUPS.items():
        assert sorted(fake.group_members(name)) == sorted(upn(m) for m in members), name
        assert fake.group_owners(name) == ([upn(owner)] if owner else []), name


def test_baseline_taken_through_either_backend_is_identical():
    texts = []
    for kind in ("fake", "rest"):
        fake = FakeAdminGraph(DOMAIN, ADMIN, BREAKGLASS)
        admin = (
            fake
            if kind == "fake"
            else RestAdminGraph(
                lambda: "tok-" + "x" * 20,
                httpx.Client(transport=GraphEmulator(fake, page_size=5).transport),
                sleep=lambda s: None,
            )
        )
        ctx = ctx_for()
        run(admin, ctx, engine.desired_state(ctx))
        texts.append(engine.render_baseline(engine.verified_state(admin, ctx), "h"))
    assert texts[0] == texts[1] and "@" not in texts[0]


def test_drift_is_repaired_toward_the_baseline(backend):
    admin, fake = backend
    ctx = ctx_for()
    run(admin, ctx, engine.desired_state(ctx))
    baseline = engine.verified_state(admin, ctx)
    drift(fake)
    plan = run(admin, ctx, baseline, apply=False)
    assert len(plan.planned) >= 10 and plan.applied == []
    result = run(admin, ctx, baseline)
    assert (
        result.remaining == []
        and engine.diff(baseline, engine.verified_state(admin, ctx), ctx) == []
    )


def test_prune_removes_unknown_objects_but_never_an_rmau_member(backend):
    admin, fake = backend
    ctx = ctx_for()
    run(admin, ctx, engine.desired_state(ctx))
    baseline = engine.verified_state(admin, ctx)
    fake.create_user(
        upn("noah.krause"),
        {
            "displayName": "Noah",
            "employeeType": "Employee",
            "userType": "Member",
            "usageLocation": "DE",
        },
        Secret("x-secret-value-3"),
    )
    fake.create_user(
        upn("extra.admin"),
        {
            "displayName": "Extra",
            "employeeType": "Employee",
            "userType": "Member",
            "usageLocation": "DE",
        },
        Secret("x-secret-value-4"),
    )
    fake.set_rmau([upn("extra.admin"), ADMIN, BREAKGLASS])
    writes = len(fake.writes)
    with pytest.raises(ProtectedObjectError):
        run(admin, ctx, baseline, prune=True)
    assert len(fake.writes) == writes  # refused before the first write
    fake.set_rmau([ADMIN, BREAKGLASS])
    thin = copy.deepcopy(baseline)
    run(admin, ctx, thin, prune=True)
    assert fake.get_user(upn("noah.krause")) is None and fake.get_user(upn("extra.admin")) is None


def test_the_seat_cap_holds_through_both(backend):
    admin, fake = backend
    ctx = ctx_for()
    for i in range(5):
        fake.users[f"x{i}"] = type(fake.users[ADMIN.lower()])(
            id=f"x{i}", upn=upn(f"x{i}"), usage_location="DE", license_sku_ids=[fake.sku.id]
        )
    with pytest.raises(LicenseCapError):
        run(admin, ctx, engine.desired_state(ctx))
    assert fake.writes == []


def test_the_admin_accounts_are_never_written_through_either(backend):
    admin, fake = backend
    ctx = ctx_for()
    run(admin, ctx, engine.desired_state(ctx))
    baseline = engine.verified_state(admin, ctx)  # taken before the drift
    drift(fake)
    run(admin, ctx, baseline, prune=False)
    assert ADMIN not in json.dumps(fake.writes, default=str)
    assert BREAKGLASS not in json.dumps(fake.writes, default=str)


def test_dynamic_groups_that_lag_are_waited_for(backend):
    admin, fake = backend
    fake.dynamic_lag = 3
    ctx = ctx_for()
    sleeps = []
    result = run(admin, ctx, engine.desired_state(ctx), sleep=sleeps.append)
    assert result.dynamic_ready and sleeps and result.remaining == []
