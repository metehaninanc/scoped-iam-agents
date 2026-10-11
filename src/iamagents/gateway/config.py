"""Gateway settings from the environment."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
AGENTS = ("ds", "am", "ig", "pam", "tl")
MODES = ("plan", "live")


class ConfigError(Exception):
    pass


def load_env_file(path: str | Path) -> None:
    """Load KEY=VALUE lines into os.environ without overriding values already set."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    mode: str = "plan"
    tokens: Mapping[str, str] = field(default_factory=dict, repr=False)  # agent -> bearer token
    admin_token: str | None = field(default=None, repr=False)
    policy_path: Path = ROOT / "policy" / "policy.yaml"
    audit_path: Path = ROOT / "state" / "audit.jsonl"
    mock_db_path: str = str(ROOT / "state" / "mock.sqlite")
    protection_ttl_seconds: float = 30.0
    outbox_ttl_hours: float = 24.0  # undelivered secrets are wiped after this
    retention_days: int | None = None  # None: use audit.retention_days from policy.yaml

    def __post_init__(self):
        if self.mode not in MODES:
            raise ConfigError(f"GATEWAY_MODE must be one of {MODES}, got {self.mode!r}")
        values = list(self.tokens.values())
        if any(not v for v in values):
            raise ConfigError("empty gateway token")
        everything = values + ([self.admin_token] if self.admin_token else [])
        if len(set(everything)) != len(everything):
            raise ConfigError("gateway tokens (including the admin token) must all differ")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        tokens = {
            a: env[f"GW_TOKEN_{a.upper()}"] for a in AGENTS if env.get(f"GW_TOKEN_{a.upper()}")
        }
        kwargs: dict = {
            "mode": env.get("GATEWAY_MODE", "plan"),
            "tokens": tokens,
            "admin_token": env.get("GW_ADMIN_TOKEN") or None,
        }
        if env.get("LOG_RETENTION_DAYS"):
            kwargs["retention_days"] = int(env["LOG_RETENTION_DAYS"])
        if env.get("OUTBOX_TTL_HOURS"):
            kwargs["outbox_ttl_hours"] = float(env["OUTBOX_TTL_HOURS"])
        if env.get("AUDIT_PATH"):
            kwargs["audit_path"] = Path(env["AUDIT_PATH"])
        if env.get("MOCK_DB_PATH"):
            kwargs["mock_db_path"] = env["MOCK_DB_PATH"]
        return cls(**kwargs)
