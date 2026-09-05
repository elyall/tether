"""Typed native handles returned by :meth:`Repo.open`.

A handle is a thin, typed carrier for whatever a caller needs to talk to the
underlying system directly. tether never sits in the data path: it hands back
the native address (a path/URI, an Icechunk repository + ref, a Neon connection
URL, a git worktree, an Iceberg table + ref, a Delta table at a version, a Lance
dataset at a branch/tag, a lakeFS ref URI) and steps out of the way.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

__all__ = [
    "DeltaHandle",
    "DoltHandle",
    "DuckLakeHandle",
    "FileHandle",
    "GitHandle",
    "Handle",
    "IcebergHandle",
    "IcechunkHandle",
    "LakeFSHandle",
    "LanceHandle",
    "MemoryHandle",
    "NeonHandle",
]


@dataclass
class Handle:
    """Base class for all native handles."""

    key: str
    """The object's native identity (URI, repository, database, ...)."""
    read_only: bool
    """Whether writes through this handle are allowed."""


@dataclass
class FileHandle(Handle):
    """A plain file or object-store artifact (always read-only)."""

    uri: str
    """Local path or object URI (`s3://`, `gs://`, `az://`, ...)."""
    version_id: str | None = None
    """Object-store version id when opened at a versioned state."""


@dataclass
class IcechunkHandle(Handle):
    """An Icechunk repository positioned at a branch (write) or tag (read).

    ``session`` is a ready-to-use Icechunk session (writable for a branch,
    read-only for a tag/snapshot); ``repository`` is provided for callers that
    want to open their own sessions.
    """

    repository: Any
    """The `icechunk.Repository`."""
    session: Any = None
    """An `icechunk.Session`: writable for a branch, read-only for a tag/snapshot."""
    branch: str | None = None
    """Branch the session is on (writable handles)."""
    tag: str | None = None
    """Tag the session was opened at (pinned reads)."""
    snapshot_id: str | None = None
    """Snapshot the session is positioned at."""


@dataclass
class NeonHandle(Handle):
    """A Neon Postgres connection URL for a branch or a pinned point in time."""

    url: str
    """`postgresql://...` connection URL (credentials per the Neon API)."""
    branch: str
    """Neon branch name the URL points at."""


@dataclass
class GitHandle(Handle):
    """A git/jj repository positioned at a sha (and optional worktree path)."""

    path: str
    """Absolute path of the repository."""
    sha: str
    """Commit the handle refers to."""
    worktree: str | None = None
    """Path of a checked-out worktree, when one was created."""


@dataclass
class IcebergHandle(Handle):
    """A pyiceberg table positioned at a branch (write) or tag (read)."""

    table: Any
    """The `pyiceberg.table.Table`."""
    ref: str | None = None
    """Branch or tag name."""
    snapshot_id: int | None = None
    """Snapshot id when opened at a recorded state."""


@dataclass
class DeltaHandle(Handle):
    """A Delta Lake table loaded at a specific version (always read-only).

    ``table`` is a ``deltalake.DeltaTable``; write through ``deltalake`` directly
    (tether only records and re-addresses versions).
    """

    uri: str
    """Table location."""
    version: int
    """Delta version the table is loaded at."""
    table: Any
    """The `deltalake.DeltaTable`."""


@dataclass
class LanceHandle(Handle):
    """A Lance dataset checked out at a branch (write) or tag/version (read).

    ``dataset`` is a ``lance.LanceDataset``; append with
    ``lance.write_dataset(table, handle.dataset, mode="append")``.
    """

    uri: str
    """Dataset location."""
    dataset: Any
    """The `lance.LanceDataset`, checked out at the branch or tag."""
    version: int
    """Version number (branch-scoped in Lance)."""
    branch: str | None = None
    """Branch the dataset is checked out on."""
    tag: str | None = None
    """Tag the dataset was opened at (pinned reads)."""


@dataclass
class LakeFSHandle(Handle):
    """A lakeFS repository ref, as a ``lakefs://repo/ref/prefix`` URI.

    ``ref`` is a branch (write) or a tag / commit id (read); the URI is what
    lakefs-spec, the S3 gateway, and ``lakectl`` all accept.
    """

    uri: str
    """`lakefs://repo/ref/prefix/`."""
    repository: str
    """lakeFS repository id."""
    ref: str
    """Branch (writable) or tag / commit id (read-only)."""
    commit_id: str | None = None
    """Commit the ref resolved to, when known."""
    prefix: str = ""
    """Path prefix the object is scoped to."""


@dataclass
class DuckLakeHandle(Handle):
    """A DuckLake catalog attached (read-only) in a DuckDB connection.

    The catalog is attached as ``alias`` in ``connection``, positioned at
    ``snapshot_id`` when opened at a committed state. ``attach_sql`` is the exact
    statement used, for callers that want the same view in their own DuckDB
    process. Call :meth:`close` when done; a DuckDB-file metadata catalog can
    only be attached once per process.
    """

    metadata: str
    """The `ducklake:...` metadata connection string."""
    alias: str
    """Catalog alias inside `connection`."""
    connection: Any
    """A `duckdb.DuckDBPyConnection` with the catalog attached (read-only)."""
    snapshot_id: int
    """Snapshot the attachment is positioned at."""
    attach_sql: str
    """The exact `ATTACH` statement used; reuse it in your own DuckDB."""
    data_path: str | None = None
    """`DATA_PATH` passed to `ATTACH`, when set in the locator."""
    table: str | None = None
    """Table the locator scopes the handle to (see `table_ref`)."""

    def table_ref(self, table: str | None = None) -> str:
        """Qualified table reference (``alias.table``) for SQL."""
        name = table or self.table
        if not name:
            raise ValueError("no table given and the locator has no 'table'")
        return f"{self.alias}.{name}"

    def close(self) -> None:
        con = self.connection
        if con is None:
            return
        with contextlib.suppress(Exception):  # best effort; may be detached already
            con.execute(f"DETACH {self.alias}")
        con.close()
        self.connection = None

    def __enter__(self) -> DuckLakeHandle:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass
class DoltHandle(Handle):
    """A Dolt revision database: ``mysql://user@host:port/db/<ref>``.

    ``ref`` is a branch (writable) or a tag / commit hash (read-only). Connect
    with ``database=handle.database_ref`` or ``USE db/ref``; the password is
    never carried in the handle.
    """

    url: str
    """`mysql://user@host:port/db/ref` (no password)."""
    database: str
    """Dolt database name."""
    ref: str
    """Branch (writable) or tag / commit hash (read-only)."""
    commit: str | None = None
    """Commit the ref resolved to, when known."""

    @property
    def database_ref(self) -> str:
        """`db/ref`, the revision database to `USE` or connect to."""
        return f"{self.database}/{self.ref}"


@dataclass
class MemoryHandle(Handle):
    """In-memory reference handle used by the conformance suite and tests."""

    store: Any
    """The `MemoryStore`."""
    system: str
    """System name inside the store."""
    ref: str
    """Branch, tag, or snapshot id."""

    def read(self) -> dict[str, Any]:
        """Return the payload at `ref`."""
        return self.store.read(self.system, self.ref)

    def write(self, payload: dict[str, Any]) -> str:
        """Write `payload` as a new snapshot on the branch; return its id."""
        if self.read_only:
            raise PermissionError(f"handle for {self.key!r} is read-only")
        return self.store.write(self.system, self.ref, payload)
