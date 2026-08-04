"""Provisioning tests.

The sandbox is not available in CI, so `nemoclaw.sh_in` / `skill_install` /
`upload` are captured and the *scripts and argv* mega would run are asserted
against. That is the part that goes wrong in practice — a wrong glob, a wrong
asset name, a wrong platform flag.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mega_nemo import nemoclaw
from mega_nemo import provision as prov
from mega_nemo.config import Settings, load_manifest
from mega_nemo.shell import Result

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
        install:
          kind: binary
          version: "4.0.0"
          asset: "keystone_{version}_linux_{arch}.tar.gz"
          release_url: "https://github.com/tacoda/keystone/releases/download/v{version}/{asset}"
          dest: /usr/local/bin/keystone
        mcp:
          register: true
          server_name: keystone
      - name: metaswarm
        upstream: https://github.com/dsifry/metaswarm.git
        fork: https://github.com/{fork_owner}/metaswarm.git
        install:
          kind: skills
          skill_globs: ["skills/*"]
          copy:
            - src: agents
              dest: agents
              optional: true
            - src: commands
              dest: commands
              optional: true
      - name: graphify
        upstream: https://github.com/Graphify-Labs/graphify.git
        fork: https://github.com/{fork_owner}/graphify.git
        install:
          kind: python-package
          package: graphifyy
          version: "0.9.32"
          skill_variant: agents
          per_package: true
    workspaces:
      - name: app
        path: app
    """
)


class Recorder:
    """Stands in for the sandbox, recording what would have been run."""

    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.skills: list[Path] = []
        self.uploads: list[tuple[Path, str]] = []

    def sh_in(self, name, script, *, workdir=None, check=True):
        self.scripts.append(script)
        if script.strip() == 'printf %s "$HOME"':
            return Result(["sh"], 0, "/home/agent", "")
        if script.strip() == "uname -m":
            return Result(["sh"], 0, "x86_64", "")
        return Result(["sh"], 0, "", "")

    def skill_install(self, name, path):
        self.skills.append(path)
        return Result(["skill"], 0, "", "")

    def upload(self, name, host, dest):
        self.uploads.append((host, dest))
        return Result(["upload"], 0, "", "")

    @property
    def all_scripts(self) -> str:
        return "\n".join(self.scripts)


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    r = Recorder()
    monkeypatch.setattr(nemoclaw, "sh_in", r.sh_in)
    monkeypatch.setattr(nemoclaw, "skill_install", r.skill_install)
    monkeypatch.setattr(nemoclaw, "upload", r.upload)
    monkeypatch.setattr(nemoclaw, "policy_list", lambda name: [])
    monkeypatch.setattr(nemoclaw, "policy_add", lambda name, p, check=False: Result(["p"], 0, "", ""))
    prov._ARCH_CACHE.clear()
    prov._HOME_CACHE.clear()
    prov._CONFIG_DIR_CACHE.clear()
    return r


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "repos.yaml").write_text(MANIFEST, encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "pyproject.toml").write_text("[project]\nname='app'\n", encoding="utf-8")
    return tmp_path


# --- keystone ---------------------------------------------------------------


def test_keystone_url_matches_the_real_release_asset(repo: Path, rec: Recorder) -> None:
    m = load_manifest(repo)
    report = prov.ProvisionReport("sbx")
    prov.install_keystone("sbx", m.source("keystone"), Settings(), report)

    assert (
        "https://github.com/tacoda/keystone/releases/download/v4.0.0/"
        "keystone_4.0.0_linux_x86_64.tar.gz" in rec.all_scripts
    )


def test_keystone_asset_follows_sandbox_arch(repo: Path, rec: Recorder, monkeypatch) -> None:
    monkeypatch.setattr(
        nemoclaw, "sh_in",
        lambda n, s, **kw: Result(["sh"], 0, "aarch64" if s.strip() == "uname -m" else "", ""),
    )
    prov._ARCH_CACHE.clear()
    assert prov._sandbox_arch("sbx") == "arm64"


def test_keystone_version_override_is_honored(repo: Path, rec: Recorder) -> None:
    m = load_manifest(repo)
    report = prov.ProvisionReport("sbx")
    prov.install_keystone("sbx", m.source("keystone"), Settings(keystone_version="3.0.0"), report)
    assert "v3.0.0/keystone_3.0.0_linux_x86_64.tar.gz" in rec.all_scripts


# --- metaswarm --------------------------------------------------------------


def test_metaswarm_installs_only_dirs_with_skill_md(repo: Path, rec: Recorder) -> None:
    clone = repo / "repos" / "metaswarm"
    for name in ("alpha", "beta"):
        (clone / "skills" / name).mkdir(parents=True)
        (clone / "skills" / name / "SKILL.md").write_text("x", encoding="utf-8")
    (clone / "skills" / "not-a-skill").mkdir(parents=True)  # no SKILL.md

    m = load_manifest(repo)
    report = prov.ProvisionReport("sbx")
    prov.install_skills(m, "sbx", m.source("metaswarm"), report)

    assert sorted(p.name for p in rec.skills) == ["alpha", "beta"]
    assert report.results[-1].ok


def test_metaswarm_copies_into_the_agent_config_dir_not_dot_claude(
    repo: Path, rec: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """langchain-deepagents-code reads /sandbox/.deepagents. Copying to
    ~/.claude/ would silently put agents where nothing looks for them."""
    clone = repo / "repos" / "metaswarm"
    (clone / "skills" / "s").mkdir(parents=True)
    (clone / "skills" / "s" / "SKILL.md").write_text("x", encoding="utf-8")
    (clone / "agents").mkdir()
    (clone / "agents" / "architect.md").write_text("a", encoding="utf-8")

    monkeypatch.setattr(
        nemoclaw, "sh_in",
        lambda n, s, **kw: Result(["sh"], 0, "/sandbox/.deepagents" if "for d in" in s else "", ""),
    )
    prov._CONFIG_DIR_CACHE.clear()

    m = load_manifest(repo)
    prov.install_skills(m, "sbx", m.source("metaswarm"), prov.ProvisionReport("sbx"))

    dests = [dest for _, dest in rec.uploads]
    assert "/sandbox/.deepagents/agents" in dests
    assert not any(".claude" in d for d in dests)


def test_agent_config_dir_falls_back_to_recorded_agent(
    repo: Path, rec: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no candidate dir exists yet (fresh sandbox), fall back to the agent
    name in sandboxes.json rather than guessing deepagents for openclaw."""
    monkeypatch.setattr(nemoclaw, "sh_in", lambda n, s, **kw: Result(["sh"], 0, "", ""))
    monkeypatch.setattr(
        nemoclaw, "get_sandbox",
        lambda n: nemoclaw.SandboxInfo.from_raw({"name": n, "agent": "openclaw"}),
    )
    prov._CONFIG_DIR_CACHE.clear()
    assert prov.agent_config_dir("sbx") == "/sandbox/.openclaw"


def test_agent_config_dir_prefers_what_exists(
    repo: Path, rec: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        nemoclaw, "sh_in",
        lambda n, s, **kw: Result(["sh"], 0, "/sandbox/.openclaw" if "for d in" in s else "", ""),
    )
    prov._CONFIG_DIR_CACHE.clear()
    assert prov.agent_config_dir("sbx") == "/sandbox/.openclaw"


def test_metaswarm_reports_failure_when_globs_match_nothing(repo: Path, rec: Recorder) -> None:
    (repo / "repos" / "metaswarm").mkdir(parents=True)
    m = load_manifest(repo)
    report = prov.ProvisionReport("sbx")
    prov.install_skills(m, "sbx", m.source("metaswarm"), report)

    assert not report.results[-1].ok
    assert "no SKILL.md" in report.results[-1].detail


def test_uncloned_source_is_reported_not_crashed(repo: Path, rec: Recorder) -> None:
    m = load_manifest(repo)
    report = prov.ProvisionReport("sbx")
    prov.install_skills(m, "sbx", m.source("metaswarm"), report)
    assert not report.results[-1].ok
    assert "not cloned" in report.results[-1].detail


# --- graphify ---------------------------------------------------------------


def _seed_graphify_clone(repo: Path) -> Path:
    """Mirror the real repo layout: skill-<variant>.md + skills/<variant>/references/."""
    clone = repo / "repos" / "graphify" / "graphify"
    clone.mkdir(parents=True)
    (clone / "skill-agents.md").write_text(
        "---\nname: graphify\ndescription: d\n---\n# /graphify\n", encoding="utf-8"
    )
    (clone / "skill-claw.md").write_text("---\nname: graphify\n---\nclaw\n", encoding="utf-8")
    refs = clone / "skills" / "agents" / "references"
    refs.mkdir(parents=True)
    (refs / "query.md").write_text("q", encoding="utf-8")
    return repo / "repos" / "graphify"


def test_graphify_installs_pypi_package_named_graphifyy(repo: Path, rec: Recorder) -> None:
    """The PyPI name is `graphifyy`, not `graphify` — the console script is
    what's called `graphify`."""
    _seed_graphify_clone(repo)
    m = load_manifest(repo)
    report = prov.ProvisionReport("sbx")
    prov.install_python_package(m, "sbx", m.source("graphify"), report)

    assert "graphifyy==0.9.32" in rec.all_scripts


def test_graphify_skill_goes_through_nemoclaw_not_its_own_installer(
    repo: Path, rec: Recorder
) -> None:
    """graphify's own `install <platform>` writes to .openclaw/ or ~/.claude/,
    neither of which the deepagents runtime reads. nemoclaw must place it."""
    _seed_graphify_clone(repo)
    m = load_manifest(repo)
    report = prov.ProvisionReport("sbx")
    prov.install_python_package(m, "sbx", m.source("graphify"), report)

    assert "graphify install" not in rec.all_scripts
    assert len(rec.skills) == 1
    assert rec.skills[0].name == "graphify"
    assert report.results[-1].ok


def test_graphify_skill_dir_is_assembled_into_standard_shape(repo: Path, tmp_path: Path) -> None:
    clone = _seed_graphify_clone(repo)
    out = prov.assemble_skill_dir(clone, "agents", tmp_path)

    assert out is not None
    assert (out / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: graphify")
    assert (out / "references" / "query.md").is_file()


def test_graphify_skill_variant_is_honored(repo: Path, tmp_path: Path) -> None:
    clone = _seed_graphify_clone(repo)
    out = prov.assemble_skill_dir(clone, "claw", tmp_path)
    assert "claw" in (out / "SKILL.md").read_text(encoding="utf-8")


def test_graphify_skill_falls_back_to_generic_skill_md(repo: Path, tmp_path: Path) -> None:
    clone = _seed_graphify_clone(repo)
    (clone / "graphify" / "skill.md").write_text("---\nname: graphify\n---\nfallback\n", encoding="utf-8")
    out = prov.assemble_skill_dir(clone, "nonexistent-platform", tmp_path)
    assert out is not None
    assert "fallback" in (out / "SKILL.md").read_text(encoding="utf-8")


def test_graph_workspace_indexes_each_package(repo: Path, rec: Recorder) -> None:
    for pkg in ("one", "two"):
        (repo / "app" / "packages" / pkg).mkdir(parents=True)
        (repo / "app" / "packages" / pkg / "package.json").write_text("{}", encoding="utf-8")
    (repo / "app" / "package.json").write_text(
        '{"workspaces": ["packages/*"]}', encoding="utf-8"
    )

    m = load_manifest(repo)
    settings = Settings(sandbox_workspace="/w")
    plan = prov.plan_workspaces(m, settings, None)[0]
    report = prov.ProvisionReport("sbx")
    prov.graph_workspace("sbx", plan, report, source=m.source("graphify"))

    assert plan.is_monorepo
    # `graphify build` is not a real subcommand; extract is what creates a graph.
    assert "graphify build" not in rec.all_scripts
    assert rec.all_scripts.count("graphify extract . --code-only") == 2
    assert "cd /w/app/packages/one" in rec.all_scripts
    assert "cd /w/app/packages/two" in rec.all_scripts


def test_graph_build_is_key_free_by_default(repo: Path, rec: Recorder) -> None:
    """Provisioning runs unattended, so the default build must not need an LLM
    key. --code-only is graphify's guaranteed-local path."""
    m = load_manifest(repo)
    plan = prov.plan_workspaces(m, Settings(sandbox_workspace="/w"), None)[0]
    prov.graph_workspace("sbx", plan, prov.ProvisionReport("sbx"), source=m.source("graphify"))
    assert "--code-only" in rec.all_scripts
    assert "--backend" not in rec.all_scripts


def test_graph_build_args_are_configurable(repo: Path, rec: Recorder) -> None:
    m = load_manifest(repo)
    src = m.source("graphify")
    src.install["build_args"] = ["--backend", "ollama", "--no-cluster"]
    plan = prov.plan_workspaces(m, Settings(sandbox_workspace="/w"), None)[0]
    prov.graph_workspace("sbx", plan, prov.ProvisionReport("sbx"), source=src)
    assert "graphify extract . --backend ollama --no-cluster" in rec.all_scripts


def test_existing_graph_is_updated_not_rebuilt(repo: Path, rec: Recorder) -> None:
    m = load_manifest(repo)
    plan = prov.plan_workspaces(m, Settings(sandbox_workspace="/w"), None)[0]
    prov.graph_workspace("sbx", plan, prov.ProvisionReport("sbx"), source=m.source("graphify"))
    assert "if [ -f graphify-out/graph.json ]; then graphify update;" in rec.all_scripts


def test_graph_workspace_respects_disable_flag(repo: Path, rec: Recorder) -> None:
    m = load_manifest(repo)
    plan = prov.plan_workspaces(m, Settings(), None)[0]
    plan.workspace.graph = False
    report = prov.ProvisionReport("sbx")
    prov.graph_workspace("sbx", plan, report, source=m.source("graphify"))

    assert report.results[-1].skipped
    assert rec.scripts == []


# --- charter / monorepo -----------------------------------------------------


def test_charter_runs_once_per_package(repo: Path, rec: Recorder) -> None:
    for pkg in ("a", "b", "c"):
        (repo / "app" / "libs" / pkg).mkdir(parents=True)
        (repo / "app" / "libs" / pkg / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (repo / "app" / "pyproject.toml").write_text(
        '[tool.uv.workspace]\nmembers = ["libs/*"]\n', encoding="utf-8"
    )

    m = load_manifest(repo)
    plan = prov.plan_workspaces(m, Settings(sandbox_workspace="/w"), None)[0]
    report = prov.ProvisionReport("sbx")
    prov.charter_workspace("sbx", plan, report)

    assert rec.all_scripts.count("keystone init") == 3
    assert "3/3 charters" in report.results[-1].detail


def test_single_package_workspace_charters_the_root(repo: Path, rec: Recorder) -> None:
    m = load_manifest(repo)
    plan = prov.plan_workspaces(m, Settings(sandbox_workspace="/w"), None)[0]
    report = prov.ProvisionReport("sbx")
    prov.charter_workspace("sbx", plan, report)

    assert not plan.is_monorepo
    assert rec.all_scripts.count("keystone init") == 1
    assert "cd /w/app" in rec.all_scripts


# --- orchestration ----------------------------------------------------------


def test_unknown_step_is_rejected(repo: Path, rec: Recorder) -> None:
    m = load_manifest(repo)
    with pytest.raises(ValueError, match="unknown step"):
        prov.provision(m, Settings(provider="build"), sandbox="sbx", only=["bogus"])


def test_only_limits_the_steps_that_run(repo: Path, rec: Recorder) -> None:
    m = load_manifest(repo)
    report = prov.provision(m, Settings(provider="build"), sandbox="sbx", only=["keystone"])
    assert [r.step for r in report.results] == ["keystone"]


def test_triangle_note_lists_every_repo(repo: Path) -> None:
    m = load_manifest(repo)
    note = prov.sandbox_triangle_note(m)
    assert "tacoda/keystone" in note
    assert "testowner/graphify" in note
    assert "upstream-is-read-only" in note
