"""The gateway: authenticates nothing itself (the transport does), binds a ticket, runs one tool.

Order of checks for every call, each audited when it fails:
1. the tool is on the calling agent's list (from `policy.yaml`) and implemented
2. a ticket is bound and `ticket_id` equals it (`ticket_mismatch` otherwise)
3. the ticket exists in the ticket store
4. the tool handler runs policy, then Graph

The active ticket is set only through `bind_ticket`, a Python call used by the harness and the
admin endpoint in `admin.py`. It is not an MCP tool, so a model cannot change it.
"""

import logging
import secrets as _secrets
from collections.abc import Callable
from datetime import UTC, datetime

from ..services.mock import MockServices
from .audit import AuditLog
from .auth import TokenRegistry
from .config import Settings
from .egress import EgressGuard
from .graph.base import GraphClient, GraphClientFactory
from .policy import Policy
from .protection import ProtectionChecker
from .secrets_registry import SECRETS, install_log_redaction
from .tools import TOOLS, Outcome, ToolDef, deny

log = logging.getLogger("iamagents.gateway")

DOMAIN_AGENTS = ("ds", "am", "ig", "pam")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Gateway:
    def __init__(
        self,
        settings: Settings,
        policy: Policy,
        services: MockServices,
        graph_factory: GraphClientFactory,
        audit: AuditLog,
        protection: ProtectionChecker | None = None,
        now: Callable[[], str] = _utc_now,
    ):
        self.settings = settings
        self.registry = TokenRegistry(settings.tokens)
        self.run_token_values: set[str] = (
            set()
        )  # every run token issued; for the report secret scan
        self.policy = policy
        self.services = services
        self.graph_factory = graph_factory
        self.audit = audit
        self.protection = protection or ProtectionChecker(settings.protection_ttl_seconds)
        self.now = now
        self.secrets = SECRETS
        self.egress = EgressGuard(self.secrets)
        install_log_redaction(self.secrets)
        self.active_ticket: str | None = None
        self.intended_calls: list[dict] = []
        # Domain agent identities that acted on the active ticket, in order of first call.
        self.ticket_agents: list[str] = []
        self.hop_exceeded = False
        self._graphs: dict[str, GraphClient] = {}

    # --- admin path (not exposed over MCP) ---

    def bind_ticket(self, ticket_id: str) -> None:
        self.active_ticket = ticket_id
        self.intended_calls = []
        self.ticket_agents = []
        self.hop_exceeded = False
        self._record("harness", None, "bind_ticket", "admin", "bound", ticket_id=ticket_id)

    def unbind_ticket(self) -> None:
        previous = self.active_ticket
        self.active_ticket = None
        self._record("harness", None, "unbind_ticket", "admin", "unbound", ticket_id=previous)

    @property
    def max_domain_agents(self) -> int:
        return int(self.policy.global_.get("reroute_limit", 2)) + 1

    def close_allowed(self) -> tuple[bool, str]:
        """Checked by `close_ticket` (Sprint 5): a ticket over the hop limit is never closed."""
        if self.hop_exceeded:
            return False, "hop_limit_exceeded"
        return True, "ok"

    def issue_run_tokens(self, agents: tuple[str, ...] = ("ds", "am", "ig", "pam", "tl")) -> dict:
        """Fresh random tokens for one run, as {GW_TOKEN_<AGENT>: value}. They replace all
        earlier tokens, and `revoke_run_tokens` ends them: a token that leaks dies with its run."""
        tokens = {a: _secrets.token_urlsafe(32) for a in agents}
        self.registry.replace(tokens)
        for value in tokens.values():
            self.run_token_values.add(value)
            self.secrets.register(value)  # blocked in results, redacted in audit records and logs
        self._record("harness", None, "issue_run_tokens", "admin", f"agents={','.join(agents)}")
        return {f"GW_TOKEN_{a.upper()}": v for a, v in tokens.items()}

    def revoke_run_tokens(self) -> None:
        self.registry.clear()
        self._record("harness", None, "revoke_run_tokens", "admin", "revoked")

    def set_graph_factory(self, factory) -> None:
        """Swap the Graph backend, for example a fresh fake directory per ticket."""
        self.graph_factory = factory
        self._graphs = {}

    # --- tools ---

    def graph_for(self, agent: str) -> GraphClient:
        if agent not in self._graphs:
            self._graphs[agent] = self.graph_factory.for_agent(agent)
        return self._graphs[agent]

    def tools_for(self, agent: str) -> list[ToolDef]:
        """Policy list intersected with the tools implemented so far."""
        return [TOOLS[name] for name in self.policy.tools_for(agent) if name in TOOLS]

    def call(self, agent: str, tool: str, args: dict | None) -> dict:
        args = args or {}
        ticket_id = args.get("ticket_id") if isinstance(args.get("ticket_id"), str) else None
        log.debug("call agent=%s tool=%s ticket=%s", agent, tool, ticket_id)
        outcome = self._dispatch(agent, tool, args, ticket_id)
        violations = self.egress.scan(outcome.result)
        if violations:
            # A tool returned something it must not. Fail closed: block the whole result.
            log.error("egress blocked tool=%s kinds=%s", tool, sorted({v.kind for v in violations}))
            outcome = Outcome(
                {
                    "ok": False,
                    "decision": "denied",
                    "reason": "internal_error",
                    "message": "Internal error.",
                },
                "blocked_egress",
                outcome.rule_id,
                "; ".join(sorted({str(v) for v in violations})),
                outcome.iso27001,
                outcome.target,
                outcome.graph_request_id,
                outcome.tier,
            )
        self._record(
            agent,
            outcome.rule_id,
            tool,
            outcome.decision,
            outcome.reason,
            ticket_id=ticket_id,
            iso27001=outcome.iso27001,
            target=outcome.target,
            graph_request_id=outcome.graph_request_id,
        )
        log.debug(
            "outcome agent=%s tool=%s decision=%s reason=%s",
            agent,
            tool,
            outcome.decision,
            outcome.reason,
        )
        return outcome.result

    def _dispatch(self, agent: str, tool: str, args: dict, ticket_id: str | None) -> Outcome:
        allowed_names = {t.name for t in self.tools_for(agent)}
        if tool not in allowed_names:
            return deny("tool_not_permitted", f"{tool} is not available to this agent.")
        if ticket_id is None:
            return deny("invalid_arguments", "Missing or invalid argument: ticket_id.")
        if self.active_ticket is None:
            return deny("no_active_ticket", "No ticket is active.")
        if ticket_id != self.active_ticket:
            return deny("ticket_mismatch", "ticket_id does not match the active ticket.")
        if agent in DOMAIN_AGENTS and agent not in self.ticket_agents:
            # The hop_limit hook is not the boundary, so the gateway enforces the same limit:
            # first domain agent plus `reroute_limit` reroutes means at most 3 identities.
            if len(self.ticket_agents) >= self.max_domain_agents:
                self.hop_exceeded = True
                return deny(
                    "hop_limit_exceeded", "Too many agents have handled this ticket; escalate."
                )
            self.ticket_agents.append(agent)
        ticket = self.services.tickets.get(ticket_id)
        if ticket is None:
            return deny("unknown_ticket", "The active ticket does not exist.")
        try:
            return TOOLS[tool].handler(self, agent, ticket, args)
        except Exception as exc:  # fail closed; log the type only, never arguments or messages
            log.error("tool %s failed: %s", tool, type(exc).__name__)
            return Outcome(
                {
                    "ok": False,
                    "decision": "denied",
                    "reason": "internal_error",
                    "message": "Internal error.",
                },
                "error",
                None,
                "internal_error",
            )

    # --- retention ---

    def run_retention(self) -> dict:
        """Purge expired audit records and wipe undelivered outbox secrets past their TTL."""
        days = self.settings.retention_days or self.policy.audit.retention_days
        now = self.now()
        purged = self.audit.purge(days, datetime.fromisoformat(now))
        expired = self.services.outbox.expire(now, self.settings.outbox_ttl_hours)
        return {"audit_purged": purged, "outbox_expired": expired, "retention_days": days}

    # --- audit ---

    def audit_auth_failure(self, reason: str) -> None:
        self._record("unauthenticated", None, None, "denied_auth", reason)

    def _record(
        self,
        agent: str,
        rule_id: str | None,
        tool: str | None,
        decision: str,
        reason: str,
        *,
        ticket_id: str | None = None,
        iso27001: tuple[str, ...] = (),
        target: str | None = None,
        graph_request_id: str | None = None,
    ) -> None:
        self.audit.append(
            {
                "ts": self.now(),
                "agent": agent,
                "ticket_id": ticket_id,
                "rule_id": rule_id,
                "tool": tool,
                "decision": decision,
                "reason": reason,
                "graph_request_id": graph_request_id,
                "iso27001": list(iso27001),
                "mode": self.settings.mode,
                "target": target,
            }
        )
