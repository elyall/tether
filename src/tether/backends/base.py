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
from dataclasses import dataclass
from enum import Enum, Flag, auto
from typing import Protocol, runtime_checkable

from tether.handles import Handle
from tether.manifest import Locator, Pin, State


class Capability(Flag):
    """What a backend can do. The first four are cumulative tiers."""

    NONE = 0
    # Cumulative tiers -----------------------------------------------------
    FINGERPRINT = auto()  # can read current state (drift detection)
    ADDRESSABLE = auto()  # recorded state is self-addressing (read later)
    PIN = auto()  # can create/delete a durable, GC-proof ref
    FORK = auto()  # can create a writable branch off a pin
    # Orthogonal flags -----------------------------------------------------
    CHEAP_FINGERPRINT = auto()  # metadata-only; no heavy connection
    RETENTION_BOUND = auto()  # pin validity bounded by a retention window
    NEEDS_QUIESCENCE = auto()  # commit should check for active writers
    ATOMIC_REF = auto()  # create-if-absent semantics for pins


class Tier(Enum):
    OBSERVED = "observed"
    ADDRESSABLE = "addressable"
    PINNABLE = "pinnable"
    FORKABLE = "forkable"


def tier_of(caps: Capability) -> Tier:
    if Capability.FORK in caps:
        return Tier.FORKABLE
    if Capability.PIN in caps:
        return Tier.PINNABLE
    if Capability.ADDRESSABLE in caps:
        return Tier.ADDRESSABLE
    return Tier.OBSERVED


class VerifyStatus(Enum):
    OK = "ok"
    DRIFTED = "drifted"
    MISSING = "missing"
    UNKNOWN = "unknown"  # cannot determine cheaply; use --deep


@dataclass
class VerifyReport:
    status: VerifyStatus
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status is VerifyStatus.OK


@runtime_checkable
class ObjectBackend(Protocol):
    """The operations tether needs from one class of system."""

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
                f"Install the matching extra (e.g. `pip install tether[{kind}]`)."
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
