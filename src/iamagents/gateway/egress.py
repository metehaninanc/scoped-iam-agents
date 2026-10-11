"""Egress guard: the last check before a tool result leaves the gateway.

A hit means a tool returned something it must not (a phone number, an IP address, a secret),
so the gateway blocks the whole result and audits `blocked_egress`. False positives would fail
tickets silently and look like agent errors, so matching is deliberately narrow:

* Keys are split into whole camelCase / snake_case / kebab-case segments and compared to a
  forbidden set. There is no substring matching. `SAFE_KEYS` lists explicit exceptions.
* Value patterns are strict: IPv4 with valid octets and no neighbouring word characters or
  dots, E.164 numbers, and separated numbers with at least 7 digits. GUIDs, ISO dates and
  timestamps are masked first, and `SAFE_VALUE_KEYS` are never pattern-scanned.
* A string containing a registered secret is always a hit, in any field.

Known limits: IPv6 is not matched, and phone numbers written with dots as separators are not
matched (dotted numerics are mostly versions). The unit tests pin both choices.
"""

import re
from dataclasses import dataclass
from typing import Any

from .secrets_registry import SECRETS, SecretRegistry

FORBIDDEN_SEGMENTS = frozenset(
    {
        "password",
        "passwords",
        "passwd",
        "pwd",
        "secret",
        "secrets",
        "token",
        "tokens",
        "tap",
        "phone",
        "phones",
        "telephone",
        "ip",
        "credential",
        "credentials",
        "otp",
        "ssn",
    }
)
FORBIDDEN_JOINED = frozenset({"apikey", "privatekey", "accesskey"})  # adjacent segments joined

SAFE_KEYS = frozenset(
    {
        "password_reset_delivered",
        "tokenissuertype",
        "token_issuer_type",
    }
)

# Values of these keys are identifiers or timestamps; they are never pattern-scanned.
SAFE_VALUE_KEYS = frozenset(
    {
        "id",
        "ts",
        "created_at",
        "createddatetime",
        "timestamp",
        "graph_request_id",
        "request_id",
        "ticket_id",
    }
)

_PIECE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_GUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_ISO = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)
_DMY_HYPHEN = re.compile(r"\b\d{1,2}-\d{1,2}-\d{2,4}\b")
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IPV4 = re.compile(rf"(?<![\w.]){_OCTET}(?:\.{_OCTET}){{3}}(?![\w]|\.\d)")
_E164 = re.compile(r"(?<![\w+])\+[1-9]\d{6,14}(?!\d)")
_SEPARATED = re.compile(
    r"(?<![\w+-])(?:\+\d{1,3}[ -]?)?(?:\(\d{1,5}\)[ -]?)?\d{2,5}(?:[ -]\d{2,6}){1,4}(?![\w-])"
)


@dataclass(frozen=True)
class Violation:
    path: str
    kind: str  # forbidden_key | secret_value | ipv4 | phone

    def __str__(self) -> str:  # never includes the offending value
        return f"{self.kind}@{self.path}"


def key_segments(key: str) -> list[str]:
    segments: list[str] = []
    for part in re.split(r"[_\-\s.]+", key):
        segments += [m.group(0).lower() for m in _PIECE.finditer(part)]
    return segments


def is_forbidden_key(key: str) -> bool:
    if key.lower() in SAFE_KEYS:
        return False
    segs = key_segments(key)
    if any(s in FORBIDDEN_SEGMENTS for s in segs):
        return True
    return any(a + b in FORBIDDEN_JOINED for a, b in zip(segs, segs[1:], strict=False))


def _mask(pattern: re.Pattern, text: str) -> str:
    return pattern.sub(lambda m: " " * len(m.group(0)), text)


def value_kind(text: str) -> str | None:
    """Return 'ipv4' or 'phone' if the string looks like one, else None."""
    if _IPV4.search(text):
        return "ipv4"
    masked = _mask(_DMY_HYPHEN, _mask(_ISO, _mask(_GUID, text)))
    if _E164.search(masked):
        return "phone"
    for m in _SEPARATED.finditer(masked):
        if sum(c.isdigit() for c in m.group(0)) >= 7:
            return "phone"
    return None


class EgressGuard:
    def __init__(self, registry: SecretRegistry = SECRETS):
        self.registry = registry

    def scan(self, obj: Any) -> list[Violation]:
        out: list[Violation] = []
        self._walk(obj, "$", None, out)
        return out

    def _walk(self, obj: Any, path: str, key: str | None, out: list[Violation]) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                k = str(k)
                child = f"{path}.{k}"
                if is_forbidden_key(k):
                    out.append(Violation(child, "forbidden_key"))
                    continue
                self._walk(v, child, k.lower(), out)
        elif isinstance(obj, (list, tuple)):
            for i, v in enumerate(obj):
                self._walk(v, f"{path}[{i}]", key, out)
        elif isinstance(obj, str):
            if self.registry.contains(obj):
                out.append(Violation(path, "secret_value"))
                return
            if key in SAFE_VALUE_KEYS:
                return
            kind = value_kind(obj)
            if kind:
                out.append(Violation(path, kind))
