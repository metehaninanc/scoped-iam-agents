"""Change requests issued by IG and consumed by DS (DS-04)."""

from .db import Database


class ChangeRequestStore:
    def __init__(self, db: Database):
        self.db = db

    def issue(
        self, ticket_id: str, user_upn: str, group_name: str, issued_by: str, created_at: str
    ) -> int:
        with self.db.conn:
            cur = self.db.conn.execute(
                "INSERT INTO change_requests"
                " (ticket_id, user_upn, group_name, issued_by, created_at) VALUES (?, ?, ?, ?, ?)",
                (ticket_id, user_upn, group_name, issued_by, created_at),
            )
        return cur.lastrowid

    def find_issued(self, ticket_id: str, user_upn: str, group_name: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM change_requests WHERE ticket_id = ? AND user_upn = ?"
            " AND group_name = ? AND state = 'issued' ORDER BY id LIMIT 1",
            (ticket_id, user_upn, group_name),
        ).fetchone()
        return dict(row) if row else None

    def consume(self, change_request_id: int) -> None:
        with self.db.conn:
            self.db.conn.execute(
                "UPDATE change_requests SET state = 'consumed' WHERE id = ?",
                (change_request_id,),
            )

    def list(self, ticket_id: str) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM change_requests WHERE ticket_id = ? ORDER BY id", (ticket_id,)
        )
        return [dict(r) for r in rows]
