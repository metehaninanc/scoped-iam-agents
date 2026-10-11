"""Issue gateway bearer tokens into `.env`: `uv run python -m iamagents.gateway.tokens`.

Creates any missing GW_TOKEN_* and GW_ADMIN_TOKEN with `secrets.token_urlsafe(32)`. Existing
values are kept unless `--rotate` is given. Values are never printed. All tokens are distinct
(random 256 bit values; the check below is a guard against a hand edited duplicate).

Token isolation is configuration, not process isolation: every agent runs in one Claude Code
process, so every GW_TOKEN_* is in the same environment. See PERMISSIONS.md section 2.
"""

import secrets
import sys
from pathlib import Path

from .config import AGENTS, ROOT, ConfigError

NAMES = [f"GW_TOKEN_{a.upper()}" for a in AGENTS] + ["GW_ADMIN_TOKEN"]


def _parse(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def issue(env_path: Path, example_path: Path, rotate: bool = False) -> list[str]:
    """Write missing (or, with rotate, all) tokens. Returns the names that were written."""
    if not env_path.exists():
        env_path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")
    lines = env_path.read_text(encoding="utf-8").splitlines()
    current = _parse("\n".join(lines))
    written = []
    for name in NAMES:
        if rotate or not current.get(name):
            current[name] = secrets.token_urlsafe(32)
            written.append(name)
    values = [current[n] for n in NAMES]
    if len(set(values)) != len(values):
        raise ConfigError("duplicate tokens in .env")
    out, seen = [], set()
    for line in lines:
        key = line.partition("=")[0].strip()
        if key in NAMES and "=" in line and not line.lstrip().startswith("#"):
            out.append(f"{key}={current[key]}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{n}={current[n]}" for n in NAMES if n not in seen]
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return written


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    written = issue(ROOT / ".env", ROOT / ".env.example", rotate="--rotate" in argv)
    print("written: " + (", ".join(written) if written else "nothing (all tokens already set)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
