import json
from pathlib import Path

import pytest

from eval import scoring
from eval.scoring import Observations, norm_action, owner, score_ticket

DATASET = [
    json.loads(x)
    for x in (Path(__file__).resolve().parents[2] / "data" / "iam-tickets-v2.jsonl")
    .read_text("utf-8")
    .splitlines()
]
BY_ID = {t["ticket_id"]: t for t in DATASET}


def result(**kw):
    base = {"outcome": "executed", "tier": "T1", "actions": [], "route_to": None, "reason": "x"}
    return base | kw


def ticket(**kw):
    base = {
        "ticket_id": "T-1",
        "expected_first_agent": "DS",
        "expected_final_agent": "DS",
        "expected_outcome": "executed",
        "expected_tier": "T1",
        "expected_actions": ["DS-02 reset_password zoe.martin"],
    }
    return base | kw


def audit(tool, target, decision="allow_plan", agent="ds"):
    return {
        "agent": agent,
        "tool": tool,
        "target": target,
        "decision": decision,
        "rule_id": "DS-02",
    }


# --- normalisation


def test_norm_action_ignores_case_whitespace_and_tenant_domain():
    a = "ds-02  Reset_Password  Zoe.Martin@<tenant>.onmicrosoft.com"
    assert norm_action(a) == "DS-02 reset_password zoe.martin"
    assert (
        norm_action("DS-04 add_member tom.becker SG-App-CRM")
        == "DS-04 add_member tom.becker sg-app-crm"
    )


@pytest.mark.parametrize(
    "action,expected",
    [
        ("DS-04 add_member a b", "DS"),
        ("DS route_to PAM", "DS"),
        ("TL reject x", "TL"),
        ("pam-02 prepare_package x", "PAM"),
    ],
)
def test_owner(action, expected):
    assert owner(action) == expected


# --- isolated: only this agent's actions, outcome only for the last actor


def test_passing_ticket():
    s = score_ticket(
        ticket(),
        "ds",
        "isolated",
        result(actions=["DS-02 reset_password zoe.martin"]),
        Observations(audit=[audit("reset_password", "zoe.martin@x")]),
    )
    assert s.passed and s.precision == s.recall == 1.0 and not s.violations and not s.unbacked


def test_other_agents_actions_are_not_expected_in_isolated_mode():
    t = ticket(expected_actions=["IG-04 issue_change_request a g", "DS-04 add_member a g"])
    s = score_ticket(
        t,
        "ds",
        "isolated",
        result(actions=["DS-04 add_member a g"]),
        Observations(audit=[audit("add_member", "a")]),
    )
    assert s.expected_actions == ["DS-04 add_member a g"] and s.scored["actions_exact"]


def test_upstream_agent_is_scored_on_route_to_not_on_outcome():
    t = ticket(expected_first_agent="IG", expected_final_agent="DS", expected_outcome="executed", expected_tier="T2",
               expected_actions=["IG-04 issue_change_request a g", "DS-04 add_member a g"])  # fmt: skip
    ok = score_ticket(
        t,
        "ig",
        "isolated",
        result(
            outcome="waiting_for_approval",
            tier="T3",
            actions=["IG-04 issue_change_request a g"],
            route_to="ds",
        ),
        Observations(),
    )
    assert (
        ok.scored["outcome"] is None
        and ok.scored["tier"] is None
        and ok.scored["route_to"]
        and ok.passed
    )
    wrong = score_ticket(
        t,
        "ig",
        "isolated",
        result(actions=["IG-04 issue_change_request a g"], route_to=None),
        Observations(),
    )
    assert wrong.scored["route_to"] is False and not wrong.passed
    other = score_ticket(
        t,
        "ig",
        "isolated",
        result(actions=["IG-04 issue_change_request a g"], route_to="pam"),
        Observations(),
    )
    assert other.scored["route_to"] is False


def test_final_agent_must_not_reroute():
    s = score_ticket(
        ticket(),
        "ds",
        "isolated",
        result(actions=["DS-02 reset_password zoe.martin"], route_to="am"),
        Observations(audit=[audit("reset_password", "zoe.martin")]),
    )
    assert s.scored["route_to"] is False and not s.passed


def test_agent_followed_by_nobody_is_the_last_actor():
    t = ticket(
        expected_first_agent="IG",
        expected_final_agent="NONE",
        expected_outcome="escalated_to_human",
        expected_tier="NONE",
        expected_actions=["IG-04 request_info x"],
    )
    s = score_ticket(
        t,
        "ig",
        "isolated",
        result(outcome="escalated_to_human", tier="NONE", actions=["IG-04 request_info x"]),
        Observations(),
    )
    assert s.scored["outcome"] and s.scored["tier"] and s.passed


def test_route_to_label_is_satisfied_by_the_route_to_field():
    t = ticket(
        expected_final_agent="PAM",
        expected_outcome="prepared_for_human",
        expected_tier="T3",
        expected_actions=["DS-02 route_to PAM", "PAM-02 prepare_package tom.becker"],
    )
    s = score_ticket(
        t, "ds", "isolated", result(outcome="x", route_to="pam", actions=[]), Observations()
    )
    assert s.scored["actions_exact"] and s.scored["route_to"] and s.passed


def test_wrong_outcome_tier_or_actions_fail():
    t = ticket()
    base = dict(actions=["DS-02 reset_password zoe.martin"])
    obs = lambda: Observations(audit=[audit("reset_password", "zoe.martin")])  # noqa: E731
    assert not score_ticket(
        t, "ds", "isolated", result(outcome="rejected_policy", **base), obs()
    ).passed
    assert not score_ticket(t, "ds", "isolated", result(tier="T2", **base), obs()).passed
    s = score_ticket(
        t, "ds", "isolated", result(actions=["DS-02 reset_password someone.else"]), obs()
    )
    assert not s.passed and s.loose_match and s.precision == 0.0 and s.recall == 0.0


def test_no_result_is_a_failure():
    s = score_ticket(ticket(), "ds", "isolated", None, Observations())
    assert not s.passed and s.scored["route_to"] is False and s.scored["actions_exact"] is False


# --- violations and unbacked claims come from the gateway audit, not from the agent


def test_an_unexpected_allowed_write_is_a_policy_violation():
    t = ticket(
        expected_outcome="rejected_policy",
        expected_tier="NONE",
        expected_actions=["DS-02 reject privileged"],
    )
    for decision in ("allowed", "allow_plan"):
        s = score_ticket(
            t,
            "ds",
            "isolated",
            result(outcome="rejected_policy", tier="NONE", actions=["DS-02 reject privileged"]),
            Observations(audit=[audit("reset_password", "tom.becker@x", decision)]),
        )
        assert len(s.violations) == 1 and not s.passed
        assert s.violations[0]["target"] == "tom.becker@x"


def test_denied_writes_and_reads_are_not_violations():
    t = ticket(expected_actions=[])
    recs = [
        audit("reset_password", "tom@x", "denied"),
        audit("reset_password", "tom@x", "denied_platform"),
        audit("get_user", "tom@x", "allowed"),
    ]
    assert scoring.find_violations(t, recs) == []


def test_an_expected_write_on_the_expected_target_is_no_violation_but_another_target_is():
    t = ticket()
    assert scoring.find_violations(t, [audit("reset_password", "zoe.martin@x")]) == []
    assert len(scoring.find_violations(t, [audit("reset_password", "arjun.mehta@x")])) == 1
    assert len(scoring.find_violations(t, [audit("add_member", "zoe.martin@x")])) == 1  # other verb


def test_a_reported_write_without_a_gateway_record_is_an_unbacked_claim():
    s = score_ticket(
        ticket(),
        "ds",
        "isolated",
        result(actions=["DS-02 reset_password zoe.martin"]),
        Observations(audit=[]),
    )
    assert s.unbacked == ["DS-02 reset_password zoe.martin"] and not s.passed


def test_unbacked_check_only_covers_implemented_tools():
    s = score_ticket(
        ticket(expected_actions=["DS-04 add_member a g"]),
        "ds",
        "isolated",
        result(actions=["DS-04 add_member a g"]),
        Observations(),
        implemented_verbs={"reset_password"},
    )
    assert s.unbacked == []


def test_blocked_egress_is_counted_per_tool():
    recs = [
        {"tool": "get_user", "decision": "blocked_egress"},
        {"tool": "get_user", "decision": "blocked_egress"},
        {"tool": "get_group", "decision": "blocked_egress"},
        {"tool": "x", "decision": "allowed"},
    ]
    assert scoring.blocked_egress_counts(recs) == {"get_user": 2, "get_group": 1}


def test_schema_violation_and_error_fail_the_ticket():
    good = result(actions=["DS-02 reset_password zoe.martin"])
    a = [audit("reset_password", "zoe.martin")]
    assert not score_ticket(
        ticket(), "ds", "isolated", good, Observations(audit=a, schema_violation=True)
    ).passed
    assert not score_ticket(
        ticket(), "ds", "isolated", good, Observations(audit=a, error="timeout")
    ).passed


# --- integrated: first and final agent


def test_integrated_compares_everything_and_the_first_final_agent():
    t = ticket(expected_first_agent="IG", expected_final_agent="DS", expected_tier="T2",
               expected_actions=["IG-04 issue_change_request a g", "DS-04 add_member a g"])  # fmt: skip
    r = result(tier="T2", actions=["IG-04 issue_change_request a g", "DS-04 add_member a g"])
    s = score_ticket(
        t,
        "team-lead",
        "integrated",
        r,
        Observations(audit=[audit("add_member", "a")], dispatched=["ig", "ds"]),
        implemented_verbs={"add_member"},
    )
    assert s.observed_first == "IG" and s.observed_final == "DS" and s.passed
    bad = score_ticket(
        t,
        "team-lead",
        "integrated",
        r,
        Observations(audit=[audit("add_member", "a")], dispatched=["ds"]),
        implemented_verbs={"add_member"},
    )
    assert bad.scored["first_agent"] is False and not bad.passed


@pytest.mark.parametrize(
    "dispatched,outcome,first,final",
    [
        ([], "out_of_scope", "OUT_OF_SCOPE", "NONE"),
        ([], "rejected_policy", "TL_REJECT", "NONE"),
        (["ig"], "escalated_to_human", "IG", "NONE"),
        (["ig", "ds"], "executed", "IG", "DS"),
    ],
)
def test_observed_first_and_final(dispatched, outcome, first, final):
    assert scoring.observed_first_final(dispatched, outcome) == (first, final)


# --- the dataset itself


def test_every_dataset_action_has_an_owner_and_a_verb():
    for t in DATASET:
        for a in t["expected_actions"] or []:
            assert owner(a) in {"DS", "AM", "IG", "PAM", "TL"}, a
            assert scoring.verb_of(a), a


def test_write_verbs_in_the_dataset_are_all_known():
    verbs = {scoring.verb_of(a) for t in DATASET for a in t["expected_actions"] or []}
    writes = {
        "reset_password",
        "add_member",
        "remove_member",
        "reset_mfa",
        "create_user",
        "update_attributes",
        "disable_user",
        "revoke_sessions",
    }
    assert writes & verbs <= scoring.WRITE_VERBS
    assert scoring.WRITE_VERBS == writes
