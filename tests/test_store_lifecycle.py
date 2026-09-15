"""Stores tether creates (`add --create`, `Repo.create`) and, once nothing
references them, reclaims in `gc --delete-stores`."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from typer import testing as typer_testing

from tether.backends.memory import default_store
from tether.cli import app
from tether.errors import BackendError, CapabilityError, ConfigError
from tether.handles import MemoryHandle
from tether.manifest import ObjectManifest, read_objects
from tether.oplog import read_created
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
