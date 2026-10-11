"""PAM request store: T3 packages with a fixed approver and executor. A human executes them."""

import json

from .db import Database


class PamRequestStore:
    def __init__(self, db: Database):
        self.db = db

    def create(
        self,
        ticket_id: str,
        kind: str,
        target_upn: str,
        approver_upn: str,
        executor_upn: str,
        state: str,
        detail: dict,
        created_at: str,
    ) -> int:
        with self.db.conn:
            cur = self.db.conn.execute(
                "INSERT INTO pam_requests (ticket_id, kind, target_upn, approver_upn,"
                " executor_upn, state, detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ticket_id,
                    kind,
                    target_upn,
                    approver_upn,
                    executor_upn,
                    state,
                    json.dumps(detail),
                    created_at,
                ),
            )
        return cur.lastrowid

    def list(self, ticket_id: str) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM pam_requests WHERE ticket_id = ? ORDER BY id", (ticket_id,)
        )
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(d)
        return out
