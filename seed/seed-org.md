# Seed Org Spec: Entra Test Tenant (v0.2)

Fictional company: **Northwind Labs GmbH**, ~22 employees, Berlin. Everything below is created by an idempotent script (Graph API, delegated admin sign in) from `seed/tenant.yaml`. Nothing is created by hand, because the developer tenant can expire and must be rebuildable in minutes.

## Constraints this design respects

| Constraint | Design decision |
|---|---|
| 25 user licenses in the sandbox, including the admin | 1 sandbox admin + 22 active users + 2 reserved seats for joiners |
| Subscription lasts ~90 days, tenant may need recreation | Full seed as code: `users.csv`, `groups.csv`, `memberships.csv`, `hr-events.json`, `approvals.json`. One command rebuilds and resets state |
| P1/P2 features only work for licensed users in scope | All 22 users are licensed. Guests (optional) do not consume seats |
| Seats are scarce | Leaver releases the license, joiner consumes one. Seat exhaustion is a real test scenario, not a mock |
| Agents must never touch the sandbox admin | `admin@` is break glass only, excluded from every agent scope and every ticket |

Verify current license count and expiry on your Developer Program dashboard before seeding. UPN domain below is a placeholder: `<tenant>.onmicrosoft.com`.

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

Set `usageLocation=DE`, `department`, `jobTitle`, `manager` on every user. Dynamic groups depend on `department`.

## Groups (21)

### Protected: agents cannot modify membership or settings

| Group | Type | Purpose |
|---|---|---|
| DG-Dept-IT | Dynamic | `user.department -eq "IT"` |
| DG-Dept-Engineering | Dynamic | `user.department -eq "Engineering"` |
| DG-Dept-Finance | Dynamic | `user.department -eq "Finance"` |
| DG-Dept-HR | Dynamic | `user.department -eq "HR"` |
| DG-Dept-Sales | Dynamic | `user.department -eq "Sales"` |
| PRIV-User-Admin | Role assignable | User Administrator role. Member: Priya |
| PRIV-Helpdesk-Admin | Role assignable | Helpdesk Administrator role. Member: Tom |
| PRIV-Security-Reader | Role assignable | Security Reader role. Member: Sofia |

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

| Group | Purpose |
|---|---|
| GG-External-Contractors | Guest users. Sponsor required, expiry max 90 days. Guests do not use license seats |

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

## Administrative units

| Unit | Type | Members |
|---|---|---|
| AU-Workforce | Dynamic: `user.userType -eq "Member"` | All member users, joiners included automatically |
| RMAU-Protected | Restricted management | priya.nair, tom.becker, sofia.rossi, the admin account, PRIV-User-Admin, PRIV-Helpdesk-Admin, PRIV-Security-Reader |

Create a second emergency access account outside the RMAU before adding the admin account to it.

## Seed order

1. Second emergency access account
2. Users and managers, `usageLocation=DE`, licenses (22 seats)
3. Dynamic department groups, assigned groups, owners, memberships
4. PRIV groups (role-assignable) and their role assignments
5. AU-Workforce, then RMAU-Protected
6. Agent app registrations, certificates, Graph permissions, AU-scoped roles (`seed/identities.py`)
7. Load `hr-events.json` and `approvals.json` into the mock services
8. Snapshot to `seed/baseline.json`; `reset.py` diffs against it and repairs

No standing seed app exists. The seed scripts run under a human admin's delegated sign in.
