"""Gateway tools. Each handler runs policy first, then Graph, and returns an `Outcome`.

Only four tools exist in Sprint 0: get_user, get_group, list_group_members, reset_password.
The other tools named in `policy/policy.yaml` arrive with their sprints and stay off the tool
list until then.

Results are structured and identical in plan and live mode, so an agent behaves the same in both.
Only the audit log records the difference (decision `allow_plan` versus `allowed`).
"""

import secrets
import string
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .graph.base import GraphForbidden, GraphNotFound, Secret
from .minimise import minimise
from .policy import CallContext, evaluate

if TYPE_CHECKING:
    from .core import Gateway


@dataclass
class Outcome:
    result: dict
    decision: str  # allowed | allow_plan | denied | denied_platform | error
    rule_id: str | None = None
    reason: str = ""
    iso27001: tuple[str, ...] = ()
    target: str | None = None
    graph_request_id: str | None = None
    tier: str | None = None
    extra: dict = field(default_factory=dict)


def deny(
    reason: str,
    message: str,
    *,
    rule_id: str | None = None,
    tier: str | None = None,
    iso27001: tuple[str, ...] = (),
    route_to: str | None = None,
    target: str | None = None,
    decision: str = "denied",
) -> Outcome:
    result = {
        "ok": False,
        "decision": "denied",
        "rule_id": rule_id,
        "tier": tier,
        "reason": reason,
        "message": message,
    }
    if route_to:
        result["route_to"] = route_to
    return Outcome(result, decision, rule_id, reason, iso27001, target, None, tier)


def _text_arg(args: dict, name: str) -> str | None:
    value = args.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _platform_denial(rule, target: str | None, err: GraphForbidden) -> Outcome:
    # The message of a Graph error is not shown to the model; the audit reason is generic.
    return deny(
        "platform_refused",
        "The directory refused this operation for this identity.",
        rule_id=rule.id,
        tier=rule.tier,
        iso27001=tuple(rule.iso27001),
        target=target,
        decision="denied_platform",
    )


def _read_outcome(
    rule, data: Any, target: str | None, graph_request_id: str | None = None
) -> Outcome:
    return Outcome(
        {"ok": True, "decision": "allowed", "rule_id": rule.id, "tier": rule.tier, "data": data},
        "allowed",
        rule.id,
        "allowed",
        tuple(rule.iso27001),
        target,
        graph_request_id,
        rule.tier,
    )


def _not_found(rule, what: str, target: str) -> Outcome:
    return Outcome(
        {
            "ok": False,
            "decision": "allowed",
            "rule_id": rule.id,
            "tier": rule.tier,
            "reason": "not_found",
            "message": f"{what} not found.",
        },
        "allowed",
        rule.id,
        "not_found",
        tuple(rule.iso27001),
        target,
        None,
        rule.tier,
    )


def _invalid(name: str) -> Outcome:
    return deny("invalid_arguments", f"Missing or invalid argument: {name}.")


def _no_rule(tool: str) -> Outcome:
    return deny("no_rule", f"No policy rule covers {tool}.")


def _minimised(gw: "Gateway", tool: str, obj: dict) -> dict:
    allowlist = gw.policy.field_allowlist(tool)
    if allowlist is None:
        raise RuntimeError(f"no field allowlist for {tool}")  # fail closed -> error outcome
    return minimise(obj, allowlist)


def get_user(gw: "Gateway", agent: str, ticket: dict, args: dict) -> Outcome:
    rule = gw.policy.rule_for(agent, "get_user")
    if rule is None:
        return _no_rule("get_user")
    upn = _text_arg(args, "upn")
    if not upn:
        return _invalid("upn")
    try:
        raw = gw.graph_for(agent).get_user(upn)
    except GraphNotFound:
        return _not_found(rule, "User", upn)
    except GraphForbidden as e:
        return _platform_denial(rule, upn, e)
    return _read_outcome(rule, _minimised(gw, "get_user", raw), upn)


def get_group(gw: "Gateway", agent: str, ticket: dict, args: dict) -> Outcome:
    rule = gw.policy.rule_for(agent, "get_group")
    if rule is None:
        return _no_rule("get_group")
    name = _text_arg(args, "name")
    if not name:
        return _invalid("name")
    try:
        raw = gw.graph_for(agent).get_group(name)
    except GraphNotFound:
        return _not_found(rule, "Group", name)
    except GraphForbidden as e:
        return _platform_denial(rule, name, e)
    return _read_outcome(rule, _minimised(gw, "get_group", raw), name)


def list_group_members(gw: "Gateway", agent: str, ticket: dict, args: dict) -> Outcome:
    rule = gw.policy.rule_for(agent, "list_group_members")
    if rule is None:
        return _no_rule("list_group_members")
    name = _text_arg(args, "name")
    if not name:
        return _invalid("name")
    try:
        raw = gw.graph_for(agent).list_group_members(name)
    except GraphNotFound:
        return _not_found(rule, "Group", name)
    except GraphForbidden as e:
        return _platform_denial(rule, name, e)
    members = [_minimised(gw, "list_group_members", m) for m in raw]
    return _read_outcome(rule, {"members": members}, name)


def generate_temp_password() -> Secret:
    rng = secrets.SystemRandom()
    chars = [
        rng.choice(string.ascii_uppercase),
        rng.choice(string.ascii_lowercase),
        rng.choice(string.digits),
        rng.choice("!#$%&*+-=?@"),
    ]
    pool = string.ascii_letters + string.digits
    chars += [rng.choice(pool) for _ in range(14)]
    rng.shuffle(chars)
    return Secret("".join(chars))


def reset_password(gw: "Gateway", agent: str, ticket: dict, args: dict) -> Outcome:
    rule = gw.policy.rule_for(agent, "reset_password")
    if rule is None:
        return _no_rule("reset_password")
    target = _text_arg(args, "upn")
    if not target:
        return _invalid("upn")

    graph = gw.graph_for(agent)
    ctx = CallContext(
        agent=agent,
        ticket_id=ticket["ticket_id"],
        requester=ticket["requester_upn"],  # from the stored ticket record, not the ticket text
        target=target,
        verification_exists=gw.services.verification.has_record,
        protection=lambda upn: gw.protection.check(graph, upn),
    )
    decision = evaluate(rule, ctx)
    if not decision.allowed:
        messages = {
            "requester_is_target": "Passwords can only be reset for the requester themselves.",
            "verification_record": "No identity verification record exists for the requester.",
            "target_not_protected": "This account is privileged and must go through PAM.",
            "protection_lookup_failed": "Protection could not be checked; treated as privileged.",
        }
        return deny(
            decision.reason,
            messages.get(decision.reason, "Not allowed by policy."),
            rule_id=decision.rule_id,
            tier=decision.tier,
            iso27001=decision.iso27001,
            route_to=decision.route_to,
            target=target,
        )

    success = {
        "ok": True,
        "decision": "allowed",
        "rule_id": rule.id,
        "tier": rule.tier,
        "status": "success",
        "message": "Password reset. The temporary password was delivered out of band.",
    }
    if gw.settings.mode == "plan":
        gw.intended_calls.append(
            {
                "ticket_id": ticket["ticket_id"],
                "agent": agent,
                "tool": "reset_password",
                "rule_id": rule.id,
                "tier": rule.tier,
                "target": target,
            }
        )
        return Outcome(
            success, "allow_plan", rule.id, "allowed", tuple(rule.iso27001), target, None, rule.tier
        )

    password = generate_temp_password()
    gw.secrets.register(password.reveal())  # second layer: egress guard, log and audit redaction
    try:
        request_id = graph.reset_password(target, password)
    except GraphForbidden as e:
        return _platform_denial(rule, target, e)
    except GraphNotFound:
        return _not_found(rule, "User", target)
    gw.services.outbox.send(
        ticket["ticket_id"], target, "temporary_password", password.reveal(), gw.now()
    )
    return Outcome(
        success, "allowed", rule.id, "allowed", tuple(rule.iso27001), target, request_id, rule.tier
    )


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Outcome]


def _schema(**props: str) -> dict:
    return {
        "type": "object",
        "properties": {"ticket_id": {"type": "string", "description": "The active ticket id"}}
        | {k: {"type": "string", "description": v} for k, v in props.items()},
        "required": ["ticket_id", *props],
        "additionalProperties": False,
    }


TOOLS: dict[str, ToolDef] = {
    t.name: t
    for t in (
        ToolDef(
            "get_user",
            "Look up a user by UPN (minimised fields).",
            _schema(upn="User principal name"),
            get_user,
        ),
        ToolDef(
            "get_group",
            "Look up a group by display name.",
            _schema(name="Group display name"),
            get_group,
        ),
        ToolDef(
            "list_group_members",
            "List the members of one group (names and UPNs).",
            _schema(name="Group display name"),
            list_group_members,
        ),
        ToolDef(
            "reset_password",
            "Reset the requester's own password. The temporary password is delivered out of band.",
            _schema(upn="UPN of the account to reset"),
            reset_password,
        ),
    )
}
