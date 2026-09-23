"""Typed errors raised across tether."""

from __future__ import annotations


class TetherError(Exception):
    """Base class for all tether errors."""


class ConfigError(TetherError):
    """The repository configuration or manifest layout is invalid."""


class VcsError(TetherError):
    """A version-control operation failed."""


class BackendError(TetherError):
    """A backend operation failed.

    Carries the object ``key`` and backend ``kind`` when known so aggregated
    errors stay legible.
    """

    def __init__(
        self,
        message: str,
        *,
        key: str | None = None,
        kind: str | None = None,
    ) -> None:
        self.key = key
        self.kind = kind
        prefix = ""
        if key is not None:
            prefix = f"[{key}] "
        elif kind is not None:
            prefix = f"[{kind}] "
        super().__init__(f"{prefix}{message}")


class MergeConflict(BackendError):
    """A native merge stopped on conflicts; nothing was written.

    ``conflicts`` names the conflicting units (paths, tables, arrays) the
    system reported, so the caller can resolve them with the system's tools.
    """

    def __init__(
        self,
        message: str,
        *,
        conflicts: list[str] | None = None,
        key: str | None = None,
        kind: str | None = None,
    ) -> None:
        super().__init__(message, key=key, kind=kind)
        self.conflicts = list(conflicts or [])


class RefMovedError(BackendError):
    """A conditional ref move found the ref elsewhere than `expected`.

    Raised by ``fork``, ``promote`` and ``merge`` when the caller passed the
    head it reviewed and the ref no longer holds it (or exists when it was
    expected absent); nothing was written. The engine reports it as a plan
    gone stale: re-plan, and review what moved.
    """


class CapabilityError(BackendError):
    """An operation was requested that the backend does not support.

    Raised, for example, when ``open`` is asked for a writable handle on an
    object whose backend is not ``FORK``-capable.
    """


class ImmutableObjectModified(BackendError):
    """An object registered as immutable changed on disk / in storage."""


class UnpinnedStateError(BackendError):
    """A pinnable object has working state that was never pinned."""


class PinDriftError(BackendError):
    """A pin no longer resolves to the state recorded in the manifest."""


class StaleWorkingCopyError(TetherError):
    """The workspace was forked from a manifest that no longer matches HEAD.

    The caller should run :meth:`tether.repo.Repo.new` to refork working
    branches before mutating any object.
    """


class StalePlanError(TetherError):
    """A saved plan no longer matches the world it was computed from.

    Raised by ``apply_*`` when object states, manifests, or the revision differ
    from what the plan recorded; re-plan instead of applying stale actions.
    """


class MultiObjectError(TetherError):
    """Aggregates per-object failures from a fan-out operation.

    ``report`` is what the operation did before it gave up, when it did
    anything (`apply_gc`, `apply_promote`): the rest landed, and the caller
    should say so, not just that something failed.
    """

    def __init__(
        self, message: str, errors: dict[str, Exception], *, report: object = None
    ) -> None:
        self.errors = errors
        self.report = report
        detail = "; ".join(f"{key}: {exc}" for key, exc in errors.items())
        super().__init__(f"{message} ({detail})")
