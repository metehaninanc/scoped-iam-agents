"""Task 0.3 offline: seed and reset logic against the in-memory admin Graph."""

import copy
import itertools
import json

import pytest

from iamagents import seedconfig as sc
from iamagents.gateway.graph.base import Secret
from iamagents.gateway.secrets_registry import SECRETS
from seed import engine
from seed.engine import (
    Change,
    Context,
    LicenseCapError,
    PreflightError,
    ProtectedObjectError,
    SkuChoiceError,
)
from seed.fake_admin import FakeAdminGraph

DOMAIN = "t.example"
ADMIN, BREAKGLASS = f"root@{DOMAIN}", f"bg@{DOMAIN}"
TENANT = sc.load_tenant()


def upn(prefix):
    return f"{prefix}@{DOMAIN}"


def make(**kw):
    admin = FakeAdminGraph(DOMAIN, ADMIN, BREAKGLASS, **kw)
    accounts = sc.resolve_accounts(
        TENANT, {"TENANT_ADMIN_UPN": ADMIN, "BREAKGLASS_UPN": BREAKGLASS}
    )
    ctx = Context(TENANT, DOMAIN, accounts, sku_part="AAD_PREMIUM_P2")
    return admin, ctx


def seed(admin, ctx, **kw):
    kw = {"apply": True, "prune": False, "wait": True, "sleep": lambda s: None} | kw
    return engine.run(admin, ctx, engine.desired_state(ctx), **kw)


@pytest.fixture
def seeded():
    admin, ctx = make()
    result = seed(admin, ctx)
    return admin, ctx, result


# --- seeding from nothing


def test_plan_changes_nothing_and_counts_the_work():
    admin, ctx = make()
    result = seed(admin, ctx, apply=False)
    assert admin.writes == [] and result.applied == []
    ops = [c.op for c in result.planned]
    assert ops.count("create_user") == 22 and ops.count("create_group") == 21
    assert ops.count("assign_license") == 22 and ops.count("assign_role") == 3
    assert result.seats == (1, 23)  # the admin holds a seat; 22 employees follow


def test_apply_builds_the_whole_org(seeded):
    admin, ctx, result = seeded
    assert result.remaining == [] and result.dynamic_ready
    assert len([u for u in admin.users.values() if u.employee_type]) == 22
    assert admin.group_roles("PRIV-User-Admin") == ["User Administrator"]
    assert admin.group_roles("PRIV-Helpdesk-Admin") == ["Helpdesk Administrator"]
    assert admin.group_roles("PRIV-Security-Reader") == ["Security Reader"]
    assert admin.group_owners("SG-App-CRM") == [upn("felix.wagner")]
    assert sorted(admin.group_members("SG-App-CRM")) == sorted(
        upn(p) for p in ["felix.wagner", "zoe.martin", "arjun.mehta", "elena.costa"]
    )
    assert len(admin.group_members("DG-Dept-IT")) == 4
    assert admin.get_user(upn("zoe.martin")).manager_upn == upn("felix.wagner")
    assert admin.get_user(upn("priya.nair")).employee_type == "Privileged"
    assert admin.list_skus()[0].consumed == 23  # 22 employees and the admin


def test_what_was_built_equals_what_was_desired(seeded):
    admin, ctx, _ = seeded
    observed = engine.observe(admin, ctx).state
    desired = engine.desired_state(ctx)
    assert engine.diff(desired, observed, ctx) == []


def test_seeding_twice_changes_nothing(seeded):
    admin, ctx, _ = seeded
    writes = len(admin.writes)
    again = seed(admin, ctx)
    assert again.planned == [] and again.applied == [] and len(admin.writes) == writes


def test_the_dataset_ticket_world_is_what_got_seeded(seeded):
    """The seeded org matches the world the tickets were labelled against."""
    admin, ctx, _ = seeded
    ref = sc.load_reference()
    for name, (_, owner, members) in ref.GROUPS.items():
        assert sorted(admin.group_members(name)) == sorted(upn(m) for m in members), name
        assert admin.group_owners(name) == ([upn(owner)] if owner else []), name


# --- secrets


def test_initial_passwords_never_appear_anywhere(monkeypatch):
    made = []

    def fake_password():
        s = Secret(f"Pw-{len(made):02d}-Zk9!xQ3mR7tVb2")
        made.append(s.reveal())
        return s

    monkeypatch.setattr(engine, "generate_temp_password", fake_password)
    admin, ctx = make()
    lines = []
    result = seed(admin, ctx, log=lines.append)
    assert len(made) == 22 == admin.passwords_supplied
    everything = (
        json.dumps(admin.writes, default=str)
        + "\n".join(lines)
        + repr(result)
        + repr(result.planned)
    )
    assert not [p for p in made if p in everything]
    assert all(SECRETS.contains(p) for p in made)  # registered for redaction


def test_the_password_travels_as_a_secret_object():
    s = engine.generate_temp_password()
    assert isinstance(s, Secret) and s.reveal() not in repr(s) and len(s.reveal()) >= 16


# --- ordering and the seat cap


def test_the_license_waits_for_the_usage_location_and_the_manager_for_the_user(seeded):
    admin, _, _ = seeded
    first_license = next(i for i, w in enumerate(admin.writes) if w[0] == "assign_license")
    last_create = max(i for i, w in enumerate(admin.writes) if w[0] == "create_user")
    assert last_create < first_license  # (the fake refuses a license without a usage location)
    assert all(i > last_create for i, w in enumerate(admin.writes) if w[0] == "set_manager")


def test_license_cap_boundary_is_exactly_25():
    admin, ctx = make()
    for i in range(2):  # two unmanaged licensed users: 1 admin + 2 + 22 = 25
        admin.users[f"x{i}"] = type(admin.users[ADMIN.lower()])(
            id=f"x{i}", upn=upn(f"x{i}"), usage_location="DE", license_sku_ids=[admin.sku.id]
        )
    ok = seed(admin, ctx, apply=False)
    assert ok.seats == (3, 25)
    admin.users["x2"] = type(admin.users[ADMIN.lower()])(
        id="x2", upn=upn("x2"), usage_location="DE", license_sku_ids=[admin.sku.id]
    )
    with pytest.raises(LicenseCapError, match="26 seats"):
        seed(admin, ctx, apply=False)


def test_the_cap_stops_before_the_first_write():
    admin, ctx = make()
    for i in range(5):
        admin.users[f"x{i}"] = type(admin.users[ADMIN.lower()])(
            id=f"x{i}", upn=upn(f"x{i}"), usage_location="DE", license_sku_ids=[admin.sku.id]
        )
    with pytest.raises(LicenseCapError):
        seed(admin, ctx)
    assert admin.writes == []


def test_a_third_joiner_would_pass_the_cap(seeded):
    admin, ctx, _ = seeded
    target = engine.desired_state(ctx)
    for name in ["noah.krause", "mira.jansen", "third.joiner"]:
        target["users"][name] = dict(
            target["users"]["zoe.martin"], displayName=name, manager="felix.wagner"
        )
    with pytest.raises(LicenseCapError):
        engine.run(admin, ctx, target, apply=False, prune=False)
    # two joiners fill the cap exactly
    del target["users"]["third.joiner"]
    assert engine.run(admin, ctx, target, apply=False, prune=False).seats == (23, 25)


# --- preflight and license choice


def test_wrong_tenant_domain_is_refused():
    admin, ctx = make()
    ctx.domain = "other.example"
    with pytest.raises(PreflightError, match="wrong tenant"):
        seed(admin, ctx, apply=False)


def test_missing_admin_account_is_refused():
    admin, ctx = make()
    del admin.users[BREAKGLASS.lower()]
    with pytest.raises(PreflightError, match="breakglass"):
        seed(admin, ctx, apply=False)


def test_license_resolution():
    admin, _ = make()
    assert engine.resolve_sku(admin, None).part_number == "AAD_PREMIUM_P2"
    assert engine.resolve_sku(admin, "AAD_PREMIUM_P2").id == admin.sku.id
    with pytest.raises(SkuChoiceError, match="not in the tenant"):
        engine.resolve_sku(admin, "OTHER")
    admin.list_skus = lambda: [
        engine.Sku("1", "AAD_PREMIUM_P1"),
        engine.Sku("2", "AAD_PREMIUM_P2", 100, 5),
    ]
    with pytest.raises(SkuChoiceError, match="LICENSE_SKU"):
        engine.resolve_sku(admin, None)


def test_an_unresolved_license_is_refused():
    admin, ctx = make()
    ctx.sku_part = None
    with pytest.raises(PreflightError, match="license"):
        engine.run(admin, ctx, engine.desired_state(ctx), apply=False, prune=False)


# --- dynamic groups


def test_waiting_for_dynamic_membership_polls_until_it_is_ready():
    admin, ctx = make(dynamic_lag=3)
    sleeps = []
    result = seed(admin, ctx, sleep=sleeps.append)
    assert result.dynamic_ready and len(sleeps) >= 1 and result.remaining == []


def test_a_dynamic_group_that_never_fills_is_reported_not_hung():
    admin, ctx = make(dynamic_lag=10_000)
    ticks = itertools.count(0, 100)
    result = seed(admin, ctx, sleep=lambda s: None, clock=lambda: next(ticks), dynamic_timeout=250)
    assert result.dynamic_ready is False


# --- the admin accounts


def test_admin_accounts_are_never_written_to(seeded):
    admin, ctx, _ = seeded
    touched = json.dumps(admin.writes, default=str)
    assert ADMIN not in touched and BREAKGLASS not in touched
    assert (
        admin.get_user(ADMIN).department == "" and admin.get_user(BREAKGLASS).license_sku_ids == []
    )


def test_admin_accounts_are_invisible_even_as_owners_and_members(seeded):
    admin, ctx, _ = seeded
    admin.groups["SG-App-CRM"]["owners"].add(ADMIN.lower())
    admin.groups["SG-App-CRM"]["members"].add(BREAKGLASS.lower())
    assert engine.diff(engine.desired_state(ctx), engine.observe(admin, ctx).state, ctx) == []


@pytest.mark.parametrize("who", ["admin", "breakglass"])
@pytest.mark.parametrize(
    "op", ["update_user", "delete_user", "set_manager", "assign_license", "add_member"]
)
def test_a_change_that_names_an_admin_account_is_refused(seeded, who, op):
    admin, ctx, _ = seeded
    prefix = ctx.prefix(ctx.accounts[who])
    detail = {"member": prefix, "manager": prefix, "attrs": {}, "license": "AAD_PREMIUM_P2"}
    with pytest.raises(ProtectedObjectError, match="admin account"):
        engine.guard(
            Change(op, "SG-App-CRM" if op == "add_member" else prefix, detail),
            ctx,
            engine.protected_set(ctx, admin),
        )


# --- reset: drift is repaired toward the baseline


@pytest.fixture
def baseline(seeded):
    admin, ctx, _ = seeded
    return engine.verified_state(admin, ctx)


def reset(admin, ctx, baseline, **kw):
    kw = {"apply": True, "prune": False, "wait": True, "sleep": lambda s: None} | kw
    return engine.run(admin, ctx, baseline, **kw)


def test_reset_on_an_untouched_tenant_has_an_empty_diff(seeded, baseline):
    admin, ctx, _ = seeded
    writes = len(admin.writes)
    result = reset(admin, ctx, baseline)
    assert result.planned == [] and len(admin.writes) == writes


def drift(admin):
    admin.delete_user(upn("zoe.martin"))
    admin.update_user(upn("mei.tanaka"), {"department": "Sales", "jobTitle": "Hacker"})
    admin.update_user(upn("ben.altmann"), {"accountEnabled": False})
    admin.remove_license(upn("katja.moser"), admin.sku.id)
    admin.remove_member("SG-App-Jira", upn("amira.haddad"))
    admin.add_member("SG-App-ERP-Create", upn("clara.schmidt"))  # breaks segregation of duties
    admin.add_member("SG-Share-HR-Confidential", upn("zoe.martin")) if False else None
    admin.add_owner("SG-App-CRM", upn("marcus.weber"))
    admin.remove_owner("SG-App-Jira", upn("jonas.keller"))
    admin.set_manager(upn("leo.fischer"), upn("lena.hoffmann"))
    admin.remove_role("PRIV-Helpdesk-Admin", "Helpdesk Administrator")
    admin.update_group_rule("DG-Dept-HR", 'user.department -eq "Sales"')
    admin.delete_group("SG-Share-Eng-Repos")


def test_every_kind_of_drift_is_repaired(seeded, baseline):
    admin, ctx, _ = seeded
    drift(admin)
    plan = reset(admin, ctx, baseline, apply=False)
    ops = {c.op for c in plan.planned}
    assert {"create_user", "update_user", "assign_license", "add_member", "remove_member", "add_owner", "remove_owner",
            "set_manager", "assign_role", "update_group_rule", "create_group"} <= ops  # fmt: skip
    assert not plan.applied
    result = reset(admin, ctx, baseline)
    assert result.remaining == [] and result.dynamic_ready
    assert engine.diff(baseline, engine.verified_state(admin, ctx), ctx) == []
    assert admin.get_user(upn("zoe.martin")) is not None
    assert upn("clara.schmidt") not in admin.group_members("SG-App-ERP-Create")
    assert sorted(admin.group_members("SG-Share-Eng-Repos")) == sorted(
        upn(m) for m in sc.load_reference().GROUPS["SG-Share-Eng-Repos"][2]
    )


def test_a_recreated_user_gets_manager_license_and_memberships_back(seeded, baseline):
    admin, ctx, _ = seeded
    admin.delete_user(upn("zoe.martin"))
    reset(admin, ctx, baseline)
    zoe = admin.get_user(upn("zoe.martin"))
    assert zoe.manager_upn == upn("felix.wagner") and admin.sku.id in zoe.license_sku_ids
    assert upn("zoe.martin") in admin.group_members("SG-App-CRM") and upn(
        "zoe.martin"
    ) in admin.group_members("DG-Dept-Sales")


def test_unknown_objects_are_reported_and_removed_only_with_prune(seeded, baseline):
    admin, ctx, _ = seeded
    admin.create_user(
        upn("noah.krause"),
        {
            "displayName": "Noah",
            "employeeType": "Employee",
            "userType": "Member",
            "usageLocation": "DE",
        },
        Secret("x-secret-value-1"),
    )
    admin.create_group("SG-Rogue", "assigned", None, [upn("marcus.weber")])
    plain = reset(admin, ctx, baseline)
    assert {c.op for c in plain.skipped} == {"delete_user", "delete_group"}
    assert admin.get_user(upn("noah.krause")) is not None and "SG-Rogue" in admin.groups
    pruned = reset(admin, ctx, baseline, prune=True)
    assert admin.get_user(upn("noah.krause")) is None and "SG-Rogue" not in admin.groups
    assert pruned.remaining == []


def test_users_outside_the_managed_scope_are_ignored(seeded, baseline):
    admin, ctx, _ = seeded
    admin.users["guest"] = type(admin.users[ADMIN.lower()])(
        id="g1", upn="guest_x.com#EXT#@t.example", usage_location="DE"
    )
    admin.create_group("Marketing", "assigned", None, [])  # not a seed group name
    assert reset(admin, ctx, baseline, prune=True).planned == []


# --- prune never touches the RMAU members or the admin accounts, under any flag


FLAGS = list(itertools.product([False, True], repeat=3))  # apply, prune, wait


@pytest.mark.parametrize("apply,prune,wait", FLAGS)
def test_prune_never_deletes_a_privileged_user_or_a_priv_group(
    seeded, baseline, apply, prune, wait
):
    """Priya, Tom, Sofia and the PRIV groups are RMAU members. Even when the baseline does not
    know them (so they look like unknown objects), nothing deletes them."""
    admin, ctx, _ = seeded
    thin = copy.deepcopy(baseline)
    for p in ("priya.nair", "tom.becker", "sofia.rossi"):
        del thin["users"][p]
    for g in ("PRIV-User-Admin", "PRIV-Helpdesk-Admin", "PRIV-Security-Reader"):
        del thin["groups"][g]
    writes = len(admin.writes)
    if apply and prune:
        with pytest.raises(ProtectedObjectError):
            reset(admin, ctx, thin, apply=apply, prune=prune, wait=wait)
        assert len(admin.writes) == writes  # refused before the first write
    else:
        reset(admin, ctx, thin, apply=apply, prune=prune, wait=wait)
        assert len(admin.writes) == writes  # a plan or no prune writes nothing
    for p in ("priya.nair", "tom.becker", "sofia.rossi"):
        assert admin.get_user(upn(p)) is not None
    assert {"PRIV-User-Admin", "PRIV-Helpdesk-Admin", "PRIV-Security-Reader"} <= set(admin.groups)


@pytest.mark.parametrize("apply,prune,wait", FLAGS)
def test_prune_never_deletes_what_the_live_rmau_holds(seeded, baseline, apply, prune, wait):
    """Members of the live RMAU are protected even if the config never heard of them."""
    admin, ctx, _ = seeded
    admin.create_user(
        upn("extra.admin"),
        {
            "displayName": "Extra",
            "employeeType": "Employee",
            "userType": "Member",
            "usageLocation": "DE",
        },
        Secret("x-secret-value-2"),
    )
    admin.create_group("SG-Extra", "assigned", None, [upn("marcus.weber")])
    admin.set_rmau([upn("extra.admin"), "SG-Extra", ADMIN, BREAKGLASS])
    writes = len(admin.writes)
    if apply and prune:
        with pytest.raises(ProtectedObjectError):
            reset(admin, ctx, baseline, apply=apply, prune=prune, wait=wait)
        assert len(admin.writes) == writes
    else:
        reset(admin, ctx, baseline, apply=apply, prune=prune, wait=wait)
    assert admin.get_user(upn("extra.admin")) is not None and "SG-Extra" in admin.groups


@pytest.mark.parametrize("apply,prune,wait", FLAGS)
def test_prune_never_touches_the_two_admin_accounts(seeded, baseline, apply, prune, wait):
    admin, ctx, _ = seeded
    before = {u: copy.deepcopy(admin.users[u]) for u in (ADMIN.lower(), BREAKGLASS.lower())}
    drift(admin)
    reset(admin, ctx, baseline, apply=apply, prune=prune, wait=wait)
    for key, rec in before.items():
        assert admin.users[key] == rec
    assert ADMIN not in json.dumps(admin.writes, default=str) and BREAKGLASS not in json.dumps(
        admin.writes, default=str
    )


def test_rmau_members_can_still_be_repaired_just_never_deleted(seeded, baseline):
    admin, ctx, _ = seeded
    admin.update_user(upn("priya.nair"), {"jobTitle": "Intruder"})
    admin.remove_member("PRIV-User-Admin", upn("priya.nair"))
    reset(admin, ctx, baseline, prune=True)
    assert admin.get_user(upn("priya.nair")).job_title == "IAM Administrator"
    assert upn("priya.nair") in admin.group_members("PRIV-User-Admin")


def test_a_group_of_the_wrong_kind_is_reported_as_unrepairable(seeded, baseline):
    admin, ctx, _ = seeded
    admin.delete_group("SG-App-CRM")
    admin.create_group("SG-App-CRM", "role_assignable", None, [upn("felix.wagner")])
    result = reset(admin, ctx, baseline)
    assert [c.op for c in result.remaining] == ["unrepairable"] and "recreate" in result.remaining[
        0
    ].detail["why"]


# --- the baseline file


def test_the_baseline_is_deterministic_and_holds_no_domain_or_address(seeded):
    admin, ctx, _ = seeded
    a = engine.render_baseline(engine.verified_state(admin, ctx), "abc123")
    b = engine.render_baseline(engine.verified_state(admin, ctx), "abc123")
    assert a == b and a.endswith("\n")
    assert "@" not in a and DOMAIN not in a and "example" not in a
    assert '"password' not in a.lower() and "secret" not in a.lower()
    data = json.loads(a)
    assert data["version"] == 1 and data["config_sha256"] == "abc123"
    assert data["groups"]["DG-Dept-IT"]["members"] == [
        "marcus.weber",
        "priya.nair",
        "sofia.rossi",
        "tom.becker",
    ]
    assert (
        data["users"]["priya.nair"]["employeeType"] == "Privileged"
        and data["users"]["zoe.martin"]["license"] == "AAD_PREMIUM_P2"
    )


def test_baseline_roundtrip_and_version_check(seeded):
    admin, ctx, _ = seeded
    state = engine.verified_state(admin, ctx)
    assert engine.parse_baseline(engine.render_baseline(state, "x")) == state
    with pytest.raises(engine.SeedError, match="version"):
        engine.parse_baseline('{"version": 99, "users": {}, "groups": {}}')


def test_baseline_diff_ignores_the_dynamic_member_lists(seeded, baseline):
    admin, ctx, _ = seeded
    # a dynamic group missing from the tenant is recreated without trying to add members
    admin.delete_group("DG-Dept-HR")
    result = reset(admin, ctx, baseline)
    assert [c.op for c in result.planned] == ["create_group"] and result.remaining == []
    assert len(admin.group_members("DG-Dept-HR")) == 3


def test_config_hash_ignores_line_endings(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"x\ny\n")
    b.write_bytes(b"x\r\ny\r\n")
    assert engine.config_hash(a) == engine.config_hash(b)


def test_an_admin_account_with_an_employee_type_is_still_invisible(seeded, baseline):
    """If someone sets employeeType on an admin account by hand, the diff must still not see it:
    otherwise reset would treat it as an unknown managed user and plan to delete it."""
    admin, ctx, _ = seeded
    admin.users[ADMIN.lower()].employee_type = "Employee"
    admin.users[BREAKGLASS.lower()].employee_type = "Privileged"
    observed = engine.observe(admin, ctx).state
    assert "root" not in observed["users"] and "bg" not in observed["users"]
    assert engine.diff(baseline, observed, ctx) == []
    reset(admin, ctx, baseline, prune=True)
    assert admin.get_user(ADMIN) is not None and admin.get_user(BREAKGLASS) is not None
