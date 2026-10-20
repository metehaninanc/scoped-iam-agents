# End of project checklist

Cleanup that must happen when the project is finished, or when the test tenant is given up. Each item says where the thing was created, so nothing is left behind in the tenant or on the machine.

## Tenant

* [ ] **Remove the delegated consent grant of the seed scripts.** `seed/` signs in with the first-party public client "Microsoft Graph Command Line Tools" (application id `14d82eec-204b-4c2f-b7e8-296a70dab67e`) through the device code flow, and the admin consents to a set of delegated scopes once (the exact list is recorded in `JOURNAL.md` at the first live run). Remove it in Entra admin center under Enterprise applications, Microsoft Graph Command Line Tools, Permissions (revoke the admin and user consent grants), or delete the `oauth2PermissionGrant` objects for that client, and delete the service principal if nothing else uses it.
* [ ] Delete the four agent app registrations (`iam-agent-ds`, `iam-agent-am`, `iam-agent-ig`, `iam-agent-pam`), their certificates and their service principals (task 0.5).
* [ ] Remove the custom roles, the role assignments of the agents and the Entra Agent ID blueprint and agent identity from spike S9 / task 0.13.
* [ ] Remove `AU-Workforce` and `RMAU-Protected` (the admin accounts leave the RMAU first), the PRIV groups and their role assignments (task 0.4).
* [ ] Delete the seeded users and groups (`uv run python -m seed.reset --prune` removes everything outside the seed, deleting the seed itself is done by hand or by dropping the tenant).
* [ ] Cancel the P2 trial licenses, or let the trial lapse, and check the admin accounts keep working without them.
* [ ] Delete the designated sign-in test users, if the later spike S12 created any.

## This machine

* [ ] Delete `.env` (tenant domain, admin addresses, gateway tokens), `certs/` and the private keys.
* [ ] Delete `state/` (audit logs, run artifacts, `state/claude-config` with its login, rendered runtimes).
* [ ] Remove leftover Claude Code transcripts of runtime sessions under `~/.claude/projects/` (folders named after `state-runtime-run-*`).
* [ ] Sign out of the dedicated Claude config dir and revoke the device code session in the admin account's sign-in activity.

## Repository

* [ ] Run `uv run pytest tests/unit -k real_tenant_values` and check that no tracked file or commit contains the tenant domain or an admin address.
* [ ] Check `eval/reports/milestones/*.md` for gateway tokens and the tenant domain (the harness scans them before writing; scan again before publishing).
* [ ] Check `.gitignore` before the last commit (CLAUDE.md rule 4).
