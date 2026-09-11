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
from tether.plan import Action, Plan
from tether.repo import (
    CommitResult,
    GcReport,
    PromoteReport,
    Repo,
    StatusReport,
    short_state,
)

app = typer.Typer(
    name="tether",
    help="jj-style version control for heterogeneous datasets.",
    no_args_is_help=True,
    add_completion=False,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _repo(*, allow_outdated: bool = False) -> Repo:
    try:
        return Repo.find(".", allow_outdated=allow_outdated)
    except TetherError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc


def _emit(data: object, *, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(data, indent=2, default=str))


def _fail(exc: Exception) -> NoReturn:
    typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _want_snapshot(repo: Repo, flag: bool | None) -> bool:
    """``--snapshot`` / ``--no-snapshot`` win; otherwise ``[snapshot] auto``."""
    return repo.config.snapshot_auto if flag is None else flag


def _age(iso: str | None) -> str:
    """``3m ago`` / ``2h ago`` / ``4d ago`` from an ISO timestamp."""
    if not iso:
        return "never"
    from datetime import UTC, datetime

    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    seconds = max(0, int((datetime.now(UTC) - then).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


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
        "stale_keys": report.stale_keys,
        "fresh": report.fresh,
        "snapshot_at": report.snapshot_at,
        "vcs_drift": [
            {"op": d.op.id, "commit": d.commit, "referenced": d.referenced}
            for d in report.vcs_drift
        ],
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
    typer.echo(
        f"initialized tether dataset at {repo.root} (dataset id "
        f"{repo.config.dataset_id}: its pins and working branches are "
        f"tether.{repo.config.dataset_id}.* / tether.ws.{repo.config.dataset_id}.*)"
    )
    _emit(
        {"root": str(repo.root), "dataset_id": repo.config.dataset_id},
        as_json=json_out,
    )


@app.command()
def add(
    key: str = typer.Argument(..., help="Object key (may contain '/')."),
    locator: str | None = typer.Argument(None, help="Primary locator (uri / path)."),
    kind: str = typer.Option(
        ...,
        "--kind",
        help="Backend kind: file, icechunk, neon, git, iceberg, delta, lance; "
        "experimental: lakefs, ducklake, dolt.",
    ),
    project_id: str | None = typer.Option(None, "--project-id", help="Neon project."),
    database: str | None = typer.Option(None, "--database", help="Neon/Dolt database."),
    role: str | None = typer.Option(None, "--role", help="Neon role for connections."),
    branch: str | None = typer.Option(
        None,
        "--branch",
        help="Upstream branch (default main) for branching backends: what `pull` "
        "reads, `promote` lands on, and `direct` writes to.",
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
        "direct: writes land on the base branch itself (no working branch).",
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
        help="Start at this native state (snapshot id, version, commit, or tag) "
        "instead of the branch head: the first commit pins it; `pull` moves on.",
    ),
    pick: bool = typer.Option(
        False,
        "--pick",
        help="List the system's history and choose the base state interactively.",
    ),
) -> None:
    """Register an object in the working copy.

    The positional LOCATOR is stored as the `uri` field; named options set
    other locator fields. The first commit pins the upstream branch head (or
    --at / --pick: a chosen native state); after that the object keeps its pin
    until `tether pull` or a working branch moves it. Nothing is contacted
    until the next status/commit (except --pick, which lists history first).
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


@app.command(name="set")
def set_(
    keys: list[str] | None = typer.Argument(None, help="Objects to change; or --all."),
    write: str | None = typer.Option(
        None,
        "--write",
        help="fork | direct: fork a working branch on `new`, or "
        "write on the base branch.",
    ),
    file: str | None = typer.Option(
        None, "--file", help="immutable | versioned (file objects)."
    ),
    pin: str | None = typer.Option(
        None, "--pin", help="native | record: a native ref per commit, or state only."
    ),
    all_: bool = typer.Option(False, "--all", help="Every registered object."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Change an object's policy in place.

    Manifest-only, logged, undoable. Changing --write lets the workspace go of
    the object's working branch (left for `gc --prune-workspaces`, never
    deleted here); run `tether new` to decide the new one. Commit afterwards
    to record the policy.
    """
    repo = _repo()
    if all_:
        keys = sorted(repo.objects)
    if not keys:
        _fail(TetherError("give one or more KEY, or --all"))
    try:
        report = repo.set_policy(keys, write=write, file=file, pin=pin)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(
            {
                "changed": {
                    k: {f: {"from": a, "to": b} for f, (a, b) in d.items()}
                    for k, d in report.changed.items()
                },
                "unchanged": report.unchanged,
                "released": report.released,
            },
            as_json=True,
        )
        return
    for key, diff in report.changed.items():
        fields = ", ".join(f"{f} {a} -> {b}" for f, (a, b) in diff.items())
        typer.echo(f"  set {key}  {fields}")
    for key in report.unchanged:
        typer.echo(f"  unchanged {key}")
    for key, ref in report.released.items():
        typer.echo(f"  released {key}  {ref} (left for gc --prune-workspaces)")
    if report.released:
        typer.echo("run `tether new` to decide the new working refs")
    if report.changed:
        typer.echo("commit to record the policy")


@app.command()
def pull(
    keys: list[str] | None = typer.Argument(
        None, help="Objects to pull; default: every object."
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Take the current state of objects that have no working branch.

    A committed object nobody here is writing to keeps its pin from commit to
    commit; `pull` is the explicit step that moves it to what is there now --
    the upstream branch head, the table's current version, the files' contents
    (like fetching and rebasing). The new state is held until the next
    `commit` pins it -- `status` shows it as `pulled` -- and a writable `open`
    before then forks from it. Objects with a working branch, `direct`
    objects, and objects not yet committed are skipped with a reason; an
    immutable file that changed is refused. Nothing is written to any store;
    `undo` reverses it.
    """
    repo = _repo()
    try:
        report = repo.pull(keys or None)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(
            {
                "pulled": {
                    k: {"from": a, "to": b} for k, (a, b) in report.pulled.items()
                },
                "up_to_date": report.up_to_date,
                "skipped": report.skipped,
                "retargeted": report.retargeted,
            },
            as_json=True,
        )
        return
    for key, (before, after) in report.pulled.items():
        note = (
            "  (pending working branch will fork from here, not the pin)"
            if key in report.retargeted
            else ""
        )
        typer.echo(
            f"  pulled {key}  {short_state(before)} -> {short_state(after)}{note}"
        )
    for key in report.up_to_date:
        typer.echo(f"  up to date {key}")
    for key, why in report.skipped.items():
        typer.echo(f"  skipped {key}: {why}")
    if not report.pulled and not report.up_to_date:
        typer.echo("nothing to pull")
    elif report.pulled:
        typer.echo("commit to pin the pulled states; `tether undo` puts them back")


@app.command()
def status(
    snapshot: bool | None = typer.Option(
        None,
        "--snapshot/--no-snapshot",
        help="Fingerprint every object now (--snapshot) or show the last snapshot "
        "and its age (--no-snapshot). Default: [snapshot] auto in tether.toml "
        "(off: show the last snapshot).",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Classify every object against its committed manifest.

    Local by default: the manifests, working refs, staleness, and the *last*
    snapshot of each object's state, with its age. `--snapshot` fans out and
    fingerprints every object first (one metadata round-trip per system; a
    suspended Neon compute wakes). A workspace with no snapshot yet takes one.
    Labels: new (never committed), modified, clean, error. `(STALE)` means the
    committed state changed since this workspace forked; run `tether new`.
    """
    repo = _repo()
    try:
        report = repo.status(do_snapshot=_want_snapshot(repo, snapshot))
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(_status_payload(report), as_json=True)
        return
    flag = f" (STALE: {', '.join(report.stale_keys)})" if report.stale else ""
    typer.echo(f"dataset {report.manifest_hash[:12]}{flag}")
    if not report.fresh:
        typer.secho(
            f"  states as fingerprinted {_age(report.snapshot_at)}; "
            "--snapshot to refresh",
            fg=typer.colors.BRIGHT_BLACK,
        )
    for drift in report.vcs_drift:
        typer.secho(f"  warning: {drift.message}", fg=typer.colors.YELLOW, err=True)
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
        False,
        "--no-snapshot",
        help="Commit the cached fingerprints as-is instead of fingerprinting first.",
    ),
    pull: bool | None = typer.Option(
        None,
        "--pull/--no-pull",
        help="Also take the upstream head of every object without a working "
        "branch, as `tether pull` would, and pin it. Default: [commit] pull in "
        "tether.toml (off: such objects keep their pin).",
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
                do_snapshot=not no_snapshot,  # commit always sees the real state
                pull=pull,
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
    eager: bool | None = typer.Option(
        None,
        "--eager/--lazy",
        help="Create every working branch now (--eager) or on the first writable "
        "open (--lazy). Default: [new] fork in tether.toml (lazy).",
    ),
    discard: bool = typer.Option(
        False,
        "--discard",
        help="Reset working branches even if they hold writes that were never "
        "committed (otherwise such a new is refused).",
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
    """Start working on top of REV: set up writable branches off its pins.

    Moves the VCS working copy to REV if given, then decides a
    `tether.ws.<workspace>.<key>` branch per Forkable object. By default the
    branch is created lazily, on the first writable `open` (workspaces that
    never write leave nothing behind); `--eager` creates them all now.
    `pin = "record"` objects always fork now, from their recorded state, so it
    cannot expire underneath them. Track-policy objects stay on their base
    branch. A working branch you already have is reset; if it holds writes you
    never committed, `new` refuses unless you pass `--discard`. `--dry-run` /
    `--plan` preview; `--from-plan` applies a saved plan.
    """
    repo = _repo()
    try:
        if from_plan is not None:
            plan = _load_plan(from_plan, "new")
            repo.apply_new(plan)
        else:
            plan = repo.plan_new(rev, keep=keep, eager=eager, discard=discard)
            if dry_run or plan_out is not None:
                _save_plan(plan, plan_out)
                _show_plan(plan, as_json=json_out)
                return
            repo.apply_new(plan, verify=False)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(
            {
                "working_refs": repo.workspace.working_refs,
                "pending_forks": repo.workspace.pending_forks,
                "fork_points": repo.workspace.fork_points,
            },
            as_json=True,
        )
        return
    typer.echo(
        "kept working refs" if plan.context.get("keep") else "working refs set up"
    )
    for key, ref in sorted(repo.workspace.working_refs.items()):
        typer.echo(f"  {key} -> {ref}")
    for key, ref in sorted(repo.workspace.pending_forks.items()):
        typer.echo(f"  {key} -> {ref}  (created on first writable open)")


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
def ops(
    limit: int = typer.Option(20, "-n", "--limit", help="Show the newest N entries."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show this workspace's operation log (what tether did to the stores).

    Every store-writing command appends an entry: the plan it applied, what it
    created or deleted, and what it replaced. The log is per workspace and
    untracked, like jj's own `op log`. `tether undo ID` reverses an entry
    where the store still allows it.
    """
    repo = _repo()
    entries = repo.ops(limit)
    drifted = {d.op.id for d in repo.vcs_drift()}
    if json_out:
        _emit(
            [{**e.to_dict(), "vcs_commit_gone": e.id in drifted} for e in entries],
            as_json=True,
        )
        return
    if not entries:
        typer.echo("no operations recorded")
        return
    for e in entries:
        flag = ""
        if e.undone_by:
            flag = f"  (undone by {e.undone_by})"
        elif e.id in drifted:
            flag = "  (vcs commit gone)"
        typer.echo(f"{e.id}  {e.at}  {e.command:<8} {e.summary()}{flag}")


@app.command()
def undo(
    op_id: str | None = typer.Argument(
        None, help="Operation id from `tether ops`; default: the newest undoable one."
    ),
    to: str | None = typer.Option(
        None,
        "--to",
        metavar="OP_ID",
        help="Undo every operation newer than OP_ID, newest first (like `jj op "
        "restore`); stops at the first one that cannot be reversed.",
    ),
    discard: bool = typer.Option(
        False,
        "--discard",
        help="Delete or reset branches even if they gained writes since the operation.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Reverse an operation where the stores still allow it.

    commit: uncommit (manifests become working-tree changes; pins stay).
    new/fork: delete the branches it created, re-point the ones it reset,
    restore workspace.toml and the VCS working copy. gc: recreate deleted
    branches and listings; deleted pins are irreversible (see `repair`).
    import/add/remove: restore the manifests. promote: refused, with the
    previous base heads printed. `--to OP_ID` walks back through every newer
    operation. Exit code 2 when part of the work could not be reversed; the
    rest was.
    """
    repo = _repo()
    if to is not None:
        if op_id is not None:
            _fail(TetherError("pass either OP_ID or --to, not both"))
        try:
            walk = repo.undo_to(to, discard=discard)
        except TetherError as exc:
            _fail(exc)
        if json_out:
            _emit(
                {
                    "to": walk.target.id,
                    "undone": [
                        {
                            "op": r.op.id,
                            "command": r.op.command,
                            "undo_id": r.undo_id,
                            "restored": r.restored,
                            "irreversible": r.irreversible,
                            "skipped": r.skipped,
                        }
                        for r in walk.reports
                    ],
                    "stopped_at": walk.stopped_at.id if walk.stopped_at else None,
                    "reason": walk.reason,
                },
                as_json=True,
            )
        else:
            for r in walk.reports:
                typer.echo(f"undid {r.op.id} ({r.op.command}: {r.op.summary()})")
                for line in r.restored:
                    typer.echo(f"  restored   {line}")
                for line in r.irreversible:
                    typer.secho(f"  IRREVERSIBLE {line}", err=True)
            if walk.stopped_at is not None:
                typer.secho(
                    f"stopped at {walk.stopped_at.id} ({walk.stopped_at.command}): "
                    f"{walk.reason}",
                    err=True,
                )
            elif not walk.reports:
                typer.echo(f"nothing newer than {walk.target.id} to undo")
            else:
                typer.echo(f"back to the state after {walk.target.id}")
        if not walk.complete:
            raise typer.Exit(2)
        return
    try:
        report = repo.undo(op_id, discard=discard)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(
            {
                "undone": report.op.id,
                "command": report.op.command,
                "undo_id": report.undo_id,
                "restored": report.restored,
                "irreversible": report.irreversible,
                "skipped": report.skipped,
            },
            as_json=True,
        )
    else:
        typer.echo(f"undid {report.op.id} ({report.op.command}: {report.op.summary()})")
        for line in report.restored:
            typer.echo(f"  restored   {line}")
        for line in report.skipped:
            typer.echo(f"  skipped    {line}")
        for line in report.irreversible:
            typer.secho(f"  IRREVERSIBLE {line}", err=True)
    if not report.complete:
        raise typer.Exit(2)


@app.command()
def repair(
    all_history: bool = typer.Option(
        False, "--all-history", help="Also check the pins of every commit in history."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be rebuilt; write nothing."
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
    """Recreate missing pins and working branches from the manifests.

    A manifest promises "this pin names this state". If the native ref is gone
    (an undone `gc`, a ref deleted by hand) but the state is still reachable in
    the store, `repair` recreates it; a working branch this workspace expects
    but the store lost is forked again from the manifest. Pins that exist but
    point elsewhere are reported, not overwritten. Exit code 2 if something
    could not be rebuilt.
    """
    repo = _repo()
    try:
        if from_plan is not None:
            plan = _load_plan(from_plan, "repair")
        else:
            plan = repo.plan_repair(all_history=all_history)
            if dry_run or plan_out is not None:
                _save_plan(plan, plan_out)
                _show_plan(plan, as_json=json_out)
                return
        report = repo.apply_repair(plan)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(
            {
                "repinned": report.repinned,
                "reforked": report.reforked,
                "failed": report.failed,
                "notes": plan.notes,
            },
            as_json=True,
        )
    else:
        for key, pid in sorted(report.repinned.items()):
            typer.echo(f"repinned  {key} -> {pid}")
        for key, ref in sorted(report.reforked.items()):
            typer.echo(f"reforked  {key} -> {ref}")
        for note in plan.notes:
            typer.echo(f"note      {note}")
        for target, why in sorted(report.failed.items()):
            typer.secho(f"FAILED    {target}: {why}", err=True)
    if report.failed:
        raise typer.Exit(2)


@app.command()
def restore(
    keys: list[str] = typer.Argument(
        ..., help="Objects whose working branch to reset."
    ),
    rev: str = typer.Option(
        ..., "--from", "-f", help="Revision whose pins to restore."
    ),
    discard: bool = typer.Option(
        False, "--discard", help="Reset even if the branch holds uncommitted writes."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the plan; write nothing."
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
    """Reset an object's working branch to what an older commit pinned.

    The per-object `jj restore --from REV`: only KEY's branch moves; the rest of
    the workspace and the manifests stay. The object is not stale afterwards --
    the next `commit` pins what you restored -- and `promote` treats the branch
    as forked from REV. Refused if the branch holds writes you never committed,
    unless `--discard`.
    """
    repo = _repo()
    try:
        if from_plan is not None:
            plan = _load_plan(from_plan, "restore")
            done = repo.apply_restore(plan)
        else:
            plan = repo.plan_restore(keys, rev, discard=discard)
            if dry_run or plan_out is not None:
                _save_plan(plan, plan_out)
                _show_plan(plan, as_json=json_out)
                return
            done = repo.apply_restore(plan, verify=False)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit({"from": rev, "working_refs": done}, as_json=True)
        return
    for key, ref in sorted(done.items()):
        typer.echo(f"{key} -> {ref}  (from {rev})")


@app.command(name="forget-workspace")
def forget_workspace(
    workspace_id: str | None = typer.Argument(
        None, help="Workspace id (full or 8 chars); default: this workspace."
    ),
    force_prune: bool = typer.Option(
        False,
        "--force-prune",
        help="Delete its branches even if they hold unpinned data.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the plan; write nothing."
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
    """Forget a workspace: its branches, its state files, and the VCS checkout.

    `jj workspace forget` / `git worktree remove` plus tether's half in one
    step: the workspace's working branches are deleted under the
    `gc --prune-workspaces` rule (head pinned or equal to the base;
    `--force-prune` for the rest), its `workspace.toml` and `ops.jsonl` are
    removed, and the VCS stops tracking the checkout (git's main worktree is
    left; jj leaves the directory). Forgetting the current workspace means the
    next tether command here starts a fresh one. Exit code 2 if a step failed.
    """
    repo = _repo()
    try:
        if from_plan is not None:
            plan = _load_plan(from_plan, "forget-workspace")
        else:
            plan = repo.plan_forget_workspace(workspace_id, force_prune=force_prune)
            if dry_run or plan_out is not None:
                _save_plan(plan, plan_out)
                _show_plan(plan, as_json=json_out)
                return
        report = repo.apply_forget_workspace(plan)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(
            {
                "workspace": report.workspace,
                "deleted_working_refs": report.deleted_working_refs,
                "kept_working_refs": report.kept_working_refs,
                "removed_files": report.removed_files,
                "vcs": report.vcs,
                "failed": report.failed,
            },
            as_json=True,
        )
    else:
        typer.echo(f"forgot workspace {report.workspace}")
        for key, refs in sorted(report.deleted_working_refs.items()):
            for ref in refs:
                typer.echo(f"  deleted  {key}: {ref}")
        for key, refs in sorted(report.kept_working_refs.items()):
            for ref in refs:
                typer.secho(
                    f"  kept     {key}: {ref} (holds data; --force-prune)", fg="yellow"
                )
        for path in report.removed_files:
            typer.echo(f"  removed  {path}")
        if report.vcs:
            typer.echo(f"  vcs      {report.vcs}")
        for target, why in sorted(report.failed.items()):
            typer.secho(f"  FAILED   {target}: {why}", err=True)
    if report.failed:
        raise typer.Exit(2)


@app.command()
def abandon(
    revs: list[str] = typer.Argument(..., help="Revisions to drop from history."),
    gc: bool = typer.Option(
        False, "--gc", help="Also release the pins only those commits referenced."
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Drop dataset commits from VCS history and see what that frees.

    `jj abandon` / a git rebase, with one difference: later commits keep their
    manifests exactly as they were (a manifest records a whole state, so it
    must not conflict with the removal of an earlier one). Then the `gc` plan:
    pins that only the dropped commits referenced. Without `--gc` nothing is
    released -- run `tether gc --no-dry-run` when ready (undoing the abandon
    in jj/git first brings the pins back into use). Not undoable by tether.
    """
    repo = _repo()
    try:
        report = repo.abandon(revs, gc=gc)
    except TetherError as exc:
        _fail(exc)
    assert report.gc_plan is not None
    if json_out:
        _emit(
            {
                "abandoned": report.abandoned,
                "gc_plan": report.gc_plan.to_dict(),
                "gc_applied": report.gc_report is not None,
            },
            as_json=True,
        )
        return
    typer.echo("abandoned " + ", ".join(c[:12] for c in report.abandoned))
    if report.gc_report is not None:
        _print_gc_report(report.gc_report)
    elif report.gc_plan.is_empty:
        typer.echo("nothing became unreferenced")
    else:
        _show_plan(report.gc_plan, as_json=False)
        typer.echo(
            "(not applied; `tether gc --no-dry-run` releases these, "
            "or re-run with --gc)"
        )


@app.command()
def upgrade(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what the upgrade would do; write nothing."
    ),
    plan_out: Path | None = typer.Option(
        None, "--plan", help="Write the plan to FILE (implies --dry-run)."
    ),
    from_plan: Path | None = typer.Option(
        None,
        "--from-plan",
        help="Apply a plan saved with --plan instead of replanning.",
    ),
    ignore_immutable: bool = typer.Option(
        False,
        "--ignore-immutable",
        help="jj: also rewrite commits jj marks immutable (pushed history).",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Bring a dataset made by an older tether up to this version.

    Runs every pending migration in order (see `tether.migrations`), writing
    the new `[tether] version` after each. When a migration changes how native
    refs are named it renames them in every store and rewrites every historical
    manifest to match, so `gc` keeps seeing the same pins from both sides.
    Rewriting history changes commit ids: every other clone must re-sync
    afterwards. Run `--dry-run` first. A failed store rename stops the upgrade
    before anything else changes (renames already made are skipped on the next
    run); exit code 2 marks a partially applied step.
    """
    repo = _repo(allow_outdated=True)
    try:
        if from_plan is not None:
            plan = _load_plan(from_plan, "upgrade")
        else:
            plan = repo.plan_upgrade(ignore_immutable=ignore_immutable)
            if dry_run or plan_out is not None:
                _save_plan(plan, plan_out)
                _show_plan(plan, as_json=json_out)
                return
        report = repo.apply_upgrade(plan)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(
            {
                "from_version": report.from_version,
                "to_version": report.to_version,
                "renamed_pins": report.renamed_pins,
                "renamed_branches": report.renamed_branches,
                "rewritten_commits": report.rewritten_commits,
                "refingerprinted": report.refingerprinted,
                "rewritten_manifests": report.rewritten_manifests,
                "failed": report.failed,
                "vcs_commit": report.vcs_commit,
            },
            as_json=True,
        )
    else:
        if report.from_version == report.to_version:
            typer.echo(f"already at version {report.to_version}")
        else:
            typer.echo(f"upgraded v{report.from_version} -> v{report.to_version}")
        for old, new in sorted(report.renamed_pins.items()):
            typer.echo(f"  pin     {old} -> {new}")
        for old, new in sorted(report.renamed_branches.items()):
            typer.echo(f"  branch  {old} -> {new}")
        if report.rewritten_commits:
            typer.echo(
                f"  rewrote {len(report.rewritten_commits)} commit(s); other clones "
                "must re-sync"
            )
        for key in report.refingerprinted:
            typer.echo(f"  rehashed {key}")
        for key in report.rewritten_manifests:
            typer.echo(f"  rewrote  {key}")
        if report.vcs_commit:
            typer.echo(f"  commit  {report.vcs_commit[:12]}")
        for target, why in sorted(report.failed.items()):
            typer.secho(f"  FAILED  {target}: {why}", err=True)
    if report.failed:
        raise typer.Exit(2)


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
        help="Extra workspace id (or 8-char prefix) whose branches --prune-workspaces "
        "must keep, e.g. a checkout on another machine; repeatable. Every live jj "
        "workspace / git worktree of this repository is kept automatically.",
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
    `tether.ws.*` branches -- those of workspaces that no longer exist (live jj
    workspaces / git worktrees are found and kept automatically) and this
    one's unused: a branch is deleted only if its head is pinned or equals the
    base head, otherwise kept -- `--force-prune` deletes those too. Dry-run by
    default: pass `--no-dry-run` (or `--from-plan`) to release.
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
    _print_gc_report(report)


def _print_gc_report(report: GcReport) -> None:
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


@app.command()
def promote(
    keys: list[str] = typer.Argument(None, help="Objects to promote (default: all)."),
    rev: str | None = typer.Option(
        None,
        "--rev",
        "-r",
        help="Promote the states pinned at this dataset commit instead of this "
        "workspace's working branches.",
    ),
    strategy: str = typer.Option(
        "auto",
        "--strategy",
        help="auto: fast-forward when the base is unchanged, else merge; "
        "ff: refuse anything but a fast-forward; merge: refuse anything but a merge.",
    ),
    message: str | None = typer.Option(
        None, "-m", "--message", help="Merge message for systems that record one."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show per-object outcomes; write nothing."
    ),
    plan_out: Path | None = typer.Option(
        None, "--plan", help="Write the plan to FILE (implies --dry-run)."
    ),
    from_plan: Path | None = typer.Option(
        None, "--from-plan", help="Apply a plan saved with --plan."
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Move each system's base branch to what this workspace's fork holds.

    The dataset commit already pins the fork's state, so nothing needs
    re-committing after a fast-forward; `promote` makes `main` in each system
    point at it too. Base unchanged since the fork -> fast-forward. Base moved
    -> native 3-way merge where the system has one (lakeFS, Dolt, git),
    otherwise refused with the system's own recipe. Then land the dataset
    commit with jj/git.
    """
    repo = _repo()
    try:
        if from_plan is not None:
            plan = _load_plan(from_plan, "promote")
            report: PromoteReport = repo.apply_promote(plan)
        else:
            plan = repo.plan_promote(
                keys or None, rev=rev, strategy=strategy, message=message
            )
            if dry_run or plan_out is not None:
                _save_plan(plan, plan_out)
                _show_plan(plan, as_json=json_out)
                return
            report = repo.apply_promote(plan, verify=False)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(
            {
                "fast_forwarded": report.fast_forwarded,
                "merged": report.merged,
                "skipped": report.skipped,
                "refused": report.refused,
                "conflicts": report.conflicts,
            },
            as_json=True,
        )
        if report.refused:
            raise typer.Exit(1)
        return
    for key, state in report.fast_forwarded.items():
        typer.echo(f"  fast-forwarded {key} -> {state}")
    for key, state in report.merged.items():
        typer.echo(f"  merged         {key} -> {state}")
    for key in report.skipped:
        typer.echo(f"  skipped        {key}")
    for key, why in report.refused.items():
        typer.secho(f"  refused        {key}: {why}", fg=typer.colors.YELLOW)
        for unit in report.conflicts.get(key, []):
            typer.echo(f"                   conflict: {unit}")
    if report.merged:
        typer.echo("run `tether commit` to pin the merge result(s)")
    if report.refused:
        raise typer.Exit(1)


# --------------------------------------------------------------------------- #
# Registries: export / publish / import
# --------------------------------------------------------------------------- #
_EXPORT_FORMATS = ("sqlite", "parquet", "csv", "jsonl")


def _echo_counts(counts: dict[str, int]) -> None:
    width = max((len(k) for k in counts), default=0)
    for name, n in counts.items():
        typer.echo(f"  {name:<{width}}  {n:>7} row(s)")


@app.command()
def export(
    path: Path = typer.Argument(
        ..., help="Output: a .sqlite file, or a directory for parquet/csv/jsonl."
    ),
    rev: list[str] = typer.Option(
        [], "--rev", "-r", help="Export only these revisions (repeatable)."
    ),
    all_history: bool = typer.Option(
        True,
        "--all-history/--no-all-history",
        help="Export every reachable commit (default when no --rev is given).",
    ),
    fmt: str = typer.Option(
        "sqlite", "--format", help="sqlite (default), parquet, csv, or jsonl."
    ),
    listings: bool = typer.Option(
        False,
        "--listings",
        help="Include per-file listings (listings, listing_entries).",
    ),
    workspace: bool = typer.Option(
        False, "--workspace", help="Include this checkout's working refs and snapshot."
    ),
    append: bool = typer.Option(
        False,
        "--append",
        help="sqlite: upsert into an existing file instead of replacing it.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """[experimental] Write tether history as relational tables for registries.

    Tables: commits, commit_parents, refs, objects, object_states, plus
    listings / listing_entries (--listings) and workspace (--workspace); see
    the Registries guide for the schema. Query the SQLite file directly or
    `ATTACH` it in DuckDB; the parquet/csv/jsonl directories get one file per
    table and a schema.json.
    """
    if fmt not in _EXPORT_FORMATS:
        _fail(TetherError(f"--format must be one of {', '.join(_EXPORT_FORMATS)}"))
    if not rev and not all_history:
        rev = ["@"] if _repo().vcs.kind == "jj" else ["HEAD"]
    repo = _repo()
    try:
        bundle = repo.export(rev or None, listings=listings, workspace=workspace)
        if fmt == "sqlite":
            written = [bundle.to_sqlite(path, append=append)]
        else:
            written = bundle.to_dir(path, fmt)
    except TetherError as exc:
        _fail(exc)
    counts = bundle.row_counts()
    if json_out:
        _emit(
            {"path": str(path), "format": fmt, "head": bundle.head, "rows": counts},
            as_json=True,
        )
        return
    typer.echo(f"exported {len(written)} file(s) to {path} ({fmt})")
    _echo_counts(counts)


@app.command()
def publish(
    to: str | None = typer.Option(
        None,
        "--to",
        help="Postgres DSN (postgresql://...). Defaults to $TETHER_PUBLISH_DSN.",
        envvar="TETHER_PUBLISH_DSN",
        show_envvar=True,
    ),
    schema: str = typer.Option("tether", "--schema", help="Target schema name."),
    rev: list[str] = typer.Option(
        [], "--rev", "-r", help="Publish only these revisions (repeatable)."
    ),
    listings: bool = typer.Option(
        False,
        "--listings",
        help="Include per-file listings (listings, listing_entries).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be written per table; write nothing."
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """[experimental] Upsert the export tables into a Postgres schema.

    Idempotent and incremental: commits already present are skipped, `refs`
    and `tether_meta` are refreshed. Run it after each `tether commit` (or from
    CI) so a registry can join `objects.uri` / `objects.commit_id` against its
    own tables. The DSN never comes from tether.toml.
    """
    if not to:
        _fail(TetherError("a Postgres DSN is required: --to or $TETHER_PUBLISH_DSN"))
    repo = _repo()
    try:
        bundle = repo.export(rev or None, listings=listings)
        report = bundle.to_postgres(to, schema=schema, dry_run=dry_run)
    except TetherError as exc:
        _fail(exc)
    if dry_run:
        plan = Plan(command="publish", context={"schema": schema, "head": bundle.head})
        for name, n in report.upserted.items():
            if n:
                plan.actions.append(
                    Action("upsert", target=f"{schema}.{name}", detail=f"{n} row(s)")
                )
        if report.skipped_commits:
            plan.notes.append(f"{report.skipped_commits} commit(s) already published")
        _show_plan(plan, as_json=json_out)
        return
    if json_out:
        _emit(
            {
                "schema": schema,
                "head": bundle.head,
                "upserted": report.upserted,
                "skipped_commits": report.skipped_commits,
            },
            as_json=True,
        )
        return
    total = sum(report.upserted.values())
    typer.echo(
        f"published {total} row(s) to schema {schema!r} "
        f"({report.skipped_commits} commit(s) already present)"
    )
    _echo_counts(report.upserted)


@app.command(name="import")
def import_(
    source: str = typer.Argument(
        ...,
        help="Postgres DSN, SQLite file, .csv, or .jsonl with the canonical columns.",
    ),
    table: str | None = typer.Option(
        None, "--table", help="SQL sources: read every row of this table."
    ),
    query: str | None = typer.Option(
        None,
        "--query",
        help="SQL sources: run this query (must yield key, kind, and locator columns). "
        "Defaults to [import] query in tether.toml.",
    ),
    sync: bool = typer.Option(
        False, "--sync", help="Also remove registered objects the source does not list."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the add/update/remove plan; write nothing."
    ),
    plan_out: Path | None = typer.Option(
        None, "--plan", help="Write the plan to FILE (implies --dry-run)."
    ),
    from_plan: Path | None = typer.Option(
        None,
        "--from-plan",
        help="Apply a plan saved with --plan instead of re-reading.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """[experimental] Register or sync objects from a registry query.

    Rows carry `key`, `kind`, and any of `uri`, `locator_json`, `policy_write`,
    `policy_file`, `policy_pin`, `at`; write the mapping from your registry's
    columns in SQL. Import changes what is tracked (manifests only) -- run
    `tether commit` afterwards to record states. `--sync` removes objects the
    source no longer lists.
    """
    from tether.registry import read_source, specs_from_rows

    repo = _repo()
    try:
        if from_plan is not None:
            plan = _load_plan(from_plan, "import")
            report = repo.apply_import(plan)
        else:
            if table is None and query is None:
                query = repo.config.import_query
            rows = read_source(source, table=table, query=query)
            specs, notes = specs_from_rows(rows, repo.config.defaults)
            plan = repo.plan_import(specs, sync=sync, notes=notes)
            if dry_run or plan_out is not None:
                _save_plan(plan, plan_out)
                _show_plan(plan, as_json=json_out)
                return
            report = repo.apply_import(plan, verify=False)
    except (TetherError, OSError, ValueError) as exc:
        _fail(exc)
    if json_out:
        _emit(
            {
                "added": report.added,
                "updated": report.updated,
                "removed": report.removed,
                "unchanged": report.unchanged,
            },
            as_json=True,
        )
        return
    typer.echo(
        f"added {len(report.added)}, updated {len(report.updated)}, "
        f"removed {len(report.removed)}, unchanged {len(report.unchanged)}"
    )
    for key in report.added:
        typer.echo(f"  added   {key}")
    for key in report.updated:
        typer.echo(f"  updated {key}")
    for key in report.removed:
        typer.secho(f"  removed {key}", fg=typer.colors.YELLOW)
    if report.added or report.updated:
        typer.echo("run `tether commit` to record their states")


def main() -> None:
    try:
        app()
    except TetherError as exc:  # pragma: no cover - defensive top-level
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
