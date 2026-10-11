"""Run the gateway: `uv run python -m iamagents.gateway`.

MCP on GATEWAY_PORT (default 8765), admin on GATEWAY_ADMIN_PORT (default 8766), both on
127.0.0.1. Sprint 0 uses the fake Graph; the real client arrives with the tenant tasks.
"""

import asyncio
import os
from pathlib import Path

import uvicorn

from ..services.mock import MockServices
from .admin import create_admin_app
from .audit import AuditLog
from .config import ROOT, Settings, load_env_file
from .core import Gateway
from .graph.fake import FakeGraphFactory
from .policy import Policy
from .server import create_mcp_app


def build_gateway(settings: Settings) -> Gateway:
    Path(settings.mock_db_path).parent.mkdir(parents=True, exist_ok=True)
    services = MockServices(settings.mock_db_path)
    if not services.tickets.list():  # dev convenience; the eval harness loads fixtures itself
        services.tickets.load_dataset(ROOT / "data" / "iam-tickets-v2.jsonl")
    return Gateway(
        settings,
        Policy.load(settings.policy_path),
        services,
        FakeGraphFactory(),
        AuditLog(settings.audit_path),
    )


async def _serve() -> None:
    load_env_file(ROOT / ".env")
    settings = Settings.from_env()
    gateway = build_gateway(settings)
    gateway.run_retention()
    mcp_port = int(os.environ.get("GATEWAY_PORT", "8765"))
    admin_port = int(os.environ.get("GATEWAY_ADMIN_PORT", "8766"))
    servers = [
        uvicorn.Server(
            uvicorn.Config(
                create_mcp_app(gateway), host="127.0.0.1", port=mcp_port, log_level="warning"
            )
        ),
        uvicorn.Server(
            uvicorn.Config(
                create_admin_app(gateway), host="127.0.0.1", port=admin_port, log_level="warning"
            )
        ),
    ]
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(_serve())
