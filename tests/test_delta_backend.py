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
    with pytest.raises(CapabilityError):
        b.fork(loc, Pin(id="abc", ref="tether.abc"), "x")
