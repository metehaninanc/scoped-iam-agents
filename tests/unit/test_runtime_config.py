"""The runtime configuration is load-bearing for token isolation, so tests pin it.

All agents run in one Claude Code process, so every GW_TOKEN_* is in the same environment.
Token isolation is configuration, not process isolation. What stops an agent from reading
another agent's token or calling the gateway directly is the denial of Bash, Read, Write, Edit,
Glob, Grep, WebFetch and WebSearch. If one of those tools becomes available, the isolation is gone.
"""

import importlib
import json
import re

import pytest
import yaml

from iamagents.gateway.config import ROOT
from iamagents.gateway.policy import Policy

RUNTIME = ROOT / "runtime" / ".claude"
AGENTS_DIR = RUNTIME / "agents"
POLICY = Policy.load(ROOT / "policy" / "policy.yaml")

MUST_DENY = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch"]
# The deny list is the second layer behind `--tools` (an allowlist, see launch.BUILTIN_TOOLS). It
# also names the built-ins that survived a deny list in the S10 probe, ToolSearch among them.
ALSO_DENIED = [
    "PowerShell", "NotebookEdit", "ToolSearch", "Monitor", "SendMessage", "TaskStop", "TaskOutput",
    "CronCreate", "CronList", "CronDelete", "EnterWorktree", "ExitWorktree", "RemoteTrigger",
    "ListAgents", "ReportFindings", "PushNotification", "TodoWrite",
]  # fmt: skip
FORBIDDEN_FOR_AGENTS = MUST_DENY + ALSO_DENIED + ["*"]

# file name -> (gateway identity, may use the dispatch tool)
AGENT_FILES = {
    "ds": ("ds", False),
    "am": ("am", False),
    "ig": ("ig", False),
    "pam": ("pam", False),
    "team-lead": ("tl", True),
}


# Spike S10: an unrestricted `Agent` tool can dispatch built-in types such as general-purpose,
# which can load connector tools through ToolSearch. Dispatch is limited to our four agents.
DISPATCH = "Agent(ds, am, ig, pam)"


def settings():
    return json.loads((RUNTIME / "settings.json").read_text(encoding="utf-8"))


def frontmatter(name):
    text = (AGENTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    match = re.match(r"^---\r?\n(.*?)\r?\n---", text, re.S)
    assert match, name
    return yaml.safe_load(match.group(1))


@pytest.mark.parametrize("tool", MUST_DENY)
def test_settings_deny_the_tools_that_would_break_token_isolation(tool):
    assert tool in settings()["permissions"]["deny"]


@pytest.mark.parametrize("tool", ALSO_DENIED)
def test_settings_also_deny_the_other_builtins_seen_in_the_probe(tool):
    assert tool in settings()["permissions"]["deny"]


@pytest.mark.parametrize("tool", MUST_DENY)
def test_settings_never_allow_them(tool):
    allow = settings()["permissions"]["allow"]
    assert not [a for a in allow if a == tool or a.startswith(f"{tool}(")]


def our_skills():
    return {p.name for p in (RUNTIME / "skills").iterdir() if p.is_dir()}


def test_settings_allow_only_gateway_tools_agent_and_our_own_skills():
    for entry in settings()["permissions"]["allow"]:
        named = re.fullmatch(r"Skill\(([\w-]+)\)", entry)
        assert (
            entry == "Agent"
            or re.fullmatch(r"mcp__gw-(tl|ds|am|ig|pam)__\*", entry)
            or (named and named.group(1) in our_skills())
        ), entry


def test_the_skill_tool_is_allowed_for_our_skills_only():
    """First isolated smoke run: an agent with no tools invoked code-review, init and
    deep-research. A bare `Skill` allow rule permits every skill of the session; named rules
    permit ours only."""
    allow = settings()["permissions"]["allow"]
    assert "Skill" not in allow
    assert {e[6:-1] for e in allow if e.startswith("Skill(")} == our_skills()
    assert len(our_skills()) == 8


@pytest.mark.parametrize("name", AGENT_FILES)
def test_no_agent_lists_a_forbidden_tool(name):
    tools = frontmatter(name)["tools"]
    for tool in tools:
        assert tool not in FORBIDDEN_FOR_AGENTS, (name, tool)
        base = re.sub(r"\(.*\)$", "", tool)
        assert base not in FORBIDDEN_FOR_AGENTS, (name, tool)


@pytest.mark.parametrize("name", AGENT_FILES)
def test_each_agent_has_only_its_own_gateway_server_token_and_tools(name):
    identity, may_dispatch = AGENT_FILES[name]
    fm = frontmatter(name)
    assert fm["name"] == name
    servers = fm["mcpServers"]
    assert len(servers) == 1
    [(server_name, cfg)] = servers[0].items()
    assert server_name == f"gw-{identity}"
    assert cfg["headers"]["Authorization"] == f"Bearer ${{GW_TOKEN_{identity.upper()}}}"
    prefix = f"mcp__gw-{identity}__"
    gateway_tools = [t for t in fm["tools"] if t.startswith("mcp__")]
    assert all(t.startswith(prefix) for t in gateway_tools)
    # the agent file and policy.yaml list exactly the same tools
    assert sorted(t.removeprefix(prefix) for t in gateway_tools) == sorted(
        POLICY.tools_for(identity)
    )
    extras = set(fm["tools"]) - set(gateway_tools)
    assert extras == ({DISPATCH, "Skill"} if may_dispatch else {"Skill"})


def test_only_the_team_lead_can_dispatch_agents():
    for name, (_, may_dispatch) in AGENT_FILES.items():
        assert any(t.startswith("Agent") for t in frontmatter(name)["tools"]) is may_dispatch


def test_dispatch_is_limited_to_the_four_domain_agents():
    tools = frontmatter("team-lead")["tools"]
    assert DISPATCH in tools and "Agent" not in tools
    assert re.fullmatch(r"Agent\((.*)\)", DISPATCH).group(1).split(", ") == [
        "ds",
        "am",
        "ig",
        "pam",
    ]


def test_subagent_nesting_is_limited():
    assert settings()["env"]["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "1"


def test_agent_files_use_the_model_from_the_project_rules():
    for name in AGENT_FILES:
        assert frontmatter(name)["model"] == "sonnet"


def test_every_hook_command_points_at_an_importable_module():
    commands = []
    for entries in settings()["hooks"].values():
        for entry in entries:
            commands += [h["command"] for h in entry["hooks"]]
    assert commands
    for command in commands:
        module = re.search(r"-m (iamagents\.hooks\.\w+)", command).group(1)
        assert callable(importlib.import_module(module).main), command


def test_hooks_cover_the_expected_events():
    hooks = settings()["hooks"]
    assert set(hooks) == {"PreToolUse", "PostToolUse", "SubagentStop", "Stop"}
    matchers = {e["matcher"] for e in hooks["PreToolUse"]}
    assert "mcp__.*" in matchers and any("Agent" in m for m in matchers)


def test_policy_identities_cover_every_agent_file():
    assert {i for i, _ in AGENT_FILES.values()} == set(POLICY.identities)


def test_dev_claude_md_and_memory_are_kept_out_of_the_agents_context():
    """S10: the runtime lives inside the repo (trust is inherited), so it would otherwise load
    the development CLAUDE.md and the dev project's auto memory into every agent."""
    s = settings()
    assert "**/CLAUDE.md" in s["claudeMdExcludes"]
    assert s["autoMemoryEnabled"] is False


@pytest.mark.parametrize("name", AGENT_FILES)
def test_launcher_builtin_set_is_exactly_what_the_agent_file_lists(name):
    """--tools is the allowlist; the agent file and the launcher must name the same built-ins."""
    from iamagents import launch

    builtins = {
        re.sub(r"\(.*\)$", "", t) for t in frontmatter(name)["tools"] if not t.startswith("mcp__")
    }
    assert set(launch.BUILTIN_TOOLS[name].split(",")) == builtins
    assert set(launch.BUILTIN_TOOLS) == set(AGENT_FILES)
    assert builtins <= {"Skill", "Agent"}
