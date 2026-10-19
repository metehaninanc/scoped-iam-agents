"""Seed org data for the fake Graph, built from `seed/tenant.yaml`.

`tenant.yaml` is the single source (generated from `seed/seed-org.md`). The fake adds fields a
real Graph object has and the minimiser must drop (phone numbers, sign-in IP addresses), so the
tests can show they never reach the model.
"""

import uuid
from pathlib import Path

from ...seedconfig import Tenant, group_members, load_tenant, resolve_accounts

ROOT = Path(__file__).resolve().parents[4]
DOMAIN = "<tenant>.onmicrosoft.com"


def build_org(domain: str | None = None, tenant: Tenant | None = None) -> dict:
    """Users, groups and the protected sets as plain data. Admin account addresses are made up
    from the domain (`admin@`, `breakglass@`); the real ones are only read at seed time."""
    domain = domain or DOMAIN
    tenant = tenant or load_tenant()
    upn = lambda prefix: f"{prefix}@{domain}"  # noqa: E731

    users = {}
    for u in tenant.users:
        address = upn(u.upn_prefix)
        users[address.lower()] = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, address)),
            "displayName": u.display_name,
            "userPrincipalName": address,
            "department": u.department,
            "jobTitle": u.job_title,
            "employeeType": u.employee_type,
            "userType": u.user_type,
            "accountEnabled": True,
            "manager": {"userPrincipalName": upn(u.manager)} if u.manager else None,
            # Fields that must never reach the model; the minimiser has to drop them.
            "mobilePhone": "+49 30 5550 0000",
            "businessPhones": ["+49 30 5550 0001"],
            "signInActivity": {"lastSignInIpAddress": "203.0.113.7"},
        }
    accounts = resolve_accounts(tenant, None, domain)
    for a in tenant.accounts:
        address = accounts[a.key]
        users[address.lower()] = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, address)),
            "displayName": "Emergency access" if a.key == "breakglass" else "Admin",
            "userPrincipalName": address,
            "department": None,
            "jobTitle": None,
            "employeeType": None,
            "userType": "Member",
            "accountEnabled": True,
            "manager": None,
            "mobilePhone": None,
            "businessPhones": [],
            "signInActivity": {},
        }

    groups = {}
    for g in tenant.groups:
        groups[g.name] = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, g.name)),
            "displayName": g.name,
            "class": g.klass,
            "owners": [upn(g.owner)] if g.owner else [],
            "members": [upn(m) for m in group_members(tenant, g)],
        }

    privileged = {u.upn_prefix for u in tenant.users if u.employee_type == "Privileged"}
    rmau_users = {upn(p).lower() for p in privileged} | {
        accounts[a.key].lower() for a in tenant.accounts if a.in_rmau
    }
    return {"users": users, "groups": groups, "rmau_users": rmau_users}
