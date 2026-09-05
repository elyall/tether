"""Typer command-line interface for tether.

Install with the ``cli`` extra (``pip install tether-vcs[cli]``). Every command that
inspects external systems fans out concurrently; ``--json`` emits
machine-readable output for agents and scripts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import NoReturn

try:
    import typer
except ImportError as exc:  # pragma: no cover - optional dep
    raise SystemExit(
        "the tether CLI requires the 'cli' extra: pip install tether-vcs[cli]"
    ) from exc

from tether.backends.base import HistoryEntry
from tether.errors import TetherError
from tether.handles import (
    DeltaHandle,
    DoltHandle,
    DuckLakeHandle,
    FileHandle,
    GitHandle,
    Handle,
    IcebergHandle,
    IcechunkHandle,
    LakeFSHandle,
    LanceHandle,
    NeonHandle,
)
from tether.manifest import Policy
from tether.plan import Plan
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


def _snapshot_default(repo: Repo, no_snapshot: bool) -> bool:
    """``[snapshot] auto`` sets the default; ``--no-snapshot`` always wins."""
    return repo.config.snapshot_auto and not no_snapshot


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
    if isinstance(handle, DeltaHandle):
        return f"{handle.uri}@v{handle.version}"
    if isinstance(handle, LanceHandle):
        ref = handle.tag or f"{handle.branch}@v{handle.version}"
        return f"{handle.uri}#{ref}"
    if isinstance(handle, LakeFSHandle):
        return handle.uri
    if isinstance(handle, DuckLakeHandle):
        return f"{handle.metadata}#snapshot={handle.snapshot_id}"
    if isinstance(handle, DoltHandle):
        return handle.url
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
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
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
    kind: str = typer.Option(
        ...,
        "--kind",
        help="Backend kind: file, icechunk, neon, git, iceberg, delta, lance, "
        "lakefs, ducklake, dolt.",
    ),
    project_id: str | None = typer.Option(None, "--project-id", help="Neon project."),
    database: str | None = typer.Option(None, "--database", help="Neon/Dolt database."),
    role: str | None = typer.Option(None, "--role", help="Neon role for connections."),
    branch: str | None = typer.Option(
        None, "--branch", help="Base branch (default main) for branching backends."
    ),
    remote: str | None = typer.Option(
        None, "--remote", help="git remote to push pins to."
    ),
    region: str | None = typer.Option(None, "--region", help="Object-store region."),
    repository: str | None = typer.Option(None, "--repository", help="lakeFS repo."),
    prefix: str | None = typer.Option(None, "--prefix", help="Path scope in a repo."),
    host: str | None = typer.Option(None, "--host", help="Dolt server host."),
    port: int | None = typer.Option(None, "--port", help="Dolt server port."),
    table: str | None = typer.Option(None, "--table", help="DuckLake table scope."),
    set_: list[str] = typer.Option(
        [], "--set", help="Extra locator field key=value (repeatable)."
    ),
    write: str = typer.Option(
        "fork",
        "--write",
        help="fork: `new` forks a per-workspace branch off the pin; "
        "track: the working ref stays the base branch.",
    ),
    file: str = typer.Option(
        "immutable",
        "--file",
        help="file backend: immutable (drift is an error) or versioned "
        "(Addressable via object-store version ids).",
    ),
    pin: str = typer.Option(
        "native",
        "--pin",
        help="native (default: create a tag/branch per commit) or record (no native "
        "ref; forks come from the recorded state while the system retains it).",
    ),
    at: str | None = typer.Option(
        None,
        "--at",
        help="Detach the base at a native state (snapshot id, version, commit, or "
        "tag) instead of the branch head; commit pins it and new forks from it.",
    ),
    pick: bool = typer.Option(
        False,
        "--pick",
        help="List the system's history and choose the base state interactively.",
    ),
) -> None:
    """Register an object in the working copy.

    The positional LOCATOR is stored as the `uri` field; named options set
    other locator fields. Nothing is contacted until the next status/commit
    (except with --pick, which lists history first).
    """
    repo = _repo()
    loc = _build_locator(
        locator,
        set_,
        project_id=project_id,
        database=database,
        role=role,
        branch=branch,
        remote=remote,
        region=region,
        repository=repository,
        prefix=prefix,
        host=host,
        port=port,
        table=table,
    )
    if at is not None:
        loc["at"] = at
    if pick:
        try:
            entries = repo.history_for(kind, loc, limit=20)
        except TetherError as exc:
            _fail(exc)
        chosen = _pick_entry(entries)
        loc["at"] = chosen.id
    try:
        policy = Policy.from_dict({"write": write, "file": file, "pin": pin})
        repo.add(key, kind, loc, policy=policy)
    except TetherError as exc:
        _fail(exc)
    suffix = f" at {loc['at']}" if "at" in loc else ""
    typer.echo(f"added {key} ({kind}){suffix}")


def _build_locator(
    locator: str | None, set_: list[str], **fields: object
) -> dict[str, object]:
    loc: dict[str, object] = {}
    if locator is not None:
        loc["uri"] = locator
    for name, value in fields.items():
        if value is not None:
            loc[name] = value
    for item in set_:
        if "=" not in item:
            _fail(TetherError(f"--set expects key=value, got {item!r}"))
        k, v = item.split("=", 1)
        loc[k] = v
    return loc


def _format_history(entries: list[HistoryEntry], *, numbered: bool = False) -> None:
    width = max((len(e.id) for e in entries), default=0)
    for n, e in enumerate(entries, start=1):
        prefix = f"{n:>3}. " if numbered else "  "
        when = (e.when or "").replace("T", " ")[:16]
        refs = f"  [{', '.join(e.refs)}]" if e.refs else ""
        typer.echo(f"{prefix}{e.id:<{width}}  {when:<16}  {e.message}{refs}")


def _pick_entry(entries: list[HistoryEntry]) -> HistoryEntry:
    if not entries:
        _fail(TetherError("no history to choose from"))
    _format_history(entries, numbered=True)
    answer = typer.prompt("Pick an entry (number or id)", default="1")
    answer = answer.strip()
    if answer.isdigit() and 1 <= int(answer) <= len(entries):
        return entries[int(answer) - 1]
    for e in entries:
        if e.id == answer or e.id.startswith(answer):
            return e
    _fail(TetherError(f"no entry matches {answer!r}"))


@app.command()
def log(
    target: str = typer.Argument(
        ..., help="Object key; or, with --kind, a locator (uri/path) to browse."
    ),
    kind: str | None = typer.Option(
        None, "--kind", help="Browse an unregistered object of this backend kind."
    ),
    ref: str | None = typer.Option(
        None,
        "--ref",
        help="Start from this branch/tag/id (default: working ref or base).",
    ),
    limit: int = typer.Option(20, "-n", "--limit", help="Max entries."),
    branch: str | None = typer.Option(
        None, "--branch", help="With --kind: base branch."
    ),
    set_: list[str] = typer.Option(
        [], "--set", help="With --kind: extra locator field key=value (repeatable)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List an object's native history (snapshots, versions, commits), newest first.

    Entry ids are valid values for `tether add --at`; native branches, tags,
    and tether pins pointing at an entry are shown in brackets.
    """
    repo = _repo()
    try:
        if kind is not None:
            loc = _build_locator(target, set_, branch=branch)
            entries = repo.history_for(kind, loc, ref=ref, limit=limit)
        else:
            entries = repo.history(target, ref=ref, limit=limit)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit([e.to_dict() for e in entries], as_json=True)
        return
    _format_history(entries)


@app.command()
def remove(key: str = typer.Argument(..., help="Object key.")) -> None:
    """Unregister an object (does not touch the external system)."""
    repo = _repo()
    try:
        repo.remove(key)
    except TetherError as exc:
        _fail(exc)
    typer.echo(f"removed {key}")


@app.command()
def status(
    no_snapshot: bool = typer.Option(
        False, "--no-snapshot", help="Reuse the cached fingerprints; contact nothing."
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Fan out, fingerprint every object, and classify each one.

    Labels: new (never committed), modified, clean, error. `(STALE)` means
    HEAD's manifests changed since this workspace forked; run `tether new`.
    """
    repo = _repo()
    try:
        report = repo.status(do_snapshot=_snapshot_default(repo, no_snapshot))
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
def snapshot(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
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


def _show_plan(plan: Plan, *, as_json: bool) -> None:
    if as_json:
        typer.echo(plan.to_json())
        return
    typer.echo(f"plan: {plan.command} ({len(plan.writes)} write(s))")
    for line in plan.render():
        typer.echo(line)


def _save_plan(plan: Plan, path: Path | None) -> None:
    if path is not None:
        path.write_text(plan.to_json())
        typer.secho(f"plan written to {path}", err=True)


def _load_plan(path: Path, expected: str) -> Plan:
    try:
        plan = Plan.from_json(path.read_text())
    except (OSError, TetherError) as exc:
        _fail(exc)
    if plan.command != expected:
        _fail(TetherError(f"{path} is a {plan.command!r} plan, expected {expected!r}"))
    return plan


@app.command()
def commit(
    message: str | None = typer.Option(
        None,
        "-m",
        "--message",
        help="VCS commit message (required unless --from-plan).",
    ),
    no_vcs: bool = typer.Option(
        False, "--no-vcs", help="Write manifests but skip the jj/git commit."
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Fail instead of recording Observed objects unrecoverably.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Skip quiescence checks (e.g. active Neon writers)."
    ),
    no_snapshot: bool = typer.Option(
        False, "--no-snapshot", help="Commit the cached fingerprints as-is."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be pinned/recorded; write nothing."
    ),
    plan_out: Path | None = typer.Option(
        None, "--plan", help="Write the plan to FILE (implies --dry-run)."
    ),
    from_plan: Path | None = typer.Option(
        None,
        "--from-plan",
        help="Apply a plan saved with --plan instead of replanning.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Pin mutable objects and record their state in the manifests.

    Pinnable/Forkable objects get a native ref `tether.<pin_id>`; Addressable
    and `pin = "record"` objects are recorded; Observed objects are recorded as
    unrecoverable. Unchanged objects are skipped. Then the manifests are
    committed. `--dry-run` / `--plan` preview the actions; `--from-plan` applies
    a saved plan after checking nothing changed underneath it.
    """
    repo = _repo()
    try:
        if from_plan is not None:
            plan = _load_plan(from_plan, "commit")
            if message is not None:
                plan.context["message"] = message
            result: CommitResult = repo.apply_commit(plan, vcs=not no_vcs)
        else:
            if message is None:
                _fail(TetherError("a message is required: -m/--message"))
            plan = repo.plan_commit(
                message,
                strict=strict,
                force=force,
                do_snapshot=_snapshot_default(repo, no_snapshot),
            )
            if dry_run or plan_out is not None:
                _save_plan(plan, plan_out)
                _show_plan(plan, as_json=json_out)
                return
            result = repo.apply_commit(plan, vcs=not no_vcs, verify=False)
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
    keep: bool = typer.Option(
        False, "--keep", help="Keep current working refs; only refresh the baseline."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show which branches would be forked; write nothing."
    ),
    plan_out: Path | None = typer.Option(
        None, "--plan", help="Write the plan to FILE (implies --dry-run)."
    ),
    from_plan: Path | None = typer.Option(
        None,
        "--from-plan",
        help="Apply a plan saved with --plan instead of replanning.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Fork fresh writable branches off the pinned state at REV.

    Moves the VCS working copy to REV if given, then creates a
    `tether.ws.<workspace>.<key>` branch per Forkable object from its pin (or
    from the recorded state for `pin = "record"` objects); track-policy objects
    stay on their base branch. `--dry-run` / `--plan` preview; `--from-plan`
    applies a saved plan.
    """
    repo = _repo()
    try:
        if from_plan is not None:
            plan = _load_plan(from_plan, "new")
            repo.apply_new(plan)
        else:
            plan = repo.plan_new(rev, keep=keep)
            if dry_run or plan_out is not None:
                _save_plan(plan, plan_out)
                _show_plan(plan, as_json=json_out)
                return
            repo.apply_new(plan, verify=False)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit({"working_refs": repo.workspace.working_refs}, as_json=True)
        return
    typer.echo(
        "forked working refs" if not plan.context.get("keep") else "kept working refs"
    )
    for key, ref in sorted(repo.workspace.working_refs.items()):
        typer.echo(f"  {key} -> {ref}")


@app.command(name="open")
def open_(
    key: str = typer.Argument(..., help="Object key."),
    rev: str | None = typer.Option(
        None,
        "-r",
        "--rev",
        help="Open read-only at this revision's pinned state (default: $TETHER_REV).",
    ),
    read_only: bool | None = typer.Option(
        None, "--read-only/--writable", help="Force read-only or writable."
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Print a native handle for an object (address on stdout).

    Without --rev, Forkable objects open writable at their working ref and
    everything else read-only at the base.
    """
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
    rev: str | None = typer.Option(
        None, "-r", "--rev", help="Verify the manifests at this revision."
    ),
    all_history: bool = typer.Option(
        False, "--all-history", help="Verify every commit in the repository."
    ),
    deep: bool = typer.Option(
        False, "--deep", help="Actually open recorded states (resolves `unknown`)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Check that recorded states and pins still resolve.

    Reports ok, drifted, missing, or unknown per object; exits 1 if anything
    is not ok.
    """
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
    rev_a: str | None = typer.Argument(None, help="From revision (default: HEAD)."),
    rev_b: str | None = typer.Argument(
        None, help="To revision (default: working tree)."
    ),
    content: bool = typer.Option(
        False, "--content", "-c", help="Also describe what changed inside each object."
    ),
    limit: int = typer.Option(20, "--limit", help="Max entries shown per object."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show object-level manifest differences between two revisions.

    With --content, changed objects whose backend supports it are diffed
    natively (files, tables, arrays, fragments, commits) using metadata only.
    """
    repo = _repo()
    try:
        entries = repo.diff(rev_a, rev_b, content=content)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(
            [
                {
                    "key": e.key,
                    "change": e.change,
                    "a": e.a_pin,
                    "b": e.b_pin,
                    "detail": e.detail.to_dict() if e.detail else None,
                    "detail_error": e.detail_error,
                }
                for e in entries
            ],
            as_json=True,
        )
        return
    for e in entries:
        if e.change == "unchanged":
            continue
        line = f"  {e.change:>9}  {e.key}"
        if e.detail is not None:
            line += f"  [{e.detail.summary}]"
        elif e.detail_error:
            line += f"  [diff failed: {e.detail_error}]"
        typer.echo(line)
        if e.detail is None:
            continue
        shown = e.detail.entries[: max(limit, 0)]
        for item in shown:
            suffix = f"  {item.detail}" if item.detail else ""
            typer.echo(f"{'':14}{item.change:>9}  {item.path}{suffix}")
        hidden = len(e.detail.entries) - len(shown)
        if hidden > 0:
            typer.echo(f"{'':14}... {hidden} more (use --limit)")


@app.command()
def gc(
    dry_run: bool = typer.Option(
        True, "--dry-run/--no-dry-run", help="Show the plan (default) or apply it."
    ),
    prune_workspaces: bool = typer.Option(
        False,
        "--prune-workspaces",
        help="Also delete `tether.ws.*` branches left by other workspaces.",
    ),
    keep_workspace: list[str] = typer.Option(
        [],
        "--keep-workspace",
        help="Workspace id (or 8-char prefix) whose branches --prune-workspaces "
        "must keep; repeatable. This workspace is always kept.",
    ),
    force_prune: bool = typer.Option(
        False,
        "--force-prune",
        help="With --prune-workspaces: delete stray branches even when they hold "
        "unpinned writes, a pin-less recorded state, or are the storage itself "
        "(Neon). Data on them is lost.",
    ),
    plan_out: Path | None = typer.Option(
        None, "--plan", help="Write the plan to FILE (implies --dry-run)."
    ),
    from_plan: Path | None = typer.Option(
        None, "--from-plan", help="Apply a plan saved with --plan."
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Release native pins that no manifest in VCS history references.

    Also forgets this workspace's refs for removed objects and deletes
    unreferenced listings. `--prune-workspaces` evaluates stray
    `tether.ws.*` branches (other workspaces' and this one's unused): a branch
    is deleted only if its head is pinned or equals the base head, otherwise
    kept -- `--force-prune` deletes those too. Dry-run by default: pass
    `--no-dry-run` (or `--from-plan`) to release.
    """
    if force_prune and not prune_workspaces:
        _fail(TetherError("--force-prune requires --prune-workspaces"))
    repo = _repo()
    try:
        if from_plan is not None:
            plan = _load_plan(from_plan, "gc")
            report: GcReport = repo.apply_gc(plan)
        else:
            plan = repo.plan_gc(
                prune_workspaces=prune_workspaces,
                keep_workspaces=set(keep_workspace) or None,
                force_prune=force_prune,
            )
            if dry_run or plan_out is not None:
                _save_plan(plan, plan_out)
                _show_plan(plan, as_json=json_out)
                return
            report = repo.apply_gc(plan)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(
            {
                "dry_run": report.dry_run,
                "unpinned": report.unpinned,
                "deleted_working_refs": report.deleted_working_refs,
                "kept_working_refs": report.kept_working_refs,
                "forgotten_working_refs": report.forgotten_working_refs,
                "deleted_listings": report.deleted_listings,
            },
            as_json=True,
        )
        return
    total = sum(len(v) for v in report.unpinned.values())
    typer.echo(f"unpinned {total} pin(s)")
    for kind, ids in report.unpinned.items():
        for pid in ids:
            typer.echo(f"  {kind}: {pid}")
    branches = sum(len(v) for v in report.deleted_working_refs.values())
    if branches:
        typer.echo(f"deleted {branches} working branch(es)")
        for key, refs in report.deleted_working_refs.items():
            for ref in refs:
                typer.echo(f"  {key}: {ref}")
    kept = sum(len(v) for v in report.kept_working_refs.values())
    if kept:
        typer.secho(
            f"kept {kept} working branch(es) holding unpinned data "
            f"(--force-prune deletes)",
            fg=typer.colors.YELLOW,
        )
        for key, refs in report.kept_working_refs.items():
            for ref in refs:
                typer.echo(f"  {key}: {ref}")
    forgotten = sum(len(v) for v in report.forgotten_working_refs.values())
    if forgotten:
        typer.echo(f"forgot {forgotten} working ref(s) of removed object(s)")
    if report.deleted_listings:
        typer.echo(f"deleted {len(report.deleted_listings)} orphan listing(s)")


def main() -> None:
    try:
        app()
    except TetherError as exc:  # pragma: no cover - defensive top-level
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
