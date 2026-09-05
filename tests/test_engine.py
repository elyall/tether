from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tether.backends.memory import default_store
from tether.errors import (
    ConfigError,
    ImmutableObjectModified,
    StaleWorkingCopyError,
)
from tether.handles import MemoryHandle
from tether.manifest import Pin, Policy, ref_for_pin, write_workspace
from tether.repo import Repo


def _mem_object(repo: Repo, key: str = "db") -> str:
    store = default_store()
    name = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(name)  # main -> s0 (empty)
    repo.add(key, "memory", {"system": name, "branch": "main"})
    return name


def test_full_lifecycle(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    backend = repo.backend_for("memory")

    res1 = repo.commit("baseline")
    assert res1.vcs_commit is not None
    pin1 = res1.pinned["db"]
    assert pin1 is not None
    assert pin1.id in backend.list_pins({"system": system})

    # Fork working refs, then write through a native handle.
    repo.new()
    wref = repo.workspace.working_refs["db"]
    assert wref and wref != "main"
    handle = repo.open("db")
    assert isinstance(handle, MemoryHandle)
    assert not handle.read_only
    handle.write({"x": 1})

    status = repo.status()
    db_status = next(o for o in status.objects if o.key == "db")
    assert db_status.changed and db_status.pinned

    res2 = repo.commit("update")
    pin2 = res2.pinned["db"]
    assert pin2 is not None and pin2.id != pin1.id

    # Read-only open at the first commit resolves the original (empty) state.
    old = repo.open("db", rev=res1.vcs_commit)
    assert isinstance(old, MemoryHandle)
    assert old.read_only
    assert old.read() == {}

    # Everything verifies; diff reports the change.
    assert all(r.ok for r in repo.verify().values())
    diff = {e.key: e for e in repo.diff(res1.vcs_commit, res2.vcs_commit)}
    assert diff["db"].change == "changed"


def test_gc_removes_orphan_pin(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    repo.commit("baseline")
    backend = repo.backend_for("memory")
    locator = {"system": system}

    # An orphan native pin that no manifest references.
    sid = default_store().system(system).branches["main"]
    backend.pin(locator, {"snapshot_id": sid}, "orphan0badid")
    assert "orphan0badid" in backend.list_pins(locator)

    dry = repo.gc(dry_run=True)
    assert "orphan0badid" in dry.unpinned.get("memory", [])
    assert "orphan0badid" in backend.list_pins(locator)  # dry-run kept it

    repo.gc(dry_run=False)
    assert "orphan0badid" not in backend.list_pins(locator)


def test_stale_working_copy_blocks_writes(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    _mem_object(repo)
    repo.commit("baseline")
    repo.new()

    # Simulate someone advancing HEAD manifests underneath us.
    repo.workspace.base = "stale-hash"
    write_workspace(repo.root, repo.workspace)
    reloaded = Repo.find(vcs_root)
    assert reloaded.is_stale()
    with pytest.raises(StaleWorkingCopyError):
        reloaded.open("db")


def test_immutable_file_drift_is_error(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    data = vcs_root / "data.bin"
    data.write_text("original", encoding="utf-8")
    repo.add("f", "file", {"uri": str(data)})
    res = repo.commit("baseline")
    assert "f" in res.unrecoverable  # Observed tier: recorded, not recoverable

    data.write_text("changed content", encoding="utf-8")
    with pytest.raises(ImmutableObjectModified):
        repo.status()


def test_track_mode_uses_base_branch(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    _mem_object(repo)
    # Switch policy to track before committing.
    repo.objects["db"].policy = Policy(write="track")
    repo.commit("baseline")
    repo.new()
    assert repo.workspace.working_refs["db"] == "main"


def test_open_missing_object_errors(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    with pytest.raises(ConfigError):
        repo.open("nope")


def test_ref_for_pin_helper_used_in_gc(vcs_root: Path) -> None:
    # Guard against accidental prefix drift between pin() and gc().
    assert ref_for_pin("abc") == "tether.abc"
    _ = Pin("abc", ref_for_pin("abc"))
