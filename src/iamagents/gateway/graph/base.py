"""Graph client interface used by the gateway.

The gateway only talks to Graph through `GraphClient`. The fake implementation lives in
`fake.py`; the real `msgraph-sdk` implementation arrives with the tenant tasks.
"""

from typing import Protocol


class GraphError(Exception):
    """Any failure talking to Graph. Messages must never contain secrets."""


class GraphForbidden(GraphError):
    """Graph refused the call (HTTP 403): a platform or token level stop."""


class GraphNotFound(GraphError):
    """The object does not exist (HTTP 404)."""


class Secret:
    """Wraps a secret so it cannot reach a log line or a repr by accident.

    `str()` and `repr()` are redacted. Only `reveal()` returns the value, and only the code
    that must hand it to Graph should call it.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(<redacted>)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "<redacted>"

    def __reduce__(self):
        raise TypeError("Secret cannot be serialised")


class GraphClient(Protocol):
    """One client per agent identity. Methods return plain dicts shaped like Graph objects."""

    def get_user(self, upn: str) -> dict: ...

    def get_group(self, name: str) -> dict: ...

    def list_group_members(self, name: str) -> list[dict]: ...

    def reset_password(self, upn: str, password: Secret) -> str:
        """Set a temporary password that must be changed at next sign in. Returns a request id."""
        ...

    def is_rmau_protected(self, upn: str) -> bool:
        """True if the user is a member of RMAU-Protected."""
        ...

    def has_active_directory_role(self, upn: str) -> bool:
        """True if the user holds any active directory role, including a PIM activation."""
        ...


class GraphClientFactory(Protocol):
    def for_agent(self, agent: str) -> GraphClient: ...
