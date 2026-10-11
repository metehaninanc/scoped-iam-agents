"""Out-of-band delivery. Secrets (temporary passwords, TAPs) are stored here and nowhere else.

`send` returns only a message id. `metadata` never includes the secret. `open_message` models
the human recipient reading their mail and is for tests and the harness, never for a tool.
"""

from .db import Database


class Outbox:
    def __init__(self, db: Database):
        self.db = db

    def send(
        self, ticket_id: str, recipient_upn: str, kind: str, secret: str, created_at: str
    ) -> int:
        with self.db.conn:
            cur = self.db.conn.execute(
                "INSERT INTO outbox (ticket_id, recipient_upn, kind, secret, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (ticket_id, recipient_upn, kind, secret, created_at),
            )
        return cur.lastrowid

    def metadata(self, ticket_id: str) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT id, ticket_id, recipient_upn, kind, created_at FROM outbox"
            " WHERE ticket_id = ? ORDER BY id",
            (ticket_id,),
        )
        return [dict(r) for r in rows]

    def open_message(self, message_id: int) -> dict | None:
        row = self.db.conn.execute("SELECT * FROM outbox WHERE id = ?", (message_id,)).fetchone()
        return dict(row) if row else None
