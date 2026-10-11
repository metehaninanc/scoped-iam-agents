"""Task 0.11: egress guard, secret redaction, outbox lifecycle, retention."""

import json
import logging
import re
from datetime import datetime, timedelta

import pytest
from conftest import ADMIN_TOKEN, add_ticket, upn
from starlette.testclient import TestClient

from iamagents.gateway.admin import create_admin_app
from iamagents.gateway.audit import (
    CHECKPOINT,
    AuditLog,
    blocked_egress_by_tool,
    verify_chain,
)
from iamagents.gateway.config import Settings
from iamagents.gateway.egress import EgressGuard, is_forbidden_key, key_segments, value_kind
from iamagents.gateway.graph import fake_org
from iamagents.gateway.secrets_registry import SECRETS
from iamagents.gateway.tools import TOOLS, Outcome, ToolDef

ZOE = upn("zoe.martin")
NOW = datetime.fromisoformat("2026-10-11T10:00:00+00:00")


# --- egress guard: keys ---


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "userPassword",
        "temporary_password",
        "mobilePhone",
        "businessPhones",
        "lastSignInIpAddress",
        "ipAddress",
        "ip_address",
        "client_secret",
        "accessToken",
        "apiKey",
        "api_key",
        "tap",
        "TAP",
        "telephone-number",
        "credentials",
    ],
)
def test_forbidden_keys_are_blocked(key):
    assert is_forbidden_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "displayName",
        "userPrincipalName",
        "jobTitle",
        "accountEnabled",
        "memberCount",
        "route_to",
        "rule_id",
        "graph_request_id",
        "recipient_upn",
        "description",
        "shipping",  # contains "ip" but not as a segment
        "tapestry",  # contains "tap"
        "skipped",
        "ipsum",
        "status",
        "tier",
        "message",
        "owners",
        "id",
        # explicit exceptions
        "password_reset_delivered",
        "tokenIssuerType",
    ],
)
def test_ordinary_keys_pass(key):
    assert not is_forbidden_key(key)


def test_key_segments():
    assert key_segments("lastSignInIpAddress") == ["last", "sign", "in", "ip", "address"]
    assert key_segments("password_reset_delivered") == ["password", "reset", "delivered"]
    assert key_segments("IPAddress") == ["ip", "address"]


# --- egress guard: values ---


@pytest.mark.parametrize(
    "text",
    ["203.0.113.7", "from 10.0.0.1 today", "192.168.1.255", "ip=8.8.8.8;"],
)
def test_ipv4_is_detected(text):
    assert value_kind(text) == "ipv4"


@pytest.mark.parametrize(
    "text",
    [
        "+49 30 5550 0000",
        "+4930555000",
        "030 5550 0001",
        "(030) 5550-0001",
        "call +14155550100 now",
    ],
)
def test_phone_is_detected(text):
    assert value_kind(text) == "phone"


NEGATIVES = [
    # GUIDs
    "0b8e1c1a-6f0e-4d3a-9c55-1f2a3b4c5d6e",
    "9F2B7A10-0C6D-4F55-8A31-5D7E4B21C0AA",
    # timestamps and dates
    "2026-09-14T08:12:00+02:00",
    "2026-10-11T10:00:00+00:00",
    "2026-10-11T10:00:00Z",
    "2026-10-11T10:00:00.123456+00:00",
    "2026-10-11",
    "2026-09-14 08:12",
    "14.09.2026",
    "14-09-2026",
    "09-14-2026",
    "Sep 14, 2026",
    # versions and near misses for IPv4
    "mcp 2.3.0",
    "v1.2.3",
    "python 3.12.14",
    "v1.2.3.4",
    "1.2.3",
    "256.1.1.1",
    "999.1.1.1",
    "1.2.3.4.5",
    "2026.10.11",
    # ticket numbers, counts, ports, ids
    "4412",
    "ticket 4412 was closed",
    "IAM-001",
    "IAM-089",
    "seat 24 of 25",
    "5 of 22 users",
    "port 8765",
    "fake-req-0001",
    "SG-App-ERP-Read",
    "zoe.martin@<tenant>.onmicrosoft.com",
    "user01@example.com",
    "a3f5c7d9e1b2a3f5c7d9e1b2a3f5c7d9e1b2a3f5c7d9e1b2a3f5c7d9e1b20123",
    "Password reset. The temporary password was delivered out of band.",
    "T3 package prepared; approver marcus.weber, executor priya.nair",
]


@pytest.mark.parametrize("text", NEGATIVES)
def test_negative_set_is_never_blocked(text):
    assert value_kind(text) is None
    assert EgressGuard().scan({"message": text, "data": {"note": [text]}}) == []


def test_safe_value_keys_are_not_pattern_scanned():
    assert EgressGuard().scan({"id": "203.0.113.7", "ts": "+49 30 5550 0000"}) == []


def test_registered_secret_is_blocked_in_any_field_even_safe_ones():
    SECRETS.register("Zq9-not-a-real-pw")
    guard = EgressGuard()
    assert [v.kind for v in guard.scan({"message": "your pw is Zq9-not-a-real-pw"})] == [
        "secret_value"
    ]
    assert [v.kind for v in guard.scan({"id": "Zq9-not-a-real-pw"})] == ["secret_value"]
    assert [v.kind for v in guard.scan({"a": [{"b": "x Zq9-not-a-real-pw y"}]})] == ["secret_value"]


def test_short_values_are_not_registered():
    SECRETS.register("abc")
    assert EgressGuard().scan({"message": "abc abc"}) == []


def test_violation_text_never_contains_the_value():
    SECRETS.register("Zq9-not-a-real-pw")
    v = EgressGuard().scan({"data": {"mobilePhone": "+49 30 5550 0000", "x": "Zq9-not-a-real-pw"}})
    text = " ".join(str(x) for x in v)
    assert "5550" not in text and "Zq9" not in text
    assert "forbidden_key@$.data.mobilePhone" in text


# --- guard against the gateway's own results ---


def run_every_tool(gw, agent_tools):
    for agent, calls in agent_tools.items():
        for tool, args in calls:
            gw.call(agent, tool, {"ticket_id": "T-1", **args})


CALLS = {
    "ds": [
        ("get_user", {"upn": ZOE}),
        ("get_user", {"upn": "nobody@x"}),
        ("get_group", {"name": "SG-App-CRM"}),
        ("list_group_members", {"name": "SG-App-CRM"}),
        ("reset_password", {"upn": ZOE}),
        ("reset_password", {"upn": upn("arjun.mehta")}),
        ("reset_password", {"upn": upn("tom.becker")}),
        ("get_user", {}),
        ("make_coffee", {}),
    ],
    "am": [("get_user", {"upn": ZOE}), ("reset_password", {"upn": ZOE})],
    "ig": [("get_user", {"upn": ZOE}), ("get_group", {"name": "SG-App-ERP-Create"})],
}


@pytest.mark.parametrize("fixture", ["gw", "live_gw"])
def test_gateways_own_results_are_never_blocked(request, fixture):
    gateway = request.getfixturevalue(fixture)
    gateway.bind_ticket("T-1")
    run_every_tool(gateway, CALLS)
    assert blocked_egress_by_tool(gateway.audit.read_all()) == {}
    assert len(gateway.audit.read_all()) > 10


def test_every_user_and_group_in_the_fake_org_passes_the_guard(gw):
    org = fake_org.build_org()
    gw.bind_ticket("T-1")
    results = []
    for user in org["users"].values():
        for agent in ("ds", "am", "ig"):
            results.append(
                gw.call(agent, "get_user", {"ticket_id": "T-1", "upn": user["userPrincipalName"]})
            )
    for name in org["groups"]:
        results.append(gw.call("ig", "get_group", {"ticket_id": "T-1", "name": name}))
        results.append(gw.call("ig", "list_group_members", {"ticket_id": "T-1", "name": name}))
    assert all(r["ok"] for r in results), [r for r in results if not r["ok"]][:2]
    assert blocked_egress_by_tool(gw.audit.read_all()) == {}
    # independent check, not using the guard: no phone number or IPv4 in anything returned
    text = json.dumps(results)
    assert not re.search(r"\+\d{6,}|\b\d{1,3}(\.\d{1,3}){3}\b|\+49 ", text)


def test_the_guard_would_catch_an_unminimised_user():
    raw = fake_org.build_org()["users"][ZOE.lower()]
    kinds = {v.kind for v in EgressGuard().scan({"data": raw})}
    assert kinds == {"forbidden_key"}


def test_allowlists_exist_and_hold_no_forbidden_key(gw):
    for tool in ("get_user", "get_group", "list_group_members"):
        allowlist = gw.policy.field_allowlist(tool)
        assert allowlist, tool
        for path in allowlist:
            assert not any(is_forbidden_key(part) for part in path.split(".")), path


def leaky_tool(name, payload):
    def handler(gw, agent, ticket, args):
        return Outcome(
            {
                "ok": True,
                "decision": "allowed",
                "rule_id": "READ-01",
                "tier": "T0",
                "data": payload,
            },
            "allowed",
            "READ-01",
            "allowed",
            ("A.5.34",),
            args.get("upn"),
        )

    return ToolDef(name, "leaky", TOOLS[name].input_schema, handler)


def test_leaky_tool_is_blocked_audited_and_counted(gw, monkeypatch):
    raw = fake_org.build_org()["users"][ZOE.lower()]
    monkeypatch.setitem(TOOLS, "get_user", leaky_tool("get_user", raw))
    gw.bind_ticket("T-1")
    res = gw.call("ds", "get_user", {"ticket_id": "T-1", "upn": ZOE})
    assert res == {
        "ok": False,
        "decision": "denied",
        "reason": "internal_error",
        "message": "Internal error.",
    }
    entry = gw.audit.read_all()[-1]
    assert entry["decision"] == "blocked_egress" and entry["tool"] == "get_user"
    assert "forbidden_key@$.data.mobilePhone" in entry["reason"]
    text = gw.audit.path.read_text(encoding="utf-8")
    assert "5550" not in text and "203.0.113" not in text
    assert blocked_egress_by_tool(gw.audit.read_all()) == {"get_user": 1}


def test_secret_in_a_result_is_blocked(gw, monkeypatch):
    SECRETS.register("Zq9-not-a-real-pw")
    monkeypatch.setitem(
        TOOLS, "get_user", leaky_tool("get_user", {"displayName": "oops Zq9-not-a-real-pw"})
    )
    gw.bind_ticket("T-1")
    res = gw.call("ds", "get_user", {"ticket_id": "T-1", "upn": ZOE})
    assert res["reason"] == "internal_error"
    entry = gw.audit.read_all()[-1]
    assert entry["decision"] == "blocked_egress"
    assert "Zq9" not in gw.audit.path.read_text(encoding="utf-8")


def test_admin_stats_reports_head_hash_and_blocked_egress(gw, monkeypatch):
    monkeypatch.setitem(
        TOOLS, "get_user", leaky_tool("get_user", fake_org.build_org()["users"][ZOE.lower()])
    )
    gw.bind_ticket("T-1")
    gw.call("ds", "get_user", {"ticket_id": "T-1", "upn": ZOE})
    gw.call("ds", "get_group", {"ticket_id": "T-1", "name": "SG-App-CRM"})
    client = TestClient(create_admin_app(gw))
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    stats = client.get("/admin/stats", headers=headers).json()
    assert stats["blocked_egress"] == {"get_user": 1}
    assert stats["audit_head"] == gw.audit.head_hash != "0" * 64
    assert stats["audit_records"] == len(gw.audit.read_all())
    assert client.get("/admin/stats").status_code == 401


# --- redaction ---


def test_log_records_redact_registered_secrets(caplog):
    SECRETS.register("Zq9-not-a-real-pw")
    caplog.set_level(logging.DEBUG)
    from iamagents.gateway.secrets_registry import install_log_redaction

    install_log_redaction()
    logging.getLogger("some.other.module").warning("pw is %s", "Zq9-not-a-real-pw")
    logging.getLogger("iamagents.graph.fake").debug(f"body {'Zq9-not-a-real-pw'}")
    assert "Zq9-not-a-real-pw" not in caplog.text
    assert caplog.text.count("[REDACTED]") == 2


def test_audit_records_redact_registered_secrets(tmp_path):
    SECRETS.register("Zq9-not-a-real-pw")
    log = AuditLog(tmp_path / "a.jsonl")
    log.append(
        {"ts": "t", "reason": "x Zq9-not-a-real-pw y", "nested": {"k": ["Zq9-not-a-real-pw"]}}
    )
    text = log.path.read_text(encoding="utf-8")
    assert "Zq9" not in text and "[REDACTED]" in text
    assert verify_chain(log.path).ok


# --- outbox lifecycle ---


def test_password_is_wiped_from_the_outbox_once_delivered(live_gw):
    gw = live_gw
    gw.bind_ticket("T-1")
    gw.call("ds", "reset_password", {"ticket_id": "T-1", "upn": ZOE})
    [msg] = gw.services.outbox.metadata("T-1")
    assert msg["state"] == "pending"
    delivered = gw.services.outbox.open_message(msg["id"])
    password = delivered["secret"]
    assert len(password) >= 16
    dump = json.dumps([dict(r) for r in gw.services.db.conn.execute("SELECT * FROM outbox")])
    assert password not in dump
    assert gw.services.outbox.metadata("T-1")[0]["state"] == "delivered"
    gw.run_retention()
    assert password not in gw.audit.path.read_text(encoding="utf-8")


def test_undelivered_secret_expires_after_the_ttl(live_gw):
    gw = live_gw
    out = gw.services.outbox
    old = out.send(
        "T-1",
        ZOE,
        "temporary_password",
        "old-secret-value",
        (NOW - timedelta(hours=25)).isoformat(),
    )
    fresh = out.send(
        "T-1",
        ZOE,
        "temporary_password",
        "fresh-secret-value",
        (NOW - timedelta(hours=23)).isoformat(),
    )
    done = out.send(
        "T-1",
        ZOE,
        "temporary_password",
        "done-secret-value",
        (NOW - timedelta(hours=30)).isoformat(),
    )
    out.open_message(done)
    assert out.expire(NOW.isoformat(), 24) == 1
    states = {m["id"]: m["state"] for m in out.metadata("T-1")}
    assert states == {old: "expired", fresh: "pending", done: "delivered"}
    dump = json.dumps([dict(r) for r in gw.services.db.conn.execute("SELECT * FROM outbox")])
    assert "old-secret-value" not in dump and "done-secret-value" not in dump
    assert "fresh-secret-value" in dump
    assert out.open_message(old)["secret"] is None


def test_settings_for_retention_and_ttl():
    s = Settings.from_env({"LOG_RETENTION_DAYS": "30", "OUTBOX_TTL_HOURS": "6"})
    assert s.retention_days == 30 and s.outbox_ttl_hours == 6
    d = Settings()
    assert d.retention_days is None and d.outbox_ttl_hours == 24


def test_run_retention_uses_policy_default_and_outbox_ttl(live_gw):
    gw = live_gw
    for days_ago in (200, 100, 91, 89, 1):
        ts = (NOW - timedelta(days=days_ago)).isoformat()
        gw.audit.append({"ts": ts, "agent": "ds", "tool": "get_user", "decision": "allowed"})
    gw.services.outbox.send(
        "T-1",
        ZOE,
        "temporary_password",
        "stale-secret-value",
        (NOW - timedelta(hours=30)).isoformat(),
    )
    report = gw.run_retention()
    assert report == {"audit_purged": 3, "outbox_expired": 1, "retention_days": 90}
    assert verify_chain(gw.audit.path).ok
    gw.settings = Settings(
        mode="live", tokens=gw.settings.tokens, retention_days=10, audit_path=gw.audit.path
    )
    assert gw.run_retention()["audit_purged"] == 1  # the 89 day record


# --- audit retention and checkpoint ---


def aged_log(tmp_path, days_ago=(200, 100, 91, 89, 1)):
    log = AuditLog(tmp_path / "a.jsonl")
    for i, d in enumerate(days_ago):
        log.append(
            {"ts": (NOW - timedelta(days=d)).isoformat(), "agent": "ds", "tool": "t", "i": i}
        )
    return log


def test_purge_keeps_the_chain_verifiable(tmp_path):
    log = aged_log(tmp_path)
    head = log.head_hash
    assert log.purge(90, NOW) == 3
    assert log.head_hash == head  # newest record unchanged
    result = verify_chain(log.path)
    assert result.ok and result.records == 3  # checkpoint + two kept
    first = log.read_all()[0]
    assert first["tool"] == CHECKPOINT and first["purged_count"] == 3
    log.append({"ts": NOW.isoformat(), "agent": "ds", "tool": "t"})
    assert verify_chain(log.path).ok and verify_chain(log.path).records == 4


def test_purge_twice_accumulates_and_nothing_to_purge_is_a_noop(tmp_path):
    log = aged_log(tmp_path)
    log.purge(90, NOW)
    assert log.purge(90, NOW) == 0
    assert log.purge(50, NOW) == 1  # the 89 day record
    assert log.read_all()[0]["purged_count"] == 4
    assert verify_chain(log.path).ok


def test_purge_everything_leaves_only_the_checkpoint_and_the_chain_continues(tmp_path):
    log = aged_log(tmp_path, days_ago=(200, 100))
    head = log.head_hash
    assert log.purge(30, NOW) == 2
    assert len(log.read_all()) == 1 and log.head_hash == head
    reopened = AuditLog(log.path)
    assert reopened.head_hash == head
    reopened.append({"ts": NOW.isoformat(), "agent": "ds", "tool": "t"})
    assert verify_chain(log.path).ok


def test_reopen_after_purge_continues_the_chain(tmp_path):
    log = aged_log(tmp_path)
    log.purge(90, NOW)
    reopened = AuditLog(log.path)
    assert reopened.head_hash == log.head_hash
    reopened.append({"ts": NOW.isoformat(), "agent": "ds", "tool": "t"})
    assert verify_chain(log.path).ok


@pytest.mark.parametrize(
    "field,value", [("purged_count", 99), ("purged_last_hash", "f" * 64), ("reason", "x")]
)
def test_edited_checkpoint_is_detected(tmp_path, field, value):
    log = aged_log(tmp_path)
    log.purge(90, NOW)
    lines = log.path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first[field] = value
    lines[0] = json.dumps(first)
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert not verify_chain(log.path).ok


def test_removed_or_misplaced_checkpoint_is_detected(tmp_path):
    log = aged_log(tmp_path)
    log.purge(90, NOW)
    lines = log.path.read_text(encoding="utf-8").splitlines()
    log.path.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")
    assert not verify_chain(log.path).ok
    log.path.write_text("\n".join([lines[1], lines[0], *lines[2:]]) + "\n", encoding="utf-8")
    assert not verify_chain(log.path).ok


def test_documented_limit_a_full_consistent_rewrite_is_not_detected(tmp_path):
    """Pins the limit stated in audit.py and DATA_PROTECTION.md: no anchor, no detection."""
    original = aged_log(tmp_path / "orig")
    forged = AuditLog(tmp_path / "forged" / "a.jsonl")
    for e in original.read_all():
        record = {k: v for k, v in e.items() if k not in ("prev_hash", "hash")}
        record["agent"] = "someone-else"  # history rewritten, hashes recomputed
        forged.append(record)
    assert verify_chain(forged.path).ok
    assert forged.head_hash != original.head_hash  # only an external copy of the head catches it


def test_tail_truncation_is_visible_only_against_a_recorded_head(tmp_path):
    log = aged_log(tmp_path)
    recorded_head = log.head_hash
    lines = log.path.read_text(encoding="utf-8").splitlines()
    log.path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    assert verify_chain(log.path).ok
    assert AuditLog(log.path).head_hash != recorded_head


def test_add_ticket_helper_still_available():
    assert callable(add_ticket)
