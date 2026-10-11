import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from iamagents import launch
from iamagents.gateway.config import ROOT

TOKENS = {
    f"GW_TOKEN_{a}": f"tok-{a.lower()}-XXXXXXXXXXXXXXXXXXXX"
    for a in ("DS", "AM", "IG", "PAM", "TL")
}
SOURCE = ROOT / "runtime" / ".claude"


@pytest.fixture
def rendered():
    path = launch.render_runtime(TOKENS)
    yield path
    launch.cleanup(path)


def test_tokens_are_filled_in_each_agent_file(rendered):
    for name, key in (
        ("ds", "DS"),
        ("am", "AM"),
        ("ig", "IG"),
        ("pam", "PAM"),
        ("team-lead", "TL"),
    ):
        text = (rendered / ".claude" / "agents" / f"{name}.md").read_text(encoding="utf-8")
        assert f"Bearer {TOKENS[f'GW_TOKEN_{key}']}" in text
        assert "${GW_TOKEN" not in text
        for other, value in TOKENS.items():
            if other != f"GW_TOKEN_{key}":
                assert value not in text  # an agent file holds only its own token


def test_tokens_appear_nowhere_but_the_agent_files(rendered):
    for f in rendered.rglob("*"):
        if f.is_file() and f.parent.name != "agents":
            text = f.read_text(encoding="utf-8", errors="replace")
            assert not any(v in text for v in TOKENS.values()), f


def test_rendered_agent_files_still_parse(rendered):
    for f in (rendered / ".claude" / "agents").glob("*.md"):
        match = re.match(r"^---\r?\n(.*?)\r?\n---", f.read_text(encoding="utf-8"), re.S)
        assert yaml.safe_load(match.group(1))["name"] == f.stem


def test_hook_commands_use_the_absolute_interpreter(rendered):
    settings = json.loads((rendered / ".claude" / "settings.json").read_text(encoding="utf-8"))
    commands = [h["command"] for es in settings["hooks"].values() for e in es for h in e["hooks"]]
    assert commands
    for command in commands:
        assert "uv run" not in command and ".." not in command
        assert command.startswith('"' + sys.executable.replace("\\", "/") + '" -m iamagents.hooks.')


def test_the_source_templates_are_not_modified(rendered):
    assert "${GW_TOKEN_DS}" in (SOURCE / "agents" / "ds.md").read_text(encoding="utf-8")
    assert "uv run --project .." in (SOURCE / "settings.json").read_text(encoding="utf-8")
    for f in SOURCE.rglob("*"):
        if f.is_file():
            assert not any(
                v in f.read_text(encoding="utf-8", errors="replace") for v in TOKENS.values()
            )


def test_missing_token_is_an_error_and_leaves_no_directory_behind():
    before = set(launch.RUNTIME_ROOT.glob("run-*")) if launch.RUNTIME_ROOT.exists() else set()
    with pytest.raises(KeyError, match="GW_TOKEN_PAM"):
        launch.render_runtime({k: v for k, v in TOKENS.items() if k != "GW_TOKEN_PAM"})
    after = set(launch.RUNTIME_ROOT.glob("run-*"))
    assert after == before  # a half rendered runtime (it may hold tokens) is removed


def test_rendering_outside_the_repo_is_refused(tmp_path):
    # Claude Code skips the inline MCP servers of agent files in untrusted directories (S10)
    with pytest.raises(ValueError, match="inside the repository"):
        launch.render_runtime(TOKENS, dest=Path(tempfile.gettempdir()) / "iam-elsewhere")


def test_default_destination_is_under_the_gitignored_state_dir_and_cleanup_removes_it():
    path = launch.render_runtime(TOKENS)
    try:
        assert path.resolve().is_relative_to(launch.RUNTIME_ROOT.resolve())
        assert (path / ".claude" / "agents" / "ds.md").exists()
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", str(path / ".claude" / "agents" / "ds.md")], cwd=ROOT
        )
        assert ignored.returncode == 0  # a rendered file (it holds tokens) can never be committed
    finally:
        launch.cleanup(path)
    assert not path.exists()


def test_scrubbed_env_keeps_only_the_allowlist():
    base = {
        "PATH": "/bin",
        "SystemRoot": "C:\\Windows",
        "USERPROFILE": "C:\\Users\\x",
        "GW_TOKEN_DS": "secret",
        "GW_ADMIN_TOKEN": "secret",
        "ANTHROPIC_API_KEY": "secret",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "TENANT_ID": "secret",
        "RANDOM_THING": "x",
    }
    env = launch.scrubbed_env(base, extra={"IAM_STATE_DIR": "/tmp/s"})
    assert set(env) == {"PATH", "SystemRoot", "USERPROFILE", "IAM_STATE_DIR"}
    assert "secret" not in json.dumps(env)


def test_scrubbed_env_of_the_real_environment_holds_no_gateway_token(monkeypatch):
    monkeypatch.setenv("GW_TOKEN_DS", "x")
    monkeypatch.setenv("GW_ADMIN_TOKEN", "y")
    env = launch.scrubbed_env()
    assert not [k for k in env if k.upper().startswith(("GW_", "ANTHROPIC", "TENANT", "AZURE"))]
