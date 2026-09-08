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
    repo.new()
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

    # An orphan native pin that no manifest references.
    sid = default_store().system(system).branches["main"]
    backend.pin(locator, {"snapshot_id": sid}, "orphan0badid")
    assert "orphan0badid" in backend.list_pins(locator)

    dry = repo.gc(dry_run=True)
    assert "orphan0badid" in dry.unpinned.get("memory", [])
    assert "orphan0badid" in backend.list_pins(locator)  # dry-run kept it

    repo.gc(dry_run=False)
    assert "orphan0badid" not in backend.list_pins(locator)


def _someone_else_commits(repo: Repo, key: str, state: dict) -> None:
    """Rewrite `key`'s committed manifest as another workspace's commit would."""
    from tether.manifest import write_object

    m = repo.objects[key]
    write_object(repo.root, m.with_pin(state=state, pin=m.pin, recoverable=True))


def test_stale_working_copy_blocks_writes(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    repo.commit("baseline")
    repo.new()
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

    # Track-policy objects write to the base and are never stale.
    tracked = f"sys-{uuid.uuid4().hex[:8]}"
    default_store().system(tracked)
    reloaded.add("t", "memory", {"system": tracked}, policy=Policy(write="track"))
    reloaded.commit("track")
    reloaded.new()
    _someone_else_commits(
        reloaded, "t", {"snapshot_id": default_store().write(tracked, "main", {"x": 1})}
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


def test_track_mode_uses_base_branch(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    _mem_object(repo)
    # Switch policy to track before committing.
    repo.objects["db"].policy = Policy(write="track")
    repo.commit("baseline")
    repo.new()
    assert repo.workspace.working_refs["db"] == "main"


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

    plan = repo.plan_new()
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
    assert isinstance(ro, MemoryHandle) and ro.read_only and ro.ref == "main"
    assert not any(b.startswith("tether.ws.") for b in branches)
    assert next(o for o in repo.status().objects if o.key == "db").changed is False
    assert repo.commit("nothing").pinned == {}  # unchanged: no branch, no new pin
    assert "db" in repo.workspace.pending_forks  # auto_fork is off; still pending

    # The first writable open forks from the pin; later opens reuse the branch.
    handle = repo.open("db")
    assert isinstance(handle, MemoryHandle)
    wref = repo.workspace.working_refs["db"]
    assert handle.ref == wref and wref in branches and wref.startswith("tether.ws.")
    assert not repo.workspace.pending_forks.get("db")
    again = repo.open("db")
    assert isinstance(again, MemoryHandle) and again.ref == wref
    assert repo.materialize_fork("db") == wref  # idempotent

    # Removing a still-pending object leaves nothing behind to gc.
    repo.new()
    assert "db" in repo.workspace.pending_forks  # re-deferred (branch reset later)
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
    eager.new()
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
    # `new` ran without being asked; the fork itself waits for the first write.
    first = repo.workspace.pending_forks["db"]
    assert first.startswith("tether.ws.")
    assert first not in default_store().system(system).branches
    handle = repo.open("db")
    assert isinstance(handle, MemoryHandle) and handle.ref == first
    default_store().write(system, first, {"x": 1})
    repo.commit("update")
    # Deferred again after the commit; the branch keeps the deterministic name
    # and is reset to the new pin on the next writable open.
    assert repo.workspace.pending_forks["db"] == first
    repo.open("db")
    assert repo.workspace.working_refs["db"] == first
    assert default_store().read(system, first) == {"x": 1}
    assert not repo.is_stale()

    # eager mode creates every branch during new/commit, as before.
    repo.config.new_fork = "eager"
    default_store().write(system, first, {"x": 2})
    repo.commit("eager")
    assert (
        repo.workspace.working_refs["db"] == first and not repo.workspace.pending_forks
    )


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
    repo.new(eager=True)
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

    new_plan = repo.plan_new()
    (fork,) = [a for a in new_plan.actions if a.op == "fork"]
    assert fork.params == {"state": {"snapshot_id": s1}}
    repo.new(eager=True)
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
    stale = repo.plan_commit("next")
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
        policy=Policy(write="track"),
    )
    plan = repo.plan_new()
    assert plan.is_empty  # nothing committed yet
    assert any("nothing committed yet" in n for n in plan.notes)

    res = repo.commit("baseline")
    plan = repo.plan_new()
    ops = {a.key: a.op for a in plan.actions}
    assert ops == {"db": "defer-fork", "tracked": "track"}
    assert plan.is_empty  # nothing is written by a lazy new
    assert (
        "first writable open" in next(a for a in plan.actions if a.key == "db").detail
    )
    plan_eager = repo.plan_new(eager=True)
    assert {a.key: a.op for a in plan_eager.actions} == {
        "db": "fork",
        "tracked": "track",
    }
    assert not plan_eager.is_empty
    assert not any(
        b.startswith("tether.ws.") for b in default_store().system(system).branches
    )

    repo.apply_new(Plan.from_json(plan.to_json()))
    assert repo.workspace.working_refs["tracked"] == "main"
    assert repo.workspace.pending_forks["db"].startswith("tether.ws.")
    assert not any(
        b.startswith("tether.ws.") for b in default_store().system(system).branches
    )
    repo.apply_new(Plan.from_json(plan_eager.to_json()))
    assert repo.workspace.working_refs["db"].startswith("tether.ws.")
    assert not repo.workspace.pending_forks

    # Planning against a revision reads the manifests there without moving.
    assert res.vcs_commit is not None
    plan_at = repo.plan_new(res.vcs_commit)
    assert plan_at.context["rev"] == res.vcs_commit
    assert {a.key for a in plan_at.actions} == {"db", "tracked"}


def test_gc_prunes_stray_branches_only_when_nothing_is_lost(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    s1 = store.write(system, "main", {"v": 1})
    repo.commit("baseline")  # pins s1
    s2 = store.write(system, "main", {"v": 2})
    repo.commit("update")  # pins s2; main head is s2
    repo.new(eager=True)
    mine = repo.workspace.working_refs["db"]
    branches = store.system(system).branches
    s3 = store.write(system, "main", {"v": 3})  # main moved on; s3 is not pinned

    # Stray branches of dead workspaces, in every situation the rule covers.
    branches["tether.ws.aaaa0001.db"] = s3  # equals base head: safe
    branches["tether.ws.aaaa0002.db"] = s1  # pinned by the first commit: safe
    branches["tether.ws.aaaa0003.db"] = s2
    store.write(system, "tether.ws.aaaa0003.db", {"v": 99})  # unpinned writes: keep
    branches["tether.ws.cafef00d.db"] = s2  # live workspace: never considered
    branches["feature-x"] = s2  # not a tether branch: never considered

    # Default gc never touches branches.
    plan = repo.plan_gc()
    assert not [a for a in plan.actions if a.op in ("delete-branch", "keep-branch")]

    plan = repo.plan_gc(prune_workspaces=True, keep_workspaces={"cafef00d"})
    by_ref = {a.target: a for a in plan.actions if a.op.endswith("-branch")}
    assert by_ref["tether.ws.aaaa0001.db"].op == "delete-branch"
    assert "equals the base branch" in by_ref["tether.ws.aaaa0001.db"].detail
    assert by_ref["tether.ws.aaaa0002.db"].op == "delete-branch"
    assert "head is pinned" in by_ref["tether.ws.aaaa0002.db"].detail
    assert by_ref["tether.ws.aaaa0003.db"].op == "keep-branch"
    assert "unpinned writes" in by_ref["tether.ws.aaaa0003.db"].detail
    assert "tether.ws.cafef00d.db" not in by_ref and "feature-x" not in by_ref
    assert mine not in by_ref  # in use by this workspace
    assert len(plan.writes) == 2  # keep-branch is not a write

    report = repo.gc(dry_run=False, prune_workspaces=True, keep_workspaces={"cafef00d"})
    assert "tether.ws.aaaa0001.db" not in branches
    assert "tether.ws.aaaa0002.db" not in branches
    assert "tether.ws.aaaa0003.db" in branches  # kept: has data
    assert "tether.ws.cafef00d.db" in branches and "feature-x" in branches
    assert mine in branches
    assert report.kept_working_refs == {"db": ["tether.ws.aaaa0003.db"]}

    # --force-prune deletes the one with data too, and says so.
    plan = repo.plan_gc(prune_workspaces=True, force_prune=True)
    (forced,) = [a for a in plan.actions if a.target == "tether.ws.aaaa0003.db"]
    assert forced.op == "delete-branch" and forced.params["forced"] is True
    assert "FORCED" in forced.detail
    repo.apply_gc(plan)
    assert "tether.ws.aaaa0003.db" not in branches
    assert "tether.ws.cafef00d.db" not in branches  # no keep list this time
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
    branches["tether.ws.aaaa0001.db"] = s1  # the only thing keeping s1 alive

    plan = repo.plan_gc(prune_workspaces=True)
    (a,) = [x for x in plan.actions if x.target == "tether.ws.aaaa0001.db"]
    assert a.op == "keep-branch" and "pin-less recorded state" in a.detail

    # A backend whose branches *are* the storage is never pruned without force.
    backend = repo.backend_for("memory")
    monkeypatch.setattr(
        backend, "capabilities", backend.capabilities | Capability.BRANCH_IS_STORAGE
    )
    branches["tether.ws.aaaa0002.db"] = branches["main"]  # would otherwise be safe
    plan = repo.plan_gc(prune_workspaces=True)
    ops = {x.target: x for x in plan.actions if x.op.endswith("-branch")}
    assert ops["tether.ws.aaaa0002.db"].op == "keep-branch"
    assert "branch is storage" in ops["tether.ws.aaaa0002.db"].detail
    plan = repo.plan_gc(prune_workspaces=True, force_prune=True)
    assert all(
        x.op == "delete-branch" for x in plan.actions if x.op.endswith("-branch")
    )


def test_gc_forgets_removed_objects_refs_without_deleting(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    store = default_store()
    repo.commit("baseline")
    repo.new(eager=True)
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

    # Re-register the object: the stray branch becomes this workspace's orphan
    # and --prune-workspaces evaluates it like any other.
    repo.add("db", "memory", {"system": system, "branch": "main"})
    plan = repo.plan_gc(prune_workspaces=True)
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
