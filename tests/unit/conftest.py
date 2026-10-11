import asyncio
import json
import socket
import threading
import time

import httpx2
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from iamagents.gateway.audit import AuditLog
from iamagents.gateway.config import ROOT, Settings
from iamagents.gateway.core import Gateway
from iamagents.gateway.graph.fake import FakeGraphFactory
from iamagents.gateway.policy import Policy
from iamagents.gateway.server import create_mcp_app
from iamagents.services.mock import MockServices

DOMAIN = "<tenant>.onmicrosoft.com"
TOKENS = {"ds": "tok-ds", "am": "tok-am", "ig": "tok-ig", "pam": "tok-pam", "tl": "tok-tl"}
ADMIN_TOKEN = "tok-admin"


def upn(prefix: str) -> str:
    return f"{prefix}@{DOMAIN}"


def add_ticket(
    services: MockServices, ticket_id: str, requester_prefix: str, verified: bool = True
):
    """Insert a minimal ticket and, by default, a verification record for the requester."""
    requester = upn(requester_prefix)
    body = {
        "ticket_id": ticket_id,
        "requester_upn": requester,
        "created_at": "2026-09-14T08:00:00+02:00",
        "split": "dev",
    }
    import json

    with services.db.conn:
        services.db.conn.execute(
            "INSERT INTO tickets (ticket_id, split, body_json) VALUES (?, 'dev', ?)",
            (ticket_id, json.dumps(body)),
        )
    if verified:
        services.verification.record(ticket_id, requester, "fixture", body["created_at"])
    return body


class Clock:
    """Controllable monotonic clock for the protection cache."""

    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def clean_secret_registry():
    from iamagents.gateway.secrets_registry import SECRETS

    SECRETS.clear()
    yield
    SECRETS.clear()


@pytest.fixture
def make_gateway(tmp_path):
    made = []

    def _make(mode: str = "plan", protection=None) -> Gateway:
        settings = Settings(
            mode=mode,
            tokens=TOKENS,
            admin_token=ADMIN_TOKEN,
            audit_path=tmp_path / f"audit-{len(made)}.jsonl",
        )
        services = MockServices()
        gw = Gateway(
            settings,
            Policy.load(ROOT / "policy" / "policy.yaml"),
            services,
            FakeGraphFactory(),
            AuditLog(settings.audit_path),
            protection=protection,
            now=lambda: "2026-10-11T10:00:00+00:00",
        )
        made.append(gw)
        return gw

    yield _make
    for gw in made:
        gw.services.db.close()


@pytest.fixture
def gw(make_gateway):
    """Plan mode gateway with tickets T-1 (zoe, verified) and T-2 (arjun, verified)."""
    g = make_gateway("plan")
    add_ticket(g.services, "T-1", "zoe.martin")
    add_ticket(g.services, "T-2", "arjun.mehta")
    return g


@pytest.fixture
def live_gw(make_gateway):
    g = make_gateway("live")
    add_ticket(g.services, "T-1", "zoe.martin")
    add_ticket(g.services, "T-2", "arjun.mehta")
    return g


@pytest.fixture
def served(make_gateway):
    gw = make_gateway("plan")
    add_ticket(gw.services, "T-1", "zoe.martin")
    add_ticket(gw.services, "T-2", "arjun.mehta")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_mcp_app(gw), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started
    yield gw, f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=5)


def with_session(url: str, token: str, fn):
    async def run():
        client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
        async with streamable_http_client(url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                return await fn(session)

    return asyncio.run(run())


def call(session, name, args):
    async def go():
        res = await session.call_tool(name, args)
        return json.loads(res.content[0].text)

    return go()
