"""Thin, typed wrapper over the ``nemoclaw`` CLI.

mega-nemo never reimplements NemoClaw — it composes it. This module exists so
that provider/model/agent inputs are validated *before* a 10-minute image build
starts, and so the rest of the codebase reads as sandbox operations rather than
argv construction.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import providers
from .config import Settings
from .shell import Result, run

STATE_FILE = Path.home() / ".nemoclaw" / "sandboxes.json"


class NemoclawError(RuntimeError):
    pass


@dataclass
class SandboxInfo:
    name: str
    provider: str
    model: str
    agent: str
    endpoint_url: str | None
    gpu: bool
    dcode_auto_approval: str
    tool_disclosure: str
    nemoclaw_version: str
    raw: dict

    @classmethod
    def from_raw(cls, raw: dict) -> SandboxInfo:
        return cls(
            name=raw.get("name", "?"),
            provider=raw.get("provider", "?"),
            model=raw.get("model", "?"),
            agent=raw.get("agent", "?"),
            endpoint_url=raw.get("endpointUrl"),
            gpu=bool(raw.get("sandboxGpuEnabled")),
            dcode_auto_approval=raw.get("dcodeAutoApprovalMode", "?"),
            tool_disclosure=raw.get("toolDisclosure", "?"),
            nemoclaw_version=raw.get("nemoclawVersion", "?"),
            raw=raw,
        )


def read_state() -> dict:
    if not STATE_FILE.is_file():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise NemoclawError(f"{STATE_FILE} is not valid JSON: {exc}") from exc


def list_sandboxes() -> list[SandboxInfo]:
    state = read_state()
    return [SandboxInfo.from_raw(v) for v in (state.get("sandboxes") or {}).values()]


def get_sandbox(name: str) -> SandboxInfo | None:
    raw = (read_state().get("sandboxes") or {}).get(name)
    return SandboxInfo.from_raw(raw) if raw else None


def default_sandbox() -> str | None:
    return read_state().get("defaultSandbox")


def exists(name: str) -> bool:
    return get_sandbox(name) is not None


def version() -> str:
    r = run(["nemoclaw", "--version"], check=False, always_run=True)
    return r.out or "unknown"


# ---------------------------------------------------------------------------
# Build inputs
# ---------------------------------------------------------------------------


@dataclass
class BuildSpec:
    """A fully-validated description of the sandbox to build."""

    name: str
    provider: providers.Provider
    model: str
    agent: str
    endpoint_url: str | None = None
    gpu: bool | None = None
    policy_tier: str | None = None
    dcode_auto_approval: str = "thread-opt-in"
    tool_disclosure: str = "progressive"
    observability: bool = False
    from_dockerfile: Path | None = None

    @property
    def policies(self) -> list[str]:
        pol = list(providers.DEFAULT_POLICIES)
        if self.provider.local:
            pol.append(providers.LOCAL_INFERENCE_POLICY)
        return pol

    def env(self) -> dict[str, str]:
        env = {
            "NEMOCLAW_PROVIDER": self.provider.key,
            "NEMOCLAW_MODEL": self.model,
            "NEMOCLAW_NON_INTERACTIVE": "1",
            "NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE": "1",
        }
        if self.policy_tier:
            env["NEMOCLAW_POLICY_TIER"] = self.policy_tier
        if self.endpoint_url:
            env["NEMOCLAW_ENDPOINT_URL"] = self.endpoint_url
        return env


def build_spec(settings: Settings, *, name: str | None = None) -> BuildSpec:
    """Validate settings into a BuildSpec, failing fast with actionable errors."""
    if not settings.provider:
        raise NemoclawError(
            "no provider. Pass --provider, set MEGA_PROVIDER, or add "
            "[sandbox] provider = \"...\" to mega.toml.\n"
            f"Known: {', '.join(sorted({p.key for p in providers.PROVIDERS}))}"
        )
    provider = providers.resolve(settings.provider)

    model = settings.model or provider.default_model
    if not model:
        raise NemoclawError(
            f"no model for provider {provider.key!r} and it has no default. Pass --model."
        )

    endpoint = settings.endpoint_url
    if provider.endpoint_required and not endpoint:
        raise NemoclawError(
            f"provider {provider.key!r} ({provider.label}) needs an explicit endpoint. "
            "Pass --endpoint-url."
        )

    agent = providers.resolve_agent(settings.agent)

    if settings.dcode_auto_approval not in {"disabled", "thread-opt-in"}:
        raise NemoclawError(
            f"dcode_auto_approval must be 'disabled' or 'thread-opt-in', "
            f"got {settings.dcode_auto_approval!r}"
        )
    if settings.tool_disclosure not in {"progressive", "direct"}:
        raise NemoclawError(
            f"tool_disclosure must be 'progressive' or 'direct', got {settings.tool_disclosure!r}"
        )

    return BuildSpec(
        name=name or settings.sandbox,
        provider=provider,
        model=model,
        agent=agent,
        endpoint_url=endpoint,
        gpu=settings.gpu,
        policy_tier=settings.policy_tier,
        dcode_auto_approval=settings.dcode_auto_approval,
        tool_disclosure=settings.tool_disclosure,
        observability=settings.observability,
    )


def credential_present(spec: BuildSpec) -> bool:
    return bool(os.environ.get(spec.provider.credential_env))


# ---------------------------------------------------------------------------
# Sandbox lifecycle
# ---------------------------------------------------------------------------


def onboard(spec: BuildSpec, *, fresh: bool = True, recreate: bool = False) -> Result:
    argv = [
        "nemoclaw",
        "onboard",
        "--name",
        spec.name,
        "--agent",
        spec.agent,
        "--non-interactive",
        "--yes",
        "--yes-i-accept-third-party-software",
        "--tool-disclosure",
        spec.tool_disclosure,
    ]
    if fresh:
        argv.append("--fresh")
    if recreate:
        argv.append("--recreate-sandbox")
    if spec.gpu is True:
        argv += ["--gpu", "--sandbox-gpu"]
    elif spec.gpu is False:
        argv += ["--no-gpu", "--no-sandbox-gpu"]
    argv.append("--observability" if spec.observability else "--no-observability")
    if spec.from_dockerfile:
        argv += ["--from", str(spec.from_dockerfile)]

    return run(argv, env=spec.env(), capture=False, timeout=None)


def rebuild(spec: BuildSpec) -> Result:
    argv = ["nemoclaw", spec.name, "rebuild", "--yes", "--tool-disclosure", spec.tool_disclosure]
    if providers.is_dcode(spec.agent):
        argv += ["--dcode-auto-approval", spec.dcode_auto_approval]
    argv.append("--observability" if spec.observability else "--no-observability")
    return run(argv, env=spec.env(), capture=False, timeout=None)


def set_inference(name: str, provider: providers.Provider, model: str) -> Result:
    """Repoint an existing sandbox at a different provider/model without a rebuild."""
    return run(
        ["nemoclaw", name, "inference", "set", "--provider", provider.name, "--model", model]
    )


def exec_in(
    name: str,
    argv: list[str],
    *,
    workdir: str | None = None,
    check: bool = True,
    timeout: int | None = 900,
    tty: bool = False,
    capture: bool = True,
) -> Result:
    """Run a command inside the sandbox.

    Internal callers want ``capture=True`` so they can parse the output;
    ``mega sandbox exec`` wants ``capture=False`` so the user sees it stream.
    """
    cmd = ["nemoclaw", name, "exec"]
    if workdir:
        cmd += ["--workdir", workdir]
    if timeout is not None:
        cmd += ["--timeout", str(timeout)]
    cmd += ["--tty" if tty else "--no-tty", "--", *argv]
    # Give the subprocess a little more rope than nemoclaw's own timeout, so
    # nemoclaw gets to report the timeout itself instead of being killed first.
    return run(cmd, check=check, timeout=None if timeout is None else timeout + 30, capture=capture)


def logs(
    name: str,
    *,
    follow: bool = False,
    tail: int | None = None,
    since: str | None = None,
) -> Result:
    argv = ["nemoclaw", name, "logs"]
    if follow:
        argv.append("--follow")
    if tail is not None:
        argv += ["--tail", str(tail)]
    if since:
        argv += ["--since", since]
    # No timeout: --follow is meant to run until the user interrupts it.
    return run(argv, check=False, capture=False, timeout=None)


def sh_in(name: str, script: str, *, workdir: str | None = None, check: bool = True) -> Result:
    """Run a shell snippet inside the sandbox."""
    return exec_in(name, ["bash", "-lc", script], workdir=workdir, check=check)


def upload(name: str, host_path: Path, sandbox_dest: str) -> Result:
    return run(["nemoclaw", name, "upload", str(host_path), sandbox_dest], timeout=None)


def download(name: str, sandbox_path: str, host_dest: Path) -> Result:
    return run(["nemoclaw", name, "download", sandbox_path, str(host_dest)], timeout=None)


def skill_install(name: str, host_skill_dir: Path) -> Result:
    return run(["nemoclaw", name, "skill", "install", str(host_skill_dir)], timeout=None)


def policy_add(name: str, preset: str, *, check: bool = False) -> Result:
    return run(["nemoclaw", name, "policy-add", preset, "--yes"], check=check)


def policy_list(name: str) -> list[str]:
    r = run(["nemoclaw", name, "policy-list"], check=False, always_run=True)
    return [ln.strip().lstrip("- ").split()[0] for ln in r.stdout.splitlines() if ln.strip()]


def mcp_add(name: str, server: str, url: str, env_key: str | None = None) -> Result:
    argv = ["nemoclaw", name, "mcp", "add", server, "--url", url]
    if env_key:
        argv += ["--env", env_key]
    return run(argv, check=False)


def status(name: str) -> Result:
    return run(["nemoclaw", name, "status"], check=False, capture=False)


def doctor(name: str, *, fix: bool = False) -> Result:
    argv = ["nemoclaw", name, "doctor"]
    if fix:
        argv.append("--fix")
    return run(argv, check=False, capture=False)


def connect(name: str) -> Result:
    return run(["nemoclaw", name, "connect"], check=False, capture=False, timeout=None)


def agent_turn(name: str, prompt: str) -> Result:
    """One non-interactive dcode turn. Useful for smoke tests and CI."""
    return run(["nemoclaw", name, "agent", "--message", prompt], check=False, capture=False,
               timeout=None)


def destroy(name: str) -> Result:
    return run(["nemoclaw", name, "destroy", "--yes"], check=False, capture=False, timeout=None)
