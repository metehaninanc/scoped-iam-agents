"""Task 0.2: seed/tenant.yaml is generated from seed/seed-org.md and validated against
data/validate.py, policy/policy.yaml and the invariants of the permission design."""

import re

import pytest

from iamagents import seedconfig as sc
from iamagents.gateway.config import ROOT
from iamagents.gateway.policy import Policy
from seed import generate_tenant as gen

POLICY = Policy.load(ROOT / "policy" / "policy.yaml")


@pytest.fixture(scope="module")
def tenant():
    return sc.load_tenant()


def mutated(tenant, fn):
    t = tenant.model_copy(deep=True)
    fn(t)
    return sc.validate_tenant(t)


def has(errors, needle):
    return any(needle in e for e in errors)


# --- generation


def test_committed_yaml_equals_a_fresh_generation():
    """The build fails if seed-org.md and tenant.yaml diverge."""
    committed = sc.TENANT_YAML.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert committed == gen.generate()
    assert gen.main(["--check"]) == 0


def test_generated_file_says_do_not_edit():
    head = sc.TENANT_YAML.read_text(encoding="utf-8").splitlines()[:2]
    assert "Generated from seed/seed-org.md" in head[0] and "Do not edit" in head[0]


def test_generation_is_deterministic():
    assert gen.generate() == gen.generate()


def test_check_fails_when_the_yaml_is_stale(tmp_path, monkeypatch):
    stale = tmp_path / "tenant.yaml"
    stale.write_text(gen.generate().replace("cap: 25", "cap: 26"), encoding="utf-8")
    monkeypatch.setattr(gen, "TARGET", stale)
    assert gen.main(["--check"]) == 1


def test_parser_rejects_a_spec_with_the_wrong_number_of_users():
    md = gen.SOURCE.read_text(encoding="utf-8")
    lines = [ln for ln in md.splitlines() if not ln.startswith("| 22 |")]
    with pytest.raises(ValueError, match="22 users"):
        gen.parse("\n".join(lines))


def test_parser_reports_a_missing_section():
    md = gen.SOURCE.read_text(encoding="utf-8").replace("## Licenses", "## Seats")
    with pytest.raises(ValueError, match="Licenses"):
        gen.parse(md)


# --- the valid config


def test_the_committed_config_is_valid(tenant):
    assert sc.validate_tenant(tenant) == []
    sc.assert_valid(tenant)


def test_shape(tenant):
    assert len(tenant.users) == 22 and len(tenant.pre_hires) == 2 and len(tenant.groups) == 21
    assert {a.key for a in tenant.accounts} == {"admin", "breakglass"}
    assert [a.name for a in tenant.admin_units] == ["AU-Workforce", "RMAU-Protected"]
    assert tenant.licenses.cap == 25 == POLICY.global_["license_cap"]


def test_every_user_has_the_attributes_the_seed_needs(tenant):
    for u in tenant.users:
        assert u.usage_location == "DE" and u.user_type == "Member" and u.job_title and u.department
        assert u.given_name and u.surname and u.display_name == f"{u.given_name} {u.surname}"


def test_employee_type_is_employee_except_for_the_three_privileged_users(tenant):
    privileged = {u.upn_prefix for u in tenant.users if u.employee_type == "Privileged"}
    assert privileged == {"priya.nair", "tom.becker", "sofia.rossi"}
    assert all(
        u.employee_type == "Employee" for u in tenant.users if u.upn_prefix not in privileged
    )


def test_au_workforce_is_the_employees_only(tenant):
    rule = tenant.admin_unit("AU-Workforce").rule
    assert rule == '(user.userType -eq "Member") and (user.employeeType -eq "Employee")'
    members = sc.dynamic_members(tenant, rule)
    assert len(members) == 19 and not ({"priya.nair", "tom.becker", "sofia.rossi"} & set(members))


def test_no_role_holder_matches_the_au_workforce_rule(tenant):
    rule = tenant.admin_unit("AU-Workforce").rule
    users = {u.upn_prefix: u for u in tenant.users}
    for key in sc.role_holders(tenant):
        attrs = (
            sc.user_attrs(users[key])
            if key in users
            else sc.account_attrs(next(a for a in tenant.accounts if a.key == key))
        )
        assert not sc.rule_matches(rule, attrs), key
    assert sc.role_holders(tenant) == {
        "admin",
        "breakglass",
        "priya.nair",
        "tom.becker",
        "sofia.rossi",
    }


def test_rmau_holds_both_admin_accounts_the_privileged_users_and_the_priv_groups(tenant):
    assert set(tenant.admin_unit("RMAU-Protected").members) == {
        "priya.nair", "tom.becker", "sofia.rossi", "${TENANT_ADMIN_UPN}", "${BREAKGLASS_UPN}",
        "PRIV-User-Admin", "PRIV-Helpdesk-Admin", "PRIV-Security-Reader",
    }  # fmt: skip


def test_the_dynamic_department_groups_are_derived_from_the_rule(tenant):
    assert sorted(sc.group_members(tenant, tenant.group("DG-Dept-IT"))) == [
        "marcus.weber",
        "priya.nair",
        "sofia.rossi",
        "tom.becker",
    ]
    assert len(sc.group_members(tenant, tenant.group("DG-Dept-Sales"))) == 4


def test_a_joiner_created_with_the_ig01_attributes_lands_in_au_workforce(tenant):
    attrs = POLICY.rule_for("ig", "joiner").model_extra["create_attributes"]
    assert attrs == {"userType": "Member", "employeeType": "Employee", "usageLocation": "DE"}
    rule = tenant.admin_unit("AU-Workforce").rule
    assert sc.rule_matches(rule, attrs | {"department": "Engineering"})
    assert not sc.rule_matches(
        rule, {"userType": "Member", "department": "Engineering"}
    )  # no employeeType: out of reach


def test_the_spec_states_the_ig01_requirement():
    md = (ROOT / "seed" / "seed-org.md").read_text(encoding="utf-8")
    assert "IG-01 must set `employeeType=Employee`" in md


def test_seat_math(tenant):
    seats = sum(u.licensed for u in tenant.users) + sum(a.licensed for a in tenant.accounts)
    assert seats == 23  # 22 employees and the licensed admin; the emergency account has no seat
    assert seats + len(tenant.pre_hires) == tenant.licenses.cap  # E1 and E2 fill the cap
    assert seats + len(tenant.pre_hires) + 1 > tenant.licenses.cap  # a third joiner must fail


def test_no_real_domain_literal_address_or_secret_in_the_yaml():
    text = sc.TENANT_YAML.read_text(encoding="utf-8")
    assert "<tenant>.onmicrosoft.com" in text
    assert not re.search(r"[\w.]+@[\w.-]+", text)  # no address at all, only ${VAR} references
    assert not re.search(r"(?i)password|secret|token|credential", text)
    assert re.findall(r"\$\{([A-Z_]+)\}", text).count("TENANT_ADMIN_UPN") >= 1


# --- negative cases: the validator must be able to fail


def test_missing_user(tenant):
    errors = mutated(tenant, lambda t: t.users.remove(t.user("ben.altmann")))
    assert has(errors, "users: ben.altmann is missing")


def test_wrong_manager_or_department(tenant):
    assert has(
        mutated(tenant, lambda t: setattr(t.user("zoe.martin"), "manager", "lena.hoffmann")),
        "zoe.martin has department/manager",
    )
    assert has(
        mutated(tenant, lambda t: setattr(t.user("mei.tanaka"), "department", "IT")),
        "mei.tanaka has department/manager",
    )


def test_manager_cycle(tenant):
    errors = mutated(tenant, lambda t: setattr(t.user("lena.hoffmann"), "manager", "marcus.weber"))
    assert has(errors, "cycle") and has(errors, "exactly one user")


def test_extra_member_in_a_restricted_group(tenant):
    errors = mutated(
        tenant, lambda t: t.group("SG-Share-HR-Confidential").members.append("zoe.martin")
    )
    assert has(errors, "SG-Share-HR-Confidential members differ")


def test_segregation_of_duties_violation(tenant):
    errors = mutated(tenant, lambda t: t.group("SG-App-ERP-Approve").members.append("ines.duarte"))
    assert has(errors, "sod: SG-App-ERP-Create and SG-App-ERP-Approve share members")


def test_wrong_owner(tenant):
    assert has(
        mutated(tenant, lambda t: setattr(t.group("SG-App-Jira"), "owner", "marcus.weber")),
        "SG-App-Jira owner",
    )


def test_a_group_with_the_wrong_class_or_a_missing_group(tenant):
    assert has(
        mutated(tenant, lambda t: setattr(t.group("SG-App-CRM"), "klass", "restricted")),
        "SG-App-CRM class",
    )
    assert has(
        mutated(tenant, lambda t: t.groups.remove(t.group("SG-Wiki-All"))),
        "groups: SG-Wiki-All is missing",
    )


def test_dynamic_group_with_explicit_members_or_no_rule(tenant):
    assert has(
        mutated(tenant, lambda t: t.group("DG-Dept-IT").members.append("lena.hoffmann")),
        "needs a rule and no explicit members",
    )
    assert has(
        mutated(
            tenant, lambda t: setattr(t.group("DG-Dept-IT"), "rule", 'user.department -eq "HR"')
        ),
        "DG-Dept-IT members differ",
    )


def test_unsupported_rule_syntax_is_reported(tenant):
    errors = mutated(
        tenant, lambda t: setattr(t.group("DG-Dept-IT"), "rule", 'user.department -contains "I"')
    )
    assert has(errors, "unsupported rule clause")


def test_a_privileged_user_who_is_an_employee_is_in_au_workforce(tenant):
    errors = mutated(tenant, lambda t: setattr(t.user("priya.nair"), "employee_type", "Employee"))
    assert has(errors, "priya.nair holds a directory role but matches the AU-Workforce rule")
    assert has(errors, "privileged set")


def test_a_non_privileged_user_marked_privileged(tenant):
    assert has(
        mutated(tenant, lambda t: setattr(t.user("zoe.martin"), "employee_type", "Privileged")),
        "privileged set",
    )


def test_the_au_workforce_rule_must_be_exact(tenant):
    old_rule = 'user.userType -eq "Member"'
    errors = mutated(tenant, lambda t: setattr(t.admin_unit("AU-Workforce"), "rule", old_rule))
    assert has(errors, "AU-Workforce rule must be exactly")


def test_admin_accounts_must_be_in_the_rmau_and_use_references(tenant):
    assert has(
        mutated(tenant, lambda t: setattr(t.accounts[0], "in_rmau", False)),
        "must be in RMAU-Protected",
    )
    assert has(
        mutated(tenant, lambda t: setattr(t.accounts[1], "upn", "breakglass@example.com")),
        "must be a ${VAR} reference",
    )
    errors = mutated(
        tenant, lambda t: t.admin_unit("RMAU-Protected").members.remove("${BREAKGLASS_UPN}")
    )
    assert has(errors, "RMAU-Protected members differ")


def test_priv_group_missing_from_the_rmau(tenant):
    errors = mutated(
        tenant, lambda t: t.admin_unit("RMAU-Protected").members.remove("PRIV-User-Admin")
    )
    assert has(errors, "RMAU-Protected members differ")


def test_a_real_domain_is_rejected(tenant):
    assert has(
        mutated(tenant, lambda t: setattr(t, "domain", "contoso-example.onmicrosoft.com")),
        "never a real domain",
    )


def test_baselines_must_match_the_policy(tenant):
    assert has(
        mutated(tenant, lambda t: t.baselines["Engineering"].append("SG-App-CRM")),
        "baselines: differ from policy",
    )
    assert has(
        mutated(tenant, lambda t: t.baselines.__setitem__("Engineering", ["SG-App-ERP-Create"])),
        "must be an open assigned group",
    )


def test_license_cap_must_match_the_policy_and_the_seat_math(tenant):
    errors = mutated(tenant, lambda t: setattr(t.licenses, "cap", 30))
    assert has(errors, "differs from policy global.license_cap") and has(
        errors, "must equal the cap"
    )
    assert has(
        mutated(tenant, lambda t: setattr(t.accounts[1], "licensed", True)), "must equal the cap"
    )


def test_protected_groups_must_match_the_policy_patterns(tenant):
    assert has(
        mutated(tenant, lambda t: setattr(t.group("SG-App-Jira"), "klass", "protected")),
        "protected groups differ",
    )


# --- rule evaluator and environment references


def test_rule_parsing_and_matching():
    assert sc.parse_rule('user.department -eq "IT"') == [("department", "IT")]
    assert sc.parse_rule('(user.a -eq "x") and (user.b -eq "y")') == [("a", "x"), ("b", "y")]
    assert sc.rule_matches('(user.a -eq "x") and (user.b -eq "y")', {"a": "x", "b": "y"})
    assert not sc.rule_matches('(user.a -eq "x") and (user.b -eq "y")', {"a": "x", "b": None})
    for bad in ('user.a -ne "x"', "user.a -eq x", "", 'user.a -eq "x" or user.b -eq "y"'):
        with pytest.raises(sc.RuleError):
            sc.parse_rule(bad)


def test_accounts_are_resolved_from_the_environment(tenant):
    env = {"TENANT_ADMIN_UPN": "root@x.example", "BREAKGLASS_UPN": " bg@x.example "}
    assert sc.resolve_accounts(tenant, env) == {
        "admin": "root@x.example",
        "breakglass": "bg@x.example",
    }


def test_a_missing_variable_is_an_error_that_names_it(tenant):
    with pytest.raises(KeyError, match="BREAKGLASS_UPN"):
        sc.resolve_accounts(tenant, {"TENANT_ADMIN_UPN": "root@x.example"})


def test_without_an_environment_the_fake_gets_made_up_addresses(tenant):
    assert sc.resolve_accounts(tenant, None) == {
        "admin": "admin@<tenant>.onmicrosoft.com",
        "breakglass": "breakglass@<tenant>.onmicrosoft.com",
    }
    assert sc.resolve_accounts(tenant, None, "t.example")["admin"] == "admin@t.example"


def test_env_ref():
    assert sc.env_ref("${TENANT_ADMIN_UPN}") == "TENANT_ADMIN_UPN"
    assert sc.env_ref("admin@x") is None and sc.env_ref("${lower}") is None


def test_policy_carries_the_seat_cap():
    assert POLICY.global_["license_cap"] == 25


# --- the fake Graph org is built from tenant.yaml


def test_fake_org_comes_from_the_config(tenant):
    from iamagents.gateway.graph import fake_org

    org = fake_org.build_org()
    assert (
        len(org["groups"]) == 21
        and len([u for u in org["users"].values() if u["employeeType"] == "Employee"]) == 19
    )
    priya = org["users"]["priya.nair@<tenant>.onmicrosoft.com"]
    assert priya["employeeType"] == "Privileged" and priya["jobTitle"] == "IAM Administrator"
    assert org["groups"]["DG-Dept-IT"]["members"] and org["groups"]["SG-App-CRM"]["owners"] == [
        "felix.wagner@<tenant>.onmicrosoft.com"
    ]


def test_fake_org_protects_both_admin_accounts_and_the_privileged_users(tenant):
    from iamagents.gateway.graph import fake_org

    org = fake_org.build_org("t.example")
    assert org["rmau_users"] == {
        "priya.nair@t.example", "tom.becker@t.example", "sofia.rossi@t.example",
        "admin@t.example", "breakglass@t.example",
    }  # fmt: skip
    for upn in org["rmau_users"]:
        attrs = {k: org["users"][upn][k] for k in ("userType", "employeeType", "department")}
        assert not sc.rule_matches(
            tenant.admin_unit("AU-Workforce").rule, attrs
        )  # outside agent write scope too


def test_the_env_example_names_both_admin_variables():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for var in ("TENANT_ADMIN_UPN=", "BREAKGLASS_UPN=", "TENANT_DOMAIN="):
        assert re.search(rf"^{var}$", text, re.M)  # present and empty
