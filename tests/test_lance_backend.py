from __future__ import annotations

from pathlib import Path

import pytest

from tether.backends.base import ObjectBackend, VerifyStatus
from tether.errors import BackendError
from tether.handles import LanceHandle
from tether.manifest import Locator, compute_pin_id, ref_for_pin, working_ref_name
from tether.testing import run_conformance

pa = pytest.importorskip("pyarrow")
lance = pytest.importorskip("lance")
from tether.backends.lance import LanceBackend  # noqa: E402


def _append(ds, n: int) -> None:
    lance.write_dataset(pa.table({"a": [n]}), ds, mode="append")


class LanceHarness:
    def __init__(self, tmp: Path) -> None:
        self.backend: ObjectBackend = LanceBackend()
        self.tmp = tmp
        self._n = 0

    def new_object(self) -> Locator:
        self._n += 1
        uri = str(self.tmp / f"d{self._n}.lance")
        lance.write_dataset(pa.table({"a": [0]}), uri)
        return {"uri": uri}

    def mutate(self, locator: Locator, working_ref: str | None) -> None:
        self._n += 1
        ds = lance.dataset(locator["uri"])
        target = (
            ds
            if working_ref in (None, "main")
            else ds.checkout_version((working_ref, None))
        )
        _append(target, self._n)


def test_lance_conformance(tmp_path: Path) -> None:
    run_conformance(LanceHarness(tmp_path))


def test_lance_fork_lifecycle(tmp_path: Path) -> None:
    b = LanceBackend()
    uri = str(tmp_path / "d.lance")
    lance.write_dataset(pa.table({"a": [1, 2]}), uri)
    loc = {"uri": uri}

    state = b.fingerprint(loc, None)
    assert state == {"branch": "main", "version": 1}
    pid = compute_pin_id("lance", b.identity(loc), state)
    pin = b.pin(loc, state, pid)
    assert pin.ref == ref_for_pin(pid)
    assert b.pin(loc, state, pid) == pin  # idempotent
    with pytest.raises(BackendError):  # same ref, different target
        b.pin(loc, {"branch": "main", "version": 0}, pid)

    # A fresh fork reports the parent's address, so a no-op commit is a no-op.
    name = working_ref_name("ws0123abcd", "tables/x")
    wref = b.fork(loc, pin, name)
    assert wref == name
    assert b.fingerprint(loc, wref) == state

    # Writes on the fork move only the fork.
    handle = b.open(loc, wref, read_only=False)
    assert isinstance(handle, LanceHandle) and not handle.read_only
    _append(handle.dataset, 3)
    forked = b.fingerprint(loc, wref)
    assert forked == {"branch": wref, "version": 2}
    assert b.fingerprint(loc, None) == state

    # Pin the fork's state: the tag names (branch, version) on the fork.
    pid2 = compute_pin_id("lance", b.identity(loc), forked)
    pin2 = b.pin(loc, forked, pid2)
    ro = b.open(loc, pin2, read_only=True)
    assert isinstance(ro, LanceHandle) and ro.read_only and ro.tag == pin2.ref
    assert ro.dataset.to_table().to_pydict() == {"a": [1, 2, 3]}
    assert b.verify(loc, forked, pin2, deep=True).ok
    assert {pid, pid2} <= b.list_pins(loc)

    # Lance keeps a tagged branch alive; delete_working_ref is a no-op for it,
    # and re-forking under the same name picks a sibling name.
    b.delete_working_ref(loc, wref)
    assert wref in lance.dataset(uri).branches.list()
    wref2 = b.fork(loc, pin, name)
    assert wref2 == f"{name}.2"
    assert b.fingerprint(loc, wref2) == state

    # Once the tag is gone the branch can be dropped and the pin is missing.
    b.unpin(loc, pin2)
    assert pid2 not in b.list_pins(loc)
    assert b.verify(loc, forked, pin2, deep=False).status is VerifyStatus.MISSING
    b.delete_working_ref(loc, wref)
    assert wref not in lance.dataset(uri).branches.list()

    # Addressable reads by (branch, version) and drift detection on the tag.
    at = b.open(loc, state, read_only=True)
    assert isinstance(at, LanceHandle) and at.version == 1
    assert b.verify(loc, {"branch": "main", "version": 0}, pin, False).status is (
        VerifyStatus.DRIFTED
    )
    assert b.verify(loc, state, None, True).ok
    assert (
        b.verify(loc, {"branch": "main", "version": 42}, None, True).status
        is VerifyStatus.MISSING
    )
    with pytest.raises(BackendError):
        b.fingerprint({"uri": str(tmp_path / "absent.lance")}, None)


def test_lance_diff_reports_fragments_and_columns(tmp_path: Path) -> None:
    b = LanceBackend()
    uri = str(tmp_path / "d.lance")
    lance.write_dataset(pa.table({"a": [1, 2, 3]}), uri)
    loc = {"uri": uri}
    s1 = b.fingerprint(loc, None)
    _append(lance.dataset(uri), 4)
    lance.dataset(uri).delete("a = 2")
    s3 = b.fingerprint(loc, None)
    d = b.diff(loc, s1, s3)
    assert d.unit == "fragments" and (d.added, d.removed, d.modified) == (1, 0, 1)
    assert {(e.path, e.change, e.detail) for e in d.entries} == {
        ("fragment 1", "added", "+1 rows"),
        ("fragment 0", "modified", "deletions changed"),
    }
    lance.dataset(uri).add_columns({"b": "a * 2"})
    s4 = b.fingerprint(loc, None)
    d2 = b.diff(loc, s3, s4)
    assert [(e.path, e.change) for e in d2.entries] == [("column b", "added")]
    assert b.diff(loc, s4, s4).is_empty

    # History lists versions newest first with the branch head marked; `at`
    # takes a version number or a tag on the base branch.
    b.pin(loc, s1, "aaaa1111bbbb")
    log = b.history(loc, None, 10)
    assert [e.id for e in log] == ["4", "3", "2", "1"]
    assert log[0].refs == ["main"] and log[-1].refs == ["tether.aaaa1111bbbb"]
    assert all(e.when for e in log)
    assert b.fingerprint(dict(loc, at="1"), None) == s1
    assert b.fingerprint(dict(loc, at="tether.aaaa1111bbbb"), None) == s1
    assert b.history(loc, None, 2) == log[:2]
    with pytest.raises(BackendError):
        b.fingerprint(dict(loc, at="not-a-tag"), None)
