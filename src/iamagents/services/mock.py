"""All mock services on one database."""

from .approvals import ApprovalStore
from .change_requests import ChangeRequestStore
from .db import Database
from .hr_events import HrEventStore
from .outbox import Outbox
from .pam_requests import PamRequestStore
from .tickets import TicketStore
from .verification import VerificationStore


class MockServices:
    def __init__(self, path: str = ":memory:"):
        self.db = Database(path)
        self.tickets = TicketStore(self.db)
        self.hr_events = HrEventStore(self.db)
        self.approvals = ApprovalStore(self.db)
        self.verification = VerificationStore(self.db)
        self.change_requests = ChangeRequestStore(self.db)
        self.pam_requests = PamRequestStore(self.db)
        self.outbox = Outbox(self.db)

    def clear(self) -> None:
        """Wipe all per-ticket state. Tickets stay loaded."""
        self.db.clear_per_ticket_state()
