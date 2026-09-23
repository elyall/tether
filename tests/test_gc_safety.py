"""`gc` keeps anything that might still be needed: pins named only by another
checkout's working tree or by an operation that has not finished, branches a
`--shared` sibling works on, and everything while jj reports a conflict."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tether.backends.memory import default_store
from tether.errors import VcsError
from tether.handles import MemoryHandle
from tether.repo import Repo


def _baseline(vcs_root: Path) -> tuple[Repo, str]:
    repo = Repo.init(vcs_root)
    system = f"sys-{uuid.uuid4().hex[:8]}"
    default_store().system(system)
    repo.add("db", "memory", {"system": system, "branch": "main"})
    repo.commit("baseline")
    return repo, system


def _write(repo: Repo, payload: dict) -> None:
    handle = repo.open("db")
    assert isinstance(handle, MemoryHandle)
    handle.write(payload)


def _other_checkout(repo: Repo, vcs_root: Path, other_root: Path, rev: str) -> Repo:
    """A second checkout of the repository, at `rev`."""
    cmd = (
        ["jj", "workspace", "add", str(other_root), "-r", rev]
        if repo.vcs.kind == "jj"
        else ["git", "worktree", "add", "--detach", str(other_root), rev]
    )
    subprocess.run(cmd, cwd=vcs_root, check=True, capture_output=True)
    return Repo.find(other_root)


def _jj(vcs_root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["jj", *args], cwd=vcs_root, check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


@pytest.mark.parametrize("how", ["undo", "novcs"])
def test_gc_counts_pins_another_checkouts_working_tree_names(
    vcs_root: Path, tmp_path: Path, how: str
) -> None:
    """r4: checkout B commits and undoes (git: `reset --soft`; jj: squash into
    `@`), or commits with `vcs=False`. Its working-tree manifest names a pin
    no history does; A's gc used to release it, and B's next commit then
    recorded a manifest whose pin `verify` called missing."""
    a, system = _baseline(vcs_root)
    b = _other_checkout(a, vcs_root, tmp_path / "other", a.vcs.bookmarks()["main"])
    b.new(bookmark="bwork")
    _write(b, {"x": "B"})
    if how == "undo":
        result = b.commit("B work")
        b.undo()
    else:
        result = b.commit("B work", vcs=False)
    pin = result.pinned["db"]
    assert pin is not None
    assert Repo.find(tmp_path / "other").objects["db"].pin == pin

    a = Repo.find(vcs_root)
    plan = a.plan_gc()
    assert not [x for x in plan.actions if x.op == "unpin"], plan.render()
    a.apply_gc(plan)
    assert pin.ref in default_store().system(system).tags
    assert all(r.ok for r in Repo.find(tmp_path / "other").verify().values())


def test_gc_keeps_the_pins_of_an_operation_that_has_not_finished(
    vcs_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit killed between its pins and its manifests leaves a started
    journal entry whose progress names the pin and no manifest anywhere; gc
    in another checkout must treat that as a reference, and the retried
    commit must still be whole."""
    from tether.repo import _commit as commit_module

    a, system = _baseline(vcs_root)
    a.new(bookmark="feat")
    _write(a, {"v": 1})

    def killed(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt  # not an Exception: no rollback, as a kill

    monkeypatch.setattr(commit_module, "write_object", killed)
    with pytest.raises(KeyboardInterrupt):
        a.commit("v1")
    monkeypatch.undo()
    started = [e for e in Repo.find(vcs_root).incomplete_ops()]
    assert [e.command for e in started] == ["commit"]
    pinned = [r for r in started[0].progress if r.get("action") == "pin"]
    assert len(pinned) == 1
    pin_ref = str(pinned[0]["ref"])

    b = _other_checkout(a, vcs_root, tmp_path / "other", a.vcs.bookmarks()["main"])
    plan = b.plan_gc()
    assert not [x for x in plan.actions if x.op == "unpin"], plan.render()
    assert any("unfinished operation" in n for n in plan.notes)
    b.apply_gc(plan)
    assert pin_ref in default_store().system(system).tags

    retried = Repo.find(vcs_root).commit("v1, again")
    assert retried.vcs_commit is not None
    assert all(r.ok for r in Repo.find(vcs_root).verify().values())


def test_prune_keeps_a_branch_a_shared_checkout_works_on(
    vcs_root: Path, tmp_path: Path
) -> None:
    """r7 / r12: A is on bookmark x with its fork still pending; B joined x
    with `--shared`, forked the branch and committed. A's `--prune-bookmarks`
    judged the branch by its own working refs alone: 'this bookmark's
    branch; no object uses it', deleted, B's next write failed."""
    a, system = _baseline(vcs_root)
    a.new(bookmark="x")  # lazy: A has no branch yet
    ref = a.workspace.pending_forks["db"]
    assert not a.workspace.working_refs
    store = default_store()
    if a.vcs.kind == "jj":
        b = _other_checkout(a, vcs_root, tmp_path / "other", a.vcs.bookmarks()["x"])
        b.new("x", shared=True)
        _write(b, {"base": 1, "b": "committed"})
        b.commit("B on x")
        assert b.workspace.working_refs["db"] == ref
        _write(b, {"base": 1, "b": "uncommitted follow-up"})
    else:
        # git checks a branch out in one worktree only: the sibling is a
        # clone, whose work shows up in the store alone. A's own pending
        # fork names the branch, and that has to be enough.
        m = a.objects["db"]
        assert m.pin is not None
        a.backend_for("memory").fork(m.locator, m.pin, ref)
        store.write(system, ref, {"base": 1, "b": "uncommitted follow-up"})

    a = Repo.find(vcs_root)
    for force in (False, True):
        plan = a.plan_gc(prune_bookmarks=True, force_prune=force)
        assert not [
            x for x in plan.actions if x.op in ("delete-branch", "keep-branch")
        ], plan.render()
    a.gc(dry_run=False, prune_bookmarks=True, force_prune=True)
    assert ref in store.system(system).branches
    assert store.read(system, ref) == {"base": 1, "b": "uncommitted follow-up"}
    if a.vcs.kind == "jj":
        b = Repo.find(tmp_path / "other")
        _write(b, {"base": 1, "b": "next write"})
        assert store.read(system, ref) == {"base": 1, "b": "next write"}


def _clone(vcs_root: Path, other_root: Path, kind: str) -> None:
    if kind == "jj":
        subprocess.run(
            ["jj", "git", "clone", "--colocate", str(vcs_root), str(other_root)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["jj", "bookmark", "track", "main@origin"],
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


def test_gc_keeps_pins_this_clone_did_not_create(
    vcs_root: Path, tmp_path: Path
) -> None:
    """r14: clone B commits on its own bookmark and has not pushed. Its pin is
    in the shared store and in no commit A has, so A's plain gc released it
    with 'no manifest in history references it'. Now A keeps it as one it
    did not create; `--release-foreign` is the opt-in."""
    from tether.oplog import pinned_path, read_pinned

    a, system = _baseline(vcs_root)
    own = a.objects["db"].pin
    assert own is not None
    assert read_pinned(a.vcs.shared_dir(), a.config.dataset_id) == {own.id}
    _clone(vcs_root, tmp_path / "clone-b", a.vcs.kind)
    b = Repo.find(tmp_path / "clone-b")
    assert b.config.dataset_id == a.config.dataset_id
    b.new(bookmark="bwork")
    _write(b, {"x": "B"})
    theirs = b.commit("B's work, not pushed").pinned["db"]
    assert theirs is not None
    # B's index is its own; A's does not learn of B's pin.
    assert theirs.id in read_pinned(b.vcs.shared_dir(), b.config.dataset_id)
    assert theirs.id not in read_pinned(a.vcs.shared_dir(), a.config.dataset_id)

    a = Repo.find(vcs_root)
    plan = a.plan_gc()
    (kept,) = [x for x in plan.actions if x.op == "keep-pin"]
    assert kept.target == theirs.ref and "not created by this clone" in kept.detail
    assert not [x for x in plan.actions if x.op == "unpin"]
    assert plan.is_empty  # informational: nothing to apply
    report = a.gc(dry_run=False)
    assert report.kept_pins == {"memory": [theirs.id]} and not report.unpinned
    store = default_store().system(system)
    assert theirs.ref in store.tags

    released = a.plan_gc(release_foreign=True)
    (unpin,) = [x for x in released.actions if x.op == "unpin"]
    assert unpin.target == theirs.ref and "not created by this clone" in unpin.detail
    assert released.context["release_foreign"] is True
    a.apply_gc(released)
    assert theirs.ref not in store.tags
    assert pinned_path(a.vcs.shared_dir()).is_file()


def test_pinned_index_is_seeded_from_the_op_logs(vcs_root: Path) -> None:
    """A clone from before the index: its own pins are what its `commit`
    entries recorded, so the first gc knows them and releases the
    unreferenced ones, and keeps a pin no op log explains."""
    from tether.manifest import compute_pin_id
    from tether.oplog import pinned_path, read_pinned

    repo, system = _baseline(vcs_root)
    repo.new(bookmark="probe")
    _write(repo, {"x": "probe"})
    result = repo.commit("probe write")
    assert result.vcs_commit is not None and result.pinned["db"] is not None
    mine = result.pinned["db"]
    pinned_path(repo.vcs.shared_dir()).unlink()  # as a 0.1.0b4 clone has none
    # A pin nothing in this clone made: dropped into the store by hand.
    backend = repo.backend_for("memory")
    m = repo.objects["db"]
    stray_state = {"snapshot_id": default_store().write(system, "main", {"v": 9})}
    stray = backend.pin(
        m.locator,
        stray_state,
        compute_pin_id(
            "memory", backend.identity(m.locator), stray_state, repo.config.dataset_id
        ),
    )
    repo.abandon([result.vcs_commit])  # `mine` is unreferenced now, like `stray`

    repo = Repo.find(vcs_root)
    plan = repo.plan_gc()
    assert read_pinned(repo.vcs.shared_dir(), repo.config.dataset_id) >= {mine.id}
    assert stray.id not in read_pinned(repo.vcs.shared_dir(), repo.config.dataset_id)
    verdicts = {a.target: a.op for a in plan.actions if a.op in ("unpin", "keep-pin")}
    assert verdicts == {mine.ref: "unpin", stray.ref: "keep-pin"}


@pytest.mark.parametrize("kind", ["lakefs", "retired"])
def test_gc_and_verify_skip_history_of_a_backend_tether_no_longer_has(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """A dataset that once held a lakeFS object (removed in 0.1.0b4) was
    stuck: after `remove` and a commit, `gc` (even `--dry-run`),
    `gc --delete-stores` and `verify --all-history` failed building a backend
    for the manifests history still holds. They skip those manifests with a
    note that says why, keep counting their pins as references, and do the
    rest of their work; so for any kind tether does not know."""
    from tether.cli import app
    from tether.manifest import ObjectManifest, Pin, key_to_relpath, write_object
    from tether.oplog import TouchedStore, append_touched

    typer_testing = pytest.importorskip("typer.testing")
    repo, system = _baseline(vcs_root)
    pid = f"{repo.config.dataset_id}.00000000000000aa"
    old = ObjectManifest(
        key="lake",
        kind=kind,
        locator={"repository": "lab", "branch": "main"},
        state={"commit": "c0ffee"},
        pin=Pin(id=pid, ref=f"tether.{pid}"),
    )
    write_object(repo.root, old)
    rel = (Path(".tether") / key_to_relpath("lake")).as_posix()
    repo.vcs.commit([rel], f"a {kind} object")
    (repo.root / rel).unlink()
    repo.vcs.commit([rel], f"remove the {kind} object")
    append_touched(
        repo.vcs.shared_dir(),
        TouchedStore(repo.config.dataset_id, kind, {"r": "lab"}, {}, "lake", ""),
    )
    # A write of the object that remains, and a dead pin of it, so each
    # command has real work besides the manifests it skips.
    repo = Repo.find(vcs_root)
    repo.new(bookmark="w")
    _write(repo, {"v": 2})
    dead = repo.commit("w").pinned["db"]
    assert dead is not None
    repo.abandon([repo.vcs.bookmarks()["w"]])
    repo = Repo.find(vcs_root)

    for plan in (repo.plan_gc(), repo.plan_gc(delete_stores=True)):
        notes = " ".join(plan.notes)
        assert f"'{kind}': 1 manifest(s) skipped" in notes, plan.render()
        assert "their pins count as references" in notes
        if kind == "lakefs":
            assert "removed in 0.1.0b4" in notes
        assert [a.target for a in plan.actions if a.op == "unpin"] == [dead.ref]
    assert f"'{kind}': 1 touched-store record(s) skipped" in " ".join(
        repo.plan_gc(delete_stores=True).notes
    )
    with pytest.warns(UserWarning, match=rf"'{kind}': 1 manifest\(s\) skipped"):
        reports = repo.verify(all_history=True)
    assert reports and all(r.ok for r in reports.values())
    assert not [label for label in reports if label.endswith(":lake")]

    monkeypatch.chdir(vcs_root)
    runner = typer_testing.CliRunner()
    r = runner.invoke(app, ["gc"])
    assert r.exit_code == 0, r.output
    assert f"'{kind}': 1 manifest(s) skipped" in " ".join(r.output.split())
    with pytest.warns(UserWarning, match="not verified"):
        r = runner.invoke(app, ["verify", "--all-history"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["gc", "--delete-stores", "--no-dry-run"])
    assert r.exit_code == 0, r.output
    assert dead.ref not in default_store().system(system).tags


def test_pinned_index_seed_claims_only_pins_this_clone_created(
    vcs_root: Path, tmp_path: Path
) -> None:
    """The seed read each commit's `pinned` result, which also names pins
    that already existed -- another clone's, taken over by a `pull` -- so
    those became this clone's to release. Only a progress record the
    backend's `created` flag vouches for counts: a commit's `pin`, a
    repair's `repin` with `created`; not a pre-flag `repin`, nor what a
    rolled-back commit made."""
    import json

    from tether.oplog import ops_path, pinned_path, read_pinned

    a, system = _baseline(vcs_root)
    _clone(vcs_root, tmp_path / "clone-b", a.vcs.kind)
    b = Repo.find(tmp_path / "clone-b")
    _write(a, {"x": "upstream"})
    theirs = a.commit("A writes on the trunk").pinned["db"]
    assert theirs is not None
    b.new("main")
    pulled = b.pull()
    assert pulled.pinned["db"] == theirs  # the same content-addressed pin
    b.new(bookmark="bwork")
    _write(b, {"x": "B"})
    mine = b.commit("B's own").pinned["db"]
    assert mine is not None
    # A repair that re-created a pin B's commit had made (the ref was lost).
    backend = b.backend_for("memory")
    backend.unpin(b.objects["db"].locator, mine)
    assert b.repair().repinned == {"db": mine.id}
    # What an older tether journaled: a `repin` with no `created` flag, and a
    # commit that rolled back.
    stray = f"{'f' * 12}"
    with ops_path(b.root).open("a", encoding="utf-8") as fh:
        for line in (
            {"id": "0" * 12, "at": "2026-01-01T00:00:00+00:00", "command": "repair"},
            {
                "progress": "0" * 12,
                "action": "repin",
                "key": "db",
                "target": f"tether.{stray}",
            },
            {"id": "1" * 12, "at": "2026-01-01T00:00:01+00:00", "command": "commit"},
            {
                "progress": "1" * 12,
                "action": "pin",
                "key": "db",
                "ref": "tether.eeeeeeeeeeee",
            },
            {"done": "1" * 12, "result": {"failed": "x", "rolled_back": True}},
        ):
            fh.write(json.dumps(line) + "\n")
    pinned_path(b.vcs.shared_dir()).unlink()  # as a clone from before the index
    b = Repo.find(tmp_path / "clone-b")
    assert b._known_pins() == {mine.id}
    assert theirs.id not in read_pinned(b.vcs.shared_dir(), b.config.dataset_id)
    assert theirs.ref in default_store().system(system).tags


@pytest.mark.parametrize("name", ["ops.jsonl", "workspace.toml"])
def test_gc_skips_another_checkouts_state_file_the_vcs_tracks(
    vcs_root: Path, tmp_path: Path, name: str
) -> None:
    """S1 refuses this checkout's `ops.jsonl` and `workspace.toml` while the
    VCS tracks them, but gc read every other live checkout's copies too: a
    tracked op log there -- one a clone shipped -- claimed a stray pin as
    this clone's, and gc released it. Another checkout's tracked file is
    skipped with a warning naming that checkout; gc goes on."""
    import json

    from tether.manifest import compute_pin_id
    from tether.oplog import pinned_path

    a, system = _baseline(vcs_root)
    backend = a.backend_for("memory")
    m = a.objects["db"]
    state = {"snapshot_id": default_store().write(system, "main", {"v": 9})}
    identity = backend.identity(m.locator)
    stray = backend.pin(
        m.locator,
        state,
        compute_pin_id("memory", identity, state, a.config.dataset_id),
    )
    other = tmp_path / "other"
    _other_checkout(a, vcs_root, other, a.vcs.bookmarks()["main"])
    tdir = other / ".tether"
    if name == "ops.jsonl":
        claim = [
            {"id": "a" * 12, "at": "2026-01-01T00:00:00+00:00", "command": "commit"},
            {"progress": "a" * 12, "action": "pin", "key": "db", "ref": stray.ref},
            {"done": "a" * 12, "result": {}},
        ]
        (tdir / name).write_text("".join(json.dumps(x) + "\n" for x in claim))
    lines = (tdir / ".gitignore").read_text().splitlines()
    (tdir / ".gitignore").write_text(
        "".join(f"{x}\n" for x in lines if x != f"/{name}")
    )
    if a.vcs.kind == "jj":
        _jj(other, "file", "track", f".tether/{name}")
        assert f".tether/{name}" in _jj(other, "file", "list").split()
    else:
        subprocess.run(
            ["git", "add", f".tether/{name}"],
            cwd=other,
            check=True,
            capture_output=True,
        )
    pinned_path(a.vcs.shared_dir()).unlink()  # the next gc seeds from op logs

    a = Repo.find(vcs_root)
    with pytest.warns(
        UserWarning, match=rf"skipping \.tether/{name} of checkout "
    ) as record:
        plan = a.plan_gc()
    named = {f"of checkout {p}:" for p in (other, other.resolve())}
    assert any(n in str(w.message) for w in record for n in named)
    verdicts = {x.target: x.op for x in plan.actions if x.op in ("unpin", "keep-pin")}
    assert verdicts == {stray.ref: "keep-pin"}, plan.render()
    if name == "workspace.toml":
        assert [r.resolve() for r, _ws in a._iter_live_workspaces()] == [
            vcs_root.resolve()
        ]


@pytest.mark.parametrize("how", ["gc", "rollback", "gc-then-reseed"])
def test_a_released_pin_recreated_by_another_clone_is_not_released_again(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """Pins are content-addressed: after this clone releases one, another
    clone committing the same state creates it again under the same id.
    The index kept the released id, so the next gc here released the other
    clone's pin as its own. It forgets what it releases -- through gc, or a
    commit's rollback -- and a re-seed replays gc's releases too."""
    from tether.oplog import pinned_path

    repo, system = _baseline(vcs_root)
    repo.new(bookmark="probe")
    _write(repo, {"x": "probe"})
    if how == "rollback":

        def refused(*args: object, **kwargs: object) -> str:
            raise VcsError("hook refused the commit")

        monkeypatch.setattr(repo.vcs, "commit", refused)
        plan = repo.plan_commit("probe write")
        (pin_action,) = [x for x in plan.actions if x.op == "pin"]
        with pytest.raises(VcsError, match="hook refused"):
            repo.apply_commit(plan)
        monkeypatch.undo()
        pid = str(pin_action.params["pin_id"])
        state = dict(pin_action.params["state"])
    else:
        result = repo.commit("probe write")
        pin = result.pinned["db"]
        assert pin is not None and result.vcs_commit is not None
        pid, state = pin.id, dict(repo.objects["db"].state or {})
        repo.abandon([result.vcs_commit])
        report = repo.gc(dry_run=False)
        assert report.unpinned == {"memory": [pid]}
    store = default_store().system(system)
    assert f"tether.{pid}" not in store.tags
    assert pid not in repo._known_pins()
    if how == "gc-then-reseed":
        pinned_path(repo.vcs.shared_dir()).unlink()
        assert pid not in Repo.find(vcs_root)._known_pins()
    # Another clone commits the same state: the same pin, made anew.
    backend = repo.backend_for("memory")
    assert backend.pin(repo.objects["db"].locator, state, pid).created
    plan = Repo.find(vcs_root).plan_gc()
    verdicts = {a.params["pin_id"]: a.op for a in plan.actions if "pin_id" in a.params}
    assert verdicts == {pid: "keep-pin"}, plan.render()


def test_gc_and_drop_refuse_while_jj_reports_a_conflicted_commit(
    vcs_root: Path,
) -> None:
    """r2: `jj rebase -b feat -d main` where both lines changed `db` leaves
    feat's commit conflicted. Its manifest has two answers, and only one is
    the tree jj shows; nothing is judged until the conflict is resolved."""
    repo, system = _baseline(vcs_root)
    if repo.vcs.kind != "jj":
        pytest.skip("jj conflicts")
    repo.new(bookmark="feat")
    _write(repo, {"x": "feat"})
    feat_pin = repo.commit("feat write").pinned["db"]
    assert feat_pin is not None
    repo.new("main")
    _write(repo, {"x": "main"})
    repo.commit("trunk write")
    _jj(vcs_root, "rebase", "-b", "feat", "-d", "main")
    repo = Repo.find(vcs_root)
    (conflicted,) = repo.vcs.conflicted_commits()
    with pytest.raises(VcsError, match="conflict"):
        repo.plan_gc()
    with pytest.raises(VcsError, match="conflict"):
        repo.plan_drop("feat")
    assert feat_pin.ref in default_store().system(system).tags
    # `abandon` of another line still does its VCS half; the store half waits.
    repo.new(bookmark="side")
    _write(repo, {"x": "side"})
    side = repo.commit("side write")
    assert side.vcs_commit is not None and side.pinned["db"] is not None
    repo.new("main")
    report = repo.abandon([side.vcs_commit])
    assert report.abandoned == [side.vcs_commit]
    assert report.gc_plan is not None and report.gc_plan.is_empty
    assert any("conflict" in n for n in report.gc_plan.notes)
    # Resolved (here by dropping the conflicted commit): gc judges again, and
    # both lines' pins are what nothing references now.
    _jj(vcs_root, "abandon", conflicted)
    repo = Repo.find(vcs_root)
    assert not repo.vcs.conflicted_commits()
    unpins = {x.target for x in repo.plan_gc().actions if x.op == "unpin"}
    assert unpins == {feat_pin.ref, side.pinned["db"].ref}


def _commit(vcs_root: Path, revset: str) -> str:
    return _jj(vcs_root, "log", "--no-graph", "-r", revset, "-T", "self.commit_id()")


def _file_conflict(vcs_root: Path, relpath: str, *, resolved: bool) -> str:
    """Two edits of `relpath` on top of main, the second rebased onto the
    first: the second is conflicted. `resolved` fixes it in a child."""
    path = vcs_root / relpath
    for side in ("A", "B"):
        _jj(vcs_root, "new", "main", "-m", f"side {side}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{side}\n", encoding="utf-8")
    _jj(vcs_root, "new", "main")
    a = _commit(vcs_root, 'description(glob:"side A*")')
    _jj(
        vcs_root,
        "rebase",
        "-r",
        _commit(vcs_root, 'description(glob:"side B*")'),
        "-d",
        a,
    )
    b = _commit(vcs_root, 'description(glob:"side B*")')
    if resolved:
        _jj(vcs_root, "new", b, "-m", "fixed")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("A and B\n", encoding="utf-8")
        _jj(vcs_root, "new", "main")
    return b


@pytest.mark.parametrize("resolved", [False, True])
@pytest.mark.parametrize(
    "relpath",
    ["README", "docs/notes.md", "other/.tether/objects/x.toml", "ds/.tetherx/y"],
)
def test_a_conflict_outside_the_datasets_manifests_blocks_neither_gc_nor_drop(
    vcs_root: Path, relpath: str, resolved: bool
) -> None:
    """The review's case: a README conflict, which jj users resolve in a
    child commit, blocked gc and drop forever. So did any conflict anywhere
    in history -- another directory's `.tether/`, a sibling whose name only
    starts like it -- resolved or not."""
    if shutil.which("jj") is None or not (vcs_root / ".jj").is_dir():
        pytest.skip("jj conflicts")
    repo = Repo.init(vcs_root / "ds")
    system = f"sys-{uuid.uuid4().hex[:8]}"
    default_store().system(system)
    repo.add("db", "memory", {"system": system, "branch": "main"})
    repo.commit("baseline")
    repo.new(bookmark="feat")
    _write(repo, {"x": "feat"})
    repo.commit("feat write")
    repo.new("main")
    conflicted = _file_conflict(vcs_root, relpath, resolved=resolved)
    repo = Repo.find(vcs_root / "ds")
    assert conflicted in repo.vcs.conflicted_commits()
    repo.plan_gc()
    report = repo.drop("feat")
    assert report.abandoned and "feat" not in repo.vcs.bookmarks()


@pytest.mark.parametrize("where", ["child", "grandchild"])
def test_a_manifest_conflict_resolved_in_a_later_commit_no_longer_blocks(
    vcs_root: Path, where: str
) -> None:
    """Resolving in a new commit on top leaves the conflicted commit in
    history, and the bookmark on it. gc reads every side of it, so what
    either side pinned is still referenced; nothing blocks any more."""
    repo, system = _baseline(vcs_root)
    if repo.vcs.kind != "jj":
        pytest.skip("jj conflicts")
    repo.new(bookmark="feat")
    _write(repo, {"x": "feat"})
    feat_pin = repo.commit("feat write").pinned["db"]
    repo.new("main")
    _write(repo, {"x": "main"})
    trunk = repo.commit("trunk write")
    assert feat_pin is not None and trunk.vcs_commit is not None
    _jj(vcs_root, "rebase", "-b", "feat", "-d", "main")
    repo = Repo.find(vcs_root)
    (conflicted,) = repo.vcs.conflicted_commits()
    with pytest.raises(VcsError, match=r"conflicted manifests under \.tether/"):
        repo.plan_gc()
    _jj(vcs_root, "new", conflicted, "-m", "on top")
    if where == "grandchild":  # a child that leaves the conflict in place
        (vcs_root / "README").write_text("unrelated\n", encoding="utf-8")
        _jj(vcs_root, "new", "-m", "resolution")
    text = repo.vcs.read_file_at(trunk.vcs_commit, ".tether/objects/db.toml")
    assert text is not None
    (vcs_root / ".tether" / "objects" / "db.toml").write_text(text, encoding="utf-8")
    _jj(vcs_root, "new", "main")
    repo = Repo.find(vcs_root)
    assert repo.vcs.conflicted_commits()[-1] == conflicted
    assert repo.vcs.bookmarks()["feat"] == conflicted
    plan = repo.plan_gc()
    assert not [a for a in plan.actions if a.op == "unpin"], plan.render()
    repo.plan_drop("feat")
    assert feat_pin.ref in default_store().system(system).tags


def test_a_manifest_conflict_still_open_on_one_line_blocks_with_the_remedy(
    vcs_root: Path,
) -> None:
    """Resolved on one line, still conflicted on a sibling line (a head):
    refused, naming that head, with a remedy that works on a commit that
    is not the working copy -- not `jj resolve`."""
    repo, _system = _baseline(vcs_root)
    if repo.vcs.kind != "jj":
        pytest.skip("jj conflicts")
    repo.new(bookmark="feat")
    _write(repo, {"x": "feat"})
    repo.commit("feat write")
    repo.new("main")
    _write(repo, {"x": "main"})
    trunk = repo.commit("trunk write")
    assert trunk.vcs_commit is not None
    _jj(vcs_root, "rebase", "-b", "feat", "-d", "main")
    (conflicted,) = Repo.find(vcs_root).vcs.conflicted_commits()
    _jj(vcs_root, "new", conflicted, "-m", "resolution")
    text = repo.vcs.read_file_at(trunk.vcs_commit, ".tether/objects/db.toml")
    assert text is not None
    (vcs_root / ".tether" / "objects" / "db.toml").write_text(text, encoding="utf-8")
    _jj(vcs_root, "new", conflicted, "-m", "other line")
    (vcs_root / "README").write_text("unrelated\n", encoding="utf-8")
    _jj(vcs_root, "new", "main")
    other = _commit(vcs_root, 'description(glob:"other line*")')
    repo = Repo.find(vcs_root)
    with pytest.raises(VcsError) as refused:
        repo.plan_gc()
    message = str(refused.value)
    assert f"commit {other[:12]} has conflicted manifests under .tether/" in message
    assert f"`jj new {other[:12]}`" in message and "`jj squash`" in message
    assert "jj resolve" not in message and conflicted[:12] not in message
    with pytest.raises(VcsError, match=other[:12]):
        repo.plan_drop("feat")


def test_promote_refuses_a_conflicted_trunk(vcs_root: Path) -> None:
    """r3: two concurrent moves of `main` (the shape a divergent `jj git
    fetch` leaves) make it a bookmark with two targets. `bookmarks()` left it
    out, the trunk guard read that as 'no trunk yet', and the apply settled
    the conflict on this bookmark's commit, dropping both other commits."""
    repo, _system = _baseline(vcs_root)
    if repo.vcs.kind != "jj":
        pytest.skip("jj conflicted bookmarks")
    repo.new(bookmark="feat")
    _write(repo, {"x": "feat"})
    repo.commit("feat write")
    _jj(vcs_root, "new", "--no-edit", "main", "-m", "landed elsewhere 1")
    _jj(vcs_root, "new", "--no-edit", "main", "-m", "landed elsewhere 2")
    s1 = _jj(
        vcs_root,
        "log",
        "--no-graph",
        "-r",
        'description(glob:"landed elsewhere 1*")',
        "-T",
        "commit_id",
    )
    s2 = _jj(
        vcs_root,
        "log",
        "--no-graph",
        "-r",
        'description(glob:"landed elsewhere 2*")',
        "-T",
        "commit_id",
    )
    op = _jj(vcs_root, "op", "log", "--no-graph", "-n1", "-T", "id")
    _jj(vcs_root, "bookmark", "set", "main", "-r", s1)
    _jj(vcs_root, "--at-op", op, "bookmark", "set", "main", "-r", s2)
    repo = Repo.find(vcs_root)
    assert repo.vcs.conflicted_bookmarks() == ["main"]
    with pytest.raises(VcsError, match="conflict"):
        repo.plan_gc()

    plan = repo.plan_promote()
    assert plan.actions and all(a.op == "refuse" for a in plan.actions)
    assert "conflicting targets" in plan.actions[0].detail
    report = repo.apply_promote(plan)
    assert report.trunk_moved is None and report.refused
    assert repo.vcs.conflicted_bookmarks() == ["main"]  # both sides still stand
    assert repo.vcs.alive_commits([s1, s2]) == {s1, s2}
    assert repo.bookmark_drift() == []  # feat is fine; the trunk is what is conflicted
