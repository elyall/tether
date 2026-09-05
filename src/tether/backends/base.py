"""Backend protocol, capability tiers, and a small registry.

A *backend* maps tether operations onto one class of system (files, Icechunk,
Neon, ...). Backends declare their capabilities; the engine (``repo.py``)
enforces them per command and degrades explicitly, and the conformance suite
(``testing.py``) runs the tier-appropriate checks against any backend.

One backend instance serves every object of its kind; per-object addressing is
carried in the ``locator`` argument to each method, so backends are cheap,
stateless coordinators over user-supplied resources.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, Flag, auto
from typing import Any, Protocol, runtime_checkable

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


@runtime_checkable
class ObjectBackend(Protocol):
    """The operations tether needs from one class of system.

    ``listing`` and ``diff`` have default implementations (no listing; diff is a
    capability error) so backends without ``DIFF`` need not define them.
    """

    kind: str
    capabilities: Capability

    def identity(self, locator: Locator) -> Locator:
        """Locator subset that participates in the content-addressed pin id.

        Defaults to the full locator; override to exclude non-identity fields
        (region, credential references, source branch, ...).
        """

    def fingerprint(self, locator: Locator, working_ref: str | None) -> State:
        """Read the current state at ``working_ref`` (or the locator's base)."""

    def pin(self, locator: Locator, state: State, pin_id: str) -> Pin:
        """Create a durable native ref for ``state``. Requires ``PIN``."""

    def unpin(self, locator: Locator, pin: Pin) -> None:
        """Release a pin. Requires ``PIN``."""

    def list_pins(self, locator: Locator) -> set[str]:
        """Return pin ids that currently exist natively. Requires ``PIN``."""

    def verify(
        self,
        locator: Locator,
        state: State,
        pin: Pin | None,
        deep: bool,
    ) -> VerifyReport:
        """Check that ``state``/``pin`` still hold."""

    def fork(self, locator: Locator, pin: Pin, name: str) -> str:
        """Create a writable branch ``name`` off ``pin``. Requires ``FORK``."""

    def delete_working_ref(self, locator: Locator, ref: str) -> None:
        """Delete a working ref created by :meth:`fork`. Requires ``FORK``."""

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
    "neon": "tether.backends.neon",
    "git": "tether.backends.git",
    "iceberg": "tether.backends.iceberg",
    "delta": "tether.backends.delta",
    "lance": "tether.backends.lance",
    "lakefs": "tether.backends.lakefs",
    "ducklake": "tether.backends.ducklake",
    "dolt": "tether.backends.dolt",
}


def register_backend(kind: str, factory: BackendFactory) -> None:
    _REGISTRY[kind] = factory


def build_backend(kind: str, config: dict | None = None) -> ObjectBackend:
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
        factory = _REGISTRY[kind]
    except KeyError as exc:
        raise ConfigError(
            f"unknown backend kind: {kind!r} "
            f"(known: {', '.join(known_kinds()) or 'none'})"
        ) from exc
    return factory(config or {})


def known_kinds() -> list[str]:
    return sorted(set(_REGISTRY) | set(_BUILTIN_MODULES))


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
    """
    fn = getattr(backend, "effective_capabilities", None)
    if callable(fn):
        return fn(locator, policy)
    return backend.capabilities
