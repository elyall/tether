from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from tether.backends.base import Capability
from tether.backends.memory import default_store
from tether.errors import BackendError, ConfigError, MergeConflict, StalePlanError
from tether.handles import MemoryHandle
from tether.manifest import Policy
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

    # Writes on the fork; main unchanged -> fast-forward.
    s2 = store.write(system, wref, {"a": 1, "b": 2})
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
    store.write(system, "main", {"a": 9, "b": 2})
    plan = repo.plan_promote()
    (a,) = plan.actions
    assert a.op == "merge" and "base moved since fork" in a.detail
    report = repo.apply_promote(plan, verify=False)
    merged = report.merged["db"]
    assert store.read(system, "main") == {"a": 9, "b": 2, "c": 3}
    assert branches[wref] == merged["snapshot_id"]  # fork reset onto the merge
    assert repo.workspace.fork_points["db"] == merged
    res = repo.commit("after merge")
    assert res.pinned["db"] is not None and repo.objects["db"].state == merged

    # Same key on both sides -> conflict: reported, nothing written, no raise.
    store.write(system, wref, {**store.read(system, wref), "k": "fork"})
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
    plan = repo.plan_promote(strategy="merge")
    (a,) = plan.actions
    assert a.op == "merge" and "base unchanged since fork" in a.detail
    with pytest.raises(ConfigError):
        repo.plan_promote(strategy="rebase")
    with pytest.raises(ConfigError):
        repo.plan_promote(["nope"])


def test_promote_refuses_when_backend_cannot(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    store = default_store()
    wref = _forked(repo)
    store.write(system, wref, {"a": 1, "b": 2})
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


def test_promote_rev_track_and_stale(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    store = default_store()
    branches = store.system(system).branches
    tracked = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(tracked)
    repo.add("tracked", "memory", {"system": tracked}, policy=Policy(write="direct"))
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
    merged = b.merge(loc, "tether.ws.x.code", "merge the fork")
    assert merged["sha"] == _git(code, "rev-parse", "main")
    assert _git(code, "log", "-1", "--format=%P").count(" ") == 1  # two parents
    assert (code / "c.py").exists() and (code / "a.py").read_text() == "a = 10\n"

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

    report = repo.promote()
    r = ic.Repository.open(ic.local_filesystem_storage(str(path)))
    assert r.lookup_branch("main") == r.lookup_branch(wref)
    assert report.fast_forwarded["zarr"] == {"snapshot_id": r.lookup_branch("main")}

    # main moves independently: no merge in Icechunk -> refused with the hint.
    commit(str(path), wref, 2)
    commit(str(path), "main", 99)
    plan = repo.plan_promote()
    (a,) = plan.actions
    assert a.op == "refuse" and "Icechunk has no merge" in a.detail


# --------------------------------------------------------------------------- #
# lakeFS / Dolt fakes: merge_into / DOLT_MERGE
# --------------------------------------------------------------------------- #
def test_lakefs_promote_and_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    from lakefs.exceptions import ConflictException  # noqa: F401
    from test_lakefs_backend import FakeLakeFS

    from tether.backends.lakefs import LakeFSBackend

    b = LakeFSBackend()
    f = FakeLakeFS()
    monkeypatch.setattr(b, "_repo", f)
    loc = {"repository": f.create_repo(), "branch": "main"}
    base = b.fingerprint(loc, None)
    b.fork(loc, base, "tether.ws.x.lake")
    repo = f(loc)
    repo.branch("tether.ws.x.lake").stage("data/1", b"x")
    repo.branch("tether.ws.x.lake").commit("fork")
    assert b.ancestor_of(loc, base, "tether.ws.x.lake") is True
    new = b.promote(loc, "tether.ws.x.lake")
    assert new == b.fingerprint(loc, None) and new != base
    assert f.repos[loc["repository"]].commits[new["commit_id"]]["data/1"] == b"x"

    repo.branch("tether.ws.x.lake").stage("data/2", b"y")
    repo.branch("tether.ws.x.lake").commit("fork again")
    repo.branch("main").stage("data/3", b"z")
    repo.branch("main").commit("main moved")
    with pytest.raises(BackendError, match="not an ancestor"):
        b.promote(loc, "tether.ws.x.lake")
    merged = b.merge(loc, "tether.ws.x.lake", "merge")
    tree = f.repos[loc["repository"]].commits[merged["commit_id"]]
    assert {k for k in tree if k.startswith("data/")} == {"data/1", "data/2", "data/3"}

    repo.branch("tether.ws.x.lake").stage("data/k", b"fork")
    repo.branch("tether.ws.x.lake").commit("k")
    repo.branch("main").stage("data/k", b"main")
    repo.branch("main").commit("k")
    with pytest.raises(MergeConflict):
        b.merge(loc, "tether.ws.x.lake", "boom")


def test_dolt_promote_and_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_dolt_backend import FakeDolt

    from tether.backends.dolt import DoltBackend

    b = DoltBackend()
    f = FakeDolt()
    monkeypatch.setattr(b, "_client", f)
    loc = {"database": f.create_db(), "branch": "main", "host": "h"}
    db = f(loc)
    base = b.fingerprint(loc, None)
    b.fork(loc, base, "tether.ws.x.ledger")
    db.commit("tether.ws.x.ledger", "fork", {"t": 5})
    assert b.ancestor_of(loc, base, "tether.ws.x.ledger") is True
    new = b.promote(loc, "tether.ws.x.ledger")
    assert db.branches["main"] == db.branches["tether.ws.x.ledger"]
    assert new["commit"] == db.branches["main"]

    db.commit("tether.ws.x.ledger", "more", {"u": 1})
    db.commit("main", "main moved", {"v": 2})
    with pytest.raises(BackendError, match="not an ancestor"):
        b.promote(loc, "tether.ws.x.ledger")
    merged = b.merge(loc, "tether.ws.x.ledger", "merge")
    assert db.commits[merged["commit"]] == {"t": 5, "u": 1, "v": 2}

    db.commit("tether.ws.x.ledger", "k", {"k": 1})
    db.commit("main", "k", {"k": 2})
    with pytest.raises(MergeConflict) as exc:
        b.merge(loc, "tether.ws.x.ledger", "boom")
    assert exc.value.conflicts == ["k"]


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
