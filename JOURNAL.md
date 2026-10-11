# Journal

## 2026-10-08: Scope, tenant and seed org

**Done.** Defined the project as a follow-up to an earlier scoped helpdesk prototype: an IAM-only system with four domain agents (Directory Services, Access Management, Identity Governance, PAM) and a team lead. Wrote the seed org spec and the ticket generation prompt.

**Decisions.**
* Domain split follows how enterprise IAM teams are organised, not ticket types. Boundaries drawn along system boundaries are easier to defend.
* Microsoft Entra over Okta. The Okta free developer org is capped far below the 22 users the seed org needs, and the governance and PAM features this project depends on (PIM, administrative units) are part of the Entra E5 developer sandbox. The design stays IdP neutral at the policy level so an Okta adapter can follow.
* No public IAM ticket dataset exists, so tickets are generated against the seeded tenant. Every object a ticket names exists in the tenant, which a scraped dataset could never offer.
* Tenant sized to the sandbox: 1 admin, 22 users, 2 reserved joiner seats. Seat exhaustion becomes a real test case.

**Next.** Generate tickets with several models.

## 2026-10-10: Dataset v1

**Done.** Generated 125 tickets with five different LLMs from the same prompt and merged them into one dataset.

**Numbers.** 125 raw, 36 dropped, 8 added by hand, 89 final. Split 61 dev, 28 heldout, stratified by class.

**Findings.**
* Most drops were duplicates: the generators converged on the same scenarios (the same leaver five times, the same mover five times).
* Label quality varied a lot between models. The worst batch used the reject route for ordinary access requests and twice asked to add users to groups they were already in.
* One generator tool appended an expired presigned storage URL, token included, to a ticket line. Raw batches stay out of the repository.
* Labelling forced ten policy decisions the spec had left open, for example department baselines, who may request a removal, and a fixed PAM approver. They are recorded in `data/DATASET.md`.

**Decision.** Added `validate.py`, which checks every ticket against the seed org. Any future batch goes through it before merging.

## 2026-10-11: Sprint 0 design

**Done.** Permission model, policy file, sprint plan, data protection note, dataset v2.

**Decisions.**
* **One MCP server, four agent identities.** The gateway is the only process with Graph credentials and holds one certificate per agent. A single shared identity would make the Entra audit log useless for telling agents apart and would turn one compromised agent into all of them.
* **Three enforcement layers.** Entra platform controls, Graph application permissions, gateway policy. The design question for every dangerous action is which layers stop it. `docs/PERMISSIONS.md` section 5 answers it per action, including the cases where only the gateway stands in the way.
* **Restricted management administrative unit for privileged objects.** Reading the Graph documentation showed that application permissions such as `GroupMember.ReadWrite.All` are tenant wide and need no directory role in app-only calls. Only an RMAU makes tenant-wide application permissions stop applying to an object. Privileged users and PRIV groups go into one, so even a gateway bug cannot let an agent touch them.
* **Unlock is not a real operation.** Cloud-only Entra has no admin unlock: smart lockout expires on its own and an admin can only end it through a password reset. Six tickets expected an unlock action. DS-01 is retired; lockouts become AM-06 triage (read sign-in logs, advise waiting or self-service reset). This is the first case where using a real tenant caught an error the synthetic labels carried.
* **No standing seed app.** Seed and reset run under a human admin's delegated sign in, so no highly privileged app credential exists between runs.
* **Secrets never reach the model.** Temporary passwords go to an out-of-band outbox; the model only sees that a reset succeeded. This limits what a prompt injection could exfiltrate.
* **ISO/IEC 27001 as evidence, not a claim.** The standard certifies a management system, not a tool. Each policy rule carries Annex A control tags so the audit log can produce evidence per control.
* **GDPR by design.** Field allowlists per tool, no IP addresses or phone numbers to the model, configurable log retention.

**Numbers.** Dataset v2: 89 tickets, 7 relabelled (6 lockouts plus 1 scope creep ticket), validator 0 errors.

**Open.** Spikes S1 to S8 in `docs/PERMISSIONS.md`. The least privileged way for IG to create users (S5) decides how broad IG's permissions end up.

**Next.** Sprint 0 build, starting with the scaffold and the seed scripts.

## 2026-10-11 (later): Claude Code agents and Entra Agent ID

**Decisions.**
* **Agents are Claude Code agents, not hand written agent code.** The team lead runs as the main agent, the domain agents as subagents, all defined as Markdown files in `runtime/.claude/`. This lets the project test subagents, skills and hooks as building blocks. Each agent file declares its own inline MCP connection to the gateway with its own token, so the agent to identity binding lives in configuration the model cannot touch. Built-in tools are denied; agents only see gateway tools and skills.
* **Hooks are not the security boundary.** They run inside the client. They guard domains, cap reroutes, trace calls with `agent_type` and enforce the result format, which makes failures early and visible. The gateway and Entra stay the enforcement layers and trust nothing a hook did.
* **The runtime has its own `.claude` folder.** Development uses the root `.claude/` as usual; the agents under test live in `runtime/.claude/` with restrictive settings. The harness runs `claude -p` from `runtime/`.
* **Entra Agent ID only where it fits.** Microsoft does not allow agent identities to hold User Administrator or other high-privilege roles, blocks permissions such as `User.ReadWrite.All`, and keeps agent identities out of administrative units. App-only password reset needs at least User Administrator, so the write agents cannot be agent identities with built-in roles. PAM is read only and becomes the Agent ID trial. If a custom role with password reset rights is accepted for an agent identity (spike S9), the write agents can follow.

**Why it matters.** The platform vendor sets the autonomous agent line at reader level. This project's claim becomes concrete: how far a gateway with external authority records can safely move that line, and which actions still need a human.

**Next.** Sprint 0 build. New tasks 0.12 (runtime skeleton and hooks) and 0.13 (Agent ID trial), new spikes S9 and S10.

## 2026-10-11: Sprint 0, task 0.1 scaffold

**Done.** `pyproject.toml` (Python 3.12 pinned in `.python-version`, hatchling, `src/iamagents` layout), package skeleton for gateway, services, hooks and eval, `.env.example` with placeholders only, ruff and pytest config, a layout smoke test. `uv run pytest` and `uv run ruff check` pass.

**Decisions.**
* `pytest` runs `tests/unit` only and deselects the `live` marker by default, so a plain run can never touch the tenant. Anything under `tests/live` is marked `live` automatically.
* `data/` is excluded from ruff. The two dataset scripts use long table lines (190 line-length findings) and rewriting them has no value.
* The tenant is Entra ID Free until the P2 trial is activated right before the tenant tasks. Everything up to spike S10 stays tenant free.

**Numbers.** 5 tests, 0 ruff findings, dataset validator unchanged.

**Next.** 0.8 mock services.

## 2026-10-11: Sprint 0, task 0.8 mock services

**Done.** SQLite mock services in `src/iamagents/services/`: tickets, HR events (E1 to E6), approval store, verification records, change requests, PAM request store, outbox. `MockServices` wires them to one database. `derive_fixtures(ticket, mode)` and `apply_fixtures` load per-ticket state.

**Decisions.**
* **Two fixtures are derived, not added to the dataset.** Verification: seeded for the requester only, never the target, and not at all when the ticket expects an `AM-01 verify_identity` step. Change request: seeded as issued by IG in `isolated` mode when the ticket expects `IG-04 issue_change_request`, not seeded in `integrated` mode. Documented in `data/DATASET.md`.
* **Reason for requester-only verification.** Identity mismatch tickets must still fail on `requester_is_target` in the gateway.
* **The approval store keeps no verdict.** Invalid approvals (wrong approver, self) are stored as recorded. Whether they count is the gateway's decision, so a model reading the store learns nothing the policy has not decided.
* **HR events load only when referenced** (plus executed preconditions), so a ticket without an event finds none.
* **Outbox secrets** are readable only through `open_message`, which tools must not call. `metadata` has no secret field.
* Every fixture load records its mode, so a report states which mode produced it.
* **Scoring note for 0.10.** In isolated mode compare only the actions of the agent under test, by rule prefix.

**Numbers.** 26 unit tests pass (21 new), ruff clean. All 89 tickets apply in both modes.

**Next.** 0.9 gateway skeleton.

## 2026-10-11: Sprint 0, task 0.9 gateway skeleton

**Done.** `src/iamagents/gateway/`: MCP server (streamable HTTP, `mcp` 2.3 low-level server), bearer token auth, per-token tool lists from `policy.yaml`, policy engine with named predicates, Graph client interface plus `FakeGraph` (seed org from `seed/seed-org.md` and `data/validate.py`), field minimiser, protection checker with cache, hash-chained audit log, admin endpoint. Four tools: `get_user`, `get_group`, `list_group_members`, `reset_password`. Other policy tools stay off the list until their sprint.

**Decisions.**
* **Active ticket binding.** `ticket_id` comes from the agent, so an injected ticket could pass another ticket's id and borrow its requester and approvals. The harness sets the active ticket through `Gateway.bind_ticket` or a separate admin endpoint (own port, own token, not MCP). Any call whose `ticket_id` differs is denied and audited as `ticket_mismatch`. No bound ticket means `no_active_ticket`. Requester comes from the stored ticket record, never from ticket text.
* **Protected principal = RMAU-Protected member OR any active directory role.** A PIM-elevated user is an admin for that window without being in the RMAU. The lookup uses the calling agent's own Graph identity (Directory Readers), a 30 s cache, and a failed lookup (not cached) counts as protected and routes to PAM. Source of RMAU membership in the real tenant is left to 0.5. Recorded in `PERMISSIONS.md` section 3.
* **Plan and live results are identical.** Only the audit differs (`allow_plan` versus `allowed`). Plan mode records an intended call and sends nothing; live mode generates the password, calls Graph as `ds` and sends the password to the outbox.
* **Secrets.** Passwords travel as a `Secret` object whose repr and str are redacted and which cannot be pickled, so even a Graph client that logs its request body at debug level does not print it. The test turns on debug logging for the gateway and the Graph client, runs a reset and checks the password is not in captured logs, the audit file, the tool result or any mock store except the outbox. A mutation check (logging the revealed value) makes that test fail.
* **FakeGraph L1 and L2 are emulated.** Marked as such in code and test names. They show that a call which gets past a buggy gateway is still refused in the model, but prove nothing about Entra. Task 0.7 is the proof.
* **Policy additions.** New rule `READ-01` (shared reads with field allowlists) and `mode`, `target` added to the audit record fields. `.env.example` tokens renamed to `GW_TOKEN_*` to match the agent files, plus `GW_ADMIN_TOKEN`.
* An unknown `require:` condition fails closed. The audit log stores identifiers only; a test checks that injected text in tool arguments does not reach it.
* Audit chain detects edits, deletions and reordering. Truncating the tail is not detectable from the file alone; the Sprint 6 hash anchoring covers it.

**Numbers.** 83 unit tests pass (57 new), ruff clean. Mutation checks: ticket binding disabled fails 2 tests, password logged fails 1.

**Open.** Real `msgraph-sdk` client and its debug logging behaviour (S1 should check that the password stays out of kiota logs). The gateway process and harness will share the mock SQLite file (`MOCK_DB_PATH`); to be wired in 0.10 and 0.12.

**Next.** 0.11 data protection basics.

## 2026-10-11: Sprint 0, task 0.11 data protection basics

**Done.** Egress guard on every tool result, secret registry with log, audit and result redaction, outbox lifecycle, audit retention with an in-chain checkpoint, `/admin/stats` and `/admin/purge`, `DATA_PROTECTION.md` updated with the implemented controls.

**Decisions.**
* **Egress guard fails closed.** A hit blocks the whole result, the agent sees a generic internal error, the audit records `blocked_egress` with the kind and path (never the value). A hit means a tool bug, not an agent mistake.
* **Precision over coverage.** False positives would silently fail tickets. Keys match by whole camelCase or snake_case segment, not substring, with an explicit safe list. Values need a strict IPv4 or phone shape, after masking GUIDs, ISO dates and timestamps. Switching the key match to substring broke 11 tests, among them the gateway's own results, so the negative set earns its place. Known gaps: IPv6, dot-separated phone numbers.
* **Blocked results are counted per tool** from the audit log (`/admin/stats`), so the eval report can show false positives as their own number.
* **Secrets in the outbox.** Wiped as soon as they are read through the outbox API; undelivered ones expire after 24 hours (`OUTBOX_TTL_HOURS`).
* **Retention checkpoint stays inside the chain.** The audit chain detects partial or accidental edits, not a full consistent rewrite; a test pins that limit. First anchor: the head hash (`AuditLog.head_hash`) is recorded in every eval report and in every journal entry for a live run. External attestation remains a Sprint 6 option.
* The `Secret` wrapper stays the primary control; the registry is a second layer, in memory only.

**Numbers.** 194 unit tests pass (111 new in this task, including a 36 entry negative set), ruff clean. Mutation checks: substring key matching fails 11 tests, unmasked dates fail 4, disabled guard fails 3.

**Next.** 0.12 runtime hooks.
