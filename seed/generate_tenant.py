"""Generate `seed/tenant.yaml` from `seed/seed-org.md`.

    uv run python -m seed.generate_tenant           # write seed/tenant.yaml
    uv run python -m seed.generate_tenant --check   # fail if the committed file is out of date

The markdown is the source. The YAML is generated and carries a "do not edit" header; a unit test
runs the same check, so the two cannot drift apart. No tenant call, no secret, no real domain.
"""

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "seed" / "seed-org.md"
TARGET = ROOT / "seed" / "tenant.yaml"
HEADER = (
    "# Generated from seed/seed-org.md by seed/generate_tenant.py. Do not edit: change the\n"
    "# markdown and run `uv run python -m seed.generate_tenant`.\n"
)
DOMAIN = "<tenant>.onmicrosoft.com"

_USER_ROW = re.compile(
    r"^\|\s*\d+\s*\|\s*([a-z.]+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|$"
)
_PREHIRE = re.compile(r"([A-Z]\w+ [A-Z]\w+) \(([^,()]+), ([^,()]+), mgr (\w+)\)")
_GROUP_ROW = re.compile(r"^\|\s*((?:DG|PRIV|SG|GG)-[\w-]+)\s*\|")
_ACCOUNT_ROW = re.compile(
    r"^\|\s*([^|]+?)\s*\|\s*`\$\{([A-Z_]+)\}`\s*\|\s*([^|]+?)\s*\|\s*(yes|no)\s*\|\s*(yes|no)\s*\|$"
)


def cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def section(md: str, title: str) -> str:
    """Text of the `## title` section (up to the next `## `)."""
    m = re.search(rf"^## {re.escape(title)}.*?$", md, re.M)
    if not m:
        raise ValueError(f"section {title!r} not found in seed-org.md")
    rest = md[m.end() :]
    nxt = re.search(r"^## ", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def parse(md: str) -> dict:
    md = md.replace("\r\n", "\n")
    first_to_prefix, users = {}, []
    for line in md.splitlines():
        m = _USER_ROW.match(line.strip())
        if m:
            prefix, name, title, dept, mgr = m.groups()
            given, _, surname = name.partition(" ")
            first_to_prefix[given] = prefix
            users.append(
                {
                    "upn_prefix": prefix,
                    "display_name": name,
                    "given_name": given,
                    "surname": surname,
                    "job_title": title,
                    "department": dept,
                    "manager": None if mgr == "none" else mgr,
                    "employee_type": "Employee",
                }  # fmt: skip
            )
    if len(users) != 22:
        raise ValueError(f"expected 22 users in seed-org.md, found {len(users)}")
    for u in users:  # manager first names -> prefixes
        if u["manager"] is not None:
            u["manager"] = first_to_prefix[u["manager"]]
    all_prefixes = [u["upn_prefix"] for u in users]

    pre_hires = [
        {"upn_prefix": f"{n.split()[0].lower()}.{n.split()[1].lower()}", "display_name": n,
         "job_title": t, "department": d, "manager": first_to_prefix[m]}
        for n, t, d, m in _PREHIRE.findall(md)
    ]  # fmt: skip

    def prefixes(cell: str) -> list[str]:
        if cell.lower().startswith("all"):
            return list(all_prefixes)
        return [first_to_prefix[x.strip()] for x in cell.split(",") if x.strip()]

    groups, sub = [], None
    klass_of = {
        "Protected": "protected",
        "Open": "open",
        "Restricted": "restricted",
        "Optional": "open",
    }
    for line in section(md, "Groups (21)").splitlines():
        h = re.match(r"^### (\w+)", line)
        if h:
            sub = h.group(1)
            continue
        if not _GROUP_ROW.match(line):
            continue
        c = cells(line)
        g = {"name": c[0], "kind": "assigned", "class": klass_of[sub]}
        if sub == "Protected":  # Group | Type | Owner | Purpose
            owner, purpose = c[2], c[3]
            g["owner"] = None if owner == "none" else first_to_prefix[owner]
            if c[1] == "Dynamic":
                g["kind"] = "dynamic"
                g["rule"] = re.search(r"`([^`]+)`", purpose).group(1)
            else:
                role, member = re.match(r"(.+?) role\. Member: (\w+)", purpose).groups()
                g["kind"], g["role"], g["members"] = (
                    "role_assignable",
                    role,
                    [first_to_prefix[member]],
                )
        elif sub == "Optional":  # Group | Owner | Purpose
            g["owner"], g["members"] = first_to_prefix[c[1]], []
        else:  # Open: Group | Owner | Members; Restricted: Group | Owner | Members | Extra control
            g["owner"], g["members"] = first_to_prefix[c[1]], prefixes(c[2])
        groups.append(g)

    baselines = {
        "all": re.findall(
            r"SG-[\w-]+",
            re.search(r"on top of ([^|\n]+)", section(md, "Department baselines")).group(1),
        )
    }
    for line in section(md, "Department baselines").splitlines():
        if (
            line.startswith("|")
            and not line.startswith("| Department")
            and not line.startswith("|---")
        ):
            depts, names = cells(line)
            for d in [x.strip() for x in depts.split(",")]:
                baselines[d] = [] if names == "none" else [x.strip() for x in names.split(",")]

    accounts = []
    for line in section(md, "Accounts outside the workforce").splitlines():
        m = _ACCOUNT_ROW.match(line.strip())
        if m:
            _, var, role, licensed, in_rmau = m.groups()
            key = var.removeprefix("TENANT_").removesuffix("_UPN").lower()
            accounts.append(
                {
                    "key": key,
                    "upn": "${" + var + "}",
                    "roles": [role],
                    "licensed": licensed == "yes",
                    "in_rmau": in_rmau == "yes",
                }
            )

    cap = int(re.search(r"license_cap:\s*(\d+)", section(md, "Licenses")).group(1))

    admin_units = []
    for line in section(md, "Administrative units").splitlines():
        if line.startswith("| AU-") or line.startswith("| RMAU-"):
            c = cells(line)
            if c[1].startswith("Dynamic"):
                admin_units.append(
                    {
                        "name": c[0],
                        "kind": "dynamic",
                        "rule": re.search(r"`([^`]+)`", c[1]).group(1),
                    }
                )
            else:
                members = [x.strip().strip("`") for x in c[2].split(",")]
                admin_units.append(
                    {"name": c[0], "kind": "restricted_management", "members": members}
                )

    # privileged = holders of a directory role through a PRIV group
    priv_members = {m for g in groups if g["kind"] == "role_assignable" for m in g["members"]}
    for u in users:
        if u["upn_prefix"] in priv_members:
            u["employee_type"] = "Privileged"

    return {
        "domain": DOMAIN,
        "licenses": {"cap": cap},
        "accounts": accounts,
        "users": users,
        "pre_hires": pre_hires,
        "groups": groups,
        "admin_units": admin_units,
        "baselines": baselines,
    }


def render(data: dict) -> str:
    return HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110)


def generate() -> str:
    return render(parse(SOURCE.read_text(encoding="utf-8")))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    text = generate()
    if "--check" in argv:
        current = (
            TARGET.read_text(encoding="utf-8").replace("\r\n", "\n") if TARGET.exists() else ""
        )
        if current != text:
            print(
                "seed/tenant.yaml is out of date: run `uv run python -m seed.generate_tenant`",
                file=sys.stderr,
            )
            return 1
        print("seed/tenant.yaml is up to date")
        return 0
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
