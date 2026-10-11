"""In-memory registry of secrets this process generated, used only to catch leaks.

The `Secret` wrapper is the primary control. The registry is a second layer: any string that
contains a registered value is blocked from tool results, redacted from audit records and
redacted from log records. Values live in memory only and are never written anywhere.
"""

import logging
import threading
from typing import Any

MIN_LENGTH = 8  # shorter values would cause false matches
MAX_ENTRIES = 1000
REDACTED = "[REDACTED]"


class SecretRegistry:
    def __init__(self):
        self._values: list[str] = []
        self._lock = threading.Lock()

    def register(self, value: str) -> None:
        if len(value) < MIN_LENGTH:
            return
        with self._lock:
            if value not in self._values:
                self._values.append(value)
                del self._values[:-MAX_ENTRIES]

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def contains(self, text: str) -> bool:
        with self._lock:
            return any(v in text for v in self._values)

    def redact(self, text: str) -> str:
        with self._lock:
            for v in self._values:
                text = text.replace(v, REDACTED)
        return text

    def redact_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact_obj(v) for v in obj]
        return obj


SECRETS = SecretRegistry()

_installed = False


def install_log_redaction(registry: SecretRegistry = SECRETS) -> None:
    """Redact registered secrets in every log record, at record creation. Idempotent."""
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:
            return record
        if registry.contains(message):
            record.msg = registry.redact(message)
            record.args = ()
        return record

    logging.setLogRecordFactory(factory)
    _installed = True
