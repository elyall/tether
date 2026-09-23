"""The engine against the VCS as users configure it: a hostile jj/git config
must not change what tether commits or reads, a commit that left the manifests
out must not report success, and a refused `git commit` must leave no trace."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from tether.backends.memory import default_store
from tether.errors import VcsError
from tether.handles import MemoryHandle
from tether.repo import Repo


def _mem_object(repo: Repo, key: str = "db") -> str:
    name = f"sys-{uuid.uuid4().hex[:8]}"
    default_store().system(name)
    repo.add(key, "memory", {"system": name, "branch": "main"})
    return name


def _write(repo: Repo, key: str, payload: dict) -> None:
    handle = repo.open(key)
    assert isinstance(handle, MemoryHandle)
    handle.write(payload)


def _jj(root: Path, *args: str) -> str:
    return subprocess.run(
        ["jj", "--color=never", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_core_loop_and_gc_under_a_hostile_user_config(
    vcs_root: Path, hostile_vcs_config: Path
) -> None:
    """init, add, commit, branch, write, commit, promote, gc -- with colour
    forced on, `all()` aliased to `@`, new files never auto-tracked and
    capped at 1 KiB. Under that config gc used to see one commit of history
    and plan to release every other pin; a listing over the cap was left
    out of the commit that reported it."""
    repo = Repo.init(vcs_root)
    system = _mem_object(repo)
    raw = vcs_root / "raw"
    raw.mkdir()
    for n in range(30):  # a listing well over the 1 KiB new-file cap
        (raw / f"plate-{n:02d}.csv").write_text(f"{n}\n", encoding="utf-8")
    repo.add("raw", "file", {"uri": str(raw)})
    first = repo.commit("baseline")
    assert first.vcs_commit and len(first.vcs_commit) == 40
    int(first.vcs_commit, 16)  # a commit id, not an escape sequence around one
    pin1 = first.pinned["db"]
    assert pin1 is not None
    listing = next(f for f in repo.vcs.files_at(first.vcs_commit, ".tether/listings"))
    assert listing.endswith(".jsonl")

    repo.new(bookmark="work")
    _write(repo, "db", {"x": 1})
    second = repo.commit("work")
    pin2 = second.pinned["db"]
    assert pin2 is not None and pin2.id != pin1.id
    assert repo.promote().trunk_moved == second.vcs_commit
    assert repo.vcs.bookmarks() == {
        "main": second.vcs_commit,
        "work": second.vcs_commit,
    }

    # Both commits are history, so both pins are referenced and nothing goes.
    plan = repo.plan_gc()
    assert not [a for a in plan.actions if a.op == "unpin"], plan.render()
    store = default_store().system(system)
    assert pin1.ref in store.tags and pin2.ref in store.tags
    assert all(r.ok for r in repo.verify(all_history=True).values())
    assert repo.vcs_drift() == []
    st = repo.status()
    assert st.bookmark_drift == [] and all(not o.changed for o in st.objects)


def _commit_id(root: Path, revset: str) -> str:
    return _jj(root, "log", "--no-graph", "-r", revset, "-T", "self.commit_id()")


def test_promote_refuses_a_conflicted_trunk_under_hostile_aliases(
    vcs_root: Path, hostile_vcs_config: Path
) -> None:
    """`conflict = 'false'` in the user's template aliases hid the trunk's
    two targets from promote's guard, and `conflicts()` aliased to nothing
    hid them from gc's."""
    repo = Repo.init(vcs_root)
    if repo.vcs.kind != "jj":
        pytest.skip("jj conflicted bookmarks")
    _mem_object(repo)
    repo.commit("baseline")
    repo.new(bookmark="feat")
    _write(repo, "db", {"x": "feat"})
    repo.commit("feat write")
    _jj(vcs_root, "new", "--no-edit", "main", "-m", "landed elsewhere 1")
    _jj(vcs_root, "new", "--no-edit", "main", "-m", "landed elsewhere 2")
    s1 = _commit_id(vcs_root, 'description(glob:"landed elsewhere 1*")')
    s2 = _commit_id(vcs_root, 'description(glob:"landed elsewhere 2*")')
    op = _jj(vcs_root, "op", "log", "--no-graph", "-n1", "-T", "self.id()").strip()
    _jj(vcs_root, "bookmark", "set", "main", "-r", s1)
    _jj(vcs_root, "--at-op", op, "bookmark", "set", "main", "-r", s2)
    repo = Repo.find(vcs_root)
    assert repo.vcs.conflicted_bookmarks() == ["main"]
    assert "feat" in repo.vcs.bookmarks()  # names as jj has them, no suffix
    with pytest.raises(VcsError, match="conflict"):
        repo.plan_gc()
    plan = repo.plan_promote()
    assert plan.actions and all(a.op == "refuse" for a in plan.actions)
    assert "conflicting targets" in plan.actions[0].detail


def test_gc_and_drop_refuse_a_conflicted_manifest_under_hostile_aliases(
    vcs_root: Path, hostile_vcs_config: Path
) -> None:
    repo = Repo.init(vcs_root)
    if repo.vcs.kind != "jj":
        pytest.skip("jj conflicts")
    _mem_object(repo)
    repo.commit("baseline")
    repo.new(bookmark="feat")
    _write(repo, "db", {"x": "feat"})
    repo.commit("feat write")
    repo.new("main")
    _write(repo, "db", {"x": "main"})
    repo.commit("trunk write")
    _jj(vcs_root, "rebase", "-b", "feat", "-d", "main")
    repo = Repo.find(vcs_root)
    assert repo.vcs.conflicted_commits() == [_commit_id(vcs_root, "feat")]
    with pytest.raises(VcsError, match="conflict"):
        repo.plan_gc()
    with pytest.raises(VcsError, match="conflict"):
        repo.plan_drop("feat")


def test_a_gc_plan_outlives_a_working_copy_snapshot_under_hostile_aliases(
    vcs_root: Path, hostile_vcs_config: Path
) -> None:
    """The history digest leaves the working-copy commits out, which jj
    rewrites on every snapshot. With `working_copies()` aliased to nothing
    it counted them, and any edit between plan and apply staled the plan."""
    repo = Repo.init(vcs_root)
    if repo.vcs.kind != "jj":
        pytest.skip("jj working-copy commits")
    readme = vcs_root / "README"
    readme.write_text("one\n", encoding="utf-8")
    _jj(vcs_root, "file", "track", "README")
    _mem_object(repo)
    repo.commit("baseline")
    plan = repo.plan_gc()
    readme.write_text("two\n", encoding="utf-8")
    _jj(vcs_root, "status")  # the edit lands in the working-copy commit
    repo.apply_gc(plan)


def test_exclusive_commits_never_include_the_root_under_hostile_aliases(
    vcs_root: Path, hostile_vcs_config: Path
) -> None:
    """With nothing else keeping history visible, only the removal of jj's
    root commit kept it out of a bookmark's commits; `root()` aliased to
    nothing put it back, and `drop` would ask jj to abandon it."""
    repo = Repo.init(vcs_root)
    if repo.vcs.kind != "jj":
        pytest.skip("jj root commit")
    (vcs_root / "a").write_text("a\n", encoding="utf-8")
    _jj(vcs_root, "file", "track", "a")
    _jj(vcs_root, "commit", "-m", "only")
    _jj(vcs_root, "bookmark", "delete", "main")
    _jj(vcs_root, "bookmark", "create", "solo", "-r", "@-")
    assert _commit_id(vcs_root, "solo-") == "0" * 40
    assert repo.vcs.exclusive_commits("solo") == [_commit_id(vcs_root, "solo")]


@pytest.mark.parametrize("first", ["fresh", "new-key", "nested-key"])
def test_commit_after_a_no_vcs_commit_lands_manifests_no_commit_tracked_yet(
    vcs_root: Path, hostile_vcs_config: Path, first: str
) -> None:
    """`commit --no-vcs` leaves manifests the VCS has never tracked; the next
    `commit` must commit them. Under `status.showUntrackedFiles = no` git
    listed nothing, `dirty` said clean, and `commit` did nothing without an
    error -- for the whole `.tether/` of a fresh dataset, a new key in an
    already-tracked directory, and a key nested in a new subdirectory."""
    repo = Repo.init(vcs_root)
    if first == "fresh":
        _mem_object(repo)
        keys = ["db"]
    else:
        _mem_object(repo)
        assert repo.commit("baseline").vcs_commit is not None
        key = "extra" if first == "new-key" else "plates/2026/raw"
        _mem_object(repo, key)
        keys = ["db", key]
    silent = repo.commit("pins only", vcs=False)
    assert silent.vcs_commit is None
    assert repo.vcs.dirty(repo._vcs_paths())
    landed = repo.commit("now the manifests")
    assert landed.vcs_commit is not None
    committed = repo._objects_at(landed.vcs_commit)
    assert sorted(committed) == sorted(keys)
    assert all(committed[k].state == repo.objects[k].state for k in keys)
    assert not repo.vcs.dirty(repo._vcs_paths())


@pytest.mark.parametrize("config", ["default", "hostile"])
@pytest.mark.parametrize("how", ["new", "new-bookmark-at-main", "drop"])
@pytest.mark.parametrize("path", ["notes.txt", "scratch/deep/notes.txt"])
def test_a_new_file_of_the_users_stays_in_the_change_it_was_made_in(
    vcs_root: Path, request: pytest.FixtureRequest, config: str, how: str, path: str
) -> None:
    """jj: a file the user created and no jj command has snapshotted yet,
    then a tether command that moves the working copy. tether's snapshots
    tracked nothing new, so the file survived the move on disk and the
    user's next jj command committed it into the destination bookmark. It
    belongs to the change it was made in -- or, under the user's own
    `auto-track = "none()"`, to no change, as their jj would leave it."""
    if config == "hostile":
        request.getfixturevalue("hostile_vcs_config")
    repo = Repo.init(vcs_root)
    if repo.vcs.kind != "jj":
        pytest.skip("jj snapshots the working copy")
    _mem_object(repo)
    repo.commit("baseline")
    repo.new(bookmark="feat")
    _write(repo, "db", {"x": 1})
    repo.commit("feat work")
    made_in = repo.vcs.position()["id"]
    user_file = vcs_root / path
    user_file.parent.mkdir(parents=True, exist_ok=True)
    user_file.write_text("mine\n", encoding="utf-8")
    if how == "new":
        repo.new("main")
    elif how == "new-bookmark-at-main":
        repo.new("main", bookmark="other")
    else:
        repo.drop("feat")
    _jj(vcs_root, "status")  # the user's next command: a snapshot, their config
    assert path not in _jj(vcs_root, "file", "list", "-r", "@").split()
    assert path not in _jj(vcs_root, "file", "list", "-r", "@-").split()
    if config == "default":
        assert path in _jj(vcs_root, "file", "list", "-r", made_in).split()
        assert not user_file.exists()
    else:
        assert user_file.read_text(encoding="utf-8") == "mine\n"


@pytest.mark.parametrize("how", ["new", "undo"])
def test_tethers_own_new_manifest_follows_the_checkout(
    vcs_root: Path, how: str
) -> None:
    """jj, default auto-track: the user's new file lands in the change it was
    made in, but a manifest an uncommitted `add` wrote is tether's, tracked
    by name when a commit takes it. Snapshotted into the change being left,
    it vanished from the checkout -- `undo` of an older `new -b` lost the
    later `add` and left a stray commit holding it."""
    repo = Repo.init(vcs_root)
    if repo.vcs.kind != "jj":
        pytest.skip("jj snapshots the working copy")
    _mem_object(repo)
    repo.commit("baseline")
    repo.new(bookmark="feat")
    new_op = repo.ops()[0]
    made_in = repo.vcs.position()["id"]
    _mem_object(repo, "aux")
    (vcs_root / "notes.txt").write_text("mine\n", encoding="utf-8")
    if how == "new":
        repo.new("main")
    else:
        repo.undo(new_op.id)
    assert "aux" in Repo.find(vcs_root).objects
    assert "notes.txt" in _jj(vcs_root, "file", "list", "-r", made_in).split()
    assert not (vcs_root / "notes.txt").exists()
    heads = _jj(
        vcs_root, "log", "--no-graph", "-r", "heads(::) ~ @", "-T", 'change_id ++ "\\n"'
    ).split()
    for head in heads:
        listed = _jj(vcs_root, "file", "list", "-r", head).split()
        assert ".tether/objects/aux.toml" not in listed, head


def test_abandon_restores_later_manifests_under_a_hostile_config(
    vcs_root: Path, hostile_vcs_config: Path
) -> None:
    """`abandon` writes a descendant's manifests back as they were; one the
    abandoned commit had added is a new file there, which a user's
    `auto-track = "none()"` left out of the rewritten commit."""
    repo = Repo.init(vcs_root)
    if repo.vcs.kind != "jj":
        pytest.skip("jj snapshots; git rewrites through plumbing")
    _mem_object(repo, "base")
    repo.commit("baseline")
    _mem_object(repo, "a")
    first = repo.commit("adds a")
    _mem_object(repo, "b")
    repo.commit("adds b")
    assert first.vcs_commit is not None
    repo.abandon([first.vcs_commit])
    tip = repo.vcs.bookmarks()["main"]
    assert sorted(repo._objects_at(tip)) == ["a", "b", "base"]
    assert not repo.vcs.dirty(repo._vcs_paths())


@pytest.mark.parametrize("how", ["rewrite", "abandon", "new-bookmark"])
def test_per_checkout_files_never_reach_an_older_commit(
    vcs_root: Path, how: str
) -> None:
    """jj: tether parks the working copy on older commits to rewrite them
    (`upgrade`, `abandon`) or starts a bookmark at one. An older
    `.tether/.gitignore` may not ignore `secrets.toml` or the op log, and a
    snapshot there under the user's auto-track (`all()` by default) squashed
    them into history. Snapshots taken while parked track nothing new."""
    from tether.vcs import JjAdapter

    if not (vcs_root / ".jj").is_dir():
        pytest.skip("jj snapshots the working copy")
    vcs = JjAdapter(vcs_root)
    tdir = vcs_root / ".tether"
    (tdir / "objects").mkdir(parents=True)
    (tdir / ".gitignore").write_text("/workspace.toml\n", encoding="utf-8")
    (tdir / "objects" / "a.toml").write_text("v = 1\n", encoding="utf-8")
    old = vcs.commit([".tether"], "a layout from before the op log")
    (tdir / ".gitignore").write_text(
        "/workspace.toml\n/ops.jsonl\n/secrets.toml\n", encoding="utf-8"
    )
    (tdir / "objects" / "b.toml").write_text("v = 1\n", encoding="utf-8")
    mid = vcs.commit([".tether"], "ignore the per-checkout files")
    (tdir / "objects" / "a.toml").write_text("v = 2\n", encoding="utf-8")
    vcs.commit([".tether"], "later")
    (tdir / "secrets.toml").write_text('[vcs]\ngit_path = "/x"\n', encoding="utf-8")
    (tdir / "ops.jsonl").write_text("{}\n", encoding="utf-8")
    if how == "rewrite":
        mapping = vcs.rewrite_history(
            ".tether/objects",
            lambda _c, files: {
                p: t.replace("v = 1", "v = 0") for p, t in files.items()
            },
        )
        assert mapping
    elif how == "abandon":
        vcs.abandon([mid], ".tether/objects")
    else:
        vcs.new_bookmark("probe", old)
    commits = _jj(
        vcs_root,
        "log",
        "--ignore-working-copy",
        "--no-graph",
        "-r",
        "::@ ~ root()",
        "-T",
        'commit_id ++ "\\n"',
    ).split()
    for commit in commits:
        listed = _jj(
            vcs_root, "file", "list", "--ignore-working-copy", "-r", commit
        ).split()
        assert ".tether/secrets.toml" not in listed, (how, commit)
        assert ".tether/ops.jsonl" not in listed, (how, commit)
    assert (tdir / "secrets.toml").is_file() and (tdir / "ops.jsonl").is_file()


def test_commit_refuses_to_report_success_when_the_manifests_were_left_out(
    vcs_root: Path,
) -> None:
    """A dataset under a directory the VCS ignores: git refuses at `add`, jj
    makes an empty commit and calls it done. Either way `commit` must raise,
    and under jj the landed (empty) commit is journaled as such."""
    (vcs_root / ".gitignore").write_text("data/\n", encoding="utf-8")
    repo = Repo.init(vcs_root / "data")
    _mem_object(repo)
    with pytest.raises(VcsError, match=r"ignored|manifest"):
        repo.commit("baseline")
    ops = repo.ops()
    assert ops and ops[0].command == "commit" and not ops[0].incomplete
    if repo.vcs.kind == "jj":
        assert "failed_after_commit" in ops[0].result
        assert "does not contain 1 of the dataset's manifest" in str(
            ops[0].result["failed_after_commit"]
        )
    else:
        assert ops[0].result.get("rolled_back") is True


def test_a_git_hook_that_refuses_the_commit_leaves_index_and_store_clean(
    vcs_root: Path,
) -> None:
    """r9 / e8: `git add` succeeded, `git commit` failed on a pre-commit hook.
    The rollback releases the pin and restores the working tree; the index
    must not keep a manifest naming the released pin, or the user's next
    plain `git commit` records a dataset commit `verify` calls missing."""
    repo = Repo.init(vcs_root)
    if repo.vcs.kind != "git":
        pytest.skip("git hooks")
    system = _mem_object(repo)
    repo.commit("baseline")
    store = default_store()
    store.write(system, "main", {"v": 1})
    hooks = vcs_root / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'lint failed' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(
        ["git", "config", "core.hooksPath", str(hooks)], cwd=vcs_root, check=True
    )
    tags_before = set(store.system(system).tags)
    with pytest.raises(VcsError, match="lint failed"):
        repo.commit("v1")
    assert set(store.system(system).tags) == tags_before  # the new pin is gone
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=vcs_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert staged == ""
    subprocess.run(["git", "config", "--unset", "core.hooksPath"], cwd=vcs_root)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "the user's own commit"],
        cwd=vcs_root,
        check=True,
    )
    assert all(r.ok for r in Repo.find(vcs_root).verify(rev="HEAD").values())
