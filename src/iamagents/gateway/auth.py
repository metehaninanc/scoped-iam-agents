"""Bearer token to agent mapping. The token, not anything the model says, decides the identity."""

import hmac
from collections.abc import Mapping


def parse_bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


class TokenRegistry:
    def __init__(self, tokens: Mapping[str, str]):
        self._tokens = dict(tokens)

    def authenticate(self, header: str | None) -> str | None:
        """Return the agent id for a valid Authorization header, else None."""
        presented = parse_bearer(header)
        if presented is None:
            return None
        found = None
        for agent, token in self._tokens.items():  # no early exit: constant work per attempt
            if hmac.compare_digest(presented.encode(), token.encode()):
                found = agent
        return found
