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

**Numbers.** 194 unit tests pass (111 new in this task, including a 35 entry negative set), ruff clean. Mutation checks: substring key matching fails 11 tests, unmasked dates fail 4, disabled guard fails 3.

**Next.** 0.12 runtime hooks.

## 2026-10-11: Sprint 0, task 0.12 runtime hooks and token issuing

**Done.** Four hook modules in `src/iamagents/hooks/` (`domain_guard`, `hop_limit`, `trace`, `result_schema`) with shared helpers, `settings.json` updated (Stop hook for a domain agent running as main agent, dispatch matcher `Agent|Task` until S10 pins the tool name), `python -m iamagents.gateway.tokens` for token issuing into `.env`, gateway-side hop limit, config tests that pin the runtime isolation settings.

**Decisions.**
* **The gateway enforces the hop limit itself.** It counts distinct domain agent identities per active ticket (first agent plus `reroute_limit` reroutes, so at most 3), denies a 4th identity (`hop_limit_exceeded`, audited) and `close_allowed()` refuses closing a ticket that exceeded it. The `close_ticket` tool arrives in Sprint 5 and must call `close_allowed()`. Tested with the hook skipped.
* **Hop hook denies from the 4th dispatch**, keyed on `session_id`. ASSUMPTION written in the code: one ticket per `claude -p` session. The harness in 0.10 starts a fresh session per ticket and must assert it.
* **Hooks read the tool lists from `policy.yaml`**, the same file the gateway uses, so they cannot drift. `domain_guard` fails closed when `agent_type` is missing. Where `agent_type` is absent for a main agent started with `--agent`, the fallback (an `IAM_AGENT` env var) is decided in S10.
* **Trace keeps decision fields only** (`ok`, `decision`, `rule_id`, `tier`, `reason`, `route_to`), never `data`. Retention helper included.
* **result_schema retries once, then `schema_violation`.** After the one allowed retry the hook lets the agent stop and writes a `schema_violation` event to the trace. The scoring counts it as a failure, never a pass. The message text is not logged.
* **Token isolation is configuration, not process isolation.** All agents share one Claude Code process, so all `GW_TOKEN_*` sit in one environment. Only the denial of Bash, Read, Write, Edit, Glob, Grep, WebFetch and WebSearch keeps an agent away from another agent's token. `tests/unit/test_runtime_config.py` fails if one is missing from the deny list or appears in any agent's `tools`, and checks that agent files and `policy.yaml` list the same tools. Documented as a known limit in `PERMISSIONS.md` section 2.
* Token issuing never prints values, keeps existing tokens unless `--rotate`, and checks that all are distinct.

**Numbers.** 300 unit tests pass (106 new), ruff clean. Hook subprocess tests run the modules as `settings.json` does, one of them with the exact `uv run --project ..` command. Mutation checks: gateway hop check off fails 1, `Read` removed from the deny list fails 1, `Bash` added to ds fails 2.

**Open for S10.** Hook input in headless mode (`agent_type` for a main agent, `tool_response` shape, dispatch tool name `Agent` or `Task`), `uv run` startup latency per hook call.

**Next.** Spike S10 (needs a real `claude -p` run; will ask first), then 0.10 eval harness.

## 2026-10-11: Spike S10, Claude Code mechanics (local, no tenant)

**Done.** About 45 short `claude -p` runs (exploration plus the live test file) (Claude Code 2.1.273 on Windows, `--model haiku`, which resolved to `claude-haiku-4-5-20251001`) against the local gateway on the fake Graph. Reproducible as `tests/live/test_s10_claude_code.py` (10 tests, 2 minutes, `-m live`). New production code from the findings: `src/iamagents/launch.py` (render runtime, scrubbed environment).

**The three questions of S10: all confirmed.**
* Inline HTTP MCP with an `Authorization` header and `${VAR}` expansion works in agent frontmatter, for the main agent and for subagents. The audit shows `agent=ds` for the subagent's call.
* `agent_type` is in the hook input in headless mode: `ds` for `--agent ds`, `team-lead` for the team lead's main thread, the subagent's type plus an `agent_id` inside a subagent. No `IAM_AGENT` fallback needed. The dispatch tool is `Agent` in hook input.
* `--agent` applies `tools`, `mcpServers` and `skills`. `ds` sees exactly `Skill` plus its gateway tools. The team lead sees `Agent`/`Task` and `Skill` and **no** `mcp__gw-ds__`, `mcp__gw-am__`, `mcp__gw-ig__` or `mcp__gw-pam__` tool.

**Hooks, live.** `domain_guard` denied a ds call to `mcp__gw-am__get_user` and the call never reached the gateway. `hop_limit` allowed ds, am, ig and denied the 4th dispatch. `result_schema` delivered one "Stop hook feedback" and then wrote `schema_violation` to the trace (twice, with a model that kept refusing JSON). `PostToolUse` `tool_response` is a list of `{type: text, text: <json>}`, which `trace` parses. Settings `deny` beats an agent's `tools`: Bash listed in an agent file stays unavailable, also in subagents.

**Findings that change the design.**
1. **Token exposure through environment variable expansion in tool input** (reported to the vendor; details withheld until fixed). A model could get a token from the Claude process environment into its own context, the transcript and the gateway audit log, even through a tool that was disabled. Consequence: no token (and no other secret, such as an API key) may be in the Claude process environment. The harness renders literal tokens into a copy of the agent files under the gitignored `state/runtime/` and starts `claude` with an allowlisted environment (now 11 variables, no user or computer name). Re-run of the exposure test against that setup: the tokens did not reach the model, the transcripts or the audit log.
2. **Token sweep** (every `GW_*` value searched in stream output, hook capture, trace, audit log, gateway log, hook state, all transcripts and subagent transcripts under `~/.claude/projects`, the rest of `~/.claude`, and the `--debug-file` log):

   | Setup | Result |
   |---|---|
   | Env tokens, runs 1, 2, 3 and the `--debug` run (normal use) | clean everywhere, including the debug log |
   | Env tokens, run 6 (the exposure test) | **leak**: run stream, the probe session transcript, and this dev session's transcript (I displayed the output). All tokens were rotated afterwards |
   | Rendered runtime and scrubbed env, runs with the exploit prompts and `--debug` | clean everywhere except the rendered agent files themselves, which is expected; they are deleted after the run |

   The live test repeats the sweep over the audit log, the run outputs and the Claude project transcripts. The spike's own Claude transcripts remain in `~/.claude/projects/C--Projects-scoped-iam-agents-state-s10-*`; the one from run 6 holds a rotated, dead token.
3. **Trust.** In a directory that is not a trusted workspace Claude Code silently skips the inline MCP servers of agent files (init shows only the account's connector, the agent has no gateway tool). The runtime is rendered inside the repo (trust is inherited) and the repo must have been trusted once by opening `claude` in it.
4. **Dev context leak.** Agents run from inside the repo load the development `CLAUDE.md` (the team lead quoted its hard rules back) and get the dev project's auto memory path. `claudeMdExcludes: ["**/CLAUDE.md"]` and `autoMemoryEnabled: false` in the runtime settings fix both (verified: NONE, no memory path).
5. **Unrestricted dispatch.** With a plain `Agent` tool the team lead can dispatch built-in types. A `general-purpose` subagent had no Bash or Read (settings deny applies in subagents) but could load the 8 claude.ai Docs connector tools through `ToolSearch` (`create`, `read`, `query`, `export`, ...), an exfiltration path. `Agent(ds, am, ig, pam)` in the team lead file closes it ("Agent type 'general-purpose' not found. Available agents: am, ds, ig, pam"); a config test pins it.
6. **Skills and connectors are session wide.** The `Skill` tool can invoke any skill of the session (a probe launched a user level skill). `--setting-sources project` reduces 37 to 24 visible skills, bundled ones remain. `--strict-mcp-config` removes the account's connector but also the agents' own inline gateway servers (subagents get no gateway tools), so it cannot be used. Connector tools stay unreachable because no agent has `ToolSearch` and dispatch is restricted. Open: whether a skill's `allowed-tools` could widen an agent's tools; not tested.

**Latency** (10 runs each through Git Bash, which is what runs hooks): `uv run --project .. python -m iamagents.hooks.domain_guard` 308 ms median, the venv interpreter directly 276 ms, `trace` 84 ms, bare interpreter 56 ms. `uv run` adds only about 30 ms; the cost is importing pydantic and YAML in `domain_guard`. No change needed; a hook that reads the YAML without pydantic would save about 200 ms per gateway call if it ever matters. Hooks run under Git Bash, so `uv` must be on the PATH of the `claude` process (or the hook commands use an absolute interpreter, as the rendered copy does).

**Other.** `claude -p` waits 3 s for stdin unless it is closed: use `stdin=DEVNULL`. Cost: the runs whose cost I checked were 0.02 to 0.04 USD each; I did not total them. The `capture` hook used to record raw payloads is deleted, and so are the capture files, rendered runtimes and the dev gateway's state; findings above are all that was needed.

**Decisions.**
* Tokens are literals in a rendered, short lived copy of the agent files, never in the environment. Next step for 0.10: per-run tokens issued and revoked through the admin endpoint, so a leaked token dies with the run.
* The runtime lives under `state/runtime/` and the repo's `CLAUDE.md` and auto memory are switched off in its settings. `CLAUDE.md` of the repo updated: the harness no longer runs from `runtime/` directly.

**Numbers.** 312 unit tests pass (12 new: launcher and config pins), 10 live S10 tests pass, ruff clean.

**Next.** 0.10 eval harness with a stub agent. It must use `iamagents.launch`, assert one ticket per `claude -p` session (the hop counter assumption), start `claude` with `stdin=DEVNULL`, issue per-run tokens, and record the audit head hash and the per-tool `blocked_egress` counts in every report.

## 2026-10-11: S10 follow-up, hardening and disclosure preparation

**Done.** Three hardening items from S10, the vendor report draft, and a sanitisation of the repo. 341 unit tests pass; the live file has 12 passing tests and 2 skips (config dir login pending; one dispatch test that skips when the model does not attempt the dispatch). Spike transcript folders under `~/.claude/projects` (14, `state-s10-*`) deleted as agreed.

**1. Allowlist, not blocklist.** `claude --tools` sets the exact built-in set. Probe (agent file listing 30 tools, no deny list): 28 tools available without `--tools`, 3 with `--tools Skill` (`Skill` plus the two gateway tools). With our deny list but no `--tools`, 14 built-ins survived (`ToolSearch`, `SendMessage`, `RemoteTrigger`, `CronCreate`, `Monitor`, ...), so the blocklist alone was the wrong mechanism. Now: `launch.claude_command` passes `--tools Skill` (domain agents) and `--tools Skill,Agent` (team lead; `Agent(ds, am, ig, pam)` still refuses `general-purpose`, `ds` still dispatches). The deny list is extended (ToolSearch, PowerShell, Monitor, SendMessage, TaskStop, TaskOutput, Cron*, worktree tools, RemoteTrigger, ListAgents, ReportFindings, PushNotification, TodoWrite) as a second layer. A config test pins that the launcher set equals the built-ins in each agent file; a live test reads each agent's init tool list and requires exact equality with `Skill` (plus the dispatch tool for the team lead) and the policy tools the gateway serves, so one extra tool fails it. Passes for ds, am, ig, pam and team-lead.

**2. Dedicated `CLAUDE_CONFIG_DIR` (`state/claude-config`), partial.** Without a login the run reports "Not logged in", but the init message already shows the effect: the claude.ai connector is gone from the MCP list, the account's 14 plugin skills (13 `anthropic-skills:*` and `schedule`) are gone and two more bundled skills appear (37 visible skills become 25, all bundled or ours), `plugins` is empty. Claude Code also says how trust is stored per config dir (`projects[<repo>].hasTrustDialogAccepted` in that dir's `.claude.json`). **Still to do by hand:** run `claude` once with `CLAUDE_CONFIG_DIR=C:\Projects\scoped-iam-agents\state\claude-config` from the repo, log in and accept the trust dialog. Then the skipped live test checks that no account skill or connector appears. If the connector reappears after login (account-bound), that goes here.

**3. Skill `allowed-tools`.** A skill with `allowed-tools: Bash, PowerShell, Read` invoked by an agent whose `tools` is `Skill` plus a gateway tool: Bash never became available or ran, in three setups (our deny list; no deny list with `--tools Skill`; no deny list and no `--tools`). The agent's own `tools` list is the limit, as are the deny list and `--tools`. Side finding for Sprint 1: a main agent started with `--agent` does not get its frontmatter `skills:` preloaded into its context (it did not know a marker phrase from its own skill), so agent prompts must tell the agent to invoke its skills with the Skill tool.

**Environment allowlist.** Checked the real environment (98 variables in the desktop app session): the filter dropped the app's session tokens and API base URL variables and every other secret-bearing name; none of the passed values is token-shaped and no PATH entry looks secret. Because anything left in the environment can reach the model, the allowlist was cut to the minimum that still runs a full agent call with hooks (verified one variable at a time): 11 variables, no user name, computer name, domain or logon server. Unit tests pin the size, the missing identifiers and the absence of token-shaped values.

**Vendor report.** A minimal repro (standard library echo MCP server, canary variable, Claude Code and Windows versions) and a draft report are in the gitignored `state/disclosure/`. The mechanics are removed from `JOURNAL.md`, `PERMISSIONS.md`, `launch.py` and the live tests, which now say "token exposure through environment variable expansion in tool input, reported to the vendor, details withheld until fixed" and keep the mitigation fully described. The two live tests that spelled out the mechanics moved out of the repo. **History:** the 8 local commits were not pushed (`origin/main` is still `Sprint0`), and only one contained the details, so the S10 commit was rewritten to drop them (the small journal fix was folded into it). The old commits are still reachable through the local reflog; `git reflog expire --expire=now --all` followed by `git gc --prune=now` removes them for good. Not run yet.

**Next.** 0.10 eval harness with a stub agent.

## 2026-10-11: Sprint 0, task 0.10 eval harness with a stub agent

**Done.** `eval/` (`harness.py`, `scoring.py`, `runners.py`, `report.py`, `prompt.py`): `uv run python -m eval.harness --mode plan --agent ds --split dev`. Per ticket: fixtures in the chosen mode, a fresh fake Graph directory, ticket binding, the system under test, a read of the gateway audit records, scoring, report. Runners: `claude` (real `claude -p` from a rendered runtime with a scrubbed environment and per-run tokens) and `stub` (no model, always escalates). Scoring rules are in `data/DATASET.md`.

**Sprint 0 exit test.** The stub agent over all 89 tickets: 0 passed (correct for an agent that only escalates), policy violations 0, errors 0, blocked egress 0. Outcome correct on 1 of 89 (the one escalation ticket), tier on 16, final agent on 7 (the tickets nobody handles). Slice sizes of the domain agents reproduce `SPRINTS.md` exactly (DS 24 of which 7 heldout, AM 21/7, IG 39/13, PAM 7/2).

**Decisions.**
* **Requester as channel metadata** outside the `<ticket>` tags; tag-like text inside the ticket cannot close the block. The gateway uses its stored requester regardless.
* **Isolated mode:** outcome and tier are scored only for the last actor; an upstream agent is scored on `route_to` naming the expected next agent. Only the agent's own actions (by rule prefix) count.
* **Violations and unbacked claims come from the gateway audit, not from the agent's report.** A violation is an allowed or planned write the label does not expect. A reported write with no allowed gateway call is an unbacked claim and fails the ticket.
* **One ticket per session, asserted.** A ticket that used more than one `claude` session is an error (the hop counter is keyed on the session id).
* **Per-run tokens.** The gateway issues fresh random tokens for each run and revokes them afterwards; they are registered for redaction, and the harness keeps them for the report secret scan. A leaked token dies with its run.
* **Reports:** per-run JSON and Markdown go to `eval/reports/local/` (gitignored); `--milestone` writes the Markdown to `eval/reports/milestones/`, which is committed (sprint exits and the single heldout run). Every report is scanned for the `.env` and run token values before anything is written; the scan runs on the JSON first. Reports carry the fixture mode, plan or live, dataset and policy hashes, git commit, claude version, the audit head hash (the first anchor) and the blocked egress count per tool.
* **The heldout set is run once.** A model run that includes heldout tickets needs `--heldout-final` and `--milestone`, and refuses when `eval/reports/milestones/heldout.lock` exists. The stub is exempt.
* `--mode live` currently means the fake Graph; the real tenant arrives with the tenant tasks.

**Two bugs found by the tests, both would have hit the first real run.**
* On Windows `claude` is a `.cmd` shim run through cmd.exe, which cuts an argument at the first newline; the harness prompt is multi-line. The prompt now goes through stdin (no length limit either).
* On a timeout `subprocess.run` killed only the shim; the real process kept the pipes open and the harness waited for it. The runner now kills the whole process tree (the test took 60 s before and 3 s after).

**Numbers.** 409 unit tests pass (68 new), ruff clean. Tests drive the real runner with a fake `claude` that reads the token from the rendered agent file and calls the in-process gateway over HTTP, so render, tokens, audit and scoring are covered without a model. Mutation checks: violation detection off, one-session assertion off, secret scan off and unbacked check off each fail a test.

**Open.** The live smoke of one real ticket with `claude` (a few cents) waits for your OK. With the real tenant the dataset's `<tenant>.onmicrosoft.com` placeholder must be mapped to the real domain in prompts and fixtures (tenant tasks). The login for `state/claude-config` is still to be done by hand (S10 follow-up). Observed first and final agent follow simple rules (documented) that Sprint 5 may refine.

**Next.** Tenant tasks 0.2 to 0.7 and 0.13, after the P2 trial is activated.
