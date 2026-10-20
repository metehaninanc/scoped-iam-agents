"""In-memory `AdminGraph` for unit tests.

It mimics the parts of Graph that shape the engine: a license needs a usage location first,
dynamic groups compute their members from the rule and cannot take manual members (and can lag
behind, `dynamic_lag`), role assignable groups carry roles, and every write is recorded in
`writes` (never with a password). The two admin accounts exist from the start, the admin is
licensed.
"""

from iamagents.gateway.graph.base import Secret
from iamagents.seedconfig import rule_matches

from .admin_graph import AdminGraphError, GroupRec, Sku, UserRec

_ATTR_FIELDS = {
    "displayName": "display_name",
    "givenName": "given_name",
    "surname": "surname",
    "jobTitle": "job_title",
    "department": "department",
    "employeeType": "employee_type",
    "userType": "user_type",
    "usageLocation": "usage_location",
    "accountEnabled": "account_enabled",
}


def _k(value: str) -> str:
    return value.lower()


class FakeAdminGraph:
    def __init__(
        self,
        domain: str,
        admin_upn: str,
        breakglass_upn: str,
        sku_part_number: str = "AAD_PREMIUM_P2",
        seats: int = 100,
        dynamic_lag: int = 0,
    ):
        self.domain = domain
        self.sku = Sku(
            id=f"sku-{sku_part_number.lower()}", part_number=sku_part_number, enabled=seats
        )
        self.users: dict[str, UserRec] = {}
        self.groups: dict[str, dict] = {}
        self.writes: list[tuple] = []
        self.passwords_supplied = 0
        self.dynamic_lag = dynamic_lag
        self._lag: dict[str, int] = {}
        self._rmau: list[str] = []
        self._ids = 0
        for upn, licensed in ((admin_upn, True), (breakglass_upn, False)):
            rec = self._new_user(
                upn, {"displayName": upn.split("@")[0], "userType": "Member", "usageLocation": "DE"}
            )
            if licensed:
                rec.license_sku_ids.append(self.sku.id)

    # --- helpers
    def _id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}-{self._ids:04d}"

    def _new_user(self, upn: str, attrs: dict) -> UserRec:
        rec = UserRec(id=self._id("u"), upn=upn)
        self._apply(rec, attrs)
        self.users[_k(upn)] = rec
        return rec

    @staticmethod
    def _apply(rec: UserRec, attrs: dict) -> None:
        for graph_name, value in attrs.items():
            if graph_name not in _ATTR_FIELDS:
                raise AdminGraphError(f"unknown user attribute {graph_name}")
            setattr(rec, _ATTR_FIELDS[graph_name], value)

    def _user(self, upn: str) -> UserRec:
        rec = self.users.get(_k(upn))
        if rec is None:
            raise AdminGraphError(f"404 user {upn} not found")
        return rec

    def _group(self, name: str) -> dict:
        g = self.groups.get(name)
        if g is None:
            raise AdminGraphError(f"404 group {name} not found")
        return g

    def _write(self, op: str, key: str, **detail) -> None:
        self.writes.append((op, key, detail))

    def set_rmau(self, members: list[str]) -> None:
        self._rmau = list(members)

    # --- tenant
    def organization_domains(self) -> list[str]:
        return [self.domain]

    def list_skus(self) -> list[Sku]:
        consumed = sum(self.sku.id in u.license_sku_ids for u in self.users.values())
        return [Sku(self.sku.id, self.sku.part_number, self.sku.enabled, consumed)]

    def rmau_members(self) -> list[str]:
        return list(self._rmau)

    # --- users
    def get_user(self, upn: str) -> UserRec | None:
        return self.users.get(_k(upn))

    def list_users(self) -> list[UserRec]:
        return list(self.users.values())

    def create_user(self, upn: str, attrs: dict, password: Secret) -> str:
        if _k(upn) in self.users:
            raise AdminGraphError(f"400 user {upn} already exists")
        assert isinstance(password, Secret), "the initial password must travel as a Secret"
        self.passwords_supplied += 1
        rec = self._new_user(upn, attrs)
        self._write("create_user", upn, attrs=dict(attrs))
        return rec.id

    def update_user(self, upn: str, attrs: dict) -> None:
        self._apply(self._user(upn), attrs)
        self._write("update_user", upn, attrs=dict(attrs))

    def set_manager(self, upn: str, manager_upn: str) -> None:
        self._user(manager_upn)
        self._user(upn).manager_upn = manager_upn
        self._write("set_manager", upn, manager=manager_upn)

    def assign_license(self, upn: str, sku_id: str) -> None:
        rec = self._user(upn)
        if not rec.usage_location:
            raise AdminGraphError("400 license assignment needs usageLocation")
        if sku_id != self.sku.id:
            raise AdminGraphError(f"400 unknown sku {sku_id}")
        if sku_id not in rec.license_sku_ids:
            rec.license_sku_ids.append(sku_id)
        self._write("assign_license", upn, sku=sku_id)

    def remove_license(self, upn: str, sku_id: str) -> None:
        rec = self._user(upn)
        if sku_id in rec.license_sku_ids:
            rec.license_sku_ids.remove(sku_id)
        self._write("remove_license", upn, sku=sku_id)

    def delete_user(self, upn: str) -> None:
        self._user(upn)
        del self.users[_k(upn)]
        for g in self.groups.values():
            g["members"].discard(_k(upn))
            g["owners"].discard(_k(upn))
        for u in self.users.values():
            if u.manager_upn and _k(u.manager_upn) == _k(upn):
                u.manager_upn = None
        self._write("delete_user", upn)

    # --- groups
    def list_groups(self) -> list[GroupRec]:
        return [GroupRec(g["id"], name, g["kind"], g["rule"]) for name, g in self.groups.items()]

    def create_group(self, name: str, kind: str, rule: str | None, owners: list[str]) -> str:
        if name in self.groups:
            raise AdminGraphError(f"400 group {name} already exists")
        if kind not in ("dynamic", "assigned", "role_assignable"):
            raise AdminGraphError(f"400 unknown group kind {kind}")
        if (kind == "dynamic") != bool(rule):
            raise AdminGraphError("400 only dynamic groups carry a rule")
        for o in owners:
            self._user(o)
        self.groups[name] = {
            "id": self._id("g"), "kind": kind, "rule": rule,
            "owners": {_k(o) for o in owners}, "members": set(), "roles": set(),
        }  # fmt: skip
        self._lag[name] = self.dynamic_lag
        self._write("create_group", name, kind=kind, rule=rule, owners=sorted(owners))
        return self.groups[name]["id"]

    def update_group_rule(self, name: str, rule: str) -> None:
        g = self._group(name)
        if g["kind"] != "dynamic":
            raise AdminGraphError("400 only dynamic groups carry a rule")
        g["rule"] = rule
        self._write("update_group_rule", name, rule=rule)

    def delete_group(self, name: str) -> None:
        self._group(name)
        del self.groups[name]
        self._write("delete_group", name)

    def _display(self, key: str) -> str:
        return self.users[key].upn if key in self.users else key

    def group_members(self, name: str) -> list[str]:
        g = self._group(name)
        if g["kind"] == "dynamic":
            if self._lag.get(name, 0) > 0:
                self._lag[name] -= 1  # the service is still evaluating the rule
                return []
            return [
                u.upn
                for u in self.users.values()
                if rule_matches(
                    g["rule"],
                    {
                        "userType": u.user_type,
                        "department": u.department,
                        "employeeType": u.employee_type,
                    },
                )
            ]
        return sorted(self._display(m) for m in g["members"])

    def group_owners(self, name: str) -> list[str]:
        return sorted(self._display(o) for o in self._group(name)["owners"])

    def group_roles(self, name: str) -> list[str]:
        return sorted(self._group(name)["roles"])

    def add_member(self, name: str, upn: str) -> None:
        g = self._group(name)
        if g["kind"] == "dynamic":
            raise AdminGraphError("400 dynamic groups take no manual members")
        self._user(upn)
        g["members"].add(_k(upn))
        self._write("add_member", name, member=upn)

    def remove_member(self, name: str, upn: str) -> None:
        g = self._group(name)
        if g["kind"] == "dynamic":
            raise AdminGraphError("400 dynamic groups take no manual members")
        g["members"].discard(_k(upn))
        self._write("remove_member", name, member=upn)

    def add_owner(self, name: str, upn: str) -> None:
        self._user(upn)
        self._group(name)["owners"].add(_k(upn))
        self._write("add_owner", name, owner=upn)

    def remove_owner(self, name: str, upn: str) -> None:
        self._group(name)["owners"].discard(_k(upn))
        self._write("remove_owner", name, owner=upn)

    def assign_role(self, name: str, role: str) -> None:
        g = self._group(name)
        if g["kind"] != "role_assignable":
            raise AdminGraphError("400 only role assignable groups can hold a role")
        g["roles"].add(role)
        self._write("assign_role", name, role=role)

    def remove_role(self, name: str, role: str) -> None:
        self._group(name)["roles"].discard(role)
        self._write("remove_role", name, role=role)
