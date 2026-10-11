"""Out-of-band delivery. Secrets (temporary passwords, TAPs) are stored here and nowhere else.

Lifecycle of a secret: `send` stores it as `pending` and returns only a message id. The human
recipient reads it through `open_message`, which returns it once and wipes it (`delivered`).
A secret nobody reads is wiped by `expire` after the TTL (`expired`). `metadata` never
includes the secret. `open_message` models the recipient reading their mail and is for tests
and the harness, never for a tool.
"""

from datetime import UTC, datetime, timedelta

from .db import Database


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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
            "SELECT id, ticket_id, recipient_upn, kind, state, created_at, delivered_at"
            " FROM outbox WHERE ticket_id = ? ORDER BY id",
            (ticket_id,),
        )
        return [dict(r) for r in rows]

    def open_message(self, message_id: int, delivered_at: str | None = None) -> dict | None:
        """Deliver once: return the message with its secret, then wipe the secret."""
        row = self.db.conn.execute("SELECT * FROM outbox WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            return None
        message = dict(row)
        if row["state"] != "pending":
            message["secret"] = None
            return message
        with self.db.conn:
            self.db.conn.execute(
                "UPDATE outbox SET secret = '', state = 'delivered', delivered_at = ? WHERE id = ?",
                (delivered_at or _utc_now(), message_id),
            )
        return message

    def expire(self, now: str, ttl_hours: float) -> int:
        """Wipe pending secrets older than the TTL. Returns how many were wiped."""
        cutoff = datetime.fromisoformat(now) - timedelta(hours=ttl_hours)
        expired = [
            r["id"]
            for r in self.db.conn.execute(
                "SELECT id, created_at FROM outbox WHERE state = 'pending'"
            )
            if datetime.fromisoformat(r["created_at"]) < cutoff
        ]
        with self.db.conn:
            for message_id in expired:
                self.db.conn.execute(
                    "UPDATE outbox SET secret = '', state = 'expired' WHERE id = ?", (message_id,)
                )
        return len(expired)
