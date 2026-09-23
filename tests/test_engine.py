from __future__ import annotations

import dataclasses
import json
import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tether.backends.memory import default_store
from tether.errors import (
    BackendError,
    ConfigError,
    ImmutableObjectModified,
    MultiObjectError,
    StalePlanError,
    StaleWorkingCopyError,
    TetherError,
    VcsError,
)
from tether.handles import MemoryHandle
from tether.manifest import Locator, ObjectManifest, Pin, Policy, State, ref_for_pin
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

    # `new` only decides the branch name (lazy); the first writable open forks.
    repo.new(bookmark="work")
    assert "db" not in repo.workspace.working_refs
    assert repo.workspace.pending_forks["db"].startswith("tether.ws.")
    handle = repo.open("db")
    assert isinstance(handle, MemoryHandle)
    assert not handle.read_only
    wref = repo.workspace.working_refs["db"]
    assert wref == handle.ref and wref != "main"
    assert "db" not in repo.workspace.pending_forks
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

    # An orphan native pin in this dataset's namespace that no manifest
    # references, and one from another dataset sharing the store.
    sid = default_store().system(system).branches["main"]
    orphan = f"{repo.config.dataset_id}.0000000000badbad"
    backend.pin(locator, {"snapshot_id": sid}, orphan)
    backend.pin(locator, {"snapshot_id": sid}, "ffffffff.0000000000badbad")
    assert {orphan, "ffffffff.0000000000badbad"} <= backend.list_pins(locator)

    # Made by hand, so no commit of this clone created it: kept by default.
    plain = repo.gc(dry_run=True)
    assert plain.kept_pins == {"memory": [orphan]} and not plain.unpinned
    dry = repo.gc(dry_run=True, release_foreign=True)
    assert dry.unpinned.get("memory", []) == [orphan] and not dry.kept_pins
    assert dry.plan is not None
    assert any("1 pin(s) of other datasets left alone" in n for n in dry.plan.notes)
    assert orphan in backend.list_pins(locator)  # dry-run kept it

    repo.gc(dry_run=False, release_foreign=True)
    assert orphan not in backend.list_pins(locator)
    assert "ffffffff.0000000000badbad" in backend.list_pins(locator)  # not ours


def _someone_else_commits(repo: Repo, key: str, state: dict) -> None:
    """Rewrite `key`'s committed manifest as another workspace's commit would:
    a new state *and* a pin that names it (a manifest whose pin points at a
    different state than it records is drift, and refused)."""
    from tether.manifest import compute_pin_id, write_object

    m = repo.objects[key]
    backend = repo.backend_for(m.kind)
    pin = backend.pin(
        m.locator,
        state,
        compute_pin_id(
            m.kind, backend.identity(m.locator), state, repo.config.dataset_id
        ),
    )
    write_object(repo.root, m.with_pin(state=state, pin=pin, recoverable=True))


def test_stale_working_copy_blocks_writes(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    repo.commit("baseline")
    repo.new(bookmark="work")
    assert not repo.is_stale()

    # Someone else's commit changes db's committed state underneath us.
    other = default_store().write(system, "main", {"theirs": 1})
    _someone_else_commits(repo, "db", {"snapshot_id": other})
    reloaded = Repo.find(vcs_root)
    assert reloaded.stale_keys() == ["db"]
    assert reloaded.status(do_snapshot=False).stale_keys == ["db"]
    with pytest.raises(StaleWorkingCopyError, match="committed state of db"):
        reloaded.open("db")

    # Registering or removing an unrelated object does not un-stale it.
    _mem_object(reloaded, "other")
    assert reloaded.stale_keys() == ["db"]
    reloaded.remove("other")
    assert reloaded.stale_keys() == ["db"]
    # `new` re-decides from the current manifests and clears it.
    reloaded.new()
    assert not reloaded.is_stale()

    # Our own commit never makes us stale, even though the manifest moves.
    handle = reloaded.open("db")
    assert isinstance(handle, MemoryHandle)
    handle.write({"mine": 1})
    reloaded.commit("mine")
    assert not reloaded.is_stale()

    # On trunk every object writes to its upstream branch and is never stale.
    reloaded.new("main")
    assert reloaded.on_trunk()
    _someone_else_commits(
        reloaded, "db", {"snapshot_id": default_store().write(system, "main", {"x": 1})}
    )
    assert Repo.find(vcs_root).stale_keys() == []


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


def test_trunk_uses_base_branch(vcs_root: Path) -> None:
    """On the trunk bookmark every object's working ref is its upstream."""
    repo = Repo.init(vcs_root)
    _mem_object(repo)
    repo.commit("baseline")
    assert repo.workspace.bookmark == "main"
    repo.new()
    assert repo.on_trunk() and repo.workspace.working_refs["db"] == "main"
    plan = repo.plan_new()
    assert [a.op for a in plan.actions] == ["trunk"]
    # Off trunk, the same object forks.
    repo.new(bookmark="work")
    assert not repo.on_trunk()
    assert (
        repo.workspace.pending_forks["db"]
        == "tether.ws." + repo.config.dataset_id + ".work"
    )


def test_lazy_forking(vcs_root: Path) -> None:
    """Lazy default: `new` creates nothing; the first writable open forks."""
    from tether.manifest import RepoConfig

    repo = Repo.init(vcs_root)
    store = default_store()
    system = _mem_object(repo)
    recorded = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(recorded)
    store.write(recorded, "main", {"v": 1})
    repo.add("scratch", "memory", {"system": recorded}, policy=Policy(pin="record"))
    repo.commit("baseline")

    plan = repo.plan_new(bookmark="work")
    ops = {a.key: a.op for a in plan.actions}
    # The pinned object defers; the pin-less one forks now -- nothing else would
    # stop its recorded snapshot from expiring before the first write.
    assert ops == {"db": "defer-fork", "scratch": "fork"}
    assert "cannot expire" in next(a for a in plan.actions if a.key == "scratch").detail
    repo.apply_new(plan, verify=False)
    branches = store.system(system).branches
    assert not any(b.startswith("tether.ws.") for b in branches)
    assert any(b.startswith("tether.ws.") for b in store.system(recorded).branches)
    assert repo.workspace.working_refs.keys() == {"scratch"}
    assert repo.workspace.pending_forks.keys() == {"db"}

    # Read-only opens, status, and commit never trigger the fork.
    ro = repo.open("db", read_only=True)
    pin = repo.objects["db"].pin
    assert pin is not None
    assert isinstance(ro, MemoryHandle) and ro.read_only and ro.ref == pin.ref
    assert not any(b.startswith("tether.ws.") for b in branches)
    assert next(o for o in repo.status().objects if o.key == "db").changed is False
    assert repo.commit("nothing").pinned == {}  # unchanged: no branch, no new pin
    assert "db" in repo.workspace.pending_forks  # still pending

    # The first writable open forks from the pin; later opens reuse the branch.
    handle = repo.open("db")
    assert isinstance(handle, MemoryHandle)
    wref = repo.workspace.working_refs["db"]
    assert handle.ref == wref and wref in branches and wref.startswith("tether.ws.")
    assert not repo.workspace.pending_forks.get("db")
    again = repo.open("db")
    assert isinstance(again, MemoryHandle) and again.ref == wref
    assert repo.materialize_fork("db") == wref  # idempotent

    # A second new keeps a branch that already sits at the pin (it was just
    # committed from): no re-fork, no deferral.
    repo.new()
    assert repo.workspace.working_refs["db"] == wref
    assert "db" not in repo.workspace.pending_forks
    (kept,) = [a for a in repo.plan_new().actions if a.key == "db"]
    assert kept.op == "reuse"
    # Removing a still-pending object leaves nothing behind to gc.
    repo.remove("scratch")
    assert "scratch" not in repo.workspace.pending_forks
    assert not [a for a in repo.plan_gc().actions if a.key == "db"]

    # A stale workspace refuses to materialize until `new` runs again.
    _someone_else_commits(
        repo, "db", {"snapshot_id": store.write(system, "main", {"theirs": 1})}
    )
    with pytest.raises(StaleWorkingCopyError):
        Repo.find(vcs_root).open("db")
    with pytest.raises(ConfigError):
        repo.materialize_fork("nope")

    # `[new] fork = "eager"` restores up-front branches.
    eager = Repo.init(vcs_root / "eager", config=RepoConfig(new_fork="eager"))
    _mem_object(eager)
    eager.commit("baseline")
    eager.new(bookmark="eager-work")
    assert eager.workspace.working_refs["db"].startswith("tether.ws.")
    assert not eager.workspace.pending_forks


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
    r2 = repo.pull(message="update")  # files sit at their pin until pulled
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


def test_commit_rollback_spares_pins_it_did_not_create(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pin()` is idempotent, so a commit can be handed a pin an earlier commit
    made. When a later pin in the same commit fails, rollback must release only
    the pins this commit created -- not the reused one history still names."""
    repo = Repo.init(vcs_root)
    store = default_store()
    a = _mem_object(repo, "a")
    store.write(a, "main", {"v": 1})
    first = repo.commit("pin a")
    pin_a = first.pinned["a"]
    assert pin_a is not None and pin_a.ref in store.system(a).tags

    # Two more objects: `b` on the *same* system and state as `a` (its pin id
    # is the same, so pin() reuses a's tag); `c` on a system whose pin fails.
    repo.add("b", "memory", {"system": a, "branch": "main"})
    c = _mem_object(repo, "c")
    store.write(c, "main", {"v": 1})
    backend = repo.backend_for("memory")
    real_pin = backend.pin

    def failing(locator: Locator, state: State, pin_id: str) -> Pin:
        if locator["system"] == c:
            raise BackendError("store unreachable", kind="memory")
        return real_pin(locator, state, pin_id)

    monkeypatch.setattr(backend, "pin", failing)
    with pytest.raises(BackendError, match="store unreachable"):
        repo.commit("pin b and c")
    # The reused tag survives; nothing was created for c.
    assert pin_a.ref in store.system(a).tags
    assert not store.system(c).tags
    assert all(r.ok for r in Repo.find(vcs_root).verify().values() if r)


def test_a_drifted_pin_is_never_read_or_forked(vcs_root: Path) -> None:
    """A tag moved by hand must not hand back the wrong data under a commit's
    name: `open --rev`, `new` from that commit, and `promote --rev` all refuse
    with PinDriftError, while a *deleted* tag still falls back to the state."""
    from tether.errors import PinDriftError

    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    store.write(system, "main", {"v": 1})
    c1 = repo.commit("v1").vcs_commit
    pin = repo.objects["db"].pin
    assert c1 is not None and pin is not None
    s1 = store.system(system).branches["main"]
    s2 = store.write(system, "main", {"v": 2})

    # Move the tag: it now names s2 while the manifest at c1 says s1.
    store.system(system).tags[pin.ref] = s2
    with pytest.raises(PinDriftError, match="no longer names the committed state"):
        repo.open("db", rev=c1)
    with pytest.raises(TetherError, match="no longer names the committed state"):
        repo.new(c1, bookmark="from-c1", eager=True)
    repo.new(bookmark="work", eager=False)  # lazy: nothing forked yet
    (action,) = [a for a in repo.plan_promote(rev=c1).actions if a.op == "refuse"]
    assert "no longer names the committed state" in action.detail

    # Put it back: everything works again, and reads by pin are exact.
    store.system(system).tags[pin.ref] = s1
    handle = repo.open("db", rev=c1)
    assert isinstance(handle, MemoryHandle) and handle.read() == {"v": 1}

    # Delete it: the recorded state is still addressable, so reads fall back.
    del store.system(system).tags[pin.ref]
    handle = repo.open("db", rev=c1)
    assert isinstance(handle, MemoryHandle) and handle.read() == {"v": 1}


def test_saved_gc_plan_will_not_delete_a_branch_that_moved(vcs_root: Path) -> None:
    """A gc plan records the head of every branch it will delete; applying it
    after the branch gained writes stops with StalePlanError, branch intact."""
    from tether.errors import StalePlanError

    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    repo.new(bookmark="work", eager=True)
    branch = repo.workspace.working_refs["db"]
    # Leave the bookmark behind: its branch becomes a stray gc may delete.
    repo.new("main")
    repo.vcs.bookmark_delete("work")
    plan = repo.plan_gc(prune_bookmarks=True)
    (delete,) = [a for a in plan.actions if a.op == "delete-branch"]
    assert delete.target == branch and delete.params["head"] is not None

    store.write(system, branch, {"late": 1})  # writes after the plan was made
    with pytest.raises(StalePlanError, match="moved since the plan was made"):
        repo.apply_gc(plan)
    assert branch in store.system(system).branches
    assert store.read(system, branch) == {"late": 1}

    # A fresh plan sees the writes and keeps the branch without --force-prune.
    fresh = repo.plan_gc(prune_bookmarks=True)
    assert [a.op for a in fresh.actions if a.target == branch] == ["keep-branch"]


def test_stale_gc_plan_does_nothing_at_all(vcs_root: Path) -> None:
    """The head check runs over every branch the plan deletes before the first
    action: a plan with an unpin *and* a delete-branch whose branch moved must
    not release the pin and then stop."""
    from tether.errors import StalePlanError

    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    repo.new(bookmark="work", eager=True)
    branch = repo.workspace.working_refs["db"]
    repo.new("main")
    repo.vcs.bookmark_delete("work")
    # An orphan pin too, so the plan carries an unpin ahead of the deletion.
    backend = repo.backend_for("memory")
    m = repo.objects["db"]
    from tether.manifest import compute_pin_id

    orphan_state = {"snapshot_id": store.write(system, "main", {"v": 9})}
    orphan = backend.pin(
        m.locator,
        orphan_state,
        compute_pin_id(
            "memory", backend.identity(m.locator), orphan_state, repo.config.dataset_id
        ),
    )
    plan = repo.plan_gc(prune_bookmarks=True, release_foreign=True)
    ops = [a.op for a in plan.actions if a.op in ("unpin", "delete-branch")]
    assert ops == ["unpin", "delete-branch"], ops

    store.write(system, branch, {"late": 1})
    with pytest.raises(StalePlanError):
        repo.apply_gc(plan)
    assert orphan.ref in store.system(system).tags  # the unpin did not run
    assert not repo.incomplete_ops()  # and no journal entry was left open


def test_gc_keeps_a_branch_that_moved_after_the_preflight(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preflight validated the whole plan; a move found at the moment of
    deletion is a race. The branch is kept and reported, the rest of the plan
    finishes, and the journal entry is complete."""
    from tether.manifest import compute_pin_id

    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    repo.new(bookmark="work", eager=True)
    branch = repo.workspace.working_refs["db"]
    repo.new("main")
    repo.vcs.bookmark_delete("work")
    backend = repo.backend_for("memory")
    m = repo.objects["db"]
    orphan_state = {"snapshot_id": store.write(system, "main", {"v": 9})}
    orphan = backend.pin(
        m.locator,
        orphan_state,
        compute_pin_id(
            "memory", backend.identity(m.locator), orphan_state, repo.config.dataset_id
        ),
    )
    plan = repo.plan_gc(prune_bookmarks=True, release_foreign=True)
    assert [a.op for a in plan.actions if a.op in ("unpin", "delete-branch")] == [
        "unpin",
        "delete-branch",
    ]

    real_unpin = backend.unpin

    def unpin_then_someone_writes(locator: Locator, pin: Pin) -> None:
        real_unpin(locator, pin)
        store.write(system, branch, {"late": 1})  # between preflight and delete

    monkeypatch.setattr(backend, "unpin", unpin_then_someone_writes)
    with pytest.raises(MultiObjectError, match="moved since the plan"):
        repo.apply_gc(plan)
    assert orphan.ref not in store.system(system).tags  # the unpin stood
    assert store.read(system, branch) == {"late": 1}  # the branch was kept
    assert not repo.incomplete_ops()
    assert repo.ops()[0].command == "gc" and repo.ops()[0].result["kept_working_refs"]


def test_force_prune_does_not_delete_blind_once_the_head_is_readable(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan that could not read a branch's head and decided under --force
    must not act if the head can be read at apply: re-plan with it in view."""
    from tether.errors import StalePlanError

    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    repo.new(bookmark="work", eager=True)
    branch = repo.workspace.working_refs["db"]
    store.write(system, branch, {"mine": 1})
    repo.new("main")
    repo.vcs.bookmark_delete("work")

    backend = repo.backend_for("memory")
    real_fp = backend.fingerprint

    def blind(locator: Locator, working_ref: str | None) -> State:
        if working_ref == branch:
            raise BackendError("unreadable", kind="memory")
        return real_fp(locator, working_ref)

    monkeypatch.setattr(backend, "fingerprint", blind)
    plan = repo.plan_gc(prune_bookmarks=True, force_prune=True)
    (delete,) = [a for a in plan.actions if a.op == "delete-branch"]
    assert delete.params.get("head") is None and delete.params.get("forced")
    monkeypatch.setattr(backend, "fingerprint", real_fp)
    with pytest.raises(StalePlanError, match="reads now"):
        repo.apply_gc(plan)
    assert store.read(system, branch) == {"mine": 1}


def test_undo_is_journaled_before_it_acts(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    _mem_object(repo)
    repo.commit("baseline")
    report = repo.undo()
    entries = repo.ops()
    assert entries[0].command == "undo" and not entries[0].incomplete
    assert (
        entries[0].undoes == report.op.id and entries[0].pre["target"] == report.op.id
    )
    # Completing the undo and marking its target undone is one record.
    assert entries[1].id == report.op.id and entries[1].undone_by == entries[0].id
    lines = [
        json.loads(line)
        for line in (vcs_root / ".tether" / "ops.jsonl").read_text().splitlines()
    ]
    done = [obj for obj in lines if obj.get("done") == entries[0].id]
    assert len(done) == 1 and done[0]["undone"] == report.op.id
    assert not any("undone" in obj and "done" not in obj for obj in lines)


def test_a_refused_undo_is_a_failed_attempt_not_an_interrupted_one(
    vcs_root: Path,
) -> None:
    """`undo` refusing (writes it would have to discard) must not leave a
    started entry behind: nothing was touched, so the attempt is recorded as
    failed and the log has no incomplete operation."""
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    repo.new(bookmark="work", eager=True)
    branch = repo.workspace.working_refs["db"]
    store.write(system, branch, {"unsaved": 1})
    with pytest.raises(TetherError, match="discard"):
        repo.undo()  # the `new` -- its branch now holds writes
    assert not repo.incomplete_ops()
    newest = repo.ops()[0]
    assert newest.command == "undo" and "discard" in newest.result["failed"]
    assert repo.ops()[1].command == "new" and repo.ops()[1].undone_by is None


def test_saved_gc_plan_is_bound_to_history(vcs_root: Path) -> None:
    """A commit made after the plan may reference a pin the plan releases."""
    from tether.errors import StalePlanError

    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    plan = repo.plan_gc()
    store.write(system, "main", {"v": 2})
    repo.commit("moved on")
    with pytest.raises(StalePlanError, match="history moved"):
        repo.apply_gc(plan)


def test_gc_plan_sees_a_commit_that_lands_during_its_history_walk(
    vcs_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit from another checkout that lands *while* plan_gc walks history
    is missing from the references it collected. The digest is taken before
    the walk, so that commit changes it and the apply refuses."""
    import subprocess

    from tether.errors import StalePlanError
    from tether.manifest import compute_pin_id

    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    baseline = repo.commit("baseline").vcs_commit
    assert baseline is not None
    backend = repo.backend_for("memory")
    m = repo.objects["db"]
    orphan_state = {"snapshot_id": store.write(system, "main", {"v": 9})}
    orphan = backend.pin(
        m.locator,
        orphan_state,
        compute_pin_id(
            "memory", backend.identity(m.locator), orphan_state, repo.config.dataset_id
        ),
    )
    other_root = tmp_path / "other-checkout"
    cmd = (
        ["jj", "workspace", "add", str(other_root)]
        if repo.vcs.kind == "jj"
        else ["git", "worktree", "add", "--detach", str(other_root)]
    )
    subprocess.run(cmd, cwd=vcs_root, check=True, capture_output=True)
    other = Repo.find(other_root)

    real_walk = repo._iter_history_objects

    def walk_with_a_commit_landing() -> Iterator[tuple[str, dict[str, ObjectManifest]]]:
        entries = list(real_walk())  # what the walk sees...
        # ...and, while it is still going, the other checkout commits a
        # manifest naming the orphan (on a branch, so git keeps it reachable)
        # and moves on, so only history names it.
        other.vcs.new_bookmark("theirs", None)
        _someone_else_commits(other, "db", orphan_state)
        other.vcs.commit(other._vcs_paths(), "theirs names the orphan")
        other.vcs.new(baseline)
        yield from entries

    monkeypatch.setattr(repo, "_iter_history_objects", walk_with_a_commit_landing)
    plan = repo.plan_gc(release_foreign=True)
    monkeypatch.undo()
    assert [a.target for a in plan.actions if a.op == "unpin"] == [orphan.ref]
    with pytest.raises(StalePlanError, match="another workspace"):
        repo.apply_gc(plan)
    assert orphan.ref in store.system(system).tags


def test_vcs_drift_asks_the_vcs_once(vcs_root: Path) -> None:
    """`status` runs `vcs_drift` every time; it must not spawn a process per
    op-log entry. Whatever the number of commits, liveness is one batched
    call and `commit_alive` is never used."""
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    for i in range(6):
        store.write(system, "main", {"v": i})
        repo.commit(f"v{i}")
    calls: list[list[str]] = []
    single: list[str] = []
    real = repo.vcs.alive_commits

    def batched(commits: list[str]) -> set[str]:
        calls.append(list(commits))
        return real(commits)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(repo.vcs, "alive_commits", batched)
        mp.setattr(repo.vcs, "commit_alive", lambda c: single.append(c) or True)
        assert repo.vcs_drift() == []
    assert len(calls) == 1 and len(calls[0]) == 6 and single == []


def test_find_does_not_touch_the_ignore_file(vcs_root: Path) -> None:
    """Opening a dataset is read-only: the ignore file is rewritten by `init`
    and `upgrade`, and by `find` only when an untracked file that exists is
    not yet ignored (never let secrets.toml reach a commit)."""
    from tether.manifest import GITIGNORE_FILENAME, tether_path

    Repo.init(vcs_root)
    gitignore = tether_path(vcs_root) / GITIGNORE_FILENAME
    text = gitignore.read_text()
    assert "/secrets.toml" in text and "/lock" in text
    stat = gitignore.stat()
    Repo.find(vcs_root)
    assert gitignore.stat().st_mtime_ns == stat.st_mtime_ns  # untouched
    # An older dataset whose ignore file predates secrets.toml: a find with
    # the file present adds the entry; without the file, nothing is written.
    gitignore.write_text("/workspace.toml\n/ops.jsonl\n")
    stat = gitignore.stat()
    Repo.find(vcs_root)
    assert gitignore.stat().st_mtime_ns == stat.st_mtime_ns
    secrets = tether_path(vcs_root) / "secrets.toml"
    secrets.write_text("[vcs]\n")
    secrets.chmod(0o600)
    Repo.find(vcs_root)
    assert "/secrets.toml" in gitignore.read_text()


def test_saved_gc_plan_survives_a_jj_snapshot(vcs_root: Path) -> None:
    """Under jj the working-copy commit's id changes whenever the tree is
    snapshotted (`jj log` after touching a file). A saved gc plan binds to
    the parent and to the history without working copies, so that alone
    does not stale it -- a real commit elsewhere still does."""
    if not (vcs_root / ".jj").exists():
        pytest.skip("jj only")
    import subprocess

    repo = Repo.init(vcs_root)
    _mem_object(repo)
    repo.commit("v1")
    plan = repo.plan_gc()
    (vcs_root / "scratch.txt").write_text("touch")
    subprocess.run(
        ["jj", "log", "-r", "@"], cwd=vcs_root, check=True, capture_output=True
    )
    repo.apply_gc(plan)  # not stale


def test_saved_gc_plan_sees_commits_made_in_other_workspaces(
    vcs_root: Path, tmp_path: Path
) -> None:
    """A pin that was an orphan at plan time may be referenced by a commit
    another checkout made since -- this checkout's head and manifests do not
    move. The plan is bound to every visible commit, so apply refuses."""
    import subprocess

    from tether.errors import StalePlanError
    from tether.manifest import compute_pin_id

    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    # An orphan pin: a state no commit names.
    backend = repo.backend_for("memory")
    m = repo.objects["db"]
    orphan_state = {"snapshot_id": store.write(system, "main", {"v": 9})}
    orphan = backend.pin(
        m.locator,
        orphan_state,
        compute_pin_id(
            "memory", backend.identity(m.locator), orphan_state, repo.config.dataset_id
        ),
    )
    plan = repo.plan_gc(release_foreign=True)
    assert [a.target for a in plan.actions if a.op == "unpin"] == [orphan.ref]

    # Meanwhile another checkout commits a manifest naming that state.
    other_root = tmp_path / "other-checkout"
    cmd = (
        ["jj", "workspace", "add", str(other_root)]
        if repo.vcs.kind == "jj"
        else ["git", "worktree", "add", "--detach", str(other_root)]
    )
    subprocess.run(cmd, cwd=vcs_root, check=True, capture_output=True)
    other = Repo.find(other_root)
    _someone_else_commits(other, "db", orphan_state)
    other.vcs.commit(other._vcs_paths(), "theirs names the orphan")

    assert repo._vcs_head_or_none() == plan.context["vcs_head"]  # our head did not move
    with pytest.raises(StalePlanError, match="another workspace"):
        repo.apply_gc(plan)
    assert orphan.ref in store.system(system).tags
    # A fresh plan sees the reference and keeps the pin.
    fresh = repo.plan_gc(release_foreign=True)
    assert not [a for a in fresh.actions if a.op in ("unpin", "keep-pin")]


def test_commit_and_gc_serialize_across_checkouts_of_one_repository(
    vcs_root: Path, tmp_path: Path
) -> None:
    """The checkout lock cannot order a gc here against a commit in another
    workspace; the repository-wide lock does. Both checkouts resolve the same
    shared store, and a commit waits while a gc holds it."""
    import subprocess

    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    other_root = tmp_path / "other-checkout"
    cmd = (
        ["jj", "workspace", "add", str(other_root)]
        if repo.vcs.kind == "jj"
        else ["git", "worktree", "add", "--detach", str(other_root)]
    )
    subprocess.run(cmd, cwd=vcs_root, check=True, capture_output=True)
    other = Repo.find(other_root)
    assert other.vcs.shared_dir() == repo.vcs.shared_dir()
    assert other.vcs.shared_dir().is_dir()

    other.REPO_LOCK_TIMEOUT = 0.3
    store.write(system, "main", {"v": 2})
    # What apply_gc holds while it decides and releases:
    with (
        repo._repo_lock(),
        pytest.raises(TetherError, match="committing, collecting or forking"),
    ):
        other.commit("racing")
    other.commit("after")  # released: no lock error


def test_promote_checks_the_source_it_reviewed(vcs_root: Path) -> None:
    """What lands must be what the plan showed: a fork that gained writes after
    planning is not promoted from a stale plan."""
    from tether.errors import StalePlanError

    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    repo.new(bookmark="work", eager=True)
    branch = repo.workspace.working_refs["db"]
    store.write(system, branch, {"v": 2})
    repo.commit("v2")
    plan = repo.plan_promote()
    store.write(system, branch, {"v": 3})  # after review
    with pytest.raises(StalePlanError, match=r"promote db: .* moved since the plan"):
        repo.apply_promote(plan)
    heads = store.system(system).branches
    assert heads["main"] != heads[branch]  # nothing landed


def test_promote_rev_checks_the_pin_it_reviewed(vcs_root: Path) -> None:
    """A saved `promote --rev` plan names a pin; if the pin is moved before
    apply, what it names is not what was reviewed -- refuse."""
    from tether.errors import StalePlanError

    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    store.write(system, "main", {"v": 0})
    repo.commit("v0")
    repo.new(bookmark="work", eager=True)
    branch = repo.workspace.working_refs["db"]
    store.write(system, branch, {"v": 1})
    c1 = repo.commit("v1").vcs_commit
    pin = repo.objects["db"].pin
    assert c1 is not None and pin is not None
    store.write(system, branch, {"v": 2})
    repo.commit("v2")
    plan = repo.plan_promote(rev=c1)  # land v1 (by its pin) on main, not v2
    (write,) = [a for a in plan.actions if a.op == "fast-forward"]
    assert "pin" in write.params["source"]

    s_other = store.write(system, branch, {"v": 3})
    store.system(system).tags[pin.ref] = s_other  # moved after review
    with pytest.raises(StalePlanError, match="no longer names the reviewed state"):
        repo.apply_promote(plan)
    assert store.read(system, "main") == {"v": 0}


def test_promote_rev_of_a_removed_object_keeps_its_safety_checks(
    vcs_root: Path,
) -> None:
    """A `--rev` plan can name an object that has since been removed from the
    working tree. Its kind and locator travel in the plan, so the base-head
    check and the scope-sibling refusal must not depend on the object still
    being registered."""
    from tether.errors import StalePlanError

    repo = Repo.init(vcs_root)
    store = default_store()
    system = _mem_object(repo, "left")
    repo.add("right", "memory", {"system": system, "branch": "main"})
    store.write(system, "main", {"v": 0})
    repo.commit("v0")
    repo.new(bookmark="work", eager=True)
    branch = repo.workspace.working_refs["left"]
    store.write(system, branch, {"v": 1})
    c1 = repo.commit("v1").vcs_commit
    assert c1 is not None

    # `left` is gone from the working tree; c1 still has it.
    repo.remove("left")
    repo.commit("drop left")

    # Naming the removed key alone by revision: its base branch is still
    # `right`'s, so the subset is refused -- not silently allowed because the
    # key is no longer registered.
    plan = repo.plan_promote(["left"], rev=c1)
    assert [a.op for a in plan.actions if a.key == "left"] == ["refuse"]
    (refuse,) = [a for a in plan.actions if a.op == "refuse"]
    assert "also right's base branch" in refuse.detail

    # The whole bookmark by revision: one write for the shared base, with
    # `right` sharing it even though `left` is not a current object.
    plan = repo.plan_promote(rev=c1)
    assert sorted(a.op for a in plan.actions) == ["fast-forward", "share"]

    # And the base-head check holds for the removed key: main moves after the
    # plan, and the apply refuses rather than merging onto the moved base.
    store.write(system, "main", {"v": 5})
    with pytest.raises(StalePlanError, match="base branch moved"):
        repo.apply_promote(plan)
    assert store.read(system, "main") == {"v": 5}


def test_new_refuses_when_branches_cannot_be_listed(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not knowing whether the bookmark's branch exists is not the same as it
    being absent: a fork would reset it. `new` refuses, and a fork planned as
    fresh is re-checked for absence at apply."""
    from tether.errors import StalePlanError

    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    repo.new(bookmark="work", eager=True)
    branch = repo.workspace.working_refs["db"]
    store.write(system, branch, {"mine": 1})
    repo.new("main")  # leave the bookmark; its branch keeps the writes

    backend = repo.backend_for("memory")
    real_list = backend.list_working_refs

    def blind(locator: Locator) -> list[str]:
        raise BackendError("listing unavailable", kind="memory")

    monkeypatch.setattr(backend, "list_working_refs", blind)
    plan = repo.plan_new("work", eager=True)
    (refuse,) = [a for a in plan.actions if a.op == "refuse"]
    assert "could not list branches" in refuse.detail
    with pytest.raises(TetherError, match="could not list branches"):
        repo.new("work", eager=True)
    monkeypatch.setattr(backend, "list_working_refs", real_list)
    assert store.read(system, branch) == {"mine": 1}  # untouched

    # A plan made while the branch was absent must not reset one that
    # appeared since.
    repo.vcs.bookmark_delete("work")
    repo.gc(dry_run=False, prune_bookmarks=True, force_prune=True)
    assert branch not in store.system(system).branches
    plan = repo.plan_new(bookmark="work", eager=True)
    (fork,) = [a for a in plan.actions if a.op == "fork"]
    assert not fork.params.get("existing")
    store.system(system).branches[branch] = store.write(system, "main", {"late": 1})
    with pytest.raises(StalePlanError, match="exists since the plan was made"):
        repo.apply_new(plan)
    assert store.read(system, branch) == {"late": 1}


def test_lazy_fork_never_resets_a_branch_new_did_not_see(vcs_root: Path) -> None:
    """A deferred fork resets an existing branch only onto the head `new`
    reviewed; writes that landed on it since are refused at open."""
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    baseline = repo.commit("baseline").vcs_commit
    assert baseline is not None
    repo.new(bookmark="work", eager=True)
    branch = repo.workspace.working_refs["db"]
    store.write(system, branch, {"v": 2})
    repo.commit("v2")
    # Move the bookmark back by hand: `new` finds the branch off the pin and
    # plans a deferred reset of the head it sees.
    repo.vcs.bookmark_set("work", baseline)
    repo.new("work")
    assert repo.workspace.pending_forks == {"db": branch}
    assert "db" in repo.workspace.pending_resets

    store.write(system, branch, {"v": 3})  # someone else, after `new`
    with pytest.raises(StaleWorkingCopyError, match="did not see"):
        repo.open("db", read_only=False)
    assert store.read(system, branch) == {"v": 3}  # untouched

    # `new` again sees the new head: unpinned writes, so it refuses without
    # --discard; with it, the open resets the branch onto the pin.
    with pytest.raises(TetherError, match="has writes since"):
        repo.new("work")
    repo.new("work", discard=True)
    handle = repo.open("db", read_only=False)
    assert isinstance(handle, MemoryHandle)
    assert handle.read() == store.read(system, "main")


def test_two_keys_on_one_system_share_the_bookmarks_branch(vcs_root: Path) -> None:
    """Two objects in one branch space (two databases of a Neon project, here
    two keys on one memory system) get *one* branch per bookmark: the second
    writable open joins the branch the first created instead of resetting it,
    and a later `new` plans one fork and one share."""
    repo = Repo.init(vcs_root)
    store = default_store()
    system = _mem_object(repo, "left")
    repo.add("right", "memory", {"system": system, "branch": "main"})
    store.write(system, "main", {"v": 0})
    repo.commit("baseline")

    repo.new(bookmark="work")  # lazy
    plan_ops = [a.op for a in repo.plan_new(keep=False).actions]
    assert sorted(plan_ops) == ["defer-fork", "share"]
    left = repo.open("left", read_only=False)
    assert isinstance(left, MemoryHandle)
    left.write({"v": 1})
    branch = repo.workspace.working_refs["left"]
    assert repo.workspace.working_refs.get("right") == branch  # adopted with it
    right = repo.open("right", read_only=False)
    assert isinstance(right, MemoryHandle) and right.ref == branch
    assert right.read() == {"v": 1}  # the shared branch, not a reset copy
    assert store.read(system, branch) == {"v": 1}

    res = repo.commit("both")
    assert res.pinned["left"] is not None and res.pinned["right"] is not None
    plan = repo.plan_new(eager=True)
    assert sorted(a.op for a in plan.actions) == ["reuse", "share"]


def test_restore_and_promote_are_closed_over_the_branch_scope(vcs_root: Path) -> None:
    """Two keys writing through one branch: restoring one alone is refused (it
    would move the other's branch too); restoring both resets the branch once;
    promoting the bookmark fast-forwards the shared branch once."""
    repo = Repo.init(vcs_root)
    store = default_store()
    system = _mem_object(repo, "left")
    repo.add("right", "memory", {"system": system, "branch": "main"})
    store.write(system, "main", {"v": 0})
    c0 = repo.commit("v0").vcs_commit
    assert c0 is not None
    repo.new(bookmark="work", eager=True)
    branch = repo.workspace.working_refs["left"]
    assert repo.workspace.working_refs["right"] == branch
    store.write(system, branch, {"v": 1})
    repo.commit("v1")

    plan = repo.plan_restore(["left"], c0)
    (refuse,) = [a for a in plan.actions if a.op == "refuse"]
    assert "also right's working branch" in refuse.detail
    with pytest.raises(TetherError, match="also right's working branch"):
        repo.restore(["left"], c0)
    assert store.read(system, branch) == {"v": 1}  # untouched

    plan = repo.plan_restore(["left", "right"], c0)
    assert sorted(a.op for a in plan.actions) == ["fork", "share"]
    done = repo.apply_restore(plan)
    assert done == {"left": branch, "right": branch}
    assert store.read(system, branch) == {"v": 0}
    assert repo.workspace.fork_points["right"] == repo.workspace.fork_points["left"]

    # Promote: naming only one of the two is refused -- the base branch it
    # moves is the other's too, whatever the source (a working ref, or a pin
    # by revision). The bookmark as a whole fast-forwards the branch once and
    # the sibling shares the result.
    store.write(system, branch, {"v": 2})
    c2 = repo.commit("v2").vcs_commit
    assert c2 is not None
    plan = repo.plan_promote(["left"])
    (refuse,) = [a for a in plan.actions if a.op == "refuse"]
    assert "also right's base branch" in refuse.detail
    plan = repo.plan_promote(["left"], rev=c2)
    assert [a.op for a in plan.actions if a.key == "left"] == ["refuse"]
    assert store.read(system, "main") == {"v": 0}
    plan = repo.plan_promote()
    assert sorted(a.op for a in plan.actions) == ["fast-forward", "share"]
    report = repo.apply_promote(plan)
    assert set(report.fast_forwarded) == {"left", "right"}
    assert store.read(system, "main") == {"v": 2}


def test_scope_members_must_pin_the_same_branch_state(vcs_root: Path) -> None:
    """A branch is at one point: two keys that share it but pin different
    states cannot both be forked from -- `new` refuses and says so."""
    repo = Repo.init(vcs_root)
    store = default_store()
    system = _mem_object(repo, "left")
    s0 = store.system(system).branches["main"]
    store.write(system, "main", {"v": 1})
    repo.add("right", "memory", {"system": system, "branch": "main", "at": s0})
    repo.commit("left at v1, right at s0")
    with pytest.raises(TetherError, match="a branch is at one point"):
        repo.new(bookmark="work", eager=True)


def test_gc_collects_references_over_the_whole_pin_namespace(vcs_root: Path) -> None:
    """When one native namespace holds several objects' pins (a Neon project,
    an Iceberg table), gc must subtract every object's references from what it
    lists there, or one object's sweep releases the others' pins."""
    from tether.backends.base import register_backend
    from tether.backends.memory import MemoryBackend

    class TableBackend(MemoryBackend):
        """A memory system holding several 'tables': identity is per table,
        pins live (and are listed) per system."""

        kind = "memtable"

        def identity(self, locator: Locator) -> Locator:
            return {"system": locator["system"], "table": locator["table"]}

        def branch_scope(self, locator: Locator) -> str:
            return str(locator["system"])

        def ref_namespace(self, locator: Locator) -> str:
            return str(locator["system"])

    register_backend("memtable", lambda config: TableBackend(store=default_store()))
    repo = Repo.init(vcs_root)
    store = default_store()
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(system)
    store.write(system, "main", {"rows": 1})
    repo.add("t1", "memtable", {"system": system, "branch": "main", "table": "t1"})
    repo.add("t2", "memtable", {"system": system, "branch": "main", "table": "t2"})
    res = repo.commit("both tables")
    p1, p2 = res.pinned["t1"], res.pinned["t2"]
    assert p1 is not None and p2 is not None and p1.id != p2.id
    plan = repo.plan_gc()
    assert not [a for a in plan.actions if a.op == "unpin"], plan.actions
    repo.gc(dry_run=False)
    assert {p1.ref, p2.ref} <= set(store.system(system).tags)


def test_commit_compensates_manifests_and_journals_the_attempt(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after the pins -- here the VCS commit -- rolls back the pins
    this commit created *and* the manifests it wrote, and the journal keeps a
    completed entry saying the commit failed and was rolled back."""
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    store.write(system, "main", {"v": 1})
    repo.commit("v1")
    before = repo.objects["db"].to_toml()
    store.write(system, "main", {"v": 2})

    def boom(*args: object, **kwargs: object) -> str:
        raise VcsError("disk full")

    monkeypatch.setattr(repo.vcs, "commit", boom)
    with pytest.raises(VcsError, match="disk full"):
        repo.commit("v2")
    # Working tree as before; the v2 pin is gone; the v1 pin is untouched.
    assert Repo.find(vcs_root).objects["db"].to_toml() == before
    tags = store.system(system).tags
    assert len(tags) == 1 and repo.objects["db"].pin is not None
    assert repo.objects["db"].pin.ref in tags
    newest = repo.ops()[0]
    assert newest.command == "commit" and not newest.incomplete
    assert newest.result["rolled_back"] and "disk full" in newest.result["failed"]


def test_a_planned_object_removed_before_apply_is_a_stale_plan(
    vcs_root: Path,
) -> None:
    """Even without verification, a key that was registered at plan time and
    is gone at apply is a stale plan -- not a KeyError from the loop -- and
    pins the commit made before reaching it are rolled back."""
    from tether.errors import StalePlanError

    repo = Repo.init(vcs_root)
    _mem_object(repo, "a")
    _mem_object(repo, "b")
    plan = repo.plan_commit("both")
    repo.remove("b")
    with pytest.raises(StalePlanError, match="'b' is no longer registered"):
        repo.apply_commit(plan, verify=False)
    assert repo.objects["a"].pin is None  # rolled back
    assert repo.ops()[0].result.get("rolled_back")


def test_commit_keeps_its_pins_when_the_vcs_commit_landed_before_the_error(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An adapter that writes the commit and then fails (say, on moving the
    bookmark) must not trigger compensation: history now names the new pin, so
    releasing it would leave a commit whose pin is missing."""
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    store.write(system, "main", {"v": 1})
    repo.commit("v1")
    store.write(system, "main", {"v": 2})

    real_commit = repo.vcs.commit

    def commit_then_raise(
        paths: list[str], message: str, *, advance: str | None = None
    ) -> str:
        real_commit(paths, message, advance=advance)
        raise VcsError("bookmark could not be moved")

    monkeypatch.setattr(repo.vcs, "commit", commit_then_raise)
    with pytest.raises(VcsError, match="bookmark could not be moved"):
        repo.commit("v2")
    fresh = Repo.find(vcs_root)
    # The commit is history, the manifest records v2, and its pin is alive.
    m = fresh.objects["db"]
    assert m.pin is not None and m.pin.ref in store.system(system).tags
    assert len(store.system(system).tags) == 2
    assert all(r.ok for r in fresh.verify().values())
    newest = fresh.ops()[0]
    assert newest.command == "commit" and not newest.incomplete
    assert (
        newest.result["vcs_commit"]
        and "bookmark" in newest.result["failed_after_commit"]
    )
    assert not fresh.is_stale()


def test_new_killed_during_its_forks_leaves_vcs_and_workspace_in_agreement(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The VCS moves to the bookmark before the forks run. A kill in the
    fan-out must not leave `workspace.toml` on the old bookmark: it is written
    -- bookmark set, every fork pending -- before the first store write, so
    what remains is a lazy `new` that the next open or `new` completes."""
    repo = Repo.init(vcs_root)
    system_a = _mem_object(repo, "a")
    system_b = _mem_object(repo, "b")
    store = default_store()
    repo.commit("baseline")
    backend = repo.backend_for("memory")
    real_fork = backend.fork

    def dies_on_b(locator: Locator, source: Pin | State, name: str) -> str:
        if locator["system"] == system_b:
            raise SystemExit(137)
        return real_fork(locator, source, name)

    monkeypatch.setattr(backend, "fork", dies_on_b)
    with pytest.raises(SystemExit):
        repo.new(bookmark="work", eager=True)
    monkeypatch.setattr(backend, "fork", real_fork)

    fresh = Repo.find(vcs_root)
    assert fresh.workspace.bookmark == "work"  # agrees with the VCS
    assert fresh.vcs.bookmarks()["work"] and not fresh.is_stale()
    branch = fresh.workspace.pending_forks["b"]
    assert fresh.workspace.pending_forks == {"a": branch, "b": branch}
    # `a`'s branch was created before the kill; the open finds it at the pin.
    assert branch in store.system(system_a).branches
    ha = fresh.open("a", read_only=False)
    hb = fresh.open("b", read_only=False)
    assert isinstance(ha, MemoryHandle) and ha.ref == branch
    assert isinstance(hb, MemoryHandle) and hb.ref == branch
    assert fresh.workspace.working_refs == {"a": branch, "b": branch}
    assert not fresh.workspace.pending_forks


def test_restore_killed_between_resets_describes_what_it_reset(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two objects on two systems, restored together; the process dies on the
    second reset. The first branch was reset *and* the workspace says so (fork
    point, base state), so nothing looks like foreign writes afterwards."""
    repo = Repo.init(vcs_root)
    system_a = _mem_object(repo, "a")
    system_b = _mem_object(repo, "b")
    store = default_store()
    store.write(system_a, "main", {"v": 0})
    store.write(system_b, "main", {"v": 0})
    c0 = repo.commit("v0").vcs_commit
    assert c0 is not None
    repo.new(bookmark="work", eager=True)
    refs = dict(repo.workspace.working_refs)
    store.write(system_a, refs["a"], {"v": 1})
    store.write(system_b, refs["b"], {"v": 1})
    repo.commit("v1")

    backend = repo.backend_for("memory")
    real_fork = backend.fork

    def dies_on_b(locator: Locator, source: Pin | State, name: str) -> str:
        if locator["system"] == system_b:
            raise SystemExit(137)
        return real_fork(locator, source, name)

    before = dict(repo.workspace.fork_points)
    monkeypatch.setattr(backend, "fork", dies_on_b)
    with pytest.raises(SystemExit):
        repo.restore(["a", "b"], c0)
    monkeypatch.setattr(backend, "fork", real_fork)

    fresh = Repo.find(vcs_root)
    assert store.read(system_a, refs["a"]) == {"v": 0}  # reset happened...
    assert store.read(system_b, refs["b"]) == {"v": 1}  # ...this one did not
    # ...and the workspace describes exactly that: a's fork point moved to the
    # restored state, b's is what it was.
    assert fresh.workspace.fork_points["a"] == {
        "snapshot_id": store.system(system_a).branches[refs["a"]]
    }
    assert fresh.workspace.fork_points["b"] == before["b"]
    assert not fresh.is_stale()
    (incomplete,) = fresh.incomplete_ops()
    assert [(r["action"], r["key"]) for r in incomplete.progress] == [("fork", "a")]
    # Re-running finishes the job.
    fresh.restore(["a", "b"], c0)
    assert store.read(system_b, refs["b"]) == {"v": 0}


def test_an_interrupted_operation_leaves_a_started_journal_entry(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The journal entry is written before the first side effect. A process
    that dies mid-way (simulated: the backend raises SystemExit) leaves it
    `started`; `ops` shows it, `undo` refuses it, `repair` names it."""
    repo = Repo.init(vcs_root)
    _mem_object(repo, "a")
    _mem_object(repo, "b")
    repo.commit("baseline")
    backend = repo.backend_for("memory")
    real_fork = backend.fork

    def dies(locator: Locator, source: Pin | State, name: str) -> str:
        if locator["system"] == repo.objects["b"].locator["system"]:
            raise SystemExit(137)  # what a kill looks like from inside
        return real_fork(locator, source, name)

    monkeypatch.setattr(backend, "fork", dies)
    with pytest.raises(SystemExit):
        repo.new(bookmark="work", eager=True)
    monkeypatch.setattr(backend, "fork", real_fork)

    fresh = Repo.find(vcs_root)
    (incomplete,) = fresh.incomplete_ops()
    assert incomplete.command == "new" and incomplete.plan is not None
    # The journal says which action had taken effect before the death.
    assert [(r["action"], r["key"]) for r in incomplete.progress] == [("fork", "a")]
    notes = fresh.plan_repair().notes
    assert any("done before it stopped: fork a" in n for n in notes)
    assert any("re-running the command finishes" in n for n in notes)
    assert fresh.ops()[0].id == incomplete.id and not fresh.ops()[0].undoable
    with pytest.raises(TetherError, match="never finished"):
        fresh.undo(incomplete.id)
    # A bare `undo` refuses too, rather than reach past the interrupted entry
    # to the baseline commit: what the `new` left is to be looked at first.
    with pytest.raises(TetherError, match="never finished"):
        fresh.undo()
    assert any(incomplete.id in n and "never finished" in n for n in notes)


def test_a_long_lived_repo_does_not_write_back_stale_workspace_state(
    vcs_root: Path,
) -> None:
    """A Repo constructed earlier (a notebook, a service) snapshots after
    another process moved the checkout to a bookmark: the snapshot cache must
    land in the workspace as it is *now*, not clobber it with the old one --
    and every writing command starts from the on-disk state."""
    stale = Repo.init(vcs_root)
    system = _mem_object(stale)
    store = default_store()
    stale.commit("baseline")
    assert stale.workspace.bookmark == "main"

    fresh = Repo.find(vcs_root)
    fresh.new(bookmark="work", eager=True)
    branch = fresh.workspace.working_refs["db"]

    store.write(system, branch, {"v": 1})
    stale.snapshot()  # in-memory view still says trunk
    now = Repo.find(vcs_root).workspace
    assert now.bookmark == "work" and now.working_refs == {"db": branch}
    # The snapshot was decided *after* the refresh: it read the work branch
    # (which has writes), not main, so a local status sees the modification.
    assert now.last_snapshot["db"] == {
        "snapshot_id": store.system(system).branches[branch]
    }
    (db,) = Repo.find(vcs_root).status(do_snapshot=False).objects
    assert db.changed
    # And the stale Repo learnt where the checkout is: its writable open goes
    # to the bookmark's branch, not the trunk.
    handle = stale.open("db", read_only=False)
    assert isinstance(handle, MemoryHandle) and handle.ref == branch
    assert stale.commit("from the stale repo").pinned["db"] is not None
    assert Repo.find(vcs_root).workspace.bookmark == "work"


def test_one_writer_per_checkout(vcs_root: Path) -> None:
    """A second Repo on the same checkout waits for the lock and gives up
    after `LOCK_TIMEOUT`; the lock is re-entrant within one Repo."""
    import fcntl

    from tether.manifest import LOCK_FILENAME, tether_path

    repo = Repo.init(vcs_root)
    _mem_object(repo)
    repo.LOCK_TIMEOUT = 0.3
    lock = tether_path(vcs_root) / LOCK_FILENAME
    with (
        repo._writer_lock(),
        repo._writer_lock(),
    ):  # re-entrant
        assert lock.exists()
        other = Repo.find(vcs_root)
        other.LOCK_TIMEOUT = 0.3
        started = time.monotonic()
        with pytest.raises(TetherError, match="another tether command is writing"):
            other.commit("blocked")
        assert time.monotonic() - started >= 0.3  # it waited, then gave up
        # Manifest-only writers wait for the lock too.
        with pytest.raises(TetherError, match="another tether command is writing"):
            other.add("late", "memory", {"system": "x", "branch": "main"})
    # Released: the other Repo can write now.
    other.commit("unblocked")
    # And a foreign holder of the file lock blocks us the same way.
    with lock.open("a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(TetherError, match="another tether command is writing"):
            repo.new(bookmark="work")
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    repo.new(bookmark="work")


def test_relative_local_paths_are_pinned_down_at_add(
    vcs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """`add` from a subdirectory with a relative path stores the absolute path
    it meant, so every later command (and every clone) reads the same files
    regardless of its working directory."""
    sub = vcs_root / "analysis" / "notebooks"
    sub.mkdir(parents=True)
    data = vcs_root / "analysis" / "data"
    data.mkdir()
    (data / "a.bin").write_bytes(b"a")
    repo = Repo.init(vcs_root)
    monkeypatch.chdir(sub)
    repo.add("raw", "file", {"uri": "../data"})
    stored = repo.objects["raw"].locator["uri"]
    assert Path(stored).is_absolute() and Path(stored) == data.resolve()
    # URLs and absolute paths pass through untouched.
    repo.add("remote", "file", {"uri": "s3://bucket/prefix/"})
    assert repo.objects["remote"].locator["uri"] == "s3://bucket/prefix/"
    repo.remove("remote")
    # The CLI's positional locator is `uri` for every kind, git included (a
    # git repository outside the checkout: one inside it came with the clone).
    code = tmp_path_factory.mktemp("outside")
    repo.add("code", "git", {"uri": os.path.relpath(code, sub)})
    assert repo.objects["code"].locator["uri"] == str(code.resolve())
    repo.remove("code")
    monkeypatch.chdir(vcs_root)
    assert "raw" in repo.commit("from the root").unrecoverable  # recorded from here


def test_pin_policy_change_takes_effect_at_the_next_commit(vcs_root: Path) -> None:
    """`set --pin record` on a pinned object: the next commit records the same
    state without a pin (gc then releases the tag); `set --pin native` brings a
    pin back; and `diff` reports a policy-only change as a change."""
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    c1 = repo.commit("pinned").vcs_commit
    pin = repo.objects["db"].pin
    assert c1 is not None and pin is not None

    repo.set_policy(["db"], pin="record")
    plan = repo.plan_commit("record instead")
    assert [a.op for a in plan.actions if a.key == "db"] == ["record"]
    assert any("pin released" in n for n in plan.notes)
    c2 = repo.commit("record instead").vcs_commit
    assert c2 is not None and repo.objects["db"].pin is None
    (entry,) = [e for e in repo.diff(c1, c2) if e.key == "db"]
    assert entry.change == "changed" and set(entry.why) == {"pin", "policy"}
    # The tag is now unreferenced by the working tree, but c1 still names it.
    assert not [a for a in repo.plan_gc().actions if a.op == "unpin"]
    assert pin.ref in store.system(system).tags

    repo.set_policy(["db"], pin="native")
    plan = repo.plan_commit("native again")
    assert [a.op for a in plan.actions if a.key == "db"] == ["pin"]
    repo.commit("native again")
    assert repo.objects["db"].pin == pin  # same state, same content-addressed id

    # A locator-only change (a field the identity ignores) is a change too.
    (entry,) = [e for e in repo.diff(c1) if e.key == "db"]
    assert entry.change == "unchanged"


def test_diff_one_revision_compares_it_with_the_working_tree(vcs_root: Path) -> None:
    """`diff REV` is REV -> working tree, not REV -> nothing."""
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    r1 = repo.commit("baseline")
    assert r1.vcs_commit is not None
    assert all(e.change == "unchanged" for e in repo.diff(r1.vcs_commit))

    default_store().write(system, "main", {"rows": 2})
    repo.pull(message="moved")  # the working tree's manifest now differs
    entries = {e.key: e for e in repo.diff(r1.vcs_commit)}
    assert entries["db"].change == "changed"
    assert entries["db"].a_pin is not None and entries["db"].b_pin is not None


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


def test_commit_keeps_the_working_branch(vcs_root: Path) -> None:
    """`commit` is not `jj commit`: it does not start a new branch. Writes keep
    landing on the same working branch, and `new` afterwards reuses it."""
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    repo.commit("baseline")
    repo.new(bookmark="work")
    handle = repo.open("db")
    assert isinstance(handle, MemoryHandle)
    branch = handle.ref
    default_store().write(system, branch, {"x": 1})
    repo.commit("update")
    assert repo.workspace.working_refs["db"] == branch
    default_store().write(system, branch, {"x": 2})
    repo.commit("again")  # a second breadcrumb on the same branch
    assert repo.workspace.working_refs["db"] == branch
    plan = repo.plan_new()
    assert [a.op for a in plan.actions if a.key == "db"] == ["reuse"]


def test_history_and_detached_base(vcs_root: Path) -> None:
    from tether.errors import CapabilityError

    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    s1 = store.write(system, "main", {"v": 1})
    s2 = store.write(system, "main", {"v": 2})

    log = repo.history("db")
    assert [e.id for e in log][:3] == [s2, s1, f"{system}:s0"]
    assert log[0].refs == ["main"]
    assert repo.history("db", limit=1) == log[:1]
    assert repo.history_for("memory", {"system": system})[0].id == s2

    # A detached base: fingerprint, commit, and fork all use the chosen state.
    repo.remove("db")
    repo.add("db", "memory", {"system": system, "branch": "main", "at": s1})
    assert repo.snapshot()["db"] == {"snapshot_id": s1}
    ro = repo.open("db", read_only=True)
    assert isinstance(ro, MemoryHandle) and ro.read() == {"v": 1}
    res = repo.commit("adopt at s1")
    pin = res.pinned["db"]
    assert pin is not None and store.system(system).tags[pin.ref] == s1
    repo.new(bookmark="work", eager=True)
    wref = repo.workspace.working_refs["db"]
    assert store.system(system).branches[wref] == s1
    assert store.system(system).branches["main"] == s2  # untouched

    # Objects whose backend lacks HISTORY refuse cleanly.
    (vcs_root / "f.bin").write_bytes(b"x")
    repo.add("f", "file", {"uri": str(vcs_root / "f.bin")})
    with pytest.raises(CapabilityError):
        repo.history("f")
    with pytest.raises(ConfigError):
        repo.history("nope")


def test_pinless_record_policy_forks_from_state(vcs_root: Path) -> None:
    """`pin = "record"` on a Forkable backend: no native ref, fork from state."""
    repo = Repo.init(vcs_root)
    store = default_store()
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(system)
    s1 = store.write(system, "main", {"v": 1})
    repo.add(
        "db",
        "memory",
        {"system": system, "branch": "main"},
        policy=Policy(pin="record"),
    )

    plan = repo.plan_commit("baseline")
    (action,) = [a for a in plan.actions if a.key == "db"]
    assert action.op == "record" and action.params["recoverable"] is True
    assert "pin=record" in action.detail

    res = repo.commit("baseline")
    assert res.pinned["db"] is None  # recorded, not pinned
    assert store.system(system).tags == {}  # no native ref was created
    assert repo.objects["db"].state == {"snapshot_id": s1}
    assert repo.objects["db"].recoverable

    new_plan = repo.plan_new(bookmark="work")
    (fork,) = [a for a in new_plan.actions if a.op == "fork"]
    assert fork.params == {"state": {"snapshot_id": s1}}
    repo.new(bookmark="work", eager=True)
    wref = repo.workspace.working_refs["db"]
    assert store.system(system).branches[wref] == s1

    # Writes on the fork are isolated; the recorded state opens at the old rev.
    store.write(system, wref, {"v": 2})
    assert store.read(system, "main") == {"v": 1}
    assert res.vcs_commit is not None
    ro = repo.open("db", rev=res.vcs_commit)
    assert isinstance(ro, MemoryHandle) and ro.read() == {"v": 1}
    assert repo.verify()["db"].status.value in ("ok", "unknown")


def test_commit_plan_roundtrip_and_stale_detection(vcs_root: Path) -> None:
    from tether.errors import StalePlanError
    from tether.plan import Plan

    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    store.write(system, "main", {"v": 1})

    plan = repo.plan_commit("baseline")
    assert plan.command == "commit"
    assert [a.op for a in plan.actions] == ["pin", "vcs-commit"]
    assert store.system(system).tags == {}  # planning wrote nothing
    assert any("db" in line and "pin" in line for line in plan.render())

    # Serialize -> deserialize -> apply, exactly like `--plan` / `--from-plan`.
    restored = Plan.from_json(plan.to_json())
    assert restored.to_dict() == plan.to_dict()
    res = repo.apply_commit(restored)
    pin = res.pinned["db"]
    assert pin is not None and pin.id == plan.actions[0].params["pin_id"]
    assert res.vcs_commit is not None

    # A plan computed before the object moved must be refused.
    stale = repo.plan_commit("next")
    assert stale.is_empty  # unchanged since the commit
    s2 = store.write(system, "main", {"v": 2})
    stale = repo.plan_commit("next", fetched={"db": {"snapshot_id": s2}})
    store.write(system, "main", {"v": 3})
    with pytest.raises(StalePlanError):
        repo.apply_commit(stale)
    assert len(store.system(system).tags) == 1  # nothing extra pinned

    # And one computed against a different manifest set.
    fresh = repo.plan_commit("next")
    repo.add("other", "memory", {"system": _mem_object(repo, "tmp"), "branch": "main"})
    with pytest.raises(StalePlanError):
        repo.apply_commit(fresh)

    # Wrong plan kind is a config error.
    with pytest.raises(ConfigError):
        repo.apply_new(fresh)


def test_new_plan_and_apply(vcs_root: Path) -> None:
    from tether.plan import Plan

    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    tracked = f"sys-{uuid.uuid4().hex[:8]}"
    default_store().system(tracked)
    repo.add(
        "tracked",
        "memory",
        {"system": tracked, "branch": "main"},
    )
    plan = repo.plan_new(bookmark="work")
    assert plan.is_empty  # nothing committed yet
    assert any("nothing committed yet" in n for n in plan.notes)

    res = repo.commit("baseline")
    plan = repo.plan_new(bookmark="work")
    ops = {a.key: a.op for a in plan.actions}
    assert ops == {"db": "defer-fork", "tracked": "defer-fork"}
    assert plan.is_empty  # nothing is written by a lazy new
    assert plan.context["bookmark"] == "work" and plan.context["create"]
    assert (
        "first writable open" in next(a for a in plan.actions if a.key == "db").detail
    )
    assert not any(
        b.startswith("tether.ws.") for b in default_store().system(system).branches
    )

    repo.apply_new(Plan.from_json(plan.to_json()))
    assert repo.workspace.bookmark == "work" and "work" in repo.vcs.bookmarks()
    assert (
        repo.workspace.pending_forks["db"] == f"tether.ws.{repo.config.dataset_id}.work"
    )
    assert not any(
        b.startswith("tether.ws.") for b in default_store().system(system).branches
    )
    plan_eager = repo.plan_new(eager=True)
    assert {a.key: a.op for a in plan_eager.actions} == {
        "db": "fork",
        "tracked": "fork",
    }
    assert not plan_eager.is_empty
    repo.apply_new(Plan.from_json(plan_eager.to_json()))
    assert repo.workspace.working_refs["db"].startswith("tether.ws.")
    assert not repo.workspace.pending_forks

    # Planning against a revision reads the manifests there without moving.
    assert res.vcs_commit is not None
    plan_at = repo.plan_new(res.vcs_commit)
    assert plan_at.context["rev"] == res.vcs_commit
    # Both `main` and `work` sit on that commit; the one this workspace is on
    # wins, and its branches already sit at the pins.
    assert plan_at.context["bookmark"] == "work"
    assert {a.key: a.op for a in plan_at.actions} == {"db": "reuse", "tracked": "reuse"}
    repo.new("main")
    assert repo.on_trunk() and repo.workspace.working_refs["db"] == "main"


def test_gc_prunes_stray_branches_only_when_nothing_is_lost(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    s1 = store.write(system, "main", {"v": 1})
    repo.commit("baseline")  # pins s1
    s2 = store.write(system, "main", {"v": 2})
    repo.commit("update")  # pins s2; main head is s2
    repo.new(bookmark="work", eager=True)
    mine = repo.workspace.working_refs["db"]
    branches = store.system(system).branches
    s3 = store.write(system, "main", {"v": 3})  # main moved on; s3 is not pinned

    # Stray branches of bookmarks that are gone, in every situation the rule
    # covers -- plus a legacy per-workspace branch from before bookmarks.
    ds = repo.config.dataset_id
    branches[f"tether.ws.{ds}.old-a"] = s3  # equals base head: safe
    branches[f"tether.ws.{ds}.old-b"] = s1  # pinned by the first commit: safe
    branches[f"tether.ws.{ds}.old-c"] = s2
    store.write(system, f"tether.ws.{ds}.old-c", {"v": 99})  # unpinned: keep
    branches[f"tether.ws.{ds}.elsewhere"] = s2  # a bookmark on another machine
    branches[f"tether.ws.{ds}.aaaa0001.db-9f2e1c"] = s1  # legacy, dead workspace
    branches["tether.ws.ffffffff.old-a"] = s3  # another dataset's: not ours
    branches["feature-x"] = s2  # not a tether branch: never considered

    # Default gc never touches branches.
    plan = repo.plan_gc()
    assert not [a for a in plan.actions if a.op in ("delete-branch", "keep-branch")]

    plan = repo.plan_gc(prune_bookmarks=True, keep_bookmarks={"elsewhere"})
    by_ref = {a.target: a for a in plan.actions if a.op.endswith("-branch")}
    assert by_ref[f"tether.ws.{ds}.old-a"].op == "delete-branch"
    assert "equals the base branch" in by_ref[f"tether.ws.{ds}.old-a"].detail
    assert "bookmark old-a (gone)" in by_ref[f"tether.ws.{ds}.old-a"].detail
    assert by_ref[f"tether.ws.{ds}.old-b"].op == "delete-branch"
    assert "head is pinned" in by_ref[f"tether.ws.{ds}.old-b"].detail
    assert by_ref[f"tether.ws.{ds}.old-c"].op == "keep-branch"
    assert "unpinned writes" in by_ref[f"tether.ws.{ds}.old-c"].detail
    legacy = by_ref[f"tether.ws.{ds}.aaaa0001.db-9f2e1c"]
    assert legacy.op == "delete-branch" and "legacy workspace aaaa0001" in legacy.detail
    assert f"tether.ws.{ds}.elsewhere" not in by_ref and "feature-x" not in by_ref
    assert mine not in by_ref  # in use by this workspace
    assert len(plan.writes) == 3  # keep-branch is not a write

    report = repo.gc(dry_run=False, prune_bookmarks=True, keep_bookmarks={"elsewhere"})
    assert f"tether.ws.{ds}.old-a" not in branches
    assert f"tether.ws.{ds}.old-b" not in branches
    assert f"tether.ws.{ds}.aaaa0001.db-9f2e1c" not in branches
    assert f"tether.ws.{ds}.old-c" in branches  # kept: has data
    assert f"tether.ws.{ds}.elsewhere" in branches and "feature-x" in branches
    assert "tether.ws.ffffffff.old-a" in branches  # foreign: untouched
    assert mine in branches
    assert report.kept_working_refs == {"db": [f"tether.ws.{ds}.old-c"]}

    # --force-prune deletes the one with data too, and says so.
    plan = repo.plan_gc(prune_bookmarks=True, force_prune=True)
    (forced,) = [a for a in plan.actions if a.target == f"tether.ws.{ds}.old-c"]
    assert forced.op == "delete-branch" and forced.params["forced"] is True
    assert "FORCED" in forced.detail
    repo.apply_gc(plan)
    assert f"tether.ws.{ds}.old-c" not in branches
    assert f"tether.ws.{ds}.elsewhere" not in branches  # no keep list this time
    assert "tether.ws.ffffffff.old-a" in branches  # even --force-prune
    assert mine in branches


def test_gc_keeps_pinless_recorded_states_and_storage_branches(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tether.backends.base import Capability

    repo = Repo.init(vcs_root)
    store = default_store()
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(system)
    s1 = store.write(system, "main", {"v": 1})
    repo.add(
        "db",
        "memory",
        {"system": system, "branch": "main"},
        policy=Policy(pin="record"),
    )
    repo.commit("baseline")  # records s1, no tag
    store.write(system, "main", {"v": 2})
    repo.commit("update")  # records s2; main head is s2
    branches = store.system(system).branches
    ds = repo.config.dataset_id
    branches[f"tether.ws.{ds}.old"] = s1  # the only thing keeping s1 alive

    plan = repo.plan_gc(prune_bookmarks=True)
    (a,) = [x for x in plan.actions if x.target == f"tether.ws.{ds}.old"]
    assert a.op == "keep-branch" and "pin-less recorded state" in a.detail

    # A backend whose branches *are* the storage is never pruned without force.
    backend = repo.backend_for("memory")
    monkeypatch.setattr(
        backend, "capabilities", backend.capabilities | Capability.BRANCH_IS_STORAGE
    )
    branches[f"tether.ws.{ds}.old2"] = branches["main"]  # would otherwise be safe
    plan = repo.plan_gc(prune_bookmarks=True)
    ops = {x.target: x for x in plan.actions if x.op.endswith("-branch")}
    assert ops[f"tether.ws.{ds}.old2"].op == "keep-branch"
    assert "branch is storage" in ops[f"tether.ws.{ds}.old2"].detail
    plan = repo.plan_gc(prune_bookmarks=True, force_prune=True)
    assert all(
        x.op == "delete-branch" for x in plan.actions if x.op.endswith("-branch")
    )


def test_gc_forgets_removed_objects_refs_without_deleting(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    repo.new(bookmark="work", eager=True)
    mine = repo.workspace.working_refs["db"]

    repo.remove("db")
    assert repo.workspace.working_refs["db"] == mine  # kept for gc to find
    plan = repo.plan_gc()
    (forget,) = [a for a in plan.actions if a.op == "forget-working-ref"]
    assert forget.target == mine
    assert not [a for a in plan.actions if a.op == "delete-branch"]
    report = repo.apply_gc(plan)
    assert report.forgotten_working_refs == {"db": [mine]}
    assert "db" not in repo.workspace.working_refs
    assert mine in store.system(system).branches  # the branch itself survives

    # Re-register the object: the stray branch becomes this bookmark's orphan
    # and --prune-bookmarks evaluates it like any other.
    repo.add("db", "memory", {"system": system, "branch": "main"})
    plan = repo.plan_gc(prune_bookmarks=True)
    (a,) = [x for x in plan.actions if x.target == mine]
    assert a.op == "delete-branch" and "no object uses it" in a.detail


def test_ref_for_pin_helper_used_in_gc(vcs_root: Path) -> None:
    # Guard against accidental prefix drift between pin() and gc().
    assert ref_for_pin("abc") == "tether.abc"
    _ = Pin("abc", ref_for_pin("abc"))


def test_stale_new_plan_does_not_move_the_working_copy(vcs_root: Path) -> None:
    from tether.errors import StalePlanError

    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    c1 = repo.commit("baseline").vcs_commit
    default_store().write(system, "main", {"v": 2})
    c2 = repo.commit("second").vcs_commit
    assert c1 and c2
    plan = repo.plan_new(c1)
    # The manifests at c1 change (someone rewrote history); the plan is stale.
    # `context` is informational; the precondition is what apply checks.
    (pre,) = [p for p in plan.preconditions if p.kind == "manifest_hash"]
    plan.preconditions[plan.preconditions.index(pre)] = dataclasses.replace(
        pre, expected="not-what-is-there"
    )
    here = repo.vcs.current_rev()
    with pytest.raises(StalePlanError):
        repo.apply_new(plan)
    assert repo.vcs.current_rev() == here  # refused before checking out c1


def test_verify_all_history_covers_recorded_states(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    store = default_store()
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(system)
    store.write(system, "main", {"v": 1})
    repo.add("db", "memory", {"system": system}, policy=Policy(pin="record"))
    (vcs_root / "f.bin").write_bytes(b"x")
    repo.add("f", "file", {"uri": str(vcs_root / "f.bin")})  # Observed: not recoverable
    res = repo.commit("baseline")
    assert res.pinned["db"] is None and "f" in res.unrecoverable

    reports = repo.verify(all_history=True, deep=True)
    labels = {label.split(":", 1)[1] for label in reports}
    assert "db" in labels  # recorded, pin-less state is a promise worth checking
    assert "f" not in labels  # Observed records are not recoverable; nothing to verify
    assert all(r.ok for r in reports.values())


def test_prune_keeps_live_workspaces_automatically(
    vcs_root: Path, tmp_path: Path
) -> None:
    import subprocess

    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    repo.new(bookmark="work", eager=True)
    mine = repo.workspace.working_refs["db"]

    # A second live checkout of the same repository, with its own tether workspace.
    other_root = tmp_path / "other-checkout"
    if repo.vcs.kind == "jj":
        subprocess.run(
            ["jj", "workspace", "add", str(other_root)],
            cwd=vcs_root,
            check=True,
            capture_output=True,
        )
    else:
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(other_root)],
            cwd=vcs_root,
            check=True,
            capture_output=True,
        )
    other = Repo.find(other_root)
    other.new(bookmark="theirs", eager=True)
    theirs = other.workspace.working_refs["db"]
    assert theirs != mine and theirs.endswith(".theirs")
    assert repo.live_workspace_ids() == {
        repo.workspace.workspace_id,
        other.workspace.workspace_id,
    }
    assert {"main", "work", "theirs"} <= repo.live_bookmarks()

    # A bookmark another live checkout works on is refused here unless shared.
    plan = repo.plan_new("theirs")
    (refuse,) = [a for a in plan.actions if a.op == "refuse"]
    assert "held by live workspace" in refuse.detail and "--shared" in refuse.detail
    with pytest.raises(TetherError, match="held by live workspace"):
        repo.new("theirs")
    assert repo.plan_new("theirs", shared=True).context["shared"] is True
    assert not [
        a for a in repo.plan_new("theirs", shared=True).actions if a.op == "refuse"
    ]

    # A branch of a bookmark that no longer exists, at the base head (safe).
    stray = f"tether.ws.{repo.config.dataset_id}.deadbeef"
    store.system(system).branches[stray] = store.system(system).branches["main"]
    plan = repo.plan_gc(prune_bookmarks=True)
    ops = {a.target: a.op for a in plan.actions if a.op.endswith("-branch")}
    assert ops == {stray: "delete-branch"}
    assert theirs not in ops and mine not in ops  # both bookmarks are live
    assert {"main", "work", "theirs"} <= set(plan.context["live_bookmarks"])
    assert any("keeping live bookmarks" in n for n in plan.notes)


def test_partial_fork_records_what_succeeded(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tether.errors import BackendError, MultiObjectError

    repo = Repo.init(vcs_root)
    _mem_object(repo, "ok")
    bad_system = _mem_object(repo, "bad")
    repo.commit("baseline")

    backend = repo.backend_for("memory")
    real_fork = backend.fork

    def flaky_fork(locator, source, name):
        if locator["system"] == bad_system:
            raise BackendError("quota exceeded", kind="memory")
        return real_fork(locator, source, name)

    monkeypatch.setattr(backend, "fork", flaky_fork)
    with pytest.raises(MultiObjectError, match="could not fork working refs for bad"):
        repo.new(bookmark="work", eager=True)

    # The branch that was created is known to the workspace, with its
    # bookkeeping; the one that failed stays *pending* -- the next `new` or
    # writable open creates it.
    ws = Repo.find(vcs_root).workspace
    assert ws.working_refs["ok"].startswith("tether.ws.")
    assert "ok" in ws.fork_points and "ok" in ws.base_states
    assert "bad" not in ws.working_refs and ws.pending_forks == {
        "bad": ws.working_refs["ok"]
    }
    store = default_store()
    assert (
        ws.working_refs["ok"]
        in store.system(repo.objects["ok"].locator["system"]).branches
    )

    # Once the cause is gone, a second `new` finishes the job.
    monkeypatch.setattr(backend, "fork", real_fork)
    repo.new(eager=True)
    assert set(repo.workspace.working_refs) == {"ok", "bad"}
    assert not repo.is_stale()


def test_two_datasets_sharing_a_store_do_not_gc_each_other(
    vcs_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Dataset A's gc must not see dataset B's pins or working branches."""
    import subprocess

    # B is its own repository. (Under jj `vcs_root` is the test's tmp_path, so
    # a subdirectory of it would be *inside* A's working copy and B's commits
    # would land in A's history, moving A's bookmarks -- a different test.)
    other_root = tmp_path_factory.mktemp("other")
    subprocess.run(["git", "init", "-q", str(other_root)], check=True)
    subprocess.run(
        ["git", "-C", str(other_root), "config", "user.email", "t@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(other_root), "config", "user.name", "t"], check=True
    )

    a = Repo.init(vcs_root)
    b = Repo.init(other_root)
    assert a.config.dataset_id != b.config.dataset_id
    store = default_store()
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(system)
    for repo in (a, b):
        repo.add("db", "memory", {"system": system, "branch": "main"})
    store.write(system, "main", {"v": 1})
    a.commit("a: v1")
    store.write(system, "main", {"v": 2})
    b.commit("b: v2")  # a different state, so a different pin
    b.new(bookmark="work", eager=True)
    b_ref = b.workspace.working_refs["db"]
    b_pin = b.objects["db"].pin
    a_pin = a.objects["db"].pin
    assert a_pin is not None and b_pin is not None and a_pin.id != b_pin.id
    assert a_pin.id.startswith(a.config.dataset_id + ".")
    assert b_pin.id.startswith(b.config.dataset_id + ".")
    assert b_ref.startswith(f"tether.ws.{b.config.dataset_id}.")

    # From A's point of view B's pin and branch are unreferenced strays -- and
    # off limits. Even --force-prune stays inside A's namespace.
    plan = a.plan_gc(prune_bookmarks=True, force_prune=True)
    targets = {x.target for x in plan.actions}
    assert not any(b_pin.id in t or t == b_ref for t in targets), targets
    assert any("of other datasets left alone" in n for n in plan.notes)
    a.gc(dry_run=False, prune_bookmarks=True, force_prune=True)
    backend = a.backend_for("memory")
    assert b_pin.id in backend.list_pins({"system": system})
    assert b_ref in store.system(system).branches
    # ... and the same state pinned by both datasets is two refs, one each.
    store.write(system, "main", {"v": 3})
    a.commit("a: v3")
    b.commit("b: v3")
    a3, b3 = a.objects["db"].pin, b.objects["db"].pin
    assert a3 is not None and b3 is not None and a3.id != b3.id
    assert {a3.id, b3.id} <= backend.list_pins({"system": system})


def test_op_log_records_every_store_write(vcs_root: Path) -> None:
    from tether.manifest import tether_path
    from tether.oplog import read_ops

    repo = Repo.init(vcs_root)
    assert repo.ops() == []
    ignore = (tether_path(vcs_root) / ".gitignore").read_text()
    assert "/ops.jsonl" in ignore and "/workspace.toml" in ignore

    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    baseline = repo.vcs.resolve("@-" if repo.vcs.kind == "jj" else "HEAD")
    repo.new(bookmark="work", eager=True)
    wref = repo.workspace.working_refs["db"]
    store.write(system, wref, {"v": 2})
    repo.commit("second")
    # The bookmark is moved back onto the baseline commit by hand; `new` then
    # finds the branch away from the pin, so the fork is deferred (lazy) and
    # the existing branch's head is recorded.
    repo.vcs.bookmark_set("work", baseline)
    repo.new("work")
    repo.open("db", read_only=False)  # materializes -> "fork" op
    repo.remove("db")
    repo.gc(dry_run=False)

    commands = [e.command for e in reversed(repo.ops())]
    assert commands == [
        "add",
        "commit",
        "new",
        "commit",
        "new",
        "fork",
        "remove",
        "gc",
    ]
    by = {e.command: e for e in reversed(repo.ops())}  # newest of each kind

    first_new = next(e for e in reversed(repo.ops()) if e.command == "new")
    assert first_new.result["created"] == ["db"] and first_new.result["reset"] == []
    assert first_new.pre["vcs"]["kind"] == repo.vcs.kind
    assert "workspace_id" in first_new.pre["workspace"]

    second_new = by["new"]
    assert second_new.result["pending_forks"] == {"db": wref}
    assert second_new.plan is not None
    (defer,) = [a for a in second_new.plan["actions"] if a["op"] == "defer-fork"]
    assert defer["params"]["existing"] == wref
    assert "snapshot_id" in defer["params"]["head"]  # head recorded before reset

    fork = by["fork"]
    assert fork.result["key"] == "db" and fork.result["ref"] == wref
    assert "db" in fork.pre["heads"]  # the branch existed and was reset

    commit = by["commit"]
    assert commit.result["pinned"]["db"].startswith(repo.config.dataset_id + ".")
    assert commit.pre["objects"]["db"] is not None and commit.result["vcs_commit"]

    assert by["remove"].pre["objects"]["db"].startswith("key = ")
    assert by["gc"].result["forgotten_working_refs"] == {"db": [wref]}
    assert all(e.undone_by is None and e.undoes is None for e in repo.ops())

    # The log is per workspace and never enters the VCS.
    assert not any(
        "ops.jsonl" in path
        for path in repo.vcs.list_files_at(repo.vcs.current_rev(), "")
    )
    assert read_ops(vcs_root) == list(reversed(repo.ops()))
    assert len(repo.ops(2)) == 2


def test_new_refuses_to_discard_unpinned_writes(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    repo.new(bookmark="work", eager=True)
    wref = repo.workspace.working_refs["db"]

    # Nothing written: a second new resets the branch without complaint.
    repo.new(eager=True)
    assert repo.workspace.working_refs["db"] == wref

    # Written but never committed: refused, before anything is touched.
    s_writes = store.write(system, wref, {"v": "uncommitted"})
    plan = repo.plan_new(eager=True)
    (refuse,) = [a for a in plan.actions if a.op == "refuse"]
    assert refuse.key == "db" and "--discard" in refuse.detail
    assert not [a for a in plan.actions if a.op == "fork"]
    with pytest.raises(TetherError, match="unpinned writes"):
        repo.apply_new(plan, verify=False)
    with pytest.raises(TetherError, match="unpinned writes"):
        repo.new()  # lazy forks reset the branch later; same refusal
    assert store.resolve(system, wref) == s_writes  # untouched
    assert not [e for e in repo.ops() if e.command == "new" and e.result.get("failed")]

    # Committing them first makes the branch *the* pinned state: kept as is.
    repo.commit("save the writes")
    plan = repo.plan_new(eager=True)
    assert [a.op for a in plan.actions] == ["reuse"]
    repo.apply_new(plan, verify=False)
    assert store.resolve(system, wref) == s_writes

    # ... and --discard throws them away on purpose, saying so in the plan.
    store.write(system, wref, {"v": "scratch"})
    plan = repo.plan_new(eager=True, discard=True)
    (fork,) = [a for a in plan.actions if a.op == "fork"]
    assert "discarding its writes" in fork.detail and plan.context["discard"]
    repo.apply_new(plan, verify=False)
    assert store.resolve(system, wref) == s_writes


# --------------------------------------------------------------------------- #
# undo
# --------------------------------------------------------------------------- #
def _undoable(repo: Repo) -> list[str]:
    return [e.command for e in repo.ops() if e.undoable]


def test_undo_commit_uncommits_and_keeps_pins(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    parent = "@-" if repo.vcs.kind == "jj" else "HEAD"
    before = repo.vcs.resolve(parent)  # the baseline dataset commit
    store.write(system, "main", {"v": 2})
    result = repo.commit("second")
    assert result.vcs_commit is not None
    pin = repo.objects["db"].pin
    assert pin is not None
    backend = repo.backend_for("memory")

    report = repo.undo()
    assert report.op.command == "commit" and report.complete
    assert any("uncommitted" in line for line in report.restored)
    # The dataset commit is gone from the VCS; the manifest change is back in
    # the working tree, so the pin is still referenced and still exists.
    assert repo.vcs.resolve(parent) == before
    assert repo.objects["db"].pin == pin
    assert pin.id in backend.list_pins({"system": system})
    assert not repo.is_stale()
    ops = repo.ops()
    assert ops[0].command == "undo" and ops[0].undoes == ops[1].id
    assert ops[1].undone_by == ops[0].id and not ops[1].undoable
    # Undoing an undo is refused; committing again just re-records the pin.
    with pytest.raises(TetherError, match="cannot undo"):
        repo.undo(ops[0].id)
    again = repo.commit("second, again")
    assert repo.objects["db"].pin == pin and again.vcs_commit
    # An older commit that is no longer the parent cannot be uncommitted.
    older = next(
        e
        for e in repo.ops()
        if e.command == "commit"
        and e.undoable
        and e.result["vcs_commit"] != again.vcs_commit
    )
    with pytest.raises(TetherError, match="no longer the working copy's parent"):
        repo.undo(older.id)


def test_undo_new_deletes_created_branches_and_restores_the_workspace(
    vcs_root: Path,
) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    ws_before = repo.workspace.to_toml()

    repo.new(bookmark="work", eager=True)
    wref = repo.workspace.working_refs["db"]
    assert wref in store.system(system).branches
    report = repo.undo()
    assert report.op.command == "new" and report.complete
    assert wref not in store.system(system).branches
    assert repo.workspace.to_toml() == ws_before
    assert "db" not in repo.workspace.working_refs
    assert "work" not in repo.vcs.bookmarks()  # the bookmark `new -b` made is gone

    # A branch that gained writes since the new is not deleted silently.
    repo.new(bookmark="work", eager=True)
    store.write(system, wref, {"v": "scratch"})
    with pytest.raises(TetherError, match="gained writes"):
        repo.undo()
    assert wref in store.system(system).branches
    report = repo.undo(discard=True)
    assert report.complete and wref not in store.system(system).branches

    # Lazy: the fork op (materialize) is undone the same way.
    repo.new(bookmark="work")
    assert "db" in repo.workspace.pending_forks
    repo.open("db", read_only=False)
    assert repo.ops()[0].command == "fork" and wref in store.system(system).branches
    report = repo.undo()
    assert report.complete and wref not in store.system(system).branches
    assert (
        "db" in repo.workspace.pending_forks and "db" not in repo.workspace.working_refs
    )


def test_undo_new_reports_reset_branches_and_restores_the_working_copy(
    vcs_root: Path,
) -> None:
    """`undo new` deletes the branches the op created; a branch it *reset* is
    not re-pointed (the store may not allow it, and the head to choose is the
    user's call) -- the report names the old head and points at `restore` /
    `new --discard`. The workspace and VCS position still come back."""
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    c1 = repo.vcs.current_rev() if repo.vcs.kind == "git" else repo.vcs.resolve("@-")
    repo.new(bookmark="work", eager=True)
    wref = repo.workspace.working_refs["db"]
    s_work = store.write(system, wref, {"v": "work"})
    repo.commit("work")  # pins the branch head; the branch is the only copy

    # The bookmark is moved back by hand (git: this moves HEAD too); `new` onto
    # it moves the working copy and resets the branch onto the old pin.
    repo.vcs.bookmark_set("work", c1)
    pos_before_new = repo.vcs.position()
    repo.new("work", eager=True)
    after_reset = store.resolve(system, wref)
    assert after_reset != s_work
    entry = repo.ops()[0]
    assert entry.command == "new" and entry.result["reset"] == ["db"]
    assert entry.pre["heads"]["db"] == {"snapshot_id": s_work}

    report = repo.undo()
    assert not report.complete  # the reset is reported, not reversed
    assert store.resolve(system, wref) == after_reset  # untouched
    (line,) = report.irreversible
    assert "was reset" in line and s_work[:8] in line and "tether restore db" in line
    # Working copy back where it was before `new`: git returns to the
    # branch/commit; jj cannot revive the abandoned empty change, so it opens
    # a fresh one on the same parent.
    if repo.vcs.kind == "git":
        assert repo.vcs.position()["id"] == pos_before_new["id"]
    else:
        assert repo.vcs.position()["parent"] == pos_before_new["parent"]
    assert repo.workspace.working_refs["db"] == wref
    # jj is back on top of the "work" commit; git's hand-moved branch left the
    # checkout at c1, so the workspace (which last committed "work") is stale.
    assert repo.is_stale() == (repo.vcs.kind == "git")


def test_undo_gc_recreates_neither_branches_nor_pins(vcs_root: Path) -> None:
    """What gc deleted from the stores stays deleted: `repair` recreates a
    bookmark's branches from its manifests, and pins while the state is still
    reachable. `undo gc` restores what it can (forgotten working refs,
    listings) and says the rest plainly."""
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    backend = repo.backend_for("memory")
    locator = {"system": system}
    sid = store.system(system).branches["main"]
    orphan = f"{repo.config.dataset_id}.0000000000badbad"
    backend.pin(locator, {"snapshot_id": sid}, orphan)
    stray = f"tether.ws.{repo.config.dataset_id}.deadbeef.db-000000"
    store.system(system).branches[stray] = sid  # equals base head: deletable

    repo.gc(dry_run=False, prune_bookmarks=True, release_foreign=True)
    assert (
        orphan not in backend.list_pins(locator)
        and stray not in store.system(system).branches
    )
    report = repo.undo()
    assert not report.complete
    text = "\n".join(report.irreversible)
    assert "branch(es) deleted" in text and stray in text and "repair" in text
    assert "pin(s) deleted" in text
    assert not any("recreated" in line for line in report.restored)
    assert stray not in store.system(system).branches  # honestly gone
    assert orphan not in backend.list_pins(locator)
    assert repo.ops()[0].command == "undo" and repo.ops()[1].undone_by


def test_undo_manifest_edits_and_promote(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")

    # add / remove / import restore the manifests they rewrote.
    repo.add("other", "memory", {"system": system, "branch": "main"})
    report = repo.undo()
    assert report.op.command == "add" and "other" not in repo.objects
    manifest = repo.objects["db"]
    repo.remove("db")
    assert "db" not in repo.objects
    repo.undo()
    assert repo.objects["db"] == manifest
    specs, _ = __import__(
        "tether.experimental.registry.registry", fromlist=["specs_from_rows"]
    ).specs_from_rows(
        [
            {
                "key": "db",
                "kind": "memory",
                "locator_json": {"system": system, "branch": "main"},
                "policy_pin": "record",
            }
        ],
        repo.config.defaults,
    )
    repo.apply_import(repo.plan_import(specs))
    assert repo.objects["db"].policy.pin == "record"
    repo.undo()
    assert repo.objects["db"] == manifest

    # promote cannot be undone; the previous head is reported.
    repo.new(bookmark="work", eager=True)
    wref = repo.workspace.working_refs["db"]
    store.write(system, wref, {"v": 2})
    repo.commit("work")
    repo.promote(["db"])
    with pytest.raises(TetherError, match="only fast-forwards") as exc:
        repo.undo()
    assert "db: base branch moved" in str(exc.value)
    # The attempt is journaled (it began before it could know), marked failed;
    # the promote itself is not marked undone.
    attempt, promoted = repo.ops()[:2]
    assert attempt.command == "undo" and attempt.result["failed"] == "nothing restored"
    assert promoted.command == "promote" and promoted.undone_by is None

    # Nothing left that can be undone -> loud. (promote moved `main` onto the
    # work commit; under git, returning to `main` therefore lands past the
    # baseline commit, which then cannot be uncommitted -- also loud.)
    for e in repo.ops():
        if e.undoable and e.command != "promote":
            try:
                repo.undo(e.id)
            except TetherError as exc:
                assert "no longer the working copy's parent" in str(exc)
                break
    with pytest.raises(
        TetherError, match=r"nothing to undo|only fast-forwards|no longer the"
    ):
        repo.undo()


def test_undo_of_an_older_op_reverts_only_the_workspace_fields_it_changed(
    vcs_root: Path,
) -> None:
    """Port of the review's `gc/r9`: undoing an older `add` by id put back the
    whole `workspace.toml` from before it -- the checkout flipped from `feat`
    to `main` with no working refs, `bookmark_drift` stayed silent, and the
    next write went straight to the store's `main` branch. Only the fields
    the operation changed come back now."""
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    _mem_object(repo, "aux")
    add_op = repo.ops()[0]
    assert add_op.command == "add"
    repo.commit("add aux")
    repo.new(bookmark="feat", eager=True)
    wref = repo.workspace.working_refs["db"]
    store.write(system, wref, {"x": "feat 1"})
    repo.commit("feat write")
    refs = dict(repo.workspace.working_refs)
    main_head = store.resolve(system, "main")

    report = repo.undo(add_op.id)
    assert report.op.command == "add" and report.complete
    assert "aux" not in repo.objects and any(
        "manifest removed" in line for line in report.restored
    )
    # The `add` changed no bookmark and no working ref, so none comes back
    # (the snapshot cache the next commit's snapshot filled may).
    assert not any(
        "bookmark" in line or "working_refs" in line for line in report.restored
    )
    repo = Repo.find(vcs_root)
    assert repo.workspace.bookmark == "feat"
    assert repo.workspace.working_refs == refs  # aux's ref stays until gc
    assert repo.bookmark_drift() == []
    handle = repo.open("db")
    assert isinstance(handle, MemoryHandle)
    handle.write({"x": "meant for feat"})
    assert store.resolve(system, "main") == main_head
    assert store.read(system, wref) == {"x": "meant for feat"}


def test_undo_of_an_older_new_keeps_a_branch_a_commit_since_recorded(
    vcs_root: Path,
) -> None:
    """Port of the review's `gc/r5`: `undo <new>` after a commit on the branch
    `new` created judged the branch free of new writes -- its head *was* the
    committed state -- and deleted it. For a `pin = "record"` object that
    branch was the only ref holding what the commit recorded, and the
    workspace rolled back to `main` under a working copy still on `probe`."""
    repo = Repo.init(vcs_root)
    store = default_store()
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(system)
    repo.add(
        "db",
        "memory",
        {"system": system, "branch": "main"},
        policy=Policy(pin="record"),
    )
    repo.commit("baseline")
    repo.new(bookmark="probe")  # pin = "record": forked now
    new_op = repo.ops()[0]
    assert new_op.command == "new" and new_op.result["created"] == ["db"]
    wref = repo.workspace.working_refs["db"]
    store.write(system, wref, {"probe": "committed"})
    result = repo.commit("probe work")
    state = repo.objects["db"].state
    assert result.vcs_commit is not None and state is not None
    assert repo.objects["db"].pin is None

    with pytest.raises(TetherError, match="a state the manifest records") as exc:
        repo.undo(new_op.id)
    assert wref in str(exc.value) and "--discard" in str(exc.value)
    assert store.resolve(system, wref) == state["snapshot_id"]
    assert repo.workspace.bookmark == "probe"
    assert repo.workspace.working_refs["db"] == wref
    assert result.vcs_commit in repo.vcs.history_revs()
    attempt = repo.ops()[0]
    assert attempt.command == "undo" and attempt.result.get("failed")
    assert not any(e.undone_by for e in repo.ops() if e.id == new_op.id)
    # Newest first: the commit comes back as working-tree changes, and the
    # branch -- now holding a state only the working tree's manifest names --
    # is still refused without `--discard`.
    report = repo.undo()
    assert report.op.command == "commit" and report.complete
    with pytest.raises(TetherError, match="a state the manifest records"):
        repo.undo()
    assert store.resolve(system, wref) == state["snapshot_id"]


def test_undo_after_a_drop_revives_nothing(vcs_root: Path) -> None:
    """Port of the review's `gc/r10`: `drop` runs `new` and `gc` internally
    and each was journaled as its own undoable entry, so two `tether undo`s
    after a drop brought the abandoned commit back (`jj new <hidden parent>`)
    -- naming a pin, a bookmark and a fork that were gone. The steps are the
    drop's children now: refused on their own, and the drop is never reached
    past."""
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    repo.new(bookmark="probe", eager=True)
    wref = repo.workspace.working_refs["db"]
    store.write(system, wref, {"x": "probe"})
    result = repo.commit("probe write")
    commit = result.vcs_commit
    assert commit is not None
    pin = result.pinned["db"]
    assert pin is not None

    repo.drop("probe")
    ops = repo.ops()
    drop = next(e for e in ops if e.command == "drop")
    steps = [e for e in ops if e.parent == drop.id]
    assert sorted(e.command for e in steps) == ["gc", "new"]
    assert not any(e.undoable for e in steps) and not drop.undoable
    assert ops[0].parent == drop.id  # the newest entry is a step of the drop
    workspace = repo.workspace.to_toml()
    position = repo.vcs.position()

    for _ in range(2):
        with pytest.raises(
            TetherError, match=r"is a step of .* which cannot be undone"
        ):
            repo.undo()
    for step in steps:
        with pytest.raises(TetherError, match="is a step of"):
            repo.undo(step.id)
    with pytest.raises(TetherError, match="cannot undo 'drop'"):
        repo.undo(drop.id)
    # Refused before anything was journaled or touched.
    assert [e.command for e in repo.ops()] == [e.command for e in ops]
    assert repo.workspace.to_toml() == workspace
    assert repo.vcs.position() == position
    assert repo.workspace.bookmark == "main" and "probe" not in repo.vcs.bookmarks()
    assert commit not in repo.vcs.history_revs()
    assert wref not in store.system(system).branches
    assert pin.id not in repo.backend_for("memory").list_pins({"system": system})
    assert all(v.ok for v in repo.verify().values())


def test_undo_commit_leaves_other_bookmarks_built_on_it_alone(vcs_root: Path) -> None:
    """Port of the review's `gc/r8`: under jj, uncommitting C (`squash --from
    C --into @`) rebased every other child of C -- a bookmark built on it
    became conflicted, read the baseline manifest, and gc planned to unpin
    its pin. Refused now while anything but the working copy builds on the
    commit. git's `reset --soft` rewrites nothing, so there the undo goes
    ahead and the other branch keeps the commit reachable."""
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    repo.new(bookmark="feat", eager=True)
    wref = repo.workspace.working_refs["db"]
    store.write(system, wref, {"x": 1})
    r1 = repo.commit("feat write 1")
    commit_op = repo.ops()[0]
    assert commit_op.command == "commit" and r1.vcs_commit is not None
    # A second line on top of feat's commit, with a commit of its own.
    repo.new(bookmark="exp", eager=True)
    store.write(system, repo.workspace.working_refs["db"], {"x": 2})
    r2 = repo.commit("exp write on top of feat")
    assert r2.vcs_commit is not None and r2.pinned["db"] is not None
    exp_manifest = repo._objects_at(r2.vcs_commit)["db"]
    repo.new("feat")  # back on feat: the working copy sits on r1 again

    if repo.vcs.kind == "jj":
        with pytest.raises(TetherError, match="other commit\\(s\\) built on it") as exc:
            repo.undo(commit_op.id)
        assert r2.vcs_commit[:12] in str(exc.value) and "jj backout" in str(exc.value)
        assert r1.vcs_commit in repo.vcs.history_revs()
        assert repo.vcs.bookmarks()["feat"] == r1.vcs_commit
    else:
        report = repo.undo(commit_op.id)
        assert report.complete
    assert repo.vcs.bookmarks()["exp"] == r2.vcs_commit
    assert repo._objects_at(r2.vcs_commit)["db"] == exp_manifest
    assert not repo.vcs.conflicted_commits()
    assert not [a for a in repo.plan_gc().actions if a.op == "unpin"]


def test_repair_recreates_missing_pins_and_branches(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    sys_ = store.system(system)
    s1 = store.write(system, "main", {"v": 1})
    repo.commit("v1")
    pin1 = repo.objects["db"].pin
    s2 = store.write(system, "main", {"v": 2})
    repo.commit("v2")
    pin2 = repo.objects["db"].pin
    assert pin1 is not None and pin2 is not None
    repo.new(bookmark="work", eager=True)
    wref = repo.workspace.working_refs["db"]

    plan = repo.plan_repair()
    assert plan.is_empty and "nothing to repair" in plan.notes

    # Someone deleted the current pin and the working branch by hand.
    del sys_.tags[pin2.ref]
    del sys_.branches[wref]
    plan = repo.plan_repair()
    assert [(a.op, a.target) for a in plan.actions] == [
        ("repin", pin2.ref),
        ("refork", wref),
    ]
    report = repo.apply_repair(plan)
    assert report.repinned == {"db": pin2.id} and report.reforked == {"db": wref}
    assert sys_.tags[pin2.ref] == s2 and sys_.branches[wref] == s2
    assert not report.failed and repo.ops()[0].command == "repair"
    assert not repo.is_stale()

    # The older commit's pin is only checked with --all-history; a pin whose
    # state is gone cannot come back and is reported, not raised.
    del sys_.tags[pin1.ref]
    assert repo.plan_repair().is_empty
    plan = repo.plan_repair(all_history=True)
    assert [a.op for a in plan.actions] == ["repin"] and plan.actions[
        0
    ].target == pin1.ref
    del sys_.snapshots[s1]
    report = repo.apply_repair(plan)
    assert not report.repinned and list(report.failed) == [f"repin {pin1.ref}"]
    assert "no longer exists" in report.failed[f"repin {pin1.ref}"]

    # A pin that exists but points elsewhere is noted, never overwritten.
    sys_.tags[pin2.ref] = s1 if s1 in sys_.snapshots else sys_.branches["main"]
    sys_.tags[pin2.ref] = store.write(system, "main", {"v": "elsewhere"})
    plan = repo.plan_repair()
    assert plan.is_empty and any("drifted" in n for n in plan.notes)


def test_abandon_frees_the_pins_only_those_commits_referenced(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    backend = repo.backend_for("memory")
    locator = {"system": system}
    repo.commit("v1")
    p1 = repo.objects["db"].pin
    store.write(system, "main", {"v": 2})
    c2 = repo.commit("v2").vcs_commit
    p2 = repo.objects["db"].pin
    store.write(system, "main", {"v": 3})
    repo.commit("v3")
    p3 = repo.objects["db"].pin
    assert p1 and p2 and p3 and c2

    # Drop the middle commit: v3's manifest is untouched, v2's pin is freed.
    report = repo.abandon([c2])
    assert report.abandoned == [c2] and report.gc_plan is not None
    assert [a.target for a in report.gc_plan.actions if a.op == "unpin"] == [p2.ref]
    assert p2.id in backend.list_pins(locator)  # not released without gc=True
    assert repo.objects["db"].pin == p3 and not repo.is_stale()
    seen = {
        m.pin.ref for _r, o in repo._iter_history_objects() for m in o.values() if m.pin
    }
    assert seen == {p1.ref, p3.ref}
    assert repo.ops()[0].command == "abandon" and not repo.ops()[0].undoable
    with pytest.raises(TetherError, match="cannot undo"):
        repo.undo()

    # Drop the tip with gc: the manifest reverts to v1 and v3's pin is gone.
    tip = repo.vcs.resolve("@-" if repo.vcs.kind == "jj" else "HEAD")
    report = repo.abandon([tip], gc=True)
    assert report.gc_report is not None
    assert set(report.gc_report.unpinned["memory"]) == {p2.id, p3.id}  # p2 was pending
    assert p3.id not in backend.list_pins(locator)
    assert repo.objects["db"].pin == p1
    assert p1.id in backend.list_pins(locator)


def test_restore_reforks_one_object_from_an_older_commit(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    other = _mem_object(repo, "other")
    store = default_store()
    s1 = store.write(system, "main", {"v": 1})
    repo.commit("v1")
    c1 = repo.vcs.resolve("@-" if repo.vcs.kind == "jj" else "HEAD")
    s2 = store.write(system, "main", {"v": 2})
    store.write(other, "main", {"o": 2})
    repo.commit("v2")
    repo.new(bookmark="work", eager=True)
    wref = repo.workspace.working_refs["db"]
    oref = repo.workspace.working_refs["other"]
    assert store.resolve(system, wref) == s2

    # Only db goes back to v1; other's branch, the manifests, and the VCS stay.
    plan = repo.plan_restore(["db"], c1)
    (fork,) = plan.actions
    assert fork.op == "fork" and fork.target == wref and "resets" in fork.detail
    done = repo.apply_restore(plan)
    assert done == {"db": wref}
    assert store.resolve(system, wref) == s1
    assert store.resolve(other, oref) == store.system(other).branches["main"]
    assert repo.objects["db"].state == {"snapshot_id": s2}  # manifest untouched
    assert not repo.is_stale()  # deliberate: the next commit pins the restore
    assert repo.workspace.fork_points["db"] == {"snapshot_id": s1}
    assert repo.ops()[0].command == "restore"
    # Committing pins the restored state under db.
    store.write(system, wref, {"v": "restored+"})
    repo.commit("back to v1 and on")
    assert repo.objects["db"].state == {"snapshot_id": store.resolve(system, wref)}
    # promote now sees a divergence (base at s2, fork point s1): a merge, not
    # a fast-forward.
    pr = repo.plan_promote(["db"])
    assert [a.op for a in pr.actions] == ["merge"], pr.actions

    # Writes on the branch block a restore without --discard. Undo of a
    # restore that *reset* the branch does not re-point it: it says what the
    # head was and leaves the move to `restore`.
    s_scratch = store.write(system, wref, {"v": "scratch"})
    plan = repo.plan_restore(["db"], c1)
    assert [a.op for a in plan.actions] == ["refuse"]
    with pytest.raises(TetherError, match="cannot restore"):
        repo.apply_restore(plan)
    repo.restore(["db"], c1, discard=True)
    assert store.resolve(system, wref) == s1
    report = repo.undo()
    assert report.op.command == "restore" and not report.complete
    assert store.resolve(system, wref) == s1  # left where restore put it
    (line,) = report.irreversible
    assert "was reset" in line and s_scratch[:8] in line and "tether restore db" in line

    # Not registered at that commit, or on the trunk: refused in the plan.
    with pytest.raises(ConfigError):
        repo.plan_restore(["nope"], c1)
    repo.add("late", "memory", {"system": _mem_object(repo, "tmp") and system})
    plan = repo.plan_restore(["late"], c1)
    assert plan.actions[0].op == "refuse" and "not registered" in plan.actions[0].detail


def test_restore_checks_the_branch_of_a_pending_fork_too(
    vcs_root: Path, tmp_path: Path
) -> None:
    """`restore` used to look for writes only on a branch this workspace had
    in `working_refs`. A fork `new` deferred is still pending here while a
    `--shared` peer (or another clone on the same bookmark) has created the
    branch and written to it; that head is checked like any other, and the
    plan binds to it. (Reviewed as e4 and r10.)"""
    a = Repo.init(vcs_root)
    if a.vcs.kind == "git":
        pytest.skip("git cannot check one branch out in two worktrees")
    system = _mem_object(a)
    store = default_store()
    a.commit("baseline")
    base = a._vcs_head_or_none()
    assert base
    a.new(bookmark="feat")  # lazy: pending fork
    b = _second_checkout(a, vcs_root, tmp_path / "peer")
    b.new("feat", shared=True)
    assert "db" in b.workspace.pending_forks and not b.workspace.working_refs

    handle = a.open("db")  # a creates the branch and writes, uncommitted
    assert isinstance(handle, MemoryHandle)
    handle.write({"wip": 1})
    ref = a.workspace.working_refs["db"]
    head = store.system(system).branches[ref]

    plan = b.plan_restore(["db"], base)
    (action,) = plan.actions
    assert action.op == "refuse" and "has writes since" in action.detail
    with pytest.raises(TetherError, match="cannot restore"):
        b.apply_restore(plan)
    assert store.system(system).branches[ref] == head  # a's write survived
    # Discarding is a choice; the plan then binds to the head it reviewed.
    plan = b.plan_restore(["db"], base, discard=True)
    (action,) = plan.actions
    assert action.op == "fork" and action.params["existing"] == ref
    assert [p.kind for p in plan.preconditions if p.key == "db"] == ["ref_head"]
    store.write(system, ref, {"wip": 2})  # a writes again before the apply
    with pytest.raises(StalePlanError, match="moved since the plan was made"):
        b.apply_restore(plan)
    b.restore(["db"], base, discard=True)
    assert store.system(system).branches[ref] == f"{system}:s0"


def test_shared_peers_materialize_one_lazy_fork_under_the_repository_lock(
    vcs_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Port of the review's `engine/e12` and `r3`: two `--shared` checkouts
    materialized the same lazy fork at once. `materialize_fork` held only the
    per-checkout lock, so the slower one listed the branch as absent while
    the faster one forked and wrote, then forked onto the pin itself and the
    reset contract threw the write away. The listing and the fork are one
    step under the repository lock now: the second checkout finds the branch
    the first created and joins it."""
    import threading

    a = Repo.init(vcs_root)
    if a.vcs.kind == "git":
        pytest.skip("git cannot check one branch out in two worktrees")
    system = _mem_object(a)
    store = default_store()
    a.commit("baseline")
    a.new(bookmark="feat")  # lazy: pending fork
    b = _second_checkout(a, vcs_root, tmp_path / "peer")
    b.new("feat", shared=True)
    assert "db" in a.workspace.pending_forks and "db" in b.workspace.pending_forks

    # b has listed the branch (absent) and is inside its fork when a starts.
    b_backend = b.backend_for("memory")
    real_verify = b_backend.verify
    b_in_window = threading.Event()
    a_started = threading.Event()
    failures: list[BaseException] = []

    def slow_verify(*args: Any, **kwargs: Any) -> Any:
        b_in_window.set()
        a_started.wait(5)
        return real_verify(*args, **kwargs)

    monkeypatch.setattr(b_backend, "verify", slow_verify)

    def b_opens() -> None:
        try:
            b.open("db")
        except BaseException as exc:  # reported to the test
            failures.append(exc)

    thread = threading.Thread(target=b_opens, name="peer")
    thread.start()
    assert b_in_window.wait(5)
    a_started.set()
    handle = a.open("db")  # waits for b's fork, then joins the branch
    assert isinstance(handle, MemoryHandle)
    handle.write({"a": "wrote this"})
    thread.join(10)
    assert not thread.is_alive() and not failures

    ref = a.workspace.working_refs["db"]
    assert b.workspace.working_refs["db"] == ref
    assert store.read(system, ref) == {"a": "wrote this"}  # nothing reset it
    assert [e.command for e in b.ops()][:1] == ["fork"]
    assert "fork" not in [e.command for e in a.ops()]  # a joined; b forked


def test_a_fork_moves_a_branch_only_from_the_head_it_listed(
    vcs_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Port of the review's `engine/r3` for a peer that shares no lock file
    (a clone on another machine): the fork itself is conditional -- the
    branch must still be absent -- so a branch another checkout created and
    wrote to between the listing and the fork is left as it is, and this
    checkout is told to run `new` again."""
    import contextlib

    a = Repo.init(vcs_root)
    if a.vcs.kind == "git":
        pytest.skip("git cannot check one branch out in two worktrees")
    system = _mem_object(a)
    store = default_store()
    a.commit("baseline")
    a.new(bookmark="feat")
    b = _second_checkout(a, vcs_root, tmp_path / "peer")
    b.new("feat", shared=True)
    monkeypatch.setattr(b, "_repo_lock", contextlib.nullcontext)  # no shared lock

    b_backend = b.backend_for("memory")
    real_list = b_backend.list_working_refs
    fired: list[str] = []

    def racing_list(locator: Locator) -> list[str]:
        out = real_list(locator)
        if not fired:  # b has just decided "absent"; a forks and writes now
            handle = a.open("db")
            assert isinstance(handle, MemoryHandle)
            fired.append(handle.ref)
            handle.write({"a": "wrote this"})
        return out

    monkeypatch.setattr(b_backend, "list_working_refs", racing_list)
    with pytest.raises(StaleWorkingCopyError, match="created by another checkout"):
        b.open("db")
    (ref,) = fired
    assert store.read(system, ref) == {"a": "wrote this"}  # a's write survived
    assert "db" in b.workspace.pending_forks and not b.workspace.working_refs
    attempt = b.ops()[0]
    assert attempt.command == "fork" and not attempt.incomplete
    assert attempt.result.get("failed")
    # a has committed: the branch is at the pin again, and b joins it.
    a.commit("a's write")
    b.new("feat", shared=True)
    assert b.workspace.working_refs["db"] == ref


def test_restore_stops_at_a_branch_that_moved_under_it(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each reset moves its branch only from the head the plan reviewed. A
    write that lands in between (a clone sharing no lock) stops the restore
    there: the branches already reset are recorded as such, the moved one is
    left alone, and the journal entry closes as a failed attempt."""
    repo = Repo.init(vcs_root)
    sys_a = _mem_object(repo, "a")
    sys_b = _mem_object(repo, "b")
    store = default_store()
    store.write(sys_a, "main", {"v": 0})
    store.write(sys_b, "main", {"v": 0})
    repo.commit("v0")
    c0 = repo.vcs.resolve("@-" if repo.vcs.kind == "jj" else "HEAD")
    store.write(sys_a, "main", {"v": 1})
    s1b = store.write(sys_b, "main", {"v": 1})
    repo.commit("v1")
    repo.new(bookmark="work", eager=True)
    refs = dict(repo.workspace.working_refs)

    plan = repo.plan_restore(["a", "b"], c0)
    assert [a.key for a in plan.actions if a.op == "fork"] == ["a", "b"]
    backend = repo.backend_for("memory")
    real_fork = backend.fork

    def racing(
        locator: Locator, source: Pin | State, name: str, *, expected: State | None
    ) -> str:
        if locator["system"] == sys_b:  # a peer writes just before b's reset
            store.write(sys_b, refs["b"], {"peer": 1})
        return real_fork(locator, source, name, expected=expected)

    monkeypatch.setattr(backend, "fork", racing)
    with pytest.raises(StalePlanError, match=r"restore b: .*a restored and recorded"):
        repo.apply_restore(plan)
    assert store.read(sys_a, refs["a"]) == {"v": 0}  # reset, as planned
    assert store.read(sys_b, refs["b"]) == {"peer": 1}  # left alone
    assert repo.workspace.fork_points["a"] == {"snapshot_id": f"{sys_a}:s1"}
    assert repo.workspace.fork_points["b"] == {"snapshot_id": s1b}
    entry = repo.ops()[0]
    assert entry.command == "restore" and not entry.incomplete
    assert entry.result["reset"] == ["a"] and "expected" in entry.result["failed"]
    assert not repo.incomplete_ops()


def test_restore_refuses_a_head_it_cannot_read(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A branch whose head cannot be read is not "nothing to lose": the plan
    refuses rather than fall back to the cached snapshot and reset writes it
    never saw (r7); `plan_new` refuses in the same situation."""
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    c0 = repo._vcs_head_or_none()
    assert c0
    repo.new(bookmark="feat", eager=True)
    ref = repo.workspace.working_refs["db"]
    store.write(system, ref, {"v": 0})
    repo.commit("v0")
    store.write(system, ref, {"uncommitted": "precious"})
    repo.status()  # the cache now holds the uncommitted head
    backend = repo.backend_for("memory")
    real = backend.fingerprint

    def flaky(locator, working_ref):
        if working_ref == ref:
            raise BackendError("timeout", kind="memory")
        return real(locator, working_ref)

    monkeypatch.setattr(backend, "fingerprint", flaky)
    plan = repo.plan_restore(["db"], c0)
    (action,) = plan.actions
    assert action.op == "refuse" and "could not be read" in action.detail
    with pytest.raises(TetherError, match="reset it blind"):
        repo.apply_restore(plan)
    assert store.read(system, ref) == {"uncommitted": "precious"}


def _second_checkout(repo: Repo, vcs_root: Path, other_root: Path) -> Repo:
    import subprocess

    if repo.vcs.kind == "jj":
        subprocess.run(
            ["jj", "workspace", "add", str(other_root)],
            cwd=vcs_root,
            check=True,
            capture_output=True,
        )
    else:
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(other_root)],
            cwd=vcs_root,
            check=True,
            capture_output=True,
        )
    return Repo.find(other_root)


def test_forget_workspace_deletes_its_files_and_checkout_not_branches(
    vcs_root: Path, tmp_path: Path
) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    repo.new(bookmark="work", eager=True)
    mine = repo.workspace.working_refs["db"]

    other_root = tmp_path / "other-checkout"
    other = _second_checkout(repo, vcs_root, other_root)
    other.new(bookmark="theirs", eager=True)
    theirs = other.workspace.working_refs["db"]
    other_id = other.workspace.workspace_id
    assert other_id in repo.live_workspace_ids()
    ws_file = other_root / ".tether" / "workspace.toml"
    assert ws_file.exists()

    # Forget the other checkout from here: its files and the VCS checkout go;
    # its bookmark's branch stays -- branches belong to bookmarks, not
    # workspaces -- until the bookmark is deleted and gc prunes it.
    plan = repo.plan_forget_workspace(other_id)
    ops = {a.op for a in plan.actions}
    assert ops == {"delete-file", "forget-vcs-workspace"}
    report = repo.apply_forget_workspace(plan)
    assert not report.failed, report.failed
    assert theirs in store.system(system).branches
    assert mine in store.system(system).branches
    assert not ws_file.exists()
    assert report.vcs and other_id not in repo.live_workspace_ids()
    assert not any(
        r.resolve() == other_root.resolve() for r in repo.vcs.workspace_roots()
    )
    assert repo.ops()[0].command == "forget-workspace" and not repo.ops()[0].undoable
    # The bookmark is still live (the VCS has it), so gc keeps its branch...
    assert "theirs" in repo.live_bookmarks()
    assert not [
        a for a in repo.plan_gc(prune_bookmarks=True).actions if a.target == theirs
    ]
    # ...until the bookmark itself is deleted.
    repo.vcs.bookmark_delete("theirs")
    (a,) = [x for x in repo.plan_gc(prune_bookmarks=True).actions if x.target == theirs]
    assert a.op == "delete-branch" and "bookmark theirs (gone)" in a.detail

    # Forgetting the current workspace: state files go, branch stays.
    report = repo.forget_workspace()
    assert mine in store.system(system).branches
    assert not (vcs_root / ".tether" / "workspace.toml").exists()
    # The main checkout stays; the next command here is a fresh workspace.
    fresh = Repo.find(vcs_root)
    assert (
        fresh.workspace.workspace_id != repo.workspace.workspace_id
        or not repo.workspace.working_refs
    )
    assert fresh.workspace.working_refs == {} and fresh.workspace.bookmark is None


def test_vcs_drift_notices_commits_removed_behind_tethers_back(vcs_root: Path) -> None:
    import subprocess

    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("v1")
    store.write(system, "main", {"v": 2})
    c2 = repo.commit("v2").vcs_commit
    assert c2 is not None
    assert repo.vcs_drift() == [] and repo.status(do_snapshot=False).vcs_drift == []

    # Remove the commit with the VCS directly, as a user would.
    if repo.vcs.kind == "jj":
        subprocess.run(
            ["jj", "abandon", c2], cwd=vcs_root, check=True, capture_output=True
        )
    else:
        subprocess.run(
            ["git", "reset", "--hard", "HEAD~1"],
            cwd=vcs_root,
            check=True,
            capture_output=True,
        )
    repo = Repo.find(vcs_root)
    if repo.vcs.kind == "jj":
        # jj deletes a bookmark whose commit is abandoned; the user puts it back.
        assert "main" not in repo.vcs.bookmarks()
        with pytest.raises(StaleWorkingCopyError, match="no longer exists"):
            repo.commit("blocked")
        repo.vcs.bookmark_set("main", "@-")
    (drift,) = repo.vcs_drift()
    assert drift.commit == c2 and drift.op.command == "commit"
    # The manifests reverted with the commit, so v2's pin is unreferenced.
    assert drift.referenced == {"db": False}
    assert "no longer in VCS history" in drift.message and "gc" in drift.message
    assert repo.status(do_snapshot=False).vcs_drift[0].commit == c2

    # tether's own removals are not drift: undo (uncommit) and abandon.
    store.write(system, "main", {"v": 3})
    c3 = repo.commit("v3").vcs_commit
    repo.undo()  # uncommit c3
    store.write(system, "main", {"v": 4})
    c4 = repo.commit("v4").vcs_commit
    repo.abandon([c4]) if c4 else None
    assert [d.commit for d in repo.vcs_drift()] == [c2]
    assert c3 is not None

    # A rewrite tether did itself (abandon rebases descendants) is followed
    # through the recorded mapping, not reported as drift.
    store.write(system, "main", {"v": 5})
    c5 = repo.commit("v5").vcs_commit
    store.write(system, "main", {"v": 6})
    c6 = repo.commit("v6").vcs_commit
    assert c5 and c6
    report = repo.abandon([c5])  # c6 is rebased and gets a new id
    assert c6 in repo.ops()[0].result["rewritten_commits"]
    assert [d.commit for d in repo.vcs_drift()] == [c2]
    del report


def test_prune_plans_undeletable_branches_as_kept(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store that will refuse the delete (Neon: pin children) is planned as
    kept with the reason -- even under --force-prune -- not failed at apply."""
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    ds = repo.config.dataset_id
    stray = f"tether.ws.{ds}.deadbeef.db-000000"
    store.system(system).branches[stray] = store.system(system).branches["main"]
    backend = repo.backend_for("memory")
    monkeypatch.setattr(
        backend,
        "working_ref_blockers",
        lambda locator, ref: "2 branch(es) hang off it" if ref == stray else None,
    )
    for force in (False, True):
        plan = repo.plan_gc(prune_bookmarks=True, force_prune=force)
        (a,) = [x for x in plan.actions if x.target == stray]
        assert a.op == "keep-branch" and "cannot be deleted" in a.detail
        assert a.params.get("blocked") is True
    report = repo.gc(dry_run=False, prune_bookmarks=True, force_prune=True)
    assert report.kept_working_refs == {"db": [stray]}
    assert stray in store.system(system).branches


def test_positions_commit_unchanged_until_pull(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the trunk, `commit` pins the upstream branches -- they are the
    working refs. Off the trunk, an object with no branch keeps its pin; the
    world moving on is `behind`, and `pull` on the trunk is what takes it."""
    from tether.backends.memory import MemoryBackend

    calls: list[str] = []
    real = MemoryBackend.fingerprint

    def counting(self, locator, working_ref):  # type: ignore[no-untyped-def]
        calls.append(str(locator.get("system")))
        return real(self, locator, working_ref)

    monkeypatch.setattr(MemoryBackend, "fingerprint", counting)

    repo = Repo.init(vcs_root)
    store = default_store()
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(system)
    s1 = store.write(system, "main", {"v": 1})
    repo.add("db", "memory", {"system": system, "branch": "main"})
    assert repo.moving_keys() == ["db"]  # no commit yet: the first commit reads it
    res = repo.commit("adopt")
    assert res.pinned["db"] is not None and calls == [system]

    # A feature bookmark forked lazily: no branch exists, so the object sits
    # at its pin and commit does not contact the store.
    repo.new(bookmark="feature")
    assert repo.moving_keys() == []
    s2 = store.write(system, "main", {"v": 2})
    calls.clear()
    plan = repo.plan_commit("again")
    assert calls == [] and plan.is_empty and "db: unchanged" in plan.notes
    assert repo.status(do_snapshot=False).objects[0].state_label == "clean"
    # Off the trunk a fan-out does not ask upstream either: main moving on is
    # not this bookmark's business.
    assert repo.status(do_snapshot=True).objects[0].state_label == "clean"
    assert calls == []
    # Reads stay at the position (the pin), not the upstream head.
    ro = repo.open("db", read_only=True)
    assert isinstance(ro, MemoryHandle) and ro.read() == {"v": 1}
    # A pull off the trunk has nothing to fetch: no branch, no commit.
    report = repo.pull()
    assert report.bookmark == "feature" and not report.committed
    assert "no branch yet" in report.skipped["db"] and report.vcs_commit is None

    # On the trunk the upstream branch *is* the working ref: a fan-out sees
    # `behind`... no -- `modified`, and commit pins it. `pull` is the same
    # thing spelled as fetch: a commit on the trunk.
    repo.new("main")
    assert repo.on_trunk() and repo.moving_keys() == ["db"]
    calls.clear()
    assert repo.status(do_snapshot=True).objects[0].state_label == "modified"
    assert calls == [system]
    report = repo.pull()
    assert report.committed == {"db": ({"snapshot_id": s1}, {"snapshot_id": s2})}
    assert report.vcs_commit and report.pinned["db"] is not None
    assert repo.objects["db"].state == {"snapshot_id": s2}
    assert repo.vcs.bookmarks()["main"] == report.vcs_commit
    assert repo.ops()[0].command == "pull"
    assert repo.pull().unchanged == ["db"]
    # undo uncommits the pull (pins stay), like a commit.
    repo.undo()
    assert repo.objects["db"].state == {"snapshot_id": s2}  # working-tree edit
    assert repo.vcs.dirty(repo._vcs_paths())
    with pytest.raises(ConfigError, match="uncommitted edits"):
        repo.pull()
    repo.commit("take main")
    assert not repo.vcs.dirty(repo._vcs_paths())

    # A read-only working copy (a revision carrying no bookmark) still learns
    # from a fan-out that upstream moved on: `behind`, and a plain commit
    # cannot move anything.
    store.write(system, "main", {"v": 3})
    pulled_commit = repo.vcs.bookmarks()["main"]
    assert res.vcs_commit is not None
    repo.vcs.bookmark_set("main", res.vcs_commit)  # main back to the adopt commit
    repo.new(pulled_commit)
    assert repo.workspace.bookmark is None
    assert repo.status(do_snapshot=True).objects[0].state_label == "behind"
    assert repo.plan_commit("ro").is_empty


def test_pull_off_trunk_is_commit(vcs_root: Path) -> None:
    """On a feature bookmark the branches *are* the working refs, so pull and
    commit see the same heads; pull just spells it as a fetch."""
    repo = Repo.init(vcs_root)
    store = default_store()
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(system)
    store.write(system, "main", {"v": 1})
    repo.add("db", "memory", {"system": system, "branch": "main"})
    repo.commit("adopt")
    repo.new(bookmark="feature", eager=True)
    wref = repo.workspace.working_refs["db"]
    s2 = store.write(system, wref, {"v": 2})
    report = repo.pull()
    assert report.committed["db"][1] == {"snapshot_id": s2}
    assert repo.vcs.bookmarks()["feature"] == report.vcs_commit
    assert repo.plan_commit("nothing").is_empty
    with pytest.raises(ConfigError, match="tether new main"):
        repo.pull("main")


def test_add_at_is_the_initial_position_only(vcs_root: Path) -> None:
    """`--at` says where a new object starts; a trunk pull moves it onto the
    branch and the manifest stops carrying `at`."""
    repo = Repo.init(vcs_root)
    store = default_store()
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(system)
    s1 = store.write(system, "main", {"v": 1})
    s2 = store.write(system, "main", {"v": 2})
    repo.add("db", "memory", {"system": system, "branch": "main", "at": s1})
    repo.commit("adopt at s1")
    assert repo.objects["db"].state == {"snapshot_id": s1}
    # `at` holds the position even on the trunk: a fan-out shows main ahead.
    assert repo.status(do_snapshot=True).objects[0].state_label == "behind"
    assert repo.plan_commit("still at s1").is_empty
    report = repo.pull()
    assert report.committed["db"][1] == {"snapshot_id": s2}
    assert repo.objects["db"].state == {"snapshot_id": s2}
    assert "at" not in repo.objects["db"].locator


def test_set_policy_changes_file_and_pin_in_place(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    store = default_store()
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(system)
    store.write(system, "main", {"v": 1})
    repo.add("db", "memory", {"system": system, "branch": "main"})
    repo.commit("baseline")
    repo.new(bookmark="work", eager=True)
    branch = repo.workspace.working_refs["db"]

    # pin changes keep the workspace's hold; undo restores the manifest.
    report = repo.set_policy(["db"], pin="record")
    assert report.changed == {"db": {"pin": ("native", "record")}}
    assert repo.workspace.working_refs["db"] == branch
    assert Repo.find(vcs_root).objects["db"].policy.pin == "record"
    assert (
        repo.ops()[0].command == "set"
        and "pin=native->record" in repo.ops()[0].summary()
    )
    repo.undo()
    assert repo.objects["db"].policy.pin == "native"

    # Nothing to change is reported, not logged; bad values refuse.
    report = repo.set_policy(["db"], pin="native")
    assert report.unchanged == ["db"] and not report.changed
    assert repo.ops()[0].command == "undo"
    with pytest.raises(ConfigError, match=r"invalid policy\.pin"):
        repo.set_policy(["db"], pin="sideways")
    with pytest.raises(ConfigError, match="nothing to set"):
        repo.set_policy(["db"])
    with pytest.raises(ConfigError, match="no such object"):
        repo.set_policy(["nope"], pin="record")


def test_bookmarks_shape_the_working_copy(vcs_root: Path) -> None:
    """The dataset bookmark and the stores' branches are one shape: init puts
    the working copy on the trunk, `new -b` names a branch per system after the
    bookmark, commit moves the bookmark, and no bookmark means read-only."""
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    assert repo.workspace.bookmark == "main" == repo.config.trunk
    c1 = repo.commit("baseline").vcs_commit
    assert c1 is not None and repo.vcs.bookmarks()["main"] == c1

    # A bookmark: one store branch named after it, and the bookmark follows
    # the commits made on it while `main` stays put.
    repo.new(bookmark="feature", eager=True)
    ds = repo.config.dataset_id
    assert repo.workspace.working_refs["db"] == f"tether.ws.{ds}.feature"
    assert repo.vcs.bookmarks()["feature"] == c1
    store.write(system, f"tether.ws.{ds}.feature", {"v": 2})
    c2 = repo.commit("on feature").vcs_commit
    assert c2 is not None and repo.vcs.bookmarks() == {"main": c1, "feature": c2}
    with pytest.raises(ConfigError, match="exists"):
        repo.new(bookmark="feature")

    # Leave the bookmark behind with the VCS: commit refuses until `new`.
    repo.vcs.new("main")
    with pytest.raises(StaleWorkingCopyError, match="not on bookmark 'feature'"):
        repo.commit("astray")
    repo.new("main")
    assert repo.on_trunk() and repo.workspace.working_refs["db"] == "main"
    assert repo.plan_commit("nothing").is_empty

    # A revision with no bookmark is read-only (git parks it on a `tether/*`
    # branch, which is not a bookmark either).
    repo.vcs.bookmark_set("main", c2)  # free c1 of its bookmark
    repo.new(c1)
    assert repo.workspace.bookmark is None and repo.workspace.working_refs == {}
    with pytest.raises(StaleWorkingCopyError, match="read-only"):
        repo.open("db", read_only=False)
    ro = repo.open("db", read_only=True)
    assert isinstance(ro, MemoryHandle) and ro.read_only
    assert repo.plan_new().context["bookmark"] is None
    assert repo.plan_commit("ro").is_empty  # nothing this checkout can move


def test_new_bookmark_never_reuses_another_bookmarks_branch(vcs_root: Path) -> None:
    """Branches belong to bookmarks. Starting a second bookmark from the first
    one's commit forks its own branch and leaves the first one's alone, even
    though that branch sits exactly at the pin (the old "reuse" shortcut)."""
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    ds = repo.config.dataset_id

    repo.new(bookmark="sweep", eager=True)
    store.write(system, f"tether.ws.{ds}.sweep", {"v": 2})
    repo.commit("trial")  # sweep's branch now sits exactly at sweep's pin

    repo.new(bookmark="scratch", eager=True)
    assert repo.workspace.working_refs["db"] == f"tether.ws.{ds}.scratch"
    heads = store.system(system)
    assert (
        heads.branches[f"tether.ws.{ds}.scratch"]
        == heads.branches[f"tether.ws.{ds}.sweep"]
    )
    store.write(system, f"tether.ws.{ds}.scratch", {"v": 3})
    assert store.read(system, f"tether.ws.{ds}.sweep") == {"v": 2}  # untouched

    # Undo deletes only the branch the second bookmark created (it refuses
    # while that branch holds writes; --discard throws them away).
    with pytest.raises(TetherError, match="gained writes"):
        repo.undo()
    repo.undo(discard=True)
    assert f"tether.ws.{ds}.scratch" not in heads.branches
    assert store.read(system, f"tether.ws.{ds}.sweep") == {"v": 2}
    assert repo.workspace.bookmark == "sweep"


def test_jj_undo_after_commit_takes_the_bookmark_back_too(vcs_root: Path) -> None:
    """`tether commit` moves the bookmark inside jj's commit operation, so a
    plain `jj undo` reverts the commit and the bookmark together and the
    workspace is coherent again (the next commit is not refused)."""
    import shutil
    import subprocess

    if not (vcs_root / ".jj").exists():
        pytest.skip("jj only")
    if shutil.which("jj") is None:
        pytest.skip("jj not on PATH")
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    c1 = repo.commit("baseline").vcs_commit
    repo.new(bookmark="work", eager=True)
    branch = f"tether.ws.{repo.config.dataset_id}.work"

    store.write(system, branch, {"v": 2})
    c2 = repo.commit("work").vcs_commit
    assert c2 is not None and repo.vcs.bookmarks()["work"] == c2

    subprocess.run(["jj", "undo"], cwd=vcs_root, check=True, capture_output=True)
    fresh = Repo.find(vcs_root)
    assert fresh.vcs.bookmarks()["work"] == c1
    assert c2 not in fresh.vcs.history_revs()
    # One operation undone leaves the working copy on the bookmark: commit again.
    c3 = fresh.commit("work, again").vcs_commit
    assert c3 is not None and fresh.vcs.bookmarks()["work"] == c3


def test_commit_refuses_when_the_bookmark_is_behind_the_parent(
    vcs_root: Path,
) -> None:
    """A `jj new` past an empty change leaves the bookmark two steps back;
    advance-bookmarks would not carry it, so commit refuses rather than move
    it in a second operation `jj undo` could not pair with the commit."""
    if not (vcs_root / ".jj").exists():
        pytest.skip("jj only")
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    repo.new(bookmark="work", eager=True)
    branch = f"tether.ws.{repo.config.dataset_id}.work"
    store.write(system, branch, {"v": 2})

    repo.vcs.new("@")  # an empty change between the bookmark and the working copy
    with pytest.raises(StaleWorkingCopyError, match="behind the working copy's parent"):
        repo.commit("skips a commit")
    # `new NAME` would re-fork and refuse over the uncommitted write; `--keep`
    # only moves the working copy back and leaves the branch as it is.
    with pytest.raises(TetherError, match="has writes since"):
        repo.new("work")
    repo.new("work", keep=True)
    c = repo.commit("work").vcs_commit
    assert c is not None and repo.vcs.bookmarks()["work"] == c
    assert store.read(system, branch) == {"v": 2}


def test_new_refuses_when_the_bookmarks_branch_head_cannot_be_read(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A branch that exists but cannot be fingerprinted is not reset blind:
    `new` refuses instead of planning a fork over writes it cannot see."""
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    ds = repo.config.dataset_id
    repo.new(bookmark="sweep", eager=True)
    branch = f"tether.ws.{ds}.sweep"
    store.write(system, branch, {"v": 2})  # uncommitted writes on the branch

    backend = repo.backend_for("memory")
    real = backend.fingerprint

    def flaky(locator: Locator, ref: str | None) -> State:
        if ref == branch:
            raise BackendError("store unreachable", kind="memory")
        return real(locator, ref)

    monkeypatch.setattr(backend, "fingerprint", flaky)
    with pytest.raises(TetherError, match="could not be read"):
        repo.new("sweep")
    monkeypatch.undo()
    assert store.read(system, branch) == {"v": 2}  # nothing was reset


def test_status_reports_bookmark_drift(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    c1 = repo.commit("baseline").vcs_commit
    assert c1 is not None
    repo.new(bookmark="feature", eager=True)
    st = repo.status(do_snapshot=False)
    assert st.bookmark == "feature" and not st.trunk and st.bookmark_drift == []
    wref = repo.workspace.working_refs["db"]
    store.write(system, wref, {"v": 2})
    c2 = repo.commit("work").vcs_commit
    assert c2 is not None and repo.bookmark_drift() == []

    # The working copy leaves the bookmark.
    repo.vcs.new("main")
    (msg,) = Repo.find(vcs_root).bookmark_drift()
    assert "left bookmark 'feature'" in msg and "tether new feature" in msg
    repo.new("feature")
    assert repo.bookmark_drift() == []

    # The bookmark is moved by hand, so its commit no longer describes the
    # branches this workspace forked / committed.
    repo.vcs.bookmark_set("feature", c1)
    if repo.vcs.kind == "jj":  # git moved the checkout too (reset --keep)
        (msg,) = repo.bookmark_drift()
        assert "was moved to" in msg and "db" in msg and "tether new feature" in msg
    repo.vcs.bookmark_set("feature", c2)
    if repo.vcs.kind == "git":
        repo.new("feature")
    assert repo.bookmark_drift() == []

    # Deleted, and deleted-with-a-lookalike (renamed).
    repo.vcs.new("main")  # git cannot delete the checked-out branch
    repo.vcs.bookmark_delete("feature")
    (msg,) = repo.bookmark_drift()
    assert "no longer exists" in msg and "tether new -b feature" in msg
    repo.vcs.bookmark_set("renamed", c2)
    repo.vcs.new("renamed")
    (msg,) = repo.bookmark_drift()
    assert "renamed?" in msg and "tether new renamed" in msg
    assert repo.status(do_snapshot=False).bookmark_drift == [msg]
