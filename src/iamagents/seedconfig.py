"""The seed config `seed/tenant.yaml`: models, loading, dynamic rule evaluation and validation.

`seed/seed-org.md` is the source and `seed/generate_tenant.py` generates the YAML. This module
reads it. `validate_tenant` checks the config against two independent sources, the constants in
`data/validate.py` (the world the ticket dataset was labelled against) and `policy/policy.yaml`,
plus the invariants of the permission design.

The two admin accounts are referenced as `${TENANT_ADMIN_UPN}` and `${BREAKGLASS_UPN}` and
resolved from `.env` at seed time. Their values never enter a committed file.
"""

import importlib.util
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .gateway.config import ROOT
from .gateway.policy import Policy

TENANT_YAML = ROOT / "seed" / "tenant.yaml"
PLACEHOLDER_DOMAIN = "<tenant>.onmicrosoft.com"
AU_WORKFORCE_RULE = '(user.userType -eq "Member") and (user.employeeType -eq "Employee")'
EMPLOYEE, PRIVILEGED = "Employee", "Privileged"
_ENV_REF = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")
_CLAUSE = re.compile(r'^\(?\s*user\.(\w+)\s+-eq\s+"([^"]*)"\s*\)?$')


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class User(_Model):
    upn_prefix: str
    display_name: str
    given_name: str
    surname: str
    job_title: str
    department: str
    manager: str | None
    employee_type: Literal["Employee", "Privileged"]
    user_type: Literal["Member"] = "Member"
    usage_location: str = "DE"
    licensed: bool = True


class PreHire(_Model):
    upn_prefix: str
    display_name: str
    job_title: str
    department: str
    manager: str


class Group(_Model):
    name: str
    kind: Literal["dynamic", "assigned", "role_assignable"]
    klass: Literal["open", "restricted", "protected"] = Field(alias="class")
    owner: str | None = None
    members: list[str] = []
    rule: str | None = None
    role: str | None = None


class Account(_Model):
    key: str
    upn: str  # an environment reference such as ${TENANT_ADMIN_UPN}, never a literal address
    roles: list[str]
    licensed: bool
    in_rmau: bool


class AdminUnit(_Model):
    name: str
    kind: Literal["dynamic", "restricted_management"]
    rule: str | None = None
    members: list[str] = []  # user prefixes, ${VAR} references and group names (RMAU only)


class Licenses(_Model):
    cap: int
    sku: str | None = None


class Tenant(_Model):
    domain: str
    users: list[User]
    pre_hires: list[PreHire]
    groups: list[Group]
    accounts: list[Account]
    admin_units: list[AdminUnit]
    licenses: Licenses
    baselines: dict[str, list[str]]

    def user(self, prefix: str) -> User:
        return next(u for u in self.users if u.upn_prefix == prefix)

    def group(self, name: str) -> Group:
        return next(g for g in self.groups if g.name == name)

    def admin_unit(self, name: str) -> AdminUnit:
        return next(a for a in self.admin_units if a.name == name)


class RuleError(ValueError):
    pass


def load_tenant(path: Path = TENANT_YAML) -> Tenant:
    return Tenant.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


# --- dynamic rules


def parse_rule(rule: str) -> list[tuple[str, str]]:
    """Parse `user.a -eq "x" and user.b -eq "y"` (with optional parentheses) into clauses."""
    clauses = []
    for part in re.split(r"\s+and\s+", rule.strip()):
        m = _CLAUSE.match(part.strip())
        if not m:
            raise RuleError(f"unsupported rule clause: {part!r}")
        clauses.append((m.group(1), m.group(2)))
    return clauses


def rule_matches(rule: str, attrs: Mapping[str, str | None]) -> bool:
    return all(attrs.get(attr) == value for attr, value in parse_rule(rule))


def user_attrs(u: User) -> dict[str, str | None]:
    return {"userType": u.user_type, "department": u.department, "employeeType": u.employee_type}


def account_attrs(_: Account) -> dict[str, str | None]:
    """An admin account is a Member and has no employeeType."""
    return {"userType": "Member", "department": None, "employeeType": None}


def dynamic_members(tenant: Tenant, rule: str) -> list[str]:
    """User prefixes that match a rule (accounts are handled separately)."""
    return [u.upn_prefix for u in tenant.users if rule_matches(rule, user_attrs(u))]


def group_members(tenant: Tenant, group: Group) -> list[str]:
    return dynamic_members(tenant, group.rule) if group.kind == "dynamic" else list(group.members)


def role_holders(tenant: Tenant) -> set[str]:
    """Keys of every account that holds a directory role: admin accounts and PRIV group members."""
    out = {a.key for a in tenant.accounts if a.roles}
    for g in tenant.groups:
        if g.kind == "role_assignable" and g.role:
            out |= set(g.members)
    return out


# --- accounts from the environment


def env_ref(value: str) -> str | None:
    m = _ENV_REF.match(value)
    return m.group(1) if m else None


def resolve_accounts(
    tenant: Tenant, env: Mapping[str, str] | None, domain: str | None = None
) -> dict[str, str]:
    """Account key -> UPN. With `env=None` (tests, the fake Graph) the UPNs are made up from the
    domain; with an environment, a missing variable is an error."""
    out = {}
    for a in tenant.accounts:
        var = env_ref(a.upn)
        if var is None:
            raise ValueError(f"account {a.key}: upn must be a ${{VAR}} reference")
        if env is None:
            out[a.key] = f"{a.key}@{domain or tenant.domain}"
        elif env.get(var):
            out[a.key] = env[var].strip()
        else:
            raise KeyError(f"{var} is not set (needed for the {a.key} account); put it in .env")
    return out


# --- validation


def load_reference():
    """The constants of `data/validate.py`, the world the dataset is labelled against."""
    spec = importlib.util.spec_from_file_location(
        "iam_dataset_validate", ROOT / "data" / "validate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_tenant(tenant: Tenant, reference=None, policy: Policy | None = None) -> list[str]:
    """Return a list of problems; an empty list means the config is valid."""
    ref = reference or load_reference()
    policy = policy or Policy.load(ROOT / "policy" / "policy.yaml")
    errors: list[str] = []
    err = errors.append

    if tenant.domain != PLACEHOLDER_DOMAIN:
        err(f"domain must be the placeholder {PLACEHOLDER_DOMAIN}, never a real domain")
    users = {u.upn_prefix: u for u in tenant.users}
    if len(users) != len(tenant.users):
        err("users: duplicate upn_prefix")

    # users, departments, managers against data/validate.py
    ref_users = {p: (dept, mgr) for p, (dept, mgr) in ref.USERS.items()}
    for p in sorted(set(ref_users) - set(users)):
        err(f"users: {p} is missing")
    for p in sorted(set(users) - set(ref_users)):
        err(f"users: {p} is not in data/validate.py")
    for p, (dept, mgr) in ref_users.items():
        if p in users and (users[p].department, users[p].manager) != (dept, mgr):
            err(
                f"users: {p} has department/manager {users[p].department}/{users[p].manager}, expected {dept}/{mgr}"
            )
    for u in tenant.users:
        if u.manager is not None and u.manager not in users:
            err(f"users: manager {u.manager} of {u.upn_prefix} does not exist")
        seen, cur = set(), u.upn_prefix
        while cur is not None and cur in users:
            if cur in seen:
                err(f"users: manager chain of {u.upn_prefix} has a cycle")
                break
            seen.add(cur)
            cur = users[cur].manager
    if sum(u.manager is None for u in tenant.users) != 1:
        err("users: exactly one user (the CEO) has no manager")
    if {h.upn_prefix for h in tenant.pre_hires} != set(ref.PREHIRE):
        err("pre_hires: do not match data/validate.py")
    for h in tenant.pre_hires:
        if h.manager not in users:
            err(f"pre_hires: manager {h.manager} of {h.upn_prefix} does not exist")

    # groups against data/validate.py
    groups = {g.name: g for g in tenant.groups}
    if len(groups) != len(tenant.groups):
        err("groups: duplicate name")
    for name in sorted(set(ref.GROUPS) - set(groups)):
        err(f"groups: {name} is missing")
    for name in sorted(set(groups) - set(ref.GROUPS)):
        err(f"groups: {name} is not in data/validate.py")
    for name, (klass, owner, members) in ref.GROUPS.items():
        g = groups.get(name)
        if g is None:
            continue
        if g.klass != klass:
            err(f"groups: {name} class {g.klass}, expected {klass}")
        if g.owner != owner:
            err(f"groups: {name} owner {g.owner}, expected {owner}")
        if g.kind == "dynamic" and not g.rule:
            continue  # reported below
        try:
            actual = group_members(tenant, g)
        except RuleError as e:
            err(f"groups: {name}: {e}")
            continue
        if sorted(actual) != sorted(members):
            err(
                f"groups: {name} members differ from data/validate.py: {sorted(set(actual) ^ set(members))}"
            )
    for g in tenant.groups:
        if g.owner is not None and g.owner not in users:
            err(f"groups: owner {g.owner} of {g.name} does not exist")
        for m in g.members:
            if m not in users:
                err(f"groups: member {m} of {g.name} does not exist")
        if g.kind == "dynamic" and (not g.rule or g.members):
            err(f"groups: {g.name} is dynamic: needs a rule and no explicit members")
        if g.kind == "role_assignable" and (not g.role or g.klass != "protected"):
            err(f"groups: {g.name} is role assignable: needs a role and class protected")
        if g.kind != "role_assignable" and g.role:
            err(f"groups: only role assignable groups carry a role ({g.name})")
        if g.role and g.role not in ref.ROLES:
            err(f"groups: role {g.role} of {g.name} is not in data/validate.py ROLES")

    # privileged users and the employeeType invariant
    privileged = {u.upn_prefix for u in tenant.users if u.employee_type == PRIVILEGED}
    if privileged != set(ref.PRIVILEGED):
        err(f"users: privileged set {sorted(privileged)} differs from data/validate.py")
    priv_group_members = {
        m for g in tenant.groups if g.kind == "role_assignable" for m in g.members
    }
    if priv_group_members != privileged:
        err("users: exactly the members of the role assignable groups must be Privileged")

    # accounts, RMAU, AU-Workforce
    accounts = {a.key: a for a in tenant.accounts}
    for a in tenant.accounts:
        if env_ref(a.upn) is None:
            err(f"accounts: {a.key} upn must be a ${{VAR}} reference, not a literal address")
        if not a.in_rmau:
            err(f"accounts: {a.key} must be in RMAU-Protected")
        if not a.roles:
            err(
                f"accounts: {a.key} has no directory role (admin accounts hold Global Administrator)"
            )
        for r in a.roles:
            if r not in ref.ROLES:
                err(f"accounts: role {r} of {a.key} is not in data/validate.py ROLES")
    refs = [env_ref(a.upn) for a in tenant.accounts]
    if len(set(refs)) != len(refs):
        err("accounts: two accounts use the same variable")
    aus = {a.name: a for a in tenant.admin_units}
    wf, rmau = aus.get("AU-Workforce"), aus.get("RMAU-Protected")
    if wf is None or rmau is None:
        err("admin_units: AU-Workforce and RMAU-Protected are required")
    else:
        if wf.kind != "dynamic" or wf.rule != AU_WORKFORCE_RULE:
            err(f"admin_units: AU-Workforce rule must be exactly {AU_WORKFORCE_RULE}")
        if rmau.kind != "restricted_management":
            err("admin_units: RMAU-Protected must be restricted management")
        expected_rmau = (
            privileged
            | {a.upn for a in tenant.accounts if a.in_rmau}
            | {g.name for g in tenant.groups if g.kind == "role_assignable"}
        )
        if set(rmau.members) != expected_rmau:
            err(
                f"admin_units: RMAU-Protected members differ: {sorted(set(rmau.members) ^ expected_rmau)}"
            )
        if wf.rule == AU_WORKFORCE_RULE:
            members = set(dynamic_members(tenant, wf.rule))
            for key in sorted(role_holders(tenant)):
                if key in accounts:
                    matched = rule_matches(wf.rule, account_attrs(accounts[key]))
                else:
                    matched = key in members
                if matched:
                    err(
                        f"admin_units: {key} holds a directory role but matches the AU-Workforce rule"
                    )
            employees = {u.upn_prefix for u in tenant.users if u.employee_type == EMPLOYEE}
            if members != employees:
                err("admin_units: AU-Workforce must contain exactly the employees")

    # baselines, protected groups and SoD against the policy
    if tenant.baselines != {k: list(v) for k, v in policy.baselines.items()}:
        err("baselines: differ from policy/policy.yaml")
    for dept in {u.department for u in tenant.users}:
        if dept not in tenant.baselines:
            err(f"baselines: no baseline for department {dept}")
    for dept, names in tenant.baselines.items():
        for n in names:
            if n not in groups or groups[n].kind != "assigned" or groups[n].klass != "open":
                err(f"baselines: {n} (baseline of {dept}) must be an open assigned group")
    protected_patterns = policy.global_.get("protected_groups", [])
    protected_names = {
        n for n in groups if any(re.fullmatch(p.replace("*", ".*"), n) for p in protected_patterns)
    }
    declared = {g.name for g in tenant.groups if g.klass == "protected"}
    if protected_names != declared:
        err(
            f"groups: protected groups differ from policy.protected_groups: {sorted(protected_names ^ declared)}"
        )
    for a, b in policy.sod_pairs:
        for n in (a, b):
            if n not in groups or groups[n].klass != "restricted":
                err(f"sod: {n} must be a restricted group")
        if a in groups and b in groups and set(groups[a].members) & set(groups[b].members):
            err(f"sod: {a} and {b} share members")

    # licenses and seats
    cap = tenant.licenses.cap
    if cap != policy.global_.get("license_cap"):
        err(f"licenses: cap {cap} differs from policy global.license_cap")
    seats_at_seed = sum(u.licensed for u in tenant.users) + sum(a.licensed for a in tenant.accounts)
    if seats_at_seed + len(tenant.pre_hires) != cap:
        err(
            f"licenses: seats {seats_at_seed} at seed plus {len(tenant.pre_hires)} joiners must equal the cap {cap}"
        )
    return errors


def assert_valid(tenant: Tenant) -> None:
    problems = validate_tenant(tenant)
    if problems:
        raise ValueError("tenant.yaml is invalid:\n  " + "\n  ".join(problems))
