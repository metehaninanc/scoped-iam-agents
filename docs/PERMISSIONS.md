# Agent Permission Model (v0.3)

This document defines what each agent is allowed to do in the Entra tenant, which layer enforces each limit, and why. Every claim about Entra or Microsoft Graph behaviour links to Microsoft documentation. Claims that the documentation does not settle are listed as spikes in section 7 and must be confirmed against the live tenant in Sprint 0 before any agent code relies on them.

## 1. Design principles

| Principle | Source | How it shows up here |
|---|---|---|
| Least privilege | Saltzer and Schroeder [1] | Each agent has its own identity with only the Graph permissions its rules need. The team lead has no Graph identity at all. |
| Complete mediation | Saltzer and Schroeder [1] | Every agent action passes through one gateway that checks policy before any Graph call. Agents never hold Graph credentials. |
| Separation of privilege | Saltzer and Schroeder [1] | Sensitive changes need two independent conditions: a policy decision and an external authority record (owner approval or HR event). |
| Separation of duties | Clark and Wilson [2] | Identity Governance decides on access requests; Directory Services executes them. The agent that approves is never the agent that writes. |
| Role based access control | Sandhu et al. [3] | Agent permissions are bound to Entra roles scoped to administrative units, not to individual objects. |
| Untrusted input | Greshake et al. [4] | Ticket text is data, never instruction. Approvals and HR events only count when they exist as records outside the ticket. |
| Control and data flow separation | Debenedetti et al. [5], Beurer-Kellner et al. [6] | The model chooses among tools, but the gateway decides whether a call is allowed using records the model cannot write. Secrets such as temporary passwords never enter the model context. |
| Never trust, always verify | NIST SP 800-207 [7] | Each call is authorised on its own, with no session-wide trust for an agent. |
| Excessive agency | OWASP LLM06:2025 [8] | Limits sit in permissions and code, not in prompts. A prompt can be bypassed; a missing permission cannot. |

## 2. Identities

| Identity | Type | Used by |
|---|---|---|
| `iam-agent-ds` | App registration, certificate credential | Directory Services agent, via the gateway |
| `iam-agent-am` | App registration, certificate credential | Access Management agent, via the gateway |
| `iam-agent-ig` | App registration, certificate credential | Identity Governance agent, via the gateway |
| `iam-agent-pam` | App registration, certificate credential | PAM agent, via the gateway |
| Team lead | No Entra identity | Reads tickets and gateway records only |
| Seed and reset | Delegated sign in by a human admin (device code) | `seed/` scripts. No standing privileged app exists. |

There is one MCP server (the gateway). It holds the four certificates and selects the identity from the calling agent's bearer token. Each Claude Code agent file in `runtime/.claude/agents/` declares its own inline connection to the gateway with its own token, so the binding between agent and identity is fixed in configuration, outside anything the model can influence. Agents only hold an MCP token, so a compromised agent cannot reach Graph directly or borrow another agent's identity. In the Entra audit log each change shows the specific agent app as the actor.

**Known limit: token isolation is configuration, not process isolation.** All agents run in one Claude Code process, so every `GW_TOKEN_*` is in the same environment. What stops an agent from reading another agent's token or calling the gateway directly is the denial of `Bash`, `Read`, `Write`, `Edit`, `Glob`, `Grep`, `WebFetch` and `WebSearch` in `runtime/.claude/settings.json`, together with agent files that list only their own gateway tools. If one of those tools were allowed, an agent could read the environment or the `.env` file, or send HTTP requests with another agent's token. `tests/unit/test_runtime_config.py` fails if any of them is missing from the deny list or appears in an agent's `tools`. Real process isolation (one process and environment per agent) would remove the limit and is not part of this prototype. The gateway still enforces tool lists per token, ticket binding and the hop limit, so a stolen token is limited to that agent's own tools.

## 3. Three enforcement layers

| Layer | Enforced by | Strength |
|---|---|---|
| L1 Platform | Entra itself: administrative unit scoping, restricted management administrative units, role-assignable groups, dynamic groups | Holds even if the gateway has a bug or is bypassed |
| L2 Token | Microsoft Graph application permissions granted to each agent app | Holds even if the gateway has a bug, but tenant wide |
| L3 Gateway | Deterministic policy code in `policy/policy.yaml` and the gateway | Fine grained (owner approval, SoD, tiers), but only as good as our code |

### Key platform facts this design relies on

1. **App-only password reset needs a role as well as a permission.** Microsoft documents that in app-only scenarios "the calling app must be assigned a supported permission *and* at least the *User Administrator* Microsoft Entra role" to update `passwordProfile` [M1]. Disabling an account app-only likewise needs a sufficiently privileged role in addition to Graph permissions [M1].
2. **Roles scoped to an administrative unit only reach non-admin users for sensitive actions.** Password reset, authentication method changes and sensitive property changes apply only to non-administrator users when the role is assigned at administrative unit scope [M2]. Service principals can receive administrative unit scoped roles but also need tenant-wide directory read, for example Directory Readers [M2].
3. **Restricted management administrative units (RMAU) override tenant-wide permissions.** Tenant-scoped roles are blocked for objects in an RMAU, and "if you assign Microsoft Graph application permissions to the application, those permissions won't apply because it's restricted" [M3]. Role-assignable groups in an RMAU can only have membership changed by Global Administrators and Privileged Role Administrators [M3].
4. **Role-assignable group membership needs an extra permission.** Adding members to a role-assignable group requires `RoleManagement.ReadWrite.Directory` in addition to `GroupMember.ReadWrite.All` [M4]. No agent holds that permission.
5. **`GroupMember.ReadWrite.All` is tenant wide and needs no role in app-only calls** [M4]. That is why the RMAU in fact 3 matters: it is the only platform control that limits a tenant-wide Graph permission.
6. **There is no admin unlock primitive in cloud-only Entra.** Smart lockout is temporary, one minute by default and longer on repeated lockouts. An admin can end it early only through a password reset, and a self-service "forgot my password" reset clears it [M5]. The old rule DS-01 "unlock" is retired; lockouts become AM-06 triage.

### Administrative units

| Unit | Type | Members | Purpose |
|---|---|---|---|
| `AU-Workforce` | Dynamic, rule `user.userType -eq "Member"` | All member users | Scope for the agents' write roles. New joiners land here automatically, so no agent needs permission to change administrative unit membership. |
| `RMAU-Protected` | Restricted management, assigned | Priya, Tom, Sofia, the admin account, the three PRIV groups | Blocks every agent's tenant-wide Graph permissions and every regular-AU role for these objects |

Privileged users are members of both. An RMAU blocks roles scoped to regular administrative units that the object also belongs to [M3], so `AU-Workforce` roles do not reach them.

Before adding the admin account to the RMAU, create a second emergency access account outside it.

**Protected principal (gateway definition).** A user is protected if they are a member of `RMAU-Protected` or hold any active directory role. The second clause covers a user elevated through PIM: not in the RMAU, but an administrator for that window. The gateway reads both from Graph with the calling agent's own Directory Readers access, caches the answer briefly, and treats a failed lookup as protected. Where the RMAU membership is read from in the real tenant (Graph or `seed/tenant.yaml`) is decided in task 0.5.

## 4. Per agent permissions

### Directory Services (DS)

| Item | Value |
|---|---|
| Graph application permissions | `User.Read.All`, `GroupMember.ReadWrite.All`, `User-PasswordProfile.ReadWrite.All` |
| Entra roles | User Administrator at `AU-Workforce`; Directory Readers at tenant |
| Can | Reset passwords of non-admin users; add and remove members of non-protected groups |
| Cannot (L1) | Reset any admin or RMAU member password; change PRIV group membership; edit dynamic groups |
| Cannot (L3) | Add a member without an approved change request from IG; remove a member unless the requester is the member, their manager or the owner |
| Residual risk | `GroupMember.ReadWrite.All` reaches every static, non-role-assignable group outside the RMAU. The gateway is the only control for restricted groups such as `SG-Share-HR-Confidential`. Optional hardening in section 6. |

The temporary password is generated by the gateway and sent through an out-of-band channel (a mock outbox in the prototype). The model only learns that the reset succeeded.

### Access Management (AM)

| Item | Value |
|---|---|
| Graph application permissions | `User.Read.All`, `AuditLog.Read.All`, `UserAuthenticationMethod.ReadWrite.All` |
| Entra roles | Authentication Administrator at `AU-Workforce`; Directory Readers at tenant |
| Can | Read sign-in logs and MFA registration; triage lockouts; reset MFA methods of non-admin users |
| Cannot (L1) | Change authentication methods of RMAU members |
| Cannot (L3) | Reset MFA without manager approval and a verification record; disable MFA; touch Conditional Access (no permission granted at all) |
| Residual risk | Sign-in logs are tenant wide. The gateway minimises the fields that reach the model (see `DATA_PROTECTION.md`). |

### Identity Governance (IG)

| Item | Value |
|---|---|
| Graph application permissions | `User.Read.All`, `User.EnableDisableAccount.All`, `User.RevokeSessions.All`, `GroupMember.ReadWrite.All`, license assignment and user create or update permissions to be fixed by spike S5 |
| Entra roles | User Administrator at `AU-Workforce`; Directory Readers at tenant |
| Can | Joiner, mover and leaver changes for the subject of a matching HR event; evaluate access requests and issue change requests to DS |
| Cannot (L1) | Disable or change privileged users |
| Cannot (L3) | Act without an HR event; delete accounts; grant ad hoc access directly (that goes IG to DS) |
| Residual risk | If spike S5 shows that user creation needs `User.ReadWrite.All`, IG holds a broad tenant-wide permission. The RMAU still protects privileged users, and the gateway restricts targets to HR event subjects. |

### Privileged Access Management (PAM)

| Item | Value |
|---|---|
| Graph application permissions | `RoleManagement.Read.Directory` plus the PIM for Groups read permission, confirmed in spike S7 |
| Entra roles | None |
| Can | Report active and eligible assignments; write a request package to the mock request store |
| Cannot | Any directory write. A human executes every T3 change. |

### Team Lead (TL)

No Entra identity. Tools: read ticket, read gateway records, route to an agent, close a ticket when a proof record exists, escalate to a human. The reroute limit (two) is enforced by orchestrator code, not by the prompt.

## 5. Which layer stops what

| Dangerous action | L1 Platform | L2 Token | L3 Gateway |
|---|---|---|---|
| DS resets Tom's password | Blocked: RMAU member and admin | Allowed | Blocked: privileged target, route to PAM |
| AM removes Priya's MFA methods | Blocked: RMAU member | Allowed | Blocked: T3 |
| DS adds Leo to `PRIV-User-Admin` | Blocked: role-assignable group in RMAU | Blocked: no `RoleManagement.ReadWrite.Directory` | Blocked: protected group |
| DS adds Arjun to `DG-Dept-Finance` | Blocked: dynamic membership | Allowed | Blocked: protected group |
| DS adds Zoe to `SG-Share-HR-Confidential` without Hanna's approval | Allowed | Allowed | Blocked: no valid approval record |
| IG gives Ines both ERP groups | Allowed | Allowed | Blocked: SoD rule |
| Any agent changes Conditional Access | Not granted | Not granted | Not exposed as a tool |
| Any agent assigns an admin role | Not granted | Not granted | Not exposed as a tool |

The last two rows of L1 and L2 allowing an action are the honest part of this table: those cases depend on our gateway alone. The bypass test in Sprint 0 calls Graph directly with each agent identity and must reproduce the L1 and L2 columns exactly.

## 6. Optional hardening

Put the four restricted groups into a second RMAU and give DS Groups Administrator at that RMAU scope. Tenant-wide `GroupMember.ReadWrite.All` would then stop applying to them [M3], so only DS could change them at the platform level, while owner approval stays in the gateway. Deferred until the core is working.

## 7. Spikes for Sprint 0

Each spike is a live test in `tests/live/` that records its outcome in the journal.

| ID | Question | Expected |
|---|---|---|
| S1 | DS identity resets a standard user's password, app-only, with the permission and the AU-scoped User Administrator role | 204 |
| S2 | Same call against Tom (admin, RMAU member) | 403 |
| S3 | DS adds a user to `PRIV-User-Admin` | 403 |
| S4 | AM deletes an authentication method of a standard user, then of Priya | Success, then 403 |
| S5 | Least privileged way for IG to create a user, set department, title and manager, and assign a license, app-only | Document the minimum set |
| S6 | How a smart lockout appears in sign-in logs (error code, fields) | Document for AM-06 |
| S7 | Read permissions for PIM eligible assignments, directory roles and PIM for Groups | Document the minimum set |
| S8 | Human T3 runbook for a privileged target inside the RMAU (whether a temporary RMAU-scoped assignment or temporary removal is required) | Document the auditable procedure |
| S9 | Entra Agent ID in the developer tenant: can we create a blueprint and agent identities without extra licences, which roles and Graph permissions are refused, and does a custom role with password reset rights get accepted at AU scope | Document; decide the scope of section 8 |
| S10 | Claude Code mechanics: inline HTTP MCP with an `Authorization` header and environment variable expansion inside subagent frontmatter; `agent_type` present in hook input in headless mode; `--agent` applying the agent file's `mcpServers` and `skills` to the main session | All three confirmed, or a documented fallback |

## 8. Entra Agent ID

Entra Agent ID gives AI agents first-class identities: an agent identity blueprint holds the credential, and each agent identity gets a token through an exchange with the blueprint [M6, M7]. Each of our four agents would show up in Entra as an agent rather than as an ordinary app, which is exactly the story this project tells.

**Why it is not the default for the write agents.** Microsoft blocks high-privilege access for agent identities at platform level. Global Administrator, Privileged Role Administrator and User Administrator cannot be assigned to agent identities; only lower privileged roles such as reader roles can. Permissions such as `User.ReadWrite.All` are refused, agent identities cannot join role-assignable groups, and they cannot be added to administrative units [M6, M7]. App-only password reset needs at least the User Administrator role [M1]. So with built-in roles an agent identity cannot reset a password, and our DS, AM and IG agents cannot be agent identities.

That restriction is itself a finding worth stating: the platform vendor draws the line for autonomous agents at reader level, and this project shows how far a gateway with external authority records can safely move that line, and where it still needs humans (T3).

**What we do.**

| Agent | Identity | Reason |
|---|---|---|
| PAM | Agent identity (if S9 passes) | Read only. Fits the reader roles Agent ID allows. |
| DS, AM, IG | Classic app registration | Need User Administrator or Authentication Administrator, which agent identities cannot hold |
| Team lead | None | No Graph access |

**What could change this.** Microsoft states that custom roles can be assigned to agents [M6]. If spike S9 shows that a custom role carrying the password reset permission is accepted for an agent identity at AU scope, DS moves to Agent ID and the others follow where their operations allow. Independent security research has also reported blocked permissions with allowed near-equivalents and blueprint owner escalation paths [M8]; any move to Agent ID gets a review of blueprint ownership and inherited permissions first.

## 9. ISO/IEC 27001:2022 Annex A mapping

The system does not make an organisation compliant; ISO/IEC 27001 certifies a management system, not a tool. It produces evidence for these controls. Each rule in `policy/policy.yaml` carries the control tags, so every audit record says which control it evidences.

| Control | Title | Evidence produced |
|---|---|---|
| A.5.3 | Segregation of duties | IG decides, DS executes; SoD rejections |
| A.5.15 | Access control | Every gateway decision with its rule id |
| A.5.16 | Identity management | HR event to account change links |
| A.5.17 | Authentication information | Password resets with verification record, out-of-band delivery |
| A.5.18 | Access rights | Each grant linked to an owner approval; leaver disable times |
| A.5.34 | Privacy and protection of PII | Field minimisation log, retention setting |
| A.8.2 | Privileged access rights | No permanent assignments; every T3 package with approver and executor |
| A.8.5 | Secure authentication | MFA reset conditions met before execution |
| A.8.15 | Logging | Hash-chained gateway audit log |
| A.8.16 | Monitoring activities | Blocked attempts per agent per day |

## References

[1] J. H. Saltzer and M. D. Schroeder, "The Protection of Information in Computer Systems," *Proceedings of the IEEE*, vol. 63, no. 9, pp. 1278 to 1308, 1975.

[2] D. D. Clark and D. R. Wilson, "A Comparison of Commercial and Military Computer Security Policies," in *Proc. IEEE Symposium on Security and Privacy*, 1987, pp. 184 to 194.

[3] R. S. Sandhu, E. J. Coyne, H. L. Feinstein and C. E. Youman, "Role-Based Access Control Models," *IEEE Computer*, vol. 29, no. 2, pp. 38 to 47, 1996.

[4] K. Greshake, S. Abdelnabi, S. Mishra, C. Endres, T. Holz and M. Fritz, "Not what you've signed up for: Compromising Real-World LLM-Integrated Applications with Indirect Prompt Injection," in *Proc. 16th ACM Workshop on Artificial Intelligence and Security (AISec)*, 2023. arXiv:2302.12173.

[5] E. Debenedetti, I. Shumailov, T. Fan, J. Hayes, N. Carlini, D. Fabian, C. Kern, C. Shi, A. Terzis and F. Tramèr, "Defeating Prompt Injections by Design," arXiv:2503.18813, 2025.

[6] L. Beurer-Kellner, B. Buesser, A.-M. Creţu, E. Debenedetti, D. Dobos, D. Fabian, M. Fischer, D. Froelicher, K. Grosse, D. Naeff, E. Ozoani, A. Paverd, F. Tramèr and V. Volhejn, "Design Patterns for Securing LLM Agents against Prompt Injections," arXiv:2506.08837, 2025.

[7] S. Rose, O. Borchert, S. Mitchell and S. Connelly, *Zero Trust Architecture*, NIST Special Publication 800-207, 2020.

[8] OWASP Foundation, "LLM06:2025 Excessive Agency," *OWASP Top 10 for LLM Applications 2025*.

[9] E. Debenedetti, J. Zhang, M. Balunović, L. Beurer-Kellner, M. Fischer and F. Tramèr, "AgentDojo: A Dynamic Environment to Evaluate Prompt Injection Attacks and Defenses for LLM Agents," in *NeurIPS Datasets and Benchmarks Track*, 2024. arXiv:2406.13352. Used as the model for the evaluation method in Sprint 6.

### Microsoft documentation

[M1] Update user, Microsoft Graph v1.0: https://learn.microsoft.com/en-us/graph/api/user-update?view=graph-rest-1.0

[M2] Assign Microsoft Entra roles with administrative unit scope: https://learn.microsoft.com/en-us/entra/identity/role-based-access-control/admin-units-assign-roles

[M3] Restricted management administrative units: https://learn.microsoft.com/en-us/entra/identity/role-based-access-control/admin-units-restricted-management

[M4] Add members to a group, Microsoft Graph v1.0: https://learn.microsoft.com/en-us/graph/api/group-post-members?view=graph-rest-1.0

[M5] Microsoft Entra smart lockout: https://learn.microsoft.com/en-us/entra/identity/authentication/howto-password-smart-lockout

[M6] Authorization in Microsoft Entra Agent ID: https://learn.microsoft.com/en-us/entra/agent-id/authorization-agent-id

[M7] Known issues and gaps for Microsoft Entra Agent ID: https://learn.microsoft.com/en-us/entra/agent-id/preview-known-issues

[M8] Compass Security, "Entra Agent ID from a Security Perspective," June 2026: https://blog.compass-security.com/2026/06/entra-agent-id-from-a-security-perspective/
