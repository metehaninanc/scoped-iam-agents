"""The tenant domain, kept out of the repository.

Committed files (the dataset, the fake org, tests, reports, docs) use the placeholder
`<tenant>.onmicrosoft.com`. The real domain lives only in `.env` as `TENANT_DOMAIN` and is
mapped in at runtime: into the tickets the harness loads, into the fake Graph org, and later
into the seed scripts. Whatever the harness writes for committing is mapped back to the
placeholder, and a final check refuses to write a file that still contains the real domain.
"""

import os
import re
from typing import Any

from .gateway.config import ROOT, load_env_file

PLACEHOLDER = "<tenant>.onmicrosoft.com"
_DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")


class TenantDomainError(Exception):
    pass


def validate(domain: str) -> str:
    domain = domain.strip().lower()
    if not _DOMAIN.match(domain):
        raise TenantDomainError("TENANT_DOMAIN is not a valid domain name")
    if domain == PLACEHOLDER:
        raise TenantDomainError("TENANT_DOMAIN is still the placeholder")
    return domain


def tenant_domain(load_env: bool = True) -> str | None:
    """The configured tenant domain, or None when `.env` does not set one."""
    if load_env:
        load_env_file(ROOT / ".env")
    value = os.environ.get("TENANT_DOMAIN", "").strip()
    return validate(value) if value else None


def apply(obj: Any, domain: str | None) -> Any:
    """Replace the placeholder by the real domain in every string of a nested structure."""
    if not domain:
        return obj
    if isinstance(obj, str):
        return obj.replace(PLACEHOLDER, domain)
    if isinstance(obj, list):
        return [apply(v, domain) for v in obj]
    if isinstance(obj, dict):
        return {k: apply(v, domain) for k, v in obj.items()}
    return obj


def to_placeholder(text: str, domain: str | None) -> str:
    """Map the real domain back to the placeholder (for anything that may be committed)."""
    return text.replace(domain, PLACEHOLDER) if domain else text


def assert_absent(text: str, domain: str | None) -> None:
    if domain and domain in text.lower():
        raise TenantDomainError("the real tenant domain would be written to a report")
