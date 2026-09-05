from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tether.backends.base import Capability, ObjectBackend, VerifyStatus
from tether.errors import BackendError, CapabilityError
from tether.handles import DeltaHandle
from tether.manifest import Locator, Pin
from tether.testing import run_conformance

pa = pytest.importorskip("pyarrow")
deltalake = pytest.importorskip("deltalake")
from tether.backends.delta import DeltaBackend  # noqa: E402


def _write(uri: str, n: int, mode: str = "append") -> None:
    deltalake.write_deltalake(uri, pa.table({"a": [n]}), mode=mode)


class DeltaHarness:
    capabilities = DeltaBackend.capabilities

    def __init__(self, tmp: Path) -> None:
        self.backend: ObjectBackend = DeltaBackend()
        self.tmp = tmp
        self._n = 0

    def new_object(self) -> Locator:
        self._n += 1
        uri = str(self.tmp / f"t{self._n}")
        _write(uri, 0, mode="overwrite")
        return {"uri": uri}

    def mutate(self, locator: Locator, working_ref: str | None) -> None:
        self._n += 1
        _write(locator["uri"], self._n)


def test_delta_conformance(tmp_path: Path) -> None:
    assert Capability.PIN not in DeltaBackend.capabilities
    run_conformance(DeltaHarness(tmp_path))


def test_delta_versions_are_addressable(tmp_path: Path) -> None:
    b = DeltaBackend()
    uri = str(tmp_path / "t")
    loc = {"uri": uri}
    _write(uri, 1, mode="overwrite")
    s0 = b.fingerprint(loc, None)
    assert s0["version"] == 0 and s0["table_id"]
    _write(uri, 2)
    s1 = b.fingerprint(loc, None)
    assert s1["version"] == 1 and s1["table_id"] == s0["table_id"]

    # Old versions stay readable (until vacuum) and verify deeply.
    assert b.verify(loc, s0, None, deep=False).status is VerifyStatus.UNKNOWN
    assert b.verify(loc, s0, None, deep=True).ok
    h0 = b.open(loc, s0, read_only=True)
    assert isinstance(h0, DeltaHandle)
    assert h0.version == 0 and h0.table.to_pyarrow_table().num_rows == 1
    latest = b.open(loc, None, read_only=True)
    assert isinstance(latest, DeltaHandle)
    assert latest.version == 1 and latest.table.to_pyarrow_table().num_rows == 2

    # A version the table never reached is missing; a recreated table drifts.
    future = dict(s1, version=99)
    assert b.verify(loc, future, None, deep=False).status is VerifyStatus.MISSING
    shutil.rmtree(uri)
    _write(uri, 1, mode="overwrite")
    assert b.verify(loc, s0, None, deep=False).status is VerifyStatus.DRIFTED

    with pytest.raises(BackendError):
        b.fingerprint({"uri": str(tmp_path / "absent")}, None)
    with pytest.raises(CapabilityError):
        b.pin(loc, s1, "abc")


def test_delta_diff_lists_commits(tmp_path: Path) -> None:
    b = DeltaBackend()
    uri = str(tmp_path / "t")
    loc = {"uri": uri}
    _write(uri, 1, mode="overwrite")
    s0 = b.fingerprint(loc, None)
    deltalake.write_deltalake(uri, pa.table({"a": [2, 3]}), mode="append")
    deltalake.DeltaTable(uri).delete("a = 2")
    s2 = b.fingerprint(loc, None)
    assert s2["version"] == 2
    d = b.diff(loc, s0, s2)
    assert d.unit == "commits" and d.modified == 2 and not d.note
    assert [e.path for e in d.entries] == ["v1", "v2"]
    assert d.entries[0].detail == "WRITE: +2 rows, +1 files"
    assert (
        d.entries[1].detail.startswith("DELETE: ") and "-1 rows" in d.entries[1].detail
    )
    assert b.diff(loc, s2, s2).is_empty
    recreated = b.diff(loc, s0, dict(s2, table_id="other"))
    assert "recreated" in recreated.note

    # History is the transaction log; `at` fixes the recorded version.
    log = b.history(loc, None, 10)
    assert [e.id for e in log] == ["2", "1", "0"]
    assert log[1].message == "WRITE: +2 rows" and log[0].when is not None
    assert b.fingerprint(dict(loc, at="1"), None)["version"] == 1
    assert b.history(loc, "1", 10)[0].id == "1"
    with pytest.raises(BackendError):
        b.fingerprint(dict(loc, at="v1"), None)
    with pytest.raises(CapabilityError):
        b.fork(loc, Pin(id="abc", ref="tether.abc"), "x")
