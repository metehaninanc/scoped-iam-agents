"""Mock approval store. Written by the approval portal or fixtures only, never by an agent.

The store keeps what was recorded and nothing else. Whether a record is a valid approval
(right approver, not self, in scope) is a policy decision made by the gateway.
"""

from .db import Database


class ApprovalStore:
    def __init__(self, db: Database):
        self.db = db

    def record(
        self, ticket_id: str, approver_upn: str, scope: str, decision: str, created_at: str
    ) -> None:
        with self.db.conn:
            self.db.conn.execute(
                "INSERT INTO approvals (ticket_id, approver_upn, scope, decision, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (ticket_id, approver_upn, scope, decision, created_at),
            )

    def get(self, ticket_id: str, scope: str | None = None) -> list[dict]:
        sql = (
            "SELECT ticket_id, approver_upn, scope, decision, created_at"
            " FROM approvals WHERE ticket_id = ?"
        )
        args: list = [ticket_id]
        if scope is not None:
            sql += " AND scope = ?"
            args.append(scope)
        return [dict(r) for r in self.db.conn.execute(sql + " ORDER BY id", args)]
