from __future__ import annotations

import pytest

pytest.importorskip("pyiceberg")

from pyiceberg.table.refs import SnapshotRef, SnapshotRefType

from tether.backends.base import Capability, VerifyStatus
from tether.backends.iceberg import IcebergBackend
from tether.handles import IcebergHandle
from tether.manifest import Policy, ref_for_pin

LOCATOR = {"identifier": "ns.t", "branch": "main", "catalog_name": "t"}


class _Snap:
    def __init__(self, sid: int) -> None:
        self.snapshot_id = sid


class _Store:
    def __init__(self) -> None:
        self.snapshots = [1001]
        self.refs: dict[str, SnapshotRef] = {
            "main": SnapshotRef(
                snapshot_id=1001, snapshot_ref_type=SnapshotRefType.BRANCH
            )
        }
        self.metadata_location = "s3://wh/ns/t/metadata/0.json"


class FakeManage:
    def __init__(self, store: _Store) -> None:
        self.store = store

    def __enter__(self) -> FakeManage:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def create_tag(self, *, snapshot_id: int, tag_name: str) -> None:
        self.store.refs[tag_name] = SnapshotRef(
            snapshot_id=snapshot_id, snapshot_ref_type=SnapshotRefType.TAG
        )

    def remove_tag(self, tag_name: str) -> None:
        self.store.refs.pop(tag_name, None)

    def create_branch(self, *, snapshot_id: int, branch_name: str) -> None:
        self.store.refs[branch_name] = SnapshotRef(
            snapshot_id=snapshot_id, snapshot_ref_type=SnapshotRefType.BRANCH
        )

    def remove_branch(self, branch_name: str) -> None:
        self.store.refs.pop(branch_name, None)


class FakeTable:
    def __init__(self, store: _Store) -> None:
        self._store = store
        self.metadata_location = store.metadata_location

    @property
    def refs(self) -> dict:
        return dict(self._store.refs)

    def snapshot_by_name(self, name: str):
        ref = self._store.refs.get(name)
        return _Snap(ref.snapshot_id) if ref else None

    def current_snapshot(self):
        return _Snap(self._store.refs["main"].snapshot_id)

    def snapshots(self):
        return [_Snap(s) for s in self._store.snapshots]

    def manage_snapshots(self) -> FakeManage:
        return FakeManage(self._store)


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> IcebergBackend:
    store = _Store()
    b = IcebergBackend()
    monkeypatch.setattr(b, "_table", lambda locator: FakeTable(store))
    return b


def test_native_pin_fork_verify_unpin(backend: IcebergBackend) -> None:
    state = backend.fingerprint(LOCATOR, None)
    assert state["snapshot_id"] == 1001

    pin = backend.pin(LOCATOR, state, "abc123def456")
    assert pin.ref == ref_for_pin("abc123def456")
    assert "abc123def456" in backend.list_pins(LOCATOR)
    assert backend.verify(LOCATOR, state, pin, deep=False).ok

    wref = backend.fork(LOCATOR, pin, "tether.ws.dead.t")
    assert backend.fingerprint(LOCATOR, wref)["snapshot_id"] == 1001

    handle = backend.open(LOCATOR, pin, read_only=True)
    assert isinstance(handle, IcebergHandle)
    assert handle.read_only and handle.ref == pin.ref

    backend.unpin(LOCATOR, pin)
    assert "abc123def456" not in backend.list_pins(LOCATOR)
    assert backend.verify(LOCATOR, state, pin, deep=False).status is (
        VerifyStatus.MISSING
    )


def test_verify_detects_drift(backend: IcebergBackend) -> None:
    pin = backend.pin(LOCATOR, {"snapshot_id": 1001}, "aaaa1111bbbb")
    report = backend.verify(LOCATOR, {"snapshot_id": 2002}, pin, deep=False)
    assert report.status is VerifyStatus.DRIFTED


def test_record_strategy_drops_pin_capability(backend: IcebergBackend) -> None:
    native = backend.effective_capabilities(LOCATOR, Policy(pin="native"))
    record = backend.effective_capabilities(LOCATOR, Policy(pin="record"))
    assert Capability.PIN in native
    assert Capability.PIN not in record
    # Record strategy still verifies via snapshot retention.
    assert backend.verify(LOCATOR, {"snapshot_id": 1001}, None, deep=False).ok
