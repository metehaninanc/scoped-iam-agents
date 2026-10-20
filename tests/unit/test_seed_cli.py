"""seed.py and reset.py as command-line tools, driven with the in-memory admin Graph."""

import json
import subprocess
import sys

import pytest
from test_seed_engine import ADMIN, BREAKGLASS, DOMAIN, drift, upn

from iamagents.gateway.config import ROOT
from iamagents.gateway.graph.base import Secret
from seed import cli, connect, engine
from seed import reset as reset_cli
from seed import seed as seed_cli
from seed.fake_admin import FakeAdminGraph

ENV = {"TENANT_DOMAIN": DOMAIN, "TENANT_ADMIN_UPN": ADMIN, "BREAKGLASS_UPN": BREAKGLASS}


class Run:
    def __init__(self, tmp_path, **fake_kw):
        self.fake = FakeAdminGraph(DOMAIN, ADMIN, BREAKGLASS, **fake_kw)
        self.lines: list[str] = []
        self.baseline = tmp_path / "baseline.json"
        self.log = tmp_path / "seed-log.jsonl"
        self.modes: list[bool] = []

    def factory(self, domain, apply, out):
        self.modes.append(apply)
        return self.fake

    def seed(self, *args, env=ENV):
        return seed_cli.main(
            list(args),
            admin_factory=self.factory,
            out=self.lines.append,
            baseline_path=self.baseline,
            log_path=self.log,
            env=env,
        )

    def reset(self, *args, env=ENV):
        return reset_cli.main(
            list(args),
            admin_factory=self.factory,
            out=self.lines.append,
            baseline_path=self.baseline,
            log_path=self.log,
            env=env,
        )

    @property
    def text(self):
        return "\n".join(self.lines)


@pytest.fixture
def run(tmp_path):
    return Run(tmp_path)


def assert_clean_output(r: Run):
    """Nothing printed, logged or written carries the domain, an address or a password."""
    everything = (
        r.text
        + (r.log.read_text("utf-8") if r.log.exists() else "")
        + (r.baseline.read_text("utf-8") if r.baseline.exists() else "")
    )
    assert DOMAIN not in everything and "@" not in everything
    assert not any("assword" in line and "random" not in line for line in r.lines)


# --- seed


def test_the_default_is_a_plan_that_writes_nothing(run):
    assert run.seed() == 0
    assert run.fake.writes == [] and not run.baseline.exists() and not run.log.exists()
    assert run.modes == [False]  # asked for the read scopes only
    assert (
        "Plan: " in run.text
        and "create_user" in run.text
        and "Licenses in use: 1 now, 23 after (cap 25)" in run.text
    )
    assert_clean_output(run)


def test_apply_builds_the_org_writes_the_baseline_and_a_log(run):
    assert run.seed("--apply") == 0
    assert run.modes == [True]
    assert len([u for u in run.fake.users.values() if u.employee_type]) == 22
    data = json.loads(run.baseline.read_text("utf-8"))
    assert data["version"] == 1 and len(data["users"]) == 22 and len(data["groups"]) == 21
    assert "Wrote seed/baseline.json" in run.text and "Dynamic groups ready: True" in run.text
    ops = [json.loads(x)["op"] for x in run.log.read_text("utf-8").splitlines()]
    assert ops.count("create_user") == 22 and ops.count("create_group") == 21
    assert_clean_output(run)


def test_the_baseline_carries_the_config_hash(run):
    run.seed("--apply")
    assert json.loads(run.baseline.read_text("utf-8"))["config_sha256"] == engine.config_hash(
        cli.TENANT_PATH
    )


def test_a_second_apply_changes_nothing_and_writes_the_same_baseline(run):
    run.seed("--apply")
    first = run.baseline.read_text("utf-8")
    writes = len(run.fake.writes)
    run.lines.clear()
    assert run.seed("--apply") == 0
    assert "Applied: 0 change(s)" in run.text and len(run.fake.writes) == writes
    assert run.baseline.read_text("utf-8") == first


def test_no_snapshot_flag(run):
    run.seed("--apply", "--no-snapshot")
    assert not run.baseline.exists()


def test_dynamic_groups_that_do_not_fill_give_exit_1_and_no_baseline(tmp_path):
    r = Run(tmp_path, dynamic_lag=10_000)
    assert r.seed("--apply", "--dynamic-timeout", "0") == 1
    assert (
        "Dynamic groups ready: False" in r.text
        and "Not clean yet" in r.text
        and not r.baseline.exists()
    )


def test_license_choice(run):
    run.fake.sku.part_number = "AAD_PREMIUM_P1"
    assert run.seed("--sku", "AAD_PREMIUM_P2") == 2 and "not in the tenant" in run.text
    run.lines.clear()
    assert run.seed("--sku", "AAD_PREMIUM_P1") == 0
    run.lines.clear()
    assert run.seed(env=ENV | {"LICENSE_SKU": "AAD_PREMIUM_P1"}) == 0  # from .env


@pytest.mark.parametrize(
    "env,needle",
    [
        ({k: v for k, v in ENV.items() if k != "TENANT_DOMAIN"}, "TENANT_DOMAIN is not set"),
        (ENV | {"TENANT_DOMAIN": "not a domain"}, "not a valid domain"),
        ({k: v for k, v in ENV.items() if k != "BREAKGLASS_UPN"}, "BREAKGLASS_UPN"),
        ({k: v for k, v in ENV.items() if k != "TENANT_ADMIN_UPN"}, "TENANT_ADMIN_UPN"),
    ],
)
def test_setup_problems_are_refused_with_the_name_of_the_missing_thing(run, env, needle):
    assert run.seed("--apply", env=env) == 2 and "refused" in run.text and needle in run.text
    assert run.fake.writes == [] and run.modes == []  # never even signed in


def test_wrong_tenant_and_missing_admin_account_are_refused(run):
    assert (
        run.seed("--apply", env=ENV | {"TENANT_DOMAIN": "other.example"}) == 2
        and "wrong tenant" in run.text
    )
    run.lines.clear()
    del run.fake.users[BREAKGLASS.lower()]
    assert run.seed("--apply") == 2 and "breakglass" in run.text
    assert run.fake.writes == []


def test_the_license_cap_is_a_refusal_before_any_write(run):
    for i in range(5):
        run.fake.users[f"x{i}"] = type(run.fake.users[ADMIN.lower()])(
            id=f"x{i}", upn=upn(f"x{i}"), usage_location="DE", license_sku_ids=[run.fake.sku.id]
        )
    assert run.seed("--apply") == 2 and "license cap is 25" in run.text and run.fake.writes == []


def test_an_invalid_config_is_refused(run, monkeypatch):
    from iamagents import seedconfig

    monkeypatch.setattr(
        cli,
        "assert_valid",
        lambda t: (_ for _ in ()).throw(ValueError("tenant.yaml is invalid: x")),
    )
    assert (
        run.seed("--apply") == 2 and "tenant.yaml is invalid" in run.text and run.fake.writes == []
    )
    assert seedconfig  # imported for the monkeypatch target's module


def test_a_graph_error_is_a_refusal_not_a_traceback(run, monkeypatch):
    from seed.admin_graph import AdminGraphError

    def boom(*a, **k):
        raise AdminGraphError("403 Authorization_RequestDenied: insufficient privileges")

    monkeypatch.setattr(run.fake, "create_user", boom)
    assert run.seed("--apply") == 2 and "insufficient privileges" in run.text


# --- reset


def test_reset_needs_a_baseline(run):
    assert run.reset() == 2 and "run `python -m seed.seed --apply` first" in run.text


def test_reset_on_a_clean_tenant(run):
    run.seed("--apply")
    run.lines.clear()
    assert (
        run.reset() == 0
        and "The tenant matches the baseline." in run.text
        and "Plan: 0 change(s)" in run.text
    )


def test_reset_plan_shows_drift_and_writes_nothing(run):
    run.seed("--apply")
    drift(run.fake)
    writes = len(run.fake.writes)
    run.lines.clear()
    assert run.reset() == 0 and len(run.fake.writes) == writes
    assert "create_user" in run.text and "update_user" in run.text and "add_member" in run.text
    assert_clean_output(run)


def test_reset_apply_repairs_and_the_second_run_is_clean(run):
    run.seed("--apply")
    drift(run.fake)
    run.lines.clear()
    assert run.reset("--apply") == 0 and "Still different after applying: 0" in run.text
    run.lines.clear()
    assert run.reset() == 0 and "The tenant matches the baseline." in run.text
    assert_clean_output(run)


def test_prune_is_needed_to_remove_unknown_objects(run):
    run.seed("--apply")
    run.fake.create_user(
        upn("noah.krause"),
        {
            "displayName": "Noah",
            "employeeType": "Employee",
            "userType": "Member",
            "usageLocation": "DE",
        },
        Secret("x-secret-value-5"),
    )
    run.lines.clear()
    assert run.reset("--apply") == 0 and "not applied without --prune" in run.text
    assert run.fake.get_user(upn("noah.krause")) is not None
    assert run.reset("--apply", "--prune") == 0
    assert run.fake.get_user(upn("noah.krause")) is None


def test_prune_is_refused_for_rmau_members_with_exit_2(run):
    run.seed("--apply")
    run.fake.create_user(
        upn("extra.admin"),
        {
            "displayName": "E",
            "employeeType": "Employee",
            "userType": "Member",
            "usageLocation": "DE",
        },
        Secret("x-secret-value-6"),
    )
    run.fake.set_rmau([upn("extra.admin"), ADMIN, BREAKGLASS])
    writes = len(run.fake.writes)
    run.lines.clear()
    assert run.reset("--apply", "--prune") == 2 and "never deleted" in run.text
    assert len(run.fake.writes) == writes and run.fake.get_user(upn("extra.admin")) is not None


def test_a_baseline_with_two_licenses_is_refused(run):
    run.seed("--apply")
    data = json.loads(run.baseline.read_text("utf-8"))
    data["users"]["zoe.martin"]["license"] = "OTHER_SKU"
    run.baseline.write_text(json.dumps(data), encoding="utf-8")
    run.lines.clear()
    assert run.reset() == 2 and "exactly one license" in run.text


def test_a_baseline_from_another_version_is_refused(run):
    run.baseline.write_text(json.dumps({"version": 9, "users": {}, "groups": {}}), encoding="utf-8")
    assert run.reset() == 2 and "version" in run.text


def test_reset_exit_code_is_1_when_something_stays_different(run):
    run.seed("--apply")
    run.fake.delete_group("SG-App-CRM")
    run.fake.create_group("SG-App-CRM", "role_assignable", None, [upn("felix.wagner")])
    run.lines.clear()
    assert run.reset("--apply") == 1 and "NEEDS A HUMAN" in run.text


# --- wiring


def test_the_default_admin_comes_from_the_device_code_connection(tmp_path, monkeypatch):
    seen = {}

    def fake_connect(domain, apply, out):
        seen["args"] = (domain, apply)
        return FakeAdminGraph(DOMAIN, ADMIN, BREAKGLASS)

    monkeypatch.setattr(connect, "connect", fake_connect)
    lines = []
    assert (
        seed_cli.main(
            ["--apply", "--no-snapshot"], out=lines.append, log_path=tmp_path / "l", env=ENV
        )
        == 0
    )
    assert seen["args"] == (DOMAIN, True)


@pytest.mark.parametrize("module", ["seed.seed", "seed.reset"])
def test_the_modules_run_as_scripts(module):
    r = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0 and "--apply" in r.stdout and "plan" in r.stdout.lower()
