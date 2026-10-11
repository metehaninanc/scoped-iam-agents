import re

from iamagents.gateway import tokens
from iamagents.gateway.config import AGENTS, ROOT

EXAMPLE = ROOT / ".env.example"


def values(path):
    return tokens._parse(path.read_text(encoding="utf-8"))


def test_example_lists_every_token_name():
    parsed = values(EXAMPLE)
    for name in tokens.NAMES:
        assert name in parsed and parsed[name] == "", name


def test_issue_creates_env_with_distinct_strong_tokens(tmp_path):
    env = tmp_path / ".env"
    written = tokens.issue(env, EXAMPLE)
    assert sorted(written) == sorted(tokens.NAMES)
    got = values(env)
    issued = [got[n] for n in tokens.NAMES]
    assert len(set(issued)) == len(tokens.NAMES) == len(AGENTS) + 1
    assert all(len(v) >= 40 and re.fullmatch(r"[A-Za-z0-9_-]+", v) for v in issued)
    assert got["GATEWAY_MODE"] == "plan"  # the rest of the example is kept


def test_issue_keeps_existing_values_and_fills_gaps(tmp_path):
    env = tmp_path / ".env"
    tokens.issue(env, EXAMPLE)
    before = values(env)
    text = env.read_text(encoding="utf-8").replace(
        f"GW_TOKEN_AM={before['GW_TOKEN_AM']}", "GW_TOKEN_AM="
    )
    env.write_text(text, encoding="utf-8")
    written = tokens.issue(env, EXAMPLE)
    assert written == ["GW_TOKEN_AM"]
    after = values(env)
    assert after["GW_TOKEN_AM"] and after["GW_TOKEN_AM"] != before["GW_TOKEN_AM"]
    assert all(after[n] == before[n] for n in tokens.NAMES if n != "GW_TOKEN_AM")


def test_rotate_replaces_everything(tmp_path):
    env = tmp_path / ".env"
    tokens.issue(env, EXAMPLE)
    before = values(env)
    assert sorted(tokens.issue(env, EXAMPLE, rotate=True)) == sorted(tokens.NAMES)
    after = values(env)
    assert all(after[n] != before[n] for n in tokens.NAMES)


def test_issue_does_not_touch_the_example(tmp_path):
    original = EXAMPLE.read_text(encoding="utf-8")
    tokens.issue(tmp_path / ".env", EXAMPLE)
    assert EXAMPLE.read_text(encoding="utf-8") == original


def test_main_prints_names_never_values(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tokens, "ROOT", tmp_path)
    (tmp_path / ".env.example").write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    assert tokens.main([]) == 0
    out = capsys.readouterr().out
    assert "GW_TOKEN_DS" in out
    for v in values(tmp_path / ".env").values():
        if len(v) >= 40:
            assert v not in out
    assert tokens.main([]) == 0
    assert "nothing" in capsys.readouterr().out


def test_issued_tokens_load_as_settings(tmp_path):
    from iamagents.gateway.config import Settings, load_env_file  # noqa: F401

    env = tmp_path / ".env"
    tokens.issue(env, EXAMPLE)
    got = values(env)
    settings = Settings.from_env({k: v for k, v in got.items()})
    assert set(settings.tokens) == set(AGENTS) and settings.admin_token
