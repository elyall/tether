"""Backend protocol, capability tiers, and a small registry.

A *backend* maps tether operations onto one class of system (files, Icechunk,
Neon, ...). Backends declare their capabilities; the engine (``tether.repo``)
enforces them per command and degrades explicitly, and the conformance suite
(``testing.py``) runs the tier-appropriate checks against any backend.

One backend instance serves every object of its kind; per-object addressing is
carried in the ``locator`` argument to each method, so backends are cheap,
stateless coordinators over user-supplied resources.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from enum import Enum, Flag, auto
from pathlib import Path
from typing import Any, Protocol, TypeVar, runtime_checkable

from tether.errors import CapabilityError
from tether.handles import Handle
from tether.manifest import Locator, Pin, State


class Capability(Flag):
    """What a backend can do.

    The first four flags are cumulative tiers (see `Tier` and `tier_of`); the
    rest are orthogonal refinements the engine and the docs use to explain
    behavior.
    """

    NONE = 0
    """No capabilities."""
    FINGERPRINT = auto()
    """Can read the current state (drift detection). Every backend has this."""
    ADDRESSABLE = auto()
    """A recorded state can be read back later without a native ref."""
    PIN = auto()
    """Can create and delete a durable, GC-proof native ref for a state."""
    FORK = auto()
    """Can create a writable branch off a pin."""
    CHEAP_FINGERPRINT = auto()
    """Fingerprints are metadata-only; no heavy connection is opened."""
    RETENTION_BOUND = auto()
    """Recorded/pinned states expire with the system's retention window."""
    NEEDS_QUIESCENCE = auto()
    """`commit` should check for active writers first."""
    ATOMIC_REF = auto()
    """Pins have create-if-absent semantics."""
    DIFF = auto()
    """Can describe what changed between two recorded states."""
    HISTORY = auto()
    """Can list the system's native history (`ObjectBackend.history`)."""
    BRANCH_IS_STORAGE = auto()
    """Deleting a branch reclaims its data immediately (Neon); `gc` never
    deletes such a working branch without `force_prune`."""
    PROMOTE = auto()
    """Can fast-forward the base branch to a working branch's head
    (`ObjectBackend.promote`)."""
    MERGE = auto()
    """Can three-way merge a working branch into the base branch
    (`ObjectBackend.merge`), raising `MergeConflict` when it cannot."""
    CREATE = auto()
    """Can make an empty store at a locator, mark it as tether's, tell when
    nothing but tether's own refs remain in it, and remove it
    (`ObjectBackend.create` / `owner` / `is_ref_empty` / `delete_store`).
    **Experimental**: the feature built on it (`add --create`,
    `gc --delete-stores`; `tether.experimental.lifecycle`) is the one tether
    operation with no `repair`, and has not yet run against real resources."""


class Tier(Enum):
    """The cumulative capability tier of a backend (or of one object)."""

    OBSERVED = "observed"
    """Drift detection only; committed state is not recoverable."""
    ADDRESSABLE = "addressable"
    """Committed state can be read back; nothing to create or GC."""
    PINNABLE = "pinnable"
    """Commit creates a durable native ref; `verify` and `gc` apply."""
    FORKABLE = "forkable"
    """`new` forks writable branches; `open` returns writable handles."""


def tier_of(caps: Capability) -> Tier:
    if Capability.FORK in caps:
        return Tier.FORKABLE
    if Capability.PIN in caps:
        return Tier.PINNABLE
    if Capability.ADDRESSABLE in caps:
        return Tier.ADDRESSABLE
    return Tier.OBSERVED


class VerifyStatus(Enum):
    """Outcome of `ObjectBackend.verify`."""

    OK = "ok"
    """The pin (or recorded state) resolves to what the manifest says."""
    DRIFTED = "drifted"
    """The pin exists but points at a different state."""
    MISSING = "missing"
    """The pin or recorded state is gone (deleted, expired)."""
    UNKNOWN = "unknown"
    """Cannot tell cheaply; verify with `deep=True`."""


@dataclass
class VerifyReport:
    """Result of `ObjectBackend.verify` for one object."""

    status: VerifyStatus
    """The outcome."""
    message: str = ""
    """Human-readable detail (what it points at, why it is unknown, ...)."""

    @property
    def ok(self) -> bool:
        """`True` when `status` is `VerifyStatus.OK`."""
        return self.status is VerifyStatus.OK


# --------------------------------------------------------------------------- #
# Content diffs
# --------------------------------------------------------------------------- #
MAX_DIFF_ENTRIES = 2000


@dataclass
class ChangeEntry:
    """One changed thing inside an object: a file, table, array, key, ..."""

    path: str
    """What changed (file path, table name, array path, ...)."""
    change: str
    """`"added"`, `"removed"`, `"modified"`, or `"renamed"`."""
    detail: str = ""
    """Backend-specific detail (`"+2 rows"`, `"+1 -1"`, `"12 chunks"`)."""

    def to_dict(self) -> dict[str, str]:
        d = {"path": self.path, "change": self.change}
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclass
class ObjectDiff:
    """What changed inside one object between two recorded states.

    Backends describe changes at whatever granularity their system exposes
    natively and cheaply (files, tables, arrays, fragments, versions); ``unit``
    names it. Entries are capped at :data:`MAX_DIFF_ENTRIES`; counts are not.
    """

    unit: str = "entries"
    """What is being counted: files, tables, arrays, fragments, commits, ..."""
    added: int = 0
    """Exact count of added units."""
    removed: int = 0
    """Exact count of removed units."""
    modified: int = 0
    """Exact count of modified (or renamed) units."""
    entries: list[ChangeEntry] = field(default_factory=list)
    """Per-unit entries, capped at `MAX_DIFF_ENTRIES`."""
    truncated: bool = False
    """Whether entries were dropped because of the cap."""
    note: str = ""
    """Context the counts alone do not convey (e.g. a missing listing)."""

    def add(self, path: str, change: str, detail: str = "") -> None:
        """Record one change: bump the matching counter and append an entry."""
        if change == "added":
            self.added += 1
        elif change == "removed":
            self.removed += 1
        else:
            self.modified += 1
        if len(self.entries) < MAX_DIFF_ENTRIES:
            self.entries.append(ChangeEntry(path, change, detail))
        else:
            self.truncated = True

    @property
    def is_empty(self) -> bool:
        """`True` when nothing changed."""
        return not (self.added or self.removed or self.modified or self.entries)

    @property
    def summary(self) -> str:
        """One line: `"+A -R ~M <unit>"` plus truncation and note."""
        text = f"+{self.added} -{self.removed} ~{self.modified} {self.unit}"
        if self.truncated:
            text += f" (first {len(self.entries)} shown)"
        if self.note:
            text += f"; {self.note}"
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "unit": self.unit,
            "added": self.added,
            "removed": self.removed,
            "modified": self.modified,
            "truncated": self.truncated,
            "note": self.note,
            "entries": [e.to_dict() for e in self.entries],
        }


Listings = tuple[str | None, str | None]


@dataclass
class HistoryEntry:
    """One native commit / snapshot / version, as listed by `ObjectBackend.history`.

    `id` is what the backend accepts as the locator's `at` field, so a user can
    pick an entry and register (or re-base) an object at exactly that state.
    """

    id: str
    """Native identifier: snapshot id, version, commit hash, ..."""
    when: str | None = None
    """ISO-8601 timestamp (UTC) when known."""
    message: str = ""
    """Commit message or a synthesized description of the change."""
    refs: list[str] = field(default_factory=list)
    """Native branch/tag names pointing here (tether pins appear as `tether.<id>`)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "when": self.when,
            "message": self.message,
            "refs": list(self.refs),
        }


@runtime_checkable
class ObjectBackend(Protocol):
    """The operations tether needs from one class of system.

    ``listing``, ``diff``, and ``history`` have default implementations (no
    listing; capability errors) so backends without ``DIFF`` / ``HISTORY`` need
    not define them.

    Locators may carry an ``at`` field naming a specific native state (a
    snapshot id, version, commit, or tag). A backend that supports it treats the
    object as *detached* there: ``fingerprint(locator, None)`` returns that state
    instead of the base branch's head, so ``commit`` pins it and ``new`` forks
    from it. Once a working ref exists it takes precedence.
    """

    kind: str
    capabilities: Capability

    MATURITY: str = "stable"
    """How much the backend has been exercised: ``"stable"`` runs its full
    lifecycle against the real system in CI (embedded stores and libraries);
    ``"experimental"`` is tested against a fake of a network service and has
    not been run against the service itself by the maintainers. `tether
    backends` prints it; `add` mentions it for experimental kinds."""

    VOLATILE_KEYS: frozenset[str] = frozenset()
    """State keys that *address* the data without identifying it -- a Neon LSN
    that advances on checkpoints, a change id derived from a sha. They stay in
    the state (``pin`` and ``open`` need them) but are excluded from equality
    and from pin ids; see :func:`content_state`."""

    def identity(self, locator: Locator) -> Locator:
        """Locator subset that participates in the content-addressed pin id.

        Defaults to the full locator; override to exclude non-identity fields
        (region, credential references, source branch, ...).
        """
        return dict(locator)

    LOCAL_PATH_KEYS: tuple[str, ...] = ()
    """Locator keys whose value may be a local filesystem path. The engine
    turns a relative one into an absolute path when the object is registered
    (`add`, `import`), against the caller's working directory, so a committed
    locator means the same path from every directory and every clone."""

    LOCAL_PATH_PREFIXES: tuple[str, ...] = ()
    """Prefixes a :attr:`LOCAL_PATH_KEYS` value may carry before its path
    (DuckLake's `ducklake:` or `ducklake:sqlite:`), which would otherwise read
    as a URL scheme. The path after the longest matching prefix is resolved
    like a bare one."""

    SAFE_CONFIG_KEYS: frozenset[str] = frozenset()
    """`[backends.<kind>]` keys the *committed* `tether.toml` may set. A clone
    arrives with that file, so anything that chooses an executable, an
    endpoint credentials are sent to, SQL to run, or which environment
    variable holds a secret must come from the untracked `.tether/secrets.toml`
    or the environment instead. A key whose value is a table (such as
    `storage_options`) must also appear in :attr:`SAFE_OPTION_KEYS`; see
    :func:`check_committed_config`."""

    SAFE_OPTION_KEYS: Mapping[str, frozenset[str]] = {}
    """For each committed option *table* (`storage_options`, `catalog`), the
    keys a clone may set inside it. An allowlist, not a pattern: a key the
    backend has not named is refused, so a new endpoint- or credential-shaped
    option cannot slip through by spelling. Everything else in that table
    comes from `secrets.toml`, where the same table may hold any key."""

    URI_KEYS: tuple[str, ...] = ("uri", "path")
    """Locator keys that name the object's store, in order of preference; the
    per-URI entries of `secrets.toml` are matched against the first present."""

    _secret_defaults: dict[str, Any]
    _secret_rules: dict[str, dict[str, Any]]

    def validate_locator(self, locator: Locator) -> None:
        """Refuse a locator this backend must never be handed.

        Called when an object is registered (`add`, `import`) so the refusal
        comes with the manifest's author present, not at the first read of a
        clone. Default: accept. Raise `BackendError` to refuse.
        """

    def configure_secrets(
        self, defaults: Mapping[str, Any], rules: Mapping[str, Mapping[str, Any]]
    ) -> None:
        """Receive the untracked per-checkout settings for this kind.

        `defaults` is the merged `[backends.<kind>]` (committed allowlisted
        keys under the secrets file's); `rules` maps a URI prefix to the
        credential options for objects under it (the engine folds
        `[objects."<key>"]` entries in as exact-URI rules). Backends read
        them through :meth:`secrets_for`.
        """
        self._secret_defaults = dict(defaults)
        self._secret_rules = {str(k): dict(v) for k, v in rules.items()}

    def secrets_for(self, locator: Locator) -> dict[str, Any]:
        """Credential options for one object: the longest matching URI prefix
        rule over the kind defaults; empty when nothing applies (the backend
        then falls back to the environment, as it always did)."""
        defaults = dict(getattr(self, "_secret_defaults", {}) or {})
        rules = getattr(self, "_secret_rules", {}) or {}
        uri = next((str(locator[k]) for k in self.URI_KEYS if locator.get(k)), None)
        if uri is None or not rules:
            return defaults
        best = max((p for p in rules if uri.startswith(p)), key=len, default=None)
        return {**defaults, **rules[best]} if best is not None else defaults

    def state_addressable(self, locator: Locator, state: State) -> bool:
        """Whether *this* recorded state can be opened again later.

        The capability says the backend can address states in general; a
        particular fingerprint may still lack the coordinate (an object in a
        bucket without versioning has no `version_id`). `commit` records such
        a state as not recoverable instead of promising a read it cannot do.
        """
        return True

    def branch_scope(self, locator: Locator) -> str:
        """The native resource that owns branches, as a stable string.

        Two objects with the same scope share one branch namespace: a fork
        under a bookmark creates *one* branch for both, and the engine forks
        it once and lets every member write through it. Defaults to the
        canonical identity (one object per system). Override where several
        objects legitimately live in one branch space -- a Neon project, an
        Iceberg table with several object keys -- and where the identity
        carries fields the branch does not (a database, a source branch).
        """
        from tether.manifest import canonical_bytes

        return canonical_bytes(self.identity(locator)).decode()

    def ref_namespace(self, locator: Locator) -> str:
        """The native resource whose pins ``list_pins`` returns, as a string.

        ``gc`` compares the pins it lists against the pins every object *in
        that namespace* references; listing a project-wide namespace against
        one object's references would release the others' pins. Defaults to
        the canonical identity.
        """
        from tether.manifest import canonical_bytes

        return canonical_bytes(self.identity(locator)).decode()

    # The protocol's own bodies raise rather than return None: a backend that
    # advertises a capability and forgets the method fails loudly at the call,
    # not with a `None` the engine writes into a manifest.
    def fingerprint(self, locator: Locator, working_ref: str | None) -> State:
        """Read the current state at ``working_ref`` (or the locator's base)."""
        raise NotImplementedError(f"{self.kind} backend does not fingerprint")

    def pin(self, locator: Locator, state: State, pin_id: str) -> Pin:
        """Create a durable native ref for ``state``. Requires ``PIN``."""
        raise NotImplementedError(f"{self.kind} backend does not pin")

    def unpin(self, locator: Locator, pin: Pin) -> None:
        """Release a pin. Requires ``PIN``."""
        raise NotImplementedError(f"{self.kind} backend does not pin")

    def list_pins(self, locator: Locator) -> set[str]:
        """Return pin ids that currently exist natively. Requires ``PIN``."""
        raise NotImplementedError(f"{self.kind} backend does not pin")

    def verify(
        self,
        locator: Locator,
        state: State,
        pin: Pin | None,
        deep: bool,
    ) -> VerifyReport:
        """Check that ``state``/``pin`` still hold."""
        raise NotImplementedError(f"{self.kind} backend does not verify")

    def fork(
        self,
        locator: Locator,
        source: Pin | State,
        name: str,
        *,
        expected: State | None = None,
    ) -> str:
        """Create a writable branch ``name`` off ``source``. Requires ``FORK``.

        ``source`` is a :class:`~tether.manifest.Pin` (fork from the native ref)
        or a recorded ``State`` (fork directly from an addressable state, used
        by ``policy.pin == "record"`` objects that carry no native ref).

        **Reset contract**: if ``name`` already exists it is moved back onto
        ``source`` (whatever was written on it is discarded); a branch already
        at ``source`` is left alone. The conformance suite checks this.

        **Conditional move**: ``expected`` is the head ``name`` must hold for
        the move to happen -- the state the caller reviewed, or
        :data:`ABSENT` when the branch must not exist yet. When it holds
        something else, raise :class:`~tether.errors.RefMovedError` and move
        nothing (compare content states: ``VOLATILE_KEYS`` may differ).
        ``None`` moves unconditionally, as before 0.1.0b4; see
        :func:`accepts_expected` for how the engine treats a backend whose
        signature predates the keyword.
        """
        raise NotImplementedError(f"{self.kind} backend does not fork")

    def delete_working_ref(self, locator: Locator, ref: str) -> None:
        """Delete a working ref created by :meth:`fork`. Requires ``FORK``."""
        raise NotImplementedError(f"{self.kind} backend does not fork")

    # -- store lifecycle (CREATE) ---------------------------------------- #
    # A store tether made is a store tether may remove once nothing refers to
    # it. Ownership is asserted by a marker *in the store*, never by a
    # manifest: manifests arrive with clones and are untrusted input.
    def create(self, locator: Locator, *, owner: str) -> State:
        """Make an empty store at `locator` and write the owner marker for
        `owner` (a dataset id); return its initial state. Requires ``CREATE``.

        Raises:
            BackendError: Anything already exists there -- `create` never
                adopts a store someone else made.
        """
        raise CapabilityError(
            f"{self.kind} backend cannot create stores", kind=self.kind
        )

    def owner(self, locator: Locator) -> str | None:
        """The dataset id in the store's owner marker, or `None` when tether
        never wrote one (or the store is gone). Requires ``CREATE``."""
        raise CapabilityError(
            f"{self.kind} backend cannot create stores", kind=self.kind
        )

    def is_ref_empty(
        self, locator: Locator, *, ignoring: Collection[str] = ()
    ) -> bool | None:
        """Whether nothing remains in the store but its base branch at the
        initial state, the owner marker, and the refs in `ignoring` (the ones a
        gc plan is about to delete). A store with a working area (a git
        checkout, a directory) must also hold no uncommitted content: files
        are data whether or not a ref names them. Requires ``CREATE``.

        `None` means the backend cannot tell -- and `None` means *keep*: a
        store is only ever removed on a definite `True`.
        """
        raise CapabilityError(
            f"{self.kind} backend cannot create stores", kind=self.kind
        )

    def delete_store(self, locator: Locator) -> None:
        """Remove the store. Called only after :meth:`is_ref_empty` returned
        `True` for it, and only for a store whose :meth:`owner` is the caller's
        dataset -- and expected to check both again itself, right before the
        irreversible step. Requires ``CREATE``."""
        raise CapabilityError(
            f"{self.kind} backend cannot create stores", kind=self.kind
        )

    def base_branch(self, locator: Locator) -> str:
        """The upstream branch a locator names: what the trunk bookmark stands
        for, `promote` moves, and a trunk working copy writes to. Default
        ``locator["branch"]`` (``main``); git uses the checked-out branch when
        the locator has no ``ref``."""
        return str(locator.get("branch", "main"))

    def configure_cache(self, cache_dir: Path) -> None:
        """Tell the backend where it may keep per-workspace scratch state.

        Called by the engine after construction with ``.tether/cache/``
        (untracked). Backends that remember expensive results between runs --
        the ``file`` backend keeps a stat -> content-hash cache there -- store
        them under this directory; everything else ignores the call.
        """
        return None

    def configure_checkout(self, root: Path) -> None:
        """Tell the backend where the dataset's VCS checkout is.

        Called by the engine after construction. Every file under `root`
        arrived with the clone, so a backend that would *run* something a
        directory there configures -- the ``git`` backend reads a
        repository's config -- refuses paths inside it; everything else
        ignores the call.
        """
        return None

    def working_ref_blockers(self, locator: Locator, ref: str) -> str | None:
        """Why ``ref`` cannot be deleted right now, or ``None`` if it can.

        Consulted by ``gc --prune-bookmarks`` and ``forget-workspace`` before
        planning a ``delete-branch``, so an undeletable branch is planned as
        kept with the reason instead of failing at apply time. Neon: a branch
        with children (pins taken on it) cannot be deleted until they are.
        Default: no blockers.
        """
        return None

    def rename_pin(self, locator: Locator, old: Pin, state: State, new_id: str) -> Pin:
        """Give the pin ``old`` (which names ``state``) the id ``new_id``.

        Used by ``tether upgrade`` when the pin naming scheme changes. Default:
        :meth:`pin` the state under the new id, then :meth:`unpin` the old one
        -- which works wherever pins are tags. Backends whose pins are branches
        with children (Neon) rename in place instead.
        """
        new = self.pin(locator, state, new_id)
        if old.ref != new.ref:
            self.unpin(locator, old)
        return new

    def rename_working_ref(self, locator: Locator, old: str, new: str) -> str:
        """Rename working branch ``old`` to ``new``; return the resulting ref.

        Used by ``tether upgrade`` when the branch naming scheme changes.
        Default: fork ``new`` from ``old``'s head state and delete ``old``.
        Requires ``FORK`` and a backend that can fork from a state.
        """
        head = self.fingerprint(locator, old)
        ref = self.fork(locator, head, new)
        if ref != old:
            self.delete_working_ref(locator, old)
        return ref

    def list_working_refs(self, locator: Locator) -> list[str]:
        """Native branches created by :meth:`fork` (``tether.ws.*``). Requires ``FORK``.

        Default: none. Used by ``gc --prune-bookmarks``.
        """
        return []

    PROMOTE_HINT: str = ""
    """What to tell a user when tether cannot move the base branch (no
    ``PROMOTE`` / ``MERGE``, or the base diverged and there is no merge)."""

    def promote(
        self,
        locator: Locator,
        source: str | Pin | State,
        *,
        expected: State | None = None,
    ) -> State:
        """Fast-forward the locator's base branch to ``source``. Requires ``PROMOTE``.

        ``source`` is a working ref name, a :class:`~tether.manifest.Pin`, or a
        recorded ``State``. Implementations must refuse (``BackendError``) when
        the base head is not an ancestor of ``source`` -- a fast-forward never
        discards anything -- and be a no-op when the base is already there.
        ``expected`` is the base head the caller reviewed: when the base holds
        another state, raise :class:`~tether.errors.RefMovedError` and move
        nothing (a compare-and-swap where the system has one -- git's
        `update-ref` with an old value, Icechunk's `from_snapshot_id`).
        Returns the base branch's new state.
        """
        raise CapabilityError(f"{self.kind} backend cannot promote", kind=self.kind)

    def merge(
        self,
        locator: Locator,
        source: str | Pin | State,
        message: str,
        *,
        expected: State | None = None,
    ) -> State:
        """Three-way merge ``source`` into the base branch.

        ``source`` is a working ref, a ``Pin``, or a ``State``. The engine
        passes the *state* the plan reviewed, so what is merged is what was
        shown -- a ref is a name someone can move between plan and apply.
        Requires ``MERGE``. Raises :class:`~tether.errors.MergeConflict` (and
        leaves the base untouched) when the system reports conflicts, and
        :class:`~tether.errors.RefMovedError` when ``expected`` is given and
        the base head is not that state. Returns the base branch's new state.
        """
        raise CapabilityError(f"{self.kind} backend cannot merge", kind=self.kind)

    def ancestor_of(
        self, locator: Locator, ancestor: State, descendant: str | Pin | State
    ) -> bool | None:
        """Whether ``ancestor`` is in ``descendant``'s history (``None``: unknown).

        Used by ``promote`` when no fork point was recorded. Default: unknown.
        """
        return None

    def open(
        self,
        locator: Locator,
        target: str | Pin | State | None,
        read_only: bool,
    ) -> Handle:
        """Return a native handle for a target.

        ``target`` is one of: ``None`` (the base/working ref), a ``str`` working
        ref, a :class:`~tether.manifest.Pin` (read a pinned state), or a
        recorded ``State`` mapping (read an addressable state without a pin).
        """
        raise NotImplementedError(f"{self.kind} backend does not open")

    def listing(self, locator: Locator, state: State) -> str | None:
        """Optional detailed description of ``state`` to store alongside it.

        The engine writes the text content-addressed under ``.tether/listings/``
        at commit time and hands both sides back to :meth:`diff` later. Used by
        backends whose state is a digest (the ``file`` backend's directory and
        prefix listings) so Observed states can still be diffed.
        """
        return None

    def diff(
        self,
        locator: Locator,
        a: State,
        b: State,
        *,
        listings: Listings = (None, None),
    ) -> ObjectDiff:
        """Describe what changed from state ``a`` to state ``b``. Requires ``DIFF``."""
        raise CapabilityError(f"{self.kind} backend cannot diff", kind=self.kind)

    def history(
        self,
        locator: Locator,
        ref: str | None = None,
        limit: int = 20,
    ) -> list[HistoryEntry]:
        """List native history, newest first, starting at ``ref`` (default: base).

        Requires ``HISTORY``. Metadata only; ``limit`` bounds the walk.
        """
        raise CapabilityError(
            f"{self.kind} backend cannot list history", kind=self.kind
        )


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
BackendFactory = Callable[[dict], ObjectBackend]
_REGISTRY: dict[str, BackendFactory] = {}

# Built-in kinds and the module that registers them. Imported lazily so optional
# third-party dependencies (icechunk, pyiceberg, ...) are only loaded on demand.
_BUILTIN_MODULES: dict[str, str] = {
    "memory": "tether.backends.memory",
    "file": "tether.backends.file",
    "icechunk": "tether.backends.icechunk",
    "neon": "tether.experimental.backends.neon",
    "git": "tether.backends.git",
    "iceberg": "tether.experimental.backends.iceberg",
    "delta": "tether.backends.delta",
    "lance": "tether.backends.lance",
    "ducklake": "tether.experimental.backends.ducklake",
    "dolt": "tether.experimental.backends.dolt",
}


def register_backend(kind: str, factory: BackendFactory) -> None:
    _REGISTRY[kind] = factory


def _factory_for(kind: str) -> BackendFactory:
    """The registered factory for `kind`, importing its built-in module on
    first use.

    Raises:
        ConfigError: Unknown kind, or its optional dependency is missing.
    """
    from importlib import import_module

    from tether.errors import ConfigError

    if kind not in _REGISTRY and kind in _BUILTIN_MODULES:
        try:
            import_module(_BUILTIN_MODULES[kind])
        except ImportError as exc:
            raise ConfigError(
                f"backend {kind!r} needs an optional dependency: {exc}. "
                f"Install the matching extra (e.g. `pip install tether-vcs[{kind}]`)."
            ) from exc
    try:
        return _REGISTRY[kind]
    except KeyError as exc:
        raise ConfigError(
            f"unknown backend kind: {kind!r} "
            f"(known: {', '.join(known_kinds()) or 'none'})"
        ) from exc


def build_backend(kind: str, config: dict | None = None) -> ObjectBackend:
    return _factory_for(kind)(config or {})


_CLASSES: dict[str, type[ObjectBackend]] = {}


def backend_class(kind: str) -> type[ObjectBackend]:
    """The backend class for `kind`, without building an instance -- for the
    class-level contract (`SAFE_CONFIG_KEYS`, `MATURITY`, `capabilities`).

    Resolved once per kind: the class in the factory's module whose `kind`
    matches; a factory that is a closure (tests register those) falls back to
    building one instance and taking its type. Raises the same `ConfigError`
    as :func:`build_backend`.
    """
    import sys

    cls = _CLASSES.get(kind)
    if cls is None:
        factory = _factory_for(kind)
        module = sys.modules.get(factory.__module__)
        # `ObjectBackend` is a Protocol with data members, so `issubclass` is
        # off the table; backends inherit from it explicitly, so the MRO tells.
        found = (
            [
                v
                for v in vars(module).values()
                if isinstance(v, type)
                and ObjectBackend in v.__mro__
                and getattr(v, "kind", None) == kind
            ]
            if module is not None
            else []
        )
        cls = _CLASSES[kind] = found[0] if len(found) == 1 else type(factory({}))
    return cls


def safe_config_keys(kind: str) -> frozenset[str]:
    """`SAFE_CONFIG_KEYS` of the backend class for `kind` (no instance built)."""
    return frozenset(getattr(backend_class(kind), "SAFE_CONFIG_KEYS", frozenset()))


def safe_option_keys(kind: str) -> Mapping[str, frozenset[str]]:
    """`SAFE_OPTION_KEYS` of the backend class for `kind` (no instance built)."""
    return dict(getattr(backend_class(kind), "SAFE_OPTION_KEYS", {}))


def known_kinds() -> list[str]:
    return sorted(set(_REGISTRY) | set(_BUILTIN_MODULES))


def base_at(locator: Locator) -> str | None:
    """The locator's ``at`` field (a detached base state), if any."""
    value = locator.get("at")
    return None if value in (None, "") else str(value)


def iso_utc(value: Any) -> str | None:
    """Render a datetime / epoch value as an ISO-8601 UTC string for reports."""
    from datetime import UTC, datetime

    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat(timespec="seconds")
    if isinstance(value, int | float):
        seconds = float(value)
        if seconds > 1e14:  # microseconds
            seconds /= 1e6
        elif seconds > 1e11:  # milliseconds
            seconds /= 1e3
        return datetime.fromtimestamp(seconds, tz=UTC).isoformat(timespec="seconds")
    return str(value)


def content_state(backend: ObjectBackend, state: State | None) -> State | None:
    """The part of a state that identifies the data: the state minus
    ``backend.VOLATILE_KEYS``.

    Everything that asks "is this the same thing?" -- drift detection, the
    unchanged check in ``commit``, pin ids, listing names, export hashes,
    ``promote``'s fork-point comparison -- goes through this. Everything that
    asks "where is it?" (``open``, ``pin``, ``verify``) uses the full state.
    """
    if state is None:
        return None
    volatile = backend.VOLATILE_KEYS
    if not volatile:
        return state
    return {k: v for k, v in state.items() if k not in volatile}


ABSENT: State = {}
"""`expected` for a conditional :meth:`ObjectBackend.fork`: the branch must
not exist yet. An empty state, which no fingerprint ever is; test for it with
``not expected`` and never mutate it."""


def check_expected(
    backend: ObjectBackend,
    locator: Locator,
    expected: State | None,
    *,
    ref: str | None = None,
    what: str = "fork",
) -> None:
    """The conditional half of a ref move, for a system with no native
    compare-and-swap: read the head and refuse when it is not ``expected``.

    ``ref`` is the working branch a `fork` moves (absent when not listed by
    `list_working_refs`; :data:`ABSENT` expects that); ``None`` means the
    locator's base branch, which `promote` and `merge` move. A check before
    the act, so a write in between still slips through -- narrower than
    git's `update-ref` or Icechunk's `from_snapshot_id`, which backends with
    one should use instead -- but the plan's reviewed head is compared, not
    ignored. `None` checks nothing.

    Raises:
        RefMovedError: The head is not ``expected``.
    """
    from tether.errors import RefMovedError

    if expected is None:
        return
    if ref is not None:
        present = ref in backend.list_working_refs(locator)
        if not expected:
            if present:
                raise RefMovedError(
                    f"{what}: {ref} exists, expected absent", kind=backend.kind
                )
            return
        if not present:
            raise RefMovedError(
                f"{what}: {ref} is gone, expected {_short(expected)}",
                kind=backend.kind,
            )
        head = backend.fingerprint(locator, ref)
    else:
        base_locator = {k: v for k, v in locator.items() if k != "at"}
        head = backend.fingerprint(base_locator, None)
    if content_state(backend, head) != content_state(backend, expected):
        raise RefMovedError(
            f"{what}: {ref or backend.base_branch(locator)} is at {_short(head)}, "
            f"expected {_short(expected)}",
            kind=backend.kind,
        )


def _short(state: State) -> str:
    return ", ".join(f"{k}={str(v)[:12]}" for k, v in sorted(state.items()))


def accepts_expected(method: Callable[..., Any]) -> bool:
    """Whether a backend's `fork`, `promote` or `merge` takes the `expected`
    keyword (see :meth:`ObjectBackend.fork`).

    Backends written before 0.1.0b4 do not declare it; the engine calls those
    without it and their moves are unconditional, as they always were, rather
    than failing every fork with a `TypeError`.
    """
    import inspect

    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):  # a builtin or a mock: let the call decide
        return True
    return "expected" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def fork_ref(
    backend: ObjectBackend,
    locator: Locator,
    source: Pin | State,
    name: str,
    expected: State | None,
) -> str:
    """:meth:`ObjectBackend.fork` with ``expected`` where the backend takes
    it; a backend predating the keyword is called the old way."""
    if expected is None or not accepts_expected(backend.fork):
        return backend.fork(locator, source, name)
    return backend.fork(locator, source, name, expected=expected)


def promote_ref(
    backend: ObjectBackend,
    locator: Locator,
    source: str | Pin | State,
    expected: State | None,
) -> State:
    """:meth:`ObjectBackend.promote`, as :func:`fork_ref` is to `fork`."""
    if expected is None or not accepts_expected(backend.promote):
        return backend.promote(locator, source)
    return backend.promote(locator, source, expected=expected)


def merge_ref(
    backend: ObjectBackend,
    locator: Locator,
    source: str | Pin | State,
    message: str,
    expected: State | None,
) -> State:
    """:meth:`ObjectBackend.merge`, as :func:`fork_ref` is to `fork`."""
    if expected is None or not accepts_expected(backend.merge):
        return backend.merge(locator, source, message)
    return backend.merge(locator, source, message, expected=expected)


_B = TypeVar("_B")

_GUARDED_METHODS = (
    "create",
    "owner",
    "is_ref_empty",
    "delete_store",
    "fingerprint",
    "pin",
    "unpin",
    "list_pins",
    "verify",
    "fork",
    "delete_working_ref",
    "list_working_refs",
    "promote",
    "merge",
    "ancestor_of",
    "open",
    "diff",
    "history",
    "listing",
    "check_quiescence",
    "branch_head",
    "resolve",
)


def wrap_library_errors(cls: type[_B]) -> type[_B]:
    """Class decorator: a backend's protocol methods re-raise its library's
    exceptions as :class:`~tether.errors.BackendError`.

    The class provides ``_library_errors() -> tuple[type[BaseException], ...]``
    (a staticmethod, importing lazily so an optional dependency is only needed
    when the backend runs). `TetherError`s pass through untouched. The engine
    catches `TetherError` at its refusal sites, so a network blip or a missing
    ref becomes a refusal or a clear message instead of a traceback from
    inside a third-party client.
    """
    from tether.errors import BackendError, TetherError

    # `_library_errors` is a convention of the decorated class, not a protocol
    # member; look it up dynamically so the decorator stays generic.
    errors: Callable[[], tuple[type[BaseException], ...]] = getattr(  # noqa: B009
        cls, "_library_errors"
    )
    kind = str(getattr(cls, "kind", "backend"))

    def guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def inner(self: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                return fn(self, *args, **kwargs)
            except TetherError:
                raise
            except errors() as exc:
                raise BackendError(
                    f"{kind}: {type(exc).__name__}: {exc}", kind=kind
                ) from exc

        return inner

    for name in _GUARDED_METHODS:
        fn = cls.__dict__.get(name)
        if callable(fn):
            setattr(cls, name, guarded(fn))
    return cls


def unsafe_option_keys(
    options: Mapping[str, Any], allowed: Collection[str] = ()
) -> list[str]:
    """Keys of a committed option table that are not on the backend's
    allowlist for it -- and so must come from `secrets.toml` instead."""
    return sorted(str(k) for k in options if k not in allowed)


def check_committed_config(
    kind: str,
    safe_keys: frozenset[str],
    config: Mapping[str, Any],
    safe_option_keys: Mapping[str, Collection[str]] | None = None,
) -> None:
    """Refuse committed `[backends.<kind>]` keys a clone must not set.

    Raises:
        ConfigError: A key outside `safe_keys`, or a key inside an option
            table that the backend's `SAFE_OPTION_KEYS` does not name (a
            table with no allowlist at all refuses every key); the message
            names the key and where it belongs.
    """
    from tether.errors import ConfigError

    tables = safe_option_keys or {}
    bad = sorted(k for k in config if k not in safe_keys)
    nested = [
        f"{k}.{sub}"
        for k, v in config.items()
        if k in safe_keys and isinstance(v, Mapping)
        for sub in unsafe_option_keys(v, tables.get(k, ()))
    ]
    if bad or nested:
        names = ", ".join(bad + nested)
        raise ConfigError(
            f"tether.toml [backends.{kind}] sets {names}; a committed file arrives "
            "with every clone and must not choose executables, endpoints, "
            "credentials, or SQL. Put it in .tether/secrets.toml (untracked) under "
            f"[backends.{kind}], or in the environment"
        )


def local_path(uri: str) -> str | None:
    """The filesystem path a locator string names, in one spelling per
    directory, or `None` for a remote URL.

    `file:///p` and `file://localhost/p` are `/p`. An absolute path is
    resolved -- symlinks followed, `.`, `..`, doubled and trailing slashes
    gone -- so `/p`, `/p/` and `/link/p` through a symlinked parent (macOS's
    `/tmp` is `/private/tmp`) are one path; a relative one (only a
    hand-written manifest has it) is normalized lexically. A bare path is
    never run through a URL parser: `#` and `?` are path characters there
    (a parser cut `/data/run#1` to `/data/run`). Anything with another scheme
    -- `s3://`, `ducklake:` -- is not local.
    """
    from urllib.parse import urlparse

    if uri.startswith("file:"):
        rest = uri[len("file:") :]
        if rest.startswith("//"):
            host, sep, path = rest[2:].partition("/")
            if host not in ("", "localhost") or not sep:
                return None
            rest = f"/{path}"
        if not rest:
            return None
        uri = rest
    elif urlparse(uri).scheme and not Path(uri).is_absolute():
        return None
    if not os.path.isabs(uri):
        return os.path.normpath(uri) if uri else uri
    return os.path.realpath(uri)


def canonical_uri(uri: str) -> str:
    """One spelling per store for identities and ref namespaces: a local path
    as :func:`local_path` resolves it (every spelling of one directory must
    be one object to pin ids, listings and `gc`), any other URL as written."""
    path = local_path(uri)
    return uri if path is None else path


def absolutize_locator(backend: ObjectBackend, locator: Locator, base: Path) -> Locator:
    """Resolve relative local paths in `locator` against `base`, and expand a
    leading `~` to the home directory.

    Only the keys the backend lists in :attr:`ObjectBackend.LOCAL_PATH_KEYS`
    are touched, and only when the value is a bare relative path or starts
    with `~`: URLs (`s3://`, `file://`, `ducklake:`...) and absolute paths
    pass through. A locator is committed and read from any directory and any
    clone, so the path it names must not depend on where `add` happened to
    run -- nor on the reader's own `~`, which the libraries behind some
    backends expand and others take for a directory named `~`.
    """
    from urllib.parse import urlparse

    out = dict(locator)
    for key in backend.LOCAL_PATH_KEYS:
        value = out.get(key)
        if not isinstance(value, str) or not value:
            continue
        prefixes = (p for p in backend.LOCAL_PATH_PREFIXES if value.startswith(p))
        prefix = max(prefixes, key=len, default="")
        path = value[len(prefix) :]
        if not path or urlparse(path).scheme:
            continue
        expanded = os.path.expanduser(path)
        if Path(expanded).is_absolute():
            if expanded != path:
                out[key] = prefix + expanded
            continue
        out[key] = prefix + str((base / expanded).resolve())
    return out


def effective_capabilities(
    backend: ObjectBackend,
    locator: Locator,
    policy: object,
) -> Capability:
    """Capabilities for a specific object.

    Some backends (notably ``file``) span tiers depending on the locator and
    policy: a local file is Observed while an S3 object in a versioned bucket is
    Addressable. Backends may implement ``effective_capabilities(locator, policy)``
    to refine their class-level :attr:`capabilities`; otherwise the class value
    is used.

    ``policy.pin == "record"`` removes ``PIN`` for any backend: the state is
    recorded without a native ref (cheaper, no retention hold) and forks come
    straight from the recorded state while the system still has it.
    """
    fn = getattr(backend, "effective_capabilities", None)
    caps = fn(locator, policy) if callable(fn) else backend.capabilities
    if getattr(policy, "pin", "native") == "record":
        caps &= ~Capability.PIN
    return caps
