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
class DeltaHandle(Handle):
    """A Delta Lake table loaded at a specific version (always read-only).

    ``table`` is a ``deltalake.DeltaTable``; write through ``deltalake`` directly
    (tether only records and re-addresses versions).
    """

    uri: str
    version: int
    table: Any  # deltalake.DeltaTable


@dataclass
class LanceHandle(Handle):
    """A Lance dataset checked out at a branch (write) or tag/version (read).

    ``dataset`` is a ``lance.LanceDataset``; append with
    ``lance.write_dataset(table, handle.dataset, mode="append")``.
    """

    uri: str
    dataset: Any  # lance.LanceDataset
    version: int
    branch: str | None = None
    tag: str | None = None


@dataclass
class LakeFSHandle(Handle):
    """A lakeFS repository ref, as a ``lakefs://repo/ref/prefix`` URI.

    ``ref`` is a branch (write) or a tag / commit id (read); the URI is what
    lakefs-spec, the S3 gateway, and ``lakectl`` all accept.
    """

    uri: str
    repository: str
    ref: str
    commit_id: str | None = None
    prefix: str = ""


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
    alias: str
    connection: Any  # duckdb.DuckDBPyConnection
    snapshot_id: int
    attach_sql: str
    data_path: str | None = None
    table: str | None = None

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
    database: str
    ref: str
    commit: str | None = None

    @property
    def database_ref(self) -> str:
        return f"{self.database}/{self.ref}"


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
