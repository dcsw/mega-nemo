"""Subprocess plumbing.

Every external command in mega-nemo goes through :func:`run`. That gives one
place to implement ``--dry-run``, one place to echo what is about to happen, and
one place to decide whether a non-zero exit is fatal.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

console = Console(stderr=True)


class CommandError(RuntimeError):
    """A command exited non-zero and the caller asked for that to be fatal."""

    def __init__(self, result: Result) -> None:
        self.result = result
        super().__init__(
            f"`{result.display}` exited {result.code}\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


@dataclass
class Result:
    argv: list[str]
    code: int
    stdout: str
    stderr: str
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def display(self) -> str:
        return " ".join(shlex.quote(a) for a in self.argv)

    @property
    def out(self) -> str:
        return self.stdout.strip()


# Set by the CLI root callback.
DRY_RUN = False
VERBOSE = False


def set_mode(*, dry_run: bool, verbose: bool) -> None:
    global DRY_RUN, VERBOSE
    DRY_RUN = dry_run
    VERBOSE = verbose


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture: bool = True,
    input_text: str | None = None,
    # Read-only probes must still execute under --dry-run, otherwise every
    # subsequent decision is made against empty output.
    always_run: bool = False,
    timeout: int | None = None,
) -> Result:
    """Run ``argv``. Returns a :class:`Result`; raises :class:`CommandError` if
    ``check`` and the command failed."""
    argv = [str(a) for a in argv]
    display = " ".join(shlex.quote(a) for a in argv)

    if DRY_RUN and not always_run:
        console.print(f"[dim]dry-run[/dim] [cyan]{display}[/cyan]")
        return Result(argv, 0, "", "", dry_run=True)

    if VERBOSE:
        where = f" [dim](in {cwd})[/dim]" if cwd else ""
        console.print(f"[dim]$[/dim] [cyan]{display}[/cyan]{where}")

    merged_env = {**os.environ, **(env or {})} if env else None

    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            env=merged_env,
            capture_output=capture,
            text=True,
            input=input_text,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        result = Result(argv, 127, "", f"{argv[0]}: not found")
        if check:
            raise CommandError(result) from exc
        return result
    except subprocess.TimeoutExpired as exc:
        result = Result(argv, 124, "", f"timed out after {timeout}s")
        if check:
            raise CommandError(result) from exc
        return result

    result = Result(argv, proc.returncode, proc.stdout or "", proc.stderr or "")
    if check and not result.ok:
        raise CommandError(result)
    return result


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def require(binary: str, hint: str = "") -> None:
    if not have(binary):
        suffix = f"\n  {hint}" if hint else ""
        raise CommandError(Result([binary], 127, "", f"{binary} is not on PATH.{suffix}"))
