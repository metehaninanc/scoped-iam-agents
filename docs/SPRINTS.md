# Sprints

Seven sprints. Sprint 0 builds everything the agents stand on; sprints 1 to 4 build one domain agent each and test it alone; sprint 5 adds the team lead; sprint 6 runs the whole system on the heldout set.

## Working rules for every sprint

* Agents are tested alone before they are combined. In sprints 1 to 4 the harness routes each ticket using its `expected_first_agent` label, so a domain agent's errors are never mixed with routing errors.
* Two evaluation modes. **Plan mode** (default): reads hit the real tenant, writes are checked by the gateway and recorded as intended calls but not sent to Graph. Fast, repeatable, used for all 89 tickets. **Live mode**: writes go to Graph and the tenant is reset after each ticket. Used for spikes, the bypass test and a curated subset.
* Exit criteria are numbers from the eval report, not impressions.
* The hard metric in every sprint is **policy violations = 0**: no executed write outside what the policy allows. Accuracy can grow sprint by sprint; violations cannot exist.
* Each work session ends with a `JOURNAL.md` entry.
* Agents are Claude Code agents in `runtime/.claude/`. The harness runs `claude -p --agent <name> --output-format json` per ticket from `runtime/`. In sprints 1 to 4 the domain agent runs as the main agent; from sprint 5 the team lead runs as main and the domain agents as subagents.
* Each domain sprint delivers three things: the agent prompt, its skills, and any hook it needs. Skills carry procedure; the gateway carries policy.
* Model: `model: sonnet` in every agent file.

## Sprint 0: Foundation

**Goal:** a reproducible tenant, four scoped agent identities, a working gateway with one audited end to end call, the Claude Code runtime skeleton, and an eval harness that scores a stub agent.

| # | Task | Done when |
|---|---|---|
| 0.1 | Repo scaffold: Python 3.12, uv, ruff, pytest, layout from `CLAUDE.md`, `.env.example` | `uv run pytest` passes on an empty suite |
| 0.2 | Seed config `seed/tenant.yaml` generated from `seed/seed-org.md` (users, managers, groups, owners, memberships, licenses, AUs, RMAU) | Config validates against `data/validate.py` constants |
| 0.3 | `seed/seed.py` and `seed/reset.py` with delegated admin sign in (device code). Idempotent. `reset.py` diffs the tenant against `seed/baseline.json` and repairs | Seed twice, then reset: diff is empty |
| 0.4 | `AU-Workforce` (dynamic, `userType` and `employeeType` rule), then `RMAU-Protected` with both admin accounts (the admin first, spike S11, then the emergency access account) | Membership matches section 3 of `PERMISSIONS.md`; no role holder matches the `AU-Workforce` rule |
| 0.5 | Four app registrations with certificates, Graph permissions and AU-scoped roles exactly as `PERMISSIONS.md` section 4 | `seed/identities.py --check` prints the effective grants and matches the doc |
| 0.6 | Spikes S1 to S11 (S1 to S9 and S11 as live tests in `tests/live/`, S10 as a local Claude Code test) | Every spike has a recorded result in the journal; `PERMISSIONS.md` updated where reality differed |
| 0.7 | Bypass test: call Graph directly with each agent identity for every row of `PERMISSIONS.md` section 5 | Observed L1 and L2 results match the table |
| 0.8 | Mock services in SQLite: tickets, HR events, approval store, verification records, change requests, PAM request store, outbox | Fixtures load from a ticket's `approval_fixture` and `hr_event_ref` |
| 0.9 | Gateway skeleton: one MCP server, streamable HTTP, bearer token per agent, tool list filtered per agent, policy engine loading `policy/policy.yaml`, Graph client per identity, field minimiser, hash-chained audit log with ISO tags | `get_user` works for all agents; `reset_password` works in plan mode and live mode; audit chain verifies |
| 0.10 | Eval harness: loads the dataset, applies fixtures, runs `claude -p --agent <name>` per ticket from `runtime/`, compares outcome, tier, first and final agent, and `expected_actions` as a set; writes a JSON and Markdown report | A stub agent that always escalates produces a full report over 89 tickets |
| 0.11 | Data protection basics: field allowlist, secrets to outbox only, retention setting | Unit tests prove a reset password never appears in any tool result or log |
| 0.12 | Claude Code runtime skeleton: agent files, skill stubs and `settings.json` in `runtime/.claude/` (already drafted), the four hook modules in `src/iamagents/hooks/`, token issuing from `.env` | A stub `ds` agent calls `get_user` through its own token; `domain_guard` blocks it from calling an `am` tool; `trace` records `agent_type`; `hop_limit` stops a third reroute |
| 0.13 | Entra Agent ID trial (spike S9): one blueprint, one agent identity for PAM, read permissions only | Token obtained through the exchange; role and permission refusals recorded; decision written in `PERMISSIONS.md` section 8 |

**Exit:** tasks 0.1 to 0.13 done, spike results journaled, bypass table reproduced.

**Out of scope:** real agent prompts and skill content, the team lead's routing logic, UI.

## Sprint 1: Directory Services agent

**Scope:** DS-02, DS-03, DS-04 (consuming IG change requests from fixtures), DS-05 rejections, handoff of privileged resets to PAM.
**Eval slice:** tickets where DS is the first or final agent (24, of which 7 heldout).
**Exit:** violations 0; outcome accuracy at least 90 percent on dev; every privileged target routed to PAM.

## Sprint 2: Access Management agent

**Scope:** AM-01 verification, AM-02 MFA reset, AM-04 minimised reads, AM-05 rejections, AM-06 lockout triage. Prerequisite: spike S12 (designated test users with known passwords in `.env`, used to generate sign-in activity and smart lockout), otherwise the sign-in logs these tickets read are empty.
**Eval slice:** 21 tickets, 7 heldout.
**Exit:** violations 0; outcome accuracy at least 90 percent on dev; no IP address or phone number in any model-visible tool result.

## Sprint 3: Identity Governance agent

The largest sprint.
**Scope:** joiner, mover, leaver from HR events; access request evaluation with owner approval, self approval fallback, SoD and duration limits; change requests to DS; guest flow; reports; seat exhaustion.
**Eval slice:** 39 tickets, 13 heldout.
**Exit:** violations 0; outcome accuracy at least 85 percent on dev; SoD, self approval and wrong approver cases all rejected.

## Sprint 4: PAM agent

**Scope:** role and eligibility reports, T3 request packages with fixed approver and executor, PAM-03 rejections, privileged reset packages handed over by DS.
**Eval slice:** 7 tickets, 2 heldout.
**Exit:** violations 0; every package names the correct approver and executor; no write tool exists for this agent.

## Sprint 5: Team lead and orchestration

**Scope:** team lead as the main agent with the four domain agents as subagents; routing skill; reroute limit through the `hop_limit` hook with the gateway refusing to close a ticket that exceeded it; closing only on a proof record; escalation; `TL_REJECT` for injected instructions; the forced reroute test in `routing_test`.
**Eval:** full dev set with real routing.
**Exit:** violations 0; routing accuracy at least 90 percent; no ticket closed without a proof record; reroute limit never exceeded.

## Sprint 6: End to end and write up

* Heldout run, once, with results frozen in the journal.
* Model comparison: the same heldout run with `claude-haiku-5-5`. Expected result: lower accuracy, same zero violations. That is the evidence that safety comes from the architecture, not the model.
* Live mode run on a curated subset of about 15 tickets, including the bypass demo.
* ISO evidence report generated from the audit log, grouped by Annex A control.
* Optional: send audit log hashes to the attestation project.
* README written last, as the project's general description.

## Starting a sprint in Claude Code

Open the repo and send:

> Read CLAUDE.md, docs/SPRINTS.md and the latest JOURNAL.md entry. We are starting Sprint N. Propose a short plan for the first task, wait for my go, then build it with tests. Stop and ask before any live tenant write outside tests/live.
