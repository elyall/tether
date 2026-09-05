"""Typer command-line interface for tether.

Install with the ``cli`` extra (``pip install tether[cli]``). Every command that
inspects external systems fans out concurrently; ``--json`` emits
machine-readable output for agents and scripts.
"""

from __future__ import annotations

import json
import sys
from typing import NoReturn

try:
    import typer
except ImportError as exc:  # pragma: no cover - optional dep
    raise SystemExit(
        "the tether CLI requires the 'cli' extra: pip install tether[cli]"
    ) from exc

from tether.errors import TetherError
from tether.handles import (
    FileHandle,
    GitHandle,
    Handle,
    IcebergHandle,
    IcechunkHandle,
    NeonHandle,
)
from tether.manifest import Policy
from tether.repo import CommitResult, GcReport, Repo, StatusReport

app = typer.Typer(
    name="tether",
    help="jj-style version control for heterogeneous datasets.",
    no_args_is_help=True,
    add_completion=False,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _repo() -> Repo:
    try:
        return Repo.find(".")
    except TetherError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc


def _emit(data: object, *, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(data, indent=2, default=str))


def _fail(exc: Exception) -> NoReturn:
    typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _handle_address(handle: Handle) -> str:
    if isinstance(handle, FileHandle):
        return handle.uri + (f"#{handle.version_id}" if handle.version_id else "")
    if isinstance(handle, NeonHandle):
        return handle.url
    if isinstance(handle, GitHandle):
        return f"{handle.path}@{handle.sha}"
    if isinstance(handle, IcechunkHandle):
        ref = handle.tag or handle.branch or handle.snapshot_id
        return f"{handle.key}#{ref}"
    if isinstance(handle, IcebergHandle):
        return f"{handle.key}#{handle.ref or handle.snapshot_id}"
    return handle.key


def _status_payload(report: StatusReport) -> dict:
    return {
        "manifest_hash": report.manifest_hash,
        "stale": report.stale,
        "objects": [
            {
                "key": o.key,
                "kind": o.kind,
                "tier": o.tier.value,
                "state": o.state_label,
                "pinned": o.pinned,
                "recoverable": o.recoverable,
                "verify": o.verify.status.value if o.verify else None,
            }
            for o in report.objects
        ],
    }


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@app.command()
def init(
    path: str = typer.Argument(".", help="Dataset root."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Initialize a tether dataset in an existing git/jj repository."""
    try:
        repo = Repo.init(path)
    except TetherError as exc:
        _fail(exc)
    typer.echo(f"initialized tether dataset at {repo.root}")
    _emit({"root": str(repo.root)}, as_json=json_out)


@app.command()
def add(
    key: str = typer.Argument(..., help="Object key (may contain '/')."),
    locator: str | None = typer.Argument(None, help="Primary locator (uri / path)."),
    kind: str = typer.Option(..., "--kind", help="Backend kind."),
    project_id: str | None = typer.Option(None, "--project-id"),
    database: str | None = typer.Option(None, "--database"),
    role: str | None = typer.Option(None, "--role"),
    branch: str | None = typer.Option(None, "--branch"),
    remote: str | None = typer.Option(None, "--remote"),
    region: str | None = typer.Option(None, "--region"),
    set_: list[str] = typer.Option(
        [], "--set", help="Extra locator field key=value (repeatable)."
    ),
    write: str = typer.Option("fork", "--write", help="fork | track"),
    file: str = typer.Option("immutable", "--file", help="immutable | versioned"),
    pin: str = typer.Option("native", "--pin", help="native | record"),
) -> None:
    """Register an object in the working copy."""
    repo = _repo()
    loc: dict[str, object] = {}
    if locator is not None:
        loc["uri"] = locator
    for name, value in (
        ("project_id", project_id),
        ("database", database),
        ("role", role),
        ("branch", branch),
        ("remote", remote),
        ("region", region),
    ):
        if value is not None:
            loc[name] = value
    for item in set_:
        if "=" not in item:
            _fail(TetherError(f"--set expects key=value, got {item!r}"))
        k, v = item.split("=", 1)
        loc[k] = v
    try:
        policy = Policy.from_dict({"write": write, "file": file, "pin": pin})
        repo.add(key, kind, loc, policy=policy)
    except TetherError as exc:
        _fail(exc)
    typer.echo(f"added {key} ({kind})")


@app.command()
def remove(key: str = typer.Argument(...)) -> None:
    """Unregister an object (does not touch the external system)."""
    repo = _repo()
    try:
        repo.remove(key)
    except TetherError as exc:
        _fail(exc)
    typer.echo(f"removed {key}")


@app.command()
def status(
    no_snapshot: bool = typer.Option(False, "--no-snapshot"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Fan out, fingerprint every object, and classify each one."""
    repo = _repo()
    try:
        report = repo.status(do_snapshot=not no_snapshot)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(_status_payload(report), as_json=True)
        return
    flag = " (STALE)" if report.stale else ""
    typer.echo(f"dataset {report.manifest_hash[:12]}{flag}")
    for o in report.objects:
        v = f" verify={o.verify.status.value}" if o.verify else ""
        rec = "" if o.recoverable else " unrecoverable"
        typer.echo(f"  {o.state_label:>9}  {o.key}  [{o.kind}/{o.tier.value}]{rec}{v}")


@app.command()
def snapshot(json_out: bool = typer.Option(False, "--json")) -> None:
    """Fingerprint every object and cache the result in the workspace."""
    repo = _repo()
    try:
        states = repo.snapshot()
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(states, as_json=True)
        return
    for key, state in states.items():
        typer.echo(f"  {key}: {state}")


@app.command()
def commit(
    message: str = typer.Option(..., "-m", "--message"),
    no_vcs: bool = typer.Option(False, "--no-vcs"),
    strict: bool = typer.Option(False, "--strict"),
    force: bool = typer.Option(False, "--force"),
    no_snapshot: bool = typer.Option(False, "--no-snapshot"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Pin mutable objects and record their state in the manifests."""
    repo = _repo()
    try:
        result: CommitResult = repo.commit(
            message,
            vcs=not no_vcs,
            strict=strict,
            force=force,
            do_snapshot=not no_snapshot,
        )
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(
            {
                "vcs_commit": result.vcs_commit,
                "pinned": {k: (p.ref if p else None) for k, p in result.pinned.items()},
                "unrecoverable": result.unrecoverable,
                "unchanged": result.unchanged,
            },
            as_json=True,
        )
        return
    for key, p in result.pinned.items():
        typer.echo(f"  pinned {key} -> {p.ref if p else '(addressable)'}")
    for key in result.unrecoverable:
        typer.secho(f"  recorded {key} (not recoverable)", fg=typer.colors.YELLOW)
    if result.vcs_commit:
        typer.echo(f"committed {result.vcs_commit[:12]}")


@app.command()
def new(
    rev: str | None = typer.Argument(None, help="Revision to fork from."),
    keep: bool = typer.Option(False, "--keep"),
) -> None:
    """Fork fresh writable branches off the pinned state at ``rev``."""
    repo = _repo()
    try:
        repo.new(rev, keep=keep)
    except TetherError as exc:
        _fail(exc)
    typer.echo("forked working refs" if not keep else "kept working refs")


@app.command(name="open")
def open_(
    key: str = typer.Argument(...),
    rev: str | None = typer.Option(None, "-r", "--rev"),
    read_only: bool | None = typer.Option(
        None, "--read-only/--writable", help="Force read-only or writable."
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Print a native handle for an object (address on stdout)."""
    repo = _repo()
    try:
        handle = repo.open(key, rev=rev, read_only=read_only)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(
            {
                "key": handle.key,
                "read_only": handle.read_only,
                "address": _handle_address(handle),
                "type": type(handle).__name__,
            },
            as_json=True,
        )
        return
    typer.echo(_handle_address(handle))


@app.command()
def verify(
    rev: str | None = typer.Option(None, "-r", "--rev"),
    all_history: bool = typer.Option(False, "--all-history"),
    deep: bool = typer.Option(False, "--deep"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Check that recorded states and pins still resolve."""
    repo = _repo()
    try:
        reports = repo.verify(rev=rev, deep=deep, all_history=all_history)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(
            {
                k: {"status": r.status.value, "message": r.message}
                for k, r in reports.items()
            },
            as_json=True,
        )
    else:
        for k, r in reports.items():
            color = typer.colors.GREEN if r.ok else typer.colors.RED
            typer.secho(f"  {r.status.value:>8}  {k}  {r.message}", fg=color)
    if any(not r.ok for r in reports.values()):
        raise typer.Exit(1)


@app.command()
def diff(
    rev_a: str | None = typer.Argument(None),
    rev_b: str | None = typer.Argument(None),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show object-level manifest differences between two revisions."""
    repo = _repo()
    try:
        entries = repo.diff(rev_a, rev_b)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(
            [
                {"key": e.key, "change": e.change, "a": e.a_pin, "b": e.b_pin}
                for e in entries
            ],
            as_json=True,
        )
        return
    for e in entries:
        if e.change != "unchanged":
            typer.echo(f"  {e.change:>9}  {e.key}")


@app.command()
def gc(
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Release native pins that no manifest in VCS history references."""
    repo = _repo()
    try:
        report: GcReport = repo.gc(dry_run=dry_run)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(
            {"dry_run": report.dry_run, "unpinned": report.unpinned},
            as_json=True,
        )
        return
    verb = "would unpin" if report.dry_run else "unpinned"
    total = sum(len(v) for v in report.unpinned.values())
    typer.echo(f"{verb} {total} pin(s)")
    for kind, ids in report.unpinned.items():
        for pid in ids:
            typer.echo(f"  {kind}: {pid}")


def main() -> None:
    try:
        app()
    except TetherError as exc:  # pragma: no cover - defensive top-level
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
