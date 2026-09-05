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


class MultiObjectError(TetherError):
    """Aggregates per-object failures from a fan-out operation."""

    def __init__(self, message: str, errors: dict[str, Exception]) -> None:
        self.errors = errors
        detail = "; ".join(f"{key}: {exc}" for key, exc in errors.items())
        super().__init__(f"{message} ({detail})")
