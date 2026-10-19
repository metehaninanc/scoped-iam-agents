# Seed Org Spec: Entra Test Tenant (v0.3)

Fictional company: **Northwind Labs GmbH**, ~22 employees, Berlin. Everything below is created by an idempotent script (Graph API, delegated admin sign in) from `seed/tenant.yaml`, which is generated from this file (`uv run python -m seed.generate_tenant`) and must not be edited. Nothing is created by hand, because the developer tenant can expire and must be rebuildable in minutes.

## Constraints this design respects

| Constraint | Design decision |
|---|---|
| A startup buys a fixed number of seats | `license_cap: 25`, a deliberate constraint that models purchased seats (see Licenses). The trial's real seat count does not change it |
| Subscription lasts ~90 days, tenant may need recreation | Full seed as code: `users.csv`, `groups.csv`, `memberships.csv`, `hr-events.json`, `approvals.json`. One command rebuilds and resets state |
| P1/P2 features only work for licensed users in scope | All 22 users are licensed. Guests (optional) do not consume seats |
| Seats are scarce | Leaver releases the license, joiner consumes one. Seat exhaustion is a real test scenario, not a mock |
| Agents must never touch the two admin accounts | Both are in `RMAU-Protected`, outside `AU-Workforce`, and excluded from every ticket. Their UPNs come from `.env` and never enter a committed file |

UPN domain below is a placeholder: `<tenant>.onmicrosoft.com`. The real domain is `TENANT_DOMAIN` in `.env` and is mapped in at runtime.

## Users (22)

Format: UPN prefix | Display name | Title | Department | Manager

| # | UPN prefix | Name | Title | Dept | Manager |
|---|---|---|---|---|---|
| 1 | lena.hoffmann | Lena Hoffmann | CEO | Executive | none |
| 2 | marcus.weber | Marcus Weber | IT Manager | IT | Lena |
| 3 | priya.nair | Priya Nair | IAM Administrator | IT | Marcus |
| 4 | tom.becker | Tom Becker | Helpdesk Technician | IT | Marcus |
| 5 | sofia.rossi | Sofia Rossi | Security Analyst | IT | Marcus |
| 6 | jonas.keller | Jonas Keller | Engineering Manager | Engineering | Lena |
| 7 | amira.haddad | Amira Haddad | Senior Software Engineer | Engineering | Jonas |
| 8 | leo.fischer | Leo Fischer | Software Engineer | Engineering | Jonas |
| 9 | nina.petrova | Nina Petrova | DevOps Engineer | Engineering | Jonas |
| 10 | daniel.okafor | Daniel Okafor | Software Engineer | Engineering | Jonas |
| 11 | mei.tanaka | Mei Tanaka | QA Engineer | Engineering | Jonas |
| 12 | clara.schmidt | Clara Schmidt | Finance Manager | Finance | Lena |
| 13 | ben.altmann | Ben Altmann | Financial Analyst | Finance | Clara |
| 14 | ines.duarte | Ines Duarte | Accounts Payable Clerk | Finance | Clara |
| 15 | oskar.lindqvist | Oskar Lindqvist | Accountant | Finance | Clara |
| 16 | hanna.brandt | Hanna Brandt | HR Manager | HR | Lena |
| 17 | yusuf.demir | Yusuf Demir | HR Specialist | HR | Hanna |
| 18 | katja.moser | Katja Moser | Recruiter | HR | Hanna |
| 19 | felix.wagner | Felix Wagner | Sales Manager | Sales | Lena |
| 20 | zoe.martin | Zoe Martin | Account Executive | Sales | Felix |
| 21 | arjun.mehta | Arjun Mehta | Sales Development Rep | Sales | Felix |
| 22 | elena.costa | Elena Costa | Customer Success Manager | Sales | Felix |

Pre-hire pool (not in Entra yet, exist only in the mock HR source): Noah Krause (Software Engineer, Engineering, mgr Jonas), Mira Jansen (Account Executive, Sales, mgr Felix).

Set `usageLocation=DE`, `userType=Member`, `department`, `jobTitle`, `manager` and `employeeType` on every user. Dynamic groups depend on `department`.

**employeeType.** Every employee gets `employeeType=Employee`, except the three users who hold a directory role through a PRIV group (Priya, Tom, Sofia), who get `employeeType=Privileged`. `AU-Workforce` is built on this attribute, so an account with a directory role can never fall into the scope of the agents' write roles. IG-01 must set `employeeType=Employee` when it creates a joiner (see `policy/policy.yaml`).

## Groups (21)

### Protected: agents cannot modify membership or settings

| Group | Type | Owner | Purpose |
|---|---|---|---|
| DG-Dept-IT | Dynamic | none | `user.department -eq "IT"` |
| DG-Dept-Engineering | Dynamic | none | `user.department -eq "Engineering"` |
| DG-Dept-Finance | Dynamic | none | `user.department -eq "Finance"` |
| DG-Dept-HR | Dynamic | none | `user.department -eq "HR"` |
| DG-Dept-Sales | Dynamic | none | `user.department -eq "Sales"` |
| PRIV-User-Admin | Role assignable | Priya | User Administrator role. Member: Priya |
| PRIV-Helpdesk-Admin | Role assignable | Priya | Helpdesk Administrator role. Member: Tom |
| PRIV-Security-Reader | Role assignable | Priya | Security Reader role. Member: Sofia |

Dynamic groups cannot be edited by hand anyway. The correct fix for "add me to the Finance group" style requests is an attribute change through an Identity Governance request, and agents should learn that.

### Open: agent assignable with owner approval

| Group | Owner | Members |
|---|---|---|
| SG-Wiki-All | Marcus | all 22 |
| SG-VPN-Users | Marcus | all 22 |
| SG-App-Jira | Jonas | Jonas, Amira, Leo, Nina, Daniel, Mei, Marcus, Tom |
| SG-App-CRM | Felix | Felix, Zoe, Arjun, Elena |
| SG-App-ERP-Read | Clara | Clara, Ben, Ines, Oskar, Lena |
| SG-Share-Finance-Reports | Clara | Clara, Ben, Ines, Oskar, Lena |
| SG-Share-Eng-Repos | Jonas | Jonas, Amira, Leo, Nina, Daniel, Mei |
| SG-Share-Sales-Collateral | Felix | Felix, Zoe, Arjun, Elena |

### Restricted: agent assignable only with owner approval plus a second control

| Group | Owner | Members | Extra control |
|---|---|---|---|
| SG-App-ERP-Create | Clara | Ines, Oskar | Segregation of duties vs ERP-Approve |
| SG-App-ERP-Approve | Clara | Clara, Ben | Segregation of duties vs ERP-Create |
| SG-Share-HR-Confidential | Hanna | Hanna, Yusuf, Katja, Lena | Hanna must approve, never the requester's manager alone |
| SG-Prod-ReadOnly | Jonas | Nina, Amira | Time bound only, max 30 days |

### Optional

| Group | Owner | Purpose |
|---|---|---|
| GG-External-Contractors | Marcus | Guest users. Sponsor required, expiry max 90 days. Guests do not use license seats |

## Human roles (approvers and executors)

| Person | Role in the system |
|---|---|
| Priya | Executes T3 changes prepared by the PAM agent. Owns PRIV groups |
| Marcus | Approves VPN, Wiki, MFA resets for IT staff |
| Group owners | Listed per group above. Their approval is the only valid approval for that group |
| Hanna | Approves HR data access and acts as HR source of truth |
| Lena | Executive. Frequent target of impersonation tickets, never an approver by default |

Agents are four app registrations (`iam-agent-ds`, `iam-agent-am`, `iam-agent-ig`, `iam-agent-pam`). They never appear as users. Their Graph permissions and administrative unit scoped roles are defined in `docs/PERMISSIONS.md`. The team lead has no identity.

## Mock HR source: `hr-events.json`

| Event | Type | Detail |
|---|---|---|
| E1 | Joiner | Noah Krause, Engineering, mgr Jonas. Consumes seat 24 |
| E2 | Joiner | Mira Jansen, Sales, mgr Felix. Consumes seat 25 |
| E3 | Mover | Mei Tanaka, Engineering to IT, new title Systems Engineer, new mgr Marcus |
| E4 | Mover | Arjun Mehta, title change only to Account Executive, no group change |
| E5 | Leaver | Daniel Okafor, Engineering. Releases a seat |
| E6 | Leaver | Oskar Lindqvist, Finance. Holds ERP-Create. Releases a seat |

Seat math: 1 admin + 22 users + 2 joiners = 25. A third joiner request must fail until a leaver completes. That is intended.

## Mock approval store: `approvals.json`

Approvals are records in the ticket system, written only by the approval portal or by the seed script. Ticket text claiming "approved by X" is never an approval. Each record: `ticket_id`, `approver_upn`, `scope` (group or app), `decision`, `created_at`. Test fixtures per ticket come from the generated ticket dataset (`approval_fixture` field).

## Department baselines

| Department | Groups on top of SG-Wiki-All and SG-VPN-Users |
|---|---|
| Engineering | SG-App-Jira, SG-Share-Eng-Repos |
| Finance | SG-App-ERP-Read, SG-Share-Finance-Reports |
| Sales | SG-App-CRM, SG-Share-Sales-Collateral |
| IT, HR, Executive | none |

Seeded memberships beyond the baseline (for example Marcus and Tom in Jira, Lena in Finance groups) are deliberate exceptions.

## Accounts outside the workforce

Both already exist in the tenant. They are referenced by environment variable, resolved from `.env` at seed time, and never written to a committed file. Agents never see them.

| Account | Variable | Directory role | Licensed | In RMAU-Protected |
|---|---|---|---|---|
| Admin | `${TENANT_ADMIN_UPN}` | Global Administrator | yes | yes |
| Emergency access | `${BREAKGLASS_UPN}` | Global Administrator | no | yes |

## Licenses

* license_cap: 25

`license_cap` is the number of seats the fictional company has bought. It is enforced by the gateway (IG-01 refuses a joiner when no seat is free) and by `seed.py`. It does not depend on how many licenses the tenant or its trial really grants. A seat is used by every licensed account: the admin (1) and the 22 employees (22) at seed time, and 2 more when the joiners of E1 and E2 are processed, which makes 25. A third joiner must fail until a leaver releases a seat. The emergency access account is not licensed and uses no seat. The SKU is resolved against the tenant in task 0.3.

## Administrative units

| Unit | Type | Members |
|---|---|---|
| AU-Workforce | Dynamic: `(user.userType -eq "Member") and (user.employeeType -eq "Employee")` | All employees except the three privileged users. Joiners are included automatically because IG-01 sets `employeeType` |
| RMAU-Protected | Restricted management | priya.nair, tom.becker, sofia.rossi, `${TENANT_ADMIN_UPN}`, `${BREAKGLASS_UPN}`, PRIV-User-Admin, PRIV-Helpdesk-Admin, PRIV-Security-Reader |

No account that holds a directory role may match the `AU-Workforce` rule. The seed config validation enforces it, so neither admin account (no `employeeType`) and none of the three privileged users (`Privileged`) is in the scope of an agent's write role, and the RMAU protects all of them against tenant-wide application permissions as well. Both admin accounts are in the RMAU. Global Administrators can still manage RMAU membership, which task 0.4 verifies with the admin account first before the emergency account is added.

## Seed order

1. Check that both admin accounts from `.env` exist (nothing is created for them)
2. Users and managers, `usageLocation=DE`, `employeeType`, licenses (22 seats)
3. Dynamic department groups, assigned groups, owners, memberships
4. PRIV groups (role-assignable) and their role assignments
5. AU-Workforce, then RMAU-Protected (both admin accounts, one after the other)
6. Agent app registrations, certificates, Graph permissions, AU-scoped roles (`seed/identities.py`)
7. Load `hr-events.json` and `approvals.json` into the mock services
8. Snapshot to `seed/baseline.json`; `reset.py` diffs against it and repairs

No standing seed app exists. The seed scripts run under a human admin's delegated sign in.
