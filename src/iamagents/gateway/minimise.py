"""Field minimiser: only allowlisted fields reach the model (DATA_PROTECTION.md)."""

from typing import Any


class MinimiseError(Exception):
    """No allowlist exists for a tool. The gateway fails closed instead of passing data through."""


def _pick(obj: dict, path: str) -> tuple[bool, Any]:
    cur: Any = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def minimise(obj: dict, allowlist: list[str]) -> dict:
    """Keep only allowlisted fields. Dotted paths keep their nesting (manager.userPrincipalName)."""
    out: dict = {}
    for path in allowlist:
        found, value = _pick(obj, path)
        if not found:
            continue
        parts = path.split(".")
        cur = out
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value
    return out
