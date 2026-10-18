"""In-memory fake Graph for unit tests and plan mode development.

EMULATED, NOT PROOF. The platform (L1) and token (L2) refusals in this file are a model of what
`docs/PERMISSIONS.md` section 5 predicts. They exist so gateway tests can show that a call which
gets past the gateway is still refused, but they prove nothing about Entra. Task 0.7 (bypass
test against the real tenant) is the only evidence for the L1 and L2 columns.
"""

import itertools
import logging

from . import fake_org
from .base import GraphClient, GraphForbidden, GraphNotFound, Secret

log = logging.getLogger("iamagents.graph.fake")

# EMULATED L2: which operations each identity's token allows (PERMISSIONS.md section 4).
EMULATED_L2: dict[str, set[str]] = {
    "ds": {"read", "reset_password"},
    "am": {"read"},
    "ig": {"read"},
    "pam": set(),
}


class FakeDirectory:
    """Shared state behind all per-identity fake clients."""

    def __init__(self, org: dict | None = None):
        org = org or fake_org.build_org()
        self.users: dict[str, dict] = org["users"]
        self.groups: dict[str, dict] = org["groups"]
        self.rmau_users: set[str] = set(org["rmau_users"])
        self.active_roles: dict[str, set[str]] = {}
        self.calls: list[tuple[str, str, str]] = []  # (identity, method, target)
        self.password_resets: list[tuple[str, str]] = []  # (identity, upn); never the password
        self.fail_lookups = False  # simulate Graph being unreachable for protection checks
        self._ids = itertools.count(1)

    def activate_role(self, upn: str, role: str = "User Administrator") -> None:
        """A PIM activation: the user is not in the RMAU but holds an active role."""
        self.active_roles.setdefault(upn.lower(), set()).add(role)

    def next_request_id(self) -> str:
        return f"fake-req-{next(self._ids):04d}"


class FakeGraphClient:
    def __init__(self, directory: FakeDirectory, identity: str):
        self.dir = directory
        self.identity = identity

    def _record(self, method: str, target: str) -> None:
        self.dir.calls.append((self.identity, method, target))
        log.debug("graph[%s] %s %s", self.identity, method, target)

    def _require_l2(self, op: str) -> None:
        if op not in EMULATED_L2.get(self.identity, set()):
            raise GraphForbidden(f"[emulated L2] {self.identity} lacks permission for {op}")

    def _user(self, upn: str) -> dict:
        user = self.dir.users.get(upn.lower())
        if user is None:
            raise GraphNotFound(f"user {upn} not found")
        return user

    def get_user(self, upn: str) -> dict:
        self._record("GET user", upn)
        self._require_l2("read")
        return dict(self._user(upn))

    def get_group(self, name: str) -> dict:
        self._record("GET group", name)
        self._require_l2("read")
        group = self.dir.groups.get(name)
        if group is None:
            raise GraphNotFound(f"group {name} not found")
        out = dict(group)
        out["memberCount"] = len(group["members"])
        out.pop("members")
        return out

    def list_group_members(self, name: str) -> list[dict]:
        self._record("GET group members", name)
        self._require_l2("read")
        group = self.dir.groups.get(name)
        if group is None:
            raise GraphNotFound(f"group {name} not found")
        return [dict(self._user(m)) for m in group["members"]]

    def reset_password(self, upn: str, password: Secret) -> str:
        self._record("PATCH user passwordProfile", upn)
        # A real client may log its request body at debug level. The Secret wrapper keeps the
        # value out of that line; the gateway test relies on it.
        log.debug("graph[%s] body passwordProfile.password=%r", self.identity, password)
        self._require_l2("reset_password")
        self._user(upn)
        # EMULATED L1: RMAU members and users with an active role are out of reach of the
        # AU scoped User Administrator role.
        if self.is_rmau_protected(upn) or self.has_active_directory_role(upn):
            raise GraphForbidden(f"[emulated L1] {upn} is protected from this identity")
        password.reveal()  # the real client sends this to Graph
        self.dir.password_resets.append((self.identity, upn))
        return self.dir.next_request_id()

    def is_rmau_protected(self, upn: str) -> bool:
        if self.dir.fail_lookups:
            raise GraphForbidden("[emulated] lookup failed")
        return upn.lower() in self.dir.rmau_users

    def has_active_directory_role(self, upn: str) -> bool:
        if self.dir.fail_lookups:
            raise GraphForbidden("[emulated] lookup failed")
        return bool(self.dir.active_roles.get(upn.lower()))


class FakeGraphFactory:
    def __init__(self, directory: FakeDirectory | None = None, domain: str | None = None):
        self.directory = directory or FakeDirectory(fake_org.build_org(domain))

    def for_agent(self, agent: str) -> GraphClient:
        return FakeGraphClient(self.directory, agent)
