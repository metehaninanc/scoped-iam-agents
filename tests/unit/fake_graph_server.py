"""A small Microsoft Graph REST emulator for tests, built on `FakeAdminGraph`.

It turns the HTTP requests of `RestAdminGraph` into calls on the in-memory fake and answers in
the shapes Graph uses (paging with `@odata.nextLink`, `$ref` links, error bodies). It can also
throttle and delay visibility. It embodies the project's understanding of Graph; the first live
plan is what checks that understanding against the real service.
"""

import json
import re
from urllib.parse import parse_qs, unquote

import httpx

from seed.admin_graph import AdminGraphError
from seed.fake_admin import FakeAdminGraph

ROLE_IDS = {
    "User Administrator": "rd-user-admin",
    "Helpdesk Administrator": "rd-helpdesk-admin",
    "Security Reader": "rd-security-reader",
    "Global Administrator": "rd-global-admin",
}
BASE = "https://graph.microsoft.com/v1.0"


def _json(status: int, body: dict | None = None, headers: dict | None = None) -> httpx.Response:
    return (
        httpx.Response(status, json=body, headers=headers)
        if body is not None
        else httpx.Response(status, headers=headers)
    )


def _err(status: int, code: str, message: str = "") -> httpx.Response:
    return _json(status, {"error": {"code": code, "message": message}})


class GraphEmulator:
    def __init__(
        self, fake: FakeAdminGraph, page_size: int = 1000, throttle_first: int = 0, lag_404: int = 0
    ):
        self.fake = fake
        self.page_size = page_size
        self.throttle = throttle_first
        self.lag_404 = lag_404
        self._new_users: dict[str, int] = {}
        self.requests: list[dict] = []
        self.transport = httpx.MockTransport(self.handle)

    # --- lookups
    def _user_by_id(self, uid: str):
        return next((u for u in self.fake.users.values() if u.id == uid), None)

    def _group_by_id(self, gid: str):
        return next(((n, g) for n, g in self.fake.groups.items() if g["id"] == gid), (None, None))

    def _user_json(self, u, full: bool = True) -> dict:
        d = {"id": u.id, "userPrincipalName": u.upn}
        if full:
            d |= {
                "displayName": u.display_name, "givenName": u.given_name, "surname": u.surname,
                "jobTitle": u.job_title or None, "department": u.department or None,
                "employeeType": u.employee_type, "userType": u.user_type, "usageLocation": u.usage_location,
                "accountEnabled": u.account_enabled,
                "assignedLicenses": [{"skuId": s, "disabledPlans": []} for s in u.license_sku_ids],
            }  # fmt: skip
            if u.manager_upn:
                d["manager"] = {"userPrincipalName": u.manager_upn}
        return d

    def _page(self, rows: list[dict], url: httpx.URL) -> httpx.Response:
        q = parse_qs(url.query.decode())
        start = int(q.get("$skiptoken", ["0"])[0])
        chunk = rows[start : start + self.page_size]
        body: dict = {"value": chunk}
        if start + self.page_size < len(rows):
            nxt = {k: v[0] for k, v in q.items() if k != "$skiptoken"} | {
                "$skiptoken": str(start + self.page_size)
            }
            query = "&".join(f"{k}={v}" for k, v in nxt.items())
            body["@odata.nextLink"] = f"{BASE}{url.path.removeprefix('/v1.0')}?{query}"
        return _json(200, body)

    # --- dispatch
    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        rec = {
            "method": request.method,
            "url": str(request.url),
            "body": body,
            "auth": request.headers.get("authorization"),
        }
        self.requests.append(rec)
        if not (rec["auth"] or "").startswith("Bearer ") or len(rec["auth"]) < 12:
            return _err(401, "InvalidAuthenticationToken")
        if self.throttle > 0:
            self.throttle -= 1
            return _json(
                429,
                {"error": {"code": "TooManyRequests", "message": "slow down"}},
                {"Retry-After": "0"},
            )
        path = unquote(request.url.path).removeprefix("/v1.0")
        try:
            return self._route(request, path, body)
        except AdminGraphError as e:
            status = int(str(e)[:3]) if str(e)[:3].isdigit() else 400
            return _err(
                status,
                "Request_BadRequest" if status == 400 else "Request_ResourceNotFound",
                str(e)[4:],
            )

    def _route(self, request: httpx.Request, path: str, body: dict | None) -> httpx.Response:
        m, f, url = request.method, self.fake, request.url
        q = parse_qs(url.query.decode())
        if (m, path) == ("GET", "/organization"):
            return _json(200, {"value": [{"verifiedDomains": [{"name": f.domain}]}]})
        if (m, path) == ("GET", "/subscribedSkus"):
            return _json(
                200,
                {
                    "value": [
                        {
                            "skuId": s.id,
                            "skuPartNumber": s.part_number,
                            "consumedUnits": s.consumed,
                            "prepaidUnits": {"enabled": s.enabled},
                        }
                        for s in f.list_skus()
                    ]
                },
            )

        # users
        if (m, path) == ("GET", "/users"):
            return self._page([self._user_json(u) for u in f.list_users()], url)
        if (m, path) == ("POST", "/users"):
            pw = (body or {}).get("passwordProfile", {}).get("password")
            if not pw:
                return _err(400, "Request_BadRequest", "password required")
            attrs = {
                k: v
                for k, v in body.items()
                if k not in ("passwordProfile", "userPrincipalName", "mailNickname")
            }
            from iamagents.gateway.graph.base import Secret

            f.create_user(body["userPrincipalName"], attrs, Secret(pw))
            self._new_users[body["userPrincipalName"].lower()] = self.lag_404
            return _json(201, self._user_json(f.get_user(body["userPrincipalName"])))
        mu = re.fullmatch(r"/users/([^/]+)(/.*)?", path)
        if mu:
            upn, rest = mu.group(1), mu.group(2) or ""
            if (
                self._new_users.get(upn.lower(), 0) > 0
            ):  # a fresh user is not visible everywhere yet
                self._new_users[upn.lower()] -= 1
                return _err(404, "Request_ResourceNotFound")
            user = f.get_user(upn)
            if user is None and not (m == "POST" and rest == ""):
                return _err(404, "Request_ResourceNotFound", f"user {upn} not found")
            if m == "GET" and rest == "":
                return _json(
                    200,
                    self._user_json(
                        user, full="$expand" in q or "assignedLicenses" in q.get("$select", [""])[0]
                    ),
                )
            if m == "PATCH" and rest == "":
                f.update_user(user.upn, body)
                return _json(204)
            if m == "DELETE" and rest == "":
                f.delete_user(user.upn)
                return _json(204)
            if m == "PUT" and rest == "/manager/$ref":
                manager = self._user_by_id(body["@odata.id"].rsplit("/", 1)[1])
                f.set_manager(user.upn, manager.upn)
                return _json(204)
            if m == "POST" and rest == "/assignLicense":
                for lic in body["addLicenses"]:
                    f.assign_license(user.upn, lic["skuId"])
                for sku in body["removeLicenses"]:
                    f.remove_license(user.upn, sku)
                return _json(200, self._user_json(f.get_user(user.upn)))

        # groups
        if (m, path) == ("GET", "/groups"):
            return self._page(
                [
                    {
                        "id": g.id,
                        "displayName": g.name,
                        "groupTypes": ["DynamicMembership"] if g.kind == "dynamic" else [],
                        "membershipRule": g.rule,
                        "isAssignableToRole": g.kind == "role_assignable",
                    }
                    for g in f.list_groups()
                ],
                url,
            )
        if (m, path) == ("POST", "/groups"):
            kind = (
                "dynamic"
                if "DynamicMembership" in body.get("groupTypes", [])
                else ("role_assignable" if body.get("isAssignableToRole") else "assigned")
            )
            owners = [
                self._user_by_id(o.rsplit("/", 1)[1]).upn for o in body.get("owners@odata.bind", [])
            ]
            gid = f.create_group(body["displayName"], kind, body.get("membershipRule"), owners)
            return _json(201, {"id": gid, "displayName": body["displayName"]})
        mg = re.fullmatch(r"/groups/([^/]+)(/.*)?", path)
        if mg:
            name, group = self._group_by_id(mg.group(1))
            rest = mg.group(2) or ""
            if group is None:
                return _err(404, "Request_ResourceNotFound")
            if m == "PATCH" and rest == "":
                f.update_group_rule(name, body["membershipRule"])
                return _json(204)
            if m == "DELETE" and rest == "":
                f.delete_group(name)
                return _json(204)
            rel = re.fullmatch(r"/(members|owners)/microsoft\.graph\.user", rest)
            if m == "GET" and rel:
                upns = f.group_members(name) if rel.group(1) == "members" else f.group_owners(name)
                return self._page([{"userPrincipalName": u} for u in upns], url)
            link = re.fullmatch(r"/(members|owners)/\$ref", rest)
            if m == "POST" and link:
                target = self._user_by_id(body["@odata.id"].rsplit("/", 1)[1])
                current = (
                    f.group_members(name) if link.group(1) == "members" else f.group_owners(name)
                )
                if target.upn in current:
                    return _err(
                        400,
                        "Request_BadRequest",
                        "One or more added object references already exist",
                    )
                (f.add_member if link.group(1) == "members" else f.add_owner)(name, target.upn)
                return _json(204)
            unlink = re.fullmatch(r"/(members|owners)/([^/]+)/\$ref", rest)
            if m == "DELETE" and unlink:
                target = self._user_by_id(unlink.group(2))
                (f.remove_member if unlink.group(1) == "members" else f.remove_owner)(
                    name, target.upn
                )
                return _json(204)

        # directory roles
        if (m, path) == ("GET", "/roleManagement/directory/roleDefinitions"):
            wanted = (
                re.search(r"displayName eq '(.*)'", q["$filter"][0]).group(1).replace("''", "'")
            )
            return _json(
                200,
                {
                    "value": [{"id": ROLE_IDS[wanted], "displayName": wanted}]
                    if wanted in ROLE_IDS
                    else []
                },
            )
        if (m, path) == ("GET", "/roleManagement/directory/roleAssignments"):
            gid = re.search(r"principalId eq '(.*)'", q["$filter"][0]).group(1)
            name, _ = self._group_by_id(gid)
            roles = f.group_roles(name) if name else []
            return _json(
                200,
                {
                    "value": [
                        {
                            "id": f"ra:{gid}:{r}",
                            "principalId": gid,
                            "roleDefinition": {"displayName": r},
                        }
                        for r in roles
                    ]
                },
            )
        if (m, path) == ("POST", "/roleManagement/directory/roleAssignments"):
            name, _ = self._group_by_id(body["principalId"])
            role = next(r for r, i in ROLE_IDS.items() if i == body["roleDefinitionId"])
            f.assign_role(name, role)
            return _json(201, {"id": f"ra:{body['principalId']}:{role}"})
        ma = re.fullmatch(r"/roleManagement/directory/roleAssignments/ra:([^:]+):(.+)", path)
        if m == "DELETE" and ma:
            name, _ = self._group_by_id(ma.group(1))
            f.remove_role(name, ma.group(2))
            return _json(204)

        # administrative units
        if (m, path) == ("GET", "/directory/administrativeUnits"):
            return _json(
                200,
                {
                    "value": [{"id": "au-rmau", "displayName": "RMAU-Protected"}]
                    if f.rmau_members()
                    else []
                },
            )
        if (m, path) == ("GET", "/directory/administrativeUnits/au-rmau/members"):
            rows = [
                {"userPrincipalName": x} if "@" in x else {"displayName": x}
                for x in f.rmau_members()
            ]
            return _json(200, {"value": rows})
        return _err(404, "Request_ResourceNotFound", f"no route for {m} {path}")
