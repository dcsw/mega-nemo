"""Manifest and settings loading.

Precedence, lowest to highest:

    repos.yaml defaults  <  mega.toml  <  mega.local.toml  <  MEGA_* env  <  CLI flags

CLI flags are applied by the caller (Typer options default to ``None`` so an
unset flag does not clobber a configured value).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MANIFEST_NAME = "repos.yaml"
SETTINGS_NAMES = ("mega.toml", "mega.local.toml")


class ConfigError(RuntimeError):
    pass


def find_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` looking for ``repos.yaml``."""
    if env := os.environ.get("MEGA_ROOT"):
        root = Path(env).expanduser().resolve()
        if not (root / MANIFEST_NAME).is_file():
            raise ConfigError(f"MEGA_ROOT={root} has no {MANIFEST_NAME}")
        return root

    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / MANIFEST_NAME).is_file():
            return candidate
    raise ConfigError(
        f"no {MANIFEST_NAME} found in {here} or any parent. "
        "Run from inside the mega-nemo repo, or set MEGA_ROOT."
    )


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


@dataclass
class Monorepo:
    enabled: bool = False
    packages: list[str] = field(default_factory=list)
    #: Files that mark a directory as a package even without a glob match.
    markers: list[str] = field(
        default_factory=lambda: [
            "package.json",
            "pyproject.toml",
            "go.mod",
            "Cargo.toml",
            "build.gradle",
            "pom.xml",
            "Gemfile",
        ]
    )


@dataclass
class Source:
    name: str
    upstream: str
    fork: str
    default_branch: str = "main"
    triangle: bool = True
    role: str = ""
    language: str = ""
    #: True when the repo is only cloned for reading/contributing and is never
    #: installed into the sandbox.
    host_only: bool = False
    install: dict[str, Any] = field(default_factory=dict)
    mcp: dict[str, Any] = field(default_factory=dict)
    monorepo: Monorepo = field(default_factory=Monorepo)

    def path(self, clone_root: Path) -> Path:
        return clone_root / self.name

    @property
    def install_kind(self) -> str:
        return str(self.install.get("kind", "none"))


@dataclass
class Workspace:
    name: str
    path: str
    monorepo: Monorepo = field(default_factory=Monorepo)
    charter: bool = True
    graph: bool = True

    def resolve(self, root: Path) -> Path:
        p = Path(self.path).expanduser()
        return p if p.is_absolute() else (root / p).resolve()


@dataclass
class Manifest:
    root: Path
    fork_owner: str
    clone_root: Path
    upstream_remote: str
    fork_remote: str
    pull_strategy: str
    require_clean: bool
    sources: list[Source]
    workspaces: list[Workspace]

    def source(self, name: str) -> Source:
        for s in self.sources:
            if s.name == name:
                return s
        known = ", ".join(s.name for s in self.sources)
        raise ConfigError(f"unknown source {name!r}. Manifest defines: {known}")

    def select(self, names: list[str] | None) -> list[Source]:
        if not names:
            return list(self.sources)
        return [self.source(n) for n in names]

    @property
    def triangle_sources(self) -> list[Source]:
        return [s for s in self.sources if s.triangle]

    def workspace(self, name: str) -> Workspace:
        for w in self.workspaces:
            if w.name == name:
                return w
        known = ", ".join(w.name for w in self.workspaces) or "(none)"
        raise ConfigError(f"unknown workspace {name!r}. Manifest defines: {known}")


def _monorepo(raw: dict[str, Any] | None) -> Monorepo:
    raw = raw or {}
    mono = Monorepo(
        enabled=bool(raw.get("enabled", False)),
        packages=list(raw.get("packages", []) or []),
    )
    if markers := raw.get("markers"):
        mono.markers = list(markers)
    return mono


def load_manifest(root: Path | None = None, *, fork_owner: str | None = None) -> Manifest:
    root = root or find_root()
    raw = yaml.safe_load((root / MANIFEST_NAME).read_text(encoding="utf-8")) or {}

    if raw.get("version") != 1:
        raise ConfigError(f"{MANIFEST_NAME}: unsupported version {raw.get('version')!r} (expected 1)")

    defaults = raw.get("defaults") or {}
    owner = (
        fork_owner
        or os.environ.get("MEGA_FORK_OWNER")
        or defaults.get("fork_owner")
    )
    if not owner:
        raise ConfigError(
            "no fork owner configured. Set defaults.fork_owner in repos.yaml, "
            "MEGA_FORK_OWNER, or pass --fork-owner."
        )

    remotes = defaults.get("remotes") or {}
    clone_root_raw = Path(defaults.get("root", "repos")).expanduser()
    clone_root = clone_root_raw if clone_root_raw.is_absolute() else root / clone_root_raw

    sources: list[Source] = []
    for entry in raw.get("sources") or []:
        if "name" not in entry or "upstream" not in entry:
            raise ConfigError(f"{MANIFEST_NAME}: every source needs name + upstream, got {entry!r}")
        fork = str(entry.get("fork") or "").format(fork_owner=owner)
        if not fork and entry.get("triangle", True):
            raise ConfigError(
                f"{MANIFEST_NAME}: source {entry['name']!r} has triangle: true but no fork URL"
            )
        sources.append(
            Source(
                name=entry["name"],
                upstream=entry["upstream"],
                fork=fork,
                default_branch=entry.get("default_branch", "main"),
                triangle=bool(entry.get("triangle", True)),
                role=entry.get("role", ""),
                language=entry.get("language", ""),
                host_only=bool(entry.get("host_only", False)),
                install=entry.get("install") or {},
                mcp=entry.get("mcp") or {},
                monorepo=_monorepo(entry.get("monorepo")),
            )
        )

    if not sources:
        raise ConfigError(f"{MANIFEST_NAME}: no sources defined")

    workspaces = [
        Workspace(
            name=w["name"],
            path=w.get("path", "."),
            monorepo=_monorepo(w.get("monorepo")),
            charter=bool(w.get("charter", True)),
            graph=bool(w.get("graph", True)),
        )
        for w in (raw.get("workspaces") or [])
    ]

    return Manifest(
        root=root,
        fork_owner=owner,
        clone_root=clone_root,
        upstream_remote=remotes.get("upstream", "upstream"),
        fork_remote=remotes.get("fork", "origin"),
        pull_strategy=defaults.get("pull_strategy", "rebase"),
        require_clean=bool(defaults.get("require_clean", True)),
        sources=sources,
        workspaces=workspaces,
    )


# ---------------------------------------------------------------------------
# Settings (sandbox defaults)
# ---------------------------------------------------------------------------


@dataclass
class Settings:
    sandbox: str = "mega-nemo"
    provider: str | None = None
    model: str | None = None
    agent: str = "langchain-deepagents-code"
    endpoint_url: str | None = None
    gpu: bool | None = None
    policy_tier: str | None = None
    dcode_auto_approval: str = "thread-opt-in"
    tool_disclosure: str = "progressive"
    observability: bool = False
    policies: list[str] = field(default_factory=list)
    #: Path inside the sandbox where workspaces are uploaded.
    sandbox_workspace: str = "/home/agent/workspace"
    keystone_version: str | None = None


_ENV_MAP = {
    "MEGA_SANDBOX": "sandbox",
    "MEGA_PROVIDER": "provider",
    "MEGA_MODEL": "model",
    "MEGA_AGENT": "agent",
    "MEGA_ENDPOINT_URL": "endpoint_url",
    "MEGA_POLICY_TIER": "policy_tier",
    "MEGA_SANDBOX_WORKSPACE": "sandbox_workspace",
    "MEGA_KEYSTONE_VERSION": "keystone_version",
    # NemoClaw's own env vars are honored as a fallback so a shell already set
    # up for `nemoclaw onboard` works with `mega` unchanged.
    "NEMOCLAW_PROVIDER": "provider",
    "NEMOCLAW_MODEL": "model",
    "NEMOCLAW_POLICY_TIER": "policy_tier",
}


def load_settings(root: Path) -> Settings:
    settings = Settings()
    for name in SETTINGS_NAMES:
        path = root / name
        if not path.is_file():
            continue
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        section = data.get("sandbox", data)
        for key, value in section.items():
            if hasattr(settings, key):
                setattr(settings, key, value)
            else:
                raise ConfigError(f"{name}: unknown setting [sandbox].{key}")

    for env_name, attr in _ENV_MAP.items():
        value = os.environ.get(env_name)
        # A NEMOCLAW_* fallback must not override an explicit MEGA_* value.
        if value and not (env_name.startswith("NEMOCLAW_") and getattr(settings, attr)):
            setattr(settings, attr, value)

    if gpu := os.environ.get("MEGA_GPU"):
        settings.gpu = gpu.lower() not in {"0", "false", "no", "off"}

    return settings


def apply_overrides(settings: Settings, **overrides: Any) -> Settings:
    """Apply CLI flags, ignoring any that are ``None``."""
    for key, value in overrides.items():
        if value is not None:
            setattr(settings, key, value)
    return settings
