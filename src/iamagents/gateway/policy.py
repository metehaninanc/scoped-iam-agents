"""Policy engine: loads `policy/policy.yaml` and evaluates rule conditions deterministically.

Conditions listed under `require:` map to named predicates below. A condition without a
predicate, or a non-string condition, fails closed. Ticket text is never an input.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Identity(BaseModel):
    app: str | None = None
    tools: list[str] = []


class Rule(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    status: str | None = None
    agent: str | list[str] | None = None
    action: str | list[str] | None = None
    tier: str | None = None
    require: list[Any] = []
    on_protected_target: dict | None = None
    # `model_fields` is reserved on pydantic models, so it is read through an alias.
    field_allowlists: dict[str, list[str]] | None = Field(default=None, alias="model_fields")
    iso27001: list[str] = []

    def agents(self) -> list[str]:
        if self.agent is None:
            return []
        return [self.agent] if isinstance(self.agent, str) else list(self.agent)

    def actions(self) -> list[str]:
        if self.action is None:
            return []
        return [self.action] if isinstance(self.action, str) else list(self.action)


class AuditConfig(BaseModel):
    format: str = "jsonl"
    hash_chain: str = "sha256"
    retention_days: int = 90
    record: list[str] = []


class Policy(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    version: float | str
    tiers: dict[str, str] = {}
    global_: dict[str, Any] = Field(default_factory=dict, alias="global")
    identities: dict[str, Identity]
    rules: list[Rule]
    sod_pairs: list[list[str]] = []
    group_limits: dict[str, Any] = {}
    baselines: dict[str, list[str]] = {}
    audit: AuditConfig = AuditConfig()

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    def tools_for(self, agent: str) -> list[str]:
        identity = self.identities.get(agent)
        return list(identity.tools) if identity else []

    def rule_for(self, agent: str, action: str) -> Rule | None:
        for rule in self.rules:
            if rule.status in ("retired", "deferred"):
                continue
            if agent in rule.agents() and action in rule.actions():
                return rule
        return None

    def field_allowlist(self, tool: str) -> list[str] | None:
        for rule in self.rules:
            if rule.field_allowlists and tool in rule.field_allowlists:
                return rule.field_allowlists[tool]
        return None


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class CallContext:
    """Facts a predicate may use. All come from gateway-held records, never from ticket text."""

    agent: str
    ticket_id: str
    requester: str
    target: str | None
    verification_exists: Callable[[str, str], bool]
    protection: Callable[[str], "ProtectionResult"]


@dataclass(frozen=True)
class ProtectionResult:
    protected: bool
    reason: str  # none | rmau | active_role | lookup_failed


def _requester_is_target(ctx: CallContext) -> Check:
    ok = ctx.target is not None and ctx.requester.lower() == ctx.target.lower()
    return Check("requester_is_target", ok)


def _verification_record(ctx: CallContext) -> Check:
    # The record must be for the requester. A record for anyone else does not count.
    return Check("verification_record", ctx.verification_exists(ctx.ticket_id, ctx.requester))


def _target_not_protected(ctx: CallContext) -> Check:
    if ctx.target is None:
        return Check("target_not_protected", False, "no_target")
    result = ctx.protection(ctx.target)
    return Check("target_not_protected", not result.protected, result.reason)


PREDICATES: dict[str, Callable[[CallContext], Check]] = {
    "requester_is_target": _requester_is_target,
    "verification_record": _verification_record,
    "target_not_protected": _target_not_protected,
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    rule_id: str | None
    tier: str | None
    reason: str
    iso27001: tuple[str, ...] = ()
    route_to: str | None = None
    checks: tuple[Check, ...] = field(default=())


def evaluate(rule: Rule, ctx: CallContext) -> Decision:
    """Evaluate every `require:` condition. All must pass; unknown conditions fail closed."""
    checks: list[Check] = []
    for cond in rule.require:
        pred = PREDICATES.get(cond) if isinstance(cond, str) else None
        if pred is None:
            checks.append(Check(str(cond), False, "unknown_condition"))
        else:
            checks.append(pred(ctx))
    failed = [c for c in checks if not c.passed]
    iso = tuple(rule.iso27001)
    if not failed:
        return Decision(True, rule.id, rule.tier, "allowed", iso, None, tuple(checks))

    route_to = None
    by_name = {c.name: c for c in checks}
    protected_failed = (
        "target_not_protected" in by_name and not by_name["target_not_protected"].passed
    )
    if protected_failed and by_name.get("requester_is_target", Check("", True)).passed:
        if rule.on_protected_target:
            route_to = rule.on_protected_target.get("route")
    tier = (rule.on_protected_target or {}).get("tier") if route_to else rule.tier
    first = failed[0]
    reason = first.name
    if first.name == "target_not_protected" and first.detail == "lookup_failed":
        reason = "protection_lookup_failed"
    return Decision(False, rule.id, tier, reason, iso, route_to, tuple(checks))
