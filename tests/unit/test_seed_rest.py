"""The REST implementation of the admin Graph, tested against the emulator and recorded requests."""

import logging
import sys
import types

import httpx
import pytest
from fake_graph_server import BASE, GraphEmulator

from iamagents.gateway.graph.base import Secret
from seed import connect
from seed.admin_graph import AdminGraphError
from seed.fake_admin import FakeAdminGraph
from seed.rest_admin import GraphRequestError, RestAdminGraph

DOMAIN = "t.example"
ADMIN, BREAKGLASS = f"root@{DOMAIN}", f"bg@{DOMAIN}"
PASSWORD = "Pw-Zk9!xQ3mR7tVb2-Unique"


def upn(prefix):
    return f"{prefix}@{DOMAIN}"


def make(emulator_kw=None, **rest_kw):
    fake = FakeAdminGraph(DOMAIN, ADMIN, BREAKGLASS)
    emu = GraphEmulator(fake, **(emulator_kw or {}))
    sleeps, tokens = [], []

    def token():
        tokens.append(1)
        return "tok-" + "x" * 20

    rest = RestAdminGraph(
        token, httpx.Client(transport=emu.transport), sleep=sleeps.append, **rest_kw
    )
    return rest, emu, sleeps, tokens


def user(rest, prefix, **attrs):
    attrs = {
        "displayName": prefix,
        "userType": "Member",
        "usageLocation": "DE",
        "employeeType": "Employee",
    } | attrs
    return rest.create_user(upn(prefix), attrs, Secret(PASSWORD))


# --- transport behaviour


def test_every_request_carries_a_fresh_bearer_token():
    rest, emu, _, tokens = make()
    rest.organization_domains()
    rest.list_skus()
    assert len(tokens) == 2 and all(
        r["auth"] == "Bearer " + "tok-" + "x" * 20 for r in emu.requests
    )


def test_the_token_is_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    rest, *_ = make()
    rest.organization_domains()
    assert "tok-xxxx" not in caplog.text and "graph GET" in caplog.text


def test_the_password_is_sent_once_and_never_logged_or_raised(caplog):
    caplog.set_level(logging.DEBUG)
    rest, emu, *_ = make()
    user(rest, "zoe.martin")
    body = next(
        r["body"] for r in emu.requests if r["method"] == "POST" and r["url"].endswith("/users")
    )
    assert body["passwordProfile"] == {"forceChangePasswordNextSignIn": True, "password": PASSWORD}
    assert body["userPrincipalName"] == upn("zoe.martin") and body["mailNickname"] == "zoe.martin"
    assert PASSWORD not in caplog.text
    with pytest.raises(AdminGraphError) as err:
        user(rest, "zoe.martin")  # a duplicate: Graph answers with an error
    assert (
        PASSWORD not in str(err.value)
        and isinstance(err.value, GraphRequestError)
        and err.value.status == 400
    )


def test_the_password_is_not_in_any_other_request():
    rest, emu, *_ = make()
    user(rest, "zoe.martin")
    rest.update_user(upn("zoe.martin"), {"jobTitle": "x"})
    rest.get_user(upn("zoe.martin"))
    others = [
        r for r in emu.requests if not (r["method"] == "POST" and r["url"].endswith("/users"))
    ]
    assert PASSWORD not in str(others)


def test_paging_follows_next_links():
    rest, emu, *_ = make({"page_size": 5})
    for i in range(12):
        user(rest, f"user{i:02d}")
    emu.requests.clear()
    users = rest.list_users()
    assert len(users) == 14  # 12 plus the two admin accounts
    assert len([r for r in emu.requests if r["method"] == "GET"]) == 3  # 5 + 5 + 4


def test_throttling_is_retried_and_waits_as_told():
    rest, emu, sleeps, _ = make({"throttle_first": 3})
    assert rest.organization_domains() == [DOMAIN]
    assert sleeps == [0.0, 0.0, 0.0]


def test_giving_up_after_the_retry_budget():
    rest, *_ = make({"throttle_first": 100}, retries=2)
    with pytest.raises(GraphRequestError) as err:
        rest.organization_domains()
    assert err.value.status == 429


def test_a_user_that_is_not_visible_yet_is_retried():
    rest, emu, sleeps, _ = make({"lag_404": 2})
    user(rest, "zoe.martin")
    user(rest, "felix.wagner")
    rest.set_manager(
        upn("zoe.martin"), upn("felix.wagner")
    )  # looks the manager up right after creating it
    assert sleeps == [1, 2]
    assert emu.fake.get_user(upn("zoe.martin")).manager_upn == upn("felix.wagner")


def test_other_errors_raise_with_status_and_code_but_no_body():
    rest, *_ = make()
    with pytest.raises(GraphRequestError) as err:
        rest.update_user(upn("nobody"), {"jobTitle": "secret-looking-value-123"})
    assert err.value.status == 404 and "secret-looking-value-123" not in str(err.value)


def test_missing_users_come_back_as_none():
    rest, *_ = make()
    assert rest.get_user(upn("nobody")) is None and rest.get_user(ADMIN).upn == ADMIN


def test_upns_are_url_encoded():
    rest, emu, *_ = make()
    emu.fake.users["guest"] = type(emu.fake.users[ADMIN.lower()])(id="g", upn="a_b#EXT#@t.example")
    rest.get_user("a_b#EXT#@t.example")
    assert "%23EXT%23" in emu.requests[-1]["url"]


# --- users, groups, links


def test_manager_license_and_delete_requests():
    rest, emu, *_ = make()
    user(rest, "zoe.martin")
    user(rest, "felix.wagner")
    rest.set_manager(upn("zoe.martin"), upn("felix.wagner"))
    sku = rest.list_skus()[0].id
    rest.assign_license(upn("zoe.martin"), sku)
    rest.remove_license(upn("zoe.martin"), sku)
    rest.assign_license(upn("zoe.martin"), sku)
    ref = next(r for r in emu.requests if r["method"] == "PUT")
    assert ref["body"]["@odata.id"].startswith(BASE + "/users/") and ref["url"].endswith(
        "/manager/$ref"
    )
    lic = [r["body"] for r in emu.requests if r["url"].endswith("/assignLicense")]
    assert lic[0] == {"addLicenses": [{"skuId": sku}], "removeLicenses": []}
    assert lic[1] == {"addLicenses": [], "removeLicenses": [sku]}
    rest.delete_user(upn("zoe.martin"))
    assert rest.get_user(upn("zoe.martin")) is None


def test_group_bodies_for_the_three_kinds():
    rest, emu, *_ = make()
    user(rest, "marcus.weber")
    rest.create_group("SG-App-CRM", "assigned", None, [upn("marcus.weber")])
    rest.create_group("DG-Dept-IT", "dynamic", 'user.department -eq "IT"', [])
    rest.create_group("PRIV-User-Admin", "role_assignable", None, [upn("marcus.weber")])
    bodies = {
        r["body"]["displayName"]: r["body"]
        for r in emu.requests
        if r["method"] == "POST" and r["url"].endswith("/groups")
    }
    a, d, p = bodies["SG-App-CRM"], bodies["DG-Dept-IT"], bodies["PRIV-User-Admin"]
    assert (
        a["securityEnabled"]
        and not a["mailEnabled"]
        and a["groupTypes"] == []
        and a["mailNickname"] == "sg-app-crm"
    )
    assert a["owners@odata.bind"][0].startswith(BASE + "/users/") and "isAssignableToRole" not in a
    assert (
        d["groupTypes"] == ["DynamicMembership"]
        and d["membershipRule"] == 'user.department -eq "IT"'
    )
    assert d["membershipRuleProcessingState"] == "On" and "owners@odata.bind" not in d
    assert (
        p["isAssignableToRole"] is True and p["groupTypes"] == [] and p["visibility"] == "Private"
    )
    kinds = {g.name: g.kind for g in rest.list_groups()}
    assert kinds == {
        "SG-App-CRM": "assigned",
        "DG-Dept-IT": "dynamic",
        "PRIV-User-Admin": "role_assignable",
    }


def test_members_owners_and_idempotent_links():
    rest, emu, *_ = make()
    user(rest, "marcus.weber")
    user(rest, "zoe.martin")
    rest.create_group("SG-App-CRM", "assigned", None, [upn("marcus.weber")])
    rest.add_member("SG-App-CRM", upn("zoe.martin"))
    rest.add_member("SG-App-CRM", upn("zoe.martin"))  # already a member: not an error
    assert rest.group_members("SG-App-CRM") == [upn("zoe.martin")]
    assert rest.group_owners("SG-App-CRM") == [upn("marcus.weber")]
    rest.add_owner("SG-App-CRM", upn("zoe.martin"))
    rest.remove_owner("SG-App-CRM", upn("marcus.weber"))
    rest.remove_member("SG-App-CRM", upn("zoe.martin"))
    rest.remove_member("SG-App-CRM", upn("zoe.martin"))  # not a member any more: not an error
    assert rest.group_members("SG-App-CRM") == [] and rest.group_owners("SG-App-CRM") == [
        upn("zoe.martin")
    ]
    link = next(
        r for r in emu.requests if r["method"] == "POST" and r["url"].endswith("/members/$ref")
    )
    assert link["body"]["@odata.id"].startswith(BASE + "/directoryObjects/")


def test_a_real_link_error_is_not_swallowed():
    rest, *_ = make()
    user(rest, "zoe.martin")
    rest.create_group("DG-Dept-IT", "dynamic", 'user.department -eq "IT"', [])
    with pytest.raises(GraphRequestError):
        rest.add_member("DG-Dept-IT", upn("zoe.martin"))  # dynamic groups take no manual members


def test_roles_through_a_role_assignable_group():
    rest, emu, *_ = make()
    rest.create_group("PRIV-User-Admin", "role_assignable", None, [])
    rest.assign_role("PRIV-User-Admin", "User Administrator")
    assert rest.group_roles("PRIV-User-Admin") == ["User Administrator"]
    post = next(
        r for r in emu.requests if r["method"] == "POST" and r["url"].endswith("/roleAssignments")
    )
    assert (
        post["body"]["directoryScopeId"] == "/"
        and post["body"]["roleDefinitionId"] == "rd-user-admin"
    )
    rest.remove_role("PRIV-User-Admin", "User Administrator")
    assert rest.group_roles("PRIV-User-Admin") == []
    with pytest.raises(GraphRequestError, match="not found"):
        rest.assign_role("PRIV-User-Admin", "No Such Role")


def test_odata_quotes_in_role_names_are_escaped():
    rest, emu, *_ = make()
    rest.create_group("PRIV-X", "role_assignable", None, [])
    with pytest.raises(GraphRequestError):
        rest.assign_role("PRIV-X", "O'Brien Admin")
    assert "O''Brien" in str(emu.requests[-1]["url"]).replace("%27", "'")


def test_the_rmau_is_read_when_it_exists():
    rest, emu, *_ = make()
    assert rest.rmau_members() == []
    emu.fake.set_rmau([ADMIN, "PRIV-User-Admin"])
    assert rest.rmau_members() == [ADMIN, "PRIV-User-Admin"]


def test_skus_and_domains():
    rest, *_ = make()
    sku = rest.list_skus()[0]
    assert (sku.part_number, sku.enabled, sku.consumed) == ("AAD_PREMIUM_P2", 100, 1)
    assert rest.organization_domains() == [DOMAIN]


def test_an_unknown_group_is_a_clear_error():
    rest, *_ = make()
    with pytest.raises(GraphRequestError, match="group SG-None not found"):
        rest.group_members("SG-None")


# --- sign-in and scopes


def test_scopes_are_read_only_for_a_plan_and_listed_for_apply():
    assert not [s for s in connect.READ_SCOPES if "ReadWrite" in s]
    assert {
        "User.ReadWrite.All",
        "Group.ReadWrite.All",
        "GroupMember.ReadWrite.All",
        "RoleManagement.ReadWrite.Directory",
    } <= set(connect.WRITE_SCOPES)
    assert (
        connect.scopes_for(False) == connect.READ_SCOPES
        and connect.scopes_for(True) == connect.WRITE_SCOPES
    )
    assert len(set(connect.WRITE_SCOPES)) == len(connect.WRITE_SCOPES)
    assert not [
        s
        for s in connect.WRITE_SCOPES + connect.READ_SCOPES
        if s.startswith(("Application", "Policy", "Mail", "Files"))
    ]


def test_device_code_sign_in_uses_the_first_party_client(monkeypatch):
    seen = {}

    class FakeCredential:
        def __init__(self, **kw):
            seen["init"] = kw
            self.kw = kw

        def get_token(self, *scopes):
            seen["scopes"] = scopes
            self.kw["prompt_callback"]("https://microsoft.com/devicelogin", "ABCD1234", None)
            return types.SimpleNamespace(token="the-access-token-value")

    monkeypatch.setitem(
        sys.modules, "azure.identity", types.SimpleNamespace(DeviceCodeCredential=FakeCredential)
    )
    shown = []
    get = connect.device_code_token("t.example", connect.READ_SCOPES, shown.append)
    assert get() == "the-access-token-value"
    assert (
        seen["init"]["client_id"] == "14d82eec-204b-4c2f-b7e8-296a70dab67e"
        and seen["init"]["tenant_id"] == "t.example"
    )
    assert seen["scopes"][0] == "https://graph.microsoft.com/User.Read.All" and len(
        seen["scopes"]
    ) == len(connect.READ_SCOPES)
    assert shown and "ABCD1234" in shown[0] and "the-access-token-value" not in shown[0]
