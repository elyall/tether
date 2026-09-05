"""DuckLake backend (Addressable, retention-bound).

A DuckLake catalog records every commit as a catalog-wide ``snapshot_id`` that
DuckDB can time-travel to (``AT (VERSION => n)`` or
``ATTACH ... (SNAPSHOT_VERSION n)``). DuckLake has no tags or branches, so
tether records snapshots rather than pinning them; ``ducklake_expire_snapshots``
bounds how long a recorded snapshot stays readable. The snapshot's commit time is
recorded too, so a dropped-and-recreated catalog is reported as drift.

Access goes through the ``duckdb`` Python package and the ``ducklake``
extension. Two DuckDB facts drive the implementation:

* A DuckDB-file metadata catalog can be attached only once per process, so
  every operation attaches, queries, and detaches, serialized per catalog, and a
  handle owns its own attachment (call ``handle.close()`` when done).
* ``TIMESTAMP WITH TIME ZONE`` results need ``pytz`` in Python, so snapshot
  times are read as ``epoch_us`` integers.

Locator: ``metadata`` (the ``ducklake:...`` connection string; credentials
belong in ``[backends.ducklake] init_sql`` -- e.g. ``CREATE SECRET`` -- or the
environment, not in manifests), optional ``data_path``, optional ``table`` to
scope the handle.
"""

from __future__ import annotations

import contextlib
import hashlib
import threading
from collections.abc import Iterator
from typing import Any

from tether.backends.base import (
    Capability,
    ObjectBackend,
    VerifyReport,
    VerifyStatus,
    register_backend,
)
from tether.errors import BackendError, CapabilityError
from tether.handles import DuckLakeHandle, Handle
from tether.manifest import Locator, Pin, State

_SNAPSHOTS_SQL = (
    "SELECT snapshot_id, epoch_us(snapshot_time) FROM ducklake_snapshots('{alias}') "
    "ORDER BY snapshot_id"
)


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _alias_for(metadata: str) -> str:
    return "tether_" + hashlib.blake2b(metadata.encode(), digest_size=4).hexdigest()


def attach_sql(
    metadata: str,
    alias: str,
    *,
    data_path: str | None = None,
    snapshot_id: int | None = None,
    read_only: bool = True,
) -> str:
    """Build the ``ATTACH`` statement tether uses (also exposed on handles)."""
    options: list[str] = []
    if data_path:
        options.append(f"DATA_PATH {_quote(data_path)}")
    if snapshot_id is not None:
        options.append(f"SNAPSHOT_VERSION {int(snapshot_id)}")
    if read_only:
        options.append("READ_ONLY")
    suffix = f" ({', '.join(options)})" if options else ""
    return f"ATTACH {_quote(metadata)} AS {alias}{suffix}"


class DuckLakeBackend(ObjectBackend):
    kind = "ducklake"
    capabilities = (
        Capability.FINGERPRINT
        | Capability.ADDRESSABLE
        | Capability.CHEAP_FINGERPRINT
        | Capability.RETENTION_BOUND
    )

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # -- helpers --------------------------------------------------------- #
    def _metadata(self, locator: Locator) -> str:
        meta = locator.get("metadata") or locator.get("uri")
        if not meta:
            raise BackendError("ducklake locator needs 'metadata'", kind="ducklake")
        meta = str(meta)
        return meta if meta.startswith("ducklake:") else f"ducklake:{meta}"

    def _data_path(self, locator: Locator) -> str | None:
        dp = locator.get("data_path")
        return str(dp) if dp else None

    def _lock(self, metadata: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(metadata, threading.Lock())

    def _connect(self) -> Any:
        """A fresh in-memory DuckDB with the ducklake extension loaded."""
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - optional dep
            raise BackendError(
                "the ducklake extra is required (`pip install tether[ducklake]`)",
                kind="ducklake",
            ) from exc
        con = duckdb.connect()
        try:
            con.execute("LOAD ducklake")
        except duckdb.Error:
            con.execute("INSTALL ducklake; LOAD ducklake")
        for stmt in self._config.get("init_sql") or []:
            con.execute(str(stmt))
        return con

    @contextlib.contextmanager
    def _attached(
        self, locator: Locator, snapshot_id: int | None = None
    ) -> Iterator[tuple[Any, str]]:
        """Attach the catalog for one operation; yields ``(connection, alias)``."""
        import duckdb

        metadata = self._metadata(locator)
        alias = _alias_for(metadata)
        sql = attach_sql(
            metadata, alias, data_path=self._data_path(locator), snapshot_id=snapshot_id
        )
        with self._lock(metadata):
            con = self._connect()
            try:
                try:
                    con.execute(sql)
                except duckdb.Error as exc:
                    where = (
                        f" at snapshot {snapshot_id}" if snapshot_id is not None else ""
                    )
                    hint = (
                        " (a DuckDB-file catalog can be attached once per process; "
                        "close open handles/connections or use a Postgres/SQLite "
                        "metadata catalog)"
                        if "already attached" in str(exc)
                        else ""
                    )
                    raise BackendError(
                        f"cannot attach ducklake catalog {metadata}{where}: "
                        f"{exc}{hint}",
                        kind="ducklake",
                    ) from exc
                yield con, alias
            finally:
                with contextlib.suppress(duckdb.Error):
                    con.execute(f"DETACH {alias}")
                con.close()

    def _snapshots(self, con: Any, alias: str) -> dict[int, int]:
        rows = con.execute(_SNAPSHOTS_SQL.format(alias=alias)).fetchall()
        return {int(sid): int(us) for sid, us in rows}

    # -- protocol -------------------------------------------------------- #
    def identity(self, locator: Locator) -> Locator:
        ident: Locator = {"metadata": self._metadata(locator)}
        if self._data_path(locator):
            ident["data_path"] = self._data_path(locator)
        return ident

    def fingerprint(self, locator: Locator, working_ref: str | None) -> State:
        with self._attached(locator) as (con, alias):
            snapshots = self._snapshots(con, alias)
        if not snapshots:  # pragma: no cover - a catalog always has snapshot 0
            raise BackendError("ducklake catalog has no snapshots", kind="ducklake")
        sid = max(snapshots)
        return {"snapshot_id": sid, "snapshot_time_us": snapshots[sid]}

    def pin(self, locator: Locator, state: State, pin_id: str) -> Pin:
        raise CapabilityError("ducklake has no tags; snapshots are recorded only")

    def unpin(self, locator: Locator, pin: Pin) -> None:
        raise CapabilityError("ducklake has no tags; snapshots are recorded only")

    def list_pins(self, locator: Locator) -> set[str]:
        return set()

    def verify(
        self,
        locator: Locator,
        state: State,
        pin: Pin | None,
        deep: bool,
    ) -> VerifyReport:
        sid = int(state["snapshot_id"])
        try:
            with self._attached(locator) as (con, alias):
                snapshots = self._snapshots(con, alias)
        except BackendError as exc:
            return VerifyReport(VerifyStatus.MISSING, str(exc))
        if sid not in snapshots:
            return VerifyReport(
                VerifyStatus.MISSING, f"snapshot {sid} is not in the catalog (expired?)"
            )
        recorded = state.get("snapshot_time_us")
        if recorded is not None and int(recorded) != snapshots[sid]:
            return VerifyReport(
                VerifyStatus.DRIFTED,
                f"snapshot {sid} has a different commit time (catalog recreated?)",
            )
        if not deep:
            return VerifyReport(VerifyStatus.OK)
        try:
            with self._attached(locator, snapshot_id=sid) as (con, alias):
                con.execute("SELECT 1").fetchall()
        except BackendError as exc:
            return VerifyReport(VerifyStatus.MISSING, str(exc))
        return VerifyReport(VerifyStatus.OK)

    def fork(self, locator: Locator, pin: Pin, name: str) -> str:
        raise CapabilityError("ducklake cannot fork; copy the catalog instead")

    def delete_working_ref(self, locator: Locator, ref: str) -> None:
        return None

    def open(
        self,
        locator: Locator,
        target: str | Pin | State | None,
        read_only: bool,
    ) -> Handle:
        import duckdb

        metadata = self._metadata(locator)
        alias = _alias_for(metadata)
        sid = int(target["snapshot_id"]) if isinstance(target, dict) else None
        sql = attach_sql(
            metadata, alias, data_path=self._data_path(locator), snapshot_id=sid
        )
        con = self._connect()
        try:
            con.execute(sql)
        except duckdb.Error as exc:
            con.close()
            raise BackendError(
                f"cannot attach ducklake catalog {metadata}: {exc}", kind="ducklake"
            ) from exc
        if sid is None:
            sid = max(self._snapshots(con, alias))
        table = locator.get("table")
        return DuckLakeHandle(
            key=metadata,
            read_only=True,  # tether never writes DuckLake catalogs
            metadata=metadata,
            alias=alias,
            connection=con,
            snapshot_id=sid,
            attach_sql=sql,
            data_path=self._data_path(locator),
            table=str(table) if table else None,
        )


def _factory(config: dict) -> DuckLakeBackend:
    return DuckLakeBackend(config)


register_backend("ducklake", _factory)
