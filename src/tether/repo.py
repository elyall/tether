"""The tether engine: snapshot, status, commit, new, open, verify, gc.

A :class:`Repo` binds a dataset root (a ``tether.toml`` plus ``.tether/``) to the
enclosing VCS and the registered backends, and orchestrates the cross-system
fan-out. It holds no data itself; it coordinates native refs and hands back
native handles.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from tether import manifest as _m
from tether.backends.base import (
    Capability,
    ObjectBackend,
    ObjectDiff,
    Tier,
    VerifyReport,
    VerifyStatus,
    build_backend,
    effective_capabilities,
    tier_of,
)
from tether.errors import (
    CapabilityError,
    ConfigError,
    ImmutableObjectModified,
    MultiObjectError,
    StaleWorkingCopyError,
    TetherError,
    UnpinnedStateError,
    VcsError,
)
from tether.handles import Handle
from tether.manifest import (
    ObjectManifest,
    Pin,
    Policy,
    RepoConfig,
    State,
    compute_pin_id,
    ensure_layout,
    find_dataset_root,
    listing_name,
    listings_dir,
    manifest_hash,
    read_config,
    read_listing,
    read_objects,
    read_workspace,
    ref_for_pin,
    remove_object,
    working_ref_name,
    write_config,
    write_listing,
    write_object,
    write_workspace,
)
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
    """Working refs were forked from a different manifest hash than HEAD's."""
    objects: list[ObjectStatus] = field(default_factory=list)
    """Per-object classifications, sorted by key."""


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
    """Working refs dropped because their object was removed."""
    deleted_listings: list[str] = field(default_factory=list)
    """`.tether/listings/` files no manifest references."""
    dry_run: bool = True
    """Whether anything was actually released."""


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
        repo.workspace.base = manifest_hash(repo.objects)
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

    def _working_ref(self, key: str) -> str | None:
        return self.workspace.working_refs.get(key)

    def _dataset_rel(self) -> Path:
        try:
            return self.root.relative_to(self.vcs.root)
        except ValueError:  # pragma: no cover - dataset outside vcs root
            return Path(".")

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
        name = listing_name(m.kind, backend.identity(m.locator), m.state)
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

    def is_stale(self) -> bool:
        """Whether HEAD's manifests changed since this workspace forked its refs.

        A stale workspace refuses writable handles until `new` reforks.
        """
        base = self.workspace.base
        return base is not None and base != self.current_manifest_hash()

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
        self.workspace.base = self.current_manifest_hash()
        write_workspace(self.root, self.workspace)
        return manifest

    def remove(self, key: str) -> None:
        """Unregister an object; the external system (and its pins) is untouched.

        Raises:
            ConfigError: If `key` is not registered.
        """
        if key not in self.objects:
            raise ConfigError(f"no such object: {key}")
        remove_object(self.root, key)
        del self.objects[key]
        self.workspace.working_refs.pop(key, None)
        self.workspace.last_snapshot.pop(key, None)
        self.workspace.base = self.current_manifest_hash()
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
                and state != m.state
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
            changed = committed and current is not None and current != m.state
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
        return StatusReport(
            manifest_hash=self.current_manifest_hash(),
            stale=self.is_stale(),
            objects=objects,
        )

    # -- commit ---------------------------------------------------------- #
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

        For each object whose state changed: `PIN`-capable backends create the
        native ref `tether.<pin_id>` (idempotent), Addressable backends have
        their state recorded, and Observed backends are recorded with
        `recoverable = False`. Backends that provide a listing have it stored
        under `.tether/listings/`. If any pin fails, pins created by this call
        are released best-effort. With `config.new_auto_fork`, `new` runs
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
        states = self.snapshot() if do_snapshot else self.workspace.last_snapshot
        keys = list(self.objects)

        # Quiescence pre-check for backends that need it.
        if not force:
            for key in keys:
                m = self.objects[key]
                backend = self.backend_for(m.kind)
                eff = effective_capabilities(backend, m.locator, m.policy)
                if Capability.NEEDS_QUIESCENCE in eff:
                    check = getattr(backend, "check_quiescence", None)
                    if callable(check):
                        check(m.locator, self._working_ref(key))

        result = CommitResult(message=message)
        created_pins: list[tuple[str, Pin]] = []
        plans: dict[str, tuple[State, Pin | None, bool]] = {}

        try:
            for key in keys:
                m = self.objects[key]
                backend = self.backend_for(m.kind)
                eff = effective_capabilities(backend, m.locator, m.policy)
                tier = tier_of(eff)
                state = states.get(key)
                if state is None:
                    continue
                # No-op if unchanged and already properly recorded.
                needs_pin = Capability.PIN in eff
                if m.state == state and (m.pin is not None or not needs_pin):
                    result.unchanged.append(key)
                    continue

                if tier is Tier.OBSERVED:
                    if strict:
                        raise UnpinnedStateError(
                            f"{key!r} is Observed-tier and cannot be pinned",
                            key=key,
                            kind=m.kind,
                        )
                    plans[key] = (state, None, False)  # recoverable=False
                    result.unrecoverable.append(key)
                elif needs_pin:
                    pin_id = compute_pin_id(m.kind, backend.identity(m.locator), state)
                    pin = backend.pin(m.locator, state, pin_id)
                    created_pins.append((key, pin))
                    plans[key] = (state, pin, True)
                    result.pinned[key] = pin
                else:  # ADDRESSABLE
                    plans[key] = (state, None, True)
                    result.pinned[key] = None
        except MultiObjectError:
            self._rollback_pins(created_pins)
            raise
        except Exception:
            self._rollback_pins(created_pins)
            raise

        # Persist manifests (and listings for backends that provide them).
        for key, (state, pin, recoverable) in plans.items():
            m = self.objects[key]
            updated = m.with_pin(state=state, pin=pin, recoverable=recoverable)
            self.objects[key] = updated
            write_object(self.root, updated)
            backend = self.backend_for(m.kind)
            if Capability.DIFF in effective_capabilities(backend, m.locator, m.policy):
                text = backend.listing(m.locator, state)
                if text is not None:
                    name = listing_name(m.kind, backend.identity(m.locator), state)
                    write_listing(self.root, name, text)

        # VCS commit (only if something changed on disk).
        if vcs and plans:
            result.vcs_commit = self.vcs.commit(self._vcs_paths(), message)

        self.workspace.base = self.current_manifest_hash()
        write_workspace(self.root, self.workspace)
        if plans and self.config.new_auto_fork:
            # jj-style: every commit leaves you on a fresh working copy.
            self.new()
        return result

    def _rollback_pins(self, created: list[tuple[str, Pin]]) -> None:
        for key, pin in created:
            m = self.objects.get(key)
            if m is None:
                continue
            # Best-effort compensation; a failed unpin is reconciled by gc.
            with contextlib.suppress(Exception):
                self.backend_for(m.kind).unpin(m.locator, pin)

    # -- new (fork working refs) ---------------------------------------- #
    def new(self, rev: str | None = None, *, keep: bool = False) -> None:
        """Start working on top of `rev`: fork fresh writable refs off its pins.

        For every `FORK`-capable object, `track` policy uses the locator's
        branch; otherwise the pin is verified (cheaply) and a branch named
        `working_ref_name(workspace_id, key)` is forked from it. Objects with no
        pin yet are skipped until their first commit. Forks run concurrently.

        Args:
            rev: Move the VCS working copy here first (`jj new` / `git checkout`)
                and reload the manifests; `None` keeps the current commit.
            keep: Only refresh the stale-detection baseline; keep working refs.

        Raises:
            MultiObjectError: A pin is missing or a fork failed.
        """
        if rev is not None:
            self.vcs.new(rev)
            self.objects = read_objects(self.root)

        if keep:
            self.workspace.base = self.current_manifest_hash()
            write_workspace(self.root, self.workspace)
            return

        # Cheap verify of pins we are about to fork from, then fork -- one
        # network round-trip chain per object, run concurrently.
        working_refs: dict[str, str] = {}
        to_fork: list[str] = []
        for key in sorted(self.objects):
            m = self.objects[key]
            backend = self.backend_for(m.kind)
            eff = effective_capabilities(backend, m.locator, m.policy)
            if Capability.FORK not in eff:
                continue
            if m.policy.write == "track":
                working_refs[key] = str(m.locator.get("branch", "main"))
                continue
            if m.pin is None or m.state is None:
                continue  # nothing pinned yet; will fork after first commit
            to_fork.append(key)

        def fork_one(key: str) -> str:
            m = self.objects[key]
            backend = self.backend_for(m.kind)
            assert m.pin is not None and m.state is not None
            report = backend.verify(m.locator, m.state, m.pin, deep=False)
            if report.status is VerifyStatus.MISSING:
                raise TetherError(f"pin missing: {report.message}")
            name = working_ref_name(self.workspace.workspace_id, key)
            return backend.fork(m.locator, m.pin, name)

        try:
            working_refs.update(self._fanout(fork_one, to_fork))
        except MultiObjectError as exc:
            raise MultiObjectError("could not fork working refs", exc.errors) from None

        self.workspace.working_refs = working_refs
        self.workspace.base = self.current_manifest_hash()
        write_workspace(self.root, self.workspace)

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
            if self.is_stale():
                raise StaleWorkingCopyError(
                    "working copy is stale (HEAD manifests changed since fork); "
                    "run `tether new` to refork before writing"
                )
            working_ref = self._working_ref(key)
            if working_ref is None:
                raise StaleWorkingCopyError(
                    f"no working ref for {key!r}; run `tether new` first"
                )
            return backend.open(m.locator, working_ref, read_only=False)

        # Read-only handle at the current working ref / base.
        return backend.open(m.locator, self._working_ref(key), read_only=True)

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
                `"<commit12>:<key>"`. History is streamed through one object
                reader and each distinct record is verified once.

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
                if m.state is None or m.pin is None:
                    continue
                targets[f"{rev[:12]}:{key}"] = m
        return self._verify_manifests(targets, deep)

    # -- gc -------------------------------------------------------------- #
    def gc(self, *, dry_run: bool = True) -> GcReport:
        """Release native pins that no manifest in VCS history references.

        Also drops this workspace's working refs for removed objects and
        deletes `.tether/listings/` files no manifest names.

        Args:
            dry_run: Only report what would be released.
        """
        # Referenced pin ids per (kind, identity-json) across all history + ws.
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
        # Also protect the working-tree manifests.
        for m in self.objects.values():
            if m.pin is not None:
                backend = self.backend_for(m.kind)
                referenced.setdefault(key_for(backend, m.locator), set()).add(m.pin.id)

        report = GcReport(dry_run=dry_run)
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
            keep = referenced.get(sys_key, set())
            orphans = sorted(live - keep)
            if not orphans:
                continue
            report.unpinned.setdefault(m.kind, []).extend(orphans)
            if not dry_run:
                for pid in orphans:
                    backend.unpin(m.locator, Pin(id=pid, ref=ref_for_pin(pid)))

        # Prune this workspace's working refs whose object was removed.
        dropped: list[str] = []
        for key, ref in list(self.workspace.working_refs.items()):
            if key in self.objects:
                continue
            report.deleted_working_refs.setdefault("(removed)", []).append(ref)
            if not dry_run:
                dropped.append(key)
        if dropped:
            for key in dropped:
                self.workspace.working_refs.pop(key, None)
            write_workspace(self.root, self.workspace)

        # Prune listings no manifest (in history or the working tree) names.
        wanted: set[str] = set()
        for m in [*all_manifests, *self.objects.values()]:
            if m.state is not None:
                backend = self.backend_for(m.kind)
                wanted.add(listing_name(m.kind, backend.identity(m.locator), m.state))
        for path in sorted(listings_dir(self.root).glob("*.jsonl")):
            if path.name in wanted:
                continue
            report.deleted_listings.append(path.name)
            if not dry_run:
                path.unlink()
        return report

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
