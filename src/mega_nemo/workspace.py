"""Monorepo support.

"Monorepo support if and as needed" means: a workspace is a *set of packages*,
and the per-package tools (graphify's knowledge graph, keystone's charter
coverage) run once per package rather than once per repo. A single-package
project is just the degenerate case with one package at the root, so every
caller can treat workspaces uniformly.

Package discovery, in order:
  1. explicit `packages:` globs in the manifest
  2. native workspace declarations already in the repo (pnpm/npm/yarn, cargo,
     go.work, uv/poetry, turbo, nx, lerna)
  3. marker files (package.json, pyproject.toml, go.mod, ...) one level deep
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import Manifest, Monorepo, Workspace

#: Files whose presence means "this repo already declares its own workspaces".
NATIVE_MANIFESTS = (
    "pnpm-workspace.yaml",
    "package.json",
    "Cargo.toml",
    "go.work",
    "pyproject.toml",
    "lerna.json",
    "turbo.json",
    "nx.json",
)


@dataclass
class Package:
    name: str
    path: Path
    #: Path relative to the workspace root; "." for a root package.
    rel: str
    language: str = ""
    source: str = "marker"  # how we found it

    @property
    def is_root(self) -> bool:
        return self.rel in {".", ""}


def _lang_for(path: Path) -> str:
    for marker, lang in (
        ("package.json", "javascript"),
        ("pyproject.toml", "python"),
        ("go.mod", "go"),
        ("Cargo.toml", "rust"),
        ("pom.xml", "java"),
        ("build.gradle", "java"),
        ("Gemfile", "ruby"),
    ):
        if (path / marker).is_file():
            return lang
    return ""


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def native_globs(root: Path) -> list[str]:
    """Read workspace globs the repo already declares for itself."""
    globs: list[str] = []

    if (p := root / "pnpm-workspace.yaml").is_file():
        globs += list(_read_yaml(p).get("packages") or [])

    if (p := root / "package.json").is_file():
        ws = _read_json(p).get("workspaces")
        if isinstance(ws, dict):
            globs += list(ws.get("packages") or [])
        elif isinstance(ws, list):
            globs += ws

    if (p := root / "Cargo.toml").is_file():
        globs += list((_read_toml(p).get("workspace") or {}).get("members") or [])

    if (p := root / "pyproject.toml").is_file():
        data = _read_toml(p)
        uv_members = ((data.get("tool") or {}).get("uv") or {}).get("workspace") or {}
        globs += list(uv_members.get("members") or [])

    if (p := root / "lerna.json").is_file():
        globs += list(_read_json(p).get("packages") or [])

    if (p := root / "go.work").is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("./") or (line and not line.startswith(("go ", "use", ")", "("))):
                candidate = line.strip("() \t")
                if candidate.startswith("./"):
                    globs.append(candidate[2:])

    return [g for g in dict.fromkeys(globs) if g]


def is_monorepo(root: Path, mono: Monorepo | None = None) -> bool:
    if mono and mono.enabled:
        return True
    return bool(native_globs(root))


def discover(root: Path, mono: Monorepo | None = None) -> list[Package]:
    """Enumerate packages under ``root``."""
    root = root.resolve()
    if not root.is_dir():
        return []

    mono = mono or Monorepo()
    found: dict[Path, Package] = {}

    def add(path: Path, source: str) -> None:
        path = path.resolve()
        if not path.is_dir() or path in found:
            return
        if any(part in {"node_modules", ".git", ".venv", "dist", "build"} for part in path.parts):
            return
        rel = "." if path == root else path.relative_to(root).as_posix()
        found[path] = Package(
            name=path.name if path != root else root.name,
            path=path,
            rel=rel,
            language=_lang_for(path),
            source=source,
        )

    globs = list(mono.packages)
    source = "manifest"
    if not globs:
        globs = native_globs(root)
        source = "native"

    for pattern in globs:
        for match in sorted(root.glob(pattern)):
            if match.is_dir() and any((match / m).is_file() for m in mono.markers):
                add(match, source)

    if not found:
        # Fall back to marker files one level deep, then the root itself.
        for child in sorted(p for p in root.iterdir() if p.is_dir()):
            if child.name.startswith("."):
                continue
            if any((child / m).is_file() for m in mono.markers):
                add(child, "marker")

    if not found or not (mono.enabled or globs):
        add(root, "root")

    return sorted(found.values(), key=lambda p: (p.rel != ".", p.rel))


def packages_for(m: Manifest, w: Workspace) -> list[Package]:
    return discover(w.resolve(m.root), w.monorepo)


def resolve_workspaces(m: Manifest, names: list[str] | None = None) -> list[Workspace]:
    if not names:
        return list(m.workspaces)
    return [m.workspace(n) for n in names]
