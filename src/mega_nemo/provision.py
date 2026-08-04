"""Install keystone + metaswarm + graphify into a NemoClaw sandbox.

The provisioning contract, in order:

  1. policies   -- open the network egress each tool actually needs
  2. keystone   -- Go binary from a GitHub release; charter framework + MCP server
  3. metaswarm  -- prompt/skill trees, deployed with `nemoclaw skill install`
  4. graphify   -- the /graphify skill; indexes each workspace package
  5. workspaces -- uploaded, then charter-scaffolded and graph-indexed per package
  6. MCP        -- keystone's MCP server registered with the dcode agent

Every step is idempotent and independently runnable (`mega provision --only keystone`).
"""

from __future__ import annotations

import shlex
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import gitx, nemoclaw
from .config import Manifest, Settings, Source, Workspace
from .shell import console, run
from .workspace import Package, packages_for

STEPS = ("policies", "keystone", "metaswarm", "graphify", "workspaces", "mcp")


@dataclass
class StepResult:
    step: str
    ok: bool
    detail: str = ""
    skipped: bool = False


@dataclass
class ProvisionReport:
    sandbox: str
    results: list[StepResult] = field(default_factory=list)

    def add(self, step: str, ok: bool, detail: str = "", skipped: bool = False) -> None:
        self.results.append(StepResult(step, ok, detail, skipped))

    @property
    def ok(self) -> bool:
        return all(r.ok or r.skipped for r in self.results)


def _q(s: str) -> str:
    return shlex.quote(s)


# ---------------------------------------------------------------------------
# 1. policies
# ---------------------------------------------------------------------------


def apply_policies(sandbox: str, presets: list[str], report: ProvisionReport) -> None:
    existing = set(nemoclaw.policy_list(sandbox))
    added, failed = [], []
    for preset in presets:
        if preset in existing:
            continue
        r = nemoclaw.policy_add(sandbox, preset)
        (added if r.ok or r.dry_run else failed).append(preset)
    detail = f"added {added or 'nothing'}" + (f", failed {failed}" if failed else "")
    report.add("policies", not failed, detail)


# ---------------------------------------------------------------------------
# 2. keystone (binary from a GitHub release)
# ---------------------------------------------------------------------------


_ARCH_CACHE: dict[str, str] = {}

#: `uname -m` -> the token GoReleaser uses in keystone's asset names.
_ARCH_MAP = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}


def _sandbox_arch(sandbox: str) -> str:
    if sandbox not in _ARCH_CACHE:
        r = nemoclaw.sh_in(sandbox, "uname -m", check=False)
        _ARCH_CACHE[sandbox] = _ARCH_MAP.get(r.out.strip(), "x86_64")
    return _ARCH_CACHE[sandbox]


def install_keystone(
    sandbox: str, source: Source, settings: Settings, report: ProvisionReport
) -> None:
    spec = source.install
    version = settings.keystone_version or str(spec.get("version", "4.0.0"))
    # The release publishes linux_x86_64 and linux_arm64; ask the sandbox which
    # it needs rather than assuming the host's architecture.
    arch = _sandbox_arch(sandbox)
    asset = str(spec.get("asset", "keystone_{version}_linux_{arch}.tar.gz")).format(
        version=version, arch=arch
    )
    url = str(spec.get("release_url", "")).format(version=version, asset=asset)
    dest = str(spec.get("dest", "/usr/local/bin/keystone"))

    if not url:
        report.add("keystone", False, "no release_url in manifest")
        return

    probe = nemoclaw.sh_in(sandbox, "command -v keystone && keystone version", check=False)
    if probe.ok and version in probe.stdout:
        report.add("keystone", True, f"already at v{version}", skipped=True)
        return

    # Tarball or bare binary — the manifest's asset name decides.
    if asset.endswith((".tar.gz", ".tgz")):
        script = f"""
set -euo pipefail
tmp=$(mktemp -d)
curl -fsSL {_q(url)} -o "$tmp/keystone.tgz"
tar -xzf "$tmp/keystone.tgz" -C "$tmp"
bin=$(find "$tmp" -type f -name keystone -perm -u+x | head -1)
[ -n "$bin" ] || bin=$(find "$tmp" -type f -name 'keystone*' ! -name '*.tgz' | head -1)
[ -n "$bin" ] || {{ echo "no keystone binary in {asset}" >&2; exit 1; }}
chmod +x "$bin"
(sudo install -m 0755 "$bin" {_q(dest)} 2>/dev/null) || install -m 0755 "$bin" {_q(dest)}
rm -rf "$tmp"
keystone version
"""
    else:
        script = f"""
set -euo pipefail
tmp=$(mktemp -d)
curl -fsSL {_q(url)} -o "$tmp/keystone"
chmod +x "$tmp/keystone"
(sudo install -m 0755 "$tmp/keystone" {_q(dest)} 2>/dev/null) || install -m 0755 "$tmp/keystone" {_q(dest)}
rm -rf "$tmp"
keystone version
"""

    r = nemoclaw.sh_in(sandbox, script, check=False)
    report.add("keystone", r.ok or r.dry_run, r.out or r.stderr.strip()[:200])


# ---------------------------------------------------------------------------
# 3 + 4. skill trees (metaswarm, graphify)
# ---------------------------------------------------------------------------


def install_skills(
    m: Manifest, sandbox: str, source: Source, report: ProvisionReport
) -> None:
    path = source.path(m.clone_root)
    if not path.is_dir():
        report.add(source.name, False, f"not cloned; run `mega repos sync {source.name}`")
        return

    globs = source.install.get("skill_globs") or ["skills/*"]
    installed, failed = [], []
    seen: set[Path] = set()

    for pattern in globs:
        for candidate in sorted(path.glob(pattern)):
            if not candidate.is_dir() or candidate in seen:
                continue
            # A skill directory is one with a SKILL.md at its root.
            if not (candidate / "SKILL.md").is_file():
                continue
            seen.add(candidate)
            r = nemoclaw.skill_install(sandbox, candidate)
            (installed if r.ok or r.dry_run else failed).append(candidate.name)

    for item in source.install.get("copy") or []:
        src = path / item["src"]
        if not src.exists():
            if not item.get("optional"):
                failed.append(item["src"])
            continue
        # Relative to the runtime's own config dir, resolved from the sandbox.
        dest = f"{agent_config_dir(sandbox)}/{item['dest']}"
        nemoclaw.sh_in(sandbox, f"mkdir -p {_q(dest)}", check=False)
        r = nemoclaw.upload(sandbox, src, dest)
        if not (r.ok or r.dry_run):
            failed.append(item["src"])

    if not seen and not failed:
        report.add(
            source.name,
            False,
            f"no SKILL.md found under {globs} in {path} — check install.skill_globs",
        )
        return

    detail = f"{len(installed)} skill(s): {', '.join(installed[:6])}"
    if failed:
        detail += f" | failed: {', '.join(failed)}"
    report.add(source.name, not failed, detail)


_HOME_CACHE: dict[str, str] = {}
_CONFIG_DIR_CACHE: dict[str, str] = {}

#: Each agent runtime keeps its skills/agents/commands under its own directory.
#: From NemoClaw's agent manifests: langchain-deepagents-code declares
#: `dir: /sandbox/.deepagents`, and skill-install.ts falls back to
#: /sandbox/.openclaw for OpenClaw.
AGENT_CONFIG_DIRS = {
    "langchain-deepagents-code": "/sandbox/.deepagents",
    "openclaw": "/sandbox/.openclaw",
    "hermes": "/sandbox/.hermes",
}


def sandbox_home(sandbox: str) -> str:
    """The agent user's ``$HOME`` inside the sandbox.

    Asked once and cached rather than hardcoded, because it is image-dependent
    and every skill/config path hangs off it.
    """
    if sandbox not in _HOME_CACHE:
        r = nemoclaw.sh_in(sandbox, 'printf %s "$HOME"', check=False)
        _HOME_CACHE[sandbox] = r.out or "/home/agent"
    return _HOME_CACHE[sandbox]


def agent_config_dir(sandbox: str) -> str:
    """Where this sandbox's agent runtime keeps skills, agents and commands.

    Resolved by probing the sandbox first — the directory that exists is the
    authoritative answer — and falling back to the agent name recorded in
    ~/.nemoclaw/sandboxes.json. Getting this wrong means metaswarm's agents and
    commands are copied somewhere the runtime never reads, and nothing errors.
    """
    if sandbox in _CONFIG_DIR_CACHE:
        return _CONFIG_DIR_CACHE[sandbox]

    candidates = list(dict.fromkeys(AGENT_CONFIG_DIRS.values()))
    probe = nemoclaw.sh_in(
        sandbox,
        "for d in " + " ".join(_q(c) for c in candidates) + '; do [ -d "$d" ] && { printf %s "$d"; exit 0; }; done',
        check=False,
    )
    resolved = probe.out.strip()

    if not resolved:
        box = nemoclaw.get_sandbox(sandbox)
        agent = box.agent if box else "langchain-deepagents-code"
        resolved = AGENT_CONFIG_DIRS.get(agent, "/sandbox/.deepagents")

    _CONFIG_DIR_CACHE[sandbox] = resolved
    return resolved


def assemble_skill_dir(source_root: Path, variant: str, dest_root: Path) -> Path | None:
    """Build a standard ``<name>/SKILL.md`` + ``references/`` directory.

    graphify stores its skill as ``graphify/skill-<variant>.md`` plus
    ``graphify/skills/<variant>/references/``, which is not a shape
    ``nemoclaw skill install`` accepts. Assembling it here means nemoclaw — not
    a hardcoded path in mega — decides where the skill finally lands.
    """
    body = source_root / "graphify" / f"skill-{variant}.md"
    if not body.is_file():
        body = source_root / "graphify" / "skill.md"
    if not body.is_file():
        return None

    skill_dir = dest_root / "graphify"
    skill_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(body, skill_dir / "SKILL.md")

    refs = source_root / "graphify" / "skills" / variant / "references"
    if refs.is_dir():
        target = skill_dir / "references"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(refs, target)

    return skill_dir


def install_python_package(
    m: Manifest, sandbox: str, source: Source, report: ProvisionReport
) -> None:
    """Install a source that ships as a Python package plus a skill.

    graphify is the case this exists for. Two halves:
      1. the `graphifyy` package, whose `graphify` CLI does the actual parsing
      2. the skill markdown, deployed through `nemoclaw skill install` so it
         lands in whatever directory this sandbox's agent runtime reads
    """
    spec = source.install
    package = str(spec.get("package") or source.name)
    version = spec.get("version")
    requirement = f"{package}=={version}" if version else package
    variant = str(spec.get("skill_variant", "agents"))

    script = f"""
set -euo pipefail
if command -v uv >/dev/null 2>&1; then
  uv pip install --system -q {_q(requirement)} 2>/dev/null || uv tool install -q {_q(requirement)}
else
  python3 -m pip install --user -q {_q(requirement)}
fi
export PATH="$HOME/.local/bin:$PATH"
command -v {_q(source.name)} >/dev/null 2>&1 || {{ echo "{source.name} not on PATH after install" >&2; exit 1; }}
"""
    r = nemoclaw.sh_in(sandbox, script, check=False)
    if not (r.ok or r.dry_run):
        report.add(source.name, False, (r.stderr.strip() or r.out)[:200])
        return

    clone = source.path(m.clone_root)
    detail = requirement
    ok = True
    if clone.is_dir():
        staging = Path(tempfile.mkdtemp(prefix="mega-skill-"))
        try:
            skill_dir = assemble_skill_dir(clone, variant, staging)
            if skill_dir is None:
                ok = False
                detail += f" | no skill-{variant}.md in {clone}"
            else:
                sr = nemoclaw.skill_install(sandbox, skill_dir)
                ok = sr.ok or sr.dry_run
                detail += f" | skill '{variant}' " + ("installed" if ok else "install failed")
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    else:
        detail += " | skill skipped (not cloned)"

    report.add(source.name, ok, detail)


# ---------------------------------------------------------------------------
# 5. workspaces
# ---------------------------------------------------------------------------


@dataclass
class WorkspacePlan:
    workspace: Workspace
    host_path: Path
    sandbox_path: str
    packages: list[Package]

    @property
    def is_monorepo(self) -> bool:
        return len(self.packages) > 1 or (
            len(self.packages) == 1 and not self.packages[0].is_root
        )


def plan_workspaces(m: Manifest, settings: Settings, names: list[str] | None) -> list[WorkspacePlan]:
    plans = []
    targets = m.workspaces if not names else [m.workspace(n) for n in names]
    for w in targets:
        host = w.resolve(m.root)
        plans.append(
            WorkspacePlan(
                workspace=w,
                host_path=host,
                sandbox_path=f"{settings.sandbox_workspace.rstrip('/')}/{w.name}",
                packages=packages_for(m, w),
            )
        )
    return plans


def sync_workspace(sandbox: str, plan: WorkspacePlan, report: ProvisionReport) -> None:
    if not plan.host_path.is_dir():
        report.add(f"workspace:{plan.workspace.name}", False, f"{plan.host_path} does not exist")
        return

    nemoclaw.sh_in(sandbox, f"mkdir -p {_q(plan.sandbox_path)}", check=False)
    r = nemoclaw.upload(sandbox, plan.host_path, plan.sandbox_path)
    if not (r.ok or r.dry_run):
        report.add(f"workspace:{plan.workspace.name}", False, r.stderr.strip()[:200])
        return

    detail = f"{len(plan.packages)} package(s)"
    if plan.is_monorepo:
        detail += " [monorepo]"
    report.add(f"workspace:{plan.workspace.name}", True, detail)


def charter_workspace(sandbox: str, plan: WorkspacePlan, report: ProvisionReport) -> None:
    """`keystone init` + `index` + `lint`, once per package."""
    if not plan.workspace.charter:
        report.add(f"charter:{plan.workspace.name}", True, "disabled in manifest", skipped=True)
        return

    failures = []
    for pkg in plan.packages:
        target = plan.sandbox_path if pkg.is_root else f"{plan.sandbox_path}/{pkg.rel}"
        script = f"""
set -euo pipefail
cd {_q(target)}
[ -d .charter ] || keystone init
keystone index
keystone lint
"""
        r = nemoclaw.sh_in(sandbox, script, check=False)
        if not (r.ok or r.dry_run):
            failures.append(f"{pkg.rel}: {r.stderr.strip().splitlines()[-1] if r.stderr else '?'}")

    report.add(
        f"charter:{plan.workspace.name}",
        not failures,
        f"{len(plan.packages) - len(failures)}/{len(plan.packages)} charters"
        + (f" | {failures[0]}" if failures else ""),
    )


def graph_workspace(
    sandbox: str, plan: WorkspacePlan, report: ProvisionReport, *, source: Source | None = None
) -> None:
    """Build a graphify knowledge graph per package.

    Per-package graphs are what make monorepo support real: a query inside one
    package does not drag in the whole repo. The skill itself was already
    deployed by :func:`install_python_package`; this only runs the indexer.
    """
    if not plan.workspace.graph:
        report.add(f"graph:{plan.workspace.name}", True, "disabled in manifest", skipped=True)
        return

    spec = source.install if source else {}
    per_package = bool(spec.get("per_package", True))
    # `graphify build` does not exist — a graph is built with
    # `graphify extract <path>` (bare `graphify <path>` rewrites to the same).
    # --code-only is the deterministic AST path that provably needs no LLM key,
    # which is the only thing safe to run unattended at provision time.
    build_args = list(spec.get("build_args") or ["--code-only"])
    arg_str = " ".join(_q(a) for a in build_args)

    targets = [
        plan.sandbox_path if pkg.is_root else f"{plan.sandbox_path}/{pkg.rel}"
        for pkg in plan.packages
    ]
    if not per_package:
        targets = [plan.sandbox_path]

    built, failed = 0, []
    for target in targets:
        script = f"""
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
command -v graphify >/dev/null 2>&1 || {{ echo "graphify not installed" >&2; exit 3; }}
cd {_q(target)}
if [ -f graphify-out/graph.json ]; then graphify update; else graphify extract . {arg_str}; fi
"""
        r = nemoclaw.sh_in(sandbox, script, check=False)
        if r.ok or r.dry_run:
            built += 1
        else:
            tail = (r.stderr.strip().splitlines() or ["?"])[-1]
            failed.append(f"{target.rsplit('/', 1)[-1]}: {tail[:80]}")

    detail = f"{built}/{len(targets)} graph(s)"
    if failed:
        detail += f" | {failed[0]}"
    report.add(f"graph:{plan.workspace.name}", not failed, detail)


# ---------------------------------------------------------------------------
# 6. MCP
# ---------------------------------------------------------------------------


def register_mcp(sandbox: str, sources: list[Source], report: ProvisionReport) -> None:
    """Register keystone's MCP server with the in-sandbox agent.

    `nemoclaw mcp add` handles HTTP servers; keystone's server is a local stdio
    process, so it is registered through keystone's own installer, which writes
    the agent-native config inside the sandbox.
    """
    registered, failed = [], []
    for s in sources:
        if not s.mcp.get("register"):
            continue
        server = s.mcp.get("server_name", s.name)
        script = """
set -euo pipefail
if command -v keystone >/dev/null 2>&1; then
  keystone mcp install --agent claude-code 2>/dev/null \
    || keystone mcp install 2>/dev/null \
    || { echo "keystone mcp install unavailable in this build" >&2; exit 3; }
else
  echo "keystone not installed" >&2; exit 4
fi
"""
        r = nemoclaw.sh_in(sandbox, script, check=False)
        (registered if r.ok or r.dry_run else failed).append(server)

    if not registered and not failed:
        report.add("mcp", True, "no MCP servers declared", skipped=True)
        return
    report.add("mcp", not failed, f"registered {registered}" + (f", failed {failed}" if failed else ""))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def provision(
    m: Manifest,
    settings: Settings,
    *,
    sandbox: str,
    only: list[str] | None = None,
    workspaces: list[str] | None = None,
    extra_policies: list[str] | None = None,
) -> ProvisionReport:
    steps = set(only) if only else set(STEPS)
    unknown = steps - set(STEPS)
    if unknown:
        raise ValueError(f"unknown step(s) {sorted(unknown)}. Valid: {', '.join(STEPS)}")

    report = ProvisionReport(sandbox=sandbox)

    if "policies" in steps:
        spec_policies = list(nemoclaw.build_spec(settings, name=sandbox).policies)
        apply_policies(sandbox, spec_policies + list(extra_policies or []), report)

    by_name = {s.name: s for s in m.sources}

    if "keystone" in steps and (src := by_name.get("keystone")):
        install_keystone(sandbox, src, settings, report)

    for step in ("metaswarm", "graphify"):
        if step in steps and (src := by_name.get(step)):
            if src.install_kind == "skills":
                install_skills(m, sandbox, src, report)
            elif src.install_kind == "python-package":
                install_python_package(m, sandbox, src, report)
            else:
                report.add(step, True, f"install.kind={src.install_kind}", skipped=True)

    if "workspaces" in steps:
        graphify_src = by_name.get("graphify")
        for plan in plan_workspaces(m, settings, workspaces):
            sync_workspace(sandbox, plan, report)
            charter_workspace(sandbox, plan, report)
            graph_workspace(sandbox, plan, report, source=graphify_src)

    if "mcp" in steps:
        register_mcp(sandbox, list(m.sources), report)

    return report


def bootstrap_git_identity(m: Manifest, sandbox: str) -> None:
    """Carry the host's git identity into the sandbox so in-sandbox commits are
    attributable, and mirror the triangle push rules."""
    name = run(["git", "config", "--global", "user.name"], check=False, always_run=True).out
    email = run(["git", "config", "--global", "user.email"], check=False, always_run=True).out
    if not (name and email):
        console.print("[yellow]![/yellow] no global git identity on the host; skipping")
        return
    nemoclaw.sh_in(
        sandbox,
        f"git config --global user.name {_q(name)} && "
        f"git config --global user.email {_q(email)} && "
        f"git config --global remote.pushDefault {_q(m.fork_remote)} && "
        "git config --global push.default current && "
        "git config --global pull.rebase true",
        check=False,
    )


def sandbox_triangle_note(m: Manifest) -> str:
    """A short brief the agent can read to understand the push/pull rules."""
    lines = [
        "# Triangle git rules (enforced by mega-nemo)",
        "",
        f"- `{m.upstream_remote}` is read-only. Its push URL is `{gitx.NO_PUSH}`; pushing fails.",
        f"- `{m.fork_remote}` is the fork under `{m.fork_owner}/`. All pushes go there.",
        "- Start work with `mega triangle start <repo> <branch>` (branches off fresh upstream).",
        "- Ship with `mega triangle push <repo>` then `mega triangle pr <repo>`.",
        "",
        "| repo | upstream | fork | role |",
        "| --- | --- | --- | --- |",
    ]
    for s in m.sources:
        fork = gitx.slug(s.fork) if s.fork else "—"
        lines.append(f"| {s.name} | {gitx.slug(s.upstream)} | {fork} | {s.role or '—'} |")
    return "\n".join(lines) + "\n"
