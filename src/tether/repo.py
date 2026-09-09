"""The tether engine: snapshot, status, commit, new, open, verify, gc.

A :class:`Repo` binds a dataset root (a ``tether.toml`` plus ``.tether/``) to the
enclosing VCS and the registered backends, and orchestrates the cross-system
fan-out. It holds no data itself; it coordinates native refs and hands back
native handles.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tether import manifest as _m
from tether.backends.base import (
    Capability,
    HistoryEntry,
    ObjectBackend,
    ObjectDiff,
    Tier,
    VerifyReport,
    VerifyStatus,
    base_at,
    build_backend,
    content_state,
    effective_capabilities,
    tier_of,
)
from tether.errors import (
    CapabilityError,
    ConfigError,
    ImmutableObjectModified,
    MergeConflict,
    MultiObjectError,
    StalePlanError,
    StaleWorkingCopyError,
    TetherError,
    UnpinnedStateError,
    VcsError,
)
from tether.export import ExportBundle, build_bundle
from tether.handles import Handle
from tether.manifest import (
    ObjectManifest,
    Pin,
    Policy,
    RepoConfig,
    State,
    compute_pin_id,
    ensure_ignored,
    ensure_layout,
    find_dataset_root,
    listing_name,
    listings_dir,
    manifest_hash,
    pin_dataset,
    read_config,
    read_listing,
    read_objects,
    read_workspace,
    ref_for_pin,
    remove_object,
    working_ref_dataset,
    working_ref_name,
    working_ref_workspace,
    workspace_path,
    write_config,
    write_listing,
    write_object,
    write_workspace,
)
from tether.oplog import OpEntry, append_op, mark_undone, read_ops
from tether.plan import Action, Plan
from tether.registry import ImportSpec, specs_from_rows
from tether.vcs import VcsAdapter, detect_vcs

TETHER_REV_ENV = "TETHER_REV"
"""Environment variable `Repo.open` reads for its default revision.

When set, `open(key)` returns a read-only handle at that commit's pinned state,
so reproducible jobs pin their inputs without code changes.
"""
# Fan-out is network-bound (S3 HEADs, control-plane calls, catalog reads); the
# Python work per object is microseconds, so threads -- not asyncio -- are the
# right tool and a generous pool costs nothing when idle.
_MAX_WORKERS = 16


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #
@dataclass
class ObjectStatus:
    """One object's classification in a `StatusReport`."""

    key: str
    """Object key."""
    kind: str
    """Backend kind."""
    tier: Tier
    """Effective capability tier for this object."""
    committed: bool
    """Whether the manifest has a committed state."""
    pinned: bool
    """Whether the manifest carries a native pin."""
    recoverable: bool
    """Whether the committed state can be reconstructed later."""
    changed: bool
    """Whether the current state differs from the committed one."""
    current_state: State | None
    """The fingerprint taken by this status (or the cached one)."""
    verify: VerifyReport | None = None
    """Cheap verify result when `RepoConfig.verify_on_status` is set."""
    error: str | None = None
    """Fingerprint failure message, if any."""

    @property
    def state_label(self) -> str:
        """`"new"`, `"modified"`, `"clean"`, or `"error"`."""
        if self.error is not None:
            return "error"
        if not self.committed:
            return "new"
        if self.changed:
            return "modified"
        return "clean"


@dataclass
class StatusReport:
    """Result of `Repo.status`."""

    manifest_hash: str
    """Hash of the committed object set (the dataset's "tree id")."""
    stale: bool
    """Some forked object's committed manifest changed underneath this workspace."""
    objects: list[ObjectStatus] = field(default_factory=list)
    """Per-object classifications, sorted by key."""
    stale_keys: list[str] = field(default_factory=list)
    """The objects that make the workspace stale (see `Repo.stale_keys`)."""


@dataclass
class CommitResult:
    """Result of `Repo.commit`."""

    message: str
    """The commit message."""
    pinned: dict[str, Pin | None] = field(default_factory=dict)
    """Objects whose state was recorded: the `Pin` created, or `None` for
    Addressable objects (recorded without a native ref)."""
    unrecoverable: list[str] = field(default_factory=list)
    """Observed-tier objects recorded with `recoverable = False`."""
    unchanged: list[str] = field(default_factory=list)
    """Objects skipped because their state was already recorded."""
    vcs_commit: str | None = None
    """Commit id of the VCS commit, or `None` if nothing changed / `vcs=False`."""


@dataclass
class GcReport:
    """Result of `Repo.gc`."""

    unpinned: dict[str, list[str]] = field(default_factory=dict)
    """Backend kind -> pin ids released (or that would be, in a dry run)."""
    deleted_working_refs: dict[str, list[str]] = field(default_factory=dict)
    """Object key -> native working branches deleted (`--prune-workspaces`)."""
    kept_working_refs: dict[str, list[str]] = field(default_factory=dict)
    """Object key -> stray branches kept because they hold unpinned data."""
    forgotten_working_refs: dict[str, list[str]] = field(default_factory=dict)
    """Object key -> refs dropped from the workspace state (object removed)."""
    deleted_listings: list[str] = field(default_factory=list)
    """`.tether/listings/` files no manifest references."""
    dry_run: bool = True
    """Whether anything was actually released."""
    plan: Plan | None = None
    """The plan that was (or would be) applied."""


@dataclass
class ImportReport:
    """Result of `Repo.apply_import`."""

    added: list[str] = field(default_factory=list)
    """Keys registered."""
    updated: list[str] = field(default_factory=list)
    """Keys whose locator or policy changed (committed state kept)."""
    removed: list[str] = field(default_factory=list)
    """Keys unregistered (`sync=True` only)."""
    unchanged: list[str] = field(default_factory=list)
    """Keys the source listed identically."""
    plan: Plan | None = None


@dataclass
class PromoteReport:
    """Result of `Repo.apply_promote`."""

    fast_forwarded: dict[str, State] = field(default_factory=dict)
    """Key -> the base branch's new state after a fast-forward."""
    merged: dict[str, State] = field(default_factory=dict)
    """Key -> the base branch's new state after a native merge."""
    skipped: list[str] = field(default_factory=list)
    """Keys whose base already matched the target, or that had nothing to promote."""
    refused: dict[str, str] = field(default_factory=dict)
    """Key -> why tether would not move the base (with the system's own recipe)."""
    conflicts: dict[str, list[str]] = field(default_factory=dict)
    """Key -> conflicting units reported by a merge that was rolled back."""
    plan: Plan | None = None


@dataclass
class UndoReport:
    """What `Repo.undo` reversed, could not reverse, and left alone.

    Attributes:
        op: The operation that was undone.
        undo_id: Id of the `undo` entry appended to the op log.
        restored: What was put back (one line each).
        irreversible: What the stores no longer allow to be put back.
        skipped: Parts that no longer applied (e.g. the working copy had moved).
    """

    op: OpEntry
    undo_id: str = ""
    restored: list[str] = field(default_factory=list)
    irreversible: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.irreversible


@dataclass
class DiffEntry:
    """One object's row in `Repo.diff`."""

    key: str
    """Object key."""
    change: str
    """`"added"`, `"removed"`, `"changed"`, or `"unchanged"`."""
    a_pin: str | None = None
    """Pin id on side A."""
    b_pin: str | None = None
    """Pin id on side B."""
    detail: ObjectDiff | None = None
    """Native content diff (only with `content=True` and a `DIFF` backend)."""
    detail_error: str | None = None
    """Backend failure while computing `detail`, if any."""


def _source_object(source: Mapping[str, Any]) -> str | Pin | State:
    """Rebuild a promote source (`{"ref"} | {"pin"} | {"state"}`) from plan params."""
    if "ref" in source:
        return str(source["ref"])
    if "pin" in source:
        return Pin.from_dict(dict(source["pin"]))
    return dict(source["state"])


def _report_dict(report: Any) -> dict[str, Any]:
    """A report dataclass as plain data for the op log (without its plan)."""
    data = dataclasses.asdict(report)
    data.pop("plan", None)
    return data


def _short_state(state: State | None) -> str:
    """Compact one-line rendering of a state for plan output."""
    if not state:
        return "?"
    parts = []
    for k, v in state.items():
        text = str(v)
        parts.append(f"{k}={text[:16]}{'…' if len(text) > 16 else ''}")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Repo
# --------------------------------------------------------------------------- #
class Repo:
    """A tether dataset: manifests in a VCS working tree plus the systems they name.

    Construct with `Repo.init` (new dataset) or `Repo.find` (existing one).
    Every method that touches external systems fans out concurrently across
    objects and aggregates failures into `MultiObjectError`.

    Attributes:
        root: Dataset root (the directory holding `tether.toml`).
        config: The committed `RepoConfig`.
        vcs: Adapter for the enclosing jj or git repository.
        objects: Committed manifests in the working tree, by key.
        workspace: Untracked per-workspace state (working refs, cached snapshot).
    """

    def __init__(
        self,
        root: Path,
        config: RepoConfig,
        vcs: VcsAdapter,
    ) -> None:
        self.root = root
        self.config = config
        self.vcs = vcs
        ensure_ignored(root)  # the op log is new since a7; never let jj snapshot it
        self.objects = read_objects(root)
        self.workspace = read_workspace(root)
        self._backends: dict[str, ObjectBackend] = {}
        # Manifest text -> parsed manifest. History walks re-read the same
        # (unchanged) manifest at hundreds of commits; parse each text once.
        self._manifest_cache: dict[str, ObjectManifest] = {}

    # -- construction ---------------------------------------------------- #
    @classmethod
    def init(
        cls,
        path: Path | str = ".",
        *,
        config: RepoConfig | None = None,
    ) -> Repo:
        """Initialize a dataset at `path` inside an existing git/jj repository.

        Creates `tether.toml`, `.tether/objects/`, and `.tether/.gitignore`
        (which ignores the untracked `workspace.toml`).

        Args:
            path: Dataset root; created if it does not exist.
            config: Repository configuration; defaults to `RepoConfig()`.

        Returns:
            The initialized repository.

        Raises:
            ConfigError: If `tether.toml` already exists at `path`.
            VcsError: If no git or jj repository encloses `path`.
        """
        root = Path(path).resolve()
        root.mkdir(parents=True, exist_ok=True)
        if _m.config_path(root).exists():
            raise ConfigError(f"tether already initialized at {root}")
        config = config or RepoConfig()
        ensure_layout(root)
        write_config(root, config)
        vcs = detect_vcs(
            root,
            jj_path=config.vcs.get("jj_path"),
            git_path=config.vcs.get("git_path"),
            prefer=str(config.vcs.get("prefer", "jj")),
        )
        repo = cls(root, config, vcs)
        write_workspace(root, repo.workspace)
        return repo

    @classmethod
    def find(cls, path: Path | str = ".") -> Repo:
        """Open the dataset whose `tether.toml` is at or above `path`.

        Raises:
            ConfigError: If no dataset root is found.
        """
        root = find_dataset_root(Path(path))
        if root is None:
            raise ConfigError(f"no tether dataset found at or above {path}")
        config = read_config(root)
        vcs = detect_vcs(
            root,
            jj_path=config.vcs.get("jj_path"),
            git_path=config.vcs.get("git_path"),
            prefer=str(config.vcs.get("prefer", "jj")),
        )
        return cls(root, config, vcs)

    # -- internals ------------------------------------------------------- #
    def backend_for(self, kind: str) -> ObjectBackend:
        """Return the (cached) backend instance for `kind`.

        Built with `config.backends[kind]` on first use.

        Raises:
            ConfigError: If the kind is unknown or its optional extra is missing.
        """
        backend = self._backends.get(kind)
        if backend is None:
            backend = build_backend(kind, self.config.backends.get(kind, {}))
            self._backends[kind] = backend
        return backend

    def _working_ref_for(self, key: str) -> str:
        """The working-branch name this workspace uses for `key`."""
        return working_ref_name(
            self.config.dataset_id, self.workspace.workspace_id, key
        )

    def _working_ref(self, key: str) -> str | None:
        return self.workspace.working_refs.get(key)

    def _content(self, kind: str, state: State | None) -> State | None:
        """`content_state` for `kind`: what equality and pin ids compare."""
        return content_state(self.backend_for(kind), state)

    def _content_of(self, kind: str, state: State) -> State:
        """Like `_content` for a state that is known to exist."""
        content = content_state(self.backend_for(kind), state)
        assert content is not None
        return content

    def _same(self, kind: str, a: State | None, b: State | None) -> bool:
        return self._content(kind, a) == self._content(kind, b)

    def _dataset_rel(self) -> Path:
        try:
            return self.root.relative_to(self.vcs.root)
        except ValueError:  # pragma: no cover - dataset outside vcs root
            return Path(".")

    def live_workspace_ids(self) -> set[str]:
        """Workspace ids of every live checkout of this dataset (this one included).

        Walks the VCS's workspaces / worktrees (`VcsAdapter.workspace_roots`)
        and reads each one's `.tether/workspace.toml` at the dataset's path.
        Checkouts that never ran tether have no id and contribute nothing.
        """
        ids = {self.workspace.workspace_id}
        rel = self._dataset_rel()
        for root in self.vcs.workspace_roots():
            path = workspace_path(root / rel)
            if not path.is_file():
                continue
            with contextlib.suppress(Exception):
                ids.add(read_workspace(root / rel).workspace_id)
        return ids

    # -- operation log --------------------------------------------------- #
    def ops(self, limit: int | None = None) -> list[OpEntry]:
        """This workspace's operation log, newest first (see `tether.oplog`)."""
        entries = list(reversed(read_ops(self.root)))
        return entries[:limit] if limit else entries

    def _log_op(
        self,
        command: str,
        *,
        plan: Plan | None = None,
        result: Mapping[str, Any] | None = None,
        pre: Mapping[str, Any] | None = None,
        undoes: str | None = None,
    ) -> OpEntry:
        entry = OpEntry.now(
            command,
            plan=plan.to_dict() if plan is not None else None,
            result=dict(result or {}),
            pre=dict(pre or {}),
            undoes=undoes,
        )
        append_op(self.root, entry)
        return entry

    def _manifest_texts(self, keys: Iterable[str]) -> dict[str, str | None]:
        """Current manifest TOML per key (`None` where the object does not exist)."""
        return {
            k: (self.objects[k].to_toml() if k in self.objects else None) for k in keys
        }

    def _vcs_paths(self) -> list[str]:
        # Never include the untracked workspace file; commit the committed
        # surface explicitly (objects dir, listings, the ignore file, config).
        rel = self._dataset_rel()
        paths = [
            (rel / _m.TETHER_DIR / _m.OBJECTS_DIR).as_posix(),
            (rel / _m.TETHER_DIR / _m.GITIGNORE_FILENAME).as_posix(),
            (rel / _m.CONFIG_FILENAME).as_posix(),
        ]
        if any(listings_dir(self.root).glob("*.jsonl")):
            paths.append((rel / _m.TETHER_DIR / _m.LISTINGS_DIR).as_posix())
        return paths

    def _listing_relpath(self, name: str) -> str:
        rel = self._dataset_rel()
        return (rel / _m.TETHER_DIR / _m.LISTINGS_DIR / name).as_posix()

    def _listing_for(self, m: ObjectManifest, rev: str | None) -> str | None:
        """Read the stored listing for a manifest's state (working tree, then VCS)."""
        if m.state is None:
            return None
        backend = self.backend_for(m.kind)
        name = listing_name(
            m.kind, backend.identity(m.locator), self._content_of(m.kind, m.state)
        )
        text = read_listing(self.root, name)
        if text is None and rev is not None:
            text = self.vcs.read_file_at(rev, self._listing_relpath(name))
        return text

    def _objects_reldir(self) -> str:
        rel = self._dataset_rel()
        return (rel / _m.TETHER_DIR / _m.OBJECTS_DIR).as_posix()

    def _parse_manifests(self, files: dict[str, str]) -> dict[str, ObjectManifest]:
        result: dict[str, ObjectManifest] = {}
        for path, text in files.items():
            if not path.endswith(".toml"):
                continue
            m = self._manifest_cache.get(text)
            if m is None:
                m = ObjectManifest.from_toml(text)
                self._manifest_cache[text] = m
            result[m.key] = m
        return result

    def _objects_at(self, rev: str) -> dict[str, ObjectManifest]:
        return self._parse_manifests(self.vcs.files_at(rev, self._objects_reldir()))

    def _iter_history_objects(self) -> Iterator[tuple[str, dict[str, ObjectManifest]]]:
        """Yield ``(commit id, manifests)`` for every commit, via one reader."""
        for rev, files in self.vcs.iter_history_files(self._objects_reldir()):
            yield rev, self._parse_manifests(files)

    def current_manifest_hash(self) -> str:
        """Hash of the committed object set in the working tree."""
        return manifest_hash(self.objects)

    def stale_keys(self) -> list[str]:
        """Forked objects whose committed manifest no longer matches this workspace.

        Each working ref (existing or pending) was forked from, or last
        committed by this workspace at, `workspace.base_states[key]`. If the
        manifest in the working tree now records a different state -- someone
        else committed, or the VCS working copy moved to another commit -- the
        fork no longer starts where the dataset says it does. `track` objects
        write to the base branch and are never stale. Registering or removing
        *other* objects does not make a workspace stale.
        """
        stale: list[str] = []
        keys = set(self.workspace.working_refs) | set(self.workspace.pending_forks)
        for key in sorted(keys):
            m = self.objects.get(key)
            if m is None or m.policy.write == "track" or m.state is None:
                continue
            expected = self.workspace.base_states.get(key)
            if expected is None or not self._same(m.kind, m.state, expected):
                stale.append(key)
        return stale

    def is_stale(self) -> bool:
        """Whether any forked object's manifest changed underneath this workspace.

        A stale workspace refuses writable handles until `new` reforks; see
        `stale_keys`.
        """
        return bool(self.stale_keys())

    def _mark_base_states(self, keys: Iterable[str]) -> None:
        """Record the committed state each of `keys` corresponds to right now."""
        for key in keys:
            m = self.objects.get(key)
            if m is not None and m.state is not None:
                self.workspace.base_states[key] = dict(m.state)
            else:
                self.workspace.base_states.pop(key, None)

    def _fanout_collect(self, fn, keys: list[str]) -> tuple[dict, dict[str, Exception]]:
        """Run ``fn(key)`` per key concurrently; return ``(results, errors)``."""
        results: dict = {}
        errors: dict[str, Exception] = {}
        if not keys:
            return results, errors
        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(keys))) as ex:
            futures = {ex.submit(fn, key): key for key in keys}
            for future in futures:
                key = futures[future]
                try:
                    results[key] = future.result()
                except Exception as exc:
                    errors[key] = exc
        return results, errors

    def _fanout(self, fn, keys: list[str]) -> dict:
        """Run ``fn(key)`` per key concurrently; aggregate failures."""
        results, errors = self._fanout_collect(fn, keys)
        if errors:
            raise MultiObjectError("fan-out failed", errors)
        return results

    # -- add / remove ---------------------------------------------------- #
    def add(
        self,
        key: str,
        kind: str,
        locator: dict,
        *,
        policy: Policy | None = None,
    ) -> ObjectManifest:
        """Register an object in the working copy.

        Writes a manifest with no state yet; the external system is not
        contacted until the next `snapshot`, `status`, or `commit`.

        Args:
            key: Free-form, path-like object key (`"zarr/imaging"`).
            kind: Backend kind (see `tether.backends.known_kinds`).
            locator: Backend-specific fields naming the object (`uri`, `branch`,
                `project_id`, ...); see the backends guide.
            policy: Per-object `Policy`; defaults to `config.defaults`.

        Returns:
            The new manifest.

        Raises:
            ConfigError: If `key` exists, is unsafe, or `kind` cannot be built.
        """
        pre = {"objects": {key: None}, "workspace": self.workspace.to_toml()}
        manifest = self._add(key, kind, locator, policy=policy)
        self._log_op("add", result={"key": key}, pre=pre)
        return manifest

    def _add(
        self,
        key: str,
        kind: str,
        locator: dict,
        *,
        policy: Policy | None = None,
    ) -> ObjectManifest:
        if key in self.objects:
            raise ConfigError(f"object already exists: {key}")
        # Validate the backend kind eagerly.
        self.backend_for(kind)
        manifest = ObjectManifest(
            key=key,
            kind=kind,
            locator=dict(locator),
            policy=policy or self.config.defaults,
        )
        write_object(self.root, manifest)
        self.objects[key] = manifest
        # A re-registered key starts without a working ref; any branch left by
        # its previous incarnation is found by `gc --prune-workspaces`.
        self.workspace.working_refs.pop(key, None)
        self.workspace.pending_forks.pop(key, None)
        self.workspace.fork_points.pop(key, None)
        self.workspace.base_states.pop(key, None)
        write_workspace(self.root, self.workspace)
        return manifest

    def remove(self, key: str) -> None:
        """Unregister an object; the external system (and its pins) is untouched.

        The object's working branch (if any) stays in the workspace state so a
        later `gc` can delete it; `remove` itself never writes to a store.

        Raises:
            ConfigError: If `key` is not registered.
        """
        if key not in self.objects:
            raise ConfigError(f"no such object: {key}")
        pre = {
            "objects": self._manifest_texts([key]),
            "workspace": self.workspace.to_toml(),
        }
        self._remove(key)
        self._log_op("remove", result={"key": key}, pre=pre)

    def _remove(self, key: str) -> None:
        if key not in self.objects:
            raise ConfigError(f"no such object: {key}")
        remove_object(self.root, key)
        del self.objects[key]
        self.workspace.last_snapshot.pop(key, None)
        self.workspace.pending_forks.pop(key, None)  # never created; nothing to gc
        self.workspace.fork_points.pop(key, None)
        self.workspace.base_states.pop(key, None)
        write_workspace(self.root, self.workspace)

    # -- snapshot / status ---------------------------------------------- #
    def snapshot(self) -> dict[str, State]:
        """Fingerprint every object concurrently and cache the result.

        Each object is read at its working ref (or its base ref). The states are
        stored in `workspace.last_snapshot`.

        Returns:
            Current state per object key.

        Raises:
            ImmutableObjectModified: An Observed object with `policy.file ==
                "immutable"` changed since it was committed.
            MultiObjectError: One or more fingerprints failed.
        """
        keys = list(self.objects)

        def fp(key: str) -> State:
            m = self.objects[key]
            backend = self.backend_for(m.kind)
            return backend.fingerprint(m.locator, self._working_ref(key))

        states: dict[str, State] = self._fanout(fp, keys)
        # Enforce immutability for Observed objects.
        for key, state in states.items():
            m = self.objects[key]
            backend = self.backend_for(m.kind)
            eff = effective_capabilities(backend, m.locator, m.policy)
            if (
                tier_of(eff) is Tier.OBSERVED
                and m.policy.file == "immutable"
                and m.state is not None
                and not self._same(m.kind, state, m.state)
            ):
                raise ImmutableObjectModified(
                    f"immutable object {key!r} changed since it was committed; "
                    f"re-register with acceptance to record the new state",
                    key=key,
                    kind=m.kind,
                )
        self.workspace.touch_snapshot(states)
        write_workspace(self.root, self.workspace)
        return states

    def status(self, *, do_snapshot: bool = True) -> StatusReport:
        """Classify every object against its committed manifest.

        Args:
            do_snapshot: Take a fresh `snapshot` first; otherwise reuse the
                cached one (no external systems are contacted).

        Returns:
            The report; `objects` are sorted by key.
        """
        states = self.snapshot() if do_snapshot else self.workspace.last_snapshot
        objects: list[ObjectStatus] = []
        for key in sorted(self.objects):
            m = self.objects[key]
            backend = self.backend_for(m.kind)
            eff = effective_capabilities(backend, m.locator, m.policy)
            current = states.get(key)
            committed = m.state is not None
            changed = (
                committed
                and current is not None
                and not self._same(m.kind, current, m.state)
            )
            report: VerifyReport | None = None
            if self.config.verify_on_status and committed:
                try:
                    report = backend.verify(m.locator, m.state, m.pin, deep=False)
                except Exception as exc:
                    report = VerifyReport(VerifyStatus.UNKNOWN, str(exc))
            objects.append(
                ObjectStatus(
                    key=key,
                    kind=m.kind,
                    tier=tier_of(eff),
                    committed=committed,
                    pinned=m.pin is not None,
                    recoverable=m.recoverable,
                    changed=bool(changed),
                    current_state=current,
                    verify=report,
                )
            )
        stale = self.stale_keys()
        return StatusReport(
            manifest_hash=self.current_manifest_hash(),
            stale=bool(stale),
            objects=objects,
            stale_keys=stale,
        )

    # -- commit ---------------------------------------------------------- #
    def plan_commit(
        self,
        message: str,
        *,
        strict: bool = False,
        force: bool = False,
        do_snapshot: bool = True,
    ) -> Plan:
        """Compute what `commit` would do without writing anywhere.

        Snapshots (read-only), runs quiescence checks, and decides per object:
        `pin` (create `tether.<pin_id>`), `record` (Addressable / `pin =
        "record"`: state only), `record` with `recoverable = false` (Observed),
        or unchanged (a note). The plan carries the captured states and the
        manifest hash so `apply_commit` can refuse a stale plan.

        Args:
            message: VCS commit message (stored in the plan).
            strict: Fail instead of recording Observed objects unrecoverably.
            force: Skip quiescence checks (`NEEDS_QUIESCENCE` backends).
            do_snapshot: Take a fresh `snapshot` first.

        Raises:
            UnpinnedStateError: `strict` and an Observed object changed.
            BackendError: A quiescence check failed.
            MultiObjectError: The snapshot failed for one or more objects.
        """
        states = self.snapshot() if do_snapshot else self.workspace.last_snapshot
        keys = list(self.objects)

        if not force:
            for key in keys:
                m = self.objects[key]
                backend = self.backend_for(m.kind)
                eff = effective_capabilities(backend, m.locator, m.policy)
                if Capability.NEEDS_QUIESCENCE in eff:
                    check = getattr(backend, "check_quiescence", None)
                    if callable(check):
                        check(m.locator, self._working_ref(key))

        plan = Plan(
            command="commit",
            context={
                "message": message,
                "manifest_hash": self.current_manifest_hash(),
                "states": {k: states[k] for k in keys if k in states},
                "workspace_id": self.workspace.workspace_id,
            },
        )
        for key in keys:
            m = self.objects[key]
            backend = self.backend_for(m.kind)
            eff = effective_capabilities(backend, m.locator, m.policy)
            state = states.get(key)
            if state is None:
                plan.notes.append(f"{key}: no fingerprint; skipped")
                continue
            needs_pin = Capability.PIN in eff
            if self._same(m.kind, m.state, state) and (
                m.pin is not None or not needs_pin
            ):
                plan.notes.append(f"{key}: unchanged")
                continue
            if tier_of(eff) is Tier.OBSERVED:
                if strict:
                    raise UnpinnedStateError(
                        f"{key!r} is Observed-tier and cannot be pinned",
                        key=key,
                        kind=m.kind,
                    )
                plan.actions.append(
                    Action(
                        "record",
                        key,
                        m.kind,
                        detail="Observed: recorded, not recoverable",
                        params={"state": state, "recoverable": False},
                    )
                )
            elif needs_pin:
                pin_id = compute_pin_id(
                    m.kind,
                    backend.identity(m.locator),
                    self._content_of(m.kind, state),
                    self.config.dataset_id,
                )
                plan.actions.append(
                    Action(
                        "pin",
                        key,
                        m.kind,
                        target=ref_for_pin(pin_id),
                        detail=f"native ref at {_short_state(state)}",
                        params={"state": state, "pin_id": pin_id},
                    )
                )
            else:
                why = "pin=record" if m.policy.pin == "record" else "Addressable"
                plan.actions.append(
                    Action(
                        "record",
                        key,
                        m.kind,
                        detail=f"{why}: state {_short_state(state)}, no native ref",
                        params={"state": state, "recoverable": True},
                    )
                )
        if plan.actions:
            plan.actions.append(
                Action("vcs-commit", detail=f"{self.vcs.kind} commit: {message!r}")
            )
        return plan

    def apply_commit(
        self,
        plan: Plan,
        *,
        vcs: bool = True,
        verify: bool = True,
    ) -> CommitResult:
        """Execute a plan from `plan_commit`.

        Args:
            plan: The plan to apply.
            vcs: Commit `.tether/` and `tether.toml` to the enclosing repository.
            verify: Re-fingerprint the planned objects and refuse the plan if
                any state or the manifest set changed since it was computed.

        Raises:
            StalePlanError: The plan was computed for a different world.
            BackendError: A pin failed (pins created by this call are released
                best-effort).
        """
        if plan.command != "commit":
            raise ConfigError(f"expected a commit plan, got {plan.command!r}")
        message = str(plan.context.get("message", ""))
        object_actions = [a for a in plan.actions if a.op in ("pin", "record")]
        if verify:
            if plan.context.get("manifest_hash") != self.current_manifest_hash():
                raise StalePlanError(
                    "manifests changed since the plan was made; re-run the plan"
                )
            current = self.snapshot()
            for a in object_actions:
                if not self._same(a.kind, current.get(a.key), a.params.get("state")):
                    raise StalePlanError(
                        f"{a.key!r} changed since the plan was made "
                        f"({_short_state(a.params.get('state'))} -> "
                        f"{_short_state(current.get(a.key))}); re-run the plan"
                    )

        result = CommitResult(message=message)
        for note in plan.notes:
            key, _, why = note.partition(": ")
            if why == "unchanged":
                result.unchanged.append(key)

        created_pins: list[tuple[str, Pin]] = []
        outcomes: dict[str, tuple[State, Pin | None, bool]] = {}
        pre = {
            "vcs": self.vcs.position() if vcs else None,
            "objects": self._manifest_texts(a.key for a in object_actions),
            "workspace": self.workspace.to_toml(),
        }
        try:
            for a in object_actions:
                m = self.objects[a.key]
                backend = self.backend_for(m.kind)
                state = dict(a.params["state"])
                if a.op == "pin":
                    pin = backend.pin(m.locator, state, str(a.params["pin_id"]))
                    created_pins.append((a.key, pin))
                    outcomes[a.key] = (state, pin, True)
                    result.pinned[a.key] = pin
                else:
                    recoverable = bool(a.params.get("recoverable", True))
                    outcomes[a.key] = (state, None, recoverable)
                    if recoverable:
                        result.pinned[a.key] = None
                    else:
                        result.unrecoverable.append(a.key)
        except Exception:
            self._rollback_pins(created_pins)
            raise

        # Persist manifests (and listings for backends that provide them).
        for key, (state, pin, recoverable) in outcomes.items():
            m = self.objects[key]
            updated = m.with_pin(state=state, pin=pin, recoverable=recoverable)
            self.objects[key] = updated
            write_object(self.root, updated)
            backend = self.backend_for(m.kind)
            if Capability.DIFF in effective_capabilities(backend, m.locator, m.policy):
                text = backend.listing(m.locator, state)
                if text is not None:
                    name = listing_name(
                        m.kind,
                        backend.identity(m.locator),
                        self._content_of(m.kind, state),
                    )
                    write_listing(self.root, name, text)

        # Commit when something was pinned, and also when the manifests are
        # already dirty in the working tree (an undone commit, an `add`, an
        # `import`): the dataset commit is what makes them history.
        if vcs and (outcomes or self.vcs.dirty(self._vcs_paths())):
            result.vcs_commit = self.vcs.commit(self._vcs_paths(), message)

        # What this workspace just committed is, by definition, not stale.
        self._mark_base_states(
            k
            for k in outcomes
            if k in self.workspace.working_refs or k in self.workspace.pending_forks
        )
        write_workspace(self.root, self.workspace)
        if outcomes or result.vcs_commit:
            self._log_op(
                "commit",
                plan=plan,
                result={
                    "vcs_commit": result.vcs_commit,
                    "pinned": {
                        k: (p.id if p else None) for k, p in result.pinned.items()
                    },
                    "unrecoverable": list(result.unrecoverable),
                },
                pre=pre,
            )
        if outcomes and self.config.new_auto_fork:
            # jj-style: every commit leaves you on a fresh working copy.
            self.new()
        return result

    def commit(
        self,
        message: str,
        *,
        vcs: bool = True,
        strict: bool = False,
        force: bool = False,
        do_snapshot: bool = True,
    ) -> CommitResult:
        """Pin and record every object's current state, then commit the manifests.

        Equivalent to `apply_commit(plan_commit(...))`. For each object whose
        state changed: `PIN`-capable backends create the native ref
        `tether.<pin_id>` (idempotent), Addressable and `pin = "record"`
        objects have their state recorded, and Observed objects are recorded
        with `recoverable = False`. Backends that provide a listing have it
        stored under `.tether/listings/`. If any pin fails, pins created by this
        call are released best-effort. With `config.new_auto_fork`, `new` runs
        afterwards.

        Args:
            message: VCS commit message.
            vcs: Commit `.tether/` and `tether.toml` to the enclosing repository.
            strict: Fail instead of recording Observed objects unrecoverably.
            force: Skip quiescence checks (`NEEDS_QUIESCENCE` backends).
            do_snapshot: Take a fresh `snapshot` first.

        Returns:
            What was pinned, recorded, or skipped, and the VCS commit id.

        Raises:
            UnpinnedStateError: `strict` and an Observed object changed.
            BackendError: A quiescence check or pin failed.
            MultiObjectError: The snapshot failed for one or more objects.
        """
        plan = self.plan_commit(
            message, strict=strict, force=force, do_snapshot=do_snapshot
        )
        return self.apply_commit(plan, vcs=vcs, verify=False)

    def _rollback_pins(self, created: list[tuple[str, Pin]]) -> None:
        for key, pin in created:
            m = self.objects.get(key)
            if m is None:
                continue
            # Best-effort compensation; a failed unpin is reconciled by gc.
            with contextlib.suppress(Exception):
                self.backend_for(m.kind).unpin(m.locator, pin)

    # -- new (fork working refs) ---------------------------------------- #
    def plan_new(
        self,
        rev: str | None = None,
        *,
        keep: bool = False,
        eager: bool | None = None,
        discard: bool = False,
    ) -> Plan:
        """Compute what `new` would do without writing anywhere.

        Reads the manifests at `rev` (or the working tree) and decides per
        `FORK`-capable object: `track` (use the base branch), `fork` now, or
        `defer-fork` (create the branch on the first writable `open`), or skip
        (no committed state yet). `apply_new` moves the VCS working copy to
        `rev` first when one is given.

        Forks are deferred by default (`config.new_fork == "lazy"`) when the
        source is a native pin: a pin is durable, so the branch can be created
        whenever a write actually happens, and workspaces that never write
        leave nothing behind. A `pin = "record"` object forks immediately even
        in lazy mode: its recorded state has no native ref, so the branch
        itself is what keeps the snapshot/version from expiring.

        A working branch this workspace already has is *reset* by the fork
        (now, or when it is materialized). If its head holds writes that were
        never committed -- its content differs from the state this workspace
        last committed or forked at -- the plan carries a `refuse` action
        instead and `apply_new` raises, unless `discard` is set. Committing
        first pins the writes; `discard` throws them away on purpose.

        Args:
            rev: Revision whose manifests to fork from (`None`: working tree).
            keep: Only refresh the baseline; keep working refs.
            eager: Fork every object during `new`; default from `config.new_fork`.
            discard: Reset working branches even if they hold unpinned writes.
        """
        objects = self._objects_at(self.vcs.resolve(rev)) if rev else self.objects
        eager = (self.config.new_fork == "eager") if eager is None else eager
        plan = Plan(
            command="new",
            context={
                "rev": rev,
                "keep": keep,
                "eager": eager,
                "discard": discard,
                "manifest_hash": manifest_hash(objects),
                "workspace_id": self.workspace.workspace_id,
            },
        )
        if keep:
            plan.notes.append("keep: refresh the baseline only; working refs unchanged")
            return plan
        for key in sorted(objects):
            m = objects[key]
            backend = self.backend_for(m.kind)
            eff = effective_capabilities(backend, m.locator, m.policy)
            if Capability.FORK not in eff:
                continue
            if m.policy.write == "track":
                branch = str(m.locator.get("branch", "main"))
                plan.actions.append(
                    Action("track", key, m.kind, target=branch, detail="write=track")
                )
                continue
            if m.state is None:
                plan.notes.append(
                    f"{key}: nothing committed yet; fork after first commit"
                )
                continue
            name = self._working_ref_for(key)
            # An existing branch of this workspace is reset by the fork (now or
            # when materialized). Record its head so the op log can restore it.
            existing = self.workspace.working_refs.get(key)
            head: State | None = None
            if existing is not None and existing != m.locator.get("branch", "main"):
                try:
                    head = backend.fingerprint(m.locator, existing)
                except TetherError:
                    existing = None  # gone already; nothing to reset
            if m.pin is not None:
                source = {"pin": m.pin.to_dict()}
                detail = f"from pin {m.pin.ref}"
                durable = True
            elif Capability.ADDRESSABLE in eff:
                source = {"state": m.state}
                detail = f"from recorded state {_short_state(m.state)} (no pin)"
                durable = False
            else:
                plan.notes.append(f"{key}: no pin and not addressable; cannot fork")
                continue
            params: dict[str, Any] = dict(source)
            if existing is not None:
                params["existing"] = existing
                params["head"] = head
                detail += f"; resets {existing}"
                # Anything on the branch beyond what this workspace last
                # committed (or forked from) is about to be thrown away.
                known = [
                    self.workspace.base_states.get(key),
                    self.objects[key].state if key in self.objects else None,
                    self.workspace.fork_points.get(key),
                ]
                unpinned = head is not None and not any(
                    self._same(m.kind, head, k) for k in known if k is not None
                )
                if unpinned and not discard:
                    plan.actions.append(
                        Action(
                            "refuse",
                            key,
                            m.kind,
                            target=existing,
                            detail=(
                                f"{existing} has writes since this workspace last "
                                f"committed ({_short_state(head)}); commit them, or "
                                "pass --discard to throw them away"
                            ),
                            params=params,
                        )
                    )
                    continue
                if unpinned:
                    detail += f", discarding its writes ({_short_state(head)})"
            if eager or not durable:
                if not durable and not eager:
                    detail += "; forked now so the state cannot expire"
                plan.actions.append(
                    Action(
                        "fork", key, m.kind, target=name, detail=detail, params=params
                    )
                )
            else:
                plan.actions.append(
                    Action(
                        "defer-fork",
                        key,
                        m.kind,
                        target=name,
                        detail=f"{detail}; created on first writable open",
                        params=params,
                    )
                )
        return plan

    def apply_new(self, plan: Plan, *, verify: bool = True) -> None:
        """Execute a plan from `plan_new`: move the VCS working copy, fork, record refs.

        Raises:
            StalePlanError: The manifests at the target differ from the plan's.
            MultiObjectError: A pin is missing or a fork failed.
        """
        if plan.command != "new":
            raise ConfigError(f"expected a new plan, got {plan.command!r}")
        refused = [a for a in plan.actions if a.op == "refuse"]
        if refused:
            raise TetherError(
                "refusing to reset working branches with unpinned writes:\n"
                + "\n".join(f"  {a.key}: {a.detail}" for a in refused)
            )
        rev = plan.context.get("rev")
        if verify:
            # Check the target *before* touching the VCS working copy, so a
            # stale plan leaves the checkout where it was.
            target = (
                self._objects_at(self.vcs.resolve(str(rev))) if rev else self.objects
            )
            if plan.context.get("manifest_hash") != manifest_hash(target):
                raise StalePlanError(
                    "manifests at the target differ from the plan; re-run the plan"
                )
        pre = {"workspace": self.workspace.to_toml(), "vcs": self.vcs.position()}
        if rev:
            self.vcs.new(str(rev))
            self.objects = read_objects(self.root)
        if plan.context.get("keep"):
            self._mark_base_states(
                set(self.workspace.working_refs) | set(self.workspace.pending_forks)
            )
            write_workspace(self.root, self.workspace)
            self._log_op("new", plan=plan, result={"vcs": self.vcs.position()}, pre=pre)
            return

        working_refs: dict[str, str] = {}
        pending: dict[str, str] = {}
        forks = {a.key: a for a in plan.actions if a.op == "fork"}
        for a in plan.actions:
            if a.op == "track":
                working_refs[a.key] = a.target
            elif a.op == "defer-fork":
                pending[a.key] = a.target

        def fork_one(key: str) -> str:
            m = self.objects[key]
            return self._fork_from_manifest(m, forks[key].target)

        # Fork concurrently; a failure for one object must not hide the branches
        # created for the others, so record everything that succeeded before
        # reporting what did not. A second `new` completes the job (existing
        # branches are reset onto the pin, not duplicated).
        forked, errors = self._fanout_collect(fork_one, list(forks))
        working_refs.update(forked)

        # Keep refs of removed objects around until `gc` deletes their branches.
        leftovers = {
            k: v
            for k, v in self.workspace.working_refs.items()
            if k not in self.objects
        }
        self.workspace.working_refs = {**leftovers, **working_refs}
        self.workspace.pending_forks = pending
        # Fork points: what each branch was created from (promote's baseline).
        fork_points = {
            k: v for k, v in self.workspace.fork_points.items() if k in leftovers
        }
        for key in forked:
            state = self.objects[key].state
            if state is not None:
                fork_points[key] = dict(state)
        self.workspace.fork_points = fork_points
        self.workspace.base_states = {
            k: v for k, v in self.workspace.base_states.items() if k in leftovers
        }
        self._mark_base_states(set(working_refs) | set(pending))
        write_workspace(self.root, self.workspace)
        # What this op did to the stores, and what it replaced, for undo.
        reset = {
            k: a.params
            for k, a in forks.items()
            if k in forked and a.params.get("existing")
        }
        pre["heads"] = {k: p.get("head") for k, p in reset.items()}
        self._log_op(
            "new",
            plan=plan,
            result={
                "vcs": self.vcs.position(),
                "created": sorted(k for k in forked if k not in reset),
                "reset": sorted(reset),
                "working_refs": dict(working_refs),
                "pending_forks": dict(pending),
                "failed": sorted(errors),
            },
            pre=pre,
        )
        if errors:
            raise MultiObjectError(
                f"could not fork working refs for {', '.join(sorted(errors))} "
                f"({len(forked)} of {len(forks)} forked and recorded; run `new` again "
                "-- it resets those branches too, so do not write to them first)",
                errors,
            )

    def _fork_from_manifest(self, m: ObjectManifest, name: str) -> str:
        """Create working branch `name` from a manifest's pin (or recorded state)."""
        backend = self.backend_for(m.kind)
        assert m.state is not None
        if m.pin is not None:
            report = backend.verify(m.locator, m.state, m.pin, deep=False)
            if report.status is VerifyStatus.MISSING:
                raise TetherError(f"pin missing: {report.message}")
            return backend.fork(m.locator, m.pin, name)
        report = backend.verify(m.locator, m.state, None, deep=True)
        if report.status is VerifyStatus.MISSING:
            raise TetherError(f"recorded state is gone: {report.message}")
        return backend.fork(m.locator, m.state, name)

    def _forget_working_state(self, key: str) -> None:
        """Drop everything this workspace knows about `key`'s working branch."""
        for table in ("working_refs", "pending_forks", "base_states", "fork_points"):
            getattr(self.workspace, table).pop(key, None)

    def materialize_fork(self, key: str) -> str:
        """Create the deferred working branch for `key` now and return it.

        Called by `open` on the first writable handle; also useful to
        pre-create branches for a job. No-op when the branch already exists.

        Raises:
            ConfigError: Unknown key, or no fork is pending for it.
            StaleWorkingCopyError: The workspace is stale; run `new` first.
            TetherError: The pin (or recorded state) to fork from is gone.
        """
        existing = self.workspace.working_refs.get(key)
        if existing is not None:
            return existing
        name = self.workspace.pending_forks.get(key)
        if name is None:
            raise ConfigError(f"no working branch pending for {key!r}; run `new`")
        stale = self.stale_keys()
        if stale:
            raise StaleWorkingCopyError(
                f"working copy is stale: the committed state of {', '.join(stale)} "
                "changed since `new`; run `tether new` before writing"
            )
        m = self.objects[key]
        backend = self.backend_for(m.kind)
        pre: dict[str, Any] = {"workspace": self.workspace.to_toml()}
        # A branch left by an earlier `new` of this workspace is about to be
        # reset; remember its head so undo can put it back.
        if name in backend.list_working_refs(m.locator):
            pre["heads"] = {key: backend.fingerprint(m.locator, name)}
        ref = self._fork_from_manifest(m, name)
        self.workspace.working_refs[key] = ref
        self.workspace.pending_forks.pop(key, None)
        if m.state is not None:
            self.workspace.fork_points[key] = dict(m.state)
        self._mark_base_states([key])
        write_workspace(self.root, self.workspace)
        self._log_op(
            "fork",
            result={"key": key, "ref": ref, "kind": m.kind, "locator": dict(m.locator)},
            pre=pre,
        )
        return ref

    def new(
        self,
        rev: str | None = None,
        *,
        keep: bool = False,
        eager: bool | None = None,
        discard: bool = False,
    ) -> None:
        """Start working on top of `rev`: set up writable refs off its pins.

        Equivalent to `apply_new(plan_new(...))`. For every `FORK`-capable
        object, `track` policy uses the locator's branch; otherwise a branch
        named `working_ref_name(dataset_id, workspace_id, key)` is forked from the
        pin -- by default *lazily*, on the first writable `open` (see `plan_new`), or
        during `new` with `eager`. `pin = "record"` objects always fork now,
        from their recorded state. Objects with no committed state yet are
        skipped. Forks run concurrently.

        Args:
            rev: Move the VCS working copy here first (`jj new`; in git, `switch`
                to a branch or onto a `tether/<rev12>` branch for a commit)
                and reload the manifests; `None` keeps the current commit.
            keep: Only refresh the stale-detection baseline; keep working refs.
            eager: Create every branch now; default `config.new_fork == "eager"`.
            discard: Reset working branches that hold unpinned writes (see
                `plan_new`); without it such a `new` is refused.

        Raises:
            TetherError: A working branch holds unpinned writes and `discard`
                is not set.
            MultiObjectError: A pin is missing or a fork failed.
        """
        self.apply_new(
            self.plan_new(rev, keep=keep, eager=eager, discard=discard), verify=False
        )

    # -- open ------------------------------------------------------------ #
    def open(
        self,
        key: str,
        *,
        rev: str | None = None,
        read_only: bool | None = None,
    ) -> Handle:
        """Return a native handle for an object.

        Args:
            key: Object key.
            rev: VCS revision whose pinned (or recorded Addressable) state to
                open read-only. Defaults to `$TETHER_REV` when set.
            read_only: Force read-only or writable. Defaults to writable for
                Forkable objects (at their working ref) and read-only otherwise.
                A writable open creates the working branch first when `new`
                deferred it (lazy forking) -- the one store write outside
                `commit`, `new`, and `gc`.

        Returns:
            A backend-specific `tether.handles.Handle`.

        Raises:
            ConfigError: Unknown key (or absent at `rev`).
            StaleWorkingCopyError: Writable open while `is_stale`, or no
                working ref exists yet (run `new`).
            CapabilityError: Writable open on a non-Forkable object, or a
                revision open on an Observed object.
            TetherError: The object has no committed state at `rev`.
        """
        if key not in self.objects and rev is None:
            raise ConfigError(f"no such object: {key}")
        rev = rev if rev is not None else os.environ.get(TETHER_REV_ENV)

        if rev is not None:
            return self._open_at_rev(key, rev)

        m = self.objects[key]
        backend = self.backend_for(m.kind)
        eff = effective_capabilities(backend, m.locator, m.policy)
        forkable = Capability.FORK in eff
        want_write = (not read_only) if read_only is not None else forkable

        if want_write:
            if not forkable:
                raise CapabilityError(
                    f"{key!r} ({m.kind}, tier {tier_of(eff).value}) does not "
                    f"support writable handles; open with read_only=True",
                    key=key,
                    kind=m.kind,
                )
            stale = self.stale_keys()
            if stale:
                raise StaleWorkingCopyError(
                    f"working copy is stale: the committed state of {', '.join(stale)} "
                    "changed since this workspace forked; run `tether new` to refork "
                    "before writing"
                )
            working_ref = self._working_ref(key)
            if working_ref is None and key in self.workspace.pending_forks:
                working_ref = self.materialize_fork(key)  # lazy fork: first write
            if working_ref is None:
                raise StaleWorkingCopyError(
                    f"no working ref for {key!r}; run `tether new` first"
                )
            return backend.open(m.locator, working_ref, read_only=False)

        # Read-only handle at the current working ref / base. A detached base
        # (`at` in the locator) reads at that state rather than the branch head.
        working_ref = self._working_ref(key)
        detached = working_ref is None and base_at(m.locator) is not None
        if detached and Capability.ADDRESSABLE in eff:
            state = m.state or backend.fingerprint(m.locator, None)
            return backend.open(m.locator, state, read_only=True)
        return backend.open(m.locator, working_ref, read_only=True)

    def _open_at_rev(self, key: str, rev: str) -> Handle:
        objects = self._objects_at(self.vcs.resolve(rev))
        if key not in objects:
            raise ConfigError(f"object {key!r} does not exist at {rev}")
        m = objects[key]
        backend = self.backend_for(m.kind)
        eff = effective_capabilities(backend, m.locator, m.policy)
        if m.state is None:
            raise TetherError(f"{key!r} has no committed state at {rev}")
        if m.pin is not None and Capability.PIN in eff:
            return backend.open(m.locator, m.pin, read_only=True)
        if Capability.ADDRESSABLE in eff:
            return backend.open(m.locator, m.state, read_only=True)
        raise CapabilityError(
            f"{key!r} ({m.kind}) is Observed-tier; its state at {rev} is not "
            f"recoverable",
            key=key,
            kind=m.kind,
        )

    # -- history --------------------------------------------------------- #
    def history(
        self,
        key: str,
        *,
        ref: str | None = None,
        limit: int = 20,
    ) -> list[HistoryEntry]:
        """List an object's native history (snapshots, versions, commits).

        Newest first, starting at ``ref`` (default: the current working ref, or
        the locator's base branch). Entry ids are valid values for the locator's
        ``at`` field. Requires the backend's ``HISTORY`` capability.

        Raises:
            ConfigError: Unknown key.
            CapabilityError: The backend cannot list history.
        """
        if key not in self.objects:
            raise ConfigError(f"no such object: {key}")
        m = self.objects[key]
        backend = self.backend_for(m.kind)
        eff = effective_capabilities(backend, m.locator, m.policy)
        if Capability.HISTORY not in eff:
            raise CapabilityError(
                f"{key!r} ({m.kind}) cannot list history", key=key, kind=m.kind
            )
        start = ref if ref is not None else self._working_ref(key)
        return backend.history(m.locator, start, limit)

    def history_for(
        self,
        kind: str,
        locator: dict,
        *,
        ref: str | None = None,
        limit: int = 20,
    ) -> list[HistoryEntry]:
        """List native history for a not-yet-registered locator (see `history`)."""
        backend = self.backend_for(kind)
        if Capability.HISTORY not in backend.capabilities:
            raise CapabilityError(f"{kind} backend cannot list history", kind=kind)
        return backend.history(dict(locator), ref, limit)

    # -- verify ---------------------------------------------------------- #
    def verify(
        self,
        *,
        rev: str | None = None,
        deep: bool = False,
        all_history: bool = False,
    ) -> dict[str, VerifyReport]:
        """Check that recorded states and pins still resolve.

        Args:
            rev: Verify the manifests at this revision instead of the working tree.
            deep: Actually open recorded states instead of the cheap check
                (turns `UNKNOWN` into `OK` / `MISSING`).
            all_history: Verify every commit in the repository; labels become
                `"<commit12>:<key>"`. Pinned and recorded (pin-less) states are
                both checked; Observed records are skipped. History is streamed
                through one object reader and each distinct record is verified
                once.

        Returns:
            A `VerifyReport` per label (object key, or commit-prefixed key).
            Backend `TetherError`s are reported as `UNKNOWN`.
        """
        if all_history:
            return self._verify_all_history(deep)
        objects = (
            self._objects_at(self.vcs.resolve(rev)) if rev is not None else self.objects
        )
        return self._verify_manifests(
            {key: m for key, m in objects.items() if m.state is not None}, deep
        )

    def _verify_manifests(
        self, targets: dict[str, ObjectManifest], deep: bool
    ) -> dict[str, VerifyReport]:
        """Verify many manifests concurrently, once per distinct (system, state, pin).

        ``targets`` maps a report label to a manifest. Identical records under
        different labels (the same pin at many commits) share one backend call.
        """
        unique: dict[str, tuple[ObjectManifest, list[str]]] = {}
        for label, m in targets.items():
            backend = self.backend_for(m.kind)
            sig = _m.canonical_bytes(
                [
                    m.kind,
                    backend.identity(m.locator),
                    m.state,
                    m.pin.to_dict() if m.pin else None,
                ]
            ).decode()
            unique.setdefault(sig, (m, []))[1].append(label)

        def verify_one(sig: str) -> VerifyReport:
            m = unique[sig][0]
            assert m.state is not None
            backend = self.backend_for(m.kind)
            try:
                return backend.verify(m.locator, m.state, m.pin, deep=deep)
            except TetherError as exc:
                return VerifyReport(VerifyStatus.UNKNOWN, str(exc))

        results = self._fanout(verify_one, list(unique))
        reports: dict[str, VerifyReport] = {}
        for sig, (_, labels) in unique.items():
            for label in labels:
                reports[label] = results[sig]
        # Preserve the caller's label order.
        return {label: reports[label] for label in targets}

    def _verify_all_history(self, deep: bool) -> dict[str, VerifyReport]:
        targets: dict[str, ObjectManifest] = {}
        for rev, objects in self._iter_history_objects():
            for key, m in objects.items():
                # Pinned states and recorded (Addressable / pin=record) states
                # are both promises; Observed records are not recoverable.
                if m.state is None or (m.pin is None and not m.recoverable):
                    continue
                targets[f"{rev[:12]}:{key}"] = m
        return self._verify_manifests(targets, deep)

    # -- gc -------------------------------------------------------------- #
    def plan_gc(
        self,
        *,
        prune_workspaces: bool = False,
        keep_workspaces: set[str] | None = None,
        force_prune: bool = False,
    ) -> Plan:
        """Compute what `gc` would release without writing anywhere.

        By default `gc` releases only tether's own *refs*: `unpin` native pins
        no manifest in VCS history (or the working tree) references,
        `forget-working-ref` for this workspace's refs whose object was
        removed (the native branch is left alone), and `delete-listing` for
        `.tether/listings/` files no manifest names.

        With `prune_workspaces`, every `tether.ws.*` branch in each system is
        considered: branches of workspaces that no longer exist (every live jj
        workspace / git worktree of this repository is kept automatically via
        `live_workspace_ids`; `keep_workspaces` adds ids that are live elsewhere,
        e.g. on another machine) and this workspace's branches no current
        object uses. A branch is planned for
        `delete-branch` only when nothing on it would be lost: its head state is
        natively pinned by some manifest in history, or equals the base
        branch's head. Otherwise it gets a `keep-branch` note (unpinned writes,
        a pin-less recorded state, or a `BRANCH_IS_STORAGE` backend such as
        Neon). `force_prune` deletes those too.
        """
        referenced: dict[str, set[str]] = {}

        def key_for(backend: ObjectBackend, locator: dict) -> str:
            ident = backend.identity(locator)
            return f"{backend.kind}|{_m.canonical_bytes(ident).decode()}"

        history_manifests: list[ObjectManifest] = []
        all_manifests: list[ObjectManifest] = []
        seen: set[str] = set()
        for _rev, objects in self._iter_history_objects():
            for m in objects.values():
                all_manifests.append(m)
                if m.pin is None:
                    continue
                backend = self.backend_for(m.kind)
                referenced.setdefault(key_for(backend, m.locator), set()).add(m.pin.id)
                sig = f"{m.kind}:{m.pin.id}:{key_for(backend, m.locator)}"
                if sig not in seen:
                    seen.add(sig)
                    history_manifests.append(m)
        for m in self.objects.values():
            if m.pin is not None:
                backend = self.backend_for(m.kind)
                referenced.setdefault(key_for(backend, m.locator), set()).add(m.pin.id)

        plan = Plan(
            command="gc",
            context={
                "workspace_id": self.workspace.workspace_id,
                "prune_workspaces": prune_workspaces,
                "keep_workspaces": sorted(keep_workspaces or ()),
                "force_prune": force_prune,
            },
        )

        # Unpin native refs not referenced by any manifest.
        checked_systems: set[str] = set()
        for m in history_manifests:
            backend = self.backend_for(m.kind)
            if Capability.PIN not in backend.capabilities:
                continue
            sys_key = key_for(backend, m.locator)
            if sys_key in checked_systems:
                continue
            checked_systems.add(sys_key)
            live = backend.list_pins(m.locator)
            # Only this dataset's namespace: pins of other datasets sharing
            # the store (or refs that are not tether pins) are never touched.
            mine = {p for p in live if pin_dataset(p) == self.config.dataset_id}
            foreign = len(live) - len(mine)
            if foreign:
                plan.notes.append(
                    f"{m.key}: {foreign} pin(s) of other datasets left alone"
                )
            keep = referenced.get(sys_key, set())
            for pid in sorted(mine - keep):
                plan.actions.append(
                    Action(
                        "unpin",
                        m.key,
                        m.kind,
                        target=ref_for_pin(pid),
                        detail="no manifest in history references it",
                        params={"locator": m.locator, "pin_id": pid},
                    )
                )

        # This workspace's working refs whose object was removed: forget the
        # ref; the native branch is only deleted by --prune-workspaces.
        for key, ref in sorted(self.workspace.working_refs.items()):
            if key in self.objects:
                continue
            plan.actions.append(
                Action(
                    "forget-working-ref",
                    key,
                    target=ref,
                    detail="object removed; branch left in place "
                    "(gc --prune-workspaces evaluates it)",
                )
            )

        if prune_workspaces:
            live = self.live_workspace_ids()
            keep = set(keep_workspaces or ()) | live
            plan.context["live_workspaces"] = sorted(w[:8] for w in live)
            others = sorted(w[:8] for w in live if w != self.workspace.workspace_id)
            if others:
                plan.notes.append(f"keeping live workspaces: {', '.join(others)}")
            self._plan_prune_workspaces(
                plan,
                [*all_manifests, *self.objects.values()],
                key_for,
                keep,
                force_prune,
            )

        # Listings no manifest (in history or the working tree) names.
        wanted: set[str] = set()
        for m in [*all_manifests, *self.objects.values()]:
            if m.state is not None:
                backend = self.backend_for(m.kind)
                wanted.add(
                    listing_name(
                        m.kind,
                        backend.identity(m.locator),
                        self._content_of(m.kind, m.state),
                    )
                )
        for path in sorted(listings_dir(self.root).glob("*.jsonl")):
            if path.name not in wanted:
                plan.actions.append(
                    Action("delete-listing", target=path.name, detail="unreferenced")
                )
        return plan

    def _plan_prune_workspaces(
        self,
        plan: Plan,
        manifests: list[ObjectManifest],
        key_for: Callable[[ObjectBackend, dict], str],
        keep_workspaces: set[str],
        force: bool,
    ) -> None:
        """Add `delete-branch` / `keep-branch` actions for stray `tether.ws.*` branches.

        A branch is safe to delete when its head state is natively pinned by
        some manifest (a tag holds everything on it) or equals the base
        branch's head (nothing was written). Anything else -- unpinned writes,
        a state that is only *recorded* (`pin = "record"`), or a backend whose
        branches are the storage itself -- is kept unless ``force``.
        """
        mine = self.workspace.workspace_id[:8]
        keep_ids = {w[:8] for w in keep_workspaces} | {mine}
        in_use = {
            ref
            for key, ref in self.workspace.working_refs.items()
            if key in self.objects
        }

        # States that hold data per system: pinned (safe) vs merely recorded.
        pinned: dict[str, list[State | None]] = {}
        recorded: dict[str, list[State | None]] = {}
        for m in manifests:
            if m.state is None:
                continue
            sys_key = key_for(self.backend_for(m.kind), m.locator)
            bucket = pinned if m.pin is not None else recorded
            content = self._content(m.kind, m.state)
            if content not in bucket.setdefault(sys_key, []):
                bucket[sys_key].append(content)

        systems_seen: set[str] = set()
        for key in sorted(self.objects):
            m = self.objects[key]
            backend = self.backend_for(m.kind)
            eff = effective_capabilities(backend, m.locator, m.policy)
            if Capability.FORK not in eff:
                continue
            sys_key = key_for(backend, m.locator)
            if sys_key in systems_seen:
                continue
            systems_seen.add(sys_key)
            storage = Capability.BRANCH_IS_STORAGE in eff
            base_head: State | None = None
            foreign = 0
            for ref in sorted(backend.list_working_refs(m.locator)):
                ws = working_ref_workspace(ref)
                if ws is None:
                    continue
                if working_ref_dataset(ref) != self.config.dataset_id:
                    foreign += 1  # another dataset's workspace; not ours to judge
                    continue
                if ws == mine:
                    if ref in in_use:
                        continue
                    origin = "this workspace's branch; no object uses it"
                elif ws in keep_ids:
                    continue
                else:
                    origin = f"workspace {ws} (not kept)"

                # Why deleting would be safe -- or why it would not.
                reason: str | None = None
                safe = ""
                full_head: State | None = None
                if storage:
                    reason = "branch is storage; deleting reclaims its data"
                else:
                    try:
                        full_head = backend.fingerprint(m.locator, ref)
                        head = self._content(m.kind, full_head)
                        if base_head is None:
                            base_head = self._content(
                                m.kind, backend.fingerprint(m.locator, None)
                            )
                    except TetherError as exc:
                        head, reason = None, f"cannot read head: {exc}"
                    if head is not None:
                        if head in pinned.get(sys_key, []):
                            safe = "head is pinned"
                        elif head == base_head:
                            safe = "head equals the base branch"
                        elif head in recorded.get(sys_key, []):
                            reason = (
                                "holds a pin-less recorded state "
                                f"({_short_state(head)})"
                            )
                        else:
                            reason = f"has unpinned writes ({_short_state(head)})"

                if reason is None:
                    plan.actions.append(
                        Action(
                            "delete-branch",
                            key,
                            m.kind,
                            target=ref,
                            detail=f"{origin}; {safe}",
                            params={"locator": m.locator, "head": full_head},
                        )
                    )
                elif force:
                    plan.actions.append(
                        Action(
                            "delete-branch",
                            key,
                            m.kind,
                            target=ref,
                            detail=f"{origin}; FORCED although it {reason}",
                            params={
                                "locator": m.locator,
                                "forced": True,
                                "head": full_head,
                            },
                        )
                    )
                else:
                    plan.actions.append(
                        Action(
                            "keep-branch",
                            key,
                            m.kind,
                            target=ref,
                            detail=f"{origin}; kept: {reason} (--force-prune deletes)",
                        )
                    )

            if foreign:
                plan.notes.append(
                    f"{key}: {foreign} working branch(es) of other datasets left alone"
                )

    def apply_gc(self, plan: Plan) -> GcReport:
        """Execute a plan from `plan_gc`.

        Unpins, deletes branches, forgets working refs, and deletes listings as
        planned (`keep-branch` actions are reported, not executed). Backend
        failures are aggregated into `MultiObjectError` after every action has
        been attempted.
        """
        if plan.command != "gc":
            raise ConfigError(f"expected a gc plan, got {plan.command!r}")
        report = GcReport(dry_run=False, plan=plan)
        errors: dict[str, Exception] = {}
        forgot = False
        pre = {"workspace": self.workspace.to_toml()}
        for a in plan.actions:
            try:
                if a.op == "unpin":
                    backend = self.backend_for(a.kind)
                    pid = str(a.params["pin_id"])
                    backend.unpin(dict(a.params["locator"]), Pin(id=pid, ref=a.target))
                    report.unpinned.setdefault(a.kind, []).append(pid)
                elif a.op == "forget-working-ref":
                    self.workspace.working_refs.pop(a.key, None)
                    forgot = True
                    report.forgotten_working_refs.setdefault(a.key, []).append(a.target)
                elif a.op == "delete-branch":
                    backend = self.backend_for(a.kind)
                    backend.delete_working_ref(dict(a.params["locator"]), a.target)
                    report.deleted_working_refs.setdefault(a.key, []).append(a.target)
                elif a.op == "keep-branch":
                    report.kept_working_refs.setdefault(a.key, []).append(a.target)
                elif a.op == "delete-listing":
                    (listings_dir(self.root) / a.target).unlink(missing_ok=True)
                    report.deleted_listings.append(a.target)
            except Exception as exc:
                errors[f"{a.op} {a.target}"] = exc
        if forgot:
            write_workspace(self.root, self.workspace)
        if plan.writes:
            self._log_op(
                "gc",
                plan=plan,
                result={
                    **_report_dict(report),
                    "failed": {k: str(v) for k, v in errors.items()},
                },
                pre=pre,
            )
        if errors:
            raise MultiObjectError("gc failed for some actions", errors)
        return report

    def gc(
        self,
        *,
        dry_run: bool = True,
        prune_workspaces: bool = False,
        keep_workspaces: set[str] | None = None,
        force_prune: bool = False,
    ) -> GcReport:
        """Release native pins that no manifest in VCS history references.

        Equivalent to `plan_gc` followed by `apply_gc` unless `dry_run`. Also
        forgets this workspace's working refs for removed objects and deletes
        `.tether/listings/` files no manifest names. With `prune_workspaces`,
        stray `tether.ws.*` branches (other workspaces' except
        `keep_workspaces`, and this workspace's unused ones) are deleted when
        their head is pinned or equals the base head, and kept otherwise unless
        `force_prune`.

        Args:
            dry_run: Only report what would be released (the report carries the plan).
            prune_workspaces: Also evaluate stray working branches.
            keep_workspaces: Extra workspace ids (or 8-char prefixes) to leave
                alone, beyond the live checkouts found automatically.
            force_prune: Delete stray branches even if they hold unpinned data
                (or belong to a `BRANCH_IS_STORAGE` backend).
        """
        plan = self.plan_gc(
            prune_workspaces=prune_workspaces,
            keep_workspaces=keep_workspaces,
            force_prune=force_prune,
        )
        if dry_run:
            report = GcReport(dry_run=True, plan=plan)
            for a in plan.actions:
                if a.op == "unpin":
                    report.unpinned.setdefault(a.kind, []).append(
                        str(a.params["pin_id"])
                    )
                elif a.op == "forget-working-ref":
                    report.forgotten_working_refs.setdefault(a.key, []).append(a.target)
                elif a.op == "delete-branch":
                    report.deleted_working_refs.setdefault(a.key, []).append(a.target)
                elif a.op == "keep-branch":
                    report.kept_working_refs.setdefault(a.key, []).append(a.target)
                elif a.op == "delete-listing":
                    report.deleted_listings.append(a.target)
            return report
        return self.apply_gc(plan)

    # -- promote (fork -> base branch) ----------------------------------- #
    def plan_promote(
        self,
        keys: Sequence[str] | None = None,
        *,
        rev: str | None = None,
        strategy: str = "auto",
        message: str | None = None,
    ) -> Plan:
        """Compute how each object's base branch could be moved to its fork.

        Promotion moves the *system's* base branch (the locator's `branch`) to
        the state a fork holds; the dataset commit already pins that state, so
        nothing tether-side needs re-committing after a fast-forward. Per
        Forkable object, the target is this workspace's working branch head
        (or, with `rev`, the state pinned at that dataset commit) and the base
        is compared to the **fork point** recorded when the branch was created
        (falling back to the backend's ancestry check):

        - base unchanged and `PROMOTE`: `fast-forward`
        - base moved (or unknown) and `MERGE`: `merge` (native 3-way merge)
        - otherwise: `refuse`, with the system's own recipe in the detail

        Args:
            keys: Objects to consider (default: all Forkable ones).
            rev: Promote the states pinned at this dataset commit instead of the
                working branches.
            strategy: `auto` (table above), `ff` (refuse anything that is not a
                fast-forward), or `merge` (refuse anything that is not a merge).
            message: Merge commit message for backends that record one.

        Raises:
            ConfigError: Unknown key or strategy.
        """
        if strategy not in ("auto", "ff", "merge"):
            raise ConfigError(
                f"unknown promote strategy {strategy!r} (auto, ff, merge)"
            )
        objects = self._objects_at(self.vcs.resolve(rev)) if rev else self.objects
        selected = list(keys) if keys else sorted(objects)
        for key in selected:
            if key not in objects:
                raise ConfigError(f"no such object: {key}")
        message = message or f"tether promote {rev or self.workspace.workspace_id[:8]}"
        plan = Plan(
            command="promote",
            context={
                "rev": rev,
                "strategy": strategy,
                "message": message,
                "manifest_hash": manifest_hash(objects),
                "workspace_id": self.workspace.workspace_id,
            },
        )
        for key in selected:
            m = objects[key]
            backend = self.backend_for(m.kind)
            eff = effective_capabilities(backend, m.locator, m.policy)
            if Capability.FORK not in eff:
                plan.notes.append(f"{key}: not forkable; nothing to promote")
                continue
            if m.policy.write == "track":
                plan.notes.append(f"{key}: write=track; already on the base branch")
                continue

            base_locator = {k: v for k, v in m.locator.items() if k != "at"}
            base_state = backend.fingerprint(base_locator, None)

            # What to promote, and what the base looked like when it was forked.
            source: dict[str, Any]
            fork_point: State | None
            if rev:
                if m.state is None:
                    plan.notes.append(f"{key}: nothing committed at {rev}")
                    continue
                target_state = m.state
                if m.pin is not None:
                    source = {"pin": m.pin.to_dict()}
                elif Capability.ADDRESSABLE in eff:
                    source = {"state": m.state}
                else:
                    plan.notes.append(
                        f"{key}: not recoverable at {rev}; cannot promote"
                    )
                    continue
                fork_point = None
            else:
                working_ref = self._working_ref(key)
                if working_ref is None:
                    plan.notes.append(
                        f"{key}: no working branch yet; nothing to promote"
                    )
                    continue
                target_state = backend.fingerprint(m.locator, working_ref)
                source = {"ref": working_ref}
                fork_point = self.workspace.fork_points.get(key)

            if self._same(m.kind, target_state, base_state):
                plan.notes.append(f"{key}: base already at the target")
                continue

            unchanged: bool | None
            if fork_point is not None:
                unchanged = self._same(m.kind, fork_point, base_state)
            else:
                unchanged = backend.ancestor_of(
                    m.locator, base_state, _source_object(source)
                )

            can_ff = Capability.PROMOTE in eff
            can_merge = Capability.MERGE in eff and "state" not in source
            params = {
                "locator": dict(m.locator),
                "source": source,
                "base_state": base_state,
                "target_state": target_state,
                "fork_point": fork_point,
            }
            base_txt = f"{m.locator.get('branch', 'main')}"
            hint = f"; {backend.PROMOTE_HINT}" if backend.PROMOTE_HINT else ""

            if unchanged is True and strategy != "merge" and can_ff:
                plan.actions.append(
                    Action(
                        "fast-forward",
                        key,
                        m.kind,
                        target=base_txt,
                        detail=f"{base_txt} {_short_state(base_state)} -> "
                        f"{_short_state(target_state)} (base unchanged since fork)",
                        params=params,
                    )
                )
            elif (
                can_merge
                and strategy != "ff"
                and (unchanged is not True or not can_ff or strategy == "merge")
            ):
                why = (
                    "base unchanged since fork"
                    if unchanged is True
                    else "base moved since fork"
                    if unchanged is False
                    else "no fork point recorded; ancestry unknown"
                )
                src_name = source.get("ref") or source.get("pin", {}).get("ref")
                plan.actions.append(
                    Action(
                        "merge",
                        key,
                        m.kind,
                        target=base_txt,
                        detail=f"merge {src_name} into {base_txt} ({why})",
                        params=params,
                    )
                )
            else:
                if unchanged is True:
                    reason = (
                        f"strategy={strategy} but the backend cannot merge"
                        if strategy == "merge"
                        else f"the {m.kind} backend cannot move a branch"
                    )
                elif unchanged is False:
                    reason = (
                        f"{base_txt} moved since the fork "
                        f"({_short_state(fork_point)} -> {_short_state(base_state)})"
                        + (
                            f" and strategy={strategy}"
                            if strategy == "ff" and Capability.MERGE in eff
                            else " and the backend cannot merge"
                        )
                    )
                else:
                    reason = (
                        "cannot tell whether the base moved (no fork point recorded)"
                    )
                plan.actions.append(
                    Action(
                        "refuse",
                        key,
                        m.kind,
                        target=base_txt,
                        detail=f"{reason}{hint}",
                        params=params,
                    )
                )
        return plan

    def apply_promote(self, plan: Plan, *, verify: bool = True) -> PromoteReport:
        """Execute a plan from `plan_promote`.

        Fast-forwards and merges run concurrently. A merge that conflicts is
        rolled back by the backend and reported under `conflicts`. After a
        merge the working branch is reset to the merge result and the fork
        point updated, so the next `commit` pins what the base now holds.

        Raises:
            StalePlanError: A base branch moved since the plan was made.
            MultiObjectError: A backend failed for reasons other than conflicts.
        """
        if plan.command != "promote":
            raise ConfigError(f"expected a promote plan, got {plan.command!r}")
        report = PromoteReport(plan=plan)
        writes = [a for a in plan.actions if a.op in ("fast-forward", "merge")]
        for a in plan.actions:
            if a.op == "refuse":
                report.refused[a.key] = a.detail
        for note in plan.notes:
            key, _, why = note.partition(": ")
            if "already at the target" in why or "nothing to promote" in why:
                report.skipped.append(key)
        if verify:
            for a in writes:
                m = self.objects.get(a.key)
                locator = dict(a.params["locator"])
                backend = self.backend_for(a.kind)
                base_locator = {k: v for k, v in locator.items() if k != "at"}
                current = backend.fingerprint(base_locator, None)
                if m is not None and not self._same(
                    a.kind, current, a.params["base_state"]
                ):
                    raise StalePlanError(
                        f"{a.key!r}: base branch moved since the plan was made "
                        f"({_short_state(a.params['base_state'])} -> "
                        f"{_short_state(current)}); re-run the plan"
                    )

        message = str(plan.context.get("message") or "tether promote")
        by_key = {a.key: a for a in writes}
        pre = {
            "workspace": self.workspace.to_toml(),
            "base_states": {a.key: a.params.get("base_state") for a in writes},
        }

        def run_one(key: str) -> tuple[str, State]:
            a = by_key[key]
            backend = self.backend_for(a.kind)
            locator = dict(a.params["locator"])
            source = _source_object(a.params["source"])
            if a.op == "fast-forward":
                return "ff", backend.promote(locator, source)
            ref = source.ref if isinstance(source, Pin) else str(source)
            return "merge", backend.merge(locator, ref, message)

        results, errors = self._fanout_collect(run_one, list(by_key))
        for key, exc in list(errors.items()):
            if isinstance(exc, MergeConflict):
                report.conflicts[key] = list(exc.conflicts)
                report.refused[key] = str(exc)
                errors.pop(key)

        touched = False
        for key, (how, new_state) in results.items():
            (report.fast_forwarded if how == "ff" else report.merged)[key] = new_state
            m = self.objects.get(key)
            working_ref = self.workspace.working_refs.get(key)
            if how == "merge" and m is not None and working_ref is not None:
                # The fork now lags the base; reset it onto the merge result so
                # the next commit pins what the base holds.
                self.workspace.working_refs[key] = self.backend_for(m.kind).fork(
                    m.locator, new_state, working_ref
                )
            if key in self.workspace.working_refs or key in self.workspace.fork_points:
                self.workspace.fork_points[key] = dict(new_state)
                self.workspace.last_snapshot[key] = dict(new_state)
                touched = True
        if touched:
            write_workspace(self.root, self.workspace)
        if results or errors:
            self._log_op(
                "promote",
                plan=plan,
                result={
                    **_report_dict(report),
                    "failed": {k: str(v) for k, v in errors.items()},
                },
                pre=pre,
            )
        if errors:
            raise MultiObjectError("promote failed for some objects", errors)
        return report

    def promote(
        self,
        keys: Sequence[str] | None = None,
        *,
        rev: str | None = None,
        strategy: str = "auto",
        message: str | None = None,
    ) -> PromoteReport:
        """Move base branches to this workspace's forks.

        Equivalent to `apply_promote(plan_promote(...))`.
        """
        plan = self.plan_promote(keys, rev=rev, strategy=strategy, message=message)
        return self.apply_promote(plan, verify=False)

    # -- undo ------------------------------------------------------------ #
    def undo(self, op_id: str | None = None, *, discard: bool = False) -> UndoReport:
        """Reverse an operation from the op log, where the stores still allow it.

        Defaults to the newest entry that can be undone (not an `undo` or
        `repair`, not already undone). What "reverse" means per command:

        - `commit`: uncommit. The dataset commit becomes working-tree changes
          again (jj `squash --into @`, git `reset --soft`) if it is still the
          working copy's parent; the pins it made stay, still referenced by
          the working-tree manifests. Committed with `vcs=False`: the
          manifests are restored from before.
        - `new` / `fork`: branches the op created are deleted; branches it
          reset are re-pointed to their recorded heads where the backend can
          fork from a state; `workspace.toml` is restored; the VCS working
          copy returns to where it was if it has not moved since. A branch
          that gained writes since the op is refused unless `discard`.
        - `gc`: deleted branches are recreated from their recorded heads
          where the backend can; deleted listings come back from the VCS;
          the workspace is restored. Deleted pins are *irreversible* --
          `repair` recreates them once a manifest references them again.
        - `import` / `add` / `remove`: manifests and workspace restored.
        - `promote`: refused; the base heads before the move are printed
          for a manual reset (backends only fast-forward).

        The undo is logged and the target marked `undone_by`. If nothing
        could be reversed the call raises instead.

        Args:
            op_id: Which entry; default the newest undoable one.
            discard: Delete or reset branches even if they gained writes.

        Raises:
            TetherError: Nothing to undo, an unknown id, a branch with writes
                (without `discard`), or an operation that cannot be reversed.
        """
        entries = self.ops()
        if op_id is None:
            target = next((e for e in entries if e.undoable), None)
            if target is None:
                raise TetherError("nothing to undo")
        else:
            target = next((e for e in entries if e.id == op_id), None)
            if target is None:
                raise TetherError(f"no operation {op_id!r} in this workspace's log")
            if target.undone_by:
                raise TetherError(f"{op_id} was already undone by {target.undone_by}")
            if target.undoes is not None or target.command in ("undo", "repair"):
                raise TetherError(f"cannot undo a {target.command}; re-run instead")

        report = UndoReport(op=target)
        handler = {
            "commit": self._undo_commit,
            "new": self._undo_new,
            "fork": self._undo_fork,
            "gc": self._undo_gc,
            "promote": self._undo_promote,
            "import": self._undo_manifests,
            "add": self._undo_manifests,
            "remove": self._undo_manifests,
        }.get(target.command)
        if handler is None:
            raise TetherError(f"cannot undo a {target.command}")
        handler(target, report, discard)
        if not report.restored and report.irreversible:
            raise TetherError(
                f"cannot undo {target.id} ({target.command}):\n"
                + "\n".join(f"  {line}" for line in report.irreversible)
            )
        entry = self._log_op(
            "undo",
            result={
                "summary": f"{target.command} {target.summary()}",
                "restored": list(report.restored),
                "irreversible": list(report.irreversible),
                "skipped": list(report.skipped),
            },
            undoes=target.id,
        )
        report.undo_id = entry.id
        mark_undone(self.root, target.id, entry.id)
        return report

    def _reload(self) -> None:
        self.objects = read_objects(self.root)
        self.workspace = read_workspace(self.root)

    def _restore_workspace(self, entry: OpEntry, report: UndoReport) -> None:
        text = entry.pre.get("workspace")
        if text is None:
            return
        _m.workspace_path(self.root).write_text(str(text), encoding="utf-8")
        self.workspace = read_workspace(self.root)
        report.restored.append("workspace.toml restored")

    def _restore_manifests(self, texts: Mapping[str, Any], report: UndoReport) -> None:
        for key, text in sorted(texts.items()):
            if text is None:
                remove_object(self.root, key)
                report.restored.append(f"{key}: manifest removed again")
            else:
                _m.object_path(self.root, key).parent.mkdir(parents=True, exist_ok=True)
                _m.object_path(self.root, key).write_text(str(text), encoding="utf-8")
                report.restored.append(f"{key}: manifest restored")
        self.objects = read_objects(self.root)

    def _branch_has_new_writes(self, key: str, ref: str) -> State | None:
        """The head of `ref` if it moved past what this workspace knows; else None."""
        m = self.objects.get(key)
        if m is None:
            return None
        backend = self.backend_for(m.kind)
        try:
            head = backend.fingerprint(m.locator, ref)
        except TetherError:
            return None  # branch gone; nothing to lose
        known = [
            self.workspace.base_states.get(key),
            self.workspace.fork_points.get(key),
            m.state,
        ]
        if any(self._same(m.kind, head, k) for k in known if k is not None):
            return None
        return head

    def _undo_branches(
        self,
        entry: OpEntry,
        report: UndoReport,
        discard: bool,
        *,
        created: Mapping[str, str],
        reset: Mapping[str, str],
    ) -> None:
        """Delete branches an op created; re-point the ones it reset."""
        # Refuse before touching anything if a branch gained writes since.
        if not discard:
            dirty = []
            for key, ref in {**created, **reset}.items():
                head = self._branch_has_new_writes(key, ref)
                if head is not None:
                    dirty.append(
                        f"{key}: {ref} has writes since ({_short_state(head)})"
                    )
            if dirty:
                raise TetherError(
                    "refusing to undo: branches gained writes; commit them or pass "
                    "--discard\n" + "\n".join(f"  {d}" for d in dirty)
                )
        heads = entry.pre.get("heads") or {}
        for key, ref in sorted(created.items()):
            m = self.objects.get(key)
            if m is None:
                report.skipped.append(f"{key}: object no longer registered; {ref} left")
                continue
            try:
                self.backend_for(m.kind).delete_working_ref(m.locator, ref)
                report.restored.append(f"{key}: deleted {ref}")
            except TetherError as exc:
                report.irreversible.append(f"{key}: could not delete {ref}: {exc}")
        for key, ref in sorted(reset.items()):
            m = self.objects.get(key)
            head = heads.get(key)
            if m is None:
                report.skipped.append(f"{key}: object no longer registered; {ref} left")
                continue
            backend = self.backend_for(m.kind)
            eff = effective_capabilities(backend, m.locator, m.policy)
            if head is None or Capability.ADDRESSABLE not in eff:
                report.irreversible.append(
                    f"{key}: {ref} was reset and its previous head "
                    f"{'is unknown' if head is None else 'cannot be re-pointed to'}"
                )
                continue
            try:
                backend.fork(m.locator, dict(head), ref)
                report.restored.append(f"{key}: {ref} back at {_short_state(head)}")
            except TetherError as exc:
                report.irreversible.append(
                    f"{key}: {ref} could not go back to {_short_state(head)}: {exc}"
                )

    def _undo_commit(self, entry: OpEntry, report: UndoReport, discard: bool) -> None:
        commit = entry.result.get("vcs_commit")
        if commit:
            if not self.vcs.uncommit(str(commit)):
                report.irreversible.append(
                    f"commit {str(commit)[:12]} is no longer the working copy's "
                    "parent; uncommit it with jj/git first"
                )
                return
            report.restored.append(
                f"uncommitted {str(commit)[:12]}; its manifests are working-tree "
                "changes again (pins kept)"
            )
            self.objects = read_objects(self.root)
            return
        # Committed with vcs=False: only the manifests were written.
        self._restore_manifests(entry.pre.get("objects") or {}, report)
        self._restore_workspace(entry, report)

    def _undo_new(self, entry: OpEntry, report: UndoReport, discard: bool) -> None:
        r = entry.result
        refs = r.get("working_refs") or {}
        self._undo_branches(
            entry,
            report,
            discard,
            created={k: refs[k] for k in r.get("created") or [] if k in refs},
            reset={k: refs[k] for k in r.get("reset") or [] if k in refs},
        )
        self._restore_workspace(entry, report)
        before, after = entry.pre.get("vcs"), r.get("vcs")
        plan_ctx = (entry.plan or {}).get("context") or {}
        if plan_ctx.get("rev") and before and after:
            if self.vcs.position().get("id") == after.get("id"):
                self.vcs.goto(before)
                self.objects = read_objects(self.root)
                report.restored.append("VCS working copy back where it was")
            else:
                report.skipped.append(
                    "VCS working copy has moved since; not returning it"
                )

    def _undo_fork(self, entry: OpEntry, report: UndoReport, discard: bool) -> None:
        key, ref = str(entry.result.get("key")), str(entry.result.get("ref"))
        existed = key in (entry.pre.get("heads") or {})
        self._undo_branches(
            entry,
            report,
            discard,
            created={} if existed else {key: ref},
            reset={key: ref} if existed else {},
        )
        self._restore_workspace(entry, report)

    def _undo_gc(self, entry: OpEntry, report: UndoReport, discard: bool) -> None:
        plan = Plan.from_dict(entry.plan) if entry.plan else Plan(command="gc")
        r = entry.result
        deleted = {
            ref
            for refs in (r.get("deleted_working_refs") or {}).values()
            for ref in refs
        }
        for a in plan.actions:
            if a.op == "delete-branch" and a.target in deleted:
                head = a.params.get("head")
                backend = self.backend_for(a.kind)
                locator = dict(a.params["locator"])
                if head is None:
                    report.irreversible.append(
                        f"{a.target}: deleted; its head was not recorded"
                    )
                    continue
                if Capability.ADDRESSABLE not in backend.capabilities:
                    report.irreversible.append(
                        f"{a.target}: deleted; {a.kind} cannot recreate a branch "
                        "from a state"
                    )
                    continue
                try:
                    backend.fork(locator, dict(head), a.target)
                    report.restored.append(
                        f"{a.target}: recreated at {_short_state(head)}"
                    )
                except TetherError as exc:
                    report.irreversible.append(
                        f"{a.target}: could not recreate at {_short_state(head)}: {exc}"
                    )
        n_pins = sum(len(v) for v in (r.get("unpinned") or {}).values())
        if n_pins:
            report.irreversible.append(
                f"{n_pins} pin(s) deleted; if a manifest references them again, "
                "`tether repair` recreates them while the state is still reachable"
            )
        for name in r.get("deleted_listings") or []:
            rel = self._listing_relpath(name)
            text = self.vcs.read_file_at("@-" if self.vcs.kind == "jj" else "HEAD", rel)
            if text is None:
                report.irreversible.append(f"listing {name}: not in the VCS either")
                continue
            (listings_dir(self.root) / name).parent.mkdir(parents=True, exist_ok=True)
            (listings_dir(self.root) / name).write_text(text, encoding="utf-8")
            report.restored.append(f"listing {name}: restored from the VCS")
        self._restore_workspace(entry, report)

    def _undo_promote(self, entry: OpEntry, report: UndoReport, discard: bool) -> None:
        before = entry.pre.get("base_states") or {}
        moved = {
            **(entry.result.get("fast_forwarded") or {}),
            **(entry.result.get("merged") or {}),
        }
        for key in sorted(moved):
            report.irreversible.append(
                f"{key}: base branch moved {_short_state(before.get(key))} -> "
                f"{_short_state(moved[key])}; tether only fast-forwards base "
                "branches, reset it in the store yourself"
            )
        if not moved:
            report.skipped.append("promote moved nothing")

    def _undo_manifests(
        self, entry: OpEntry, report: UndoReport, discard: bool
    ) -> None:
        self._restore_manifests(entry.pre.get("objects") or {}, report)
        self._restore_workspace(entry, report)

    # -- export (manifests -> tables) ------------------------------------ #
    def export(
        self,
        revs: Sequence[str] | None = None,
        *,
        listings: bool = False,
        workspace: bool = False,
    ) -> ExportBundle:
        """Derive relational tables from the repository's history.

        The tables (`commits`, `commit_parents`, `refs`, `objects`,
        `object_states`, optional `listings` / `listing_entries` and
        `workspace`) are what `tether export` writes and `tether publish`
        upserts; see `tether.export.TABLES`.

        Args:
            revs: Revisions to include (jj revsets / git revisions). `None`
                exports every reachable commit.
            listings: Include per-file listings from `.tether/listings/`.
            workspace: Include this checkout's working refs and last snapshot.
        """
        return build_bundle(self, revs=revs, listings=listings, workspace=workspace)

    # -- import (registry rows -> manifests) ----------------------------- #
    def plan_import(
        self,
        specs: Sequence[ImportSpec],
        *,
        sync: bool = False,
        notes: Sequence[str] = (),
    ) -> Plan:
        """Diff desired objects against the working tree without writing.

        Actions: `add` for new keys, `update` when a registered key's locator
        or policy differs (its committed state and pin are kept), and, with
        `sync`, `remove` for registered keys the source no longer lists.

        Raises:
            ConfigError: A key's `kind` would change; remove and re-add it
                explicitly instead.
        """
        plan = Plan(
            command="import",
            context={
                "manifest_hash": self.current_manifest_hash(),
                "sync": sync,
                "rows": len(specs),
            },
            notes=list(notes),
        )
        wanted = {s.key: s for s in specs}
        for key in sorted(wanted):
            spec = wanted[key]
            current = self.objects.get(key)
            params = {"locator": dict(spec.locator), "policy": spec.policy.to_dict()}
            if current is None:
                plan.actions.append(
                    Action(
                        "add",
                        key,
                        spec.kind,
                        target=str(spec.locator.get("uri", "")),
                        detail="register",
                        params=params,
                    )
                )
                continue
            if current.kind != spec.kind:
                raise ConfigError(
                    f"{key!r} is registered as {current.kind!r} but the source says "
                    f"{spec.kind!r}; remove and re-add it to change kinds"
                )
            changed = []
            if dict(current.locator) != dict(spec.locator):
                changed.append("locator")
            if current.policy != spec.policy:
                changed.append("policy")
            if changed:
                plan.actions.append(
                    Action(
                        "update",
                        key,
                        spec.kind,
                        target=str(spec.locator.get("uri", "")),
                        detail=f"{' and '.join(changed)} changed; committed state kept",
                        params=params,
                    )
                )
            else:
                plan.notes.append(f"{key}: unchanged")
        if sync:
            for key in sorted(set(self.objects) - set(wanted)):
                plan.actions.append(
                    Action(
                        "remove",
                        key,
                        self.objects[key].kind,
                        detail="not listed by the source (sync)",
                    )
                )
        return plan

    def apply_import(self, plan: Plan, *, verify: bool = True) -> ImportReport:
        """Write the manifests a `plan_import` plan describes.

        Touches only `.tether/objects/` and the workspace state; commit the
        result with `commit` as usual.

        Raises:
            StalePlanError: The working tree's manifests changed since planning.
        """
        if plan.command != "import":
            raise ConfigError(f"expected an import plan, got {plan.command!r}")
        if verify and plan.context.get("manifest_hash") != self.current_manifest_hash():
            raise StalePlanError(
                "manifests changed since the plan was made; re-run the plan"
            )
        report = ImportReport(plan=plan)
        for note in plan.notes:
            key, _, why = note.partition(": ")
            if why == "unchanged":
                report.unchanged.append(key)
        pre = {
            "objects": self._manifest_texts(a.key for a in plan.actions),
            "workspace": self.workspace.to_toml(),
        }
        for a in plan.actions:
            if a.op == "add":
                self._add(
                    a.key,
                    a.kind,
                    dict(a.params["locator"]),
                    policy=Policy.from_dict(a.params["policy"]),
                )
                report.added.append(a.key)
            elif a.op == "update":
                current = self.objects[a.key]
                updated = dataclasses.replace(
                    current,
                    locator=dict(a.params["locator"]),
                    policy=Policy.from_dict(a.params["policy"]),
                )
                write_object(self.root, updated)
                self.objects[a.key] = updated
                if dict(current.locator) != dict(updated.locator):
                    # The working branch lives in the *old* system; keeping it
                    # would send writes there until the next `new`. Drop the
                    # workspace's hold (the branch itself is left for `gc`).
                    self._forget_working_state(a.key)
                report.updated.append(a.key)
            elif a.op == "remove":
                self._remove(a.key)
                report.removed.append(a.key)
        write_workspace(self.root, self.workspace)
        if plan.actions:
            self._log_op("import", plan=plan, result=_report_dict(report), pre=pre)
        return report

    def import_objects(
        self, rows: Iterable[Mapping[str, Any]], *, sync: bool = False
    ) -> ImportReport:
        """Register / update / remove objects from canonical rows in one step.

        Rows carry `key`, `kind`, and any of `uri`, `locator_json`,
        `policy_write`, `policy_file`, `policy_pin`, `at` (see
        `tether.registry.CANONICAL_COLUMNS`); missing policy fields take
        `config.defaults`. Equivalent to `apply_import(plan_import(...))`.
        """
        specs, notes = specs_from_rows(rows, self.config.defaults)
        return self.apply_import(
            self.plan_import(specs, sync=sync, notes=notes), verify=False
        )

    # -- diff ------------------------------------------------------------ #
    def diff(
        self,
        rev_a: str | None = None,
        rev_b: str | None = None,
        *,
        content: bool = False,
    ) -> list[DiffEntry]:
        """Object-level manifest diff; with ``content`` also what changed inside.

        Content diffs run concurrently for every ``changed`` object whose backend
        declares ``DIFF``; per-object failures land in ``detail_error`` rather
        than aborting the whole diff.
        """
        resolved_a = self.vcs.resolve(rev_a) if rev_a is not None else None
        resolved_b = self.vcs.resolve(rev_b) if rev_b is not None else None
        a = self._objects_at(resolved_a) if resolved_a is not None else self.objects
        b = self._objects_at(resolved_b) if resolved_b is not None else None
        # Default: compare working tree (a) against its parent commit.
        if b is None and rev_a is None:
            try:
                resolved_b = self.vcs.current_rev()
                b = self._objects_at(resolved_b)
            except VcsError:
                b = {}
            a, b = b, a  # b = parent, a = working; present as parent -> working
            resolved_a, resolved_b = resolved_b, None
        elif b is None:
            b = {}

        entries: list[DiffEntry] = []
        for key in sorted(set(a) | set(b)):
            ma = a.get(key)
            mb = b.get(key)
            pa = ma.pin.id if ma and ma.pin else None
            pb = mb.pin.id if mb and mb.pin else None
            if ma and not mb:
                change = "removed"
            elif mb and not ma:
                change = "added"
            elif pa != pb or (ma and mb and ma.state != mb.state):
                change = "changed"
            else:
                change = "unchanged"
            entries.append(DiffEntry(key=key, change=change, a_pin=pa, b_pin=pb))

        if content:
            self._attach_content_diffs(entries, a, b, resolved_a, resolved_b)
        return entries

    def _attach_content_diffs(
        self,
        entries: list[DiffEntry],
        a: dict[str, ObjectManifest],
        b: dict[str, ObjectManifest],
        rev_a: str | None,
        rev_b: str | None,
    ) -> None:
        by_key = {e.key: e for e in entries}
        keys: list[str] = []
        for e in entries:
            if e.change != "changed":
                continue
            ma, mb = a[e.key], b[e.key]
            if ma.state is None or mb.state is None or ma.kind != mb.kind:
                continue
            backend = self.backend_for(mb.kind)
            if Capability.DIFF not in effective_capabilities(
                backend, mb.locator, mb.policy
            ):
                continue
            keys.append(e.key)

        def diff_one(key: str) -> ObjectDiff:
            ma, mb = a[key], b[key]
            assert ma.state is not None and mb.state is not None
            backend = self.backend_for(mb.kind)
            listings = (self._listing_for(ma, rev_a), self._listing_for(mb, rev_b))
            return backend.diff(mb.locator, ma.state, mb.state, listings=listings)

        results, errors = self._fanout_collect(diff_one, keys)
        for key, result in results.items():
            by_key[key].detail = result
        for key, err in errors.items():
            by_key[key].detail_error = str(err)
