"""Is a principal protected? Member of RMAU-Protected OR holding any active directory role.

A user elevated through PIM is not in the RMAU but is an administrator for that window, so the
role check matters. The lookup uses the calling agent's own Graph identity (Directory Readers)
and a short cache. If the lookup fails the target counts as protected. Failures are not cached.
"""

import time
from collections.abc import Callable

from .graph.base import GraphClient, GraphError
from .policy import ProtectionResult


class ProtectionChecker:
    def __init__(self, ttl_seconds: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl_seconds
        self.clock = clock
        self._cache: dict[str, tuple[float, ProtectionResult]] = {}

    def check(self, graph: GraphClient, upn: str) -> ProtectionResult:
        key = upn.lower()
        hit = self._cache.get(key)
        if hit and self.clock() - hit[0] < self.ttl:
            return hit[1]
        try:
            if graph.is_rmau_protected(upn):
                result = ProtectionResult(True, "rmau")
            elif graph.has_active_directory_role(upn):
                result = ProtectionResult(True, "active_role")
            else:
                result = ProtectionResult(False, "none")
        except GraphError:
            return ProtectionResult(True, "lookup_failed")
        except Exception:  # any other failure also fails closed
            return ProtectionResult(True, "lookup_failed")
        self._cache[key] = (self.clock(), result)
        return result
