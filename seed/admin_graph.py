"""What the seed and reset scripts need from Microsoft Graph, as an interface.

Two implementations satisfy it: `seed.fake_admin.FakeAdminGraph` (in memory, for unit tests) and
`seed.rest_admin.RestAdminGraph` (the real tenant, signed in as a human admin through the device
code flow). A shared contract test runs the same scenarios against both, so the engine is tested
offline before it ever touches a tenant.

Addresses are full user principal names. Groups are addressed by display name (unique in the seed).
Nothing in this interface returns or logs a secret: `create_user` takes the initial password as a
`Secret`, which redacts itself.
"""

from dataclasses import dataclass, field
from typing import Protocol

from iamagents.gateway.graph.base import Secret


@dataclass
class Sku:
    id: str
    part_number: str
    enabled: int = 0
    consumed: int = 0


@dataclass
class UserRec:
    id: str
    upn: str
    display_name: str = ""
    given_name: str = ""
    surname: str = ""
    job_title: str = ""
    department: str = ""
    employee_type: str | None = None
    user_type: str = "Member"
    usage_location: str | None = None
    account_enabled: bool = True
    manager_upn: str | None = None
    license_sku_ids: list[str] = field(default_factory=list)


@dataclass
class GroupRec:
    id: str
    name: str
    kind: str  # dynamic | assigned | role_assignable
    rule: str | None = None


class AdminGraph(Protocol):
    # tenant
    def organization_domains(self) -> list[str]: ...
    def list_skus(self) -> list[Sku]: ...
    def rmau_members(self) -> list[str]:
        """Users (as UPN) and groups (as display name) in RMAU-Protected; [] if it does not exist."""
        ...

    # users
    def get_user(self, upn: str) -> UserRec | None: ...
    def list_users(self) -> list[UserRec]: ...
    def create_user(self, upn: str, attrs: dict, password: Secret) -> str: ...
    def update_user(self, upn: str, attrs: dict) -> None: ...
    def set_manager(self, upn: str, manager_upn: str) -> None: ...
    def assign_license(self, upn: str, sku_id: str) -> None: ...
    def remove_license(self, upn: str, sku_id: str) -> None: ...
    def delete_user(self, upn: str) -> None: ...

    # groups
    def list_groups(self) -> list[GroupRec]: ...
    def create_group(self, name: str, kind: str, rule: str | None, owners: list[str]) -> str: ...
    def update_group_rule(self, name: str, rule: str) -> None: ...
    def delete_group(self, name: str) -> None: ...
    def group_members(self, name: str) -> list[str]: ...
    def group_owners(self, name: str) -> list[str]: ...
    def group_roles(self, name: str) -> list[str]: ...
    def add_member(self, name: str, upn: str) -> None: ...
    def remove_member(self, name: str, upn: str) -> None: ...
    def add_owner(self, name: str, upn: str) -> None: ...
    def remove_owner(self, name: str, upn: str) -> None: ...
    def assign_role(self, name: str, role: str) -> None: ...
    def remove_role(self, name: str, role: str) -> None: ...


class AdminGraphError(Exception):
    """A Graph call failed. Messages carry the status and error code, never a request body."""
