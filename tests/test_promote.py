from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from tether.backends.base import Capability
from tether.backends.memory import default_store
from tether.errors import BackendError, ConfigError, MergeConflict, StalePlanError
from tether.handles import MemoryHandle
from tether.plan import Plan
from tether.repo import Repo


def _mem(repo: Repo, key: str = "db") -> str:
    store = default_store()
    name = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(name)
    store.write(name, "main", {"a": 1})
    repo.add(key, "memory", {"system": name, "branch": "main"})
    return name


def _forked(repo: Repo, key: str = "db") -> str:
    """Commit, `new`, and open writable so the working branch exists."""
    repo.commit("baseline")
    repo.new(bookmark="work")
    handle = repo.open(key)
    assert isinstance(handle, MemoryHandle)
    return handle.ref


# --------------------------------------------------------------------------- #
# memory backend: the reference semantics
# --------------------------------------------------------------------------- #
def test_memory_backend_promote_and_merge() -> None:
    from tether.backends.memory import MemoryBackend

    store = default_store()
    b = MemoryBackend(store)
    name = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(name)
    s1 = store.write(name, "main", {"a": 1})
    loc = {"system": name, "branch": "main"}
    assert Capability.PROMOTE in b.capabilities and Capability.MERGE in b.capabilities

    # Fork, write on the fork: main is an ancestor -> fast-forward.
    b.fork(loc, {"snapshot_id": s1}, "tether.ws.x.db")
    s2 = store.write(name, "tether.ws.x.db", {"a": 1, "b": 2})
    assert b.ancestor_of(loc, {"snapshot_id": s1}, "tether.ws.x.db") is True
    assert b.ancestor_of(loc, {"snapshot_id": s2}, {"snapshot_id": s1}) is False
    assert b.promote(loc, "tether.ws.x.db") == {"snapshot_id": s2}
    assert store.system(name).branches["main"] == s2
    assert b.promote(loc, "tether.ws.x.db") == {"snapshot_id": s2}  # idempotent

    # Both sides move on different keys: promote refuses, merge combines.
    s3 = store.write(name, "tether.ws.x.db", {"a": 1, "b": 2, "c": 3})
    s4 = store.write(name, "main", {"a": 9, "b": 2})
    with pytest.raises(BackendError, match="not an ancestor"):
        b.promote(loc, "tether.ws.x.db")
    merged = b.merge(loc, "tether.ws.x.db", "merge it")
    assert store.read(name, "main") == {"a": 9, "b": 2, "c": 3}
    assert set(store.system(name).parents[merged["snapshot_id"]]) == {s3, s4}

    # Same key changed differently on both sides: conflict, main untouched.
    store.write(name, "tether.ws.x.db", {"a": 1, "b": 2, "c": 3, "k": "fork"})
    head = store.write(name, "main", {"a": 9, "b": 2, "c": 3, "k": "main"})
    with pytest.raises(MergeConflict) as exc:
        b.merge(loc, "tether.ws.x.db", "boom")
    assert exc.value.conflicts == ["k"]
    assert store.system(name).branches["main"] == head


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #
def test_fork_points_are_recorded_by_both_fork_paths(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    s1 = default_store().system(system).branches["main"]
    repo.commit("baseline")

    repo.new(bookmark="work")  # lazy: no fork point until the branch exists
    assert "db" not in repo.workspace.fork_points
    repo.open("db")
    assert repo.workspace.fork_points["db"] == {"snapshot_id": s1}

    repo.new(eager=True)  # eager: recorded during new
    assert repo.workspace.fork_points["db"] == {"snapshot_id": s1}
    repo.remove("db")
    assert "db" not in repo.workspace.fork_points


def test_promote_fast_forward_merge_and_conflict(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    store = default_store()
    branches = store.system(system).branches
    wref = _forked(repo)

    # Nothing written yet: base already at the target.
    plan = repo.plan_promote()
    assert plan.is_empty and any("already at the target" in n for n in plan.notes)

    # Writes on the fork are landed once committed: the trunk moves to the
    # bookmark's commit, whose manifest must describe what the base holds.
    s2 = store.write(system, wref, {"a": 1, "b": 2})
    plan = repo.plan_promote()
    (a,) = plan.actions
    assert a.op == "refuse" and "writes since the last commit" in a.detail
    assert plan.is_empty
    repo.commit("b")
    # Committed; main unchanged -> fast-forward.
    plan = repo.plan_promote()
    (a,) = plan.actions
    assert a.op == "fast-forward" and "base unchanged since fork" in a.detail
    assert a.params["fork_point"] == {"snapshot_id": branches["main"]}
    assert branches["main"] != s2
    report = repo.apply_promote(Plan.from_json(plan.to_json()))
    assert report.fast_forwarded == {"db": {"snapshot_id": s2}}
    assert branches["main"] == s2
    assert repo.workspace.fork_points["db"] == {"snapshot_id": s2}
    assert repo.promote().skipped == ["db"]  # idempotent

    # Both sides move on different keys -> merge; the fork is reset onto the
    # merge result so the next commit pins what main now holds.
    store.write(system, wref, {"a": 1, "b": 2, "c": 3})
    repo.commit("c")
    store.write(system, "main", {"a": 9, "b": 2})
    plan = repo.plan_promote()
    (a,) = plan.actions
    assert a.op == "merge" and "base moved since fork" in a.detail
    report = repo.apply_promote(plan)
    merged = report.merged["db"]
    assert store.read(system, "main") == {"a": 9, "b": 2, "c": 3}
    assert branches[wref] == merged["snapshot_id"]  # fork reset onto the merge
    assert repo.workspace.fork_points["db"] == merged
    assert report.trunk_moved is None  # the commit does not describe main yet
    res = repo.commit("after merge")
    assert res.pinned["db"] is not None and repo.objects["db"].state == merged
    # Commit, then promote again: nothing to write, and the trunk moves.
    report = repo.promote()
    assert report.skipped == ["db"] and report.trunk_moved == res.vcs_commit
    assert repo.vcs.bookmarks()["main"] == res.vcs_commit

    # Same key on both sides -> conflict: reported, nothing written, no raise.
    store.write(system, wref, {**store.read(system, wref), "k": "fork"})
    repo.commit("k")
    head = store.write(system, "main", {**store.read(system, "main"), "k": "main"})
    report = repo.promote()
    assert report.conflicts == {"db": ["k"]}
    assert "db" in report.refused and not report.merged
    assert branches["main"] == head

    # Strategies: ff refuses the divergence; merge refuses a plain fast-forward.
    plan = repo.plan_promote(strategy="ff")
    (a,) = plan.actions
    assert a.op == "refuse" and "strategy=ff" in a.detail
    store.write(system, "main", store.read(system, wref))  # realign main to the fork
    branches["main"] = branches[wref]
    repo.workspace.fork_points["db"] = {"snapshot_id": branches["main"]}
    store.write(system, wref, {**store.read(system, wref), "z": 1})
    repo.commit("z")
    plan = repo.plan_promote(strategy="merge")
    (a,) = plan.actions
    assert a.op == "merge" and "base unchanged since fork" in a.detail
    with pytest.raises(ConfigError):
        repo.plan_promote(strategy="rebase")
    with pytest.raises(ConfigError):
        repo.plan_promote(["nope"])


def test_promote_lands_only_committed_states(vcs_root: Path) -> None:
    """The trunk moves to the bookmark's commit, so what lands must be what
    that commit records: a fork holding writes since the last commit is
    refused, and nothing -- neither the store's base nor the trunk -- moves.
    (r7 / r2B: the store's main ended at the uncommitted head while the
    trunk commit recorded the committed state.)"""
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    store = default_store()
    wref = _forked(repo)
    store.write(system, wref, {"x": "committed"})
    committed = repo.commit("feat write").vcs_commit
    store.write(system, wref, {"x": "UNCOMMITTED"})
    main_before = repo.vcs.bookmarks()["main"]

    plan = repo.plan_promote()
    (a,) = plan.actions
    assert a.op == "refuse" and "writes since the last commit" in a.detail
    assert "`tether commit` them first" in a.detail
    report = repo.apply_promote(plan)
    assert "db" in report.refused and not report.fast_forwarded
    assert report.trunk_moved is None
    assert store.read(system, "main") == {"a": 1}
    assert repo.vcs.bookmarks()["main"] == main_before
    assert not [e for e in repo.ops() if e.command == "promote"]
    # Committed, the same head lands, and the trunk follows.
    later = repo.commit("feat write 2").vcs_commit
    assert later != committed
    report = repo.promote()
    assert store.read(system, "main") == {"x": "UNCOMMITTED"}
    assert report.trunk_moved == later


def test_merges_run_before_fast_forwards(vcs_root: Path) -> None:
    """A merge is what can still stop at apply (a conflict); a fast-forward
    that has landed cannot be taken back. Merges go first, and when one
    stops, the fast-forwards are held so the bookmark does not land half.
    (r13: a fast-forward landed, then the other system's merge conflicted.)"""
    repo = Repo.init(vcs_root)
    store = default_store()
    db = _mem(repo, "db")
    db2 = _mem(repo, "db2")
    repo.commit("baseline")
    repo.new(bookmark="feat", eager=True)
    refs = dict(repo.workspace.working_refs)
    store.write(db, refs["db"], {"a": 1, "k": "feat"})
    store.write(db2, refs["db2"], {"a": 1, "k": "feat"})
    repo.commit("feat writes")
    # db2's base moved on the same key: its merge will conflict; db's is a
    # clean fast-forward.
    store.write(db2, "main", {"a": 1, "k": "trunk"})
    db_main_before = store.system(db).branches["main"]

    plan = repo.plan_promote()
    ops = {a.key: a.op for a in plan.actions}
    assert ops == {"db": "fast-forward", "db2": "merge"}
    report = repo.apply_promote(plan)
    assert report.conflicts == {"db2": ["k"]} and "db2" in report.refused
    assert "db" in report.held and "merge of db2 did not land" in report.held["db"]
    assert not report.fast_forwarded and report.trunk_moved is None
    assert store.system(db).branches["main"] == db_main_before  # not landed
    assert store.read(db2, "main") == {"a": 1, "k": "trunk"}


def test_a_write_landing_on_the_fork_during_a_merge_is_kept(vcs_root: Path) -> None:
    """After a merge the fork is reset onto the merge result -- from the head
    the plan reviewed. A write that lands on the fork in between is not
    discarded by the reset: the branch is left alone and reported, so the
    next commit pins it for another merge. (r11: the reset was
    unconditional and the late write's snapshot lost its only ref.)"""
    from tether.backends.memory import MemoryBackend

    repo = Repo.init(vcs_root)
    system = _mem(repo)
    store = default_store()
    wref = _forked(repo)
    store.write(system, wref, {"a": 1, "feat": 1})
    repo.commit("feat write")
    store.write(system, "main", {"a": 1, "trunk": 1})  # a merge, then
    plan = repo.plan_promote()
    assert [a.op for a in plan.actions] == ["merge"]

    real_merge = MemoryBackend.merge

    def merge_with_a_concurrent_write(self, locator, source, message, **kw):
        store.write(system, wref, {**store.read(system, wref), "late": 1})
        return real_merge(self, locator, source, message, **kw)

    MemoryBackend.merge = merge_with_a_concurrent_write  # type: ignore[method-assign]
    try:
        report = repo.apply_promote(plan)
    finally:
        MemoryBackend.merge = real_merge  # type: ignore[method-assign]
    merged = report.merged["db"]
    assert store.read(system, "main") == {"a": 1, "feat": 1, "trunk": 1}
    assert (
        "db" in report.kept_forks
        and "gained writes during the merge" in (report.kept_forks["db"])
    )
    assert store.read(system, wref) == {"a": 1, "feat": 1, "late": 1}  # kept
    assert store.system(system).branches[wref] != merged["snapshot_id"]
    assert repo.workspace.fork_points["db"] != merged  # the fork was not moved
    # The late write is committed and merged like any other.
    repo.commit("late")
    report = repo.promote()
    assert store.read(system, "main") == {"a": 1, "feat": 1, "trunk": 1, "late": 1}


def test_promote_refuses_a_base_that_moved_under_the_apply(vcs_root: Path) -> None:
    """The plan's `base_state` is re-checked before the first action; a commit
    that lands on the base *after* that check and before the move is caught
    by the backend's conditional move (`expected`), and refused rather than
    overwritten."""
    from tether.backends.memory import MemoryBackend

    repo = Repo.init(vcs_root)
    system = _mem(repo)
    store = default_store()
    wref = _forked(repo)
    store.write(system, wref, {"a": 1, "b": 2})
    repo.commit("b")
    plan = repo.plan_promote()
    real_promote = MemoryBackend.promote

    def promote_after_a_concurrent_commit(self, locator, source, **kw):
        store.write(system, "main", {"a": 1, "concurrent": True})
        return real_promote(self, locator, source, **kw)

    MemoryBackend.promote = promote_after_a_concurrent_commit  # type: ignore[method-assign]
    try:
        report = repo.apply_promote(plan)
    finally:
        MemoryBackend.promote = real_promote  # type: ignore[method-assign]
    assert "db" in report.refused and "expected" in report.refused["db"]
    assert not report.fast_forwarded and report.trunk_moved is None
    assert store.read(system, "main") == {"a": 1, "concurrent": True}  # kept


def test_promote_never_moves_the_trunk_backwards(vcs_root: Path) -> None:
    """The trunk bookmark advanced (an unrelated dataset commit) after the
    feature bookmark forked. Landing the feature would set `main` to the
    feature's commit and drop the newer one off `main`: refused at plan, with
    the manifests to merge first; naming keys still lands a subset."""
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    other = _mem(repo, "other")
    store = default_store()
    wref = _forked(repo)  # bookmark `work`, forked from the baseline
    store.write(system, wref, {"a": 1, "b": 2})
    repo.commit("feature work")
    feature_commit = repo.vcs.bookmarks()["work"]

    # Meanwhile main moves on: an unrelated object changes on the trunk.
    trunk_checkout = Repo.find(vcs_root)
    trunk_checkout.new("main")
    store.write(other, "main", {"x": 1})
    trunk_checkout.commit("unrelated on main")
    main_commit = trunk_checkout.vcs.bookmarks()["main"]
    assert not trunk_checkout.vcs.is_ancestor(main_commit, feature_commit)

    repo.new("work")
    plan = repo.plan_promote()
    assert {a.op for a in plan.actions if a.key} == {"refuse"}
    assert any("backwards or sideways" in n for n in plan.notes)
    report = repo.apply_promote(plan)
    assert not report.fast_forwarded and report.trunk_moved is None
    assert repo.vcs.bookmarks()["main"] == main_commit  # untouched
    assert store.read(system, "main") == {"a": 1}  # nothing landed

    # A subset lands the data and leaves the trunk where it is, as always.
    report = repo.promote(["db"])
    assert report.fast_forwarded and report.trunk_moved is None
    assert store.read(system, "main") == {"a": 1, "b": 2}
    assert repo.vcs.bookmarks()["main"] == main_commit


def test_reusing_a_branch_keeps_its_fork_point(vcs_root: Path) -> None:
    """`commit` then `new work` again reuses the branch. Its fork point is
    where it diverged from main, not its own head: the next promote is a
    fast-forward, not a spurious merge that claims 'base moved'."""
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    store = default_store()
    wref = _forked(repo)
    fork_point = dict(repo.workspace.fork_points["db"])
    store.write(system, wref, {"a": 1, "b": 2})
    repo.commit("v2 on work")
    repo.new("work")  # reuse: the branch sits at its pin
    assert repo.workspace.fork_points["db"] == fork_point  # unchanged
    store.write(system, wref, {"a": 1, "b": 2, "c": 3})
    repo.commit("v3 on work")
    plan = repo.plan_promote()
    (a,) = plan.actions
    assert a.op == "fast-forward" and "base unchanged since fork" in a.detail
    repo.apply_promote(plan)
    assert store.read(system, "main") == {"a": 1, "b": 2, "c": 3}


def test_promote_refuses_when_backend_cannot(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    store = default_store()
    wref = _forked(repo)
    store.write(system, wref, {"a": 1, "b": 2})
    repo.commit("b")
    backend = repo.backend_for("memory")

    # No MERGE: divergence is refused with the backend's recipe.
    monkeypatch.setattr(
        backend, "capabilities", backend.capabilities & ~Capability.MERGE
    )
    monkeypatch.setattr(backend, "PROMOTE_HINT", "do it by hand")
    store.write(system, "main", {"a": 9})
    plan = repo.plan_promote()
    (a,) = plan.actions
    assert a.op == "refuse" and "moved since the fork" in a.detail
    assert "cannot merge" in a.detail and "do it by hand" in a.detail
    assert plan.is_empty  # refuse is not a write
    report = repo.apply_promote(plan, verify=False)
    assert "db" in report.refused and not report.fast_forwarded

    # Neither capability: even a clean fast-forward is refused.
    monkeypatch.setattr(
        backend, "capabilities", backend.capabilities & ~Capability.PROMOTE
    )
    store.system(system).branches["main"] = repo.workspace.fork_points["db"][
        "snapshot_id"
    ]
    (a,) = repo.plan_promote().actions
    assert a.op == "refuse" and "cannot move a branch" in a.detail


def test_promote_lands_a_bookmark_whole_or_not_at_all(vcs_root: Path) -> None:
    """One system that cannot move its branch holds the others back: nothing
    moves, the report says what would have, the trunk stays. Naming keys lands
    a subset on purpose; an unchanged non-promotable system does not block."""
    from tether.backends.base import register_backend
    from tether.backends.memory import MemoryBackend

    class StuckBackend(MemoryBackend):
        kind = "stuck"
        capabilities = MemoryBackend.capabilities & ~(
            Capability.PROMOTE | Capability.MERGE
        )
        PROMOTE_HINT = "copy the rows by hand"

    register_backend("stuck", lambda config: StuckBackend(store=default_store()))

    repo = Repo.init(vcs_root)
    store = default_store()
    movable = _mem(repo, "db")
    stuck = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(stuck)
    store.write(stuck, "main", {"rows": 1})
    repo.add("tbl", "stuck", {"system": stuck, "branch": "main"})
    repo.commit("baseline")
    repo.new(bookmark="work", eager=True)
    db_ref = repo.workspace.working_refs["db"]
    tbl_ref = repo.workspace.working_refs["tbl"]

    # Only the movable one changed: the stuck system is not in the way.
    store.write(movable, db_ref, {"a": 2})
    c1 = repo.commit("db only").vcs_commit
    report = repo.promote()
    assert set(report.fast_forwarded) == {"db"}
    assert (
        store.system(movable).branches["main"] == store.system(movable).branches[db_ref]
    )
    assert not report.refused and not report.held and report.trunk_moved == c1

    # Both changed: the stuck one is refused, so the movable one is held.
    store.write(movable, db_ref, {"a": 3})
    store.write(stuck, tbl_ref, {"rows": 2})
    repo.commit("both")
    main_db_before = store.system(movable).branches["main"]
    plan = repo.plan_promote()
    assert {a.op for a in plan.actions} == {"refuse", "hold"} and plan.is_empty
    report = repo.apply_promote(plan, verify=False)
    assert "tbl" in report.refused and "copy the rows by hand" in report.refused["tbl"]
    assert "db" in report.held and "would fast-forward" in report.held["db"]
    assert not report.fast_forwarded and report.trunk_moved is None
    assert store.system(movable).branches["main"] == main_db_before  # untouched

    # Naming the key is the user's choice to land a subset.
    report = repo.promote(keys=["db"])
    assert "db" in report.fast_forwarded and not report.held
    assert report.trunk_moved is None  # tbl still has not landed


def test_promote_rev_track_and_stale(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    store = default_store()
    branches = store.system(system).branches
    tracked = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(tracked)
    repo.add("tracked", "memory", {"system": tracked})
    wref = _forked(repo)
    store.write(system, wref, {"a": 1, "b": 2})
    c2 = repo.commit("fork work").vcs_commit  # pins the fork's state
    assert c2 is not None

    # --rev: promote what a dataset commit pinned; no fork point -> ancestry check.
    # `tracked` was never written to on this bookmark: its branch sits at the
    # pin, so there is nothing to move.
    plan = repo.plan_promote(rev=c2)
    ops = {a.key: a.op for a in plan.actions}
    assert ops.get("db") == "fast-forward" and ops.get("tracked") != "fast-forward"
    assert "pin" in plan.actions[0].params["source"]
    report = repo.apply_promote(plan)
    pinned = repo.objects["db"].state
    assert pinned is not None
    assert branches["main"] == pinned["snapshot_id"]
    assert report.fast_forwarded["db"] == pinned

    # A plan goes stale when the base moves after planning.
    store.write(system, wref, {"a": 1, "b": 2, "c": 3})
    repo.commit("c")
    plan = repo.plan_promote()
    store.write(system, "main", {"a": 5})
    with pytest.raises(StalePlanError):
        repo.apply_promote(plan)
    with pytest.raises(ConfigError):
        repo.apply_commit(plan)


# --------------------------------------------------------------------------- #
# git backend: a real fast-forward and a real three-way merge
# --------------------------------------------------------------------------- #
def _git(path: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def test_git_promote_and_merge(tmp_path: Path) -> None:
    from tether.backends.git import GitBackend

    code = tmp_path / "code"
    code.mkdir()
    _git(code, "init", "-q", "-b", "main")
    _git(code, "config", "user.email", "c@o.de")
    _git(code, "config", "user.name", "coder")
    (code / "a.py").write_text("a = 1\n")
    _git(code, "add", "-A")
    _git(code, "commit", "-qm", "v0")
    b = GitBackend()
    loc = {"path": str(code)}
    base = b.fingerprint(loc, None)

    # Fork, commit on the fork, fast-forward main (which is checked out).
    b.fork(loc, base, "tether.ws.x.code")
    _git(code, "checkout", "-q", "tether.ws.x.code")
    (code / "b.py").write_text("b = 2\n")
    _git(code, "add", "-A")
    _git(code, "commit", "-qm", "fork work")
    fork_sha = _git(code, "rev-parse", "HEAD")
    _git(code, "checkout", "-q", "main")
    assert b.ancestor_of(loc, base, "tether.ws.x.code") is True
    new = b.promote(loc, "tether.ws.x.code")
    assert new["sha"] == fork_sha == _git(code, "rev-parse", "main")

    # Diverge on different files: promote refuses, merge makes a merge commit.
    _git(code, "checkout", "-q", "tether.ws.x.code")
    (code / "c.py").write_text("c = 3\n")
    _git(code, "add", "-A")
    _git(code, "commit", "-qm", "more fork work")
    _git(code, "checkout", "-q", "main")
    (code / "a.py").write_text("a = 10\n")
    _git(code, "add", "-A")
    _git(code, "commit", "-qm", "main moved")
    with pytest.raises(BackendError, match="not an ancestor"):
        b.promote(loc, "tether.ws.x.code")
    # Merge from a *state*: the fork commit the plan reviewed. A commit added
    # to the fork after review is not part of the merge.
    reviewed = b.fingerprint(loc, "tether.ws.x.code")
    _git(code, "checkout", "-q", "tether.ws.x.code")
    (code / "late.py").write_text("late = 1\n")
    _git(code, "add", "-A")
    _git(code, "commit", "-qm", "after review")
    _git(code, "checkout", "-q", "main")
    merged = b.merge(loc, reviewed, "merge the fork")
    assert merged["sha"] == _git(code, "rev-parse", "main")
    assert _git(code, "log", "-1", "--format=%P").count(" ") == 1  # two parents
    assert (code / "c.py").exists() and (code / "a.py").read_text() == "a = 10\n"
    assert not (code / "late.py").exists()

    # Same file on both sides: conflict, aborted, tree clean, main unchanged.
    _git(code, "checkout", "-q", "tether.ws.x.code")
    (code / "a.py").write_text("a = 'fork'\n")
    _git(code, "add", "-A")
    _git(code, "commit", "-qm", "fork edits a")
    _git(code, "checkout", "-q", "main")
    (code / "a.py").write_text("a = 'main'\n")
    _git(code, "add", "-A")
    _git(code, "commit", "-qm", "main edits a")
    head = _git(code, "rev-parse", "main")
    with pytest.raises(MergeConflict) as exc:
        b.merge(loc, "tether.ws.x.code", "boom")
    assert exc.value.conflicts == ["a.py"]
    assert _git(code, "rev-parse", "main") == head
    assert _git(code, "status", "--porcelain") == ""

    # Promoting while another branch is checked out uses update-ref.
    _git(code, "checkout", "-q", "-b", "elsewhere")
    _git(code, "branch", "-f", "tether.ws.x.code", "main")
    _git(code, "checkout", "-q", "tether.ws.x.code")
    (code / "d.py").write_text("d = 4\n")
    _git(code, "add", "-A")
    _git(code, "commit", "-qm", "d")
    _git(code, "checkout", "-q", "elsewhere")
    promoted = b.promote({"path": str(code), "ref": "main"}, "tether.ws.x.code")
    assert promoted["sha"] == _git(code, "rev-parse", "tether.ws.x.code")
    with pytest.raises(BackendError, match="check out main"):
        b.merge({"path": str(code), "ref": "main"}, "tether.ws.x.code", "x")


# --------------------------------------------------------------------------- #
# icechunk: fast-forward only
# --------------------------------------------------------------------------- #
def test_icechunk_promote(tmp_path: Path, vcs_root: Path) -> None:
    ic = pytest.importorskip("icechunk")
    zarr = pytest.importorskip("zarr")

    def commit(uri: str, branch: str, value: int) -> None:
        r = ic.Repository.open(ic.local_filesystem_storage(uri))
        session = r.writable_session(branch)
        zarr.open_group(store=session.store, mode="a").attrs["v"] = value
        session.commit(f"v={value}")

    path = tmp_path / "store"
    path.mkdir()
    r = ic.Repository.create(ic.local_filesystem_storage(str(path)))
    session = r.writable_session("main")
    zarr.create_group(store=session.store).attrs["v"] = 0
    session.commit("init")

    repo = Repo.init(vcs_root)
    repo.add("zarr", "icechunk", {"uri": str(path), "branch": "main"})
    repo.commit("baseline")
    repo.new(bookmark="work")
    handle = repo.open("zarr")
    wref = repo.workspace.working_refs["zarr"]
    assert handle.branch == wref if hasattr(handle, "branch") else True
    commit(str(path), wref, 1)
    repo.commit("v1")

    report = repo.promote()
    r = ic.Repository.open(ic.local_filesystem_storage(str(path)))
    assert r.lookup_branch("main") == r.lookup_branch(wref)
    assert report.fast_forwarded["zarr"] == {"snapshot_id": r.lookup_branch("main")}

    # main moves independently: no merge in Icechunk -> refused with the hint.
    commit(str(path), wref, 2)
    repo.commit("v2")
    commit(str(path), "main", 99)
    plan = repo.plan_promote()
    (a,) = plan.actions
    assert a.op == "refuse" and "Icechunk has no merge" in a.detail


def test_promote_moves_the_trunk_bookmark(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    store = default_store()
    wref = _forked(repo)
    main_before = repo.vcs.bookmarks()["main"]
    store.write(system, wref, {"a": 1})
    c2 = repo.commit("fork work").vcs_commit
    assert (
        repo.vcs.bookmarks()["work"] == c2
        and repo.vcs.bookmarks()["main"] == main_before
    )

    report = repo.promote()
    assert report.fast_forwarded and report.trunk_moved == c2
    assert repo.vcs.bookmarks()["main"] == c2
    assert store.system(system).branches["main"] == store.system(system).branches[wref]
    # Nothing to promote: the trunk stays where it is.
    assert repo.promote().trunk_moved is None
    # Promoting a specific revision moves stores only.
    store.write(system, wref, {"a": 2})
    c3 = repo.commit("more").vcs_commit
    assert repo.promote(rev=c3).trunk_moved is None
    assert repo.vcs.bookmarks()["main"] == c2
