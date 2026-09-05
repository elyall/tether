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

    # All-history verify walks both commits via the batched reader and
    # labels reports by commit prefix (jj also reports the working-copy commit).
    assert res2.vcs_commit is not None
    label1 = f"{res1.vcs_commit[:12]}:db"
    label2 = f"{res2.vcs_commit[:12]}:db"
    history = repo.verify(all_history=True)
    assert {label1, label2} <= set(history)
    assert all(r.ok for r in history.values())

    # Dropping a pin natively is reported as missing at that commit only.
    backend.unpin({"system": system}, pin1)
    history = repo.verify(all_history=True)
    assert not history[label1].ok
    assert history[label2].ok


def test_verify_dedupes_identical_records(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    repo.commit("baseline")
    for i in range(3):  # commits that do not touch the object
        (vcs_root / f"note{i}.txt").write_text("x", encoding="utf-8")
        repo.vcs.commit([f"note{i}.txt"], f"note {i}")

    backend = repo.backend_for("memory")
    calls: list[tuple] = []
    real_verify = backend.verify

    def counting_verify(locator, state, pin, deep):
        calls.append((locator["system"], pin.id if pin else None))
        return real_verify(locator, state, pin, deep)

    monkeypatch.setattr(backend, "verify", counting_verify)
    reports = repo.verify(all_history=True)
    # The same pin appears at >= 4 commits (jj also has the working-copy
    # commit) but is verified once.
    assert len([k for k in reports if k.endswith(":db")]) >= 4
    pin = repo.objects["db"].pin
    assert pin is not None
    assert calls == [(system, pin.id)]


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


def test_content_diff_and_listings(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    data = vcs_root / "plate"
    (data / "sub").mkdir(parents=True)
    (data / "a.bin").write_bytes(b"aaaa")
    (data / "sub" / "b.bin").write_bytes(b"bb")
    repo.add("raw/plate", "file", {"uri": str(data)}, policy=Policy(file="versioned"))
    r1 = repo.commit("baseline")
    assert r1.vcs_commit is not None

    # The directory listing was stored content-addressed and committed.
    listings = sorted(p.name for p in (vcs_root / ".tether" / "listings").iterdir())
    assert len(listings) == 1 and listings[0].endswith(".jsonl")
    assert repo.vcs.files_at(r1.vcs_commit, ".tether/listings").keys() == {
        f".tether/listings/{listings[0]}"
    }

    # Change both objects and commit again.
    default_store().write(system, "main", {"rows": 3, "schema": "v2"})
    (data / "a.bin").write_bytes(b"aaaaaaaa")
    (data / "sub" / "b.bin").unlink()
    (data / "c.bin").write_bytes(b"c")
    r2 = repo.commit("update")
    assert r2.vcs_commit is not None

    entries = {e.key: e for e in repo.diff(r1.vcs_commit, r2.vcs_commit, content=True)}
    db = entries["db"]
    assert db.change == "changed" and db.detail is not None
    assert db.detail.unit == "keys" and db.detail.added == 2
    plate = entries["raw/plate"]
    assert plate.change == "changed" and plate.detail is not None
    assert (plate.detail.added, plate.detail.removed, plate.detail.modified) == (
        1,
        1,
        1,
    )
    assert {e.path: e.change for e in plate.detail.entries} == {
        "a.bin": "modified",
        "c.bin": "added",
        "sub/b.bin": "removed",
    }
    # Without --content no detail is computed; unchanged objects never are.
    assert all(e.detail is None for e in repo.diff(r1.vcs_commit, r2.vcs_commit))

    # Listings survive in VCS even when the working-tree copy is gone.
    for p in (vcs_root / ".tether" / "listings").iterdir():
        p.unlink()
    again = {e.key: e for e in repo.diff(r1.vcs_commit, r2.vcs_commit, content=True)}
    assert again["raw/plate"].detail is not None
    assert again["raw/plate"].detail.added == 1 and not again["raw/plate"].detail.note

    # gc prunes listings that no manifest in history references.
    from tether.manifest import listing_path

    orphan = listing_path(vcs_root, "deadbeefdeadbeefdead.jsonl")
    orphan.parent.mkdir(exist_ok=True)
    orphan.write_text('{"p":"x","k":"1:1","s":1}\n', encoding="utf-8")
    report = repo.gc(dry_run=True)
    assert report.deleted_listings == ["deadbeefdeadbeefdead.jsonl"]
    assert orphan.exists()
    repo.gc(dry_run=False)
    assert not orphan.exists()


def test_content_diff_reports_backend_failures_per_object(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    r1 = repo.commit("baseline")
    default_store().write(system, "main", {"x": 1})
    r2 = repo.commit("update")
    assert r1.vcs_commit and r2.vcs_commit
    backend = repo.backend_for("memory")

    def boom(*args, **kwargs):
        raise RuntimeError("no diff for you")

    monkeypatch.setattr(backend, "diff", boom)
    entries = {e.key: e for e in repo.diff(r1.vcs_commit, r2.vcs_commit, content=True)}
    assert entries["db"].detail is None
    assert entries["db"].detail_error == "no diff for you"


def test_new_auto_fork_reforks_after_commit(vcs_root: Path) -> None:
    from tether.manifest import RepoConfig

    repo = Repo.init(vcs_root, config=RepoConfig(new_auto_fork=True))
    system = _mem_object(repo)
    repo.commit("baseline")
    first = repo.workspace.working_refs["db"]
    assert first.startswith("tether.ws.")  # forked without an explicit new()
    default_store().write(system, first, {"x": 1})
    repo.commit("update")
    # Re-forked from the new pin: same deterministic name, now at the new state.
    assert repo.workspace.working_refs["db"] == first
    assert default_store().read(system, first) == {"x": 1}
    assert not repo.is_stale()


def test_ref_for_pin_helper_used_in_gc(vcs_root: Path) -> None:
    # Guard against accidental prefix drift between pin() and gc().
    assert ref_for_pin("abc") == "tether.abc"
    _ = Pin("abc", ref_for_pin("abc"))
