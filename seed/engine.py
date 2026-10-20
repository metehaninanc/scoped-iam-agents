"""Seed and reset logic: desired state, observed state, diff, guards, apply.

Both `seed.py` and `reset.py` use one mechanism. A *state* is a plain dict keyed by user prefix
and group name (so it holds no domain and can be committed as `seed/baseline.json`):

    {"users":  {"zoe.martin": {"displayName": ..., "department": ..., "manager": "felix.wagner",
                               "license": "AAD_PREMIUM_P2" | None, ...}},
     "groups": {"SG-App-CRM": {"kind": "assigned", "rule": None, "role": None,
                               "owners": [...], "members": [...]}}}

`seed` diffs the state described by `seed/tenant.yaml` against the tenant. `reset` diffs the tenant
against the snapshot `seed/baseline.json` and repairs toward it. Everything is a plan until
`apply` is asked for.

Safety rules, enforced here and tested:

* The two admin accounts are invisible to the diff (never created, changed, owned, deleted) and any
  change that names one is refused.
* Members of `RMAU-Protected` (the privileged users, the PRIV groups, and whatever the live RMAU
  holds) are never deleted, under any flag. Unknown objects are only deleted with `prune`.
* A joiner or a license assignment that would pass `license_cap` is refused before the first write.
* Initial passwords are random, travel as `Secret` and are never printed, logged or stored.
"""

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from iamagents.gateway.secrets_registry import SECRETS
from iamagents.gateway.tools import generate_temp_password
from iamagents.seedconfig import Tenant, dynamic_members

from .admin_graph import AdminGraph, Sku

MANAGED_GROUP = re.compile(r"^(DG|PRIV|SG|GG)-")
MANAGED_EMPLOYEE_TYPES = ("Employee", "Privileged")
BASELINE_VERSION = 1

USER_FIELDS = {  # state key -> Graph attribute name
    "displayName": "displayName",
    "givenName": "givenName",
    "surname": "surname",
    "jobTitle": "jobTitle",
    "department": "department",
    "employeeType": "employeeType",
    "userType": "userType",
    "usageLocation": "usageLocation",
    "accountEnabled": "accountEnabled",
}


class SeedError(Exception):
    pass


class ProtectedObjectError(SeedError):
    """A change would touch an admin account or delete a member of the RMAU."""


class LicenseCapError(SeedError):
    pass


class PreflightError(SeedError):
    pass


class SkuChoiceError(SeedError):
    pass


@dataclass
class Context:
    tenant: Tenant
    domain: str
    accounts: dict[str, str]  # account key -> UPN
    sku_part: str | None = None

    def upn(self, prefix: str) -> str:
        return f"{prefix}@{self.domain}"

    def prefix(self, upn: str) -> str:
        suffix = "@" + self.domain.lower()
        return upn[: -len(suffix)].lower() if upn.lower().endswith(suffix) else upn.lower()

    @property
    def admin_upns(self) -> set[str]:
        return {u.lower() for u in self.accounts.values()}

    def is_admin(self, upn: str) -> bool:
        return upn.lower() in self.admin_upns


@dataclass(frozen=True)
class Change:
    op: str
    key: str
    detail: dict = field(default_factory=dict)
    destructive: bool = False  # only applied with prune

    def line(self) -> str:
        """One readable line. Attributes are summarised; nothing here can hold a secret."""
        detail = dict(self.detail)
        attrs = detail.pop("attrs", None)
        parts = [f"{k}={v}" for k, v in detail.items()]
        if attrs is not None:
            shown = (
                attrs
                if self.op == "update_user"
                else {k: attrs[k] for k in ("department", "employeeType") if k in attrs}
            )
            parts.append(", ".join(f"{k}={v}" for k, v in shown.items()))
        extra = ", ".join(p for p in parts if p)
        return f"{self.op:<20} {self.key}" + (f"  ({extra})" if extra else "")


PRIORITY = {
    "create_user": 0, "update_user": 1, "set_manager": 2, "remove_license": 3, "assign_license": 4,
    "create_group": 5, "update_group_rule": 6, "add_owner": 7, "add_member": 8, "assign_role": 9,
    "remove_member": 10, "remove_owner": 11, "remove_role": 12,
    "delete_group": 13, "delete_user": 14, "unrepairable": 15,
}  # fmt: skip


@dataclass
class Protected:
    users: set[str]  # prefixes
    groups: set[str]
    admin_upns: set[str]


# --- state


def desired_state(ctx: Context) -> dict:
    t = ctx.tenant
    users = {}
    for u in t.users:
        users[u.upn_prefix] = {
            "displayName": u.display_name, "givenName": u.given_name, "surname": u.surname,
            "jobTitle": u.job_title, "department": u.department, "employeeType": u.employee_type,
            "userType": u.user_type, "usageLocation": u.usage_location, "accountEnabled": True,
            "manager": u.manager, "license": ctx.sku_part if u.licensed else None,
        }  # fmt: skip
    groups = {}
    for g in t.groups:
        groups[g.name] = {
            "kind": g.kind, "rule": g.rule, "role": g.role,
            "owners": [g.owner] if g.owner else [],
            "members": None if g.kind == "dynamic" else sorted(g.members),
        }  # fmt: skip
    return {"users": users, "groups": groups}


@dataclass
class Observed:
    state: dict
    sku_by_id: dict[str, Sku]
    seats_in_use: int  # users in the whole tenant holding the chosen license
    dynamic_members: dict[str, list[str]]  # group -> prefixes
    sku_id: str | None


def observe(admin: AdminGraph, ctx: Context) -> Observed:
    skus = {s.id: s for s in admin.list_skus()}
    chosen = next((s for s in skus.values() if s.part_number == ctx.sku_part), None)
    desired_prefixes = {u.upn_prefix for u in ctx.tenant.users}
    users, seats = {}, 0
    for u in admin.list_users():
        if chosen and chosen.id in u.license_sku_ids:
            seats += 1
        if ctx.is_admin(u.upn):
            continue  # invisible to the diff
        prefix = ctx.prefix(u.upn)
        if u.employee_type not in MANAGED_EMPLOYEE_TYPES and prefix not in desired_prefixes:
            continue  # not ours
        users[prefix] = {
            "displayName": u.display_name or "", "givenName": u.given_name or "", "surname": u.surname or "",
            "jobTitle": u.job_title or "", "department": u.department or "", "employeeType": u.employee_type,
            "userType": u.user_type, "usageLocation": u.usage_location, "accountEnabled": u.account_enabled,
            "manager": ctx.prefix(u.manager_upn) if u.manager_upn else None,
            "license": chosen.part_number if chosen and chosen.id in u.license_sku_ids else None,
        }  # fmt: skip

    def people(upns: list[str]) -> list[str]:
        return sorted(ctx.prefix(x) for x in upns if not ctx.is_admin(x))

    groups, dynamic = {}, {}
    for g in admin.list_groups():
        if not MANAGED_GROUP.match(g.name):
            continue
        entry = {
            "kind": g.kind,
            "rule": g.rule,
            "role": None,
            "owners": people(admin.group_owners(g.name)),
            "members": None,
        }
        if g.kind == "dynamic":
            dynamic[g.name] = people(admin.group_members(g.name))
        else:
            entry["members"] = people(admin.group_members(g.name))
        if g.kind == "role_assignable":
            roles = admin.group_roles(g.name)
            entry["role"] = (
                roles[0] if len(roles) == 1 else (None if not roles else "+".join(roles))
            )
        groups[g.name] = entry
    return Observed(
        {"users": users, "groups": groups}, skus, seats, dynamic, chosen.id if chosen else None
    )


# --- diff


def diff(target: dict, current: dict, ctx: Context | None = None) -> list[Change]:
    """Changes that turn `current` into `target`. Objects only in `current` become destructive
    deletes (applied only with prune)."""
    out: list[Change] = []
    tu, cu = target["users"], current["users"]
    for prefix, want in tu.items():
        have = cu.get(prefix)
        if have is None:
            attrs = {USER_FIELDS[k]: want[k] for k in USER_FIELDS}
            out.append(Change("create_user", prefix, {"attrs": attrs}))
            if want["manager"]:
                out.append(Change("set_manager", prefix, {"manager": want["manager"]}))
            if want["license"]:
                out.append(Change("assign_license", prefix, {"license": want["license"]}))
            continue
        changed = {USER_FIELDS[k]: want[k] for k in USER_FIELDS if have.get(k) != want[k]}
        if changed:
            out.append(Change("update_user", prefix, {"attrs": changed}))
        if have.get("manager") != want["manager"]:
            if want["manager"]:
                out.append(Change("set_manager", prefix, {"manager": want["manager"]}))
            else:
                out.append(
                    Change(
                        "unrepairable", prefix, {"why": "a manager cannot be cleared by the seed"}
                    )
                )
        if have.get("license") != want["license"]:
            if have.get("license"):
                out.append(Change("remove_license", prefix, {"license": have["license"]}))
            if want["license"]:
                out.append(Change("assign_license", prefix, {"license": want["license"]}))
    for prefix in cu:
        if prefix not in tu:
            out.append(Change("delete_user", prefix, {}, destructive=True))

    tg, cg = target["groups"], current["groups"]
    for name, want in tg.items():
        have = cg.get(name)
        if have is None:
            out.append(
                Change(
                    "create_group",
                    name,
                    {"kind": want["kind"], "rule": want["rule"], "owners": want["owners"]},
                )
            )
            if (
                want["kind"] != "dynamic"
            ):  # a baseline lists the computed members of a dynamic group
                for m in want["members"] or []:
                    out.append(Change("add_member", name, {"member": m}))
            if want["role"]:
                out.append(Change("assign_role", name, {"role": want["role"]}))
            continue
        if have["kind"] != want["kind"]:
            out.append(
                Change(
                    "unrepairable",
                    name,
                    {"why": f"group is {have['kind']}, expected {want['kind']}: recreate it"},
                )
            )
            continue
        if want["kind"] == "dynamic" and have["rule"] != want["rule"]:
            out.append(Change("update_group_rule", name, {"rule": want["rule"]}))
        for o in sorted(set(want["owners"]) - set(have["owners"])):
            out.append(Change("add_owner", name, {"owner": o}))
        for o in sorted(set(have["owners"]) - set(want["owners"])):
            out.append(Change("remove_owner", name, {"owner": o}))
        if want["kind"] != "dynamic":
            for m in sorted(set(want["members"] or []) - set(have["members"] or [])):
                out.append(Change("add_member", name, {"member": m}))
            for m in sorted(set(have["members"] or []) - set(want["members"] or [])):
                out.append(Change("remove_member", name, {"member": m}))
        if want["role"] != have["role"]:
            if have["role"]:
                out.append(Change("remove_role", name, {"role": have["role"]}))
            if want["role"]:
                out.append(Change("assign_role", name, {"role": want["role"]}))
    for name in cg:
        if name not in tg:
            out.append(Change("delete_group", name, {}, destructive=True))
    return sorted(
        out,
        key=lambda c: (PRIORITY[c.op], c.key, json.dumps(c.detail, sort_keys=True, default=str)),
    )


# --- protection and the seat cap


def protected_set(ctx: Context, admin: AdminGraph) -> Protected:
    t = ctx.tenant
    users = {u.upn_prefix for u in t.users if u.employee_type == "Privileged"}
    groups = {g.name for g in t.groups if g.kind == "role_assignable"}
    for member in admin.rmau_members():  # whatever the live RMAU holds
        if "@" in member:
            users.add(ctx.prefix(member))
        else:
            groups.add(member)
    return Protected(users, groups, ctx.admin_upns)


def guard(change: Change, ctx: Context, protected: Protected) -> None:
    """Refuse a change that touches an admin account, or deletes a member of the RMAU."""
    named = {change.key, *(str(v) for v in change.detail.values() if isinstance(v, str))}
    for n in named:
        if ctx.is_admin(n) or ctx.is_admin(ctx.upn(n)):
            raise ProtectedObjectError(f"{change.op}: refuses to touch an admin account")
    if change.op == "delete_user" and change.key in protected.users:
        raise ProtectedObjectError(
            f"delete_user {change.key}: member of RMAU-Protected is never deleted"
        )
    if change.op == "delete_group" and change.key in protected.groups:
        raise ProtectedObjectError(
            f"delete_group {change.key}: member of RMAU-Protected is never deleted"
        )


def seat_check(ctx: Context, observed: Observed, changes: list[Change]) -> tuple[int, int]:
    """(seats now, seats after the changes). Raises when the cap would be passed."""
    adds = sum(c.op == "assign_license" for c in changes)
    removes = sum(c.op == "remove_license" for c in changes)
    after = observed.seats_in_use + adds - removes
    cap = ctx.tenant.licenses.cap
    if after > cap:
        raise LicenseCapError(f"{after} seats would be in use, the license cap is {cap}")
    return observed.seats_in_use, after


# --- apply


def apply_change(admin: AdminGraph, ctx: Context, change: Change, sku_ids: dict[str, str]) -> None:
    d, key = change.detail, change.key
    if change.op == "create_user":
        password = generate_temp_password()
        SECRETS.register(password.reveal())  # second layer behind the Secret wrapper
        admin.create_user(ctx.upn(key), d["attrs"], password)
    elif change.op == "update_user":
        admin.update_user(ctx.upn(key), d["attrs"])
    elif change.op == "set_manager":
        admin.set_manager(ctx.upn(key), ctx.upn(d["manager"]))
    elif change.op == "assign_license":
        admin.assign_license(ctx.upn(key), sku_ids[d["license"]])
    elif change.op == "remove_license":
        admin.remove_license(ctx.upn(key), sku_ids[d["license"]])
    elif change.op == "delete_user":
        admin.delete_user(ctx.upn(key))
    elif change.op == "create_group":
        admin.create_group(key, d["kind"], d["rule"], [ctx.upn(o) for o in d["owners"]])
    elif change.op == "update_group_rule":
        admin.update_group_rule(key, d["rule"])
    elif change.op == "delete_group":
        admin.delete_group(key)
    elif change.op == "add_member":
        admin.add_member(key, ctx.upn(d["member"]))
    elif change.op == "remove_member":
        admin.remove_member(key, ctx.upn(d["member"]))
    elif change.op == "add_owner":
        admin.add_owner(key, ctx.upn(d["owner"]))
    elif change.op == "remove_owner":
        admin.remove_owner(key, ctx.upn(d["owner"]))
    elif change.op == "assign_role":
        admin.assign_role(key, d["role"])
    elif change.op == "remove_role":
        admin.remove_role(key, d["role"])
    else:
        raise SeedError(f"{change.op} cannot be applied: {d}")


@dataclass
class Result:
    planned: list[Change]
    applied: list[Change] = field(default_factory=list)
    skipped: list[Change] = field(default_factory=list)  # destructive without prune
    remaining: list[Change] = field(default_factory=list)  # after apply, should be empty
    seats: tuple[int, int] = (0, 0)
    dynamic_ready: bool = True


# --- preflight


def preflight(admin: AdminGraph, ctx: Context) -> None:
    domains = {d.lower() for d in admin.organization_domains()}
    if ctx.domain.lower() not in domains:
        raise PreflightError(
            "the tenant's verified domains do not include TENANT_DOMAIN: wrong tenant?"
        )
    for key, upn in ctx.accounts.items():
        if admin.get_user(upn) is None:
            raise PreflightError(f"the {key} account from .env does not exist in the tenant")


def resolve_sku(admin: AdminGraph, wanted: str | None) -> Sku:
    skus = admin.list_skus()
    if wanted:
        for s in skus:
            if s.part_number == wanted:
                return s
        raise SkuChoiceError(
            f"license {wanted} is not in the tenant; available: {[s.part_number for s in skus]}"
        )
    candidates = [s for s in skus if s.part_number.upper().startswith(("AAD_PREMIUM", "ENTRA"))]
    if len(candidates) == 1:
        return candidates[0]
    listing = ", ".join(f"{s.part_number} ({s.consumed}/{s.enabled} used)" for s in skus)
    raise SkuChoiceError(f"choose the license with LICENSE_SKU in .env; available: {listing}")


# --- dynamic groups


def wait_for_dynamic(
    admin: AdminGraph,
    ctx: Context,
    timeout: float = 1200,
    poll: float = 20,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Wait until every dynamic group holds the members its rule selects. Evaluation runs in the
    service and takes minutes after the first creation."""
    deadline = clock() + timeout
    while True:
        users_now = {ctx.prefix(u.upn): u for u in admin.list_users() if not ctx.is_admin(u.upn)}
        ready = True
        for g in ctx.tenant.groups:
            if g.kind != "dynamic":
                continue
            expected = sorted(p for p in dynamic_members(ctx.tenant, g.rule) if p in users_now)
            actual = sorted(
                ctx.prefix(x) for x in admin.group_members(g.name) if not ctx.is_admin(x)
            )
            ready &= actual == expected
        if ready:
            return True
        if clock() >= deadline:
            return False
        sleep(poll)


# --- the two runs


def plan_changes(
    admin: AdminGraph, ctx: Context, target: dict
) -> tuple[Observed, list[Change], Protected]:
    obs = observe(admin, ctx)
    return obs, diff(target, obs.state, ctx), protected_set(ctx, admin)


def run(
    admin: AdminGraph,
    ctx: Context,
    target: dict,
    *,
    apply: bool,
    prune: bool,
    wait: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = lambda s: None,
    dynamic_timeout: float = 1200,
) -> Result:
    if ctx.sku_part is None:
        raise PreflightError("the license is not resolved: call resolve_sku first")
    preflight(admin, ctx)
    obs, changes, protected = plan_changes(admin, ctx, target)
    todo = [c for c in changes if c.op != "unrepairable" and (prune or not c.destructive)]
    result = Result(planned=changes, skipped=[c for c in changes if c.destructive and not prune])
    result.seats = seat_check(ctx, obs, todo)
    if not apply:
        return result
    sku_ids = {s.part_number: s.id for s in obs.sku_by_id.values()}
    for c in todo:
        guard(c, ctx, protected)  # a hard stop, whatever the flags
    for c in todo:
        apply_change(admin, ctx, c, sku_ids)
        result.applied.append(c)
        log(c.line())
    if wait and any(g.kind == "dynamic" for g in ctx.tenant.groups):
        result.dynamic_ready = wait_for_dynamic(admin, ctx, dynamic_timeout, 20, sleep, clock)
    after = observe(admin, ctx)
    result.remaining = [
        c for c in diff(target, after.state, ctx) if c.op == "unrepairable" or not c.destructive
    ]
    return result


# --- baseline file


def config_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:16]


def render_baseline(state: dict, config_sha: str) -> str:
    payload = {"version": BASELINE_VERSION, "config_sha256": config_sha, **state}
    return json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def parse_baseline(text: str) -> dict:
    data = json.loads(text)
    if data.get("version") != BASELINE_VERSION:
        raise SeedError(f"baseline version {data.get('version')} is not supported")
    return {"users": data["users"], "groups": data["groups"]}


def verified_state(admin: AdminGraph, ctx: Context) -> dict:
    """The observed state, with dynamic groups' computed members recorded for the baseline."""
    obs = observe(admin, ctx)
    state = json.loads(json.dumps(obs.state))
    for name, members in obs.dynamic_members.items():
        state["groups"][name]["members"] = members
    return state
