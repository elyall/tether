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
    Listings,
    ObjectBackend,
    ObjectDiff,
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
_CHANGES_SQL = (
    "SELECT snapshot_id, changes FROM ducklake_snapshots('{alias}') "
    "WHERE snapshot_id > {lo} AND snapshot_id <= {hi} ORDER BY snapshot_id"
)
# Table ids -> (schema, name), including dropped tables (history is kept).
_TABLES_SQL = (
    "SELECT t.table_id, s.schema_name, t.table_name "
    "FROM __ducklake_metadata_{alias}.ducklake_table t "
    "JOIN __ducklake_metadata_{alias}.ducklake_schema s USING (schema_id)"
)
_ROW_CHANGES_SQL = (
    "SELECT change_type, count(*) FROM ducklake_table_changes("
    "'{alias}', {schema}, {table}, {lo}, {hi}) GROUP BY 1"
)
_MAX_ROW_COUNTED_TABLES = 25


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
        | Capability.DIFF
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
                "the ducklake extra is required (`pip install tether-vcs[ducklake]`)",
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

    def diff(
        self,
        locator: Locator,
        a: State,
        b: State,
        *,
        listings: Listings = (None, None),
    ) -> ObjectDiff:
        """Per-table changes across the snapshots in ``(a, b]``.

        The catalog's ``changes`` map names what each snapshot did (created,
        altered, dropped, inserted into, deleted from); row counts come from
        ``ducklake_table_changes`` for the changed tables that still exist.
        """
        sa, sb = int(a["snapshot_id"]), int(b["snapshot_id"])
        out = ObjectDiff(unit="tables")
        if a.get("snapshot_time_us") != b.get("snapshot_time_us") and sa == sb:
            out.note = "catalog was recreated between the two states"
        if sa == sb:
            return out
        lo, hi = min(sa, sb), max(sa, sb)
        if sb < sa:
            out.note = "b is older than a"
        with self._attached(locator) as (con, alias):
            names: dict[str, tuple[str, str]] = {}
            with contextlib.suppress(Exception):  # metadata layout may differ
                for tid, schema, name in con.execute(
                    _TABLES_SQL.format(alias=alias)
                ).fetchall():
                    names[str(tid)] = (str(schema), str(name))
            per_table: dict[str, set[str]] = {}
            other: dict[str, set[str]] = {}
            rows = con.execute(
                _CHANGES_SQL.format(alias=alias, lo=lo, hi=hi)
            ).fetchall()
            for _sid, changes in rows:
                for kind, values in dict(changes or {}).items():
                    if not str(kind).startswith(("tables_", "inlined_")):
                        other.setdefault(str(kind), set()).update(map(str, values))
                        continue
                    for value in values:
                        label = _table_label(str(value), names)
                        per_table.setdefault(label, set()).add(str(kind))
            counted = 0
            for label in sorted(per_table):
                kinds = per_table[label]
                if "tables_dropped" in kinds:
                    out.add(label, "removed", "dropped")
                    continue
                change = "added" if "tables_created" in kinds else "modified"
                detail = self._row_counts(con, alias, label, names, lo, hi, kinds)
                if detail is not None:
                    counted += 1
                if counted > _MAX_ROW_COUNTED_TABLES:
                    detail = None
                out.add(label, change, detail or _describe(kinds))
            for kind in sorted(other):
                out.add(kind, "modified", ", ".join(sorted(other[kind])))
        return out

    def _row_counts(
        self,
        con: Any,
        alias: str,
        label: str,
        names: dict[str, tuple[str, str]],
        lo: int,
        hi: int,
        kinds: set[str],
    ) -> str | None:
        if not kinds & {
            "tables_inserted_into",
            "tables_deleted_from",
            "inlined_insert",
            "inlined_delete",
        }:
            return None
        if "." not in label:
            return None
        schema, table = label.split(".", 1)
        try:
            rows = con.execute(
                _ROW_CHANGES_SQL.format(
                    alias=alias,
                    schema=_quote(schema),
                    table=_quote(table),
                    lo=lo + 1,
                    hi=hi,
                )
            ).fetchall()
        except Exception:  # table dropped/recreated mid-range; fall back
            return None
        counts = {str(k): int(v) for k, v in rows}
        parts = []
        if counts.get("insert"):
            parts.append(f"+{counts['insert']} rows")
        if counts.get("delete"):
            parts.append(f"-{counts['delete']} rows")
        for key, value in counts.items():
            if key not in ("insert", "delete"):
                parts.append(f"{value} {key}")
        extra = _describe(
            kinds
            - {
                "tables_inserted_into",
                "tables_deleted_from",
                "inlined_insert",
                "inlined_delete",
            }
        )
        if extra:
            parts.append(extra)
        return ", ".join(parts) or None


def _table_label(value: str, names: dict[str, tuple[str, str]]) -> str:
    if value in names:
        schema, name = names[value]
        return f"{schema}.{name}"
    return value  # already a qualified name (tables_created) or unknown id


def _describe(kinds: set[str]) -> str:
    words = {
        "tables_created": "created",
        "tables_altered": "schema altered",
        "tables_inserted_into": "rows inserted",
        "tables_deleted_from": "rows deleted",
        "inlined_insert": "rows inserted",
        "inlined_delete": "rows deleted",
        "tables_dropped": "dropped",
    }
    return ", ".join(sorted({words.get(k, k) for k in kinds}))


def _factory(config: dict) -> DuckLakeBackend:
    return DuckLakeBackend(config)


register_backend("ducklake", _factory)
