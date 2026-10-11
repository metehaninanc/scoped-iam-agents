"""Server level tests: bearer auth, per-token tool lists, admin endpoint, end to end over HTTP."""

import json

import httpx
import pytest
from conftest import ADMIN_TOKEN, TOKENS, call, upn, with_session
from starlette.testclient import TestClient

from iamagents.gateway.admin import create_admin_app

ZOE = upn("zoe.martin")


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


# --- per-run tokens (eval harness): a leaked token dies with its run


def test_run_tokens_replace_the_old_tokens_and_end_with_revoke(served):
    gw, url = served
    old = TOKENS["ds"]
    tokens = gw.issue_run_tokens()
    assert set(tokens) == {f"GW_TOKEN_{a}" for a in ("DS", "AM", "IG", "PAM", "TL")}
    assert len(set(tokens.values())) == 5 and old not in tokens.values()
    assert gw.registry.authenticate(f"Bearer {tokens['GW_TOKEN_DS']}") == "ds"
    assert gw.registry.authenticate(f"Bearer {old}") is None  # the static token no longer works

    gw.bind_ticket("T-1")
    res = with_session(
        url, tokens["GW_TOKEN_DS"], lambda s: call(s, "get_user", {"ticket_id": "T-1", "upn": ZOE})
    )
    assert res["ok"]

    gw.revoke_run_tokens()
    import httpx

    r = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {tokens['GW_TOKEN_DS']}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert r.status_code == 401  # revoked: the run is over


def test_run_tokens_never_reach_the_audit_log_or_the_redaction_registry_leaks(served):
    gw, _ = served
    tokens = gw.issue_run_tokens()
    gw.revoke_run_tokens()
    text = gw.audit.path.read_text(encoding="utf-8")
    assert not any(v in text for v in tokens.values())
    assert gw.run_token_values == set(tokens.values())  # kept in memory for the report secret scan
    gw.audit.append({"ts": "t", "reason": f"oops {tokens['GW_TOKEN_AM']}"})
    assert tokens["GW_TOKEN_AM"] not in gw.audit.path.read_text(encoding="utf-8")  # redacted


def test_registry_rejects_duplicate_or_empty_tokens():
    import pytest

    from iamagents.gateway.auth import TokenRegistry

    reg = TokenRegistry({})
    with pytest.raises(ValueError):
        reg.replace({"ds": "same", "am": "same"})
    with pytest.raises(ValueError):
        reg.replace({"ds": ""})


def test_graph_factory_can_be_swapped_per_ticket(served):
    from iamagents.gateway.graph.fake import FakeGraphFactory

    gw, _ = served
    gw.bind_ticket("T-1")
    gw.call("ds", "get_user", {"ticket_id": "T-1", "upn": ZOE})
    first = gw.graph_factory.directory
    assert first.calls
    gw.set_graph_factory(FakeGraphFactory())
    assert gw.graph_factory.directory is not first and gw.graph_factory.directory.calls == []
