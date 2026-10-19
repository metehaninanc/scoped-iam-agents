"""Launch Claude Code agents safely: render the runtime outside the repo, scrub the environment.

Spike S10 found these problems with running `claude -p` straight from `runtime/` with the
tokens in the environment:

1. Claude Code can substitute environment variable values into tool input: the value reaches the
   model's context, the transcript and, for gateway tools, the audit log. This was observed in
   our own runs and the mechanics are not published. Consequence: the process environment must
   hold no secret at all. `scrubbed_env` passes an allowlist only.
2. The tokens therefore live as literals in a rendered copy of the agent files, not in the
   environment. `render_runtime` writes that copy to a fresh directory and `cleanup` removes it.
3. Claude Code only loads the inline MCP servers of agent files in a trusted workspace, and it
   skips them silently otherwise. Trust is recorded per path and inherited by subdirectories, so
   the copy lives under the repo's gitignored `state/` (the repo must have been trusted once by
   opening `claude` in it). A directory outside the repo gets no gateway tools at all.
4. A directory inside the repo inherits the development `CLAUDE.md` and the dev project's auto
   memory. `runtime/.claude/settings.json` turns both off (`claudeMdExcludes`,
   `autoMemoryEnabled`), and a live test checks that the agents see neither.
5. The built-in tool set is an allowlist, not a blocklist. A deny list leaves every tool that is
   not on it (a dozen survived in the probe, among them `ToolSearch`, `SendMessage`,
   `RemoteTrigger`, `CronCreate`), and new Claude Code versions add more. `claude_command` passes
   `--tools` with the exact built-in set of each agent; the deny list in `settings.json` and each
   agent's own `tools` list are further layers. A live test compares every agent's init tool list
   with the expected set, so one extra tool fails it.

The committed agent files keep `${GW_TOKEN_*}` placeholders; they are templates.
"""

import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from .gateway.config import ROOT

# The variables Windows and the Claude CLI need, and nothing else. Whatever is in the environment
# can end up in the model's context (see the module docstring), so this is the minimum that
# still runs a full agent call with hooks (verified one variable at a time in S10 follow-up): no
# user name, computer name, domain or logon server, no program or data directories.
ENV_ALLOW = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
    }
)

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8765/mcp"  # as written in the agent files
RUNTIME_ROOT = ROOT / "state" / "runtime"  # gitignored; trust is inherited from the repo
_PLACEHOLDER = re.compile(r"\$\{(GW_TOKEN_[A-Z]+)\}")
_HOOK_PREFIX = "uv run --project .. python"


# The exact built-in tools of each agent, passed as `--tools`. MCP tools come from the agent files.
# The domain agents get the Skill tool only; the team lead also dispatches (limited to the four
# domain agents by `Agent(ds, am, ig, pam)` in its file).
BUILTIN_TOOLS = {
    "ds": "Skill",
    "am": "Skill",
    "ig": "Skill",
    "pam": "Skill",
    "team-lead": "Skill,Agent",
}
# The init message of a session lists the dispatch tool under this name.
INIT_NAME = {"Agent": "Task"}

# Dedicated Claude Code config directory for runtime runs: no user skills, settings or account
# connectors from the developer's own configuration. One interactive login and trust acceptance
# in it is needed (see `config_dir_ready`).
CONFIG_DIR = ROOT / "state" / "claude-config"


def config_dir_ready(path: Path = CONFIG_DIR) -> bool:
    """True once someone has logged in with CLAUDE_CONFIG_DIR pointing at `path`."""
    return (path / ".credentials.json").exists()


def config_dir_problem(path: Path = CONFIG_DIR, repo: Path = ROOT) -> str | None:
    """Why the dedicated config dir cannot run the agents yet, or None when it can.

    Two things are needed in it: a login, and the repo accepted in the trust dialog. Without
    the trust, Claude Code silently skips the inline MCP servers of the agent files and the
    agents run with no gateway tools at all (found in the first isolated smoke run).
    """
    how = f"run `claude` from {repo} with CLAUDE_CONFIG_DIR={path}"
    if not config_dir_ready(path):
        return f"not logged in in {path}: {how} and log in"
    try:
        state = json.loads((path / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return f"{path / '.claude.json'} is missing or unreadable: {how}"
    wanted = str(repo).replace("\\", "/").rstrip("/").lower()
    for key, value in (state.get("projects") or {}).items():
        if key.replace("\\", "/").rstrip("/").lower() == wanted and value.get(
            "hasTrustDialogAccepted"
        ):
            return None
    return f"the repository is not trusted in {path}: {how} and accept the trust dialog"


def scrubbed_env(
    base: Mapping[str, str] | None = None, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Allowlisted environment for `claude -p`. No tokens, no API keys, no other secrets."""
    base = os.environ if base is None else base
    env = {k: v for k, v in base.items() if k.upper() in ENV_ALLOW}
    if extra:
        env.update(extra)
    return env


def render_runtime(
    tokens: Mapping[str, str],
    dest: Path | None = None,
    source: Path = ROOT / "runtime" / ".claude",
    python: str | None = None,
    gateway_url: str | None = None,
) -> Path:
    """Copy the runtime config to a fresh directory and fill in the gateway tokens.

    Returns the directory to use as the working directory of `claude -p`. Placeholders are
    replaced in the agent files only. Hook commands are changed from `uv run --project ..`
    (relative to the repo) to the absolute interpreter, because the copy is outside the repo.
    """
    created = dest is None or not Path(dest).exists()
    if dest is None:
        RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        dest = Path(tempfile.mkdtemp(prefix="run-", dir=RUNTIME_ROOT))
    dest = Path(dest)
    if not dest.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError(
            "the rendered runtime must be inside the repository: Claude Code skips the inline "
            "MCP servers of agent files in untrusted directories, and trust is inherited "
            "from the repo"
        )
    try:
        return _render_into(dest, tokens, source, python, gateway_url)
    except BaseException:
        if created:  # never leave a half rendered directory behind: it may already hold tokens
            shutil.rmtree(dest, ignore_errors=True)
        raise


def _render_into(
    dest: Path,
    tokens: Mapping[str, str],
    source: Path,
    python: str | None,
    gateway_url: str | None = None,
) -> Path:
    claude_dir = dest / ".claude"
    shutil.copytree(source, claude_dir, dirs_exist_ok=True)
    for agent_file in (claude_dir / "agents").glob("*.md"):
        text = agent_file.read_text(encoding="utf-8")

        def fill(match: re.Match, name=agent_file.name) -> str:
            key = match.group(1)
            if not tokens.get(key):
                raise KeyError(f"no token for {key} (needed by {name})")
            return tokens[key]

        text = _PLACEHOLDER.sub(fill, text)
        if gateway_url:
            text = text.replace(DEFAULT_GATEWAY_URL, gateway_url)
        agent_file.write_text(text, encoding="utf-8")
    interpreter = (python or sys.executable).replace("\\", "/")
    settings_path = claude_dir / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    for entries in settings.get("hooks", {}).values():
        for entry in entries:
            for hook in entry["hooks"]:
                hook["command"] = hook["command"].replace(_HOOK_PREFIX, f'"{interpreter}"')
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return dest


def cleanup(runtime_dir: Path) -> None:
    """Remove a rendered runtime (it holds literal tokens)."""
    shutil.rmtree(runtime_dir, ignore_errors=True)


def claude_command(
    agent: str,
    prompt: str | None,
    *,
    model: str = "haiku",
    max_turns: int = 10,
    claude: str | None = None,
    extra: Sequence[str] = (),
) -> list[str]:
    """Argument list for one `claude -p` run of `agent`.

    `--tools` takes a variable number of values, so it is placed before another option and
    never directly before the prompt. With `prompt=None` the prompt is read from stdin, which
    the harness uses: on Windows `claude` is a `.cmd` shim run through cmd.exe, which cuts an
    argument at its first newline. Without a prompt argument and without piped input `claude -p`
    waits 3 seconds for stdin, so callers either pass `input=` or close stdin.
    """
    exe = claude or shutil.which("claude")
    if exe is None:
        raise FileNotFoundError("the claude CLI is not on PATH")
    return [
        exe,
        "-p",
        "--tools",
        BUILTIN_TOOLS[agent],
        "--agent",
        agent,
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        str(max_turns),
        *extra,
        *([prompt] if prompt is not None else []),
    ]
