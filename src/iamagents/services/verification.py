"""Identity verification records. Written by the AM `verify_identity` tool or by a fixture."""

from .db import Database


class VerificationStore:
    def __init__(self, db: Database):
        self.db = db

    def record(self, ticket_id: str, subject_upn: str, method: str, created_at: str) -> None:
        with self.db.conn:
            self.db.conn.execute(
                "INSERT INTO verifications (ticket_id, subject_upn, method, created_at)"
                " VALUES (?, ?, ?, ?)",
                (ticket_id, subject_upn, method, created_at),
            )

    def has_record(self, ticket_id: str, subject_upn: str) -> bool:
        row = self.db.conn.execute(
            "SELECT 1 FROM verifications WHERE ticket_id = ? AND subject_upn = ?",
            (ticket_id, subject_upn),
        ).fetchone()
        return row is not None
