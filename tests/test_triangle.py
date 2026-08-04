"""Triangle workflow tests against real local git repos.

Two bare repos stand in for GitHub: `upstream.git` and `fork.git`. Everything
else is the production code path — no mocks — so these tests catch real config
mistakes in the triangle wiring.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from mega_nemo import gitx
from mega_nemo.config import load_manifest


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def init_bare(path: Path) -> Path:
    path.mkdir(parents=True)
    sh("git", "init", "--bare", "-b", "main", ".", cwd=path)
    return path


def seed(upstream: Path, tmp: Path) -> None:
    work = tmp / "_seed"
    work.mkdir()
    sh("git", "init", "-b", "main", ".", cwd=work)
    sh("git", "config", "user.email", "t@example.com", cwd=work)
    sh("git", "config", "user.name", "Test", cwd=work)
    (work / "README.md").write_text("upstream v1\n", encoding="utf-8")
    sh("git", "add", "-A", cwd=work)
    sh("git", "commit", "-m", "initial", cwd=work)
    sh("git", "remote", "add", "origin", str(upstream), cwd=work)
    sh("git", "push", "-u", "origin", "main", cwd=work)


@pytest.fixture
def world(tmp_path: Path):
    upstream = init_bare(tmp_path / "upstream.git")
    fork = init_bare(tmp_path / "fork.git")
    seed(upstream, tmp_path)

    root = tmp_path / "mega"
    root.mkdir()
    (root / "repos.yaml").write_text(
        textwrap.dedent(f"""
        version: 1
        defaults:
          fork_owner: testowner
          root: repos
          pull_strategy: rebase
        sources:
          - name: demo
            upstream: {upstream}
            fork: {fork}
            default_branch: main
        """),
        encoding="utf-8",
    )
    m = load_manifest(root)
    return m, m.source("demo"), upstream, fork


def commit(repo: Path, text: str, message: str) -> None:
    (repo / "README.md").write_text(text, encoding="utf-8")
    sh("git", "config", "user.email", "t@example.com", cwd=repo)
    sh("git", "config", "user.name", "Test", cwd=repo)
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-m", message, cwd=repo)


# ---------------------------------------------------------------------------


def test_clone_and_wire(world) -> None:
    m, s, upstream, fork = world
    path = gitx.ensure_clone(m, s)
    st = gitx.wire_triangle(m, s, path)

    assert st.wired and st.push_blocked
    assert gitx.config_get(path, "remote.upstream.url") == str(upstream)
    assert gitx.config_get(path, "remote.origin.url") == str(fork)
    assert gitx.config_get(path, "remote.pushDefault") == "origin"
    assert gitx.config_get(path, "branch.main.remote") == "upstream"
    assert gitx.config_get(path, "branch.main.pushRemote") == "origin"


def test_upstream_push_is_blocked(world) -> None:
    m, s, _, _ = world
    path = gitx.ensure_clone(m, s)
    gitx.wire_triangle(m, s, path)
    commit(path, "local edit\n", "local")

    r = gitx.git(path, "push", "upstream", "main", check=False)
    assert not r.ok, "pushing to upstream must fail"
    # git reports the blocked scheme back, which is where the explanation lives.
    assert gitx.NO_PUSH_SCHEME in (r.stderr + r.stdout)


def test_bare_push_goes_to_fork(world) -> None:
    """`git push` with no arguments must land on the fork, not upstream."""
    m, s, upstream, fork = world
    path = gitx.ensure_clone(m, s)
    gitx.wire_triangle(m, s, path)
    commit(path, "fork-bound\n", "for the fork")

    gitx.git(path, "push")

    assert sh("git", "log", "-1", "--pretty=%s", "main", cwd=fork) == "for the fork"
    assert sh("git", "log", "-1", "--pretty=%s", "main", cwd=upstream) == "initial"


def test_pull_rebases_local_work_onto_upstream(world) -> None:
    m, s, upstream, _ = world
    path = gitx.ensure_clone(m, s)
    gitx.wire_triangle(m, s, path)
    commit(path, "mine\n", "my change")

    other = path.parent / "_other"
    subprocess.run(["git", "clone", str(upstream), str(other)], check=True, capture_output=True)
    (other / "NEW.md").write_text("upstream moved\n", encoding="utf-8")
    sh("git", "config", "user.email", "u@example.com", cwd=other)
    sh("git", "config", "user.name", "Up", cwd=other)
    sh("git", "add", "-A", cwd=other)
    sh("git", "commit", "-m", "upstream change", cwd=other)
    sh("git", "push", "origin", "main", cwd=other)

    st = gitx.pull(m, s)

    assert st.behind_upstream == 0
    assert st.ahead_of_upstream == 1
    log = sh("git", "log", "--pretty=%s", cwd=path).splitlines()
    assert log[0] == "my change" and log[1] == "upstream change"


def test_pull_refuses_dirty_tree(world) -> None:
    m, s, _, _ = world
    path = gitx.ensure_clone(m, s)
    gitx.wire_triangle(m, s, path)
    (path / "README.md").write_text("uncommitted\n", encoding="utf-8")

    with pytest.raises(gitx.DirtyRepo, match="uncommitted"):
        gitx.pull(m, s)


def test_pull_force_stashes_and_restores(world) -> None:
    m, s, _, _ = world
    path = gitx.ensure_clone(m, s)
    gitx.wire_triangle(m, s, path)
    (path / "scratch.txt").write_text("wip\n", encoding="utf-8")

    gitx.pull(m, s, force=True)

    assert (path / "scratch.txt").read_text(encoding="utf-8") == "wip\n"


def test_start_branch_bases_on_upstream(world) -> None:
    m, s, _, _ = world
    path = gitx.start_branch(m, s, "feature/x")

    assert gitx.current_branch(path) == "feature/x"
    assert gitx.config_get(path, "branch.feature/x.remote") == "upstream"
    assert gitx.config_get(path, "branch.feature/x.pushRemote") == "origin"


def test_push_topic_branch_to_fork_only(world) -> None:
    m, s, upstream, fork = world
    path = gitx.start_branch(m, s, "feature/y")
    commit(path, "feature work\n", "feat")

    gitx.push(m, s)

    assert "feature/y" in sh("git", "branch", "--list", "feature/y", cwd=fork)
    assert sh("git", "branch", "--list", "feature/y", cwd=upstream) == ""


def test_status_counts_drift(world) -> None:
    m, s, _, _ = world
    path = gitx.ensure_clone(m, s)
    gitx.wire_triangle(m, s, path)
    commit(path, "a\n", "one")
    commit(path, "b\n", "two")

    st = gitx.status(m, s, do_fetch=True)
    assert st.ahead_of_upstream == 2
    assert st.behind_upstream == 0
    assert st.ahead_of_fork == 2  # branch not on fork yet
    assert st.clean


def test_wire_is_idempotent(world) -> None:
    m, s, _, _ = world
    path = gitx.ensure_clone(m, s)
    first = gitx.wire_triangle(m, s, path)
    second = gitx.wire_triangle(m, s, path)
    assert (first.upstream_url, first.fork_url) == (second.upstream_url, second.fork_url)
    assert second.push_blocked
    remotes = sorted(gitx.git(path, "remote").stdout.split())
    assert remotes == ["origin", "upstream"]


def test_wrong_default_branch_falls_back_to_remote_head(world, tmp_path: Path) -> None:
    """A manifest that names a branch the upstream does not have must not
    silently produce nonsense drift counts (graphify's default is `v8`)."""
    m, s, upstream, _ = world
    object.__setattr__(s, "default_branch", "trunk")  # upstream only has `main`

    path = gitx.ensure_clone(m, s)
    gitx.wire_triangle(m, s, path)

    branch, note = gitx.effective_branch(m, s, path)
    assert branch == "main"
    assert "trunk" in note and "main" in note

    st = gitx.status(m, s, do_fetch=True)
    assert st.upstream_branch == "main"
    assert st.behind_upstream == 0 and st.ahead_of_upstream == 0


def test_correct_default_branch_reports_no_drift_note(world) -> None:
    m, s, _, _ = world
    path = gitx.ensure_clone(m, s)
    gitx.wire_triangle(m, s, path)
    branch, note = gitx.effective_branch(m, s, path)
    assert branch == "main" and note == ""


def test_pull_uses_remote_head_when_manifest_is_wrong(world) -> None:
    m, s, _, _ = world
    object.__setattr__(s, "default_branch", "trunk")
    gitx.ensure_clone(m, s)
    st = gitx.pull(m, s)  # must not raise "trunk does not exist"
    assert st.upstream_branch == "main"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/tacoda/keystone.git", "tacoda/keystone"),
        ("https://github.com/dsifry/metaswarm", "dsifry/metaswarm"),
        ("git@github.com:Graphify-Labs/graphify.git", "Graphify-Labs/graphify"),
        ("ssh://git@github.com/NVIDIA/NemoClaw.git", "NVIDIA/NemoClaw"),
    ],
)
def test_slug_parsing(url: str, expected: str) -> None:
    assert gitx.slug(url) == expected


def test_slug_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        gitx.slug("not a url")
