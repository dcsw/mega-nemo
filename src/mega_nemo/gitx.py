"""Triangular git workflow.

A *triangle* here is the standard fork workflow, wired the way git natively
supports it (``git help workflows``, "Triangular Workflows"):

        upstream (tacoda/keystone)
           |  fetch                    ^ pull request
           v                           |
        local clone  ---- push ---->  fork (dcsw/keystone)

Per repo we set:

    remote.upstream.url            canonical repo
    remote.upstream.pushurl        no-push://<name>   -- makes push-to-upstream fail loudly
    remote.origin.url              your fork
    remote.pushDefault             origin
    branch.<b>.remote              upstream           -- `git pull` follows upstream
    branch.<b>.pushRemote          origin             -- `git push` goes to the fork
    push.default                   current

So a bare ``git pull`` in any clone pulls from upstream and a bare ``git push``
pushes to your fork, with no flags and no way to accidentally push to upstream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import Manifest, Source
from .shell import CommandError, Result, run

#: Assigned as upstream's push URL, so `git push upstream` fails.
#:
#: git resolves an unknown URL scheme by exec'ing `git-remote-<scheme>`, then
#: reports `git: 'remote-<scheme>' is not a git command`. Putting the
#: explanation *in the scheme* turns that otherwise cryptic failure into an
#: instruction the reader can act on.
NO_PUSH_SCHEME = "upstream-is-read-only-use-mega-triangle-push"
NO_PUSH = f"{NO_PUSH_SCHEME}://upstream"

_SLUG_RE = re.compile(
    r"""(?:git@|https?://|ssh://git@)  # scheme
        (?P<host>[^/:]+)
        [:/]
        (?P<owner>[^/]+)
        /
        (?P<repo>[^/]+?)
        (?:\.git)?$""",
    re.VERBOSE,
)


def slug(url: str) -> str:
    """``https://github.com/tacoda/keystone.git`` -> ``tacoda/keystone``."""
    m = _SLUG_RE.match(url.strip())
    if not m:
        raise ValueError(f"cannot parse owner/repo out of {url!r}")
    return f"{m['owner']}/{m['repo']}"


def owner(url: str) -> str:
    return slug(url).split("/", 1)[0]


def git(repo: Path, *args: str, check: bool = True, **kw) -> Result:
    return run(["git", *args], cwd=repo, check=check, **kw)


def config_get(repo: Path, key: str) -> str | None:
    r = git(repo, "config", "--get", key, check=False, always_run=True)
    return r.out or None


def config_set(repo: Path, key: str, value: str) -> None:
    if config_get(repo, key) != value:
        git(repo, "config", key, value)


# ---------------------------------------------------------------------------


@dataclass
class RepoStatus:
    name: str
    path: Path
    exists: bool
    branch: str = ""
    clean: bool = True
    dirty_files: int = 0
    ahead_of_fork: int = 0
    behind_upstream: int = 0
    ahead_of_upstream: int = 0
    upstream_url: str = ""
    fork_url: str = ""
    push_blocked: bool = False
    wired: bool = False
    note: str = ""
    #: The upstream branch actually compared against — see :func:`effective_branch`.
    upstream_branch: str = ""

    @property
    def in_sync(self) -> bool:
        return self.exists and self.clean and self.behind_upstream == 0 and self.ahead_of_fork == 0


def current_branch(repo: Path) -> str:
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False, always_run=True).out


def is_clean(repo: Path) -> tuple[bool, int]:
    r = git(repo, "status", "--porcelain", check=False, always_run=True)
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    return (not lines, len(lines))


def _count(repo: Path, rev_range: str) -> int:
    r = git(repo, "rev-list", "--count", rev_range, check=False, always_run=True)
    return int(r.out) if r.ok and r.out.isdigit() else 0


def ref_exists(repo: Path, ref: str) -> bool:
    return git(repo, "rev-parse", "--verify", "--quiet", ref, check=False, always_run=True).ok


def remote_head(repo: Path, remote: str) -> str:
    """The remote's actual default branch, per ``refs/remotes/<remote>/HEAD``."""
    r = git(
        repo, "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD",
        check=False, always_run=True,
    )
    if r.ok and r.out.startswith(f"{remote}/"):
        return r.out[len(remote) + 1 :]
    # A clone made before HEAD was set, or a remote added by hand: ask the remote.
    r = git(repo, "remote", "show", remote, check=False, always_run=True, timeout=30)
    for line in r.stdout.splitlines():
        if "HEAD branch:" in line:
            branch = line.split("HEAD branch:", 1)[1].strip()
            if branch and branch != "(unknown)":
                return branch
    return ""


def effective_branch(m: Manifest, s: Source, repo: Path) -> tuple[str, str]:
    """The upstream branch to compare and integrate against.

    Manifests drift: an upstream can rename its default branch, or the manifest
    can simply be wrong (graphify's default is ``v8``, not ``main``). Comparing
    against a branch that does not exist silently produces nonsense drift
    counts, so prefer the manifest's value only when it actually exists on the
    remote, and fall back to the remote's real HEAD otherwise.

    Returns ``(branch, note)`` where ``note`` is empty when they agree.
    """
    if ref_exists(repo, f"{m.upstream_remote}/{s.default_branch}"):
        return s.default_branch, ""
    actual = remote_head(repo, m.upstream_remote)
    if not actual:
        return s.default_branch, f"{m.upstream_remote}/{s.default_branch} not found"
    return actual, f"manifest says {s.default_branch!r}, upstream default is {actual!r}"


# ---------------------------------------------------------------------------
# Clone + wire
# ---------------------------------------------------------------------------


def ensure_clone(m: Manifest, s: Source, *, depth: int | None = None) -> Path:
    """Clone from upstream if the working copy is missing."""
    path = s.path(m.clone_root)
    if (path / ".git").is_dir():
        return path

    m.clone_root.mkdir(parents=True, exist_ok=True)
    argv = ["git", "clone", "--origin", m.upstream_remote]
    if depth:
        argv += ["--depth", str(depth), "--no-single-branch"]
    argv += [s.upstream, str(path)]
    run(argv)
    return path


def wire_triangle(m: Manifest, s: Source, path: Path) -> RepoStatus:
    """Make the clone's remotes and branch config match the triangle."""
    status = RepoStatus(name=s.name, path=path, exists=True)

    if not (path / ".git").is_dir():
        status.exists = False
        status.note = "not cloned"
        return status

    existing = {
        line.split()[0]
        for line in git(path, "remote", check=False, always_run=True).stdout.splitlines()
        if line.strip()
    }

    # upstream: fetch-only.
    if m.upstream_remote in existing:
        git(path, "remote", "set-url", m.upstream_remote, s.upstream)
    else:
        git(path, "remote", "add", m.upstream_remote, s.upstream)
    git(path, "remote", "set-url", "--push", m.upstream_remote, NO_PUSH)

    if not s.triangle:
        status.upstream_url = s.upstream
        status.wired = True
        status.push_blocked = True
        status.note = "pull-only (triangle: false)"
        status.branch = current_branch(path)
        status.clean, status.dirty_files = is_clean(path)
        return status

    # origin: your fork, the push target.
    if m.fork_remote in existing:
        git(path, "remote", "set-url", m.fork_remote, s.fork)
    else:
        git(path, "remote", "add", m.fork_remote, s.fork)

    config_set(path, "remote.pushDefault", m.fork_remote)
    config_set(path, "push.default", "current")
    # Keep `git pull --rebase` the default so upstream history stays linear.
    config_set(path, "pull.rebase", "true")

    upstream_branch, drift = effective_branch(m, s, path)
    branch = current_branch(path) or upstream_branch
    if branch != "HEAD":
        config_set(path, f"branch.{branch}.remote", m.upstream_remote)
        config_set(path, f"branch.{branch}.merge", f"refs/heads/{upstream_branch}")
        config_set(path, f"branch.{branch}.pushRemote", m.fork_remote)

    status.note = drift
    status.branch = branch
    status.upstream_url = s.upstream
    status.fork_url = s.fork
    status.wired = True
    status.push_blocked = config_get(path, f"remote.{m.upstream_remote}.pushurl") == NO_PUSH
    status.clean, status.dirty_files = is_clean(path)
    return status


def fetch(m: Manifest, s: Source, path: Path, *, prune: bool = True) -> None:
    argv = ["fetch", m.upstream_remote, "--tags"]
    if prune:
        argv.append("--prune")
    git(path, *argv, check=False)
    if s.triangle:
        # The fork may not exist yet; a failure here is informational.
        git(path, "fetch", m.fork_remote, check=False)


def status(m: Manifest, s: Source, *, do_fetch: bool = False) -> RepoStatus:
    path = s.path(m.clone_root)
    if not (path / ".git").is_dir():
        return RepoStatus(name=s.name, path=path, exists=False, note="not cloned")

    if do_fetch:
        fetch(m, s, path)

    st = RepoStatus(name=s.name, path=path, exists=True)
    st.branch = current_branch(path)
    st.clean, st.dirty_files = is_clean(path)
    st.upstream_url = config_get(path, f"remote.{m.upstream_remote}.url") or ""
    st.fork_url = config_get(path, f"remote.{m.fork_remote}.url") or ""
    st.push_blocked = config_get(path, f"remote.{m.upstream_remote}.pushurl") == NO_PUSH
    st.wired = bool(st.upstream_url) and (not s.triangle or bool(st.fork_url))

    upstream_branch, drift = effective_branch(m, s, path)
    st.upstream_branch = upstream_branch
    st.note = drift

    up_ref = f"{m.upstream_remote}/{upstream_branch}"
    if ref_exists(path, up_ref):
        st.behind_upstream = _count(path, f"HEAD..{up_ref}")
        st.ahead_of_upstream = _count(path, f"{up_ref}..HEAD")
    else:
        st.note = f"no {up_ref} (fetch first)"

    if s.triangle and st.branch and st.branch != "HEAD":
        fork_ref = f"{m.fork_remote}/{st.branch}"
        if ref_exists(path, fork_ref):
            st.ahead_of_fork = _count(path, f"{fork_ref}..HEAD")
        else:
            st.ahead_of_fork = st.ahead_of_upstream
            if not st.note:
                st.note = "branch not on fork yet"

    return st


# ---------------------------------------------------------------------------
# Triangle verbs
# ---------------------------------------------------------------------------


class DirtyRepo(RuntimeError):
    pass


def pull(
    m: Manifest,
    s: Source,
    *,
    strategy: str | None = None,
    force: bool = False,
) -> RepoStatus:
    """Fetch upstream and integrate ``upstream/<default_branch>`` into HEAD."""
    path = ensure_clone(m, s)
    wire_triangle(m, s, path)
    fetch(m, s, path)

    clean, dirty = is_clean(path)
    if not clean and m.require_clean and not force:
        raise DirtyRepo(
            f"{s.name}: {dirty} uncommitted change(s) in {path}. "
            "Commit/stash them, or rerun with --force to stash automatically."
        )

    stashed = False
    if not clean and force:
        stashed = git(path, "stash", "push", "-u", "-m", "mega-triangle-pull", check=False).ok

    upstream_branch, _ = effective_branch(m, s, path)
    target = f"{m.upstream_remote}/{upstream_branch}"
    if not ref_exists(path, target):
        raise CommandError(Result(["git", "rev-parse", target], 1, "", f"{target} does not exist"))

    strategy = (strategy or m.pull_strategy).lower()
    try:
        if strategy == "rebase":
            git(path, "rebase", target)
        elif strategy == "merge":
            git(path, "merge", "--no-edit", target)
        elif strategy == "ff-only":
            git(path, "merge", "--ff-only", target)
        elif strategy == "reset":
            git(path, "reset", "--hard", target)
        else:
            raise ValueError(f"unknown pull strategy {strategy!r}")
    finally:
        if stashed:
            git(path, "stash", "pop", check=False)

    return status(m, s)


def push(
    m: Manifest,
    s: Source,
    *,
    branch: str | None = None,
    set_upstream: bool = True,
    force_with_lease: bool = False,
) -> RepoStatus:
    """Push HEAD (or ``branch``) to the fork. Never touches upstream."""
    path = s.path(m.clone_root)
    if not (path / ".git").is_dir():
        raise CommandError(Result(["git"], 1, "", f"{s.name}: not cloned; run `mega repos sync`"))
    if not s.triangle:
        raise ValueError(f"{s.name} is configured triangle: false — nothing to push to")

    wire_triangle(m, s, path)
    branch = branch or current_branch(path)
    if branch in {"", "HEAD"}:
        raise ValueError(f"{s.name}: detached HEAD; check out a branch first")

    argv = ["push"]
    if force_with_lease:
        argv.append("--force-with-lease")
    if set_upstream:
        argv.append("--set-upstream")
    argv += [m.fork_remote, f"{branch}:{branch}"]
    git(path, *argv)
    return status(m, s)


def start_branch(m: Manifest, s: Source, branch: str, *, from_upstream: bool = True) -> Path:
    """Create a topic branch off fresh upstream — the correct start of a triangle."""
    path = ensure_clone(m, s)
    wire_triangle(m, s, path)
    fetch(m, s, path)

    upstream_branch, _ = effective_branch(m, s, path)
    base = f"{m.upstream_remote}/{upstream_branch}" if from_upstream else "HEAD"
    if from_upstream and not ref_exists(path, base):
        raise CommandError(Result(["git", "rev-parse", base], 1, "", f"{base} does not exist"))

    if ref_exists(path, f"refs/heads/{branch}"):
        git(path, "checkout", branch)
    else:
        git(path, "checkout", "-b", branch, base)

    config_set(path, f"branch.{branch}.remote", m.upstream_remote)
    config_set(path, f"branch.{branch}.merge", f"refs/heads/{upstream_branch}")
    config_set(path, f"branch.{branch}.pushRemote", m.fork_remote)
    return path


def open_pr(
    m: Manifest,
    s: Source,
    *,
    branch: str | None = None,
    title: str | None = None,
    body: str | None = None,
    draft: bool = True,
    web: bool = False,
) -> Result:
    """Complete the triangle: fork branch -> PR against upstream, via `gh`."""
    path = s.path(m.clone_root)
    branch = branch or current_branch(path)
    fork_owner = owner(s.fork)
    base, _ = effective_branch(m, s, path)

    argv = [
        "gh",
        "pr",
        "create",
        "--repo",
        slug(s.upstream),
        "--base",
        base,
        "--head",
        f"{fork_owner}:{branch}",
    ]
    if draft:
        argv.append("--draft")
    if web:
        argv.append("--web")
    else:
        argv += ["--title", title or f"{branch}", "--body", body or ""]
    return run(argv, cwd=path)


def ensure_fork(m: Manifest, s: Source) -> bool:
    """Create the GitHub fork if it does not exist. Returns True if it exists now."""
    target = slug(s.fork)
    probe = run(["gh", "repo", "view", target, "--json", "name"], check=False, always_run=True)
    if probe.ok:
        return True
    created = run(
        ["gh", "repo", "fork", slug(s.upstream), "--clone=false", "--remote=false"],
        check=False,
    )
    return created.ok or created.dry_run
