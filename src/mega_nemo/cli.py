"""`mega` — the mega-nemo CLI."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import gitx, nemoclaw, providers, shell
from . import provision as prov
from . import workspace as ws
from .config import (
    ConfigError,
    Manifest,
    Settings,
    apply_overrides,
    find_root,
    load_manifest,
    load_settings,
)
from .shell import CommandError

out = Console()
err = Console(stderr=True)

app = typer.Typer(
    name="mega",
    help="Run keystone + metaswarm + graphify inside a NemoClaw deepagents (dcode) sandbox, "
    "with triangular git workflows back to every source repo.",
    no_args_is_help=True,
    add_completion=True,
)

sandbox_app = typer.Typer(name="sandbox", help="Build and operate the dcode sandbox.", no_args_is_help=True)
repos_app = typer.Typer(name="repos", help="Clone and wire the source repos.", no_args_is_help=True)
tri_app = typer.Typer(name="triangle", help="upstream -> fork -> local push/pull workflows.", no_args_is_help=True)
wsp_app = typer.Typer(name="workspace", help="Workspaces and monorepo packages.", no_args_is_help=True)

app.add_typer(sandbox_app)
app.add_typer(repos_app)
app.add_typer(tri_app)
app.add_typer(wsp_app)


class Ctx:
    """Loaded lazily so `--help` never touches the filesystem."""

    def __init__(self) -> None:
        self.fork_owner: str | None = None
        self._root: Path | None = None
        self._manifest: Manifest | None = None
        self._settings: Settings | None = None

    @property
    def root(self) -> Path:
        if self._root is None:
            self._root = find_root()
        return self._root

    @property
    def m(self) -> Manifest:
        if self._manifest is None:
            self._manifest = load_manifest(self.root, fork_owner=self.fork_owner)
        return self._manifest

    @property
    def s(self) -> Settings:
        if self._settings is None:
            self._settings = load_settings(self.root)
        return self._settings


ctx = Ctx()

# --- shared option types ----------------------------------------------------

ProviderOpt = Annotated[
    str | None,
    typer.Option("--provider", "-p", help="Inference provider (build, openai, anthropic, gemini, "
                "openrouter, ollama, vllm, custom, ... or an OpenShell name like nvidia-prod)."),
]
ModelOpt = Annotated[
    str | None,
    typer.Option("--model", "-m", help="Model id, e.g. nvidia/nemotron-3-super-120b-a12b."),
]
AgentOpt = Annotated[
    str | None,
    typer.Option("--agent", "-a", help="Agent runtime. dcode/deepagents -> langchain-deepagents-code."),
]
NameOpt = Annotated[str | None, typer.Option("--name", "-n", help="Sandbox name.")]
EndpointOpt = Annotated[str | None, typer.Option("--endpoint-url", help="Required for custom/compatible providers.")]
GpuOpt = Annotated[bool | None, typer.Option("--gpu/--no-gpu", help="Force GPU passthrough on or off.")]
TierOpt = Annotated[str | None, typer.Option("--policy-tier", help="NemoClaw policy tier, e.g. restricted.")]
ApprovalOpt = Annotated[
    str | None,
    typer.Option("--dcode-auto-approval", help="dcode thread approval: disabled | thread-opt-in."),
]
OnlyOpt = Annotated[
    list[str] | None,
    typer.Option("--only", help=f"Limit to steps: {', '.join(prov.STEPS)}. Repeatable."),
]
WorkspacesOpt = Annotated[
    list[str] | None,
    typer.Option("--workspace", "-w", help="Limit to these workspaces. Repeatable."),
]
PolicyOpt = Annotated[
    list[str] | None,
    typer.Option("--policy", help="Extra NemoClaw policy preset. Repeatable."),
]


def _settings_for(
    provider=None, model=None, agent=None, name=None, endpoint_url=None, gpu=None, policy_tier=None
) -> Settings:
    return apply_overrides(
        ctx.s,
        provider=provider,
        model=model,
        agent=agent,
        sandbox=name,
        endpoint_url=endpoint_url,
        gpu=gpu,
        policy_tier=policy_tier,
    )


def _fail(msg: str, code: int = 1) -> None:
    err.print(f"[bold red]error[/bold red] {msg}")
    raise typer.Exit(code)


@app.callback()
def root_callback(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print mutating commands, don't run them.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Echo every command.")] = False,
    fork_owner: Annotated[str | None, typer.Option("--fork-owner", help="Override defaults.fork_owner.")] = None,
) -> None:
    shell.set_mode(dry_run=dry_run, verbose=verbose)
    ctx.fork_owner = fork_owner


# ===========================================================================
# repos
# ===========================================================================


@repos_app.command("list")
def repos_list() -> None:
    """Show the source manifest."""
    m = ctx.m
    t = Table(title=f"sources (forks under {m.fork_owner}/)", header_style="bold")
    for col in ("repo", "role", "upstream", "fork", "triangle", "install", "cloned"):
        t.add_column(col)
    for s in m.sources:
        cloned = (s.path(m.clone_root) / ".git").is_dir()
        t.add_row(
            s.name,
            s.role or "—",
            gitx.slug(s.upstream),
            gitx.slug(s.fork) if s.fork else "—",
            "[green]yes[/green]" if s.triangle else "no",
            s.install_kind,
            "[green]yes[/green]" if cloned else "[yellow]no[/yellow]",
        )
    out.print(t)
    out.print(f"[dim]clones: {m.clone_root}[/dim]")


@repos_app.command("sync")
def repos_sync(
    names: Annotated[list[str] | None, typer.Argument(help="Repos to sync. Default: all.")] = None,
    depth: Annotated[int | None, typer.Option("--depth", help="Shallow clone depth for first clone.")] = None,
    fork: Annotated[bool, typer.Option("--fork/--no-fork", help="Create missing GitHub forks with `gh`.")] = False,
    fetch: Annotated[bool, typer.Option("--fetch/--no-fetch", help="Fetch after wiring.")] = True,
) -> None:
    """Clone missing repos and wire every clone for the triangle. Idempotent."""
    m = ctx.m
    rows = []
    for s in m.select(names):
        try:
            if fork and s.triangle:
                if not gitx.ensure_fork(m, s):
                    err.print(f"[yellow]![/yellow] {s.name}: could not create fork {gitx.slug(s.fork)}")
            path = gitx.ensure_clone(m, s, depth=depth)
            st = gitx.wire_triangle(m, s, path)
            if fetch:
                gitx.fetch(m, s, path)
            rows.append((s.name, "[green]ok[/green]", st.branch or "—", st.note or ""))
        except CommandError as exc:
            rows.append((s.name, "[red]fail[/red]", "—", str(exc).splitlines()[0][:80]))

    t = Table(title="repos sync", header_style="bold")
    for col in ("repo", "status", "branch", "note"):
        t.add_column(col)
    for row in rows:
        t.add_row(*row)
    out.print(t)


@repos_app.command("fork")
def repos_fork(
    names: Annotated[list[str] | None, typer.Argument()] = None,
) -> None:
    """Create the GitHub forks for every triangle repo (requires `gh auth login`)."""
    shell.require("gh", "Install: https://cli.github.com")
    m = ctx.m
    for s in m.select(names):
        if not s.triangle:
            out.print(f"[dim]{s.name}: triangle: false, skipped[/dim]")
            continue
        ok = gitx.ensure_fork(m, s)
        mark = "[green]ok[/green]" if ok else "[red]fail[/red]"
        out.print(f"{mark} {s.name} -> {gitx.slug(s.fork)}")


# ===========================================================================
# triangle
# ===========================================================================


def _status_table(m: Manifest, sources, *, do_fetch: bool) -> Table:
    t = Table(title="triangle status", header_style="bold")
    for col in ("repo", "branch", "clean", "behind upstream", "ahead of fork", "wired", "note"):
        t.add_column(col)
    for s in sources:
        st = gitx.status(m, s, do_fetch=do_fetch)
        if not st.exists:
            t.add_row(s.name, "—", "—", "—", "—", "[yellow]no[/yellow]", "not cloned")
            continue
        behind = f"[yellow]{st.behind_upstream}[/yellow]" if st.behind_upstream else "0"
        ahead = f"[cyan]{st.ahead_of_fork}[/cyan]" if st.ahead_of_fork else "0"
        clean = "[green]yes[/green]" if st.clean else f"[red]{st.dirty_files} dirty[/red]"
        wired = "[green]yes[/green]" if st.wired and st.push_blocked else "[yellow]partial[/yellow]"
        t.add_row(s.name, st.branch or "—", clean, behind, ahead, wired, st.note)
    return t


@tri_app.command("status")
def tri_status(
    names: Annotated[list[str] | None, typer.Argument()] = None,
    fetch: Annotated[bool, typer.Option("--fetch/--no-fetch", help="Fetch upstream first for accurate counts.")] = True,
) -> None:
    """How far each clone has drifted from upstream and from your fork."""
    m = ctx.m
    out.print(_status_table(m, m.select(names), do_fetch=fetch))


@tri_app.command("pull")
def tri_pull(
    names: Annotated[list[str] | None, typer.Argument(help="Repos to pull. Default: all.")] = None,
    strategy: Annotated[str | None, typer.Option("--strategy", "-s", help="rebase | merge | ff-only | reset")] = None,
    force: Annotated[bool, typer.Option("--force", help="Auto-stash local changes instead of refusing.")] = False,
) -> None:
    """Pull each repo from **upstream** (never from the fork)."""
    m = ctx.m
    failed = False
    for s in m.select(names):
        try:
            st = gitx.pull(m, s, strategy=strategy, force=force)
            out.print(f"[green]ok[/green] {s.name} @ {st.branch} "
                      f"[dim](behind {st.behind_upstream}, ahead of fork {st.ahead_of_fork})[/dim]")
        except (gitx.DirtyRepo, CommandError, ValueError) as exc:
            failed = True
            err.print(f"[red]fail[/red] {s.name}: {str(exc).splitlines()[0]}")
    if failed:
        raise typer.Exit(1)


@tri_app.command("push")
def tri_push(
    names: Annotated[list[str] | None, typer.Argument(help="Repos to push. Default: all triangle repos.")] = None,
    branch: Annotated[str | None, typer.Option("--branch", "-b", help="Branch to push. Default: current.")] = None,
    force_with_lease: Annotated[bool, typer.Option("--force-with-lease", help="Safe force-push post-rebase.")] = False,
) -> None:
    """Push each repo to **your fork** (upstream pushes are blocked by config)."""
    m = ctx.m
    targets = [s for s in m.select(names) if s.triangle]
    if not targets:
        _fail("no triangle-enabled repos selected")
    failed = False
    for s in targets:
        try:
            st = gitx.push(m, s, branch=branch, force_with_lease=force_with_lease)
            out.print(f"[green]ok[/green] {s.name} -> {gitx.slug(s.fork)} @ {st.branch}")
        except (CommandError, ValueError) as exc:
            failed = True
            err.print(f"[red]fail[/red] {s.name}: {str(exc).splitlines()[0]}")
    if failed:
        raise typer.Exit(1)


@tri_app.command("start")
def tri_start(
    branch: Annotated[str, typer.Argument(help="Topic branch name.")],
    names: Annotated[list[str] | None, typer.Argument(help="Repos. Default: all triangle repos.")] = None,
) -> None:
    """Start a topic branch off freshly-fetched upstream — the correct triangle entry point."""
    m = ctx.m
    for s in m.select(names):
        if not s.triangle:
            continue
        try:
            path = gitx.start_branch(m, s, branch)
            base = f"{m.upstream_remote}/{s.default_branch}"
            out.print(f"[green]ok[/green] {s.name}: {branch} off {base} [dim]({path})[/dim]")
        except (CommandError, ValueError) as exc:
            err.print(f"[red]fail[/red] {s.name}: {str(exc).splitlines()[0]}")


@tri_app.command("pr")
def tri_pr(
    name: Annotated[str, typer.Argument(help="Repo to open a PR for.")],
    branch: Annotated[str | None, typer.Option("--branch", "-b")] = None,
    title: Annotated[str | None, typer.Option("--title", "-t")] = None,
    body: Annotated[str | None, typer.Option("--body")] = None,
    draft: Annotated[bool, typer.Option("--draft/--ready")] = True,
    web: Annotated[bool, typer.Option("--web", help="Open the PR form in a browser instead.")] = False,
) -> None:
    """Close the triangle: open a PR from your fork branch against upstream."""
    shell.require("gh", "Install: https://cli.github.com")
    m = ctx.m
    s = m.source(name)
    if not s.triangle:
        _fail(f"{name} is configured triangle: false")
    r = gitx.open_pr(m, s, branch=branch, title=title, body=body, draft=draft, web=web)
    if r.ok or r.dry_run:
        out.print(f"[green]ok[/green] {r.out or 'PR created'}")
    else:
        _fail(r.stderr.strip() or "gh pr create failed")


@tri_app.command("sync")
def tri_sync(
    names: Annotated[list[str] | None, typer.Argument()] = None,
    strategy: Annotated[str | None, typer.Option("--strategy", "-s")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Pull from upstream, then push the result to your fork. The full round trip."""
    m = ctx.m
    for s in m.select(names):
        try:
            gitx.pull(m, s, strategy=strategy, force=force)
            if s.triangle:
                st = gitx.push(m, s)
                out.print(f"[green]ok[/green] {s.name}: upstream -> local -> {gitx.slug(s.fork)} @ {st.branch}")
            else:
                out.print(f"[green]ok[/green] {s.name}: pulled (pull-only repo)")
        except (gitx.DirtyRepo, CommandError, ValueError) as exc:
            err.print(f"[red]fail[/red] {s.name}: {str(exc).splitlines()[0]}")


# ===========================================================================
# sandbox
# ===========================================================================


@sandbox_app.command("create")
def sandbox_create(
    provider: ProviderOpt = None,
    model: ModelOpt = None,
    name: NameOpt = None,
    agent: AgentOpt = None,
    endpoint_url: EndpointOpt = None,
    gpu: GpuOpt = None,
    policy_tier: TierOpt = None,
    recreate: Annotated[bool, typer.Option("--recreate", help="Recreate if the sandbox already exists.")] = False,
) -> None:
    """Build the sandbox. Provider and model are the two required inputs."""
    shell.require("nemoclaw", "Install: https://github.com/NVIDIA/NemoClaw")
    settings = _settings_for(provider, model, agent, name, endpoint_url, gpu, policy_tier)
    try:
        spec = nemoclaw.build_spec(settings)
    except (nemoclaw.NemoclawError, providers.UnknownProvider, ValueError) as exc:
        _fail(str(exc))

    if nemoclaw.exists(spec.name) and not recreate:
        _fail(f"sandbox {spec.name!r} already exists. Use --recreate, or `mega sandbox rebuild`.")

    if not nemoclaw.credential_present(spec):
        err.print(
            f"[yellow]![/yellow] {spec.provider.credential_env} is not set. "
            "NemoClaw will prompt for it, which fails under --non-interactive."
        )

    out.print(
        f"[bold]building[/bold] {spec.name}\n"
        f"  provider  {spec.provider.key} [dim]({spec.provider.name} — {spec.provider.label})[/dim]\n"
        f"  model     {spec.model}\n"
        f"  agent     {spec.agent}\n"
        f"  gpu       {'auto' if spec.gpu is None else spec.gpu}\n"
        f"  policies  {', '.join(spec.policies)}"
    )
    r = nemoclaw.onboard(spec, fresh=True, recreate=recreate)
    if not (r.ok or r.dry_run):
        _fail("nemoclaw onboard failed; rerun with -v, or `nemoclaw onboard --resume`")

    # onboard has no --dcode-auto-approval; a rebuild applies it.
    if providers.is_dcode(spec.agent) and spec.dcode_auto_approval != "disabled":
        out.print(f"[dim]applying dcode auto-approval: {spec.dcode_auto_approval}[/dim]")
        nemoclaw.rebuild(spec)

    out.print(f"[green]ok[/green] sandbox {spec.name} built. Next: [cyan]mega provision[/cyan]")


@sandbox_app.command("rebuild")
def sandbox_rebuild(
    name: NameOpt = None,
    provider: ProviderOpt = None,
    model: ModelOpt = None,
    agent: AgentOpt = None,
    dcode_auto_approval: ApprovalOpt = None,
) -> None:
    """Rebuild an existing sandbox at the current agent version."""
    settings = _settings_for(provider, model, agent, name)
    if dcode_auto_approval:
        settings.dcode_auto_approval = dcode_auto_approval
    try:
        spec = nemoclaw.build_spec(settings)
    except (nemoclaw.NemoclawError, ValueError) as exc:
        _fail(str(exc))
    if not nemoclaw.exists(spec.name):
        _fail(f"no sandbox named {spec.name!r}. Run `mega sandbox create` first.")
    r = nemoclaw.rebuild(spec)
    if not (r.ok or r.dry_run):
        _fail("nemoclaw rebuild failed")
    out.print(f"[green]ok[/green] rebuilt {spec.name}")


@sandbox_app.command("inference")
def sandbox_inference(
    provider: ProviderOpt = None,
    model: ModelOpt = None,
    name: NameOpt = None,
) -> None:
    """Repoint an existing sandbox at a different provider/model (no rebuild)."""
    settings = _settings_for(provider, model, None, name)
    if not settings.provider or not settings.model:
        _fail("both --provider and --model are required")
    try:
        p = providers.resolve(settings.provider)
    except providers.UnknownProvider as exc:
        _fail(str(exc))
    r = nemoclaw.set_inference(settings.sandbox, p, settings.model)
    if not (r.ok or r.dry_run):
        _fail(r.stderr.strip() or "inference set failed")
    out.print(f"[green]ok[/green] {settings.sandbox} -> {p.name} / {settings.model}")


@sandbox_app.command("list")
def sandbox_list() -> None:
    """List NemoClaw sandboxes on this host."""
    boxes = nemoclaw.list_sandboxes()
    if not boxes:
        out.print("[dim]no sandboxes[/dim]")
        return
    default = nemoclaw.default_sandbox()
    t = Table(title="sandboxes", header_style="bold")
    for col in ("name", "provider", "model", "agent", "gpu", "dcode approval", "nemoclaw"):
        t.add_column(col)
    for b in boxes:
        label = f"[bold]{b.name}[/bold] *" if b.name == default else b.name
        t.add_row(label, b.provider, b.model, b.agent, "yes" if b.gpu else "no",
                  b.dcode_auto_approval, b.nemoclaw_version)
    out.print(t)
    out.print("[dim]* = nemoclaw default sandbox[/dim]")


@sandbox_app.command("status")
def sandbox_status(name: NameOpt = None) -> None:
    """Show one sandbox's health."""
    nemoclaw.status(_settings_for(name=name).sandbox)


@sandbox_app.command("connect")
def sandbox_connect(name: NameOpt = None) -> None:
    """Shell into the sandbox."""
    nemoclaw.connect(_settings_for(name=name).sandbox)


@sandbox_app.command("destroy")
def sandbox_destroy(
    name: NameOpt = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Destroy the sandbox. Workspace state inside it is lost."""
    target = _settings_for(name=name).sandbox
    if not yes and not typer.confirm(f"Destroy sandbox {target!r} and its workspace state?"):
        raise typer.Abort()
    nemoclaw.destroy(target)


@app.command("agent")
def agent_turn(
    prompt: Annotated[str, typer.Argument(help="Prompt for one non-interactive dcode turn.")],
    name: NameOpt = None,
) -> None:
    """Run a single agent turn in the sandbox (smoke tests, CI, scripted work)."""
    nemoclaw.agent_turn(_settings_for(name=name).sandbox, prompt)


# ===========================================================================
# workspace
# ===========================================================================


@wsp_app.command("list")
def workspace_list(
    names: Annotated[list[str] | None, typer.Argument()] = None,
) -> None:
    """List workspaces and, for monorepos, the packages inside them."""
    m = ctx.m
    targets = ws.resolve_workspaces(m, names)
    if not targets:
        out.print("[dim]no workspaces defined in repos.yaml[/dim]")
        return
    for w in targets:
        root = w.resolve(m.root)
        packages = ws.packages_for(m, w)
        mono = ws.is_monorepo(root, w.monorepo)
        t = Table(
            title=f"{w.name} [dim]{root}[/dim] {'[monorepo]' if mono else '[single package]'}",
            header_style="bold",
        )
        for col in ("package", "path", "language", "found via"):
            t.add_column(col)
        for p in packages:
            t.add_row(p.name, p.rel, p.language or "—", p.source)
        out.print(t)


@wsp_app.command("add")
def workspace_add(
    name: Annotated[str, typer.Argument(help="Workspace name.")],
    path: Annotated[str, typer.Argument(help="Path to the project (absolute, or relative to the repo root).")],
    monorepo: Annotated[bool, typer.Option("--monorepo/--single", help="Force monorepo package expansion.")] = False,
    packages: Annotated[list[str] | None, typer.Option("--package", help="Package glob. Repeatable.")] = None,
) -> None:
    """Append a workspace to repos.yaml."""
    import yaml as _yaml

    m = ctx.m
    manifest_path = m.root / "repos.yaml"
    raw = _yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    entries = raw.setdefault("workspaces", []) or []
    if any(w.get("name") == name for w in entries):
        _fail(f"workspace {name!r} already exists in repos.yaml")

    entry = {
        "name": name,
        "path": path,
        "monorepo": {"enabled": bool(monorepo or packages)},
        "charter": True,
        "graph": True,
    }
    if packages:
        entry["monorepo"]["packages"] = list(packages)
    entries.append(entry)
    raw["workspaces"] = entries
    manifest_path.write_text(_yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    out.print(f"[green]ok[/green] added workspace {name} -> {path}")


# ===========================================================================
# provision / up / doctor
# ===========================================================================


@app.command("provision")
def provision_cmd(
    name: NameOpt = None,
    only: OnlyOpt = None,
    workspaces: WorkspacesOpt = None,
    policy: PolicyOpt = None,
) -> None:
    """Install keystone, metaswarm and graphify into the sandbox and sync workspaces."""
    m = ctx.m
    settings = _settings_for(name=name)
    if not nemoclaw.exists(settings.sandbox) and not shell.DRY_RUN:
        _fail(f"no sandbox named {settings.sandbox!r}. Run `mega sandbox create` first.")

    prov.bootstrap_git_identity(m, settings.sandbox)
    try:
        report = prov.provision(
            m, settings, sandbox=settings.sandbox, only=only, workspaces=workspaces,
            extra_policies=policy,
        )
    except (ValueError, nemoclaw.NemoclawError, ConfigError) as exc:
        _fail(str(exc))

    t = Table(title=f"provision {report.sandbox}", header_style="bold")
    for col in ("step", "result", "detail"):
        t.add_column(col)
    for r in report.results:
        mark = "[dim]skip[/dim]" if r.skipped else ("[green]ok[/green]" if r.ok else "[red]fail[/red]")
        t.add_row(r.step, mark, r.detail)
    out.print(t)

    note = m.root / "TRIANGLE.md"
    note.write_text(prov.sandbox_triangle_note(m), encoding="utf-8")
    out.print(f"[dim]triangle rules written to {note}[/dim]")

    if not report.ok:
        raise typer.Exit(1)


@app.command("up")
def up(
    provider: ProviderOpt = None,
    model: ModelOpt = None,
    name: NameOpt = None,
    agent: AgentOpt = None,
    endpoint_url: EndpointOpt = None,
    gpu: GpuOpt = None,
    policy_tier: TierOpt = None,
    fork: Annotated[bool, typer.Option("--fork/--no-fork", help="Create missing GitHub forks.")] = False,
    recreate: Annotated[bool, typer.Option("--recreate", help="Recreate the sandbox if it exists.")] = False,
) -> None:
    """One shot: sync repos, build the sandbox for <provider>/<model>, provision everything."""
    settings = _settings_for(provider, model, agent, name, endpoint_url, gpu, policy_tier)
    try:
        spec = nemoclaw.build_spec(settings)
    except (nemoclaw.NemoclawError, providers.UnknownProvider, ValueError) as exc:
        _fail(str(exc))

    out.print("[bold]1/3[/bold] syncing source repos")
    repos_sync(names=None, depth=None, fork=fork, fetch=True)

    out.print(f"\n[bold]2/3[/bold] sandbox {spec.name} ({spec.provider.key} / {spec.model})")
    if nemoclaw.exists(spec.name) and not recreate:
        out.print(f"[dim]{spec.name} already exists; skipping create (pass --recreate to rebuild)[/dim]")
    else:
        sandbox_create(provider, model, name, agent, endpoint_url, gpu, policy_tier, recreate)

    out.print("\n[bold]3/3[/bold] provisioning")
    provision_cmd(name=spec.name, only=None, workspaces=None, policy=None)


@app.command("doctor")
def doctor(
    name: NameOpt = None,
    fix: Annotated[bool, typer.Option("--fix", help="Also run `nemoclaw <name> doctor --fix`.")] = False,
) -> None:
    """Check the host, the manifest, the clones, and the sandbox."""
    problems = 0
    t = Table(title="mega doctor", header_style="bold")
    for col in ("check", "result", "detail"):
        t.add_column(col)

    def row(check: str, ok: bool, detail: str = "", warn: bool = False) -> None:
        nonlocal problems
        if not ok and not warn:
            problems += 1
        mark = "[green]ok[/green]" if ok else ("[yellow]warn[/yellow]" if warn else "[red]fail[/red]")
        t.add_row(check, mark, detail)

    for binary, hint in (
        ("git", "required"),
        ("nemoclaw", "required — https://github.com/NVIDIA/NemoClaw"),
        ("docker", "required by nemoclaw's openshell driver"),
        ("gh", "optional — needed for `mega triangle pr` and --fork"),
    ):
        present = shell.have(binary)
        row(f"host: {binary}", present, hint if not present else "", warn=(binary == "gh"))

    try:
        m = ctx.m
        row("manifest", True, f"{len(m.sources)} sources, {len(m.workspaces)} workspaces, fork owner {m.fork_owner}")
    except ConfigError as exc:
        row("manifest", False, str(exc))
        out.print(t)
        raise typer.Exit(1) from exc

    row("nemoclaw version", True, nemoclaw.version())

    for s in m.sources:
        st = gitx.status(m, s)
        if not st.exists:
            row(f"repo: {s.name}", False, "not cloned — run `mega repos sync`", warn=True)
        elif s.triangle and not st.push_blocked:
            row(f"repo: {s.name}", False, "upstream push is NOT blocked — run `mega repos sync`")
        elif not st.clean:
            row(f"repo: {s.name}", True, f"{st.dirty_files} uncommitted change(s)")
        else:
            row(f"repo: {s.name}", True, f"{st.branch}, behind upstream {st.behind_upstream}")

    settings = _settings_for(name=name)
    box = nemoclaw.get_sandbox(settings.sandbox)
    if not box:
        row(f"sandbox: {settings.sandbox}", False, "does not exist — run `mega sandbox create`", warn=True)
    else:
        row(f"sandbox: {settings.sandbox}", True, f"{box.provider} / {box.model} / {box.agent}")
        if box.agent not in providers.DCODE_AGENTS:
            row("sandbox agent", False, f"{box.agent} is not a dcode runtime", warn=True)
        try:
            p = providers.resolve(box.provider)
            present = bool(os.environ.get(p.credential_env))
            row(
                "credential",
                present,
                "present" if present else f"{p.credential_env} not set in this shell",
                warn=True,
            )
        except providers.UnknownProvider as exc:
            row("credential", False, str(exc), warn=True)

    out.print(t)
    if box and fix:
        nemoclaw.doctor(settings.sandbox, fix=True)
    if problems:
        err.print(f"[red]{problems} problem(s)[/red]")
        raise typer.Exit(1)
    out.print("[green]all checks passed[/green]")


@app.command("providers")
def providers_cmd() -> None:
    """List the provider vocabulary this build of mega-nemo accepts."""
    t = Table(title="providers", header_style="bold")
    for col in ("key (--provider)", "openshell name", "credential env", "default model", "model mode"):
        t.add_column(col)
    for p in providers.PROVIDERS:
        t.add_row(p.key, p.name, p.credential_env, p.default_model or "—", p.model_mode)
    out.print(t)
    out.print("\n[bold]agents[/bold] (--agent)")
    for alias, canonical in sorted(providers.AGENTS.items()):
        marker = " [dim](alias)[/dim]" if alias != canonical else ""
        out.print(f"  {alias} -> {canonical}{marker}")


def main() -> None:
    try:
        app()
    except ConfigError as exc:
        err.print(f"[bold red]config error[/bold red] {exc}")
        sys.exit(2)
    except CommandError as exc:
        err.print(f"[bold red]command failed[/bold red] {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        err.print("\n[yellow]interrupted[/yellow]")
        sys.exit(130)


if __name__ == "__main__":
    main()
