from pathlib import Path

import pytest

from iamagents.services.fixtures import apply_fixtures, derive_fixtures, last_load
from iamagents.services.mock import MockServices

DATASET = Path(__file__).resolve().parents[2] / "data" / "iam-tickets-v2.jsonl"
DOMAIN = "@<tenant>.onmicrosoft.com"


@pytest.fixture
def services():
    s = MockServices()
    s.tickets.load_dataset(DATASET)
    yield s
    s.db.close()


def ticket(services, ticket_id):
    t = services.tickets.get(ticket_id)
    assert t is not None
    return t


def test_dataset_loads_89_tickets(services):
    assert len(services.tickets.list()) == 89
    assert len(services.tickets.list("heldout")) == 28
    assert len(services.tickets.list("dev")) == 61


def test_every_ticket_applies_in_both_modes(services):
    for mode in ("isolated", "integrated"):
        for t in services.tickets.list():
            fx = apply_fixtures(services, t, mode)
            assert fx.mode == mode
            assert last_load(services)["mode"] == mode


def test_invalid_mode_rejected(services):
    with pytest.raises(ValueError):
        derive_fixtures(ticket(services, "IAM-001"), "bogus")


# approvals


def test_valid_approval_is_loaded(services):
    t = next(t for t in services.tickets.list() if t["approval_fixture"]["state"] == "valid")
    apply_fixtures(services, t, "isolated")
    got = services.approvals.get(t["ticket_id"], t["approval_fixture"]["scope"])
    assert len(got) == 1
    assert got[0]["approver_upn"] == t["approval_fixture"]["approver_upn"]


@pytest.mark.parametrize("state", ["invalid_wrong_approver", "invalid_self"])
def test_invalid_approvals_are_stored_as_recorded(services, state):
    # the store keeps the record; the gateway decides it is not a valid approval
    t = next(t for t in services.tickets.list() if t["approval_fixture"]["state"] == state)
    apply_fixtures(services, t, "isolated")
    got = services.approvals.get(t["ticket_id"])
    assert [g["approver_upn"] for g in got] == [t["approval_fixture"]["approver_upn"]]
    assert all("state" not in g for g in got)


@pytest.mark.parametrize("state", ["none", "not_applicable"])
def test_no_approval_record_for_none_states(services, state):
    t = next(t for t in services.tickets.list() if t["approval_fixture"]["state"] == state)
    apply_fixtures(services, t, "isolated")
    assert services.approvals.get(t["ticket_id"]) == []


# HR events


def test_hr_event_loaded_only_when_referenced(services):
    with_ref = ticket(services, "IAM-031")
    apply_fixtures(services, with_ref, "isolated")
    assert services.hr_events.get("E5")["status"] == "pending"
    assert services.hr_events.get("E1") is None
    apply_fixtures(services, ticket(services, "IAM-001"), "isolated")
    assert services.hr_events.get("E5") is None


def test_iam_080_preconditions_mark_e1_e2_executed(services):
    apply_fixtures(services, ticket(services, "IAM-080"), "isolated")
    assert services.hr_events.get("E1")["status"] == "executed"
    assert services.hr_events.get("E2")["status"] == "executed"


# verification


def test_verification_seeded_for_requester_only(services):
    t = ticket(services, "IAM-001")
    fx = apply_fixtures(services, t, "isolated")
    assert [v["subject_upn"] for v in fx.verifications] == [t["requester_upn"]]
    assert services.verification.has_record(t["ticket_id"], t["requester_upn"])
    for target in t["target_objects"]:
        if target != t["requester_upn"]:
            assert not services.verification.has_record(t["ticket_id"], target)


def test_verification_not_seeded_when_agent_must_verify(services):
    verify_tickets = [
        t
        for t in services.tickets.list()
        if any(a.startswith("AM-01 verify_identity") for a in t["expected_actions"] or [])
    ]
    assert len(verify_tickets) == 7
    for t in verify_tickets:
        fx = apply_fixtures(services, t, "isolated")
        assert fx.verifications == ()
        assert not services.verification.has_record(t["ticket_id"], t["requester_upn"])


def test_identity_mismatch_target_never_verified(services):
    # ticket where the requester asks for somebody else's reset: the target has no record
    t = next(
        t
        for t in services.tickets.list()
        if any(a.startswith("DS-02 reject") for a in t["expected_actions"] or [])
        and t["target_objects"]
        and t["target_objects"][0] != t["requester_upn"]
    )
    apply_fixtures(services, t, "isolated")
    assert not services.verification.has_record(t["ticket_id"], t["target_objects"][0])


# change requests


def test_change_request_seeded_in_isolated_mode_only(services):
    t = ticket(services, "IAM-041")
    fx = apply_fixtures(services, t, "isolated")
    assert len(fx.change_requests) == 1
    cr = services.change_requests.find_issued(
        "IAM-041", "elena.costa" + DOMAIN, "SG-Share-Finance-Reports"
    )
    assert cr is not None and cr["issued_by"] == "ig"

    apply_fixtures(services, t, "integrated")
    assert services.change_requests.list("IAM-041") == []


def test_change_request_count_matches_ig04_steps(services):
    for t in services.tickets.list():
        steps = [
            a for a in t["expected_actions"] or [] if a.startswith("IG-04 issue_change_request")
        ]
        assert len(derive_fixtures(t, "isolated").change_requests) == len(steps)
        assert derive_fixtures(t, "integrated").change_requests == ()


def test_change_request_can_be_consumed_once(services):
    apply_fixtures(services, ticket(services, "IAM-041"), "isolated")
    args = ("IAM-041", "elena.costa" + DOMAIN, "SG-Share-Finance-Reports")
    cr = services.change_requests.find_issued(*args)
    services.change_requests.consume(cr["id"])
    assert services.change_requests.find_issued(*args) is None


# isolation between tickets and mode record


def test_fixtures_do_not_leak_between_tickets(services):
    apply_fixtures(services, ticket(services, "IAM-041"), "isolated")
    apply_fixtures(services, ticket(services, "IAM-001"), "isolated")
    assert services.change_requests.list("IAM-041") == []
    assert not services.verification.has_record("IAM-041", "x")
    assert services.approvals.get("IAM-041") == []
    assert last_load(services)["ticket_id"] == "IAM-001"


def test_clear_keeps_tickets(services):
    apply_fixtures(services, ticket(services, "IAM-031"), "isolated")
    services.clear()
    assert services.hr_events.get("E5") is None
    assert len(services.tickets.list()) == 89


def test_mode_is_recorded_per_load(services):
    apply_fixtures(services, ticket(services, "IAM-041"), "isolated")
    assert last_load(services) == {
        "ticket_id": "IAM-041",
        "mode": "isolated",
        "loaded_at": ticket(services, "IAM-041")["created_at"],
    }
    apply_fixtures(services, ticket(services, "IAM-041"), "integrated")
    assert last_load(services)["mode"] == "integrated"


# other stores


def test_pam_request_roundtrip(services):
    services.pam_requests.create(
        "IAM-070",
        "mfa_reset",
        "tom.becker" + DOMAIN,
        "marcus.weber" + DOMAIN,
        "priya.nair" + DOMAIN,
        "prepared_for_human",
        {"note": "x"},
        "2026-09-14T08:00:00+02:00",
    )
    got = services.pam_requests.list("IAM-070")
    assert got[0]["approver_upn"] == "marcus.weber" + DOMAIN
    assert got[0]["detail"] == {"note": "x"}


def test_outbox_secret_only_through_open_message(services):
    msg_id = services.outbox.send(
        "IAM-002", "zoe.martin" + DOMAIN, "temporary_password", "S3cret-Pw!", "t"
    )
    meta = services.outbox.metadata("IAM-002")
    assert "S3cret-Pw!" not in repr(meta)
    assert "secret" not in meta[0]
    assert services.outbox.open_message(msg_id)["secret"] == "S3cret-Pw!"
