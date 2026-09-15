"""Stores tether creates (`add --create`, `Repo.create`) and, once nothing
references them, reclaims in `gc --delete-stores`."""

from __future__ import annotations

import json
import uuid
import warnings
from pathlib import Path

import pytest
from typer import testing as typer_testing

from tether.backends.memory import default_store
from tether.cli import app
from tether.errors import BackendError, CapabilityError, ConfigError
from tether.experimental.lifecycle import read_created
from tether.handles import MemoryHandle
from tether.manifest import ObjectManifest, read_objects
from tether.repo import Repo

runner = typer_testing.CliRunner()


def _fresh_system() -> str:
    return f"sys-{uuid.uuid4().hex[:8]}"


def _mem(repo: Repo, key: str) -> MemoryHandle:
    handle = repo.open(key)
    assert isinstance(handle, MemoryHandle)
    return handle


# ----------------------------------------------------------------------------- #
# add --create
# ----------------------------------------------------------------------------- #


def test_add_create_makes_owns_and_indexes_the_store(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _fresh_system()
    locator = {"system": system, "branch": "main"}

    m = repo.add("scratch/emb", "memory", locator, create=True)

    assert m.origin == "created"
    backend = repo.backend_for("memory")
    assert backend.owner(locator) == repo.config.dataset_id
    assert backend.is_ref_empty(locator) is True
    # The index lives with the repository, not the checkout, and is untracked.
    (entry,) = repo.created_stores()
    assert entry.kind == "memory" and entry.key == "scratch/emb"
    assert entry.identity == dict(backend.identity(locator))
    assert entry.dataset_id == repo.config.dataset_id
    index = repo.vcs.shared_dir() / "tether-created.jsonl"
    assert index.is_file() and index.resolve() != (vcs_root / ".tether").resolve()
    # The manifest round-trips its origin; the default stays out of the file.
    text = (vcs_root / ".tether" / "objects" / "scratch" / "emb.toml").read_text()
    assert 'origin = "created"' in text
    assert ObjectManifest.from_toml(text).origin == "created"
    plain = ObjectManifest(key="k", kind="memory", locator={}, policy=m.policy)
    assert "origin" not in plain.to_toml()
    # The add op remembers what it made, for `undo`.
    entry_op = repo.ops()[0]
    assert entry_op.command == "add"
    assert entry_op.result["created"]["kind"] == "memory"
    assert entry_op.result["created"]["identity"] == dict(backend.identity(locator))
    st = next(o for o in repo.status().objects if o.key == "scratch/emb")
    assert st.origin == "created"


def test_add_create_refuses_an_existing_store_and_a_kind_without_create(
    vcs_root: Path,
) -> None:
    repo = Repo.init(vcs_root)
    system = _fresh_system()
    default_store().system(system)  # someone else's data
    with pytest.raises(BackendError, match="already exists"):
        repo.add("db", "memory", {"system": system}, create=True)
    assert "db" not in repo.objects and not repo.created_stores()
    # A file *object* (not a directory) at a remote prefix cannot be created.
    with pytest.raises(CapabilityError, match="cannot create"):
        repo.add("blob", "file", {"uri": "s3://bucket/prefix/"}, create=True)


def test_add_create_on_a_bookmark_forks_the_working_branch_right_away(
    vcs_root: Path,
) -> None:
    """After `new`, an added object has no pending fork (`new` already ran).
    A *created* store gets its branch at once, forked from the initial state,
    so the first writable open needs no second `new`."""
    repo = Repo.init(vcs_root)
    repo.new(bookmark="probe")
    system = _fresh_system()
    locator = {"system": system, "branch": "main"}
    repo.add("scratch/probe", "memory", locator, create=True)

    ref = repo.workspace.working_refs["scratch/probe"]
    assert ref == f"tether.ws.{repo.config.dataset_id}.probe"
    assert "scratch/probe" not in repo.workspace.pending_forks
    assert repo.workspace.fork_points["scratch/probe"] == {
        "snapshot_id": f"{system}:s0"
    }
    assert repo.ops()[0].result["fork"] == ref

    handle = _mem(repo, "scratch/probe")
    assert not handle.read_only and handle.ref == ref
    handle.write({"x": 1})
    result = repo.commit("probe write")
    pin = result.pinned["scratch/probe"]
    assert pin is not None
    backend = repo.backend_for("memory")
    # Not empty while the fork and the pin exist; empty once gc ignores them.
    assert backend.is_ref_empty(locator) is False
    assert backend.is_ref_empty(locator, ignoring={ref, pin.ref}) is True
    # main never moved: the store's base is still pristine.
    assert default_store().system(system).branches["main"] == f"{system}:s0"


def test_add_create_on_the_trunk_writes_through_the_base_branch(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _fresh_system()
    repo.add("scratch/emb", "memory", {"system": system}, create=True)
    assert "scratch/emb" not in repo.workspace.working_refs
    handle = _mem(repo, "scratch/emb")
    assert handle.ref == "main" and not handle.read_only


# ----------------------------------------------------------------------------- #
# Repo.create
# ----------------------------------------------------------------------------- #


def test_repo_create_makes_registers_and_opens_in_one_verb(vcs_root: Path) -> None:
    """`Repo.create` is `add(create=True)` + `open`: the whole setup of a
    throwaway environment in one line, and `open` stays a plain read."""
    repo = Repo.init(vcs_root)
    system = _fresh_system()
    handle = repo.create("scratch/emb", "memory", {"system": system})
    assert isinstance(handle, MemoryHandle) and not handle.read_only
    assert repo.objects["scratch/emb"].origin == "created"
    assert (
        repo.backend_for("memory").owner({"system": system}) == repo.config.dataset_id
    )
    assert [c.key for c in repo.created_stores()] == ["scratch/emb"]
    # The same object opens again as any other; a second create is a clash.
    assert _mem(repo, "scratch/emb").ref == handle.ref
    with pytest.raises(ConfigError, match="already exists"):
        repo.create("scratch/emb", "memory", {"system": _fresh_system()})


def test_repo_create_on_a_bookmark_hands_back_the_fork(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    repo.new(bookmark="probe")
    system = _fresh_system()
    handle = repo.create("scratch/probe", "memory", {"system": system})
    assert isinstance(handle, MemoryHandle)
    assert handle.ref == f"tether.ws.{repo.config.dataset_id}.probe"
    handle.write({"x": 1})
    assert repo.commit("probe write").pinned["scratch/probe"] is not None
    # The index remembers which bookmark the store was made on: gc can later
    # tell this clone's branch from another actor's.
    (entry,) = repo.created_stores()
    assert entry.bookmark == "probe"


def test_open_of_an_unknown_key_is_still_an_error(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    with pytest.raises(ConfigError, match="no such object"):
        repo.open("scratch/emb")


def test_add_create_journals_before_it_writes_to_the_store(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Like every store-writing command, `add --create` begins its op before
    the store write: a crash in `backend.create` leaves a started entry."""
    repo = Repo.init(vcs_root)
    system = _fresh_system()
    backend = repo.backend_for("memory")

    def boom(locator: dict, *, owner: str) -> dict:
        raise BackendError("simulated outage", kind="memory")

    monkeypatch.setattr(backend, "create", boom)
    with pytest.raises(BackendError, match="outage"):
        repo.add("scratch/emb", "memory", {"system": system}, create=True)
    (started,) = [e for e in repo.incomplete_ops() if e.command == "add"]
    assert started.incomplete
    assert "scratch/emb" not in repo.objects and not repo.created_stores()


# ----------------------------------------------------------------------------- #
# undo add
# ----------------------------------------------------------------------------- #


def test_undo_add_removes_the_store_it_created_while_empty(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _fresh_system()
    locator = {"system": system, "branch": "main"}
    repo.add("scratch/emb", "memory", locator, create=True)

    report = repo.undo()
    assert report.complete
    assert any("removed the store it created" in line for line in report.restored)
    assert "scratch/emb" not in repo.objects
    assert not repo.created_stores()
    assert system not in default_store().systems
    with pytest.raises(BackendError, match="deleted"):
        repo.backend_for("memory").fingerprint(locator, None)


def test_undo_add_on_a_bookmark_deletes_its_fork_too(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    repo.new(bookmark="probe")
    system = _fresh_system()
    repo.add("scratch/probe", "memory", {"system": system}, create=True)
    assert "scratch/probe" in repo.workspace.working_refs
    report = repo.undo()
    assert report.complete, report.irreversible
    assert system not in default_store().systems
    assert "scratch/probe" not in repo.workspace.working_refs


def test_undo_add_keeps_a_store_that_holds_writes(vcs_root: Path) -> None:
    """`undo add` is for the mistaken add, not a way to lose data: once
    something was written, the store stays and `gc` decides later."""
    repo = Repo.init(vcs_root)
    system = _fresh_system()
    repo.add("scratch/emb", "memory", {"system": system}, create=True)
    _mem(repo, "scratch/emb").write({"x": 1})  # on main: the base moved

    report = repo.undo()
    assert not report.complete
    assert any("holds writes" in line and "gc" in line for line in report.irreversible)
    assert "scratch/emb" not in repo.objects  # the manifest is still undone
    assert system in default_store().systems
    assert [c.key for c in repo.created_stores()] == ["scratch/emb"]  # gc's turn


def test_undo_add_leaves_a_store_whose_marker_is_not_ours(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _fresh_system()
    repo.add("scratch/emb", "memory", {"system": system}, create=True)
    default_store().system(system).owner = "someone-else"
    report = repo.undo()
    assert any("owner marker" in line for line in report.irreversible)
    assert system in default_store().systems


# ----------------------------------------------------------------------------- #
# CLI
# ----------------------------------------------------------------------------- #


def test_cli_add_create_and_status_show_the_origin(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(vcs_root)
    assert runner.invoke(app, ["init"]).exit_code == 0
    system = _fresh_system()
    r = runner.invoke(
        app,
        [
            "add",
            "scratch/emb",
            "--kind",
            "memory",
            "--set",
            f"system={system}",
            "--create",
        ],
    )
    assert r.exit_code == 0, r.output
    assert "added scratch/emb (memory, created)" in r.output
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, r.output
    assert "scratch/emb  [memory/forkable] (created)" in r.output
    r = runner.invoke(app, ["status", "--json"])
    assert json.loads(r.output)["objects"][0]["origin"] == "created"
    # Refused where a store exists: nothing registered, nothing indexed.
    r = runner.invoke(
        app,
        ["add", "again", "--kind", "memory", "--set", f"system={system}", "--create"],
    )
    assert r.exit_code != 0 and "already exists" in r.output
    repo = Repo.find(vcs_root)
    assert "again" not in read_objects(repo.root)
    assert len(read_created(repo.vcs.shared_dir())) == 1


# ----------------------------------------------------------------------------- #
# gc: reclaiming created stores
# ----------------------------------------------------------------------------- #


def _baseline(vcs_root: Path) -> tuple[Repo, str]:
    """A dataset with one adopted object committed on the trunk."""
    repo = Repo.init(vcs_root)
    system = _fresh_system()
    default_store().system(system)
    repo.add("db", "memory", {"system": system, "branch": "main"})
    repo.commit("baseline")
    return repo, system


def _probe_with_created_store(repo: Repo) -> tuple[str, str, str]:
    """On bookmark `probe`: create a store, write, commit. Returns the store's
    system, its fork branch, and the probe commit."""
    repo.new(bookmark="probe")
    system = _fresh_system()
    repo.add(
        "scratch/probe", "memory", {"system": system, "branch": "main"}, create=True
    )
    fork = repo.workspace.working_refs["scratch/probe"]
    _mem(repo, "scratch/probe").write({"x": 1})
    commit = repo.commit("probe write").vcs_commit
    assert commit is not None
    return system, fork, commit


def _drop_bookmark(repo: Repo, root: Path, name: str, commit: str) -> Repo:
    """Leave the bookmark for the trunk and make its commit invisible, as a
    user abandoning an experiment would."""
    import subprocess

    repo.new("main")
    if repo.vcs.kind == "jj":
        subprocess.run(
            ["jj", "abandon", commit], cwd=root, check=True, capture_output=True
        )
    if name in repo.vcs.bookmarks():
        repo.vcs.bookmark_delete(name)
    return Repo.find(root)


def test_gc_reclaims_a_created_store_once_its_fork_is_abandoned(vcs_root: Path) -> None:
    """The story: a probe bookmark creates a store, writes, commits; the
    bookmark is abandoned; `gc --prune-bookmarks` unpins, deletes the fork,
    then deletes the store -- and the world matches the pre-fork listing."""
    repo, _db = _baseline(vcs_root)
    before = set(default_store().systems)
    system, fork, commit = _probe_with_created_store(repo)
    backend = repo.backend_for("memory")
    locator = {"system": system, "branch": "main"}
    pins = {p for p in backend.list_pins(locator)}
    assert len(pins) == 1 and fork in default_store().system(system).branches

    # Referenced (the probe commit names it): nothing about the store is planned.
    plan = repo.plan_gc(prune_bookmarks=True, delete_stores=True)
    assert not [a for a in plan.actions if a.key == "scratch/probe"]
    assert any("still referenced" in n for n in plan.notes)

    repo = _drop_bookmark(repo, vcs_root, "probe", commit)
    assert "scratch/probe" not in repo.objects
    # Without --prune-bookmarks the fork blocks the store: kept, with the reason.
    plan = repo.plan_gc(delete_stores=True)
    ops = [(a.op, a.target) for a in plan.actions if a.key == "scratch/probe"]
    assert ("keep-store", system) in ops
    assert any(a.op == "unpin" for a in plan.actions if a.key == "scratch/probe")
    (kept,) = [a for a in plan.actions if a.op == "keep-store"]
    assert fork in kept.detail and "--prune-bookmarks" in kept.detail
    assert not [a for a in plan.actions if a.op == "delete-store"]

    plan = repo.plan_gc(prune_bookmarks=True, delete_stores=True)
    # (`new main` left this checkout's working ref behind: forgotten as usual.)
    ops = [
        a.op
        for a in plan.actions
        if a.key == "scratch/probe" and a.op != "forget-working-ref"
    ]
    assert ops == ["unpin", "delete-branch", "delete-store"]
    assert plan.actions[-1].op == "delete-store"
    (pre,) = [p for p in plan.preconditions if p.kind == "store_empty"]
    assert set(pre.params["ignoring"]) == {fork, *(f"tether.{p}" for p in pins)}
    delete_branch = next(a for a in plan.actions if a.op == "delete-branch")
    assert "head is pinned" in delete_branch.detail

    report = repo.apply_gc(plan)
    assert report.deleted_stores == {"scratch/probe": system}
    assert not report.kept_stores
    assert set(default_store().systems) == before  # the world as it was
    assert not repo.created_stores()
    with pytest.raises(BackendError, match="deleted"):
        backend.fingerprint(locator, None)

    undo = repo.undo()
    assert not undo.complete
    assert any("created store" in line and system in line for line in undo.irreversible)
    assert system not in default_store().systems  # honestly gone


def test_gc_leaves_created_stores_alone_unless_asked(vcs_root: Path) -> None:
    """Opt-in, and experimental: a deleted store has no `repair`, and gc only
    knows what this clone has fetched. Without `delete_stores` a plain gc
    plans nothing for the store -- not even the unpins inside it (the
    touched-store step is part of the same experimental feature)."""
    repo, _db = _baseline(vcs_root)
    system, fork, commit = _probe_with_created_store(repo)
    repo = _drop_bookmark(repo, vcs_root, "probe", commit)
    plan = repo.plan_gc(prune_bookmarks=True)
    assert not [
        a
        for a in plan.actions
        if a.key == "scratch/probe" and a.op != "forget-working-ref"
    ]
    assert plan.context["delete_stores"] is False
    report = repo.gc(dry_run=False, prune_bookmarks=True)
    assert not report.deleted_stores and not report.kept_stores
    sys_ = default_store().system(system)
    assert system in default_store().systems and fork in sys_.branches
    assert [c.key for c in repo.created_stores()] == ["scratch/probe"]
    # Asked: unpin, delete the fork, delete the store; the indexes forget it.
    report = repo.gc(dry_run=False, prune_bookmarks=True, delete_stores=True)
    assert report.deleted_stores == {"scratch/probe": system}
    assert [t.key for t in repo.touched_stores()] == ["db"]


def test_gc_touches_nothing_in_a_store_where_another_actor_may_be_alive(
    vcs_root: Path,
) -> None:
    """A same-dataset branch of a bookmark this clone never had: another
    clone or environment may have fetched the probe and be working in the
    store. Its commits are not in our history, so nothing else protects it --
    the plan leaves the store whole, our own pins included, until it is
    fetched or `--force-prune` says otherwise."""
    repo, _db = _baseline(vcs_root)
    system, fork, commit = _probe_with_created_store(repo)
    repo = _drop_bookmark(repo, vcs_root, "probe", commit)
    sys_ = default_store().system(system)
    theirs = f"tether.ws.{repo.config.dataset_id}.their-probe"
    sys_.branches[theirs] = sys_.branches["main"]  # same dataset id, unknown bookmark

    plan = repo.plan_gc(prune_bookmarks=True, delete_stores=True)
    (kept,) = [a for a in plan.actions if a.op == "keep-store"]
    assert theirs in kept.detail and "never had" in kept.detail
    assert not [
        a for a in plan.actions if a.op in ("unpin", "delete-branch", "delete-store")
    ]
    repo.apply_gc(plan)
    assert set(sys_.tags) and fork in sys_.branches  # our pin and fork untouched

    # --force-prune is the explicit override: everything goes, marked FORCED.
    forced = repo.plan_gc(prune_bookmarks=True, force_prune=True, delete_stores=True)
    ops = [(a.op, a.target) for a in forced.actions if a.key == "scratch/probe"]
    assert ("delete-branch", theirs) in ops and ("delete-store", system) in ops
    theirs_action = next(a for a in forced.actions if a.target == theirs)
    assert "FORCED" in theirs_action.detail and "never had" in theirs_action.detail
    repo.apply_gc(forced)
    assert system not in default_store().systems


def test_gc_accounts_for_bookmarks_another_live_checkout_of_this_clone_made(
    vcs_root: Path, tmp_path: Path
) -> None:
    """A branch of a bookmark some checkout of *this* clone created (its op
    log says so) is ours to judge under the prune rules, even after the
    bookmark itself is gone."""
    import subprocess

    repo, _db = _baseline(vcs_root)
    other_root = tmp_path / "other-checkout"
    cmd = (
        ["jj", "workspace", "add", str(other_root)]
        if repo.vcs.kind == "jj"
        else ["git", "worktree", "add", "--detach", str(other_root)]
    )
    subprocess.run(cmd, cwd=vcs_root, check=True, capture_output=True)
    other = Repo.find(other_root)
    other.new(bookmark="side")  # logged in the other checkout's ops.jsonl
    other.new(bookmark="side-2")  # ...and left behind for another
    other.vcs.bookmark_delete("side")

    system, _fork, commit = _probe_with_created_store(repo)
    repo = _drop_bookmark(repo, vcs_root, "probe", commit)
    sys_ = default_store().system(system)
    side = f"tether.ws.{repo.config.dataset_id}.side"
    sys_.branches[side] = sys_.branches["main"]  # at the base: nothing written

    plan = repo.plan_gc(prune_bookmarks=True, delete_stores=True)
    side_action = next(a for a in plan.actions if a.target == side)
    assert side_action.op == "delete-branch" and "equals the base" in side_action.detail
    assert plan.actions[-1].op == "delete-store"


def test_gc_keeps_a_created_store_with_a_foreign_branch(vcs_root: Path) -> None:
    repo, _db = _baseline(vcs_root)
    system, _fork, commit = _probe_with_created_store(repo)
    repo = _drop_bookmark(repo, vcs_root, "probe", commit)
    sys_ = default_store().system(system)
    sys_.branches["tether.ws.ffffffff.theirs"] = sys_.branches["main"]

    plan = repo.plan_gc(prune_bookmarks=True, delete_stores=True)
    (kept,) = [a for a in plan.actions if a.op == "keep-store"]
    assert "1 working branch(es) of other datasets" in kept.detail
    assert not [a for a in plan.actions if a.op == "delete-store"]
    # tether's own garbage in it still goes; the store itself stays.
    report = repo.apply_gc(plan)
    assert report.kept_stores["scratch/probe"] == kept.detail
    assert system in default_store().systems
    assert "tether.ws.ffffffff.theirs" in sys_.branches
    assert [c.key for c in repo.created_stores()] == ["scratch/probe"]


def test_gc_keeps_a_created_store_another_live_workspace_still_uses(
    vcs_root: Path, tmp_path: Path
) -> None:
    """An uncommitted `add --create` in another checkout's working tree is a
    reference too: nothing in history names the store, but the store is in
    use."""
    import subprocess

    repo, _db = _baseline(vcs_root)
    other_root = tmp_path / "other-checkout"
    cmd = (
        ["jj", "workspace", "add", str(other_root)]
        if repo.vcs.kind == "jj"
        else ["git", "worktree", "add", "--detach", str(other_root)]
    )
    subprocess.run(cmd, cwd=vcs_root, check=True, capture_output=True)
    other = Repo.find(other_root)
    system = _fresh_system()
    other.add(
        "scratch/theirs", "memory", {"system": system}, create=True
    )  # uncommitted

    repo = Repo.find(vcs_root)
    assert [c.key for c in repo.created_stores()] == ["scratch/theirs"]  # shared index
    plan = repo.plan_gc(prune_bookmarks=True, delete_stores=True)
    assert not [a for a in plan.actions if a.key == "scratch/theirs"]
    assert any("still referenced" in n for n in plan.notes)
    assert system in default_store().systems


def test_gc_never_trusts_a_manifest_that_claims_creation(vcs_root: Path) -> None:
    """A clone can say `origin = "created"` about anything. Without the owner
    marker *and* the index entry, gc plans nothing for the store."""
    repo, _db = _baseline(vcs_root)
    system = _fresh_system()
    default_store().system(system)
    m = repo.add("claimed", "memory", {"system": system})
    forged = ObjectManifest(
        key=m.key, kind=m.kind, locator=m.locator, policy=m.policy, origin="created"
    )
    path = vcs_root / ".tether" / "objects" / "claimed.toml"
    path.write_text(forged.to_toml(), encoding="utf-8")
    repo = Repo.find(vcs_root)
    assert repo.objects["claimed"].origin == "created"
    repo.commit("claims")
    repo.objects.pop("claimed")
    path.unlink()
    repo = Repo.find(vcs_root)
    plan = repo.plan_gc(prune_bookmarks=True, delete_stores=True)
    assert not [a for a in plan.actions if a.op in ("delete-store", "keep-store")]
    assert system in default_store().systems


def test_gc_leaves_a_created_store_whose_marker_is_someone_elses(
    vcs_root: Path,
) -> None:
    repo, _db = _baseline(vcs_root)
    system, _fork, commit = _probe_with_created_store(repo)
    repo = _drop_bookmark(repo, vcs_root, "probe", commit)
    default_store().system(system).owner = "cafecafe"
    plan = repo.plan_gc(prune_bookmarks=True, delete_stores=True)
    assert not [
        a
        for a in plan.actions
        if a.key == "scratch/probe" and a.op != "forget-working-ref"
    ]
    assert any("cafecafe" in n and "left alone" in n for n in plan.notes)
    assert [c.key for c in repo.created_stores()] == ["scratch/probe"]  # not forgotten


def test_gc_forgets_a_created_store_that_is_already_gone(vcs_root: Path) -> None:
    repo, _db = _baseline(vcs_root)
    system, _fork, commit = _probe_with_created_store(repo)
    repo = _drop_bookmark(repo, vcs_root, "probe", commit)
    default_store().systems.pop(system)  # removed by hand
    plan = repo.plan_gc(delete_stores=True)
    (forget,) = [
        a
        for a in plan.actions
        if a.key == "scratch/probe" and a.op != "forget-working-ref"
    ]
    assert forget.op == "forget-store"
    report = repo.apply_gc(plan)
    assert report.forgotten_stores == ["scratch/probe"]
    assert not repo.created_stores()


def test_gc_store_empty_precondition_stops_a_stale_plan(vcs_root: Path) -> None:
    """Between plan and apply someone tagged the store: the plan's `ignoring`
    set no longer covers what is there, so nothing in the plan runs."""
    from tether.errors import StalePlanError

    repo, _db = _baseline(vcs_root)
    system, fork, commit = _probe_with_created_store(repo)
    repo = _drop_bookmark(repo, vcs_root, "probe", commit)
    plan = repo.plan_gc(prune_bookmarks=True, delete_stores=True)
    assert plan.actions[-1].op == "delete-store"
    sys_ = default_store().system(system)
    sys_.tags["release-1"] = sys_.branches["main"]
    with pytest.raises(StalePlanError, match="no longer empty"):
        repo.apply_gc(plan)
    assert system in default_store().systems and fork in sys_.branches  # nothing ran
    # A fresh plan sees the tag and keeps the store.
    fresh = repo.plan_gc(prune_bookmarks=True, delete_stores=True)
    (kept,) = [a for a in fresh.actions if a.op == "keep-store"]
    assert "not tether's own" in kept.detail


def test_gc_keeps_a_created_store_whose_fork_holds_unpinned_writes(
    vcs_root: Path,
) -> None:
    """The prune rules apply inside a created store too: a fork with writes
    nothing pinned is kept (and so is the store) unless --force-prune."""
    repo, _db = _baseline(vcs_root)
    system, fork, commit = _probe_with_created_store(repo)
    default_store().write(system, fork, {"x": 2})  # after the commit: unpinned
    repo = _drop_bookmark(repo, vcs_root, "probe", commit)
    plan = repo.plan_gc(prune_bookmarks=True, delete_stores=True)
    (kept,) = [a for a in plan.actions if a.op == "keep-store"]
    assert "unpinned writes" in kept.detail and "--force-prune" in kept.detail
    forced = repo.plan_gc(prune_bookmarks=True, force_prune=True, delete_stores=True)
    assert [
        a.op
        for a in forced.actions
        if a.key == "scratch/probe" and a.op != "forget-working-ref"
    ] == ["unpin", "delete-branch", "delete-store"]
    report = repo.apply_gc(forced)
    assert report.deleted_stores == {"scratch/probe": system}


def test_cli_gc_reports_stores(vcs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _db = _baseline(vcs_root)
    system, _fork, commit = _probe_with_created_store(repo)
    repo = _drop_bookmark(repo, vcs_root, "probe", commit)
    monkeypatch.chdir(vcs_root)
    r = runner.invoke(app, ["gc", "--prune-bookmarks"])
    assert r.exit_code == 0, r.output
    assert "delete-store" not in r.output
    r = runner.invoke(app, ["gc", "--prune-bookmarks", "--delete-stores"])
    assert r.exit_code == 0, r.output
    assert "delete-store" in r.output and system in r.output
    r = runner.invoke(
        app, ["gc", "--prune-bookmarks", "--delete-stores", "--no-dry-run", "--json"]
    )
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["deleted_stores"] == {"scratch/probe": system}
    assert system not in default_store().systems


# ----------------------------------------------------------------------------- #
# stores this clone touched
# ----------------------------------------------------------------------------- #


def test_touched_index_remembers_where_this_clone_forked_and_pinned(
    vcs_root: Path,
) -> None:
    repo = Repo.init(vcs_root)
    system = _fresh_system()
    default_store().system(system)
    repo.add("db", "memory", {"system": system, "branch": "main"})
    assert not repo.touched_stores()
    repo.commit("baseline")  # a pin
    (entry,) = repo.touched_stores()
    assert entry.kind == "memory" and entry.key == "db"
    assert entry.identity == dict(
        repo.backend_for("memory").identity({"system": system})
    )
    repo.new(bookmark="work")
    _mem(repo, "db")  # a fork: same store, no second entry
    assert len(repo.touched_stores()) == 1
    index = repo.vcs.shared_dir() / "tether-touched.jsonl"
    assert (
        index.is_file()
        and index.parent == (repo.vcs.shared_dir() / "tether.lock").parent
    )


def test_gc_releases_dead_refs_in_a_touched_store_no_manifest_names(
    vcs_root: Path,
) -> None:
    """The second actor's side of the orphan: B forks and pins in a store it
    did not create, abandons its bookmark, and B's plain `gc` still finds and
    releases B's own refs there -- so the creator can reclaim the store."""
    repo = Repo.init(vcs_root)
    home = _fresh_system()
    default_store().system(home)
    repo.add("db", "memory", {"system": home})
    repo.commit("baseline")
    # An existing store, registered only on a bookmark: its manifest lives
    # in the probe commit alone.
    repo.new(bookmark="probe")
    shared = _fresh_system()
    default_store().system(shared)
    repo.add("shared/emb", "memory", {"system": shared, "branch": "main"})
    commit = repo.commit("register shared").vcs_commit
    assert commit is not None
    repo.new("probe")  # re-plan on the bookmark: the object now has a state to fork
    handle = _mem(repo, "shared/emb")
    fork = handle.ref
    handle.write({"x": 1})
    commit2 = repo.commit("probe write").vcs_commit
    assert commit2 is not None
    sys_ = default_store().system(shared)
    assert fork in sys_.branches and len(sys_.tags) == 2

    repo = _drop_bookmark(repo, vcs_root, "probe", commit2)
    repo = _drop_bookmark(repo, vcs_root, "probe", commit)
    assert "shared/emb" not in repo.objects
    plan = repo.plan_gc(prune_bookmarks=True, delete_stores=True)
    ours = [
        a
        for a in plan.actions
        if a.key == "shared/emb" and a.op != "forget-working-ref"
    ]
    assert sorted(a.op for a in ours) == ["delete-branch", "unpin", "unpin"]
    assert all("touched by this clone" in a.detail for a in ours)
    repo.apply_gc(plan)
    assert fork not in sys_.branches and not sys_.tags
    assert shared in default_store().systems  # not ours to delete
    # Nothing of ours is left: the next plan forgets the store.
    again = repo.plan_gc(prune_bookmarks=True, delete_stores=True)
    (forget,) = [a for a in again.actions if a.key == "shared/emb"]
    assert forget.op == "forget-touched"
    repo.apply_gc(again)
    assert not [t for t in repo.touched_stores() if t.key == "shared/emb"]


def test_gc_leaves_a_touched_store_where_another_actor_may_be_alive(
    vcs_root: Path,
) -> None:
    repo, _db = _baseline(vcs_root)
    system, fork, commit = _probe_with_created_store(repo)
    repo = _drop_bookmark(repo, vcs_root, "probe", commit)
    sys_ = default_store().system(system)
    theirs = f"tether.ws.{repo.config.dataset_id}.their-probe"
    sys_.branches[theirs] = sys_.branches["main"]
    (repo.vcs.shared_dir() / "tether-created.jsonl").unlink()  # touched step only
    plan = repo.plan_gc(prune_bookmarks=True, delete_stores=True)
    assert not [a for a in plan.actions if a.op in ("unpin", "delete-branch")]
    assert any("never had" in n and theirs in n for n in plan.notes)
    assert fork in sys_.branches and sys_.tags


def test_gc_can_reclaim_a_created_store_by_locator(vcs_root: Path) -> None:
    """The creator's clone is gone with its index; any clone of the dataset
    can still reclaim the store by naming it -- under the same rules, the
    marker in the store being the authority."""
    repo, _db = _baseline(vcs_root)
    system, _fork, commit = _probe_with_created_store(repo)
    repo = _drop_bookmark(repo, vcs_root, "probe", commit)
    (repo.vcs.shared_dir() / "tether-created.jsonl").unlink()  # the index is lost
    assert not repo.created_stores()

    plan = repo.plan_gc(prune_bookmarks=True)
    assert not [a for a in plan.actions if a.op == "delete-store"]
    plan = repo.plan_gc(prune_bookmarks=True, stores=[("memory", {"system": system})])
    assert plan.context["delete_stores"] is True
    assert plan.context["stores"] == [["memory", {"system": system}]]
    assert plan.actions[-1].op == "delete-store"
    report = repo.apply_gc(plan)
    assert report.deleted_stores == {system: system}
    assert system not in default_store().systems

    # A store nobody marked is not tether's to remove, index or not.
    other = _fresh_system()
    default_store().system(other)
    plan = repo.plan_gc(stores=[("memory", {"system": other})])
    assert not [a for a in plan.actions if a.op in ("delete-store", "forget-store")]
    assert any("no owner marker" in n for n in plan.notes)
    assert other in default_store().systems


def test_cli_gc_store_option(vcs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _db = _baseline(vcs_root)
    system, _fork, commit = _probe_with_created_store(repo)
    repo = _drop_bookmark(repo, vcs_root, "probe", commit)
    (repo.vcs.shared_dir() / "tether-created.jsonl").unlink()
    monkeypatch.chdir(vcs_root)
    r = runner.invoke(
        app,
        [
            "gc",
            "--prune-bookmarks",
            "--store",
            f"memory={system}",
            "--no-dry-run",
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["deleted_stores"] == {system: system}
    r = runner.invoke(app, ["gc", "--store", "nonsense"])
    assert r.exit_code != 0 and "KIND=LOCATOR" in r.output


def test_gc_leaves_a_store_holding_a_pin_no_commit_of_this_clone_made(
    vcs_root: Path,
) -> None:
    """The same-bookmark case: another actor fetched the probe and commits on
    it, so its pins carry our dataset id under a bookmark we *did* have. The
    only trace is that no `commit` in any checkout of this clone recorded
    those pin ids -- and that is enough to leave the store whole."""
    repo, _db = _baseline(vcs_root)
    system, fork, commit = _probe_with_created_store(repo)
    repo = _drop_bookmark(repo, vcs_root, "probe", commit)
    sys_ = default_store().system(system)
    theirs = f"{repo.config.dataset_id}.00000000deadbeef"
    sys_.tags[f"tether.{theirs}"] = sys_.branches[fork]

    plan = repo.plan_gc(prune_bookmarks=True, delete_stores=True)
    (kept,) = [a for a in plan.actions if a.op == "keep-store"]
    assert "no commit of this clone made" in kept.detail and theirs in kept.detail
    assert not [a for a in plan.actions if a.op in ("unpin", "delete-branch")]
    forced = repo.plan_gc(prune_bookmarks=True, force_prune=True, delete_stores=True)
    assert forced.actions[-1].op == "delete-store"


def _clone(vcs_root: Path, other_root: Path, kind: str) -> None:
    import subprocess

    if kind == "jj":
        subprocess.run(
            ["jj", "git", "clone", "--colocate", str(vcs_root), str(other_root)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["jj", "bookmark", "track", "main@origin", "probe@origin"],
            cwd=other_root,
            check=True,
            capture_output=True,
        )
    else:
        subprocess.run(
            ["git", "clone", "-q", str(vcs_root), str(other_root)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "switch", "-q", "probe"],
            cwd=other_root,
            check=True,
            capture_output=True,
        )


def _push_probe(other_root: Path, kind: str, commit: str) -> None:
    """B publishes `probe`. A deleted its copy meanwhile, so jj's safe push
    wants a fetch first and the bookmark set again where B has it."""
    import subprocess

    steps = (
        [
            ["jj", "git", "fetch"],
            ["jj", "bookmark", "set", "probe", "-r", commit],
            ["jj", "git", "push", "-b", "probe"],
        ]
        if kind == "jj"
        else [["git", "push", "-q", "origin", "probe"]]
    )
    for argv in steps:
        proc = subprocess.run(argv, cwd=other_root, capture_output=True, text=True)
        assert proc.returncode == 0, f"{argv}: {proc.stdout}{proc.stderr}"


def test_two_clones_the_creator_never_deletes_the_other_clones_work(
    vcs_root: Path, tmp_path: Path
) -> None:
    """Clone A creates a store on `probe`; clone B fetches `probe`, forks the
    same branch, writes, commits. A abandons the bookmark without fetching
    B's commit and runs `gc --delete-stores`: the store must stay -- B's pin
    is one no commit of A made. Once B's work reaches A, the store is simply
    referenced again."""
    a, _db = _baseline(vcs_root)
    system, fork, commit = _probe_with_created_store(a)
    other_root = tmp_path / "clone-b"
    _clone(vcs_root, other_root, a.vcs.kind)

    b = Repo.find(other_root)
    assert b.config.dataset_id == a.config.dataset_id
    b.new("probe")
    handle = _mem(b, "scratch/probe")
    assert handle.ref == fork  # the same branch: the bookmark's
    handle.write({"x": 2})
    b_commit = b.commit("b writes")
    b_pin = b_commit.pinned["scratch/probe"]
    assert b_pin is not None

    a = _drop_bookmark(a, vcs_root, "probe", commit)
    plan = a.plan_gc(prune_bookmarks=True, delete_stores=True)
    (kept,) = [x for x in plan.actions if x.op == "keep-store"]
    assert b_pin.id in kept.detail and "no commit of this clone made" in kept.detail
    assert not [
        x for x in plan.actions if x.op in ("unpin", "delete-branch", "delete-store")
    ]
    a.apply_gc(plan)
    sys_ = default_store().system(system)
    assert fork in sys_.branches and b_pin.ref in sys_.tags

    # B's work arrives: the bookmark is back and its manifest names the store.
    assert b_commit.vcs_commit is not None
    _push_probe(other_root, a.vcs.kind, b_commit.vcs_commit)
    a = Repo.find(vcs_root)
    plan = a.plan_gc(prune_bookmarks=True, delete_stores=True)
    assert not [
        x
        for x in plan.actions
        if x.key == "scratch/probe" and x.op != "forget-working-ref"
    ]
    assert any("still referenced" in n for n in plan.notes)


# ----------------------------------------------------------------------------- #
# the journals are memory aids: never a reason for a stable command to fail
# ----------------------------------------------------------------------------- #


def test_a_torn_line_in_the_touched_journal_breaks_nothing(
    vcs_root: Path,
) -> None:
    """An interrupted append leaves half a line. Every reader skips it, as
    `read_ops` does; `open`, `commit`, and `gc --delete-stores` proceed; and
    the next append lands after it."""
    from tether.oplog import read_touched

    repo, _db = _baseline(vcs_root)
    journal = repo.vcs.shared_dir() / "tether-touched.jsonl"
    assert journal.is_file()
    with journal.open("a", encoding="utf-8") as fh:
        fh.write('{"dataset_id": "0a1b2c3d", "kind": "memory", "ide')  # torn
    system, fork, commit = _probe_with_created_store(repo)  # forks and pins
    assert fork and commit
    keys = sorted(t.key for t in read_touched(repo.vcs.shared_dir()))
    assert keys == ["db", "scratch/probe"]
    repo = _drop_bookmark(repo, vcs_root, "probe", commit)
    report = repo.gc(dry_run=False, prune_bookmarks=True, delete_stores=True)
    assert report.deleted_stores == {"scratch/probe": system}
    # The torn line is dropped when the file is rewritten.
    text = journal.read_text(encoding="utf-8")
    assert '"kind": "memory", "ide\n' not in text and text.endswith("\n")
    assert all(json.loads(line) for line in text.splitlines())


def test_a_torn_line_in_the_created_index_breaks_nothing(vcs_root: Path) -> None:
    repo, _db = _baseline(vcs_root)
    system, _fork, commit = _probe_with_created_store(repo)
    index = repo.vcs.shared_dir() / "tether-created.jsonl"
    with index.open("a", encoding="utf-8") as fh:
        fh.write("{not json")
    assert [c.key for c in repo.created_stores()] == ["scratch/probe"]
    repo = _drop_bookmark(repo, vcs_root, "probe", commit)
    report = repo.gc(dry_run=False, prune_bookmarks=True, delete_stores=True)
    assert report.deleted_stores == {"scratch/probe": system}


def test_recording_a_touch_is_best_effort(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The journal cannot be written (here: its directory is a file). The pin
    and the fork still happen; one warning says what was not recorded."""
    import tether.repo._core as core

    repo = Repo.init(vcs_root)
    system = _fresh_system()
    default_store().system(system)
    repo.add("db", "memory", {"system": system})

    def boom(*args: object, **kwargs: object) -> bool:
        raise OSError("disk says no")

    monkeypatch.setattr(core, "append_touched", boom)
    with pytest.warns(UserWarning, match="touched-store index"):
        result = repo.commit("v1")
    assert result.pinned["db"] is not None  # the pin landed
    repo.new(bookmark="w")
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # warned once per Repo, not per call
        handle = _mem(repo, "db")
    assert not handle.read_only
