"""SQLite connection and schema shared by all mock services."""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY,
    split TEXT NOT NULL,
    body_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hr_events (
    event_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    approver_upn TEXT NOT NULL,
    scope TEXT NOT NULL,
    decision TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    subject_upn TEXT NOT NULL,
    method TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS change_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    user_upn TEXT NOT NULL,
    group_name TEXT NOT NULL,
    issued_by TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'issued',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pam_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    target_upn TEXT NOT NULL,
    approver_upn TEXT NOT NULL,
    executor_upn TEXT NOT NULL,
    state TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    recipient_upn TEXT NOT NULL,
    kind TEXT NOT NULL,
    secret TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE TABLE IF NOT EXISTS fixture_loads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    loaded_at TEXT NOT NULL
);
"""

# Tables that hold per-ticket state and are wiped between tickets. `tickets` is kept.
PER_TICKET_TABLES = (
    "hr_events",
    "approvals",
    "verifications",
    "change_requests",
    "pam_requests",
    "outbox",
    "fixture_loads",
)


class Database:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def clear_per_ticket_state(self) -> None:
        with self.conn:
            for table in PER_TICKET_TABLES:
                self.conn.execute(f"DELETE FROM {table}")  # fixed table names, no user input

    def close(self) -> None:
        self.conn.close()
