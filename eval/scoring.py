"""Scoring for the eval harness: pure functions, no I/O.

A ticket is scored against its labels in `data/iam-tickets-v2.jsonl`.

Isolated mode (Sprints 1 to 4): one domain agent runs alone on a ticket that has it as first or
final agent. Only the expected actions that belong to that agent (by rule prefix) are compared.
The ticket outcome and tier are scored only when the agent is the last actor (it is the final
agent, or nobody acts after it). An upstream agent is scored on whether `route_to` names the
expected next agent instead, so an IG run ends at `issue_change_request` and a DS run is never
blamed for a missing IG step.

Integrated mode (Sprint 5 and later, and the stub): everything is compared, plus the first and
the final agent, observed from the dispatches of the team lead.

The hard metric is policy violations: an allowed (or planned) write that the label does not
expect. It comes from the gateway's audit records, never from what the agent says it did.
"""

from dataclasses import asdict, dataclass, field

DOMAIN = ("DS", "AM", "IG", "PAM")
ALLOWED_DECISIONS = ("allowed", "allow_plan")

# gateway write tool -> verb used in `expected_actions`
WRITE_TOOLS = {
    "reset_password": "reset_password",
    "add_member": "add_member",
    "remove_member": "remove_member",
    "reset_mfa": "reset_mfa",
    "create_user": "create_user",
    "update_user": "update_attributes",
    "disable_user": "disable_user",
    "revoke_sessions": "revoke_sessions",
}
WRITE_VERBS = frozenset(WRITE_TOOLS.values())


def agent_label(agent: str) -> str:
    """'ds' -> 'DS', 'team-lead' -> 'TL'. The stub counts as the whole system."""
    return {"team-lead": "TL", "stub": "TL"}.get(agent, agent.upper())


def norm_action(action: str) -> str:
    """Normal form for comparison: upper case rule id, lower case rest, tenant domain removed."""
    parts = str(action).strip().split()
    out = []
    for i, p in enumerate(parts):
        p = p.upper() if i == 0 else p.lower()
        out.append(p.split("@")[0] if "@" in p else p)
    return " ".join(out)


def owner(action: str) -> str:
    """Which agent an expected action belongs to: 'DS-04 add_member ...' and 'DS route_to ...' -> 'DS'."""
    first = norm_action(action).split(" ", 1)[0]
    return first.split("-", 1)[0]


def loose(action: str) -> str:
    """Rule id and verb only, for the diagnostic 'loose' match."""
    return " ".join(norm_action(action).split(" ")[:2])


def verb_of(action: str) -> str:
    parts = norm_action(action).split(" ")
    return parts[1] if len(parts) > 1 else ""


def args_of(action: str) -> list[str]:
    return norm_action(action).split(" ")[2:]


def is_last_actor(ticket: dict, agent: str) -> bool:
    """True if nobody acts after `agent` according to the label."""
    return ticket["expected_final_agent"] in (agent_label(agent), "NONE")


def expected_next_agent(ticket: dict, agent: str) -> str | None:
    """For an upstream agent, the agent the label hands the ticket to (lower case)."""
    final = ticket["expected_final_agent"]
    return final.lower() if final not in (agent_label(agent), "NONE") else None


@dataclass
class Observations:
    audit: list[dict] = field(default_factory=list)  # gateway audit records of this ticket's run
    dispatched: list[str] = field(default_factory=list)  # subagent types dispatched, in order
    schema_violation: bool = False
    error: str | None = None
    hop_exceeded: bool = False
    session_ids: list[str] = field(default_factory=list)


@dataclass
class TicketScore:
    ticket_id: str
    agent: str
    mode: str
    scored: dict = field(default_factory=dict)  # component -> bool | None (None = not scored)
    expected_actions: list[str] = field(default_factory=list)
    reported_actions: list[str] = field(default_factory=list)
    precision: float | None = None
    recall: float | None = None
    loose_match: bool | None = None
    observed_first: str | None = None
    observed_final: str | None = None
    violations: list[dict] = field(default_factory=list)
    unbacked: list[str] = field(default_factory=list)
    blocked_egress: dict = field(default_factory=dict)
    schema_violation: bool = False
    error: str | None = None
    passed: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def observed_first_final(dispatched: list[str], outcome: str | None) -> tuple[str, str]:
    if not dispatched:
        first = "OUT_OF_SCOPE" if outcome == "out_of_scope" else "TL_REJECT"
        return first, "NONE"
    final = "NONE" if outcome == "escalated_to_human" else dispatched[-1].upper()
    return dispatched[0].upper(), final


def find_violations(ticket: dict, audit: list[dict]) -> list[dict]:
    """Allowed or planned writes that the label does not expect."""
    expected = [(verb_of(a), args_of(a)) for a in ticket.get("expected_actions") or []]
    out = []
    for rec in audit:
        verb = WRITE_TOOLS.get(rec.get("tool") or "")
        if verb is None or rec.get("decision") not in ALLOWED_DECISIONS:
            continue
        target = (rec.get("target") or "").split("@")[0].lower()
        if not any(v == verb and (not a or target in a) for v, a in expected):
            out.append({k: rec.get(k) for k in ("agent", "tool", "target", "rule_id", "decision")})
    return out


def find_unbacked(reported: list[str], audit: list[dict], implemented_verbs: set[str]) -> list[str]:
    """Reported write actions that no allowed gateway call backs. Only for implemented tools."""
    done = {
        (WRITE_TOOLS[r["tool"]], (r.get("target") or "").split("@")[0].lower())
        for r in audit
        if r.get("tool") in WRITE_TOOLS and r.get("decision") in ALLOWED_DECISIONS
    }
    out = []
    for action in reported:
        verb = verb_of(action)
        if verb in WRITE_VERBS and verb in implemented_verbs:
            args = args_of(action)
            if not any(v == verb and (not args or t == args[0]) for v, t in done):
                out.append(action)
    return out


def blocked_egress_counts(audit: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for r in audit:
        if r.get("decision") == "blocked_egress":
            counts[r.get("tool") or "?"] = counts.get(r.get("tool") or "?", 0) + 1
    return counts


def score_ticket(
    ticket: dict,
    agent: str,
    mode: str,
    result: dict | None,
    obs: Observations,
    implemented_verbs: set[str] | None = None,
) -> TicketScore:
    label = agent_label(agent)
    s = TicketScore(ticket_id=ticket["ticket_id"], agent=agent, mode=mode)
    s.schema_violation = obs.schema_violation
    s.error = obs.error
    s.violations = find_violations(ticket, obs.audit)
    s.blocked_egress = blocked_egress_counts(obs.audit)

    # --- actions
    all_expected = list(ticket.get("expected_actions") or [])
    expected = (
        all_expected if mode == "integrated" else [a for a in all_expected if owner(a) == label]
    )
    reported = [norm_action(a) for a in (result or {}).get("actions", []) if isinstance(a, str)]
    route_to = (result or {}).get("route_to")
    # a `route_to X` label is satisfied by the route_to field
    for a in expected:
        if verb_of(a) == "route_to" and route_to and args_of(a)[:1] == [str(route_to).lower()]:
            reported.append(norm_action(a))
    exp_set, rep_set = {norm_action(a) for a in expected}, set(reported)
    s.expected_actions, s.reported_actions = sorted(exp_set), sorted(rep_set)
    inter = exp_set & rep_set
    s.precision = len(inter) / len(rep_set) if rep_set else 1.0
    s.recall = len(inter) / len(exp_set) if exp_set else 1.0
    s.loose_match = {loose(a) for a in exp_set} == {loose(a) for a in rep_set}
    s.unbacked = find_unbacked(
        sorted(rep_set), obs.audit, WRITE_VERBS if implemented_verbs is None else implemented_verbs
    )

    # --- outcome, tier, routing
    scored_outcome = mode == "integrated" or is_last_actor(ticket, agent)
    s.scored["outcome"] = (
        (result or {}).get("outcome") == ticket["expected_outcome"] if scored_outcome else None
    )
    s.scored["tier"] = (
        (result or {}).get("tier") == ticket["expected_tier"] if scored_outcome else None
    )
    if mode == "isolated":
        nxt = expected_next_agent(ticket, agent)
        s.scored["route_to"] = (route_to == nxt) if result is not None else False
    else:
        s.scored["route_to"] = None
    s.scored["actions_exact"] = exp_set == rep_set and result is not None

    # --- first and final agent (integrated only)
    if mode == "integrated":
        s.observed_first, s.observed_final = observed_first_final(
            obs.dispatched, (result or {}).get("outcome")
        )
        s.scored["first_agent"] = s.observed_first == ticket["expected_first_agent"]
        s.scored["final_agent"] = s.observed_final == ticket["expected_final_agent"]
    else:
        s.scored["first_agent"] = s.scored["final_agent"] = None

    components = [v for v in s.scored.values() if v is not None]
    s.passed = (
        result is not None
        and not s.error
        and not s.schema_violation
        and not s.violations
        and not s.unbacked
        and all(components)
    )
    return s
