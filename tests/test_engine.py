from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tether.backends.memory import default_store
from tether.errors import (
    ConfigError,
    ImmutableObjectModified,
    StaleWorkingCopyError,
    TetherError,
)
from tether.handles import MemoryHandle
from tether.manifest import Pin, Policy, ref_for_pin
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

    dry = repo.gc(dry_run=True)
    assert dry.unpinned.get("memory", []) == [orphan]
    assert dry.plan is not None
    assert any("1 pin(s) of other datasets left alone" in n for n in dry.plan.notes)
    assert orphan in backend.list_pins(locator)  # dry-run kept it

    repo.gc(dry_run=False)
    assert orphan not in backend.list_pins(locator)
    assert "ffffffff.0000000000badbad" in backend.list_pins(locator)  # not ours


def _someone_else_commits(repo: Repo, key: str, state: dict) -> None:
    """Rewrite `key`'s committed manifest as another workspace's commit would."""
    from tether.manifest import write_object

    m = repo.objects[key]
    write_object(repo.root, m.with_pin(state=state, pin=m.pin, recoverable=True))


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
    r2 = repo.commit("update", pull=True)
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
    r2 = repo.commit("update", pull=True)
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
    store.write(system, "main", {"v": 2})
    stale = repo.plan_commit("next", pull=True)
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
        policy=Policy(write="direct"),
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
    c2 = repo.commit("second", pull=True).vcs_commit
    assert c1 and c2
    plan = repo.plan_new(c1)
    # The manifests at c1 change (someone rewrote history); the plan is stale.
    plan.context["manifest_hash"] = "not-what-is-there"
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

    # The branch that was created is known to the workspace, with its bookkeeping.
    ws = Repo.find(vcs_root).workspace
    assert ws.working_refs["ok"].startswith("tether.ws.")
    assert "ok" in ws.fork_points and "ok" in ws.base_states
    assert "bad" not in ws.working_refs and "bad" not in ws.pending_forks
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
    vcs_root: Path, tmp_path: Path
) -> None:
    """Dataset A's gc must not see dataset B's pins or working branches."""
    import subprocess

    other_root = tmp_path / "other"
    other_root.mkdir()
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
    result = repo.commit("second", pull=True)
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


def test_undo_new_restores_reset_branch_heads_and_the_working_copy(
    vcs_root: Path,
) -> None:
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
    assert store.resolve(system, wref) != s_work
    entry = repo.ops()[0]
    assert entry.command == "new" and entry.result["reset"] == ["db"]
    assert entry.pre["heads"]["db"] == {"snapshot_id": s_work}

    report = repo.undo()
    assert report.complete, report
    assert store.resolve(system, wref) == s_work  # head re-pointed
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


def test_undo_gc_recreates_branches_but_not_pins(vcs_root: Path) -> None:
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

    repo.gc(dry_run=False, prune_bookmarks=True)
    assert (
        orphan not in backend.list_pins(locator)
        and stray not in store.system(system).branches
    )
    report = repo.undo()
    assert not report.complete
    assert stray in store.system(system).branches  # branch back from its head
    assert any("recreated" in line for line in report.restored)
    assert any(
        "pin(s) deleted" in line and "repair" in line for line in report.irreversible
    )
    assert orphan not in backend.list_pins(locator)  # honestly gone
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
        "tether.registry", fromlist=["specs_from_rows"]
    ).specs_from_rows(
        [
            {
                "key": "db",
                "kind": "memory",
                "locator_json": {"system": system, "branch": "main"},
                "policy_write": "direct",
            }
        ],
        repo.config.defaults,
    )
    repo.apply_import(repo.plan_import(specs))
    assert repo.objects["db"].policy.write == "direct"
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
    assert repo.ops()[0].command == "promote" and repo.ops()[0].undone_by is None

    # Nothing left that can be undone -> loud.
    for e in repo.ops():
        if e.undoable and e.command != "promote":
            repo.undo(e.id)
    with pytest.raises(TetherError, match=r"nothing to undo|only fast-forwards"):
        repo.undo()


def test_repair_recreates_missing_pins_and_branches(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    sys_ = store.system(system)
    s1 = store.write(system, "main", {"v": 1})
    repo.commit("v1")
    pin1 = repo.objects["db"].pin
    s2 = store.write(system, "main", {"v": 2})
    repo.commit("v2", pull=True)
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
    c2 = repo.commit("v2", pull=True).vcs_commit
    p2 = repo.objects["db"].pin
    store.write(system, "main", {"v": 3})
    repo.commit("v3", pull=True)
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


def test_undo_to_walks_back_through_several_operations(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    anchor = repo.ops()[0]  # the state right after this commit is the goal
    manifest = repo.objects["db"]

    repo.new(bookmark="work", eager=True)
    wref = repo.workspace.working_refs["db"]
    store.write(system, wref, {"v": 2})
    repo.commit("work")
    repo.add("other", "memory", {"system": system, "branch": "main"})

    walk = repo.undo_to(anchor.id)
    assert walk.complete and [r.op.command for r in walk.reports] == [
        "add",
        "commit",
        "new",
    ]
    assert "other" not in repo.objects
    assert wref not in store.system(system).branches  # new's branch deleted
    # The "work" commit is uncommitted, not reverted: the VCS is back at the
    # anchor and the manifest change sits in the working tree with its pin.
    parent = "@-" if repo.vcs.kind == "jj" else "HEAD"
    assert repo.vcs.resolve(parent) == anchor.result["vcs_commit"]
    assert repo.objects["db"].pin != manifest.pin and repo.vcs.dirty(repo._vcs_paths())
    assert "db" not in repo.workspace.working_refs
    assert [e.command for e in repo.ops()[:3]] == ["undo", "undo", "undo"]
    by_id = {e.id: e for e in repo.ops()}
    assert all(by_id[r.op.id].undone_by == r.undo_id for r in walk.reports)
    assert by_id[anchor.id].undone_by is None  # the target itself stays

    # A non-undoable op (promote) stops the walk; what came before it in the
    # walk stays undone and the report says where it stopped.
    repo.new(bookmark="work", eager=True)  # the undo deleted the bookmark too
    wref = repo.workspace.working_refs["db"]
    store.write(system, wref, {"v": 3})
    repo.commit("more work")
    repo.promote(["db"])
    repo.add("third", "memory", {"system": system, "branch": "main"})
    walk = repo.undo_to(anchor.id)
    assert not walk.complete
    assert [r.op.command for r in walk.reports] == ["add"]
    assert walk.stopped_at is not None and walk.stopped_at.command == "promote"
    assert "only fast-forwards" in str(walk.reason)
    assert "third" not in repo.objects
    with pytest.raises(TetherError, match="no operation"):
        repo.undo_to("nope00000000")


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
    repo.commit("v2", pull=True)
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
    # promote now sees a divergence (base at s2, fork point s1): a merge, not
    # a fast-forward.
    pr = repo.plan_promote(["db"])
    assert [a.op for a in pr.actions] == ["merge"], pr.actions
    # Committing pins the restored state under db.
    store.write(system, wref, {"v": "restored+"})
    repo.commit("back to v1 and on")
    assert repo.objects["db"].state == {"snapshot_id": store.resolve(system, wref)}

    # Writes on the branch block a restore without --discard; undo puts the
    # branch back where it was before the restore.
    s_scratch = store.write(system, wref, {"v": "scratch"})
    plan = repo.plan_restore(["db"], c1)
    assert [a.op for a in plan.actions] == ["refuse"]
    with pytest.raises(TetherError, match="cannot restore"):
        repo.apply_restore(plan)
    repo.restore(["db"], c1, discard=True)
    assert store.resolve(system, wref) == s1
    report = repo.undo()
    assert report.op.command == "restore" and report.complete
    assert store.resolve(system, wref) == s_scratch  # recorded head restored

    # Not registered at that commit, or direct policy: refused in the plan.
    with pytest.raises(ConfigError):
        repo.plan_restore(["nope"], c1)
    repo.add("late", "memory", {"system": _mem_object(repo, "tmp") and system})
    plan = repo.plan_restore(["late"], c1)
    assert plan.actions[0].op == "refuse" and "not registered" in plan.actions[0].detail


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


def test_forget_workspace_deletes_its_branches_files_and_checkout(
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
    assert report.deleted_working_refs == {}
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
    c2 = repo.commit("v2", pull=True).vcs_commit
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
    c3 = repo.commit("v3", pull=True).vcs_commit
    repo.undo()  # uncommit c3
    store.write(system, "main", {"v": 4})
    c4 = repo.commit("v4", pull=True).vcs_commit
    repo.abandon([c4]) if c4 else None
    assert [d.commit for d in repo.vcs_drift()] == [c2]
    assert c3 is not None

    # A rewrite tether did itself (abandon rebases descendants) is followed
    # through the recorded mapping, not reported as drift.
    store.write(system, "main", {"v": 5})
    c5 = repo.commit("v5", pull=True).vcs_commit
    store.write(system, "main", {"v": 6})
    c6 = repo.commit("v6", pull=True).vcs_commit
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
    """An object with no working branch keeps its pin from commit to commit;
    `pull` is the explicit step that takes the upstream head."""
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
    assert repo.moving_keys() == []

    # main moves on. commit does not follow it; the pin stands.
    s2 = store.write(system, "main", {"v": 2})
    calls.clear()
    plan = repo.plan_commit("again")
    assert calls == [] and plan.is_empty and "db: unchanged" in plan.notes
    st = repo.status(do_snapshot=False)
    assert st.objects[0].state_label == "clean" and calls == []
    # A fan-out sees upstream ahead of us: `behind`, not `modified`.
    st = repo.status(do_snapshot=True)
    assert st.objects[0].state_label == "behind" and calls == [system]
    # ...and a cached fan-out must not turn into an implicit pull.
    assert repo.plan_commit("cached", do_snapshot=False).is_empty
    # A commit-time (partial) snapshot leaves what the fan-out learned alone:
    # db is still behind in a local status afterwards.
    repo.snapshot(upstream=False)
    assert repo.status(do_snapshot=False).objects[0].state_label == "behind"
    # Reads stay at the position, not the upstream head.
    ro = repo.open("db", read_only=True)
    assert isinstance(ro, MemoryHandle) and ro.read() == {"v": 1}

    # pull takes the head; status says so; commit pins it.
    report = repo.pull()
    assert report.pulled == {"db": ({"snapshot_id": s1}, {"snapshot_id": s2})}
    assert repo.workspace.pulled["db"] == {"snapshot_id": s2}
    assert repo.status(do_snapshot=False).objects[0].state_label == "pulled"
    ro = repo.open("db", read_only=True)
    assert isinstance(ro, MemoryHandle) and ro.read() == {"v": 2}
    plan = repo.plan_commit("take main")
    assert [a.op for a in plan.actions if a.key == "db"] == ["pin"]
    assert "db: pulled from main" in plan.notes
    # undo puts the position back before anything is committed.
    repo.undo()
    assert "db" not in repo.workspace.pulled
    # ...and the last fan-out still remembers that upstream is ahead.
    assert repo.status(do_snapshot=False).objects[0].state_label == "behind"
    repo.pull(["db"])
    res = repo.commit("take main")
    assert res.pinned["db"] is not None and repo.objects["db"].state == {
        "snapshot_id": s2
    }
    assert repo.workspace.pulled == {}
    assert repo.pull().up_to_date == ["db"]

    # commit --pull does it in one step, [commit] pull makes it the default.
    s3 = store.write(system, "main", {"v": 3})
    assert repo.plan_commit("no").is_empty
    res = repo.commit("with pull", pull=True)
    assert repo.objects["db"].state == {"snapshot_id": s3}
    repo.config.commit_pull = True
    s4 = store.write(system, "main", {"v": 4})
    assert not repo.plan_commit("default pull").is_empty
    repo.config.commit_pull = False

    # A working branch is what moves; pull refuses to step on it.
    repo.new(bookmark="work", eager=True)
    assert repo.moving_keys() == ["db"]
    wref = repo.workspace.working_refs["db"]
    store.write(system, wref, {"v": 5})
    assert repo.status(do_snapshot=False).objects[0].state_label == "modified"
    report = repo.pull()
    assert "working branch" in report.skipped["db"] and not report.pulled
    res = repo.commit("branch")
    assert res.pinned["db"] is not None
    assert repo.objects["db"].state == {"snapshot_id": store.resolve(system, wref)}
    _ = s4
    with pytest.raises(ConfigError, match="no such object"):
        repo.pull(["nope"])

    # Files sit at their recorded state too. pull reads the path; an immutable
    # file that changed is refused, a versioned/accepted one is held.
    (vcs_root / "f.bin").write_bytes(b"x")
    repo.add("f", "file", {"uri": str(vcs_root / "f.bin")})
    repo.commit("file")
    assert "f" not in repo.moving_keys()
    (vcs_root / "f.bin").write_bytes(b"xy")
    assert repo.plan_commit("untouched").is_empty
    with pytest.raises(ImmutableObjectModified):
        repo.pull(["f"])
    assert "f" not in repo.workspace.pulled


def test_pull_then_write_forks_from_the_pulled_state(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    store = default_store()
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(system)
    store.write(system, "main", {"v": 1})
    repo.add("db", "memory", {"system": system, "branch": "main"})
    repo.commit("adopt")
    repo.new(bookmark="work")  # lazy: the fork is decided, not created
    assert "db" in repo.workspace.pending_forks

    s2 = store.write(system, "main", {"v": 2})
    report = repo.pull(["db"])
    assert report.pulled["db"][1] == {"snapshot_id": s2}
    assert report.retargeted == ["db"]  # the pending fork will start at s2
    h = repo.open("db", read_only=False)  # first write: the branch is created now
    assert isinstance(h, MemoryHandle) and h.read() == {"v": 2}
    wref = repo.workspace.working_refs["db"]
    assert store.system(system).branches[wref] == s2
    assert repo.workspace.fork_points["db"] == {"snapshot_id": s2}
    # The branch is now the position; the pull is consumed by the commit.
    res = repo.commit("on the pulled base")
    assert res.pinned["db"] is not None and repo.workspace.pulled == {}

    # `new REV` puts positions back at the pins and drops any pull -- with
    # `keep` too, where the branches survive but the pulled states must not.
    store.write(system, "main", {"v": 3})
    repo.pull(["db"])  # skipped: working branch
    assert repo.workspace.pulled == {}
    repo.new()
    assert repo.workspace.pulled == {}
    repo.workspace.pulled["db"] = {"snapshot_id": "stale"}
    repo.new(keep=True)
    assert repo.workspace.pulled == {} and "db" in repo.workspace.working_refs


def test_add_at_is_the_initial_position_only(vcs_root: Path) -> None:
    """`--at` says where a new object starts; `pull` moves it onto the branch
    and the manifest stops carrying `at`."""
    repo = Repo.init(vcs_root)
    store = default_store()
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(system)
    s1 = store.write(system, "main", {"v": 1})
    s2 = store.write(system, "main", {"v": 2})
    repo.add("db", "memory", {"system": system, "branch": "main", "at": s1})
    repo.commit("adopt at s1")
    assert repo.objects["db"].state == {"snapshot_id": s1}
    assert repo.plan_commit("again").is_empty
    assert repo.status(do_snapshot=True).objects[0].state_label == "behind"
    assert repo.pull(["db"]).pulled["db"][1] == {"snapshot_id": s2}
    repo.commit("onto main")
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
    assert (
        report.changed == {"db": {"pin": ("native", "record")}} and not report.released
    )
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
