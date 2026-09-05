from __future__ import annotations

import pytest

pytest.importorskip("pyiceberg")

from pyiceberg.table.refs import SnapshotRef, SnapshotRefType
from pyiceberg.table.snapshots import Operation, Summary

from tether.backends.base import Capability, VerifyStatus
from tether.backends.iceberg import IcebergBackend
from tether.handles import IcebergHandle
from tether.manifest import Policy, ref_for_pin

LOCATOR = {"identifier": "ns.t", "branch": "main", "catalog_name": "t"}


class _Snap:
    def __init__(
        self,
        sid: int,
        parent: int | None = None,
        summary: Summary | None = None,
    ) -> None:
        self.snapshot_id = sid
        self.parent_snapshot_id = parent
        self.summary = summary


class _Store:
    def __init__(self) -> None:
        self.snapshots = {
            1001: _Snap(
                1001, None, Summary(Operation.APPEND, **{"added-records": "10"})
            )
        }
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
        return list(self._store.snapshots.values())

    def manage_snapshots(self) -> FakeManage:
        return FakeManage(self._store)


@pytest.fixture
def store() -> _Store:
    return _Store()


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch, store: _Store) -> IcebergBackend:
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


def test_diff_walks_snapshot_ancestry(backend: IcebergBackend, store: _Store) -> None:
    store.snapshots[1002] = _Snap(
        1002,
        1001,
        Summary(Operation.APPEND, **{"added-records": "5", "added-data-files": "1"}),
    )
    store.snapshots[1003] = _Snap(
        1003, 1002, Summary(Operation.DELETE, **{"deleted-records": "2"})
    )
    store.snapshots[2001] = _Snap(  # a diverged branch head
        2001, 1001, Summary(Operation.APPEND, **{"total-records": "99"})
    )
    d = backend.diff(LOCATOR, {"snapshot_id": 1001}, {"snapshot_id": 1003})
    assert d.unit == "snapshots" and d.modified == 2 and not d.note
    assert [(e.path, e.detail) for e in d.entries] == [
        ("1002", "append: +5 rows, +1 files"),
        ("1003", "delete: -2 rows"),
    ]
    assert backend.diff(LOCATOR, {"snapshot_id": 1003}, {"snapshot_id": 1003}).is_empty
    diverged = backend.diff(LOCATOR, {"snapshot_id": 1003}, {"snapshot_id": 2001})
    assert "not an ancestor" in diverged.note
    assert [(e.path, e.detail) for e in diverged.entries] == [
        ("total-records", "? -> 99")
    ]


def test_record_strategy_drops_pin_capability(backend: IcebergBackend) -> None:
    native = backend.effective_capabilities(LOCATOR, Policy(pin="native"))
    record = backend.effective_capabilities(LOCATOR, Policy(pin="record"))
    assert Capability.PIN in native
    assert Capability.PIN not in record
    # Record strategy still verifies via snapshot retention.
    assert backend.verify(LOCATOR, {"snapshot_id": 1001}, None, deep=False).ok
