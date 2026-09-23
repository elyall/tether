"""Typer command-line interface for tether.

Install with the ``cli`` extra (``pip install tether-vcs[cli]``). Every command that
inspects external systems fans out concurrently; ``--json`` emits
machine-readable output for agents and scripts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, NoReturn

try:
    import typer
    from typer.core import TyperCommand, TyperOption
except ImportError as exc:  # pragma: no cover - optional dep
    raise SystemExit(
        "the tether CLI requires the 'cli' extra: pip install tether-vcs[cli]"
    ) from exc

from tether.backends.base import Capability, HistoryEntry, tier_of
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
    LanceHandle,
    NeonHandle,
)
from tether.manifest import Policy
from tether.plan import Plan
from tether.repo import (
    CommitResult,
    DropReport,
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
    # Rich markup would take `[snapshot]` or `[experimental]` for style tags.
    rich_markup_mode=None,
)

PARTIAL = 3
"""Exit status when a command did part of its job and a store refused the
rest. Not 2: Click exits 2 on a usage error."""


def _show_version(value: bool) -> None:
    if value:
        from tether import __version__

        typer.echo(f"tether {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        help="Print the installed tether-vcs version and exit.",
        callback=_show_version,
        is_eager=True,
    ),
) -> None:
    """jj-style version control for heterogeneous datasets."""


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


def _experimental_note(what: str) -> None:
    """The one-line caveat every experimental surface prints, on stderr."""
    typer.secho(f"note: {what}", fg=typer.colors.YELLOW, err=True)


def _handle_address(handle: Handle, *, with_password: bool = False) -> str:
    if isinstance(handle, FileHandle):
        return handle.uri + (f"#{handle.version_id}" if handle.version_id else "")
    if isinstance(handle, NeonHandle):
        # Shell history and CI logs keep stdout; the full URL is opt-in.
        return handle.url if with_password else handle.redacted_url
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
        "bookmark": report.bookmark,
        "trunk": report.trunk,
        "bookmark_drift": report.bookmark_drift,
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
                "origin": o.origin,
                "verify": o.verify.status.value if o.verify else None,
                "error": o.error,
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
    if json_out:
        _emit(
            {"root": str(repo.root), "dataset_id": repo.config.dataset_id},
            as_json=True,
        )
        return
    typer.echo(
        f"initialized tether dataset at {repo.root} (dataset id "
        f"{repo.config.dataset_id}: its pins and working branches are "
        f"tether.{repo.config.dataset_id}.* / tether.ws.{repo.config.dataset_id}.*)"
    )


def _kind_help() -> str:
    """`add --kind` help, each kind's maturity read from its class."""
    from tether.backends.base import build_backend, known_kinds

    by_maturity: dict[str, list[str]] = {}
    for kind in known_kinds():
        if kind == "memory":  # the in-process reference backend, for tests
            continue
        try:
            maturity = build_backend(kind).MATURITY
        except TetherError:
            maturity = "not installed"
        by_maturity.setdefault(maturity, []).append(kind)
    stable = ", ".join(by_maturity.pop("stable", []))
    rest = "".join(f"; {m}: {', '.join(k)}" for m, k in by_maturity.items())
    return f"Backend kind: {stable}{rest}."


class _AddCommand(TyperCommand):
    """`add`, whose `--kind` help names each kind's maturity. Reading it
    imports every backend module, so it happens only when help is shown."""

    def resolve_help(self) -> None:
        for param in self.params:
            if param.name == "kind" and isinstance(param, TyperOption):
                param.help = _kind_help()

    def format_help(self, ctx: Any, formatter: Any) -> None:
        self.resolve_help()
        super().format_help(ctx, formatter)


@app.command(cls=_AddCommand)
def add(
    key: str = typer.Argument(..., help="Object key (may contain '/')."),
    locator: str | None = typer.Argument(None, help="Primary locator (uri / path)."),
    kind: str = typer.Option(
        ...,
        "--kind",
        help="Backend kind; `tether backends` lists each with its maturity.",
    ),
    project_id: str | None = typer.Option(None, "--project-id", help="Neon project."),
    database: str | None = typer.Option(None, "--database", help="Neon/Dolt database."),
    role: str | None = typer.Option(None, "--role", help="Neon role for connections."),
    branch: str | None = typer.Option(
        None,
        "--branch",
        help="Upstream branch (default main) for branching backends: what the trunk "
        "bookmark stands for -- `pull` reads it, `promote` lands on it.",
    ),
    remote: str | None = typer.Option(
        None, "--remote", help="git remote to push pins to."
    ),
    region: str | None = typer.Option(None, "--region", help="Object-store region."),
    host: str | None = typer.Option(None, "--host", help="Dolt server host."),
    port: int | None = typer.Option(None, "--port", help="Dolt server port."),
    table: str | None = typer.Option(None, "--table", help="DuckLake table scope."),
    set_: list[str] = typer.Option(
        [], "--set", help="Extra locator field key=value (repeatable)."
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
        "instead of the branch head: the first commit pins it; a later `pull` on "
        "the trunk moves on.",
    ),
    create: bool = typer.Option(
        False,
        "--create",
        help="Make an empty store at the locator first (backends with CREATE) and "
        "mark it as this dataset's; `gc --delete-stores` removes it again once "
        "nothing references it. Refused if anything already exists there. On a "
        "bookmark the store's working branch is forked at once (no `new` needed).",
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
        policy = Policy.from_dict({"file": file, "pin": pin})
        repo.add(key, kind, loc, policy=policy, create=create)
    except TetherError as exc:
        _fail(exc)
    suffix = f" at {loc['at']}" if "at" in loc else ""
    made = ", created" if create else ""
    typer.echo(f"added {key} ({kind}{made}){suffix}")
    if create:
        _experimental_note(
            "the store lifecycle (`--create`, `gc --delete-stores`) is experimental: "
            "deleting a store has no `repair`; see the reclaiming-storage guide"
        )
    if repo.backend_for(kind).MATURITY != "stable":
        _experimental_note(
            f"the {kind} backend is {repo.backend_for(kind).MATURITY}: tested "
            "against a fake of the service, not the service itself (`tether "
            "backends`)"
        )


@app.command()
def backends(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List backend kinds with their maturity, tier, and capabilities."""
    from tether.backends.base import build_backend, known_kinds

    rows: list[dict[str, Any]] = []
    for kind in known_kinds():
        try:
            backend = build_backend(kind)
        except TetherError as exc:
            rows.append(
                {
                    "kind": kind,
                    "installed": False,
                    "maturity": None,
                    "tier": None,
                    "capabilities": [],
                    "note": str(exc),
                }
            )
            continue
        rows.append(
            {
                "kind": kind,
                "installed": True,
                "maturity": backend.MATURITY,
                "tier": tier_of(backend.capabilities).name.lower(),
                "capabilities": [
                    c.name.lower() for c in Capability if c in backend.capabilities
                ],
                "note": None,
            }
        )
    if as_json:
        _emit(rows, as_json=True)
        return
    for r in rows:
        if not r["installed"]:
            typer.echo(f"{r['kind']:<10} not installed")
            continue
        caps = ", ".join(str(c) for c in r["capabilities"])
        typer.echo(f"{r['kind']:<10} {r['maturity']:<13} {r['tier']:<11} {caps}")


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

    Manifest-only, logged, undoable. Commit afterwards to record the policy.
    Whether writes fork or land upstream is not a policy: it is the bookmark
    you are on (`tether new`).
    """
    repo = _repo()
    if all_:
        keys = sorted(repo.objects)
    if not keys:
        _fail(TetherError("give one or more KEY, or --all"))
    try:
        report = repo.set_policy(keys, file=file, pin=pin)
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
            },
            as_json=True,
        )
        return
    for key, diff in report.changed.items():
        fields = ", ".join(f"{f} {a} -> {b}" for f, (a, b) in diff.items())
        typer.echo(f"  set {key}  {fields}")
    for key in report.unchanged:
        typer.echo(f"  unchanged {key}")
    if report.changed:
        typer.echo("commit to record the policy")


@app.command()
def pull(
    bookmark: str | None = typer.Argument(
        None, help="The bookmark to pull; default and only choice: the one you are on."
    ),
    message: str | None = typer.Option(
        None, "-m", "--message", help="Commit message (default: pull B: N objects)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Fetch the heads of this bookmark's branches and commit them onto it.

    The dataset's `git fetch` + rebase. On `main` (the trunk) that is every
    object's upstream branch -- and, for systems without branches, the object
    itself; a table's current version, a directory's contents. What differs
    from the bookmark's commit is pinned and committed on the bookmark, which
    moves; the working copy ends up on top. Nothing moved: no commit. An
    immutable file that changed is refused. `undo` uncommits it (pins stay).
    """
    repo = _repo()
    try:
        report = repo.pull(bookmark, message=message)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(
            {
                "bookmark": report.bookmark,
                "committed": {
                    k: {"from": a, "to": b} for k, (a, b) in report.committed.items()
                },
                "unchanged": report.unchanged,
                "skipped": report.skipped,
                "vcs_commit": report.vcs_commit,
                "pinned": {k: (p.id if p else None) for k, p in report.pinned.items()},
            },
            as_json=True,
        )
        return
    for key, (before, after) in report.committed.items():
        typer.echo(f"  pulled {key}  {short_state(before)} -> {short_state(after)}")
    for key in report.unchanged:
        typer.echo(f"  up to date {key}")
    for key, why in report.skipped.items():
        typer.echo(f"  skipped {key}: {why}")
    if report.vcs_commit:
        typer.echo(f"committed {report.vcs_commit[:12]} on {report.bookmark}")
    else:
        typer.echo(f"{report.bookmark} is up to date")


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
    Labels: new (never committed), modified, clean, error (its store could not
    be read; exit 1). `(STALE)` means the committed state changed since this
    workspace forked; run `tether new`.
    """
    repo = _repo()
    try:
        report = repo.status(do_snapshot=_want_snapshot(repo, snapshot))
    except TetherError as exc:
        _fail(exc)
    failed = [o for o in report.objects if o.error is not None]
    if json_out:
        _emit(_status_payload(report), as_json=True)
        if failed:
            raise typer.Exit(1)
        return
    flag = f" (STALE: {', '.join(report.stale_keys)})" if report.stale else ""
    if report.bookmark is None:
        where = "on no bookmark: read-only (`tether new -b NAME` to write)"
    else:
        where = f"on {'trunk ' if report.trunk else ''}bookmark {report.bookmark}"
    typer.echo(f"dataset {report.manifest_hash[:12]}{flag}; {where}")
    for line in report.bookmark_drift:
        typer.secho(f"  warning: {line}", fg=typer.colors.YELLOW, err=True)
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
        made = " (created)" if o.origin == "created" else ""
        typer.echo(
            f"  {o.state_label:>9}  {o.key}  [{o.kind}/{o.tier.value}]{made}{rec}{v}"
        )
    for o in failed:
        typer.secho(f"error: {o.key}: {o.error}", fg=typer.colors.RED, err=True)
    if failed:
        raise typer.Exit(1)


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


def _save_plan(repo: Repo, plan: Plan, path: Path | None) -> None:
    if path is not None:
        # The plan binds to the checkout's id; a later `--from-plan` finds it
        # only if the file that holds it exists.
        repo.require_persisted_workspace()
        path.write_text(plan.to_json())
        typer.secho(f"plan written to {path}", err=True)


def _refuse_preview_with_apply(
    dry_run: bool | None, plan_out: Path | None, from_plan: Path | None
) -> None:
    if from_plan is not None and (dry_run or plan_out is not None):
        _fail(
            TetherError(
                "--from-plan applies a saved plan; it cannot be combined with "
                "--dry-run or --plan"
            )
        )


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
    _refuse_preview_with_apply(dry_run, plan_out, from_plan)
    repo = _repo()
    try:
        if from_plan is not None:
            plan = _load_plan(from_plan, "commit")
            if message is not None:
                plan.context["message"] = message
            result: CommitResult = repo.apply_commit(plan, vcs=not no_vcs)
        elif dry_run or plan_out is not None:
            if message is None:
                _fail(TetherError("a message is required: -m/--message"))
            plan = repo.plan_commit(
                message,
                strict=strict,
                force=force,
                do_snapshot=not no_snapshot,  # commit always sees the real state
            )
            _save_plan(repo, plan, plan_out)
            _show_plan(plan, as_json=json_out)
            return
        else:
            if message is None:
                _fail(TetherError("a message is required: -m/--message"))
            # Plan and apply under one lock (Repo.commit): planning outside it
            # and applying with the refreshed manifests is a window in which a
            # concurrent `remove` turns into an error mid-commit.
            result = repo.commit(
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
    rev: str | None = typer.Argument(
        None, help="Bookmark to work on, or a revision to start -b NAME from."
    ),
    bookmark: str | None = typer.Option(
        None,
        "-b",
        "--bookmark",
        help="Create this bookmark at REV (default: here) and work on it: one store "
        "branch per Forkable object, named after it.",
    ),
    shared: bool = typer.Option(
        False,
        "--shared",
        help="Work on a bookmark another live checkout already holds (both then "
        "write the same store branches).",
    ),
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
    """Start working on a bookmark: its store branches become your working refs.

    `tether new -b NAME [REV]` creates bookmark NAME (at REV, default here) and
    forks a `tether.ws.<dataset>.NAME` branch per Forkable object off its
    pins -- lazily, on the first writable `open`, or now with `--eager`.
    `tether new NAME` joins an existing bookmark; `tether new main` (the
    trunk) writes straight to every object's upstream branch; `tether new REV`
    with no bookmark there is read-only. A bookmark another live checkout holds
    is refused unless `--shared`. A working branch that holds writes you never
    committed is not reset unless `--discard`. `--dry-run` / `--plan` preview;
    `--from-plan` applies a saved plan.
    """
    _refuse_preview_with_apply(dry_run, plan_out, from_plan)
    repo = _repo()
    try:
        if from_plan is not None:
            plan = _load_plan(from_plan, "new")
            repo.apply_new(plan)
        else:
            plan = repo.plan_new(
                rev,
                bookmark=bookmark,
                shared=shared,
                keep=keep,
                eager=eager,
                discard=discard,
            )
            if dry_run or plan_out is not None:
                _save_plan(repo, plan, plan_out)
                _show_plan(plan, as_json=json_out)
                return
            repo.apply_new(plan)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(
            {
                "bookmark": repo.workspace.bookmark,
                "working_refs": repo.workspace.working_refs,
                "pending_forks": repo.workspace.pending_forks,
                "fork_points": repo.workspace.fork_points,
            },
            as_json=True,
        )
        return
    if repo.workspace.bookmark is None:
        typer.echo("on no bookmark: read-only (`tether new -b NAME` to write)")
        return
    where = "trunk " if repo.on_trunk() else ""
    verb = "kept working refs" if plan.context.get("keep") else "working refs set up"
    typer.echo(f"on {where}bookmark {repo.workspace.bookmark}; {verb}")
    for key, ref in sorted(repo.workspace.working_refs.items()):
        if key in repo.objects:
            typer.echo(f"  {key} -> {ref}")
        else:
            typer.echo(f"  {key} -> {ref}  (not at this revision; branch kept for gc)")
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
    with_password: bool = typer.Option(
        False,
        "--with-password",
        help="Print a connection URL with its password (Neon); by default the "
        "password is redacted. Never honoured under --json.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Print a native handle for an object (address on stdout).

    Without --rev, Forkable objects open writable at their working ref and
    everything else read-only at the base. A connection URL is printed with
    its password redacted unless --with-password (for `psql "$(tether open
    db --with-password)"`); --json is always redacted.
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
    typer.echo(_handle_address(handle, with_password=with_password))


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
    rev_a: str | None = typer.Argument(
        None, help="From revision (default: the last commit, HEAD or jj's @-)."
    ),
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
                    "why": list(e.why),
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
        elif e.why and "state" not in e.why:
            line += f"  [{' and '.join(e.why)} changed; same state]"
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
        if e.incomplete:
            flag = "  (INCOMPLETE: never finished; see `tether repair --dry-run`)"
        elif e.undone_by:
            flag = f"  (undone by {e.undone_by})"
        elif e.id in drifted:
            flag = "  (vcs commit gone)"
        if e.parent:
            flag += f"  (step of {e.parent})"
        typer.echo(f"{e.id}  {e.at}  {e.command:<8} {e.summary()}{flag}")


@app.command()
def undo(
    op_id: str | None = typer.Argument(
        None,
        help="Operation id from `tether ops`; default: the newest operation "
        "(refused, not skipped, when it cannot be undone).",
    ),
    discard: bool = typer.Option(
        False,
        "--discard",
        help="Delete the branches the operation created even if they gained writes.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Reverse an operation where the stores still allow it.

    commit/pull: uncommit (manifests become working-tree changes; pins stay).
    new/fork: delete the branches it *created* and restore workspace.toml and
    the VCS working copy; branches it reset are reported with their old head
    (`restore` / `new --discard` put them where you want). gc: restore the
    forgotten working refs and listings; deleted branches and pins are
    irreversible (see `repair`). import/add/remove/set: restore the manifests.
    promote: refused, with the previous base heads printed. drop, and the
    `new` and `gc` it runs: refused (the VCS's undo brings the commits back,
    `repair` the branches and pins). An older operation, named by id, gets
    back only the workspace fields it changed that no later operation changed
    again; the rest are listed as skipped. Exit code 3 when part of the work
    could not be reversed; the rest was.
    """
    repo = _repo()
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
        raise typer.Exit(PARTIAL)


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
    point elsewhere are reported, not overwritten. Exit code 3 if something
    could not be rebuilt.
    """
    _refuse_preview_with_apply(dry_run, plan_out, from_plan)
    repo = _repo()
    try:
        if from_plan is not None:
            plan = _load_plan(from_plan, "repair")
        else:
            plan = repo.plan_repair(all_history=all_history)
            if dry_run or plan_out is not None:
                _save_plan(repo, plan, plan_out)
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
        raise typer.Exit(PARTIAL)


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
    _refuse_preview_with_apply(dry_run, plan_out, from_plan)
    repo = _repo()
    try:
        if from_plan is not None:
            plan = _load_plan(from_plan, "restore")
            done = repo.apply_restore(plan)
        else:
            plan = repo.plan_restore(keys, rev, discard=discard)
            if dry_run or plan_out is not None:
                _save_plan(repo, plan, plan_out)
                _show_plan(plan, as_json=json_out)
                return
            done = repo.apply_restore(plan)
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
    """Forget a workspace: its state files and the VCS checkout.

    `jj workspace forget` / `git worktree remove` plus tether's half in one
    step: the workspace's `workspace.toml` and `ops.jsonl` are removed and the
    VCS stops tracking the checkout (git's main worktree is left; jj leaves
    the directory). Store branches belong to bookmarks, not workspaces, so
    none are touched -- delete the bookmark and `gc --prune-bookmarks` for
    that. Forgetting the current workspace means the next tether command here
    starts a fresh one. Exit code 3 if a step failed.
    """
    _refuse_preview_with_apply(dry_run, plan_out, from_plan)
    repo = _repo()
    try:
        if from_plan is not None:
            plan = _load_plan(from_plan, "forget-workspace")
        else:
            plan = repo.plan_forget_workspace(workspace_id)
            if dry_run or plan_out is not None:
                _save_plan(repo, plan, plan_out)
                _show_plan(plan, as_json=json_out)
                return
        report = repo.apply_forget_workspace(plan)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(
            {
                "workspace": report.workspace,
                "removed_files": report.removed_files,
                "vcs": report.vcs,
                "failed": report.failed,
            },
            as_json=True,
        )
    else:
        typer.echo(f"forgot workspace {report.workspace}")
        for path in report.removed_files:
            typer.echo(f"  removed  {path}")
        if report.vcs:
            typer.echo(f"  vcs      {report.vcs}")
        for target, why in sorted(report.failed.items()):
            typer.secho(f"  FAILED   {target}: {why}", err=True)
    if report.failed:
        raise typer.Exit(PARTIAL)


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
def drop(
    bookmark: str = typer.Argument(..., help="The bookmark to throw away."),
    to: str | None = typer.Option(
        None,
        "--to",
        help="Where to leave for when this checkout is on the bookmark "
        "(default: the trunk).",
    ),
    delete_stores: bool = typer.Option(
        False,
        "--delete-stores",
        help="Also reclaim stores created on it (experimental; see `gc`).",
    ),
    force_prune: bool = typer.Option(
        False,
        "--force-prune",
        help="Delete its branches even when they hold unpinned writes. Data on "
        "them is lost.",
    ),
    dry_run: bool | None = typer.Option(
        None, "--dry-run/--no-dry-run", help="Only show the plan (default)."
    ),
    plan_out: Path | None = typer.Option(
        None, "--plan", help="Write the plan to FILE (implies --dry-run)."
    ),
    from_plan: Path | None = typer.Option(
        None, "--from-plan", help="Apply a plan saved with --plan."
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Throw a bookmark away: its commits, the bookmark, and its store leftovers.

    The opposite of `promote`, in one step: leave the bookmark if this checkout
    is on it (`--to`, default the trunk), drop the commits only it reaches,
    delete it, then release what it held in the stores -- `gc
    --prune-bookmarks` for this bookmark alone, under its rules (a branch with
    unpinned writes is kept unless `--force-prune`; `--delete-stores` adds the
    experimental created-store step). Refused for the trunk and for a
    bookmark another live checkout works on. Dry-run by default: the plan
    shows the store side as it will be once the commits are gone. Not undoable
    by tether (`jj undo` / the reflog bring the commits back; `repair` the pins
    and branches).
    """
    _refuse_preview_with_apply(dry_run, plan_out, from_plan)
    if delete_stores and not json_out:
        _experimental_note(
            "reclaiming created stores is experimental: `delete-store` has no "
            "`repair`; read the plan"
        )
    repo = _repo()
    try:
        if from_plan is not None:
            plan = _load_plan(from_plan, "drop")
            report: DropReport = repo.apply_drop(plan)
        else:
            plan = repo.plan_drop(
                bookmark, to=to, delete_stores=delete_stores, force_prune=force_prune
            )
            if dry_run is not False or plan_out is not None:
                _save_plan(repo, plan, plan_out)
                _show_plan(plan, as_json=json_out)
                if not json_out:
                    typer.echo("(not applied; `--no-dry-run` drops it)")
                return
            report = repo.apply_drop(plan)
    except TetherError as exc:
        _fail(exc)
    gc = report.gc_report
    if json_out:
        _emit(
            {
                "bookmark": report.bookmark,
                "left_for": report.left_for,
                "abandoned": report.abandoned,
                "gc": {
                    "unpinned": gc.unpinned,
                    "deleted_working_refs": gc.deleted_working_refs,
                    "kept_working_refs": gc.kept_working_refs,
                    "forgotten_working_refs": gc.forgotten_working_refs,
                    "deleted_listings": gc.deleted_listings,
                    "deleted_stores": gc.deleted_stores,
                    "kept_stores": gc.kept_stores,
                    "forgotten_stores": gc.forgotten_stores,
                }
                if gc is not None
                else None,
            },
            as_json=True,
        )
        return
    if report.left_for:
        typer.echo(f"left {report.bookmark} for {report.left_for}")
    n = len(report.abandoned)
    typer.echo(
        f"dropped {report.bookmark}: {n} commit(s)"
        + (" (" + ", ".join(c[:12] for c in report.abandoned) + ")" if n else "")
    )
    if gc is not None:
        _print_gc_report(gc)


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

    One migration (see `tether.upgrade`) whose parts run on what the dataset
    shows, not on the version it recorded; the new `[tether] version` is
    written once at the end. Where native refs predate the dataset namespace it
    renames them in every store and rewrites every historical manifest to
    match, so `gc` keeps seeing the same pins from both sides. Rewriting
    history changes commit ids: every other clone must re-sync afterwards. Run
    `--dry-run` first. A failed store rename stops the upgrade before anything
    else changes (renames already made are skipped on the next run); exit code
    3 marks a partially applied step.
    """
    _refuse_preview_with_apply(dry_run, plan_out, from_plan)
    repo = _repo(allow_outdated=True)
    try:
        if from_plan is not None:
            plan = _load_plan(from_plan, "upgrade")
        else:
            plan = repo.plan_upgrade(ignore_immutable=ignore_immutable)
            if dry_run or plan_out is not None:
                _save_plan(repo, plan, plan_out)
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
                "rewritten_indexes": report.rewritten_indexes,
                "copied_listings": report.copied_listings,
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
        for name, n in sorted(report.rewritten_indexes.items()):
            typer.echo(f"  index   {name}: {n} record(s)")
        if report.copied_listings:
            typer.echo(
                f"  listings {len(report.copied_listings)} stored under new names"
            )
        if report.vcs_commit:
            typer.echo(f"  commit  {report.vcs_commit[:12]}")
        for target, why in sorted(report.failed.items()):
            typer.secho(f"  FAILED  {target}: {why}", err=True)
    if report.failed:
        raise typer.Exit(PARTIAL)


@app.command()
def gc(
    dry_run: bool | None = typer.Option(
        None, "--dry-run/--no-dry-run", help="Show the plan (default) or apply it."
    ),
    prune_bookmarks: bool = typer.Option(
        False,
        "--prune-bookmarks",
        help="Also delete `tether.ws.*` branches of bookmarks that are gone, legacy "
        "per-workspace branches, and this workspace's unused ones.",
    ),
    keep_bookmark: list[str] = typer.Option(
        [],
        "--keep-bookmark",
        help="Bookmark whose branches --prune-bookmarks must keep although the VCS "
        "here does not have it (e.g. it lives on another machine); repeatable. "
        "Bookmarks the VCS has, or a live checkout works on, are kept automatically.",
    ),
    force_prune: bool = typer.Option(
        False,
        "--force-prune",
        help="With --prune-bookmarks: delete stray branches even when they hold "
        "unpinned writes, a pin-less recorded state, or are the storage itself "
        "(Neon). Data on them is lost.",
    ),
    delete_stores: bool = typer.Option(
        False,
        "--delete-stores",
        help="Also reclaim stores this dataset created (`add --create`) that "
        "nothing references any more: their pins and branches are released and, "
        "when only tether's own refs remained, the store is deleted. Irreversible; "
        "fetch every bookmark first.",
    ),
    store: list[str] = typer.Option(
        [],
        "--store",
        help="KIND=LOCATOR (repeatable): a created store to consider even when this "
        "clone's index does not have it -- its creator's clone is gone. Same rules "
        "(the owner marker in the store must name this dataset); implies "
        "--delete-stores.",
    ),
    release_foreign: bool = typer.Option(
        False,
        "--release-foreign",
        help="Also release unreferenced pins this clone did not create. By default "
        "they are kept and listed (`keep-pin`): another clone's commits, not "
        "fetched yet, may name them.",
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

    Only pins this clone created are released; any other unreferenced pin is
    kept and listed as `keep-pin` (`--release-foreign` releases those too).
    Also forgets this workspace's refs for removed objects and deletes
    unreferenced listings. `--prune-bookmarks` evaluates stray `tether.ws.*`
    branches -- those of bookmarks that no longer exist (bookmarks the VCS has
    or a live checkout works on are kept), legacy per-workspace branches, and
    this bookmark's unused ones: a branch is deleted only if its head is pinned
    or equals the base head, otherwise kept -- `--force-prune` deletes those
    too. `--delete-stores` also reclaims stores this dataset created
    (`add --create`) that nothing references any more, once only tether's own
    refs remain in them (`delete-store`). Dry-run by default: pass
    `--no-dry-run` (or `--from-plan`) to release. Refused while jj reports a
    conflicted bookmark or commit.
    """
    _refuse_preview_with_apply(dry_run, plan_out, from_plan)
    if force_prune and not prune_bookmarks:
        _fail(TetherError("--force-prune requires --prune-bookmarks"))
    claimed: list[tuple[str, dict]] = []
    for item in store:
        kind_, sep, where = item.partition("=")
        if not sep or not kind_ or not where:
            _fail(TetherError(f"--store expects KIND=LOCATOR, got {item!r}"))
        claimed.append((kind_, {"uri": where}))
    if (delete_stores or claimed) and not json_out:
        _experimental_note(
            "reclaiming created stores is experimental: `delete-store` has no "
            "`repair`; fetch every bookmark first, and read the plan"
        )
    repo = _repo()
    try:
        if from_plan is not None:
            plan = _load_plan(from_plan, "gc")
            report: GcReport = repo.apply_gc(plan)
        else:
            plan = repo.plan_gc(
                prune_bookmarks=prune_bookmarks,
                keep_bookmarks=set(keep_bookmark) or None,
                force_prune=force_prune,
                delete_stores=delete_stores,
                stores=claimed,
                release_foreign=release_foreign,
            )
            if dry_run is not False or plan_out is not None:
                _save_plan(repo, plan, plan_out)
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
                "kept_pins": report.kept_pins,
                "deleted_working_refs": report.deleted_working_refs,
                "kept_working_refs": report.kept_working_refs,
                "forgotten_working_refs": report.forgotten_working_refs,
                "deleted_listings": report.deleted_listings,
                "deleted_stores": report.deleted_stores,
                "kept_stores": report.kept_stores,
                "forgotten_stores": report.forgotten_stores,
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
    foreign = sum(len(v) for v in report.kept_pins.values())
    if foreign:
        typer.secho(
            f"kept {foreign} unreferenced pin(s) this clone did not create "
            "(--release-foreign releases)",
            fg=typer.colors.YELLOW,
        )
        for kind, ids in report.kept_pins.items():
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
    if report.deleted_stores:
        typer.echo(f"deleted {len(report.deleted_stores)} created store(s)")
        for key, where in report.deleted_stores.items():
            typer.echo(f"  {key}: {where}")
    if report.kept_stores:
        typer.secho(
            f"kept {len(report.kept_stores)} unreferenced created store(s)",
            fg=typer.colors.YELLOW,
        )
        for key, why in report.kept_stores.items():
            typer.echo(f"  {key}: {why}")
    if report.forgotten_stores:
        typer.echo(
            f"forgot {len(report.forgotten_stores)} store(s) with nothing of "
            "tether's left in them"
        )


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
    """Land this bookmark on the trunk: move each system's upstream branch to
    what the bookmark's branch holds, then move the trunk bookmark.

    The dataset commit already pins the branch's state, so nothing needs
    re-committing after a fast-forward; `promote` makes `main` in each system
    point at it too, and when everything fast-forwarded, sets the trunk
    bookmark to this bookmark's commit. Base unchanged since the fork ->
    fast-forward. Base moved -> native 3-way merge where the system has one
    (Dolt, git), otherwise refused with the system's own recipe; after
    a merge, `commit` then `promote` again to move the trunk.
    """
    _refuse_preview_with_apply(dry_run, plan_out, from_plan)
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
                _save_plan(repo, plan, plan_out)
                _show_plan(plan, as_json=json_out)
                return
            report = repo.apply_promote(plan)
    except TetherError as exc:
        _fail(exc)
    if json_out:
        _emit(
            {
                "fast_forwarded": report.fast_forwarded,
                "merged": report.merged,
                "skipped": report.skipped,
                "refused": report.refused,
                "held": report.held,
                "conflicts": report.conflicts,
                "kept_forks": report.kept_forks,
                "trunk_moved": report.trunk_moved,
                "trunk_held": report.trunk_held,
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
    for key, why in report.kept_forks.items():
        typer.secho(f"  kept           {key}: {why}", fg=typer.colors.YELLOW)
    for key in report.skipped:
        typer.echo(f"  skipped        {key}")
    for key, why in report.refused.items():
        typer.secho(f"  refused        {key}: {why}", fg=typer.colors.YELLOW)
        for unit in report.conflicts.get(key, []):
            typer.echo(f"                   conflict: {unit}")
    for key, why in report.held.items():
        typer.echo(f"  held           {key}: {why}")
    if report.held:
        typer.echo(
            "nothing moved: a bookmark is planned whole or not at all; name keys to "
            "land a subset, or finish the refused systems on the trunk and promote "
            "again"
        )
    if report.merged:
        typer.echo("run `tether commit` to pin the merge result(s), then promote again")
    if report.trunk_moved:
        typer.echo(f"{repo.config.trunk} -> {report.trunk_moved[:12]}")
    if report.trunk_held:
        typer.echo(f"{repo.config.trunk} not moved: {report.trunk_held}")
    if report.refused:
        raise typer.Exit(1)


# --------------------------------------------------------------------------- #
# Registries: export / publish / import (experimental) -- registered from
# tether.experimental.cli so this module stays the core loop.
# --------------------------------------------------------------------------- #
from tether.experimental.cli import register as _register_experimental  # noqa: E402

_register_experimental(app)


def main() -> None:
    try:
        app()
    except TetherError as exc:  # pragma: no cover - defensive top-level
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
