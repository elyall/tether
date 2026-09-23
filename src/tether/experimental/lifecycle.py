"""Store lifecycle: create a store tether then owns, and reclaim it.

**Experimental.** Everything else tether does to a store is recoverable from
the manifests: a deleted pin can be `repair`ed, a deleted branch re-forked.
`delete-store` cannot be, and its blast radius is a whole store. The feature
has also not yet run against the resources it was written for -- an S3
prefix, a second clone -- outside tests with stand-ins. So it lives here,
behind the same seam as the registry: the stable surface is what users type
and import (`tether add --create`, `tether gc --delete-stores`,
`Repo.create`, `Repo.add(create=True)`), and the engine half is imported
lazily by those entry points. Module paths under `tether.experimental` are
not API.

What is here:

- the created-store index beside the repository lock (`tether-created.jsonl`)
  and the reader of the touched-store journal (`tether-touched.jsonl`). The
  journal itself -- what this clone has forked a branch in or pinned -- is
  core-owned and written by every fork and pin (`tether.oplog.append_touched`),
  so that stable commands never import this module and the record is complete
  by the time the feature graduates; only the consumer lives here;
- `create_store`: the `add --create` path (make, mark, index, register, fork
  on a bookmark), and `undo_add_store`, its reversal while the store is
  still empty;
- the `gc --delete-stores` step (`plan_stores`, `apply_store_action`): release
  this dataset's dead refs in stores no manifest names, and delete a created
  store once the backend confirms nothing else remains, under the rules
  documented on `plan_created_stores` and `plan_store_refs`.

The backend contract this rides on (`Capability.CREATE`: `create`, `owner`,
`is_ref_empty`, `delete_store`) stays in `tether.backends.base` with
conformance coverage; it is a per-backend fact, marked experimental in its
docstring.

**Graduation criteria** -- what has to be true before this moves back into
`tether.repo` and loses the experimental note:

1. The S3 arm of Icechunk's `delete_store` has run once against a real bucket
   (the in-memory obstore test covers the logic, not the service).
2. The two-clone scenario in `tests/test_store_lifecycle.py` has run against a
   real remote (a hosted git or jj remote), not a local clone.
3. One release cycle with the feature in users' hands and no data-loss report.
4. A second Forkable backend declares `CREATE` (Neon project or branch; Lance
   once an empty schema is acceptable), showing the capability generalises
   beyond "make an empty directory or repository".
5. A decision on the touched index: keep it as the mechanism for dead refs in
   unnamed stores, or fold that into `--prune-bookmarks` by walking the stores
   of every manifest in history.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tether import manifest as _m
from tether.backends.base import (
    Capability,
    absolutize_locator,
    effective_capabilities,
)
from tether.errors import CapabilityError, ConfigError, StalePlanError, TetherError
from tether.manifest import (
    ObjectManifest,
    Policy,
    State,
    bookmark_slug,
    canonical_bytes,
    compute_pin_id,
    pin_dataset,
    read_objects,
    ref_for_pin,
    working_ref_bookmark,
    working_ref_dataset,
)
from tether.oplog import (
    OpEntry,
    TouchedStore,
    append_index_entry,
    read_jsonl,
    read_ops,
    read_touched,
    remove_index_entry,
    remove_touched,
)
from tether.plan import Action, Plan
from tether.repo._reports import GcReport, UndoReport, short_state

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tether.repo import Repo

# --------------------------------------------------------------------------- #
# The indexes
# --------------------------------------------------------------------------- #
CREATED_FILENAME = "tether-created.jsonl"
"""Repository-wide index of stores tether *created*, kept next to the
repository lock in the shared VCS store (`VcsAdapter.shared_dir()`): one clone
of the dataset, every checkout, never committed. History cannot hold this --
an abandoned fork takes the manifest that named the store with it -- and a
workspace's own log dies with `forget-workspace`. This survives both.
"""


@dataclass(frozen=True)
class CreatedStore:
    """One store tether made (`add --create`); `gc` may remove it once nothing
    references it and the store confirms the owner marker."""

    dataset_id: str
    kind: str
    identity: dict[str, Any]
    """`ObjectBackend.identity(locator)`: what a manifest reference is matched on."""
    locator: dict[str, Any]
    key: str
    """The key it was registered under at creation (informational)."""
    at: str
    bookmark: str | None = None
    """The bookmark the store was created on. Its `tether.ws.*` branch is the
    one this clone can account for when `gc` judges the store; a same-dataset
    branch of a bookmark this clone never had may be another actor's."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "kind": self.kind,
            "identity": dict(self.identity),
            "locator": dict(self.locator),
            "key": self.key,
            "at": self.at,
            "bookmark": self.bookmark,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CreatedStore:
        return cls(
            dataset_id=str(data["dataset_id"]),
            kind=str(data["kind"]),
            identity=dict(data.get("identity") or {}),
            locator=dict(data.get("locator") or {}),
            key=str(data.get("key", "")),
            at=str(data.get("at", "")),
            bookmark=str(data["bookmark"]) if data.get("bookmark") else None,
        )


def created_path(shared_dir: Path) -> Path:
    return shared_dir / CREATED_FILENAME


def append_created(shared_dir: Path, entry: CreatedStore) -> None:
    """Record a store tether just created (journal-style, synced)."""
    append_index_entry(created_path(shared_dir), entry.to_dict())


def read_created(shared_dir: Path, dataset_id: str | None = None) -> list[CreatedStore]:
    """Every created store on record (optionally one dataset's), oldest first."""
    out: list[CreatedStore] = []
    for data in read_jsonl(created_path(shared_dir)):
        entry = CreatedStore.from_dict(data)
        if dataset_id is None or entry.dataset_id == dataset_id:
            out.append(entry)
    return out


def remove_created(shared_dir: Path, dataset_id: str, identity: dict[str, Any]) -> None:
    """Forget a store (deleted by `gc`, or by `undo add`)."""
    remove_index_entry(created_path(shared_dir), dataset_id, identity)


def created_stores(repo: Repo) -> list[CreatedStore]:
    """Stores this dataset created (`add --create`) and has not yet removed."""
    return read_created(repo.vcs.shared_dir(), repo.config.dataset_id)


def touched_stores(repo: Repo) -> list[TouchedStore]:
    """Stores this clone has forked or pinned in."""
    return read_touched(repo.vcs.shared_dir(), repo.config.dataset_id)


# --------------------------------------------------------------------------- #
# Creating
# --------------------------------------------------------------------------- #
def create_store(
    repo: Repo,
    key: str,
    kind: str,
    locator: dict,
    *,
    policy: Policy | None,
    pre: dict[str, Any],
) -> ObjectManifest:
    """The `add --create` path, under the caller's writer lock: require
    `CREATE`, journal, have the backend make the store with this dataset's
    owner marker, index it, register it with `origin = "created"`, and on a
    non-trunk bookmark fork its working branch at once."""
    backend = repo.backend_for(kind)
    resolved = absolutize_locator(backend, dict(locator), Path.cwd())
    backend.validate_locator(resolved)
    repo._secrets_for_new_object(kind, backend, key, resolved)
    eff = effective_capabilities(backend, resolved, policy or repo.config.defaults)
    if Capability.CREATE not in eff:
        raise CapabilityError(
            f"{kind!r} cannot create a store at this locator; register an "
            "existing one instead",
            key=key,
            kind=kind,
        )
    result: dict[str, Any] = {"key": key}
    # A store write follows: journal first, like every other command that
    # writes to a store, so a crash leaves a started entry.
    op = repo._begin_op("add", pre=pre)
    initial = backend.create(resolved, owner=repo.config.dataset_id)
    identity = dict(backend.identity(resolved))
    append_created(
        repo.vcs.shared_dir(),
        CreatedStore(
            dataset_id=repo.config.dataset_id,
            kind=kind,
            identity=identity,
            locator=dict(resolved),
            key=key,
            at=_m._now(),
            bookmark=repo.workspace.bookmark,
        ),
    )
    result["created"] = {"kind": kind, "identity": identity, "locator": dict(resolved)}
    manifest = repo._add(key, kind, resolved, policy=policy, origin="created")
    ref = repo._adopt_into_bookmark(key, initial)
    if ref is not None:
        result["fork"] = ref
    repo._end_op(op, result=result)
    return manifest


def undo_add_store(repo: Repo, entry: OpEntry, report: UndoReport) -> None:
    """`add --create` made a store: take it away again while it is still
    empty (its own working branch, if `add` forked one, does not count); a
    store that has been written to stays, and `gc` decides later."""
    created = entry.result.get("created")
    if not created:
        return
    key = str(entry.result.get("key", ""))
    kind = str(created["kind"])
    locator = dict(created["locator"])
    backend = repo.backend_for(kind)
    fork = entry.result.get("fork")
    ignoring = {str(fork)} if fork else set()
    try:
        if backend.owner(locator) != repo.config.dataset_id:
            report.irreversible.append(
                f"{key}: the store at {_where(locator)} no longer carries this "
                "dataset's owner marker; left alone"
            )
            return
        if backend.is_ref_empty(locator, ignoring=ignoring) is not True:
            report.irreversible.append(
                f"{key}: the store at {_where(locator)} holds writes; left for "
                "`gc --delete-stores` to reclaim once nothing references it"
            )
            return
        if fork:
            backend.delete_working_ref(locator, str(fork))
        backend.delete_store(locator)
    except TetherError as exc:
        report.irreversible.append(f"{key}: could not remove the store: {exc}")
        return
    remove_created(
        repo.vcs.shared_dir(), repo.config.dataset_id, dict(created["identity"])
    )
    remove_touched(
        repo.vcs.shared_dir(), repo.config.dataset_id, dict(created["identity"])
    )
    report.restored.append(f"{key}: removed the store it created")


# --------------------------------------------------------------------------- #
# Reclaiming (gc --delete-stores)
# --------------------------------------------------------------------------- #
@dataclass
class _StoreContext:
    """What the store steps know about the rest of the world (see
    `GcOps._store_context`)."""

    in_use: set[str]
    """`kind|identity` of every store some manifest (history, this working
    tree, any live checkout's working tree) names."""
    keep_slugs: set[str]
    """Slugs of live bookmarks: their branches are never stray."""
    known_slugs: set[str]
    """Slugs of every bookmark this clone has had (live ones and every `new`
    any live checkout logged)."""
    known_pins: set[str]
    """Pin ids this clone created (`tether-pinned.jsonl`, the index `gc`
    itself releases by): the pins this clone can account for."""
    referenced_pins: set[str] = field(default_factory=set)
    """Pin ids some manifest names (or an unfinished operation made), in
    whichever store: never released, even in a store `in_use` misses under
    another spelling of it."""


@dataclass
class _StoreRefs:
    """What `GcOps._plan_store_refs` found and planned in one store."""

    ignoring: set[str] = field(default_factory=set)
    """Refs this plan releases (for `is_ref_empty(ignoring=...)`)."""
    blockers: list[str] = field(default_factory=list)
    ours: list[str] = field(default_factory=list)
    strangers: list[str] = field(default_factory=list)
    pins: set[str] = field(default_factory=set)
    our_pins: list[str] = field(default_factory=list)
    stranger_pins: list[str] = field(default_factory=list)
    foreign_pins: int = 0
    foreign_refs: int = 0


def _in_use(
    repo: Repo, ctx: _StoreContext, kind: str, identity: dict, locator: dict
) -> bool:
    """Whether a manifest names an indexed store: by the identity recorded
    when it was indexed, or by the one its locator has now. Identities have
    been normalized since stores were indexed (`file://` folded into paths,
    then symlinks and trailing slashes), and an index is never committed."""
    tags = {f"{kind}|{canonical_bytes(identity).decode()}"}
    with contextlib.suppress(TetherError):
        now = dict(repo.backend_for(kind).identity(locator))
        tags.add(f"{kind}|{canonical_bytes(now).decode()}")
    return bool(tags & ctx.in_use)


def _where(locator: dict) -> str:
    """The locator field that names a store, for plan targets and messages."""
    for k in ("uri", "path", "system", "project_id", "table"):
        if locator.get(k):
            return str(locator[k])
    return str(locator)


def _store_context(
    repo: Repo, manifests: list[ObjectManifest], keep_bookmarks: set[str]
) -> _StoreContext:
    """What both store steps need to know about the rest of the world."""

    def ident(kind: str, identity: dict) -> str:
        return f"{kind}|{canonical_bytes(identity).decode()}"

    in_use: set[str] = set()
    referenced = {m.pin.id for m in manifests if m.pin is not None}
    for m in manifests:
        in_use.add(ident(m.kind, dict(repo.backend_for(m.kind).identity(m.locator))))
    for root, _ws in repo._iter_live_workspaces():
        if root.resolve() == repo.root.resolve():
            continue
        for m in read_objects(root).values():
            in_use.add(
                ident(m.kind, dict(repo.backend_for(m.kind).identity(m.locator)))
            )
            if m.pin is not None:
                referenced.add(m.pin.id)
    referenced |= repo._inflight_pins()
    keep_slugs = {bookmark_slug(b) for b in keep_bookmarks}
    # What this clone can account for: the bookmarks any `new` in a live
    # checkout's op log made, and the pins the clone's index says it
    # created. Refs of this dataset that neither explains may be another
    # actor's -- one who fetched the bookmark and works in the store with
    # commits this clone does not have.
    known_slugs = set(keep_slugs)
    for root, _ws in repo._iter_live_workspaces():
        for op in read_ops(root):
            if op.command == "new" and op.result.get("bookmark"):
                known_slugs.add(bookmark_slug(str(op.result["bookmark"])))
    return _StoreContext(
        in_use, keep_slugs, known_slugs, repo._known_pins(), referenced
    )


def _plan_store_refs(
    repo: Repo,
    plan: Plan,
    *,
    key: str,
    kind: str,
    locator: dict,
    identity: dict,
    ctx: _StoreContext,
    prune_bookmarks: bool,
    force: bool,
    created_on: str | None,
    why: str,
) -> _StoreRefs:
    """Plan the release of this dataset's own refs in a store no manifest
    names: unpin our pins, delete our stray branches under the
    `--prune-bookmarks` rules. Shared by the touched and created steps.

    "Our own" has one carve-out. Another clone or environment of the
    *same* dataset may have fetched the bookmark that named the store and
    be working in it; its branches and pins carry our dataset id, and its
    commits are not in our history unless fetched. So a same-dataset
    branch is *accounted for* only when its bookmark is one this clone
    knows: the one the store was created on, a live bookmark, or one a
    `new` in any live checkout of this clone made. Any other same-dataset
    branch means someone may be alive in there, and nothing is planned
    for the store -- not even our pins, which same-dataset
    content-addressed commits share. `force` overrides, explicitly.
    """
    ds = repo.config.dataset_id
    backend = repo.backend_for(kind)
    eff = effective_capabilities(backend, locator, repo.config.defaults)
    out = _StoreRefs()
    if Capability.FORK in eff:
        mine_slugs = set(ctx.known_slugs)
        if created_on:
            mine_slugs.add(bookmark_slug(created_on))
        for ref in sorted(backend.list_working_refs(locator)):
            if working_ref_dataset(ref) != ds:
                out.foreign_refs += 1
                continue
            slug = working_ref_bookmark(ref)
            if slug is not None and slug not in mine_slugs:
                out.strangers.append(ref)
            else:
                out.ours.append(ref)
    if Capability.PIN in eff:
        out.pins = set(backend.list_pins(locator))
        ours = sorted(p for p in out.pins if pin_dataset(p) == ds)
        out.foreign_pins = len(out.pins) - len(ours)
        # A pin a manifest names stays, and so does the store holding it
        # (`is_ref_empty` sees it): the store is in use under another spelling.
        mine = [p for p in ours if p not in ctx.referenced_pins]
        out.our_pins = [p for p in mine if p in ctx.known_pins]
        out.stranger_pins = [p for p in mine if p not in ctx.known_pins]
    if (out.strangers or out.stranger_pins) and not force:
        what = []
        if out.strangers:
            what.append(
                "branch(es) of bookmark(s) this clone never had: "
                + ", ".join(out.strangers)
            )
        if out.stranger_pins:
            what.append(
                "pin(s) no commit of this clone made: " + ", ".join(out.stranger_pins)
            )
        out.blockers.append(
            "; ".join(what)
            + " -- another checkout or clone of this dataset may be working "
            "in the store; fetch it, or --force-prune"
        )
        return out  # touch nothing
    for pid in [*out.our_pins, *out.stranger_pins]:
        plan.actions.append(
            Action(
                "unpin",
                key,
                kind,
                target=ref_for_pin(pid),
                detail=why,
                params={"locator": locator, "pin_id": pid},
            )
        )
        out.ignoring.add(ref_for_pin(pid))
    base_head: State | None = None
    for ref in [*out.ours, *out.strangers]:
        slug = working_ref_bookmark(ref)
        if not prune_bookmarks:
            out.blockers.append(
                f"working branch {ref} (gc --prune-bookmarks evaluates it)"
            )
            continue
        if slug is not None and slug in ctx.keep_slugs:
            out.blockers.append(f"branch {ref} belongs to live bookmark {slug}")
            continue
        reason: str | None = None
        head: State | None = None
        safe = ""
        if ref in out.strangers:
            reason = "belongs to a bookmark this clone never had"
        else:
            try:
                head = backend.fingerprint(locator, ref)
                content = repo._content_of(kind, head)
                if base_head is None:
                    base_head = repo._content(kind, backend.fingerprint(locator, None))
                if compute_pin_id(kind, identity, content, ds) in out.pins:
                    safe = "head is pinned"
                elif content == base_head:
                    safe = "head equals the base branch"
                else:
                    reason = f"has unpinned writes ({short_state(content)})"
            except TetherError as exc:
                reason = f"cannot read head: {exc}"
        if reason is not None and not force:
            out.blockers.append(f"branch {ref} {reason} (--force-prune deletes)")
            continue
        if head is None and ref in out.strangers:
            with contextlib.suppress(TetherError):
                head = backend.fingerprint(locator, ref)
        plan.actions.append(
            Action(
                "delete-branch",
                key,
                kind,
                target=ref,
                detail=f"{why}; "
                + (safe if reason is None else f"FORCED although it {reason}"),
                params={
                    "locator": locator,
                    "head": head,
                    **({"forced": True} if reason is not None else {}),
                },
            )
        )
        out.ignoring.add(ref)
    return out


def _plan_touched_stores(
    repo: Repo,
    plan: Plan,
    ctx: _StoreContext,
    *,
    prune_bookmarks: bool,
    force: bool,
    skip: set[str],
) -> None:
    """Release this dataset's dead refs in stores this clone forked or
    pinned in but no manifest names any more (an abandoned bookmark took
    the manifest with it). Without this, those refs stayed forever -- and
    kept the store's creator from ever reclaiming it. `skip` holds the
    identities the created-store step is handling in the same plan.
    """
    for e in touched_stores(repo):
        tag = f"{e.kind}|{canonical_bytes(e.identity).decode()}"
        if tag in skip or _in_use(repo, ctx, e.kind, e.identity, e.locator):
            continue
        locator = dict(e.locator)
        where = _where(locator)
        try:
            refs = _plan_store_refs(
                repo,
                plan,
                key=e.key,
                kind=e.kind,
                locator=locator,
                identity=dict(e.identity),
                ctx=ctx,
                prune_bookmarks=prune_bookmarks,
                force=force,
                created_on=None,
                why="in a store no manifest names (touched by this clone)",
            )
        except TetherError as exc:
            plan.notes.append(
                f"{e.key}: cannot list {where} (touched by this clone): {exc}"
            )
            continue
        if (refs.strangers or refs.stranger_pins) and not force:
            plan.notes.append(f"{e.key}: {where}: {refs.blockers[0]}")
            continue
        if not refs.our_pins and not refs.ours and not refs.strangers:
            # Nothing of ours is left in it: stop looking.
            plan.actions.append(
                Action(
                    "forget-touched",
                    e.key,
                    e.kind,
                    target=where,
                    detail="nothing of this dataset's remains in the store; "
                    "dropping it from the touched-store index",
                    params={"locator": locator, "identity": dict(e.identity)},
                )
            )
        for line in refs.blockers:
            plan.notes.append(f"{e.key}: {where}: kept {line}")


def _plan_created_stores(
    repo: Repo,
    plan: Plan,
    ctx: _StoreContext,
    *,
    prune_bookmarks: bool,
    force: bool,
    claimed: Sequence[tuple[str, dict]] = (),
) -> set[str]:
    """Add `delete-store` / `keep-store` (and, through `_plan_store_refs`,
    the unpins and branch deletions inside such a store) for stores this
    dataset created.

    For each entry of the created-store index -- plus any store `claimed`
    by kind and locator, for a store whose creator's clone is gone -- whose
    identity no manifest anywhere still names: the owner marker must name
    this dataset (never a manifest field: a clone can say
    `origin = "created"` about anything), everything of ours in it is
    released by this plan, and when the backend confirms nothing else
    remains, the store goes last, guarded by a `store_empty` precondition.
    Returns the identities considered, for the touched step to skip.
    """
    ds = repo.config.dataset_id
    entries: list[tuple[CreatedStore, bool]] = [(e, True) for e in created_stores(repo)]
    seen = {f"{e.kind}|{canonical_bytes(e.identity).decode()}" for e, _ in entries}
    for kind, locator in claimed:
        backend = repo.backend_for(kind)
        resolved = absolutize_locator(backend, dict(locator), Path.cwd())
        identity = dict(backend.identity(resolved))
        tag = f"{kind}|{canonical_bytes(identity).decode()}"
        if tag in seen:
            continue
        seen.add(tag)
        entries.append(
            (
                CreatedStore(
                    dataset_id=ds,
                    kind=kind,
                    identity=identity,
                    locator=dict(resolved),
                    key=_where(resolved),
                    at="",
                ),
                False,
            )
        )
    if not entries:
        return set()

    deletions: list[tuple[CreatedStore, list[str]]] = []
    still_used = 0
    for e, indexed in entries:
        if _in_use(repo, ctx, e.kind, e.identity, e.locator):
            still_used += 1
            continue
        backend = repo.backend_for(e.kind)
        locator = dict(e.locator)
        where = _where(locator)

        def keep(reason: str, *, e: CreatedStore = e, where: str = where) -> None:
            plan.actions.append(
                Action(
                    "keep-store",
                    e.key,
                    e.kind,
                    target=where,
                    detail=f"created store is unreferenced; kept: {reason}",
                    params={
                        "locator": dict(e.locator),
                        "identity": dict(e.identity),
                    },
                )
            )

        try:
            owner = backend.owner(locator)
        except TetherError as exc:
            keep(f"cannot read its owner marker: {exc}")
            continue
        if owner is None:
            if not indexed:
                plan.notes.append(
                    f"{where}: no owner marker; not a store tether created"
                )
                continue
            # Gone (or its marker is): nothing of ours to remove; stop
            # asking about it.
            plan.actions.append(
                Action(
                    "forget-store",
                    e.key,
                    e.kind,
                    target=where,
                    detail="no owner marker: the store was removed or taken over; "
                    "dropping it from the created-store index",
                    params={
                        "locator": dict(e.locator),
                        "identity": dict(e.identity),
                    },
                )
            )
            continue
        if owner != ds:
            plan.notes.append(
                f"{e.key}: created store {where} carries dataset {owner}'s "
                "owner marker; left alone"
            )
            continue

        try:
            refs = _plan_store_refs(
                repo,
                plan,
                key=e.key,
                kind=e.kind,
                locator=locator,
                identity=dict(e.identity),
                ctx=ctx,
                prune_bookmarks=prune_bookmarks,
                force=force,
                created_on=e.bookmark,
                why="in an unreferenced created store",
            )
        except TetherError as exc:
            keep(f"cannot list its refs: {exc}")
            continue
        if refs.blockers:
            keep("; ".join(refs.blockers))
            continue
        try:
            empty = backend.is_ref_empty(locator, ignoring=refs.ignoring)
        except TetherError as exc:
            keep(f"cannot list what remains: {exc}")
            continue
        if empty is None:
            keep("the backend cannot tell what remains in it")
            continue
        if not empty:
            what = []
            if refs.foreign_pins:
                what.append(f"{refs.foreign_pins} pin(s) of other datasets")
            if refs.foreign_refs:
                what.append(f"{refs.foreign_refs} working branch(es) of other datasets")
            what.append("refs or writes that are not tether's own")
            keep(f"holds {', or '.join(what) if len(what) > 1 else what[0]}")
            continue
        deletions.append((e, sorted(refs.ignoring)))

    # Store deletions go last: everything else in the plan runs first, and
    # the precondition is checked again at that moment with nothing ignored.
    for e, ignoring_refs in deletions:
        where = _where(dict(e.locator))
        plan.actions.append(
            Action(
                "delete-store",
                e.key,
                e.kind,
                target=where,
                detail="created by this dataset; unreferenced; only tether's own "
                "refs remained",
                params={"locator": dict(e.locator), "identity": dict(e.identity)},
            )
        )
        plan.require(
            "store_empty",
            True,
            key=e.key,
            backend=e.kind,
            locator=dict(e.locator),
            ignoring=ignoring_refs,
            detail=f"{e.key}: the store at {where} is no longer empty "
            "({observed}); re-run the plan",
        )
    if still_used:
        plan.notes.append(f"{still_used} created store(s) still referenced; kept")
    return seen


def plan_stores(
    repo: Repo,
    plan: Plan,
    manifests: list[ObjectManifest],
    *,
    keep_bookmarks: set[str],
    prune_bookmarks: bool,
    force: bool,
    claimed: Sequence[tuple[str, dict]] = (),
) -> None:
    """The `gc --delete-stores` step: created stores first (they may be
    deleted), then this clone's dead refs in every other store it touched."""
    ctx = _store_context(repo, manifests, keep_bookmarks)
    handled = _plan_created_stores(
        repo, plan, ctx, prune_bookmarks=prune_bookmarks, force=force, claimed=claimed
    )
    _plan_touched_stores(
        repo, plan, ctx, prune_bookmarks=prune_bookmarks, force=force, skip=handled
    )


STORE_OPS = frozenset({"keep-store", "forget-store", "forget-touched", "delete-store"})
"""The plan verbs this module applies (`apply_store_action`)."""


def apply_store_action(
    repo: Repo,
    a: Action,
    report: GcReport,
    errors: dict[str, Exception],
    op: OpEntry | None,
) -> None:
    """Apply one store verb of a gc plan (called from `apply_gc`, after every
    other action; `delete-store` re-checks owner and emptiness first)."""
    ds = repo.config.dataset_id
    shared = repo.vcs.shared_dir()
    if a.op == "keep-store":
        report.kept_stores[a.key] = a.detail
    elif a.op == "forget-store":
        remove_created(shared, ds, dict(a.params["identity"]))
        report.forgotten_stores.append(a.key)
    elif a.op == "forget-touched":
        remove_touched(shared, ds, dict(a.params["identity"]))
        report.forgotten_stores.append(a.key)
    elif a.op == "delete-store":
        backend = repo.backend_for(a.kind)
        locator = dict(a.params["locator"])
        # The refs this plan ignored are gone now; the store must be empty
        # outright, and still ours.
        if backend.owner(locator) != ds:
            report.kept_stores[a.key] = "owner marker changed"
            errors[f"{a.op} {a.target}"] = StalePlanError(
                f"{a.key}: the store at {a.target} no longer carries this "
                "dataset's owner marker"
            )
            return
        if backend.is_ref_empty(locator) is not True:
            report.kept_stores[a.key] = "not empty at the moment of deletion"
            errors[f"{a.op} {a.target}"] = StalePlanError(
                f"{a.key}: the store at {a.target} is not empty; re-run the plan"
            )
            return
        backend.delete_store(locator)
        remove_created(shared, ds, dict(a.params["identity"]))
        remove_touched(shared, ds, dict(a.params["identity"]))
        report.deleted_stores[a.key] = a.target
        repo._progress(op, "delete-store", key=a.key, target=a.target)
    else:  # pragma: no cover - STORE_OPS guards the dispatch
        raise ConfigError(f"not a store action: {a.op!r}")


def preview_store_action(a: Action, report: GcReport) -> None:
    """What a dry run reports for one store verb."""
    if a.op == "delete-store":
        report.deleted_stores[a.key] = a.target
    elif a.op == "keep-store":
        report.kept_stores[a.key] = a.detail
    elif a.op in ("forget-store", "forget-touched"):
        report.forgotten_stores.append(a.key)
