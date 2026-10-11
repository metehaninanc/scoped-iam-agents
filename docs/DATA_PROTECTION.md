# Data Protection Design Note

This is an engineering design note, not legal advice. It records how the system is built so that a company deploying it can run its own data protection assessment.

## Prototype scope

All people, groups and HR events are fictional and live in an isolated developer tenant. The only real personal data processed is the operator's own admin sign-in data. No real employee data is used during development or evaluation.

## What reaches the model

The language model is a processor of whatever the gateway sends it. The gateway applies a field allowlist per tool (see `model_fields` in `policy/policy.yaml`), in line with data minimisation (GDPR Art. 5(1)(c)) and data protection by design (Art. 25).

| Data | Reaches the model | Notes |
|---|---|---|
| Ticket text | Yes | Unavoidable; treated as untrusted |
| Display name, UPN, department, title, manager | Yes | Needed for routing and policy explanations |
| Sign-in logs | Reduced | Time, app, error code, failure reason, city, country. No IP address, no device details |
| MFA methods | Reduced | Method types only, never phone numbers |
| Temporary passwords, TAPs | Never | Delivered out of band through the outbox |
| Group membership lists | Yes, for the requested group only | Report tools return names and UPNs |

## Logs and retention

The gateway audit log keeps who did what, to whom and under which rule. It stores identifiers, not ticket bodies. Sign-in data fetched for triage is not persisted by the gateway.

| Setting | Default | Effect |
|---|---|---|
| `audit.retention_days` in `policy.yaml`, override `LOG_RETENTION_DAYS` | 90 | `AuditLog.purge` removes expired leading records at gateway start and on `POST /admin/purge` |
| `OUTBOX_TTL_HOURS` | 24 | Undelivered outbox secrets are wiped after this time |

Purging would break the hash chain, so it writes a `retention_checkpoint` record as the first line. It holds the hash of the last purged record and the number purged, and the first kept record must link to it. The remaining chain still verifies, and an edited or removed checkpoint is detected.

**Limit of the hash chain.** Without an external anchor the chain detects partial or accidental edits, not a full rewrite of the file: someone who can rewrite the whole file can recompute every hash. Removing records from the tail is also invisible from the file alone. The first anchor is the head hash (`AuditLog.head_hash`, served by `GET /admin/stats`): every eval report and every journal entry for a live run records the current head hash, and a later mismatch against a recorded head shows the file changed. External attestation of the head hash is a Sprint 6 option. A test pins this limit (`test_documented_limit_a_full_consistent_rewrite_is_not_detected`).

## Decisions about people

Account disablement and access changes that affect an employee are triggered by HR events or named human approvers, not by the model's judgement. The agent executes a decision a human already made, which keeps the system clear of solely automated decision making with significant effects (Art. 22).

## For a real deployment

* The LLM provider is a processor. A data processing agreement and a valid transfer mechanism for any processing outside the EU are needed.
* An agent that reads sign-in logs processes employee monitoring data. A data protection impact assessment (Art. 35) is likely appropriate.
* In Germany, technical systems suited to monitoring employee behaviour or performance are subject to works council co-determination (BetrVG §87(1) No. 6). Involve the works council before deployment where one exists.
* ISO/IEC 27001 Annex A control A.5.34 (Privacy and protection of PII) is evidenced by the minimisation log and the retention setting.

## Implemented controls

| Control | Where | Test |
|---|---|---|
| Field allowlist per tool | `model_fields` of `READ-01` in `policy.yaml`, applied by `gateway/minimise.py`; a tool without an allowlist fails closed | `test_allowlists_exist_and_hold_no_forbidden_key`, `test_get_user_works_for_ds_am_ig` |
| Temporary passwords never reach the model | Generated in the gateway, wrapped in `Secret` (redacted repr), delivered to the outbox only | `test_password_never_appears_in_logs_audit_or_result` |
| Outbox secrets are short lived | Wiped on delivery, expired after `OUTBOX_TTL_HOURS` | `test_password_is_wiped_from_the_outbox_once_delivered`, `test_undelivered_secret_expires_after_the_ttl` |
| Egress guard | `gateway/egress.py` runs on every tool result. A hit blocks the whole result, audits `blocked_egress`, and the agent sees a generic internal error | `test_leaky_tool_is_blocked_audited_and_counted`, `test_negative_set_is_never_blocked` |
| Secret redaction as a second layer | `gateway/secrets_registry.py`: registered secrets are blocked in results and redacted in audit records and log records | `test_log_records_redact_registered_secrets`, `test_audit_records_redact_registered_secrets` |
| Retention | `AuditLog.purge`, `Outbox.expire`, `Gateway.run_retention` | `test_purge_keeps_the_chain_verifiable`, `test_run_retention_uses_policy_default_and_outbox_ttl` |

**Egress guard precision.** A false positive fails a ticket and looks like an agent error, so the guard is narrow. Keys are compared by whole camelCase or snake_case segment against a forbidden set (`password`, `secret`, `token`, `tap`, `phone`, `ip` and a few more), never by substring, with an explicit safe list (`password_reset_delivered`, `tokenIssuerType`). Value patterns are IPv4 with valid octets and no neighbouring word characters or dots, E.164 numbers, and separated numbers with at least 7 digits; GUIDs, ISO dates and timestamps are masked first and identifier fields are not pattern scanned. A negative set (GUIDs, timestamps, versions, ticket numbers, dates) must never be blocked. Not covered: IPv6 and dot-separated phone numbers. The number of blocked results per tool appears in every eval report, so a false positive shows up as its own number.
