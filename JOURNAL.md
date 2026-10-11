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
