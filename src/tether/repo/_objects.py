"""Registering, observing, and reading objects: add/remove, set, pull, snapshot
and status, open, history, verify, diff."""

from __future__ import annotations

import dataclasses
import os

try:  # POSIX advisory locks; Windows has no fcntl and gets no writer lock
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from tether import manifest as _m
from tether.backends.base import (
    Capability,
    HistoryEntry,
    ObjectBackend,
    ObjectDiff,
    Tier,
    VerifyReport,
    VerifyStatus,
    absolutize_locator,
    effective_capabilities,
    tier_of,
)
from tether.errors import (
    BackendError,
    CapabilityError,
    ConfigError,
    ImmutableObjectModified,
    PinDriftError,
    StaleWorkingCopyError,
    TetherError,
    VcsError,
)
from tether.handles import Handle
from tether.manifest import (
    Locator,
    ObjectManifest,
    Pin,
    Policy,
    State,
    remove_object,
    write_object,
    write_workspace,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tether.repo import Repo

    pass


from tether.repo._core import TETHER_REV_ENV, RepoCore
from tether.repo._reports import (
    DiffEntry,
    ObjectStatus,
    PullReport,
    SetReport,
    StatusReport,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


class ObjectOps(RepoCore):
    """Registering, observing, and reading objects: add/remove, set, pull,
    snapshot and status, open, history, verify, diff."""

    # -- add / remove ---------------------------------------------------------- #
    def add(
        self: Repo,
        key: str,
        kind: str,
        locator: dict,
        *,
        policy: Policy | None = None,
        create: bool = False,
    ) -> ObjectManifest:
        """Register an object in the working copy.

        Writes a manifest with no state yet; the external system is not
        contacted until the next `snapshot`, `status`, or `commit` -- unless
        `create`, which makes the store first.

        Args:
            key: Free-form, path-like object key (`"zarr/imaging"`).
            kind: Backend kind (see `tether.backends.known_kinds`).
            locator: Backend-specific fields naming the object (`uri`, `branch`,
                `project_id`, ...); see the backends guide.
            policy: Per-object `Policy`; defaults to `config.defaults`.
            create: Make an empty store at the locator (`CREATE` backends), mark
                it as this dataset's, and record it in the repository-wide
                index so `gc` can remove it once nothing references it. Refused
                if anything already exists there. On a non-trunk bookmark the
                new store's working branch is forked right away, so the first
                writable `open` needs no `new`.

        Returns:
            The new manifest.

        Raises:
            ConfigError: If `key` exists, is unsafe, or `kind` cannot be built.
            CapabilityError: `create` for a kind without `CREATE`.
            BackendError: `create` where a store already exists.
        """
        with self._writer_lock():
            pre = {"objects": {key: None}, "workspace": self.workspace.to_toml()}
            if key in self.objects:
                raise ConfigError(f"object already exists: {key}")
            if not create:
                manifest = self._add(key, kind, locator, policy=policy)
                self._log_op("add", result={"key": key}, pre=pre)
                return manifest
            # Experimental: the store lifecycle lives behind the same seam as
            # the registry and is imported only when asked for.
            from tether.experimental.lifecycle import create_store

            return create_store(self, key, kind, locator, policy=policy, pre=pre)

    def create(
        self: Repo,
        key: str,
        kind: str,
        locator: dict,
        *,
        policy: Policy | None = None,
    ) -> Handle:
        """Make a store, register it as `key`, and open it: one verb for a
        throwaway environment.

        `add(key, kind, locator, create=True)` followed by `open(key)`. The
        store is created empty with this dataset's owner marker and recorded in
        the repository-wide index, so `gc --delete-stores` can remove it once
        nothing references it; on a non-trunk bookmark its working branch is
        forked at once, so the handle is writable without a `new`.

        Args:
            key: Object key to register.
            kind: Backend kind with the `CREATE` capability.
            locator: Where to make the store (backend-specific; nothing may
                exist there yet).
            policy: Per-object `Policy`; defaults to `config.defaults`.

        Raises:
            ConfigError: `key` is already registered.
            CapabilityError: `kind` cannot create a store at this locator.
            BackendError: Something already exists at the locator.
        """
        self.add(key, kind, locator, policy=policy, create=True)
        return self.open(key)

    def _add(
        self,
        key: str,
        kind: str,
        locator: dict,
        *,
        policy: Policy | None = None,
        origin: Literal["adopted", "created"] = "adopted",
    ) -> ObjectManifest:
        if key in self.objects:
            raise ConfigError(f"object already exists: {key}")
        # Validate the backend kind eagerly -- and pin down relative local
        # paths now, against the caller's directory: the manifest is read from
        # every directory and every clone.
        backend = self.backend_for(kind)
        resolved = absolutize_locator(backend, dict(locator), Path.cwd())
        backend.validate_locator(resolved)
        manifest = ObjectManifest(
            key=key,
            kind=kind,
            locator=resolved,
            policy=policy or self.config.defaults,
            origin=origin,
        )
        write_object(self.root, manifest)
        self.objects[key] = manifest
        self._objects_gen += 1
        # A re-registered key starts without a working ref; any branch left by
        # its previous incarnation is found by `gc --prune-bookmarks`.
        self.workspace.working_refs.pop(key, None)
        self.workspace.pending_forks.pop(key, None)
        self.workspace.pending_resets.pop(key, None)
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
        with self._writer_lock():
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
        self._objects_gen += 1
        self.workspace.last_snapshot.pop(key, None)
        self.workspace.pending_forks.pop(key, None)  # never created; nothing to gc
        self.workspace.pending_resets.pop(key, None)
        self.workspace.fork_points.pop(key, None)
        self.workspace.base_states.pop(key, None)
        write_workspace(self.root, self.workspace)

    # -- set ------------------------------------------------------------------- #
    def set_policy(
        self,
        keys: Sequence[str],
        *,
        file: str | None = None,
        pin: str | None = None,
    ) -> SetReport:
        """Change policy fields of registered objects in place.

        Manifest-only, logged, undoable. Commit afterwards to record the
        policy. (Whether writes fork or land upstream is not a policy: it is
        the bookmark the working copy is on.)

        Args:
            keys: Objects to change.
            file: New file policy (`immutable` | `versioned`), or None.
            pin: New pin policy (`native` | `record`), or None.

        Raises:
            ConfigError: Unknown key, an invalid value, or nothing to set.
        """
        with self._writer_lock():
            if file is None and pin is None:
                raise ConfigError("nothing to set: pass --file or --pin")
            report = SetReport()
            updates: dict[str, ObjectManifest] = {}
            for key in keys:
                m = self.objects.get(key)
                if m is None:
                    raise ConfigError(f"no such object: {key}")
                wanted = Policy.from_dict(
                    {
                        "file": file if file is not None else m.policy.file,
                        "pin": pin if pin is not None else m.policy.pin,
                    }
                )
                diff = {
                    f: (getattr(m.policy, f), getattr(wanted, f))
                    for f in ("file", "pin")
                    if getattr(m.policy, f) != getattr(wanted, f)
                }
                if not diff:
                    report.unchanged.append(key)
                    continue
                report.changed[key] = diff
                updates[key] = dataclasses.replace(m, policy=wanted)
            if not updates:
                return report
            pre = {
                "objects": self._manifest_texts(updates),
                "workspace": self.workspace.to_toml(),
            }
            for key, updated in updates.items():
                write_object(self.root, updated)
                self.objects[key] = updated
            self._objects_gen += 1
            self._log_op(
                "set",
                result={
                    "changed": {
                        k: {f: list(v) for f, v in d.items()}
                        for k, d in report.changed.items()
                    },
                },
                pre=pre,
            )
            return report

    # -- pull ------------------------------------------------------------------ #
    def pull(
        self: Repo, bookmark: str | None = None, *, message: str | None = None
    ) -> PullReport:
        """Fetch the heads of this bookmark's branches and commit them onto it.

        The dataset's `git fetch` + rebase. On the trunk the branches are every
        object's upstream branch (and, for systems without branches, the
        object itself); on any other bookmark they are its `tether.ws.*`
        branches -- which are this workspace's working refs, so a pull there
        is what `commit` already does. What differs from the bookmark's commit
        is pinned and committed on the bookmark, which moves; the working copy
        ends up on top. Nothing moved: no commit.

        Args:
            bookmark: Must be the bookmark this workspace works on (default).
                Pulling another bookmark means `tether new NAME` first.
            message: Commit message; default `pull <bookmark>: <n> object(s)`.

        Raises:
            ConfigError: No bookmark, another bookmark, or `.tether/` has
                uncommitted manifest edits.
            ImmutableObjectModified: An Observed `file = "immutable"` object
                changed; re-register it to accept the new state.
            MultiObjectError: A fingerprint failed.
        """
        with self._writer_lock():
            mine = self.workspace.bookmark
            if mine is None:
                raise ConfigError(
                    "this working copy is on no bookmark; `tether new NAME` to work on "
                    "one, then pull"
                )
            if bookmark is not None and bookmark != mine:
                raise ConfigError(
                    f"this workspace works on {mine!r}; `tether new {bookmark}` first"
                )
            self._check_on_bookmark()
            if self.vcs.dirty(self._vcs_paths()):
                raise ConfigError(
                    "manifests have uncommitted edits; `tether commit` them (or undo) "
                    "before pulling"
                )
            report = PullReport(bookmark=mine)
            moving = set(self.moving_keys())
            targets: list[str] = []
            for key, m in self.objects.items():
                if m.state is None:
                    report.skipped[key] = "not committed yet; the first commit reads it"
                elif key in moving:
                    targets.append(key)  # a working branch: its head is the position
                elif self.on_trunk():
                    targets.append(key)  # upstream branch, or the object itself
                else:
                    report.skipped[key] = "no branch yet; sits at its pin"

            def fp(key: str) -> State:
                m = self.objects[key]
                backend = self.backend_for(m.kind)
                ref = self._working_ref(key)
                if ref is None:
                    return backend.fingerprint(self._upstream_locator(m), None)
                return backend.fingerprint(m.locator, ref)

            heads = self._fanout(fp, targets)
            self._enforce_immutability(heads)
            fetched: dict[str, State] = {}
            for key in targets:
                m = self.objects[key]
                assert m.state is not None
                if self._same(m.kind, heads[key], m.state):
                    report.unchanged.append(key)
                else:
                    report.committed[key] = (dict(m.state), dict(heads[key]))
                fetched[key] = dict(heads[key])
                self.workspace.last_snapshot[key] = dict(heads[key])
            if not report.committed:
                write_workspace(self.root, self.workspace)
                return report
            n = len(report.committed)
            text = message or f"pull {mine}: {n} object{'s' if n != 1 else ''}"
            plan = self.plan_commit(text, fetched=fetched)
            result = self.apply_commit(plan, verify=False)
            report.vcs_commit = result.vcs_commit
            report.pinned = dict(result.pinned)
            return report

    # -- snapshot / status ----------------------------------------------------- #
    def moving_keys(self) -> list[str]:
        """Objects whose position can change without `pull`: what `commit`
        fingerprints.

        An object's *position* is what the next commit records. It moves when
        the object has a working ref (the bookmark's branch, or the upstream
        branch on the trunk), or when it has no committed state yet (the first
        commit reads it). Everything else -- a committed object with no working
        branch, whether its system has branches or not -- stays at its previous
        pin or recorded state until `pull` takes what is there now, as an
        unchanged file stays as the parent commit had it.
        """
        return [
            key
            for key, m in self.objects.items()
            if self._working_ref(key) is not None or m.state is None
        ]

    def _upstream_locator(self, m: ObjectManifest) -> Locator:
        """The locator with `at` dropped: the upstream branch head, not the
        state the object was first registered at."""
        return {k: v for k, v in m.locator.items() if k != "at"}

    def snapshot(self, *, upstream: bool = True) -> dict[str, State]:
        """Fingerprint objects concurrently and cache the result.

        Objects with a working ref are read there; the others at their upstream
        branch head (`upstream=True`, the default -- what `tether snapshot` and
        `status --snapshot` do, so `status` can say `behind`), or not at all
        (`upstream=False`: only `moving_keys` are contacted and the committed or
        pulled state stands in for the rest -- what `commit` does). States are
        stored in `workspace.last_snapshot`.

        Args:
            upstream: Contact the upstream branch of objects without a working
                ref.

        Returns:
            Current state per object key.

        Raises:
            ImmutableObjectModified: An Observed object with `policy.file ==
                "immutable"` changed since it was committed.
            MultiObjectError: One or more fingerprints failed.
        """
        # Under the lock from the first decision to the last write: which refs
        # to read comes from the workspace as it is *now* on disk (the lock
        # refreshes it), not from the one this Repo loaded, which another
        # process may have moved to a different bookmark since. Reading first
        # and locking only the write would cache one bookmark's states under
        # another's name.
        with self._writer_lock():
            moving = set(self.moving_keys())
            # Upstream only means something where the position *could* follow
            # it: on the trunk (or on no bookmark). A feature bookmark forked
            # from pins; the world moving on is not its business until it is
            # promoted.
            ask_upstream = upstream and (
                self.on_trunk() or self.workspace.bookmark is None
            )
            keys = list(self.objects) if ask_upstream else sorted(moving)

            def fp(key: str) -> State:
                m = self.objects[key]
                backend = self.backend_for(m.kind)
                ref = self._working_ref(key)
                if ref is None and key not in moving:
                    return backend.fingerprint(self._upstream_locator(m), None)
                return backend.fingerprint(m.locator, ref)

            states: dict[str, State] = self._fanout(fp, keys)
            for key, m in self.objects.items():
                if key not in states and m.state is not None:
                    states[key] = dict(m.state)
            self._enforce_immutability(states)
            # Cache what was actually read. A partial (commit-time) snapshot
            # must not overwrite what an earlier fan-out learned about
            # upstream: a `behind` object stays `behind` in a local `status`
            # after an unrelated commit. Objects no longer registered drop out.
            cached = {
                k: v
                for k, v in self.workspace.last_snapshot.items()
                if k in self.objects
            }
            for key in keys:  # fingerprinted now
                if key in states:
                    cached[key] = states[key]
            for key, state in states.items():  # positions: only where unknown
                cached.setdefault(key, state)
            self.workspace.touch_snapshot(cached)
            write_workspace(self.root, self.workspace)
        return states

    def _enforce_immutability(self, states: dict[str, State]) -> None:
        """Raise for an Observed `file = "immutable"` object that changed."""
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

    def status(self, *, do_snapshot: bool = True) -> StatusReport:
        """Classify every object against its committed manifest.

        Objects with a working ref compare the branch head to the committed
        state (`modified`). Objects without one are `behind` when a fan-out on
        the trunk saw the upstream branch move past the committed state (`pull`
        takes it), else `clean`.

        Args:
            do_snapshot: Take a fresh `snapshot` first; otherwise reuse the
                cached one (no external systems are contacted) and report its
                age. A workspace with no snapshot yet always takes one.

        Returns:
            The report; `objects` are sorted by key.
        """
        fresh = do_snapshot or not self.workspace.last_snapshot
        states = self.snapshot() if fresh else self.workspace.last_snapshot
        moving = set(self.moving_keys())
        objects: list[ObjectStatus] = []
        for key in sorted(self.objects):
            m = self.objects[key]
            backend = self.backend_for(m.kind)
            eff = effective_capabilities(backend, m.locator, m.policy)
            current = states.get(key)
            committed = m.state is not None
            differs = (
                committed
                and current is not None
                and not self._same(m.kind, current, m.state)
            )
            changed = bool(differs) and key in moving
            behind = bool(differs) and key not in moving
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
                    changed=changed,
                    behind=behind,
                    origin=m.origin,
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
            bookmark=self.workspace.bookmark,
            trunk=self.on_trunk(),
            bookmark_drift=self.bookmark_drift(),
            vcs_drift=self.vcs_drift(),
            fresh=fresh,
            snapshot_at=self.workspace.last_snapshot_at,
        )

    # -- open ------------------------------------------------------------------ #
    def open(
        self: Repo,
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
            # Under the checkout lock, which re-reads the workspace and the
            # manifests: a `Repo` that has lived a while (a notebook) must
            # hand back a handle on the branch the checkout is on *now* --
            # another process may have run `new` since -- not on the working
            # ref it loaded at construction, which on the trunk is upstream.
            with self._writer_lock():
                return self._open_writable(key)

        # Read-only handle at the object's position: its working ref when it
        # has one, else the committed pin / state -- rather than wherever
        # upstream is now. Before the first commit, the registered base (`at`,
        # or the branch head).
        working_ref = self._working_ref(key)
        if working_ref is None:
            if m.pin is not None and Capability.PIN in eff:
                return self._open_pinned(m, backend, eff)
            if Capability.ADDRESSABLE in eff:
                state = m.state or backend.fingerprint(m.locator, None)
                return backend.open(m.locator, state, read_only=True)
        return backend.open(m.locator, working_ref, read_only=True)

    def _open_writable(self: Repo, key: str) -> Handle:
        """`open(key, read_only=False)` once the checkout lock has refreshed
        the workspace and the manifests (see `open`)."""
        m = self.objects.get(key)
        if m is None:
            raise ConfigError(f"no such object: {key}")
        backend = self.backend_for(m.kind)
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
            if self.workspace.bookmark is None:
                raise StaleWorkingCopyError(
                    f"no working ref for {key!r}: this working copy is on no "
                    "bookmark and is read-only; `tether new -b NAME` to write"
                )
            raise StaleWorkingCopyError(
                f"no working ref for {key!r}; run `tether new` first"
            )
        return backend.open(m.locator, working_ref, read_only=False)

    def _pinned_source(
        self, m: ObjectManifest, backend: ObjectBackend, eff: Capability
    ) -> Pin | State:
        """What a committed object is read or forked from: its pin, checked.

        The manifest names both a state and the native ref that was created
        for it. Before the ref is trusted, `verify` confirms it still points
        at that state:

        - OK: the pin (exact, and the retention hold).
        - MISSING (someone deleted the ref): the recorded state, if the
          backend can address it -- the store may still hold it, as it does
          between a deletion and `repair`.
        - DRIFTED (someone moved the ref): refused. Reading or forking a
          drifted pin would hand back the wrong data under a commit's name.

        Raises:
            PinDriftError: The pin points at a different state.
            BackendError: The pin is gone and the backend cannot address the
                recorded state.
        """
        assert m.state is not None
        if m.pin is None:
            if Capability.ADDRESSABLE in eff:
                return dict(m.state)
            raise CapabilityError(
                f"{m.key!r} has no pin and {m.kind} cannot address a recorded state",
                key=m.key,
                kind=m.kind,
            )
        report = backend.verify(m.locator, m.state, m.pin, deep=False)
        if report.status is VerifyStatus.DRIFTED:
            raise PinDriftError(
                f"pin {m.pin.ref} of {m.key!r} no longer names the committed state "
                f"({report.message}); `tether verify` reports it and `repair` never "
                "overwrites a drifted pin -- move the ref back by hand, or use a "
                "commit whose pins hold",
                key=m.key,
                kind=m.kind,
            )
        if report.status is VerifyStatus.MISSING:
            if Capability.ADDRESSABLE in eff:
                return dict(m.state)
            raise BackendError(f"pin missing: {report.message}", key=m.key, kind=m.kind)
        return m.pin

    def _open_pinned(
        self, m: ObjectManifest, backend: ObjectBackend, eff: Capability
    ) -> Handle:
        """Open read-only at the committed state (see `_pinned_source`)."""
        return backend.open(
            m.locator, self._pinned_source(m, backend, eff), read_only=True
        )

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
            return self._open_pinned(m, backend, eff)
        if Capability.ADDRESSABLE in eff:
            return backend.open(m.locator, m.state, read_only=True)
        raise CapabilityError(
            f"{key!r} ({m.kind}) is Observed-tier; its state at {rev} is not "
            f"recoverable",
            key=key,
            kind=m.kind,
        )

    # -- history --------------------------------------------------------------- #
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

    # -- verify ---------------------------------------------------------------- #
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

    # -- diff ------------------------------------------------------------------ #
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
            b = self.objects  # `diff REV`: that revision -> the working tree

        entries: list[DiffEntry] = []
        for key in sorted(set(a) | set(b)):
            ma = a.get(key)
            mb = b.get(key)
            pa = ma.pin.id if ma and ma.pin else None
            pb = mb.pin.id if mb and mb.pin else None
            why: tuple[str, ...] = ()
            if ma and not mb:
                change = "removed"
            elif mb and not ma:
                change = "added"
            else:
                assert ma is not None and mb is not None
                why = tuple(
                    name
                    for name, differs in (
                        ("state", ma.state != mb.state),
                        ("pin", pa != pb),
                        ("locator", dict(ma.locator) != dict(mb.locator)),
                        ("policy", ma.policy != mb.policy),
                    )
                    if differs
                )
                change = "changed" if why else "unchanged"
            entries.append(
                DiffEntry(key=key, change=change, a_pin=pa, b_pin=pb, why=why)
            )

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
            if e.change != "changed" or "state" not in e.why:
                continue  # same state: nothing inside the object to describe
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
