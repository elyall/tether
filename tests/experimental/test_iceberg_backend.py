from __future__ import annotations

import pytest

pytest.importorskip("pyiceberg")

from pyiceberg.table.refs import SnapshotRef, SnapshotRefType
from pyiceberg.table.snapshots import Operation, Summary

from tether.backends.base import Capability, VerifyStatus
from tether.errors import BackendError
from tether.experimental.backends.iceberg import IcebergBackend
from tether.handles import IcebergHandle
from tether.manifest import Pin, Policy, compute_pin_id, ref_for_pin

LOCATOR = {"identifier": "ns.t", "branch": "main", "catalog_name": "t"}


class _Snap:
    def __init__(
        self,
        sid: int,
        parent: int | None = None,
        summary: Summary | None = None,
        timestamp_ms: int | None = None,
    ) -> None:
        self.snapshot_id = sid
        self.parent_snapshot_id = parent
        self.summary = summary
        self.timestamp_ms = timestamp_ms


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
        self.version = 0


class FakeManage:
    def __init__(self, store: _Store) -> None:
        self.store = store

    def __enter__(self) -> FakeManage:
        return self

    def __exit__(self, *exc: object) -> None:
        # Every table commit -- on any branch -- writes a new metadata.json.
        self.store.version += 1
        self.store.metadata_location = (
            f"s3://wh/ns/t/metadata/{self.store.version}.json"
        )

    def create_tag(self, *, snapshot_id: int, tag_name: str) -> None:
        self.store.refs[tag_name] = SnapshotRef(
            snapshot_id=snapshot_id, snapshot_ref_type=SnapshotRefType.TAG
        )

    def remove_tag(self, tag_name: str) -> None:
        self.store.refs.pop(tag_name, None)

    def create_branch(self, *, snapshot_id: int, branch_name: str) -> None:
        # pyiceberg stages a set-snapshot-ref update: creates or re-points.
        self.store.refs[branch_name] = SnapshotRef(
            snapshot_id=snapshot_id, snapshot_ref_type=SnapshotRefType.BRANCH
        )

    def set_current_snapshot(self, *, snapshot_id: int) -> None:
        self.create_branch(snapshot_id=snapshot_id, branch_name="main")

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
        main = self._store.refs.get("main")
        return _Snap(main.snapshot_id) if main else None

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


def test_history_and_at(backend: IcebergBackend, store: _Store) -> None:
    store.snapshots[1002] = _Snap(
        1002,
        1001,
        Summary(Operation.APPEND, **{"added-records": "5"}),
        timestamp_ms=1_700_000_000_000,
    )
    store.refs["main"] = SnapshotRef(
        snapshot_id=1002, snapshot_ref_type=SnapshotRefType.BRANCH
    )
    entries = backend.history(LOCATOR, None, 10)
    assert [e.id for e in entries] == ["1002", "1001"]
    assert entries[0].refs == ["main"] and entries[0].message == "append: +5 rows"
    assert entries[0].when == "2023-11-14T22:13:20+00:00"
    # `at` accepts a snapshot id or a ref name.
    assert backend.fingerprint(dict(LOCATOR, at="1001"), None)["snapshot_id"] == 1001
    assert backend.fingerprint(dict(LOCATOR, at="main"), None)["snapshot_id"] == 1002
    assert backend.history(LOCATOR, "1001", 10)[0].id == "1001"


def test_record_strategy_drops_pin_capability(backend: IcebergBackend) -> None:
    from tether.backends.base import effective_capabilities

    native = effective_capabilities(backend, LOCATOR, Policy(pin="native"))
    record = effective_capabilities(backend, LOCATOR, Policy(pin="record"))
    assert Capability.PIN in native
    assert Capability.PIN not in record
    # Record strategy still verifies via snapshot retention.
    assert backend.verify(LOCATOR, {"snapshot_id": 1001}, None, deep=False).ok


def test_unrelated_table_commits_do_not_change_the_state(
    backend: IcebergBackend, store: _Store
) -> None:
    """metadata.json is rewritten on every commit; the branch's snapshot is not."""
    before = backend.fingerprint(LOCATOR, None)
    with FakeTable(store).manage_snapshots() as ms:  # someone else's commit
        ms.create_tag(snapshot_id=1001, tag_name="release-1")
    assert store.metadata_location.endswith("/1.json")
    assert backend.fingerprint(LOCATOR, None) == before
    assert "metadata_location" not in before


def test_empty_table_commits_and_reads_empty(
    backend: IcebergBackend, store: _Store
) -> None:
    """A new table has no snapshot, so no `main` ref and nothing to tag;
    pyiceberg also refuses branch writes until the first snapshot exists."""
    store.snapshots.clear()
    store.refs.clear()
    state = backend.fingerprint(LOCATOR, None)
    assert state == {"snapshot_id": -1}
    pin_id = compute_pin_id("iceberg", backend.identity(LOCATOR), state, "d5d5d5d5")
    pin = backend.pin(LOCATOR, state, pin_id)
    assert not pin.created and pin.ref not in store.refs
    assert backend.verify(LOCATOR, state, pin, deep=True).ok
    assert backend.verify(LOCATOR, state, None, deep=True).ok
    assert backend.history(LOCATOR, None, 5) == []
    for source in (pin, state):
        handle = backend.open(LOCATOR, source, read_only=True)
        assert isinstance(handle, IcebergHandle) and handle.snapshot_id == -1
        with pytest.raises(BackendError, match="no snapshot yet"):
            backend.fork(LOCATOR, source, "tether.ws.dead.t")

    # The first write lands on main; the empty state is where it started.
    store.snapshots[1001] = _Snap(
        1001, None, Summary(Operation.APPEND, **{"added-records": "10"})
    )
    store.refs["main"] = SnapshotRef(
        snapshot_id=1001, snapshot_ref_type=SnapshotRefType.BRANCH
    )
    first = backend.fingerprint(LOCATOR, None)
    assert backend.ancestor_of(LOCATOR, state, first) is True
    diff = backend.diff(LOCATOR, state, first)
    assert not diff.note
    assert [(e.path, e.detail) for e in diff.entries] == [("1001", "append: +10 rows")]
    handle = backend.open(LOCATOR, pin, read_only=True)  # still the empty baseline
    assert isinstance(handle, IcebergHandle) and handle.snapshot_id == -1
    other = compute_pin_id("iceberg", backend.identity(LOCATOR), first, "d5d5d5d5")
    with pytest.raises(BackendError, match="not found"):
        backend.open(LOCATOR, Pin(id=other, ref=ref_for_pin(other)), read_only=True)


def test_fork_onto_an_existing_name_resets_it(
    backend: IcebergBackend, store: _Store
) -> None:
    store.snapshots[1002] = _Snap(1002, 1001)
    pin = backend.pin(LOCATOR, {"snapshot_id": 1001}, "abc123def456")
    wref = backend.fork(LOCATOR, pin, "tether.ws.dead.t")
    store.refs[wref] = SnapshotRef(
        snapshot_id=1002, snapshot_ref_type=SnapshotRefType.BRANCH
    )  # writes on the fork
    assert backend.fingerprint(LOCATOR, wref)["snapshot_id"] == 1002
    assert backend.fork(LOCATOR, pin, "tether.ws.dead.t") == wref
    assert backend.fingerprint(LOCATOR, wref)["snapshot_id"] == 1001  # reset
