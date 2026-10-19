import json
import os
import subprocess

import pytest
from test_eval_harness import cfg, fake_claude, final  # noqa: F401  (fixture import)

from eval import harness
from iamagents import tenant
from iamagents.gateway.config import ROOT
from iamagents.gateway.graph import fake_org

FAKE_DOMAIN = "contoso-example.onmicrosoft.com"


def test_apply_replaces_the_placeholder_everywhere_in_a_structure():
    row = {
        "a": f"x@{tenant.PLACEHOLDER}",
        "b": [f"y@{tenant.PLACEHOLDER}", 3],
        "c": {"d": tenant.PLACEHOLDER},
        "e": None,
    }
    out = tenant.apply(row, FAKE_DOMAIN)
    assert tenant.PLACEHOLDER not in json.dumps(out) and out["b"][1] == 3 and out["e"] is None
    assert out["a"] == f"x@{FAKE_DOMAIN}"
    assert tenant.apply(row, None) is row  # no domain: unchanged


def test_to_placeholder_is_the_inverse_and_assert_absent_catches_leftovers():
    text = f"zoe@{FAKE_DOMAIN} and {FAKE_DOMAIN}"
    back = tenant.to_placeholder(text, FAKE_DOMAIN)
    assert back == f"zoe@{tenant.PLACEHOLDER} and {tenant.PLACEHOLDER}"
    tenant.assert_absent(back, FAKE_DOMAIN)
    with pytest.raises(tenant.TenantDomainError):
        tenant.assert_absent(text, FAKE_DOMAIN)
    tenant.assert_absent(text, None)


@pytest.mark.parametrize(
    "bad", ["", "not a domain", "-x.com", "x", tenant.PLACEHOLDER, "a b.onmicrosoft.com"]
)
def test_invalid_domains_are_rejected(bad):
    with pytest.raises(tenant.TenantDomainError):
        tenant.validate(bad)


def test_validate_normalises_case_and_whitespace():
    assert tenant.validate("  Contoso-Example.onmicrosoft.com ") == FAKE_DOMAIN


def test_tenant_domain_reads_the_environment(monkeypatch):
    monkeypatch.setenv("TENANT_DOMAIN", FAKE_DOMAIN)
    assert tenant.tenant_domain(load_env=False) == FAKE_DOMAIN
    monkeypatch.setenv("TENANT_DOMAIN", "")
    assert tenant.tenant_domain(load_env=False) is None


def test_fake_org_uses_the_given_domain():
    org = fake_org.build_org(FAKE_DOMAIN)
    assert all(u["userPrincipalName"].endswith("@" + FAKE_DOMAIN) for u in org["users"].values())
    assert f"admin@{FAKE_DOMAIN}" in org["users"]
    assert all(
        o.endswith("@" + FAKE_DOMAIN)
        for g in org["groups"].values()
        for o in g["owners"] + g["members"]
    )
    assert all(m.endswith("@" + FAKE_DOMAIN) for m in org["rmau_users"])
    assert "<tenant>" in str(fake_org.build_org())  # the default stays the placeholder


def test_the_dataset_is_mapped_in_at_load_time_only():
    mapped = harness.load_dataset(harness.DATASET, FAKE_DOMAIN)
    assert tenant.PLACEHOLDER not in json.dumps(mapped) and FAKE_DOMAIN in json.dumps(mapped)
    assert tenant.PLACEHOLDER in harness.DATASET.read_text(
        "utf-8"
    )  # the file keeps the placeholder
    assert FAKE_DOMAIN not in harness.DATASET.read_text("utf-8")


def test_mapped_run_reaches_the_gateway_with_the_real_domain_but_reports_keep_the_placeholder(
    tmp_path, fake_claude
):  # noqa: F811
    call = ["reset_password", {"ticket_id": "IAM-007", "upn": f"ben.altmann@{FAKE_DOMAIN}"}]
    sc = {"calls": [call], "final": final(actions=["DS-02 reset_password ben.altmann"])}
    claude = fake_claude({"IAM-007": sc})
    # the fake CLI sends the placeholder domain; map it the way a model would copy it from the ticket
    c = cfg(
        tmp_path,
        agent="ds",
        ids=["IAM-007"],
        claude=claude,
        tenant_domain=FAKE_DOMAIN,
        timeout=60,
        milestone=True,
    )
    rep = harness.run_eval(c, log=lambda *_: None)

    work_audit = (tmp_path / "work" / "t" / "audit.jsonl").read_text("utf-8")
    assert FAKE_DOMAIN in work_audit  # the gateway (local state) works with the real domain
    assert (
        '"decision":"allow_plan"' in work_audit
    )  # the write was allowed: requester and target match
    rec = json.loads(next((tmp_path / "work").rglob("fake_record.json")).read_text("utf-8"))
    assert (
        f"requester: ben.altmann@{FAKE_DOMAIN}" in rec["prompt"]
    )  # the agent is told the real one

    for path in rep["paths"].values():
        text = open(path, encoding="utf-8").read()
        assert FAKE_DOMAIN not in text  # nothing that can be committed holds it
    assert rep["summary"]["tickets_run"] == 1 and rep["summary"]["policy_violations"] == 0
    assert rep["tickets"][0]["score"]["passed"], rep["tickets"][0]["failed"]


def test_a_report_that_still_holds_the_domain_is_refused(tmp_path, monkeypatch):
    from eval import report as reporting

    monkeypatch.setattr(
        tenant, "to_placeholder", lambda text, domain: text
    )  # simulate a broken mapping
    rep = {
        "meta": {"label": "x"},
        "summary": {"policy_violations": 0},
        "tickets": [{"u": f"a@{FAKE_DOMAIN}"}],
        "skipped": [],
    }
    with pytest.raises(tenant.TenantDomainError):
        reporting.write_reports(rep, True, [], tmp_path, domain=FAKE_DOMAIN)
    assert not list(tmp_path.rglob("*.*"))


def test_cli_maps_the_domain_from_the_environment(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(harness.tenant, "tenant_domain", lambda: FAKE_DOMAIN)
    monkeypatch.setattr(
        harness,
        "run_eval",
        lambda c: (
            seen.setdefault("cfg", c)
            and {
                "summary": {"passed": 0, "tickets_run": 0, "policy_violations": 0},
                "meta": {"label": "x"},
                "paths": {"markdown": "m"},
            }
        ),
    )
    harness.main(["--agent", "stub"])
    assert seen["cfg"].tenant_domain == FAKE_DOMAIN


def env_values(*names):
    """Values of the given variables in .env (never printed)."""
    from iamagents.gateway.config import load_env_file

    load_env_file(ROOT / ".env")
    return [v.strip().lower() for n in names if (v := os.environ.get(n, "")).strip()]


def test_real_tenant_values_are_in_no_tracked_file_and_not_in_history():
    """Guard for CLAUDE.md rule 4: the tenant domain and both admin addresses live in .env only."""
    values = env_values("TENANT_DOMAIN", "TENANT_ADMIN_UPN", "BREAKGLASS_UPN")
    if not values:
        pytest.skip("no tenant values in .env")
    files = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True
    ).stdout.split()
    for f in files:
        text = (ROOT / f).read_text("utf-8", errors="ignore").lower()
        assert not [v for v in values if v in text], f  # the file name only, never the value
    log = subprocess.run(
        ["git", "log", "--all", "-p"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    ).stdout.lower()
    assert not [v for v in values if v in log]
