"""Seed org data for the fake Graph, taken from the repo's own sources.

Users come from the table in `seed/seed-org.md`, groups and memberships from the constants in
`data/validate.py`. Task 0.2 replaces both with `seed/tenant.yaml`.
"""

import importlib.util
import re
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
DOMAIN = "<tenant>.onmicrosoft.com"
ADMIN_UPN = f"admin@{DOMAIN}"

_ROW = re.compile(
    r"^\|\s*\d+\s*\|\s*([a-z.]+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|$"
)


def _load_validate():
    spec = importlib.util.spec_from_file_location(
        "iam_dataset_validate", ROOT / "data" / "validate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_titles() -> dict[str, tuple[str, str]]:
    titles = {}
    for line in (ROOT / "seed" / "seed-org.md").read_text(encoding="utf-8").splitlines():
        m = _ROW.match(line.strip())
        if m:
            prefix, name, title, _dept, _mgr = m.groups()
            titles[prefix] = (name, title)
    return titles


def build_org() -> dict:
    """Return users, groups and the protected sets as plain data."""
    v = _load_validate()
    names = _seed_titles()
    upn = lambda prefix: f"{prefix}@{DOMAIN}"  # noqa: E731

    users = {}
    for prefix, (dept, mgr) in v.USERS.items():
        name, title = names[prefix]
        users[upn(prefix).lower()] = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, upn(prefix))),
            "displayName": name,
            "userPrincipalName": upn(prefix),
            "department": dept,
            "jobTitle": title,
            "accountEnabled": True,
            "manager": {"userPrincipalName": upn(mgr)} if mgr else None,
            # Fields that must never reach the model; the minimiser has to drop them.
            "mobilePhone": "+49 30 5550 0000",
            "businessPhones": ["+49 30 5550 0001"],
            "signInActivity": {"lastSignInIpAddress": "203.0.113.7"},
        }
    users[ADMIN_UPN.lower()] = {
        "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, ADMIN_UPN)),
        "displayName": "Sandbox Admin",
        "userPrincipalName": ADMIN_UPN,
        "department": "IT",
        "jobTitle": "Break glass",
        "accountEnabled": True,
        "manager": None,
        "mobilePhone": None,
        "businessPhones": [],
        "signInActivity": {},
    }

    groups = {}
    for name, (klass, owner, members) in v.GROUPS.items():
        groups[name] = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, name)),
            "displayName": name,
            "class": klass,
            "owners": [upn(owner)] if owner else [],
            "members": [upn(m) for m in members],
        }

    rmau_users = {upn(p).lower() for p in v.PRIVILEGED} | {ADMIN_UPN.lower()}
    return {"users": users, "groups": groups, "rmau_users": rmau_users}
