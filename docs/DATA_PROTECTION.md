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

The gateway audit log keeps who did what, to whom and under which rule. It stores identifiers, not ticket bodies. Retention is a setting (`audit.retention_days`, default 90). Sign-in data fetched for triage is not persisted by the gateway.

## Decisions about people

Account disablement and access changes that affect an employee are triggered by HR events or named human approvers, not by the model's judgement. The agent executes a decision a human already made, which keeps the system clear of solely automated decision making with significant effects (Art. 22).

## For a real deployment

* The LLM provider is a processor. A data processing agreement and a valid transfer mechanism for any processing outside the EU are needed.
* An agent that reads sign-in logs processes employee monitoring data. A data protection impact assessment (Art. 35) is likely appropriate.
* In Germany, technical systems suited to monitoring employee behaviour or performance are subject to works council co-determination (BetrVG §87(1) No. 6). Involve the works council before deployment where one exists.
* ISO/IEC 27001 Annex A control A.5.34 (Privacy and protection of PII) is evidenced by the minimisation log and the retention setting.
