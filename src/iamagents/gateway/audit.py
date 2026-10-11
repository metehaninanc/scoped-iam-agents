"""Hash-chained audit log (JSONL). Evidence for ISO/IEC 27001 A.8.15 and A.8.16.

Each record carries the sha256 of the previous record, so editing, removing or reordering a
line breaks the chain. The log stores identifiers, never ticket text, and registered secrets
are redacted from every record before it is written.

LIMIT: without an external anchor the chain detects partial or accidental edits, not a full
rewrite of the file. Someone who can rewrite the whole file can recompute every hash. Cutting
records off the tail is likewise invisible from the file alone. The first anchor is the head
hash (`AuditLog.head_hash`, also served by `/admin/stats`): every eval report and every
journal entry for a live run records it. External attestation is a Sprint 6 option.

RETENTION: `purge` drops expired leading records and writes a `retention_checkpoint` record as
the first line. The checkpoint holds the hash of the last purged record, and the first kept
record must link to it, so the remaining chain still verifies.
"""

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .secrets_registry import SECRETS, SecretRegistry

GENESIS = "0" * 64
CHECKPOINT = "retention_checkpoint"


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


def _lines(path: Path) -> list[str]:
    return [r for r in path.read_text(encoding="utf-8").splitlines() if r.strip()]


def verify_chain(path: str | Path) -> VerifyResult:
    p = Path(path)
    if not p.exists():
        return VerifyResult(True, 0)
    prev = GENESIS
    count = 0
    for n, raw in enumerate(_lines(p), start=1):
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            return VerifyResult(False, count, "unparseable record", n)
        stored = entry.pop("hash", None)
        is_checkpoint = entry.get("tool") == CHECKPOINT
        if is_checkpoint and n != 1:
            return VerifyResult(False, count, "retention checkpoint is not the first record", n)
        expected_prev = GENESIS if is_checkpoint else prev
        if entry.get("prev_hash") != expected_prev:
            return VerifyResult(False, count, "chain broken: prev_hash mismatch", n)
        if stored != _digest(expected_prev, entry):
            return VerifyResult(False, count, "record changed: hash mismatch", n)
        # After a checkpoint the next record links to the last purged record, not the checkpoint.
        prev = entry.get("purged_last_hash", stored) if is_checkpoint else stored
        count += 1
    return VerifyResult(True, count)


class AuditLog:
    def __init__(self, path: str | Path, registry: SecretRegistry = SECRETS):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.registry = registry
        self._lock = threading.Lock()
        self._last = self._read_last_hash()

    @property
    def head_hash(self) -> str:
        """Hash of the newest record. Record it in eval reports and live-run journal entries."""
        return self._last

    def _read_last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        last = GENESIS
        for raw in _lines(self.path):
            entry = json.loads(raw)
            last = (
                entry.get("purged_last_hash", entry["hash"])
                if entry.get("tool") == CHECKPOINT
                else entry["hash"]
            )
        return last

    def append(self, record: dict) -> dict:
        with self._lock:
            entry = self.registry.redact_obj(dict(record))
            entry["prev_hash"] = self._last
            entry["hash"] = _digest(self._last, entry)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(_canonical(entry) + "\n")
            self._last = entry["hash"]
            return entry

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(r) for r in _lines(self.path)]

    def purge(self, retention_days: int, now: datetime) -> int:
        """Drop expired leading records; returns how many (the checkpoint is not counted)."""
        with self._lock:
            if not self.path.exists():
                return 0
            entries = [json.loads(r) for r in _lines(self.path)]
            old_checkpoint = (
                entries[0] if entries and entries[0].get("tool") == CHECKPOINT else None
            )
            body = entries[1:] if old_checkpoint else entries
            cutoff = now - timedelta(days=retention_days)
            k = 0
            while k < len(body) and datetime.fromisoformat(body[k]["ts"]) < cutoff:
                k += 1
            if k == 0:
                return 0
            checkpoint = {
                "ts": now.isoformat(timespec="seconds"),
                "agent": "gateway",
                "tool": CHECKPOINT,
                "decision": "admin",
                "reason": f"retention {retention_days} days",
                "purged_count": (old_checkpoint or {}).get("purged_count", 0) + k,
                "purged_through_ts": body[k - 1]["ts"],
                "purged_last_hash": body[k - 1]["hash"],
                "prev_hash": GENESIS,
            }
            checkpoint["hash"] = _digest(GENESIS, checkpoint)
            kept = [checkpoint, *body[k:]]
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text("".join(_canonical(e) + "\n" for e in kept), encoding="utf-8")
            os.replace(tmp, self.path)
            self._last = kept[-1]["hash"] if len(kept) > 1 else checkpoint["purged_last_hash"]
            return k


def blocked_egress_by_tool(records: list[dict]) -> dict[str, int]:
    """Count `blocked_egress` decisions per tool, so false positives show up as their own number."""
    counts: dict[str, int] = {}
    for r in records:
        if r.get("decision") == "blocked_egress":
            counts[r.get("tool") or "?"] = counts.get(r.get("tool") or "?", 0) + 1
    return counts
