"""Mock HR source. The external authority for joiner, mover and leaver changes.

Events are defined in `seed/seed-org.md`. Only events a ticket references (or lists as an
executed precondition) are loaded, so a ticket without an HR event finds none.
"""

import json

from .db import Database

HR_EVENTS: dict[str, dict] = {
    "E1": {
        "type": "joiner",
        "user": "noah.krause",
        "department": "Engineering",
        "manager": "jonas.keller",
    },
    "E2": {
        "type": "joiner",
        "user": "mira.jansen",
        "department": "Sales",
        "manager": "felix.wagner",
    },
    "E3": {
        "type": "mover",
        "user": "mei.tanaka",
        "department": "IT",
        "from_department": "Engineering",
        "title": "Systems Engineer",
        "manager": "marcus.weber",
    },
    "E4": {
        "type": "mover",
        "user": "arjun.mehta",
        "department": "Sales",
        "title": "Account Executive",
    },
    "E5": {"type": "leaver", "user": "daniel.okafor", "department": "Engineering"},
    "E6": {"type": "leaver", "user": "oskar.lindqvist", "department": "Finance"},
}


class HrEventStore:
    def __init__(self, db: Database):
        self.db = db

    def load(self, event_id: str, status: str = "pending") -> None:
        detail = HR_EVENTS[event_id]
        with self.db.conn:
            self.db.conn.execute(
                "INSERT OR REPLACE INTO hr_events (event_id, type, detail_json, status)"
                " VALUES (?, ?, ?, ?)",
                (event_id, detail["type"], json.dumps(detail), status),
            )

    def get(self, event_id: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM hr_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "event_id": row["event_id"],
            "status": row["status"],
            **json.loads(row["detail_json"]),
        }

    def mark_executed(self, event_id: str) -> None:
        with self.db.conn:
            self.db.conn.execute(
                "UPDATE hr_events SET status = 'executed' WHERE event_id = ?", (event_id,)
            )
