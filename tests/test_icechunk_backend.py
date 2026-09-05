from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tether.backends.base import ObjectBackend
from tether.manifest import Locator
from tether.testing import run_conformance

ic = pytest.importorskip("icechunk")
zarr = pytest.importorskip("zarr")


def _new_repo(path: Path) -> str:
    """Create an Icechunk repo at ``path`` with one commit; return its URI."""
    path.mkdir(parents=True, exist_ok=True)
    storage = ic.local_filesystem_storage(str(path))
    repo = ic.Repository.create(storage)
    session = repo.writable_session("main")
    group = zarr.create_group(store=session.store)
    group.attrs["v"] = 0
    session.commit("init")
    return str(path)


def _commit(uri: str, branch: str, value: int) -> None:
    repo = ic.Repository.open(ic.local_filesystem_storage(uri))
    session = repo.writable_session(branch)
    group = zarr.open_group(store=session.store, mode="a")
    group.attrs["v"] = value
    session.commit(f"set v={value}")


class IcechunkHarness:
    def __init__(self, tmp: Path) -> None:
        from tether.backends.icechunk import IcechunkBackend

        self.backend: ObjectBackend = IcechunkBackend()
        self.tmp = tmp
        self._n = 0

    def new_object(self) -> Locator:
        uri = _new_repo(self.tmp / f"repo-{uuid.uuid4().hex[:8]}")
        return {"uri": uri, "branch": "main"}

    def mutate(self, locator: Locator, working_ref: str | None) -> None:
        self._n += 1
        _commit(locator["uri"], working_ref or "main", self._n)


def test_icechunk_backend_conformance(tmp_path: Path) -> None:
    run_conformance(IcechunkHarness(tmp_path))


def test_icechunk_engine_lifecycle(vcs_root: Path) -> None:
    from tether.handles import IcechunkHandle
    from tether.repo import Repo

    repo = Repo.init(vcs_root)
    uri = _new_repo(vcs_root / "imaging.icechunk")
    repo.add("zarr/imaging", "icechunk", {"uri": uri, "branch": "main"})

    res = repo.commit("baseline imaging")
    pin = res.pinned["zarr/imaging"]
    assert pin is not None
    backend = repo.backend_for("icechunk")
    assert pin.id in backend.list_pins({"uri": uri})

    # Fork a working branch and confirm it starts at the pinned snapshot.
    repo.new()
    wref = repo.workspace.working_refs["zarr/imaging"]
    assert wref and wref != "main"
    handle = repo.open("zarr/imaging")
    assert isinstance(handle, IcechunkHandle) and not handle.read_only

    # Advance the fork; status should report drift from the pin.
    _commit(uri, wref, 99)
    status = repo.status()
    obj = next(o for o in status.objects if o.key == "zarr/imaging")
    assert obj.changed

    res2 = repo.commit("imaging update")
    pin2 = res2.pinned["zarr/imaging"]
    assert pin2 is not None and pin2.id != pin.id

    # Read-only open at the first commit lands on the original snapshot/tag.
    old = repo.open("zarr/imaging", rev=res.vcs_commit)
    assert isinstance(old, IcechunkHandle) and old.read_only
    assert old.tag == pin.ref

    assert all(r.ok for r in repo.verify(deep=True).values())

    # Content diff between two commits: node-level changes from Icechunk
    # (written on the forked working branch, which is what tether tracks).
    ic_repo = ic.Repository.open(ic.local_filesystem_storage(uri))
    session = ic_repo.writable_session(wref)
    group = zarr.open_group(store=session.store, mode="a")
    arr = group.create_array("x", shape=(4,), chunks=(2,), dtype="i4")
    arr[:] = [1, 2, 3, 4]
    session.commit("add x")
    res3 = repo.commit("with array")
    entries = {
        e.key: e for e in repo.diff(res2.vcs_commit, res3.vcs_commit, content=True)
    }
    d = entries["zarr/imaging"].detail
    assert d is not None and d.unit == "nodes" and d.added == 1
    assert {(e.path, e.change, e.detail) for e in d.entries} >= {
        ("/x", "added", "array")
    }
