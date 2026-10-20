"""`AdminGraph` over the Microsoft Graph REST API, for a human admin's delegated token.

Plain `httpx` calls instead of `msgraph-sdk`: the seed scripts are small, every request body is
explicit and unit-testable with a mock transport, and nothing is hidden in a generated client.
(The gateway's own Graph client, for the app identities, is a separate piece.)

Rules for this module:

* A request body is never logged, never put in an exception and never printed. The only body that
  carries a secret is `POST /users` (the initial password).
* The token comes from a callable that is asked for it on every request, so a refresh is picked up.
* Throttling (429, 503, 504) is retried with `Retry-After`; a 404 right after a create is retried a
  few times because Graph is eventually consistent.
"""

import logging
import re
import time
from collections.abc import Callable
from urllib.parse import quote

import httpx

from iamagents.gateway.graph.base import Secret

from .admin_graph import AdminGraphError, GroupRec, Sku, UserRec

GRAPH = "https://graph.microsoft.com/v1.0"
log = logging.getLogger("seed.rest")
_USER_SELECT = (
    "id,userPrincipalName,displayName,givenName,surname,jobTitle,department,employeeType,"
    "userType,usageLocation,accountEnabled,assignedLicenses"
)
_USER_QUERY = {"$select": _USER_SELECT, "$expand": "manager($select=userPrincipalName)"}
_RETRY_STATUS = (429, 503, 504)


class GraphRequestError(AdminGraphError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code = status, code


def _quote_odata(value: str) -> str:
    return value.replace("'", "''")


class RestAdminGraph:
    def __init__(
        self,
        token: Callable[[], str],
        client: httpx.Client | None = None,
        base: str = GRAPH,
        sleep: Callable[[float], None] = time.sleep,
        retries: int = 5,
    ):
        self._token = token
        self._client = client or httpx.Client(timeout=60)
        self._base = base.rstrip("/")
        self._sleep = sleep
        self._retries = retries
        self._user_ids: dict[str, str] = {}
        self._group_ids: dict[str, str] = {}

    # --- transport
    def _request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None,
                 ok: tuple[int, ...] = (200, 201, 202, 204), missing_ok: bool = False,
                 retry_missing: bool = False) -> dict | None:  # fmt: skip
        url = path if path.startswith("http") else f"{self._base}{path}"
        attempt = 0
        while True:
            headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
            resp = self._client.request(method, url, params=params, json=json, headers=headers)
            log.debug(
                "graph %s %s -> %s",
                method,
                re.sub(r"[^/]*@[^/?]*", "<upn>", url.split("?")[0]),
                resp.status_code,
            )
            if resp.status_code in ok:
                return resp.json() if resp.content else None
            if resp.status_code in _RETRY_STATUS and attempt < self._retries:
                wait = float(resp.headers.get("Retry-After", 2**attempt))
                attempt += 1
                self._sleep(wait)
                continue
            if resp.status_code == 404:
                if retry_missing and attempt < self._retries:
                    attempt += 1
                    self._sleep(attempt)  # the object was created a moment ago: let it replicate
                    continue
                if missing_ok:
                    return None
            raise self._error(resp)

    @staticmethod
    def _error(resp: httpx.Response) -> GraphRequestError:
        code, message = "error", ""
        try:
            err = resp.json().get("error", {})
            code, message = err.get("code", "error"), err.get("message", "")
        except ValueError:
            pass
        return GraphRequestError(
            resp.status_code, code, message[:300]
        )  # status, code, Graph's message; no body

    def _get_all(self, path: str, params: dict | None = None) -> list[dict]:
        out, url, query = [], path, dict(params or {})
        while url:
            page = self._request("GET", url, params=query) or {}
            out += page.get("value", [])
            url, query = page.get("@odata.nextLink"), None
        return out

    # --- identifiers
    def _uid(self, upn: str, refresh: bool = False) -> str:
        key = upn.lower()
        if refresh or key not in self._user_ids:
            data = self._request(
                "GET", f"/users/{quote(upn)}", params={"$select": "id"}, retry_missing=True
            )
            self._user_ids[key] = data["id"]
        return self._user_ids[key]

    def _gid(self, name: str) -> str:
        if name not in self._group_ids:
            self.list_groups()
        if name not in self._group_ids:
            raise GraphRequestError(404, "Request_ResourceNotFound", f"group {name} not found")
        return self._group_ids[name]

    # --- tenant
    def organization_domains(self) -> list[str]:
        org = (self._request("GET", "/organization") or {}).get("value", [])
        return [d["name"] for o in org for d in o.get("verifiedDomains", [])]

    def list_skus(self) -> list[Sku]:
        return [
            Sku(
                s["skuId"],
                s["skuPartNumber"],
                s.get("prepaidUnits", {}).get("enabled", 0),
                s.get("consumedUnits", 0),
            )
            for s in self._get_all("/subscribedSkus")
        ]

    def rmau_members(self) -> list[str]:
        found = self._get_all(
            "/directory/administrativeUnits", {"$filter": "displayName eq 'RMAU-Protected'"}
        )
        if not found:
            return []
        out = []
        for m in self._get_all(f"/directory/administrativeUnits/{found[0]['id']}/members"):
            out.append(m.get("userPrincipalName") or m.get("displayName") or "")
        return [x for x in out if x]

    # --- users
    @staticmethod
    def _user(d: dict) -> UserRec:
        return UserRec(
            id=d["id"], upn=d["userPrincipalName"], display_name=d.get("displayName") or "",
            given_name=d.get("givenName") or "", surname=d.get("surname") or "",
            job_title=d.get("jobTitle") or "", department=d.get("department") or "",
            employee_type=d.get("employeeType"), user_type=d.get("userType") or "Member",
            usage_location=d.get("usageLocation"), account_enabled=bool(d.get("accountEnabled", True)),
            manager_upn=(d.get("manager") or {}).get("userPrincipalName"),
            license_sku_ids=[lic["skuId"] for lic in d.get("assignedLicenses", [])],
        )  # fmt: skip

    def get_user(self, upn: str) -> UserRec | None:
        data = self._request("GET", f"/users/{quote(upn)}", params=_USER_QUERY, missing_ok=True)
        return self._user(data) if data else None

    def list_users(self) -> list[UserRec]:
        users = [self._user(d) for d in self._get_all("/users", {**_USER_QUERY, "$top": "999"})]
        self._user_ids.update({u.upn.lower(): u.id for u in users})
        return users

    def create_user(self, upn: str, attrs: dict, password: Secret) -> str:
        body = {
            **attrs,
            "userPrincipalName": upn,
            "mailNickname": upn.split("@")[0],
            "passwordProfile": {
                "forceChangePasswordNextSignIn": True,
                "password": password.reveal(),
            },
        }
        data = self._request("POST", "/users", json=body)
        self._user_ids[upn.lower()] = data["id"]
        return data["id"]

    def update_user(self, upn: str, attrs: dict) -> None:
        self._request("PATCH", f"/users/{quote(upn)}", json=attrs)

    def set_manager(self, upn: str, manager_upn: str) -> None:
        manager_id = self._uid(manager_upn)
        self._request(
            "PUT",
            f"/users/{quote(upn)}/manager/$ref",
            json={"@odata.id": f"{self._base}/users/{manager_id}"},
            retry_missing=True,
        )

    def assign_license(self, upn: str, sku_id: str) -> None:
        self._request(
            "POST",
            f"/users/{quote(upn)}/assignLicense",
            json={"addLicenses": [{"skuId": sku_id}], "removeLicenses": []},
            retry_missing=True,
        )

    def remove_license(self, upn: str, sku_id: str) -> None:
        self._request(
            "POST",
            f"/users/{quote(upn)}/assignLicense",
            json={"addLicenses": [], "removeLicenses": [sku_id]},
        )

    def delete_user(self, upn: str) -> None:
        self._request("DELETE", f"/users/{quote(upn)}")
        self._user_ids.pop(upn.lower(), None)

    # --- groups
    def list_groups(self) -> list[GroupRec]:
        out = []
        for g in self._get_all(
            "/groups",
            {
                "$select": "id,displayName,groupTypes,membershipRule,isAssignableToRole",
                "$top": "999",
            },
        ):
            kind = "dynamic" if "DynamicMembership" in g.get("groupTypes", []) else (
                "role_assignable" if g.get("isAssignableToRole") else "assigned")  # fmt: skip
            out.append(GroupRec(g["id"], g["displayName"], kind, g.get("membershipRule")))
            self._group_ids[g["displayName"]] = g["id"]
        return out

    def create_group(self, name: str, kind: str, rule: str | None, owners: list[str]) -> str:
        body: dict = {
            "displayName": name, "mailNickname": re.sub(r"[^A-Za-z0-9-]", "", name).lower(),
            "mailEnabled": False, "securityEnabled": True, "groupTypes": [],
        }  # fmt: skip
        if kind == "dynamic":
            body |= {
                "groupTypes": ["DynamicMembership"],
                "membershipRule": rule,
                "membershipRuleProcessingState": "On",
            }
        elif kind == "role_assignable":
            body |= {"isAssignableToRole": True, "visibility": "Private"}
        if owners:
            body["owners@odata.bind"] = [f"{self._base}/users/{self._uid(o)}" for o in owners]
        data = self._request("POST", "/groups", json=body)
        self._group_ids[name] = data["id"]
        return data["id"]

    def update_group_rule(self, name: str, rule: str) -> None:
        self._request("PATCH", f"/groups/{self._gid(name)}", json={"membershipRule": rule})

    def delete_group(self, name: str) -> None:
        self._request("DELETE", f"/groups/{self._gid(name)}")
        self._group_ids.pop(name, None)

    def _people(self, name: str, relation: str) -> list[str]:
        rows = self._get_all(
            f"/groups/{self._gid(name)}/{relation}/microsoft.graph.user",
            {"$select": "userPrincipalName", "$top": "999"},
        )
        return sorted(r["userPrincipalName"] for r in rows)

    def group_members(self, name: str) -> list[str]:
        return self._people(name, "members")

    def group_owners(self, name: str) -> list[str]:
        return self._people(name, "owners")

    def _link(self, name: str, relation: str, upn: str) -> None:
        body = {"@odata.id": f"{self._base}/directoryObjects/{self._uid(upn)}"}
        try:
            self._request("POST", f"/groups/{self._gid(name)}/{relation}/$ref", json=body)
        except GraphRequestError as e:
            if e.status != 400 or "already exist" not in str(e):
                raise  # an existing link is fine: the call is idempotent

    def _unlink(self, name: str, relation: str, upn: str) -> None:
        self._request(
            "DELETE", f"/groups/{self._gid(name)}/{relation}/{self._uid(upn)}/$ref", missing_ok=True
        )

    def add_member(self, name: str, upn: str) -> None:
        self._link(name, "members", upn)

    def remove_member(self, name: str, upn: str) -> None:
        self._unlink(name, "members", upn)

    def add_owner(self, name: str, upn: str) -> None:
        self._link(name, "owners", upn)

    def remove_owner(self, name: str, upn: str) -> None:
        self._unlink(name, "owners", upn)

    # --- directory roles held through a role assignable group
    def _role_assignments(self, name: str) -> list[dict]:
        return self._get_all(
            "/roleManagement/directory/roleAssignments",
            {
                "$filter": f"principalId eq '{self._gid(name)}'",
                "$expand": "roleDefinition($select=displayName)",
            },
        )

    def group_roles(self, name: str) -> list[str]:
        return sorted(a["roleDefinition"]["displayName"] for a in self._role_assignments(name))

    def assign_role(self, name: str, role: str) -> None:
        defs = self._get_all(
            "/roleManagement/directory/roleDefinitions",
            {"$filter": f"displayName eq '{_quote_odata(role)}'"},
        )
        if not defs:
            raise GraphRequestError(404, "RoleNotFound", f"directory role {role} not found")
        self._request(
            "POST",
            "/roleManagement/directory/roleAssignments",
            json={
                "principalId": self._gid(name),
                "roleDefinitionId": defs[0]["id"],
                "directoryScopeId": "/",
            },
        )

    def remove_role(self, name: str, role: str) -> None:
        for a in self._role_assignments(name):
            if a["roleDefinition"]["displayName"] == role:
                self._request("DELETE", f"/roleManagement/directory/roleAssignments/{a['id']}")
