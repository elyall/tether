"""DuckLake backend tests against real DuckDB + the ducklake extension.

The extension is downloaded on first ``INSTALL``; when that is impossible (no
network) the module is skipped rather than failed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tether.backends.base import Capability, ObjectBackend, VerifyStatus
from tether.errors import BackendError, CapabilityError
from tether.handles import DuckLakeHandle
from tether.manifest import Locator, Pin
from tether.testing import run_conformance

duckdb = pytest.importorskip("duckdb")
from tether.backends.ducklake import DuckLakeBackend, attach_sql  # noqa: E402

try:
    _probe = duckdb.connect()
    try:
        _probe.execute("LOAD ducklake")
    except duckdb.Error:
        _probe.execute("INSTALL ducklake; LOAD ducklake")
    _probe.close()
except duckdb.Error as _exc:  # pragma: no cover - env dependent
    pytest.skip(f"ducklake extension unavailable: {_exc}", allow_module_level=True)


def _lake(tmp: Path, name: str) -> Locator:
    return {
        "metadata": f"ducklake:{tmp / f'{name}.ducklake'}",
        "data_path": f"{tmp / f'{name}_files'}/",
        "table": "t",
    }


def _sql(locator: Locator, *statements: str) -> None:
    """Run statements against the lake in a short-lived writer connection."""
    con = duckdb.connect()
    con.execute("LOAD ducklake")
    con.execute(
        attach_sql(
            locator["metadata"], "w", data_path=locator["data_path"], read_only=False
        )
    )
    try:
        for stmt in statements:
            con.execute(stmt)  # no fetch: TIMESTAMPTZ results would need pytz
    finally:
        con.execute("DETACH w")
        con.close()


class DuckLakeHarness:
    capabilities = DuckLakeBackend.capabilities

    def __init__(self, tmp: Path) -> None:
        self.backend: ObjectBackend = DuckLakeBackend()
        self.tmp = tmp
        self._n = 0

    def new_object(self) -> Locator:
        self._n += 1
        loc = _lake(self.tmp, f"lake{self._n}")
        _sql(loc, "CREATE TABLE w.t (a INTEGER)", "INSERT INTO w.t VALUES (0)")
        return loc

    def mutate(self, locator: Locator, working_ref: str | None) -> None:
        self._n += 1
        _sql(locator, f"INSERT INTO w.t VALUES ({self._n})")


def test_ducklake_conformance(tmp_path: Path) -> None:
    assert Capability.PIN not in DuckLakeBackend.capabilities
    run_conformance(DuckLakeHarness(tmp_path))


def test_ducklake_snapshots_are_addressable(tmp_path: Path) -> None:
    b = DuckLakeBackend()
    loc = _lake(tmp_path, "lake")
    _sql(loc, "CREATE TABLE w.t (a INTEGER)", "INSERT INTO w.t VALUES (1)")
    s1 = b.fingerprint(loc, None)
    assert s1["snapshot_id"] == 2 and isinstance(s1["snapshot_time_us"], int)
    _sql(loc, "INSERT INTO w.t VALUES (2)")
    s2 = b.fingerprint(loc, None)
    assert s2["snapshot_id"] == 3 and s2 != s1

    # Cheap verify is definitive (one catalog query); deep also attaches at
    # the snapshot.
    assert b.verify(loc, s1, None, deep=False).ok
    assert b.verify(loc, s1, None, deep=True).ok
    assert b.identity(loc) == {
        "metadata": loc["metadata"],
        "data_path": loc["data_path"],
    }

    # Open at the old snapshot: the attached view is as-of that snapshot.
    h = b.open(loc, s1, read_only=True)
    assert isinstance(h, DuckLakeHandle) and h.read_only
    with h:
        assert h.snapshot_id == 2
        assert h.connection.execute(f"SELECT a FROM {h.table_ref()}").fetchall() == [
            (1,)
        ]
        assert "SNAPSHOT_VERSION 2" in h.attach_sql and "READ_ONLY" in h.attach_sql
        with pytest.raises(duckdb.Error):
            h.connection.execute(f"INSERT INTO {h.table_ref()} VALUES (9)")
    assert h.connection is None  # closed and detached
    # After closing the handle the catalog is attachable again.
    latest = b.open(loc, None, read_only=True)
    assert isinstance(latest, DuckLakeHandle)
    assert latest.snapshot_id == 3
    assert latest.connection.execute(
        "SELECT count(*) FROM " + latest.table_ref()
    ).fetchall() == [(2,)]
    latest.close()

    # Expired snapshots are missing; a recreated catalog drifts.
    _sql(loc, "CALL ducklake_expire_snapshots('w', versions => [2])")
    assert b.verify(loc, s1, None, deep=False).status is VerifyStatus.MISSING
    assert b.verify(loc, s2, None, deep=False).ok
    Path(loc["metadata"].removeprefix("ducklake:")).unlink()
    shutil.rmtree(loc["data_path"])
    _sql(
        loc,
        "CREATE TABLE w.t (a INTEGER)",
        "INSERT INTO w.t VALUES (1)",
        "INSERT INTO w.t VALUES (2)",
    )
    assert b.fingerprint(loc, None)["snapshot_id"] == 3
    assert b.verify(loc, s2, None, deep=False).status is VerifyStatus.DRIFTED

    with pytest.raises(BackendError):
        b.fingerprint(
            {"metadata": f"ducklake:{tmp_path / 'missing' / 'x.ducklake'}"}, None
        )
    with pytest.raises(CapabilityError):
        b.pin(loc, s2, "abc")
    with pytest.raises(CapabilityError):
        b.fork(loc, Pin(id="abc", ref="tether.abc"), "x")


def test_ducklake_diff_per_table(tmp_path: Path) -> None:
    b = DuckLakeBackend()
    loc = _lake(tmp_path, "lake")
    _sql(loc, "CREATE TABLE w.t (a INTEGER)", "INSERT INTO w.t VALUES (1), (2)")
    s1 = b.fingerprint(loc, None)
    _sql(
        loc,
        "INSERT INTO w.t SELECT range FROM range(5000)",
        "DELETE FROM w.t WHERE a = 1",
        "CREATE TABLE w.u (b TEXT)",
        "CREATE TABLE w.gone (c INTEGER)",
        "DROP TABLE w.gone",
    )
    s2 = b.fingerprint(loc, None)
    d = b.diff(loc, s1, s2)
    assert d.unit == "tables"
    by_path = {e.path: e for e in d.entries}
    assert by_path["main.t"].change == "modified"
    # Exact delete counts depend on how DuckLake rewrites inlined rows.
    assert by_path["main.t"].detail.startswith("+5000 rows, -")
    assert by_path["main.u"].change == "added"
    assert by_path["main.gone"].change == "removed"
    assert b.diff(loc, s2, s2).is_empty
    assert "older" in b.diff(loc, s2, s1).note

    # History describes each snapshot's changes; `at` fixes the snapshot.
    log = b.history(loc, None, 3)
    assert [e.id for e in log] == [
        str(s2["snapshot_id"]),
        str(s2["snapshot_id"] - 1),
        str(s2["snapshot_id"] - 2),
    ]
    assert "dropped" in log[0].message and log[0].when is not None
    at = b.fingerprint(dict(loc, at=str(s1["snapshot_id"])), None)
    assert at == s1
    with pytest.raises(BackendError):
        b.fingerprint(dict(loc, at="999"), None)


def test_attach_sql_quotes_and_options() -> None:
    assert (
        attach_sql("ducklake:a'b.db", "x")
        == "ATTACH 'ducklake:a''b.db' AS x (READ_ONLY)"
    )
    assert attach_sql("ducklake:m", "x", data_path="s3://b/p/", snapshot_id=7) == (
        "ATTACH 'ducklake:m' AS x "
        "(DATA_PATH 's3://b/p/', SNAPSHOT_VERSION 7, READ_ONLY)"
    )
    assert attach_sql("ducklake:m", "x", read_only=False) == "ATTACH 'ducklake:m' AS x"
