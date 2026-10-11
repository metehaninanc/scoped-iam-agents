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
from collections.abc import Callable
from datetime import UTC, datetime

from ..services.mock import MockServices
from .audit import AuditLog
from .config import Settings
from .graph.base import GraphClient, GraphClientFactory
from .policy import Policy
from .protection import ProtectionChecker
from .tools import TOOLS, Outcome, ToolDef, deny

log = logging.getLogger("iamagents.gateway")


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
        self.policy = policy
        self.services = services
        self.graph_factory = graph_factory
        self.audit = audit
        self.protection = protection or ProtectionChecker(settings.protection_ttl_seconds)
        self.now = now
        self.active_ticket: str | None = None
        self.intended_calls: list[dict] = []
        self._graphs: dict[str, GraphClient] = {}

    # --- admin path (not exposed over MCP) ---

    def bind_ticket(self, ticket_id: str) -> None:
        self.active_ticket = ticket_id
        self.intended_calls = []
        self._record("harness", None, "bind_ticket", "admin", "bound", ticket_id=ticket_id)

    def unbind_ticket(self) -> None:
        previous = self.active_ticket
        self.active_ticket = None
        self._record("harness", None, "unbind_ticket", "admin", "unbound", ticket_id=previous)

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
