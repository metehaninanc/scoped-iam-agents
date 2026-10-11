"""Launch Claude Code agents safely: render the runtime outside the repo, scrub the environment.

Spike S10 found these problems with running `claude -p` straight from `runtime/` with the
tokens in the environment:

1. Claude Code can substitute environment variable values into tool input: the value reaches the
   model's context, the transcript and, for gateway tools, the audit log. This is reported to the
   vendor and the details are withheld until it is fixed. Consequence: the process environment
   must hold no secret at all. `scrubbed_env` passes an allowlist only.
2. The tokens therefore live as literals in a rendered copy of the agent files, not in the
   environment. `render_runtime` writes that copy to a fresh directory and `cleanup` removes it.
3. Claude Code only loads the inline MCP servers of agent files in a trusted workspace, and it
   skips them silently otherwise. Trust is recorded per path and inherited by subdirectories, so
   the copy lives under the repo's gitignored `state/` (the repo must have been trusted once by
   opening `claude` in it). A directory outside the repo gets no gateway tools at all.
4. A directory inside the repo inherits the development `CLAUDE.md` and the dev project's auto
   memory. `runtime/.claude/settings.json` turns both off (`claudeMdExcludes`,
   `autoMemoryEnabled`), and a live test checks that the agents see neither.

The committed agent files keep `${GW_TOKEN_*}` placeholders; they are templates.
"""

import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .gateway.config import ROOT

# Variables Windows and the Claude CLI need. Nothing else is passed on.
ENV_ALLOW = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "PROGRAMDATA",
        "COMMONPROGRAMFILES",
        "ALLUSERSPROFILE",
        "PUBLIC",
        "USERNAME",
        "USERDOMAIN",
        "COMPUTERNAME",
        "OS",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
        "SESSIONNAME",
        "LOGONSERVER",
    }
)

RUNTIME_ROOT = ROOT / "state" / "runtime"  # gitignored; trust is inherited from the repo
_PLACEHOLDER = re.compile(r"\$\{(GW_TOKEN_[A-Z]+)\}")
_HOOK_PREFIX = "uv run --project .. python"


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
        return _render_into(dest, tokens, source, python)
    except BaseException:
        if created:  # never leave a half rendered directory behind: it may already hold tokens
            shutil.rmtree(dest, ignore_errors=True)
        raise


def _render_into(dest: Path, tokens: Mapping[str, str], source: Path, python: str | None) -> Path:
    claude_dir = dest / ".claude"
    shutil.copytree(source, claude_dir, dirs_exist_ok=True)
    for agent_file in (claude_dir / "agents").glob("*.md"):
        text = agent_file.read_text(encoding="utf-8")

        def fill(match: re.Match, name=agent_file.name) -> str:
            key = match.group(1)
            if not tokens.get(key):
                raise KeyError(f"no token for {key} (needed by {name})")
            return tokens[key]

        agent_file.write_text(_PLACEHOLDER.sub(fill, text), encoding="utf-8")
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
