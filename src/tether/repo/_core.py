"""The engine core: state, locks, construction, backends, the operation log, and
plan verification. Command families are mixins over :class:`RepoCore`."""

from __future__ import annotations

import contextlib
import dataclasses
import os
import warnings

try:  # POSIX advisory locks; Windows has no fcntl and gets no writer lock
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]
from collections.abc import Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Self

from tether import manifest as _m
from tether.backends.base import (
    Capability,
    ObjectBackend,
    VerifyStatus,
    absolutize_locator,
    base_at,
    build_backend,
    check_committed_config,
    content_state,
    effective_capabilities,
    safe_config_keys,
    safe_option_keys,
)
from tether.errors import (
    ConfigError,
    MultiObjectError,
    StalePlanError,
    StaleWorkingCopyError,
    TetherError,
    VcsError,
)
from tether.manifest import (
    CONFIG_VERSION,
    Locator,
    ObjectManifest,
    Pin,
    RepoConfig,
    State,
    WorkspaceState,
    claim_workspace,
    ensure_ignored,
    ensure_layout,
    find_dataset_root,
    listing_name,
    listings_dir,
    manifest_hash,
    read_config,
    read_listing,
    read_objects,
    read_secrets,
    read_workspace,
    working_ref_name,
    workspace_path,
    write_config,
    write_workspace,
)
from tether.oplog import (
    OpEntry,
    TouchedStore,
    append_op,
    append_touched,
    mark_done,
    mark_progress,
    read_ops,
)
from tether.plan import Plan, Precondition
from tether.repo._reports import (
    VcsDrift,
    short_state,
)
from tether.vcs import VcsAdapter, detect_vcs

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tether.experimental.lifecycle import CreatedStore
    from tether.experimental.registry import ExportBundle, ImportReport, ImportSpec
    from tether.repo import Repo
    from tether.upgrade import UpgradeReport

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


_UNTRUSTED_VCS_KEYS = ("git_path", "jj_path")


def _vcs_executables(root: Path, config: RepoConfig) -> tuple[str | None, str | None]:
    """Where `git` and `jj` come from: `.tether/secrets.toml`, then the
    environment (`TETHER_GIT`, `TETHER_JJ`), never the committed `tether.toml`.

    Raises:
        ConfigError: The committed file names an executable -- a clone must
            not choose what runs on your machine.
    """
    committed = [k for k in _UNTRUSTED_VCS_KEYS if config.vcs.get(k)]
    if committed:
        raise ConfigError(
            f"tether.toml [vcs] sets {', '.join(committed)}; a committed file "
            "arrives with every clone and must not choose executables. Put it in "
            ".tether/secrets.toml (untracked) under [vcs], or set TETHER_GIT / "
            "TETHER_JJ"
        )
    secrets = read_secrets(root)
    jj = secrets.vcs.get("jj_path") or os.environ.get("TETHER_JJ")
    git = secrets.vcs.get("git_path") or os.environ.get("TETHER_GIT")
    return (str(jj) if jj else None, str(git) if git else None)


class RepoCore:
    """The engine's shared state and helpers; command families are mixins
    over it (see :mod:`tether.repo`). Not constructed directly -- `Repo` is.
    """

    def __init__(
        self,
        root: Path,
        config: RepoConfig,
        vcs: VcsAdapter,
        *,
        allow_outdated: bool = False,
    ) -> None:
        if config.version > CONFIG_VERSION:
            raise ConfigError(
                f"tether.toml is version {config.version}, newer than this tether "
                f"understands ({CONFIG_VERSION}); upgrade tether-vcs"
            )
        if config.version < CONFIG_VERSION and not allow_outdated:
            from tether.upgrade import outdated_message

            raise ConfigError(outdated_message(config.version))
        self.root = root
        self.config = config
        self.vcs = vcs
        # A read-only command must not dirty the tree, so the ignore file is
        # only touched when an untracked file that exists is not yet ignored
        # (an older dataset that just gained a secrets.toml); `init` and
        # `upgrade` write the full list.
        ensure_ignored(root, only_present=True)
        self.objects = read_objects(root)
        self.workspace = read_workspace(root)
        if not workspace_path(root).is_file():
            # A read-only checkout keeps the id in memory, as before.
            with contextlib.suppress(OSError):
                self.workspace = claim_workspace(root, self.workspace)
        self.secrets = read_secrets(root)
        if self.secrets.insecure:
            warnings.warn(
                f"{_m.secrets_path(root)} is readable by other users; "
                "`chmod 600` it -- it may hold credentials",
                stacklevel=2,
            )
        self._backends: dict[str, ObjectBackend] = {}
        self._touched: set[str] | None = None
        """Identities this `Repo` has already recorded in the touched journal."""
        self._touched_warned = False
        self._objects_gen = 0
        """Bumped by every in-place change to `objects` (add, remove, set); a
        reload replaces the dict. Together they date the per-object secret
        rules a backend holds (see `backend_for`)."""
        self._secret_stamp: dict[str, tuple[object, int]] = {}
        # Manifest text -> parsed manifest. History walks re-read the same
        # (unchanged) manifest at hundreds of commits; parse each text once.
        self._manifest_cache: dict[str, ObjectManifest] = {}
        self._lock_depth = 0
        self._repo_lock_depth = 0

    @contextlib.contextmanager
    def _writer_lock(self) -> Iterator[None]:
        """One writer per checkout, for the duration of a writing command.

        Two `tether` processes racing in the same checkout would interleave
        journal entries and `workspace.toml` writes and plan against each
        other's half-done work. The lock is advisory (`flock` on
        `.tether/lock`), re-entrant within one `Repo`, and held only while the
        command runs, so a stale file after a crash locks nothing.

        Taking the lock also re-reads `workspace.toml` and the manifests: a
        `Repo` that has lived a while (a notebook, a service) must not write
        back the workspace it loaded at construction over what another
        process wrote since. Between commands a `Repo` holds no unsaved state,
        so the refresh loses nothing.
        """
        if self._lock_depth:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        if fcntl is None:
            # No advisory locks (Windows): still one refresh per outermost
            # entry, and nested entries must not re-read half-written state.
            self._lock_depth = 1
            try:
                self._refresh()
                yield
            finally:
                self._lock_depth = 0
            return
        import time

        path = _m.tether_path(self.root) / _m.LOCK_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as fh:
            # Wait, as the repository lock does: a `status` running while a
            # `commit` finishes should follow it, not fail.
            deadline = time.monotonic() + self.LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise TetherError(
                            "another tether command is writing in this checkout "
                            f"({path} is locked); wait for it to finish"
                        ) from exc
                    time.sleep(0.05)
            self._lock_depth = 1
            try:
                self._refresh()
                yield
            finally:
                self._lock_depth = 0
                with contextlib.suppress(OSError):
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    REPO_LOCK_TIMEOUT = 60.0
    """Seconds a command waits for the repository-wide lock before giving up."""

    LOCK_TIMEOUT = 30.0
    """Seconds a command waits for the checkout lock before giving up."""

    @contextlib.contextmanager
    def _repo_lock(self) -> Iterator[None]:
        """One writer per *repository*, across every checkout of it.

        The checkout lock cannot order a `gc` here against a `commit` in
        another workspace of the same repository, and that pair races: the
        commit references a pin after gc decided it was unreferenced and
        before it was released. Commands that create references to pins
        (`commit`, `pull`) or release pins (`gc`), and the ones that remove
        commits from history (`undo`, `abandon`), take this lock -- a `flock`
        on `tether.lock` in the store every checkout shares (git's common
        dir, jj's repo dir). Waiting, not failing: these commands are short.
        """
        if self._repo_lock_depth or fcntl is None:
            self._repo_lock_depth += 1
            try:
                yield
            finally:
                self._repo_lock_depth -= 1
            return
        import time

        path = self.vcs.shared_dir() / "tether.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as fh:
            deadline = time.monotonic() + self.REPO_LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise TetherError(
                            "another tether command is committing or collecting "
                            f"in a checkout of this repository ({path} is locked); "
                            "wait for it to finish"
                        ) from exc
                    time.sleep(0.05)
            self._repo_lock_depth = 1
            try:
                yield
            finally:
                self._repo_lock_depth = 0
                with contextlib.suppress(OSError):
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _refresh(self) -> None:
        """Reload the per-checkout state from disk (see `_writer_lock`).

        A checkout with no `workspace.toml` keeps the in-memory state: one
        construction could not write (a read-only checkout), or
        `forget-workspace` removed it.
        """
        if _m.workspace_path(self.root).is_file():
            self.workspace = read_workspace(self.root)
        self.objects = read_objects(self.root)

    # -- construction ---------------------------------------------------------- #
    @classmethod
    def init(
        cls,
        path: Path | str = ".",
        *,
        config: RepoConfig | None = None,
    ) -> Self:
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
        ensure_layout(root)  # writes .tether/.gitignore for every untracked file
        write_config(root, config)
        jj_path, git_path = _vcs_executables(root, config)
        vcs = detect_vcs(
            root,
            jj_path=jj_path,
            git_path=git_path,
            prefer=str(config.vcs.get("prefer", "jj")),
        )
        repo = cls(root, config, vcs)
        repo.workspace.bookmark = repo.adopt_trunk()
        write_workspace(root, repo.workspace)
        return repo

    def adopt_trunk(self) -> str:
        """Put this working copy on a bookmark and return its name.

        The trunk bookmark stands for every object's upstream branch, so a
        fresh dataset starts there, as a fresh git repository is on `main`.
        Under git the branch HEAD is on *is* the trunk, whatever it is called
        (`[vcs] trunk` is written to say so); a detached HEAD is parked on the
        trunk branch. Under jj the trunk bookmark is created at `@` when the
        repository has none; when it has bookmarks but not the trunk, the one
        the working copy is on is used if it is unambiguous, else the trunk is
        created. Used by `init` and by the v3 upgrade.
        """
        vcs = self.vcs
        trunk = self.config.trunk
        here = vcs.current_bookmarks()
        if vcs.kind == "git":
            if here and here[0] != trunk:
                self.config.vcs["trunk"] = here[0]
                write_config(self.root, self.config)
                return here[0]
            if not here:  # detached HEAD: park the dataset on the trunk branch
                vcs.new_bookmark(trunk, None)
            return trunk
        if trunk in vcs.bookmarks():
            return trunk if trunk in here or len(here) != 1 else here[0]
        if len(here) == 1:
            return here[0]
        vcs.bookmark_set(trunk, "@")
        return trunk

    @classmethod
    def find(cls, path: Path | str = ".", *, allow_outdated: bool = False) -> Self:
        """Open the dataset whose `tether.toml` is at or above `path`.

        Args:
            path: Anywhere inside the dataset.
            allow_outdated: Open a dataset whose `[tether] version` is older
                than this tether's (only `upgrade` should).

        Raises:
            ConfigError: If no dataset root is found, or the dataset's version
                is not this tether's (run `tether upgrade`).
        """
        root = find_dataset_root(Path(path))
        if root is None:
            raise ConfigError(f"no tether dataset found at or above {path}")
        config = read_config(root)
        jj_path, git_path = _vcs_executables(root, config)
        vcs = detect_vcs(
            root,
            jj_path=jj_path,
            git_path=git_path,
            prefer=str(config.vcs.get("prefer", "jj")),
        )
        return cls(root, config, vcs, allow_outdated=allow_outdated)

    # -- internals ------------------------------------------------------------- #
    def backend_for(self, kind: str) -> ObjectBackend:
        """Return the (cached) backend instance for `kind`.

        Built with `config.backends[kind]` on first use. Its per-object
        credential rules follow the objects: an `[objects."<key>"]` entry in
        `secrets.toml` names a key, and which store that key means is known
        only once the object is registered -- so the rules are recomputed when
        `objects` has changed since they were last pushed (a reload replaces
        the dict; `add`/`remove`/`set` bump `_objects_gen`), and an object
        added after the backend was first built gets its credentials too. On
        the hot paths (a fingerprint fan-out, a history walk) nothing is
        recomputed.

        Raises:
            ConfigError: If the kind is unknown or its optional extra is missing.
        """
        backend = self._backends.get(kind)
        if backend is None:
            committed = dict(self.config.backends.get(kind, {}))
            local = dict(self.secrets.backends.get(kind, {}))
            # The committed file is untrusted input: only allowlisted keys, with
            # nested option tables screened. The secrets file may set anything
            # and wins where both set a key.
            check_committed_config(
                kind, safe_config_keys(kind), committed, safe_option_keys(kind)
            )
            backend = build_backend(kind, {**committed, **local})
            backend.configure_cache(_m.tether_path(self.root) / _m.CACHE_DIR)
            backend.configure_secrets(
                {**committed, **local}, self._secret_rules_for(kind, backend)
            )
            self._backends[kind] = backend
        elif self.secrets.objects:
            # The stamp keeps a reference to the dict it saw, so a reloaded
            # `objects` (a new dict) always misses; the counter catches
            # in-place changes.
            stamp = (self.objects, self._objects_gen)
            seen = self._secret_stamp.get(kind)
            if seen is None or seen[0] is not stamp[0] or seen[1] != stamp[1]:
                rules = self._secret_rules_for(kind, backend)
                if rules != getattr(backend, "_secret_rules", None):
                    committed = dict(self.config.backends.get(kind, {}))
                    local = dict(self.secrets.backends.get(kind, {}))
                    backend.configure_secrets({**committed, **local}, rules)
                self._secret_stamp[kind] = stamp
        return backend

    def _secret_rules_for(
        self, kind: str, backend: ObjectBackend
    ) -> dict[str, dict[str, Any]]:
        """URI-prefix -> credential options for `kind`: `[uris."<prefix>"]` as
        written, plus each `[objects."<key>"]` entry as an exact rule on that
        object's own store URI (so an object entry beats a prefix)."""
        rules: dict[str, dict[str, Any]] = dict(self.secrets.uris)
        for key, options in self.secrets.objects.items():
            m = self.objects.get(key)
            if m is None or m.kind != kind:
                continue
            uri = next(
                (str(m.locator[k]) for k in backend.URI_KEYS if m.locator.get(k)), None
            )
            if uri is not None:
                rules[uri] = {**rules.get(uri, {}), **options}
        return rules

    def _working_ref_for(self, key: str) -> str:
        """The store branch this workspace's bookmark stands for (`key` names
        the object; objects sharing a system share the branch)."""
        if self.workspace.bookmark is None:
            raise StaleWorkingCopyError(
                "this working copy is on no bookmark and is read-only; "
                "`tether new -b NAME` to start one, or `tether new NAME` to join one"
            )
        return working_ref_name(self.config.dataset_id, self.workspace.bookmark)

    def bookmark_drift(self) -> list[str]:
        """How the VCS bookmark and this workspace have parted, if they have.

        The bookmark can be deleted or renamed (`jj bookmark delete/rename`),
        the working copy can leave it (`jj new`, `git switch`), or it can be
        moved by hand so its commit no longer describes what the store branches
        hold. VCS-only checks; no store is contacted. Each message says what
        to do. Empty when everything agrees, or on no bookmark.
        """
        b = self.workspace.bookmark
        if b is None:
            return []
        out: list[str] = []
        marks = self.vcs.bookmarks()
        here = self.vcs.current_bookmarks()
        if b not in marks:
            if self.vcs.kind == "git" and here == [b]:
                return []  # unborn branch: HEAD is on it
            renamed = [
                n for n in here if n != b and n != self.config.trunk and n in marks
            ]
            if renamed:
                out.append(
                    f"bookmark {b!r} is gone and {renamed[0]!r} sits where the working "
                    f"copy is -- renamed? `tether new {renamed[0]}` continues there "
                    f"(its store branches are forked anew; {b!r}'s go to "
                    "`gc --prune-bookmarks`)"
                )
            else:
                out.append(
                    f"bookmark {b!r} no longer exists in the VCS; `tether new -b {b}` "
                    "recreates it here, `tether new NAME` joins another; its store "
                    "branches stay until `gc --prune-bookmarks`"
                )
            return out
        on_it = self.vcs.is_ancestor(b, "@") if self.vcs.kind == "jj" else here == [b]
        if not on_it:
            out.append(
                f"the working copy has left bookmark {b!r} (now on "
                f"{', '.join(here) or 'no bookmark'}); `tether new {b}` returns to it, "
                "`tether new` works where you are"
            )
            return out
        # Moved by hand: the bookmark's commit no longer records what this
        # workspace forked from / last committed on its branches.
        moved: list[str] = []
        with contextlib.suppress(Exception):
            at_mark = self._objects_at(marks[b])
            for key, expected in self.workspace.base_states.items():
                m = at_mark.get(key)
                if m is not None and m.state is not None:
                    if not self._same(m.kind, m.state, expected):
                        moved.append(key)
                elif key in self.objects and self.objects[key].state is not None:
                    moved.append(key)
        if moved:
            out.append(
                f"bookmark {b!r} was moved to {marks[b][:12]}: its commit no longer "
                f"records what the store branches of {', '.join(sorted(moved))} hold; "
                f"`tether new {b}` resets them onto the pins there (refused while "
                "they hold unpinned writes; --discard to drop those)"
            )
        return out

    def _check_on_bookmark(self) -> None:
        """Refuse to commit when the VCS working copy has left the workspace's
        bookmark (a `jj new` / `git switch` behind tether's back): the commit
        would move the bookmark somewhere its store branches do not describe.

        Under jj the bookmark must be on the working copy or its parent: that
        is where `new` and `commit` leave it, and the only place from which
        jj's advance-bookmarks setting carries it onto the new commit inside
        the commit's own operation (so `jj undo` reverts both). A bookmark
        further back would need a second operation to catch up, and `jj undo`
        would then strand the commit; refuse instead.
        """
        b = self.workspace.bookmark
        if b is None:
            return
        marks = self.vcs.bookmarks()
        here = self.vcs.current_bookmarks()
        if b not in marks:
            if self.vcs.kind == "git" and here == [b]:
                return  # an unborn branch: HEAD is on it, the first commit births it
            raise StaleWorkingCopyError(
                f"bookmark {b!r} no longer exists; `tether new -b {b}` recreates it, "
                "`tether new NAME` joins another"
            )
        if self.vcs.kind != "jj":
            if here != [b]:
                raise StaleWorkingCopyError(
                    f"the working copy is not on bookmark {b!r} (it moved to "
                    f"{', '.join(here) or 'no bookmark'}); run `tether new {b}` to "
                    "return, or `tether new` to work where you are"
                )
            return
        if not self.vcs.is_ancestor(b, "@"):
            raise StaleWorkingCopyError(
                f"the working copy is not on bookmark {b!r} (it moved to "
                f"{', '.join(here) or 'no bookmark'}); run `tether new {b}` to return, "
                "or `tether new` to work where you are"
            )
        if self.vcs.is_ancestor(b, "@--"):
            raise StaleWorkingCopyError(
                f"bookmark {b!r} is behind the working copy's parent: there are "
                "commits between them that the commit would skip over, and the "
                f"bookmark could not move with it in one operation; run `tether new "
                f"{b} --keep` to put the working copy back on the bookmark (its "
                "branches are untouched)"
            )

    def _pick_bookmark(self, candidates: list[str]) -> str | None:
        """Which of the bookmarks on a commit to work on: the one this
        workspace already has, else the trunk, else the only one, else none."""
        if self.workspace.bookmark in candidates:
            return self.workspace.bookmark
        if self.config.trunk in candidates:
            return self.config.trunk
        return candidates[0] if len(candidates) == 1 else None

    def on_trunk(self) -> bool:
        """Whether this workspace works on the trunk bookmark (`config.trunk`),
        where every object's working ref is its upstream branch."""
        return self.workspace.bookmark == self.config.trunk

    def created_stores(self: Repo) -> list[CreatedStore]:
        """Stores this dataset created (`add --create`) and has not yet removed;
        see :mod:`tether.experimental.lifecycle` (experimental)."""
        from tether.experimental.lifecycle import created_stores

        return created_stores(self)

    def touched_stores(self: Repo) -> list[TouchedStore]:
        """Stores this clone has forked or pinned in; see
        :mod:`tether.experimental.lifecycle` (experimental)."""
        from tether.experimental.lifecycle import touched_stores

        return touched_stores(self)

    def _note_touched(self, key: str, kind: str, locator: Locator) -> None:
        """Record that this clone wrote a ref into `locator`'s store, in the
        core-owned touched journal (`tether-touched.jsonl` beside the
        repository lock), so a later `gc --delete-stores` can release the dead
        refs this clone leaves behind. Best effort: an index miss costs a
        later gc a store it must then be told about with `--store`, never
        data, so nothing here may fail the pin or fork that called it.
        Idempotent and cheap: one in-memory set per `Repo`, one appended line
        per new store."""
        try:
            backend = self.backend_for(kind)
            identity = dict(backend.identity(locator))
            tag = f"{kind}|{_m.canonical_bytes(identity).decode()}"
            if self._touched is None:
                self._touched = set()
            if tag in self._touched:
                return
            append_touched(
                self.vcs.shared_dir(),
                TouchedStore(
                    dataset_id=self.config.dataset_id,
                    kind=kind,
                    identity=identity,
                    locator=dict(locator),
                    key=key,
                    at=_m._now(),
                ),
            )
            self._touched.add(tag)
        except Exception as exc:
            if not self._touched_warned:
                self._touched_warned = True
                warnings.warn(
                    f"could not record {key} in the touched-store index: {exc}",
                    stacklevel=2,
                )

    def _iter_live_workspaces(self) -> Iterator[tuple[Path, WorkspaceState]]:
        """Every live checkout of this dataset that has run tether: its dataset
        root and its `workspace.toml`, this checkout included.

        Walks the VCS's workspaces / worktrees (`VcsAdapter.workspace_roots`)
        and reads each one's state at the dataset's relative path; checkouts
        that never ran tether have no file and are skipped, as is one whose
        file cannot be read (a checkout mid-write, an older format).
        """
        rel = self._dataset_rel()
        for root in self.vcs.workspace_roots():
            if not workspace_path(root / rel).is_file():
                continue
            with contextlib.suppress(Exception):
                yield (root / rel), read_workspace(root / rel)

    def bookmark_holders(self, bookmark: str) -> list[str]:
        """Workspace ids of *other* live checkouts working on `bookmark`."""
        here = self.root.resolve()
        return [
            ws.workspace_id[:8]
            for root, ws in self._iter_live_workspaces()
            if root.resolve() != here
            and ws.bookmark == bookmark
            and ws.workspace_id != self.workspace.workspace_id
        ]

    def _working_ref(self, key: str) -> str | None:
        ref = self.workspace.working_refs.get(key)
        if ref is None and self.on_trunk():
            # On the trunk a Forkable object's working ref *is* its upstream
            # branch, whether or not `new` has recorded it yet -- unless it was
            # registered `--at` a state: that is its position until a `pull`
            # moves it onto the branch.
            m = self.objects.get(key)
            if m is not None and base_at(m.locator) is None:
                backend = self.backend_for(m.kind)
                if Capability.FORK in effective_capabilities(
                    backend, m.locator, m.policy
                ):
                    with contextlib.suppress(TetherError):
                        return backend.base_branch(m.locator)
        return ref

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

    def live_bookmarks(self) -> set[str]:
        """Bookmarks whose store branches must stay: every bookmark the VCS
        has, plus any a live checkout's `workspace.toml` still works on."""
        names = set(self.vcs.bookmarks())
        if self.workspace.bookmark:
            names.add(self.workspace.bookmark)
        names.update(
            ws.bookmark for _, ws in self._iter_live_workspaces() if ws.bookmark
        )
        return names

    def live_workspace_ids(self) -> set[str]:
        """Workspace ids of every live checkout of this dataset (this one included).

        Walks the VCS's workspaces / worktrees (`VcsAdapter.workspace_roots`)
        and reads each one's `.tether/workspace.toml` at the dataset's path.
        Checkouts that never ran tether have no id and contribute nothing.
        """
        ids = {self.workspace.workspace_id}
        ids.update(ws.workspace_id for _, ws in self._iter_live_workspaces())
        return ids

    # -- operation log --------------------------------------------------------- #
    def ops(self, limit: int | None = None) -> list[OpEntry]:
        """This workspace's operation log, newest first (see `tether.oplog`)."""
        entries = list(reversed(read_ops(self.root)))
        return entries[:limit] if limit else entries

    def vcs_drift(self) -> list[VcsDrift]:
        """Dataset commits in the op log that the VCS no longer has.

        A `commit` entry counts as drifted when its commit is not part of
        visible history any more (`VcsAdapter.commit_alive`) and tether did
        not do that itself: entries undone by `tether undo`, dropped by
        `tether abandon`, or rewritten by `abandon` / `upgrade` (whose recorded
        `rewritten_commits` are followed) are not drift. Newest first.
        """
        entries = self.ops()
        gone: set[str] = set()
        alias: dict[str, str] = {}
        for e in entries:
            if e.command == "abandon":
                gone.update(str(c) for c in e.result.get("abandoned") or [])
            for old, new in (e.result.get("rewritten_commits") or {}).items():
                alias[str(old)] = str(new)
        # Resolve every commit the log names first, then ask the VCS once:
        # `status` runs this on every invocation and must stay local.
        wanted: list[tuple[OpEntry, str]] = []
        for e in entries:
            if e.command != "commit" or e.undone_by or not e.result.get("vcs_commit"):
                continue
            commit = str(e.result["vcs_commit"])
            seen: set[str] = set()
            while commit in alias and commit not in seen:
                seen.add(commit)
                commit = alias[commit]
            if commit not in gone:
                wanted.append((e, commit))
        alive = self.vcs.alive_commits(sorted({c for _, c in wanted}))
        out: list[VcsDrift] = []
        for e, commit in wanted:
            if commit in alive:
                continue
            pinned = {k: v for k, v in (e.result.get("pinned") or {}).items() if v}
            referenced: dict[str, bool] = {}
            for k, pid in pinned.items():
                current = self.objects[k].pin if k in self.objects else None
                referenced[k] = current is not None and current.id == pid
            out.append(
                VcsDrift(
                    op=e, commit=str(e.result["vcs_commit"]), referenced=referenced
                )
            )
        return out

    def _log_op(
        self,
        command: str,
        *,
        plan: Plan | None = None,
        result: Mapping[str, Any] | None = None,
        pre: Mapping[str, Any] | None = None,
        undoes: str | None = None,
    ) -> OpEntry:
        """Record an operation after the fact (working-tree-only commands)."""
        entry = OpEntry.now(
            command,
            plan=plan.to_dict() if plan is not None else None,
            result=dict(result or {}),
            pre=dict(pre or {}),
            undoes=undoes,
        )
        append_op(self.root, entry)
        return entry

    def _begin_op(
        self,
        command: str,
        *,
        plan: Plan | None = None,
        pre: Mapping[str, Any] | None = None,
        undoes: str | None = None,
    ) -> OpEntry:
        """Journal an operation *before* its first side effect on a store.

        The entry carries the plan and what `undo` needs, is synced to disk,
        and stays `started` until `_end_op` marks it done. An interruption in
        between leaves a visible, non-undoable record of what was attempted
        instead of silence.
        """
        entry = OpEntry.now(
            command,
            plan=plan.to_dict() if plan is not None else None,
            pre=dict(pre or {}),
            undoes=undoes,
        )
        entry.status = "started"
        append_op(self.root, entry)
        return entry

    def _end_op(
        self,
        entry: OpEntry,
        *,
        result: Mapping[str, Any] | None = None,
        pre: Mapping[str, Any] | None = None,
        undone: str | None = None,
    ) -> OpEntry:
        """Complete a journaled operation with its result (and late `pre`).

        `undone` names the entry this one reversed; the mark rides in the same
        record, so it cannot be lost between two appends.
        """
        entry.result = dict(result or {})
        if pre:
            entry.pre = {**entry.pre, **dict(pre)}
        entry.status = "done"
        mark_done(self.root, entry.id, entry.result, dict(pre or {}), undone=undone)
        return entry

    def _progress(self, op: OpEntry | None, action: str, **detail: Any) -> None:
        """Journal one completed action of a running operation."""
        if op is not None:
            mark_progress(self.root, op.id, action, **detail)

    def incomplete_ops(self) -> list[OpEntry]:
        """Journal entries that began and never completed (an interrupted run)."""
        return [e for e in read_ops(self.root) if e.incomplete]

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
                m = self._resolve_locator(ObjectManifest.from_toml(text))
                self._manifest_cache[text] = m
            result[m.key] = m
        return result

    def _resolve_locator(self, m: ObjectManifest) -> ObjectManifest:
        """Pin down a relative local path in a *historical* manifest.

        Manifests written before v4 could hold a path as typed; the v4
        migration rewrites the working tree, but history keeps the old text.
        Read at `--rev`, such a path means the dataset root -- the same rule
        the migration applied -- not whatever directory this command runs in.
        """
        try:
            backend = self.backend_for(m.kind)
        except TetherError:
            return m  # an uninstalled extra; the locator is not used anyway
        if not backend.LOCAL_PATH_KEYS:
            return m
        resolved = absolutize_locator(backend, dict(m.locator), self.root)
        if resolved == dict(m.locator):
            return m
        return dataclasses.replace(m, locator=resolved)

    def _objects_at(self, rev: str) -> dict[str, ObjectManifest]:
        return self._parse_manifests(self.vcs.files_at(rev, self._objects_reldir()))

    def _iter_history_objects(self) -> Iterator[tuple[str, dict[str, ObjectManifest]]]:
        """Yield ``(commit id, manifests)`` for every commit, via one reader."""
        for rev, files in self.vcs.iter_history_files(self._objects_reldir()):
            yield rev, self._parse_manifests(files)

    def _vcs_commit_landed(self, before: Mapping[str, Any] | None) -> str | None:
        """The commit id if the VCS position moved since `before` (a commit
        landed even though the adapter raised afterwards), else `None`."""
        try:
            now = self.vcs.position()
        except VcsError:
            return None
        then = dict(before or {})
        if now.get("kind") == "jj":
            # The working-copy commit id changes on every snapshot; what tells
            # a landed commit is `@` having moved onto a new parent.
            if now.get("parent") and now.get("parent") != then.get("parent"):
                return str(now["parent"])
            return None
        if now.get("commit") and now.get("commit") != then.get("commit"):
            return str(now["commit"])
        return None

    def _vcs_head_or_none(self) -> str | None:
        """The commit a plan binds to, or `None` before the first commit: HEAD
        under git; under jj the working copy's *parent*, since the working-copy
        commit's own id changes whenever jj snapshots the tree."""
        try:
            if self.vcs.kind == "jj":
                return self.vcs.position().get("parent") or self.vcs.current_rev()
            return self.vcs.current_rev()
        except VcsError:
            return None

    def current_manifest_hash(self) -> str:
        """Hash of the committed object set in the working tree."""
        return manifest_hash(self.objects)

    def stale_keys(self) -> list[str]:
        """Forked objects whose committed manifest no longer matches this workspace.

        Each working ref (existing or pending) was forked from, or last
        committed by this workspace at, `workspace.base_states[key]`. If the
        manifest in the working tree now records a different state -- someone
        else committed, or the VCS working copy moved to another commit -- the
        fork no longer starts where the dataset says it does. `direct` objects
        write to the base branch and are never stale. Registering or removing
        *other* objects does not make a workspace stale.
        """
        stale: list[str] = []
        keys = set(self.workspace.working_refs) | set(self.workspace.pending_forks)
        for key in sorted(keys):
            m = self.objects.get(key)
            if m is None or self.on_trunk() or m.state is None:
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

    def _verify_plan(self, plan: Plan, command: str, *, verify: bool = True) -> None:
        """The drift contract, in one place: refuse to apply a plan whose
        preconditions no longer hold.

        Every `apply_*` calls this before its first action. `plan_*` records
        what it saw as :class:`~tether.plan.Precondition`s (manifest hash,
        workspace id, branch heads, history digest, ...); this re-reads each
        and raises :class:`~tether.errors.StalePlanError` with the plan's own
        message on the first mismatch. A saved plan applied later, or a slow
        apply, therefore acts on the world it was reviewed against or not at
        all.

        Not here, by design: checks that are races *during* apply and are
        handled per action -- gc's post-preflight branch move (reported as
        `kept`), commit's per-object re-fingerprint against the snapshot,
        promote's pin re-verify right before the fast-forward.

        Raises:
            ConfigError: The plan is for another command.
            StalePlanError: A precondition failed.
        """
        self._require_command(plan, command)
        if not verify:
            return
        for pre in plan.preconditions:
            self._check_precondition(pre)

    @staticmethod
    def _require_command(plan: Plan, command: str) -> None:
        """Refuse a plan made for another command."""
        if plan.command != command:
            raise ConfigError(f"expected a {command} plan, got {plan.command!r}")

    def _check_precondition(self, pre: Precondition) -> None:
        """Run one precondition against the current state (see `_verify_plan`)."""
        kind, params = pre.kind, pre.params

        def fail(observed: object = None) -> NoReturn:
            # A literal substitution: the detail is plan-authored text and may
            # carry an object key with braces in it, which `str.format` would
            # read as a field.
            detail = pre.detail or f"{kind} changed since the plan was made"
            detail = detail.replace("{observed}", str(observed))
            if "re-run" not in detail:
                detail += "; re-run the plan"
            raise StalePlanError(detail)

        if kind == "manifest_hash":
            rev = params.get("rev")
            observed = (
                manifest_hash(self._objects_at(self.vcs.resolve(str(rev))))
                if rev
                else self.current_manifest_hash()
            )
            if observed != pre.expected:
                fail(observed)
        elif kind == "workspace_id":
            if pre.expected not in (None, self.workspace.workspace_id):
                fail(self.workspace.workspace_id)
        elif kind == "vcs_head":
            observed = self._vcs_head_or_none()
            if observed != pre.expected:
                fail(observed)
        elif kind == "history_digest":
            observed = self.vcs.history_digest()
            if observed != pre.expected:
                fail(observed)
        elif kind == "config_version":
            if self.config.version != pre.expected:
                fail(self.config.version)
        elif kind == "ref_absent":
            backend = self.backend_for(str(params["backend"]))
            if params["ref"] in backend.list_working_refs(dict(params["locator"])):
                fail(params["ref"])
        elif kind == "ref_head":
            self._require_head(
                self.backend_for(str(params["backend"])),
                dict(params["locator"]),
                str(params["ref"]),
                dict(pre.expected) if pre.expected is not None else None,
                what=str(params.get("what") or pre.key or "plan"),
            )
        elif kind == "base_state":
            backend = self.backend_for(str(params["backend"]))
            locator = dict(params["locator"])
            base_locator = {k: v for k, v in locator.items() if k != "at"}
            observed = backend.fingerprint(base_locator, None)
            if not self._same(str(params["backend"]), observed, pre.expected):
                fail(short_state(observed))
        elif kind == "pin_state":
            backend = self.backend_for(str(params["backend"]))
            pin = Pin.from_dict(dict(params["pin"]))
            checked = backend.verify(
                dict(params["locator"]), dict(pre.expected), pin, deep=False
            )
            if checked.status is not VerifyStatus.OK:
                fail(f"{checked.status.value}: {checked.message}")
        elif kind == "no_new_holders":
            holders = self.bookmark_holders(str(params["bookmark"]))
            if holders:
                fail(", ".join(holders))
        elif kind == "bookmark_head":
            observed = self.vcs.bookmarks().get(str(params["bookmark"]))
            if observed != pre.expected:
                fail(observed or "gone")
        elif kind == "store_empty":
            # A created store gc is about to remove: nothing may remain in it
            # but tether's own refs the same plan deletes first (`ignoring`).
            # `apply_gc` asks once more, with nothing ignored, right before
            # the delete.
            backend = self.backend_for(str(params["backend"]))
            observed = backend.is_ref_empty(
                dict(params["locator"]), ignoring=set(params.get("ignoring") or ())
            )
            if observed is not True:
                fail("cannot tell" if observed is None else "not empty")
        else:  # pragma: no cover - PRECONDITION_KINDS guards the constructor
            raise ConfigError(f"unknown plan precondition {kind!r}")

    def _require_head(
        self,
        backend: ObjectBackend,
        locator: Locator,
        ref: str,
        expected: State | None,
        *,
        what: str,
    ) -> None:
        """Refuse a destructive step when `ref` no longer holds what the plan saw.

        Plans record the head of every branch they will delete or reset. A
        saved plan applied later, or a slow apply, must not act on a branch
        that gained writes in between: re-read the head immediately before
        the step and stop with `StalePlanError` if it moved. `expected` is
        `None` when the plan could not read the head; then there is nothing
        to compare and the plan's own verdict stands.
        """
        if expected is None:
            # The plan could not read this head and (under --force) decided
            # blind. If it can be read now, the plan should be made again with
            # the head in view rather than act on what nobody has seen.
            try:
                now = backend.fingerprint(locator, ref)
            except TetherError:
                return  # still unreadable: the plan's verdict stands
            raise StalePlanError(
                f"{what}: the plan could not read {ref}'s head, but it reads now "
                f"({short_state(now)}); re-run the plan to review it"
            )
        now = backend.fingerprint(locator, ref)
        if not self._same(backend.kind, now, expected):
            raise StalePlanError(
                f"{what}: {ref} moved since the plan was made "
                f"({short_state(expected)} -> {short_state(now)}); re-run the plan"
            )

    # -- upgrade --------------------------------------------------------------- #
    # -- upgrade (tether.upgrade; removed at 0.1.0) ---------------------------- #
    # Thin delegates: the alpha-format migration lives under `tether.upgrade`
    # and is imported on first use. See that package's docstring for the
    # removal contract.
    def plan_upgrade(self: Repo, *, ignore_immutable: bool = False) -> Plan:
        """Compute what bringing this dataset to the current version would do;
        see :func:`tether.upgrade.plan_upgrade`."""
        from tether.upgrade import plan_upgrade

        return plan_upgrade(self, ignore_immutable=ignore_immutable)

    def apply_upgrade(self: Repo, plan: Plan) -> UpgradeReport:
        """Execute a plan from `plan_upgrade`; see
        :func:`tether.upgrade.apply_upgrade`."""
        from tether.upgrade import apply_upgrade

        return apply_upgrade(self, plan)

    def upgrade(self: Repo, *, ignore_immutable: bool = False) -> UpgradeReport:
        """Bring the dataset to this tether's version; see
        :func:`tether.upgrade.upgrade`."""
        from tether.upgrade import upgrade

        return upgrade(self, ignore_immutable=ignore_immutable)

    # -- registry (experimental; tether.experimental.registry.ops) ------------- #
    # Thin delegates: the bodies live under `tether.experimental` and are
    # imported on first use, so `import tether` never loads the registry
    # layer. `repo.export()` and friends keep working unchanged.
    def export(
        self: Repo,
        revs: Sequence[str] | None = None,
        *,
        listings: bool = False,
        workspace: bool = False,
    ) -> ExportBundle:
        """Derive relational tables from the repository's history; see
        :func:`tether.experimental.registry.ops.export`."""
        from tether.experimental.registry.ops import export

        return export(self, revs, listings=listings, workspace=workspace)

    def plan_import(
        self: Repo,
        specs: Sequence[ImportSpec],
        *,
        sync: bool = False,
        notes: Sequence[str] = (),
    ) -> Plan:
        """Diff desired objects against the working tree without writing; see
        :func:`tether.experimental.registry.ops.plan_import`."""
        from tether.experimental.registry.ops import plan_import

        return plan_import(self, specs, sync=sync, notes=notes)

    def apply_import(self: Repo, plan: Plan, *, verify: bool = True) -> ImportReport:
        """Write the manifests a `plan_import` plan describes; see
        :func:`tether.experimental.registry.ops.apply_import`."""
        from tether.experimental.registry.ops import apply_import

        return apply_import(self, plan, verify=verify)

    def import_objects(
        self: Repo, rows: Iterable[Mapping[str, Any]], *, sync: bool = False
    ) -> ImportReport:
        """Register / update / remove objects from canonical rows; see
        :func:`tether.experimental.registry.ops.import_objects`."""
        from tether.experimental.registry.ops import import_objects

        return import_objects(self, rows, sync=sync)
