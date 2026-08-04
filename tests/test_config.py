from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mega_nemo.config import ConfigError, find_root, load_manifest, load_settings

MANIFEST = textwrap.dedent(
    """
    version: 1
    defaults:
      fork_owner: testowner
      root: repos
      pull_strategy: rebase
    sources:
      - name: keystone
        upstream: https://github.com/tacoda/keystone.git
        fork: https://github.com/{fork_owner}/keystone.git
        role: charter
        install:
          kind: binary
      - name: readonly
        upstream: https://github.com/x/y.git
        triangle: false
        install:
          kind: none
    workspaces:
      - name: app
        path: .
    """
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "repos.yaml").write_text(MANIFEST, encoding="utf-8")
    return tmp_path


def test_fork_url_is_templated(repo: Path) -> None:
    m = load_manifest(repo)
    assert m.source("keystone").fork == "https://github.com/testowner/keystone.git"


def test_fork_owner_override_wins(repo: Path) -> None:
    m = load_manifest(repo, fork_owner="someone-else")
    assert m.source("keystone").fork.startswith("https://github.com/someone-else/")


def test_env_fork_owner(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEGA_FORK_OWNER", "envowner")
    assert load_manifest(repo).fork_owner == "envowner"


def test_triangle_false_needs_no_fork(repo: Path) -> None:
    m = load_manifest(repo)
    assert m.source("readonly").fork == ""
    assert [s.name for s in m.triangle_sources] == ["keystone"]


def test_triangle_true_without_fork_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "repos.yaml").write_text(
        "version: 1\ndefaults:\n  fork_owner: o\nsources:\n"
        "  - name: a\n    upstream: https://github.com/x/y.git\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="no fork URL"):
        load_manifest(tmp_path)


def test_unknown_source_lists_known(repo: Path) -> None:
    with pytest.raises(ConfigError, match="keystone"):
        load_manifest(repo).source("nope")


def test_find_root_walks_up(repo: Path) -> None:
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    assert find_root(nested) == repo


def test_settings_reject_unknown_key(repo: Path) -> None:
    (repo / "mega.toml").write_text('[sandbox]\nbogus = 1\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown setting"):
        load_settings(repo)


def test_settings_precedence_local_over_base(repo: Path) -> None:
    (repo / "mega.toml").write_text('[sandbox]\nprovider = "build"\n', encoding="utf-8")
    (repo / "mega.local.toml").write_text('[sandbox]\nprovider = "openai"\n', encoding="utf-8")
    assert load_settings(repo).provider == "openai"


def test_mega_env_beats_nemoclaw_env(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEGA_PROVIDER", "openai")
    monkeypatch.setenv("NEMOCLAW_PROVIDER", "build")
    assert load_settings(repo).provider == "openai"


def test_nemoclaw_env_used_when_mega_absent(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEGA_PROVIDER", raising=False)
    monkeypatch.setenv("NEMOCLAW_PROVIDER", "gemini")
    assert load_settings(repo).provider == "gemini"
