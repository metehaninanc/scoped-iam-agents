"""Derive and load per-ticket fixtures. The dataset itself is never changed.

Approval fixtures and HR events come straight from the ticket (`approval_fixture`,
`hr_event_ref`, `preconditions`). Two more are derived here, see `data/DATASET.md`:

* Verification: a record for the requester (never the target) unless the ticket expects an
  `AM-01 verify_identity` step, because then the agent must create it itself.
* Change request: in `isolated` mode, an `IG-04 issue_change_request <user> <group>` step is
  seeded as already issued by IG so DS can be tested alone. In `integrated` mode nothing is
  seeded and IG must issue it in the same run.
"""

import re
from dataclasses import dataclass

from .mock import MockServices

MODES = ("isolated", "integrated")

_CR_ACTION = re.compile(r"^IG-04 issue_change_request (\S+) (\S+)$")
_PRECONDITION = re.compile(r"^hr:(E\d+) executed$")


@dataclass(frozen=True)
class Fixtures:
    ticket_id: str
    mode: str
    created_at: str
    approvals: tuple[dict, ...] = ()
    hr_events: tuple[tuple[str, str], ...] = ()  # (event_id, status)
    verifications: tuple[dict, ...] = ()
    change_requests: tuple[dict, ...] = ()


def derive_fixtures(ticket: dict, mode: str) -> Fixtures:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    ticket_id = ticket["ticket_id"]
    created_at = ticket["created_at"]
    requester = ticket["requester_upn"]
    domain = requester.split("@", 1)[1]
    actions = ticket.get("expected_actions") or []

    approvals = []
    af = ticket.get("approval_fixture") or {}
    if af.get("state") not in (None, "none", "not_applicable") and af.get("approver_upn"):
        approvals.append(
            {
                "ticket_id": ticket_id,
                "approver_upn": af["approver_upn"],
                "scope": af["scope"],
                "decision": "approved",
                "created_at": created_at,
            }
        )

    hr_events: dict[str, str] = {}
    if ticket.get("hr_event_ref"):
        hr_events[ticket["hr_event_ref"]] = "pending"
    for pre in ticket.get("preconditions") or []:
        m = _PRECONDITION.match(pre)
        if m:
            hr_events[m.group(1)] = "executed"

    verifications = []
    if not any(a.startswith("AM-01 verify_identity") for a in actions):
        verifications.append(
            {
                "ticket_id": ticket_id,
                "subject_upn": requester,
                "method": "fixture",
                "created_at": created_at,
            }
        )

    change_requests = []
    if mode == "isolated":
        for a in actions:
            m = _CR_ACTION.match(a)
            if m:
                change_requests.append(
                    {
                        "ticket_id": ticket_id,
                        "user_upn": f"{m.group(1)}@{domain}",
                        "group_name": m.group(2),
                        "issued_by": "ig",
                        "created_at": created_at,
                    }
                )

    return Fixtures(
        ticket_id=ticket_id,
        mode=mode,
        created_at=created_at,
        approvals=tuple(approvals),
        hr_events=tuple(hr_events.items()),
        verifications=tuple(verifications),
        change_requests=tuple(change_requests),
    )


def apply_fixtures(services: MockServices, ticket: dict, mode: str) -> Fixtures:
    """Wipe per-ticket state, then load the fixtures for this ticket and record the mode."""
    fx = derive_fixtures(ticket, mode)
    services.clear()
    for a in fx.approvals:
        services.approvals.record(**a)
    for event_id, status in fx.hr_events:
        services.hr_events.load(event_id, status)
    for v in fx.verifications:
        services.verification.record(**v)
    for c in fx.change_requests:
        services.change_requests.issue(**c)
    with services.db.conn:
        services.db.conn.execute(
            "INSERT INTO fixture_loads (ticket_id, mode, loaded_at) VALUES (?, ?, ?)",
            (fx.ticket_id, fx.mode, fx.created_at),
        )
    return fx


def last_load(services: MockServices) -> dict | None:
    """The ticket and mode of the most recent fixture load, so a report can state its mode."""
    row = services.db.conn.execute(
        "SELECT ticket_id, mode, loaded_at FROM fixture_loads ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None
