"""Typed native handles returned by :meth:`Repo.open`.

A handle is a thin, typed carrier for whatever a caller needs to talk to the
underlying system directly. tether never sits in the data path: it hands back
the native address (a path/URI, an Icechunk repository + ref, a Neon connection
URL, a git worktree, an Iceberg table + ref) and steps out of the way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


@dataclass
class Handle:
    """Base class for all native handles."""

    key: str
    read_only: bool


@dataclass
class FileHandle(Handle):
    """A plain file or object-store artifact."""

    uri: str
    version_id: str | None = None


@dataclass
class IcechunkHandle(Handle):
    """An Icechunk repository positioned at a branch (write) or tag (read).

    ``session`` is a ready-to-use Icechunk session (writable for a branch,
    read-only for a tag/snapshot); ``repository`` is provided for callers that
    want to open their own sessions.
    """

    repository: Any  # icechunk.Repository (avoids a hard import)
    session: Any = None  # icechunk.Session
    branch: str | None = None
    tag: str | None = None
    snapshot_id: str | None = None


@dataclass
class NeonHandle(Handle):
    """A Neon Postgres connection URL for a branch or a pinned point in time."""

    url: str
    branch: str


@dataclass
class GitHandle(Handle):
    """A git/jj repository positioned at a sha (and optional worktree path)."""

    path: str
    sha: str
    worktree: str | None = None


@dataclass
class IcebergHandle(Handle):
    """A pyiceberg table positioned at a branch (write) or tag (read)."""

    table: Any  # pyiceberg Table
    ref: str | None = None
    snapshot_id: int | None = None


@dataclass
class MemoryHandle(Handle):
    """In-memory reference handle used by the conformance suite and tests."""

    store: Any
    system: str
    ref: str

    def read(self) -> dict[str, Any]:
        return self.store.read(self.system, self.ref)

    def write(self, payload: dict[str, Any]) -> str:
        if self.read_only:
            raise PermissionError(f"handle for {self.key!r} is read-only")
        return self.store.write(self.system, self.ref, payload)
