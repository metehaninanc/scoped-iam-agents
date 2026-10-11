"""Server level tests: bearer auth, per-token tool lists, admin endpoint, end to end over HTTP."""

import asyncio
import json
import socket
import threading
import time

import httpx
import httpx2
import pytest
import uvicorn
from conftest import ADMIN_TOKEN, TOKENS, add_ticket, upn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.testclient import TestClient

from iamagents.gateway.admin import create_admin_app
from iamagents.gateway.server import create_mcp_app

ZOE = upn("zoe.martin")


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


def test_tool_list_is_filtered_by_token(served):
    _, url = served

    async def names(session):
        return sorted(t.name for t in (await session.list_tools()).tools)

    assert with_session(url, TOKENS["ds"], names) == [
        "get_group",
        "get_user",
        "list_group_members",
        "reset_password",
    ]
    assert with_session(url, TOKENS["am"], names) == ["get_user"]
    assert with_session(url, TOKENS["pam"], names) == []


@pytest.mark.parametrize("agent", ["ds", "am", "ig"])
def test_get_user_end_to_end(served, agent):
    gw, url = served
    gw.bind_ticket("T-1")
    res = with_session(
        url, TOKENS[agent], lambda s: call(s, "get_user", {"ticket_id": "T-1", "upn": ZOE})
    )
    assert res["ok"] and res["data"]["displayName"] == "Zoe Martin"
    assert gw.audit.read_all()[-1]["agent"] == agent


def test_reset_password_plan_mode_end_to_end(served):
    gw, url = served
    gw.bind_ticket("T-1")
    res = with_session(
        url, TOKENS["ds"], lambda s: call(s, "reset_password", {"ticket_id": "T-1", "upn": ZOE})
    )
    assert res["ok"] and res["status"] == "success"
    assert gw.audit.read_all()[-1]["decision"] == "allow_plan"
    assert gw.graph_factory.directory.password_resets == []


def test_other_agents_tool_is_denied_over_http(served):
    gw, url = served
    gw.bind_ticket("T-1")
    res = with_session(
        url, TOKENS["am"], lambda s: call(s, "reset_password", {"ticket_id": "T-1", "upn": ZOE})
    )
    assert res["reason"] == "tool_not_permitted"


def test_ticket_mismatch_over_http(served):
    gw, url = served
    gw.bind_ticket("T-1")
    res = with_session(
        url,
        TOKENS["ds"],
        lambda s: call(s, "reset_password", {"ticket_id": "T-2", "upn": upn("arjun.mehta")}),
    )
    assert res["reason"] == "ticket_mismatch"
    assert gw.audit.read_all()[-1]["reason"] == "ticket_mismatch"


@pytest.mark.parametrize("header", [None, "Bearer wrong", "tok-ds", f"Bearer {ADMIN_TOKEN}"])
def test_bad_tokens_get_401_and_are_audited(served, header):
    gw, url = served
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if header:
        headers["Authorization"] = header
    r = httpx.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    entry = gw.audit.read_all()[-1]
    assert entry["decision"] == "denied_auth" and entry["agent"] == "unauthenticated"
    assert "wrong" not in json.dumps(entry)


def test_admin_endpoint_binds_ticket(make_gateway):
    gw = make_gateway("plan")
    client = TestClient(create_admin_app(gw))
    ok = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    assert client.post("/admin/bind", json={"ticket_id": "T-5"}).status_code == 401
    assert (
        client.post(
            "/admin/bind", json={"ticket_id": "T-5"}, headers={"Authorization": "Bearer tok-ds"}
        ).status_code
        == 401
    )
    assert gw.active_ticket is None
    assert client.post("/admin/bind", json={"ticket_id": "T-5"}, headers=ok).json() == {
        "active_ticket": "T-5"
    }
    assert gw.active_ticket == "T-5"
    assert client.get("/admin/status", headers=ok).json() == {
        "active_ticket": "T-5",
        "mode": "plan",
    }
    assert client.post("/admin/bind", json={}, headers=ok).status_code == 400
    assert client.post("/admin/unbind", headers=ok).json() == {"active_ticket": None}


def test_admin_endpoint_is_off_without_a_configured_token(tmp_path):
    from iamagents.gateway.audit import AuditLog
    from iamagents.gateway.config import ROOT, Settings
    from iamagents.gateway.core import Gateway
    from iamagents.gateway.graph.fake import FakeGraphFactory
    from iamagents.gateway.policy import Policy
    from iamagents.services.mock import MockServices

    settings = Settings(tokens=TOKENS, admin_token=None, audit_path=tmp_path / "a.jsonl")
    gw = Gateway(
        settings,
        Policy.load(ROOT / "policy" / "policy.yaml"),
        MockServices(),
        FakeGraphFactory(),
        AuditLog(settings.audit_path),
    )
    client = TestClient(create_admin_app(gw))
    assert (
        client.post(
            "/admin/bind", json={"ticket_id": "T-1"}, headers={"Authorization": "Bearer "}
        ).status_code
        == 401
    )
