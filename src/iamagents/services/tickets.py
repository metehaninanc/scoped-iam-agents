"""Ticket store. Filled from the dataset by the harness; agents never write it."""

import json
from pathlib import Path

from .db import Database


class TicketStore:
    def __init__(self, db: Database):
        self.db = db

    def load_dataset(self, path: str | Path) -> int:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in lines if line.strip()]
        with self.db.conn:
            self.db.conn.execute("DELETE FROM tickets")
            self.db.conn.executemany(
                "INSERT INTO tickets (ticket_id, split, body_json) VALUES (?, ?, ?)",
                [(r["ticket_id"], r["split"], json.dumps(r)) for r in rows],
            )
        return len(rows)

    def get(self, ticket_id: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT body_json FROM tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        return json.loads(row["body_json"]) if row else None

    def list(self, split: str | None = None) -> list[dict]:
        sql = "SELECT body_json FROM tickets"
        args: tuple = ()
        if split:
            sql += " WHERE split = ?"
            args = (split,)
        rows = self.db.conn.execute(sql + " ORDER BY ticket_id", args).fetchall()
        return [json.loads(r["body_json"]) for r in rows]
