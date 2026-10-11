"""Hash-chained audit log (JSONL). Evidence for ISO/IEC 27001 A.8.15 and A.8.16.

Each record carries the sha256 of the previous record, so editing, removing or reordering a
line breaks the chain. Cutting records off the tail is not detectable from the file alone; the
optional hash anchoring in Sprint 6 covers that. The log stores identifiers, never ticket text.
"""

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path

GENESIS = "0" * 64


def _canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(prev_hash: str, record: dict) -> str:
    return hashlib.sha256((prev_hash + _canonical(record)).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    records: int
    error: str | None = None
    line: int | None = None


def verify_chain(path: str | Path) -> VerifyResult:
    p = Path(path)
    if not p.exists():
        return VerifyResult(True, 0)
    prev = GENESIS
    count = 0
    for n, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            return VerifyResult(False, count, "unparseable record", n)
        stored = entry.pop("hash", None)
        if entry.get("prev_hash") != prev:
            return VerifyResult(False, count, "chain broken: prev_hash mismatch", n)
        if stored != _digest(prev, entry):
            return VerifyResult(False, count, "record changed: hash mismatch", n)
        prev = stored
        count += 1
    return VerifyResult(True, count)


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last = self._read_last_hash()

    def _read_last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        last = GENESIS
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                last = json.loads(raw)["hash"]
        return last

    def append(self, record: dict) -> dict:
        with self._lock:
            entry = dict(record)
            entry["prev_hash"] = self._last
            entry["hash"] = _digest(self._last, entry)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(_canonical(entry) + "\n")
            self._last = entry["hash"]
            return entry

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [
            json.loads(r) for r in self.path.read_text(encoding="utf-8").splitlines() if r.strip()
        ]
