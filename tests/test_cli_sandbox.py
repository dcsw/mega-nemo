"""CLI tests for the sandbox passthrough commands.

Driven through Typer's CliRunner with `nemoclaw` stubbed, so these assert on the
argv mega builds and on the exit code it returns — the two things that make
`mega sandbox exec` usable in a script.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mega_nemo import cli, nemoclaw
from mega_nemo.shell import Result

runner = CliRunner()

MANIFEST = textwrap.dedent(
    """
    version: 1
    defaults:
      fork_owner: testowner
      root: repos
    sources:
      - name: keystone
        upstream: https://github.com/tacoda/keystone.git
        fork: https://github.com/{fork_owner}/keystone.git
    workspaces:
      - name: platform
        path: platform
        monorepo:
          enabled: true
          packages: ["apps/*"]
    """
)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "repos.yaml").write_text(MANIFEST, encoding="utf-8")
    (tmp_path / "mega.toml").write_text(
        '[sandbox]\nsandbox = "box"\nprovider = "build"\n'
        'sandbox_workspace = "/home/agent/workspace"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    # The CLI context is module-level and caches; reset it per test.
    cli.ctx = cli.Ctx()
    monkeypatch.setattr(nemoclaw, "exists", lambda name: True)
    return tmp_path


class ExecSpy:
    def __init__(self, code: int = 0) -> None:
        self.calls: list[dict] = []
        self.code = code

    def __call__(self, name, argv, **kw):
        self.calls.append({"name": name, "argv": argv, **kw})
        return Result(["nemoclaw"], self.code, "", "")

    @property
    def last(self) -> dict:
        return self.calls[-1]


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> ExecSpy:
    s = ExecSpy()
    monkeypatch.setattr(nemoclaw, "exec_in", s)
    return s


# --- exec -------------------------------------------------------------------


def test_exec_passes_the_command_through(repo: Path, spy: ExecSpy) -> None:
    result = runner.invoke(cli.app, ["sandbox", "exec", "--", "keystone", "lint"])
    assert result.exit_code == 0
    assert spy.last["argv"] == ["keystone", "lint"]
    assert spy.last["name"] == "box"


def test_exec_preserves_flags_meant_for_the_inner_command(repo: Path, spy: ExecSpy) -> None:
    """`--code-only` belongs to graphify, not to mega."""
    runner.invoke(cli.app, ["sandbox", "exec", "--", "graphify", "extract", ".", "--code-only"])
    assert spy.last["argv"] == ["graphify", "extract", ".", "--code-only"]


def test_exec_propagates_the_exit_code(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nemoclaw, "exec_in", ExecSpy(code=3))
    result = runner.invoke(cli.app, ["sandbox", "exec", "--", "false"])
    assert result.exit_code == 3


def test_exec_with_no_command_is_an_error(repo: Path, spy: ExecSpy) -> None:
    result = runner.invoke(cli.app, ["sandbox", "exec"])
    assert result.exit_code != 0
    assert spy.calls == []


def test_exec_workspace_shorthand_resolves_to_sandbox_path(repo: Path, spy: ExecSpy) -> None:
    runner.invoke(cli.app, ["sandbox", "exec", "-w", "platform", "--", "ls"])
    assert spy.last["workdir"] == "/home/agent/workspace/platform"


def test_exec_package_narrows_to_a_monorepo_package(repo: Path, spy: ExecSpy) -> None:
    runner.invoke(
        cli.app, ["sandbox", "exec", "-w", "platform", "--package", "apps/api", "--", "graphify", "update"]
    )
    assert spy.last["workdir"] == "/home/agent/workspace/platform/apps/api"


def test_exec_unknown_workspace_fails_before_running(repo: Path, spy: ExecSpy) -> None:
    result = runner.invoke(cli.app, ["sandbox", "exec", "-w", "nope", "--", "ls"])
    assert result.exit_code != 0
    assert spy.calls == []


def test_exec_package_without_workspace_is_rejected(repo: Path, spy: ExecSpy) -> None:
    result = runner.invoke(cli.app, ["sandbox", "exec", "--package", "apps/api", "--", "ls"])
    assert result.exit_code != 0
    assert spy.calls == []


def test_exec_workdir_and_workspace_are_mutually_exclusive(repo: Path, spy: ExecSpy) -> None:
    result = runner.invoke(
        cli.app, ["sandbox", "exec", "--workdir", "/tmp", "-w", "platform", "--", "ls"]
    )
    assert result.exit_code != 0
    assert spy.calls == []


def test_exec_raw_workdir_is_used_verbatim(repo: Path, spy: ExecSpy) -> None:
    runner.invoke(cli.app, ["sandbox", "exec", "--workdir", "/sandbox/.deepagents", "--", "ls"])
    assert spy.last["workdir"] == "/sandbox/.deepagents"


def test_exec_timeout_zero_means_no_limit(repo: Path, spy: ExecSpy) -> None:
    runner.invoke(cli.app, ["sandbox", "exec", "--timeout", "0", "--", "sleep", "1"])
    assert spy.last["timeout"] is None


def test_exec_streams_rather_than_capturing(repo: Path, spy: ExecSpy) -> None:
    runner.invoke(cli.app, ["sandbox", "exec", "--", "ls"])
    assert spy.last["capture"] is False


def test_exec_name_override(repo: Path, spy: ExecSpy) -> None:
    runner.invoke(cli.app, ["sandbox", "exec", "--name", "other", "--", "ls"])
    assert spy.last["name"] == "other"


# --- logs -------------------------------------------------------------------


class LogSpy:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, name, **kw):
        self.calls.append({"name": name, **kw})
        return Result(["nemoclaw"], 0, "", "")


@pytest.fixture
def logspy(monkeypatch: pytest.MonkeyPatch) -> LogSpy:
    s = LogSpy()
    monkeypatch.setattr(nemoclaw, "logs", s)
    return s


def test_logs_defaults(repo: Path, logspy: LogSpy) -> None:
    result = runner.invoke(cli.app, ["sandbox", "logs"])
    assert result.exit_code == 0
    assert logspy.calls[-1] == {"name": "box", "follow": False, "tail": None, "since": None}


def test_logs_follow_and_tail(repo: Path, logspy: LogSpy) -> None:
    runner.invoke(cli.app, ["sandbox", "logs", "-f", "-n", "200"])
    assert logspy.calls[-1]["follow"] is True
    assert logspy.calls[-1]["tail"] == 200


def test_logs_since(repo: Path, logspy: LogSpy) -> None:
    runner.invoke(cli.app, ["sandbox", "logs", "--since", "10m"])
    assert logspy.calls[-1]["since"] == "10m"


def test_logs_on_missing_sandbox_fails_clearly(
    repo: Path, logspy: LogSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nemoclaw, "exists", lambda name: False)
    result = runner.invoke(cli.app, ["sandbox", "logs"])
    assert result.exit_code != 0
    assert logspy.calls == []


# --- argv construction ------------------------------------------------------


def test_logs_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(
        nemoclaw, "run", lambda argv, **kw: (seen.append(argv), Result(argv, 0, "", ""))[1]
    )
    nemoclaw.logs("box", follow=True, tail=50, since="1h")
    assert seen[-1] == ["nemoclaw", "box", "logs", "--follow", "--tail", "50", "--since", "1h"]


def test_exec_argv_uses_double_dash_separator(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(
        nemoclaw, "run", lambda argv, **kw: (seen.append(argv), Result(argv, 0, "", ""))[1]
    )
    nemoclaw.exec_in("box", ["echo", "hi"], workdir="/w", timeout=60)
    assert seen[-1] == [
        "nemoclaw", "box", "exec", "--workdir", "/w", "--timeout", "60", "--no-tty", "--", "echo", "hi",
    ]


def test_exec_argv_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(
        nemoclaw, "run", lambda argv, **kw: (seen.append(argv), Result(argv, 0, "", ""))[1]
    )
    nemoclaw.exec_in("box", ["bash"], tty=True, timeout=None)
    assert "--tty" in seen[-1] and "--no-tty" not in seen[-1]
    assert "--timeout" not in seen[-1]
