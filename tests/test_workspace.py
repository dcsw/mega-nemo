from __future__ import annotations

import json
from pathlib import Path

from mega_nemo.config import Monorepo
from mega_nemo.workspace import discover, is_monorepo, native_globs


def make(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def test_single_package_repo_yields_root(tmp_path: Path) -> None:
    make(tmp_path, {"pyproject.toml": "[project]\nname='x'\n"})
    packages = discover(tmp_path)
    assert len(packages) == 1
    assert packages[0].is_root
    assert packages[0].language == "python"


def test_manifest_globs_win(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "packages/a/package.json": "{}",
            "packages/b/package.json": "{}",
            "other/c/package.json": "{}",
        },
    )
    packages = discover(tmp_path, Monorepo(enabled=True, packages=["packages/*"]))
    assert [p.rel for p in packages] == ["packages/a", "packages/b"]
    assert all(p.source == "manifest" for p in packages)


def test_pnpm_workspace_detected(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "pnpm-workspace.yaml": "packages:\n  - 'apps/*'\n",
            "apps/web/package.json": "{}",
            "apps/api/package.json": "{}",
        },
    )
    assert native_globs(tmp_path) == ["apps/*"]
    assert is_monorepo(tmp_path)
    assert sorted(p.rel for p in discover(tmp_path)) == ["apps/api", "apps/web"]


def test_npm_workspaces_array(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "package.json": json.dumps({"name": "root", "workspaces": ["libs/*"]}),
            "libs/one/package.json": "{}",
        },
    )
    assert [p.rel for p in discover(tmp_path)] == ["libs/one"]


def test_cargo_workspace_members(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n',
            "crates/core/Cargo.toml": '[package]\nname="core"\n',
        },
    )
    packages = discover(tmp_path)
    assert [p.rel for p in packages] == ["crates/core"]
    assert packages[0].language == "rust"


def test_uv_workspace_members(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "pyproject.toml": '[tool.uv.workspace]\nmembers = ["libs/*"]\n',
            "libs/pkg/pyproject.toml": "[project]\nname='pkg'\n",
        },
    )
    assert [p.rel for p in discover(tmp_path)] == ["libs/pkg"]


def test_node_modules_is_never_a_package(tmp_path: Path) -> None:
    make(
        tmp_path,
        {
            "package.json": json.dumps({"workspaces": ["*"]}),
            "node_modules/dep/package.json": "{}",
            "app/package.json": "{}",
        },
    )
    rels = [p.rel for p in discover(tmp_path)]
    assert "app" in rels
    assert not any("node_modules" in r for r in rels)


def test_marker_fallback_one_level_deep(tmp_path: Path) -> None:
    make(tmp_path, {"svc-a/go.mod": "module a\n", "svc-b/go.mod": "module b\n"})
    packages = discover(tmp_path, Monorepo(enabled=True))
    assert sorted(p.rel for p in packages) == ["svc-a", "svc-b"]
    assert all(p.language == "go" for p in packages)


def test_empty_dir_is_still_one_root_package(tmp_path: Path) -> None:
    packages = discover(tmp_path)
    assert len(packages) == 1 and packages[0].is_root


def test_missing_dir_yields_nothing(tmp_path: Path) -> None:
    assert discover(tmp_path / "nope") == []
