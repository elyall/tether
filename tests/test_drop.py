"""`tether drop BOOKMARK`: the opposite of `promote`, in one step -- leave the
bookmark, drop the commits only it reaches, delete it, release what it held
in the stores."""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest
from typer import testing as typer_testing

from tether.backends.memory import MemoryBackend, default_store
from tether.cli import app
from tether.errors import BackendError, ConfigError, MultiObjectError, StalePlanError
from tether.handles import MemoryHandle
from tether.plan import Plan
from tether.repo import Repo

runner = typer_testing.CliRunner()


def _mem(repo: Repo, key: str) -> MemoryHandle:
    handle = repo.open(key)
    assert isinstance(handle, MemoryHandle)
    return handle


def _baseline(vcs_root: Path) -> tuple[Repo, str]:
    repo = Repo.init(vcs_root)
    system = f"sys-{uuid.uuid4().hex[:8]}"
    default_store().system(system)
    repo.add("db", "memory", {"system": system, "branch": "main"})
    repo.commit("baseline")
    return repo, system


def _probe(repo: Repo, name: str = "probe") -> tuple[str, str, str]:
    """A bookmark with one committed write on `db`: (commit, fork branch, pin id)."""
    repo.new(bookmark=name)
    _mem(repo, "db").write({"x": name})
    result = repo.commit(f"{name} write")
    assert result.vcs_commit is not None
    pin = result.pinned["db"]
    assert pin is not None
    return result.vcs_commit, repo.workspace.working_refs["db"], pin.id


def _other_checkout(repo: Repo, vcs_root: Path, tmp_path: Path) -> Repo:
    """A second checkout of the repository at this one's commit."""
    other_root = tmp_path / "other-checkout"
    cmd = (
        ["jj", "workspace", "add", str(other_root)]
        if repo.vcs.kind == "jj"
        else ["git", "worktree", "add", "--detach", str(other_root)]
    )
    subprocess.run(cmd, cwd=vcs_root, check=True, capture_output=True)
    return Repo.find(other_root)


def test_drop_from_the_bookmark_you_are_on(vcs_root: Path) -> None:
    """The headline: one command. The plan lists every step and previews the
    store side as it will be once the commits are gone; apply does exactly
    that and leaves the checkout on the trunk."""
    repo, system = _baseline(vcs_root)
    commit, fork, pin = _probe(repo)
    store = default_store().system(system)

    plan = repo.plan_drop("probe")
    ops = [(a.op, a.target) for a in plan.actions]
    assert ops == [
        ("leave-bookmark", "main"),
        ("abandon-commit", commit[:12]),
        ("delete-bookmark", "probe"),
        ("unpin", f"tether.{pin}"),
        ("delete-branch", fork),
    ]
    assert (
        next(a for a in plan.actions if a.op == "abandon-commit").detail
        == "probe write"
    )
    branch = next(a for a in plan.actions if a.op == "delete-branch")
    assert "was committed by a dropped commit" in branch.detail
    assert plan.context["leave"] == "main" and plan.context["commits"] == [commit]
    assert {p.kind for p in plan.preconditions} >= {"bookmark_head", "no_new_holders"}
    assert any("re-planned at apply" in n for n in plan.notes)

    report = repo.apply_drop(plan)
    assert report.left_for == "main" and report.abandoned == [commit]
    assert repo.workspace.bookmark == "main"
    assert "probe" not in repo.vcs.bookmarks()
    assert commit not in repo.vcs.history_revs()
    assert report.gc_report is not None
    assert report.gc_report.deleted_working_refs == {"db": [fork]}
    assert report.gc_report.unpinned == {"memory": [pin]}
    assert fork not in store.branches
    assert f"tether.{pin}" not in store.tags
    assert store.branches["main"] == f"{system}:s0"  # main untouched
    # Journaled, not undoable (like abandon): the VCS's undo is the way back.
    ops_log = repo.ops()
    drop = next(e for e in ops_log if e.command == "drop")
    assert not drop.undoable
    assert "dropped probe: 1 commit(s), 1 pin(s), 1 branch(es)" in drop.summary()


def test_drop_from_elsewhere_needs_no_leave(vcs_root: Path) -> None:
    repo, system = _baseline(vcs_root)
    commit, fork, _pin = _probe(repo)
    repo.new("main")
    plan = repo.plan_drop("probe")
    assert [a.op for a in plan.actions][:2] == ["abandon-commit", "delete-bookmark"]
    assert plan.context["leave"] is None
    report = repo.drop("probe")
    assert report.left_for is None and report.abandoned == [commit]
    assert fork not in default_store().system(system).branches


def test_drop_to_a_bookmark_of_your_choice(vcs_root: Path) -> None:
    repo, _system = _baseline(vcs_root)
    repo.new(bookmark="sweep")
    _mem(repo, "db").write({"x": "sweep"})
    repo.commit("sweep write")
    commit, _fork, _pin = _probe(repo)  # probe branches off sweep
    plan = repo.plan_drop("probe", to="sweep")
    assert plan.actions[0].op == "leave-bookmark" and plan.actions[0].target == "sweep"
    report = repo.apply_drop(plan)
    assert report.left_for == "sweep" and repo.workspace.bookmark == "sweep"
    assert report.abandoned == [commit]
    assert "sweep" in repo.vcs.bookmarks()
    with pytest.raises(ConfigError, match="no bookmark 'nowhere'"):
        repo.plan_drop("sweep", to="nowhere")


def test_drop_keeps_commits_another_bookmark_also_reaches(vcs_root: Path) -> None:
    """Only the line that leaves visible history with the bookmark is dropped:
    a bookmark that branched off it keeps its base commits."""
    repo, _system = _baseline(vcs_root)
    repo.new(bookmark="probe")
    _mem(repo, "db").write({"x": 1})
    shared = repo.commit("shared base").vcs_commit
    repo.new(bookmark="keep")  # off probe's tip: reaches `shared`
    repo.new("probe")
    _mem(repo, "db").write({"x": 2})
    only = repo.commit("probe only").vcs_commit
    assert shared and only

    plan = repo.plan_drop("probe")
    assert [a.target for a in plan.actions if a.op == "abandon-commit"] == [only[:12]]
    report = repo.apply_drop(plan)
    assert report.abandoned == [only]
    history = repo.vcs.history_revs()
    assert shared in history and only not in history
    assert repo.vcs.bookmarks()["keep"] is not None
    # `keep` still works: its manifest still names the shared pin.
    assert repo._objects_at("keep")["db"].pin is not None


def test_drop_refuses_the_trunk_an_unknown_bookmark_and_a_held_one(
    vcs_root: Path, tmp_path: Path
) -> None:
    repo, _system = _baseline(vcs_root)
    with pytest.raises(ConfigError, match="trunk"):
        repo.plan_drop("main")
    with pytest.raises(ConfigError, match="no bookmark 'nope'"):
        repo.plan_drop("nope")
    _probe(repo)
    repo.new("main")
    other = _other_checkout(repo, vcs_root, tmp_path)
    other.new("probe")  # someone is working there
    with pytest.raises(ConfigError, match="worked on by checkout"):
        Repo.find(vcs_root).plan_drop("probe")


def test_drop_keeps_a_branch_with_writes_no_commit_pinned(vcs_root: Path) -> None:
    """Committed work on the bookmark is thrown away by name; writes nobody
    committed are not -- the branch is kept and said so, `--force-prune`
    overrides."""
    repo, system = _baseline(vcs_root)
    _commit, fork, _pin = _probe(repo)
    default_store().write(system, fork, {"x": "uncommitted"})
    report = repo.drop("probe")
    assert report.gc_report is not None
    assert report.gc_report.kept_working_refs == {"db": [fork]}
    assert fork in default_store().system(system).branches
    # The bookmark is gone; a later sweep with force releases the branch.
    forced = repo.gc(dry_run=False, prune_bookmarks=True, force_prune=True)
    assert forced.deleted_working_refs == {"db": [fork]}


def test_drop_with_delete_stores_reclaims_the_created_store(vcs_root: Path) -> None:
    repo, _system = _baseline(vcs_root)
    repo.new(bookmark="probe")
    scratch = f"sys-{uuid.uuid4().hex[:8]}"
    handle = repo.create("scratch/probe", "memory", {"system": scratch})
    assert isinstance(handle, MemoryHandle)
    handle.write({"x": 1})
    repo.commit("probe write")
    plan = repo.plan_drop("probe", delete_stores=True)
    assert plan.actions[-1].op == "delete-store"
    report = repo.apply_drop(plan)
    assert report.gc_report is not None
    assert report.gc_report.deleted_stores == {"scratch/probe": scratch}
    assert scratch not in default_store().systems
    assert not repo.created_stores()


def test_drop_plan_is_stale_when_the_bookmark_moves(vcs_root: Path) -> None:
    repo, _system = _baseline(vcs_root)
    _probe(repo)
    repo.new("main")
    plan = repo.plan_drop("probe")
    saved = Plan.from_dict(json.loads(plan.to_json()))
    repo.new("probe")
    _mem(repo, "db").write({"x": "later"})
    later = repo.commit("later").vcs_commit
    repo.new("main")
    with pytest.raises(StalePlanError, match="since the drop plan was made"):
        repo.apply_drop(saved)
    assert later in repo.vcs.history_revs() and "probe" in repo.vcs.bookmarks()


def test_saved_drop_plan_applies_only_in_the_checkout_that_made_it(
    vcs_root: Path, tmp_path: Path
) -> None:
    """Whether to leave the bookmark is decided from where the planning
    checkout stands. Another checkout at the same commit sees the same head
    and history, so only the workspace binding stops it applying the plan."""
    repo, system = _baseline(vcs_root)
    commit, fork, _pin = _probe(repo)
    repo.new("main")
    plan = repo.plan_drop("probe")
    saved = Plan.from_dict(json.loads(plan.to_json()))
    other = _other_checkout(repo, vcs_root, tmp_path)
    assert other._vcs_head_or_none() == repo._vcs_head_or_none()

    with pytest.raises(StalePlanError, match="made in another checkout"):
        other.apply_drop(saved)
    assert "probe" in repo.vcs.bookmarks() and commit in repo.vcs.history_revs()
    assert not [e for e in other.ops() if e.command == "drop"]
    # Where it was made, the same plan still holds.
    report = Repo.find(vcs_root).apply_drop(saved)
    assert report.abandoned == [commit]
    assert fork not in default_store().system(system).branches


def test_a_drop_plan_from_before_the_binding_is_bound_by_its_context(
    vcs_root: Path, tmp_path: Path
) -> None:
    """Plans saved by 0.1.0b3 carry the workspace id in their context but not
    as a precondition; apply compares it anyway."""
    repo, _system = _baseline(vcs_root)
    commit, _fork, _pin = _probe(repo)
    repo.new("main")
    old = repo.plan_drop("probe").to_dict()
    old["preconditions"] = [
        p for p in old["preconditions"] if p["kind"] != "workspace_id"
    ]
    other = _other_checkout(repo, vcs_root, tmp_path)
    with pytest.raises(StalePlanError, match="made in another checkout"):
        other.apply_drop(Plan.from_dict(old))
    assert "probe" in repo.vcs.bookmarks() and commit in repo.vcs.history_revs()


def test_a_saved_drop_plan_applies_in_a_checkout_that_had_no_workspace_file(
    vcs_root: Path, tmp_path: Path
) -> None:
    """The workspace id binds a saved plan to its checkout, so it must outlive
    the `Repo` that minted it. A fresh checkout (a clone, a new jj workspace
    or git worktree) had no `workspace.toml`, every `Repo` there minted an id
    of its own, and `--from-plan` there always refused."""
    repo, _system = _baseline(vcs_root)
    commit, _fork, _pin = _probe(repo)
    repo.new("main")
    other_root = _other_checkout(repo, vcs_root, tmp_path).root
    saved = Repo.find(other_root).plan_drop("probe").to_json()
    report = Repo.find(other_root).apply_drop(Plan.from_dict(json.loads(saved)))
    assert report.abandoned == [commit]


@pytest.mark.parametrize("edits", [False, True])
def test_drop_refuses_when_the_vcs_and_the_workspace_file_disagree(
    vcs_root: Path, edits: bool
) -> None:
    """After a `git switch probe` (or `jj new probe`) by hand, `workspace.toml`
    still says main. Whether to leave was decided from the VCS, and git
    refused to delete the checked-out branch half-way through the drop; jj
    saw no bookmark at all once the working copy had edits, planned no leave,
    and the abandon rebased the working copy onto the trunk. Neither side is
    trusted alone now: the plan's `workspace_bookmark` precondition wants
    them to agree, and refuses before anything is journaled."""
    if edits and not (vcs_root / ".jj").exists():
        pytest.skip("only a jj working copy carries edits as a commit of its own")
    repo, system = _baseline(vcs_root)
    commit, fork, _pin = _probe(repo)
    repo.new("main")
    cmd = (
        ["git", "switch", "-q", "probe"]
        if repo.vcs.kind == "git"
        else ["jj", "new", "probe"]
    )
    subprocess.run(cmd, cwd=vcs_root, check=True, capture_output=True)
    if edits:
        (vcs_root / "notes.txt").write_text("my uncommitted work\n")
    repo = Repo.find(vcs_root)
    assert repo.workspace.bookmark == "main"
    assert repo._vcs_bookmarks_here() == ["probe"]
    plan = repo.plan_drop("probe")
    with pytest.raises(StalePlanError, match="this checkout is on probe now"):
        repo.apply_drop(plan)
    with pytest.raises(StalePlanError, match="`tether new` to settle it"):
        repo.drop("probe")
    assert "probe" in repo.vcs.bookmarks() and commit in repo.vcs.history_revs()
    assert fork in default_store().system(system).branches
    assert not [e for e in repo.ops() if e.command == "drop"]
    if edits:
        assert (vcs_root / "notes.txt").read_text() == "my uncommitted work\n"
    # Settled either way, the drop goes through: onto probe, it leaves first.
    repo.new("probe")
    report = repo.drop("probe")
    assert report.left_for == "main" and report.abandoned == [commit]
    assert repo.workspace.bookmark == "main" and "probe" not in repo.vcs.bookmarks()
    assert not repo.incomplete_ops()


def test_a_drop_whose_store_half_fails_closes_its_journal_entry(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commits and the bookmark are gone when the store half fails, so
    re-running `drop` cannot finish it. The entry ends with the failure, as
    gc's does, instead of staying `started`; a plain gc finishes the job."""
    repo, _system = _baseline(vcs_root)
    commit, _fork, pin = _probe(repo)

    def unavailable(self: MemoryBackend, locator: dict, pin: object) -> None:
        raise BackendError("store unavailable", kind="memory")

    with monkeypatch.context() as m:
        m.setattr(MemoryBackend, "unpin", unavailable)
        with pytest.raises(MultiObjectError):
            repo.drop("probe")
    assert not repo.incomplete_ops()
    (entry,) = [e for e in repo.ops() if e.command == "drop"]
    assert entry.result["abandoned"] == [commit]
    assert "store unavailable" in str(entry.result["failed"])
    report = repo.gc(dry_run=False)
    assert report.unpinned == {"memory": [pin]}


def test_drop_plan_is_stale_when_this_checkout_moves_onto_the_bookmark(
    vcs_root: Path,
) -> None:
    """A bookmark with no commits of its own sits on its base's commit, so a
    `new` onto it moves neither the bookmark nor the head the plan bound to.
    The plan's decision not to leave no longer holds: dropping the bookmark
    under this checkout would leave it on nothing."""
    repo, _system = _baseline(vcs_root)
    repo.new(bookmark="probe")
    repo.new("main")
    plan = repo.plan_drop("probe")
    assert plan.context["leave"] is None and plan.context["commits"] == []
    repo.new("probe")
    with pytest.raises(StalePlanError, match="this checkout is on probe now"):
        repo.apply_drop(plan)
    assert "probe" in repo.vcs.bookmarks() and repo.workspace.bookmark == "probe"
    assert not [e for e in repo.ops() if e.command == "drop"]


def test_drop_plan_is_stale_when_another_bookmark_reaches_its_commits(
    vcs_root: Path,
) -> None:
    """The plan promised a set of commits only this bookmark reaches. Another
    bookmark set onto the line in between (a `promote`, a `jj bookmark set`)
    moves neither the dropped bookmark nor this checkout, and changes no
    visible commit -- so only re-deriving the set at apply time can catch it,
    and it must: jj would otherwise abandon commits another bookmark sits on."""
    repo, _system = _baseline(vcs_root)
    commit, fork, _pin = _probe(repo)
    repo.new("main")
    plan = repo.plan_drop("probe")
    saved = Plan.from_dict(json.loads(plan.to_json()))
    assert saved.context["commits"] == [commit]

    repo.vcs.bookmark_set("keep", commit)  # a second bookmark now reaches it
    with pytest.raises(StalePlanError, match="another bookmark now reaches"):
        repo.apply_drop(saved)
    assert commit in repo.vcs.history_revs()
    assert repo.vcs.bookmarks()["probe"] == commit
    assert fork in default_store().system(_system).branches
    # A fresh plan has nothing exclusive to abandon; the bookmark still goes.
    fresh = repo.plan_drop("probe")
    assert not [a for a in fresh.actions if a.op == "abandon-commit"]
    assert fresh.actions[0].op == "delete-bookmark"


def _tag(root: Path, kind: str, name: str, commit: str) -> None:
    subprocess.run(
        ["git", "tag", name, commit], cwd=root, check=True, capture_output=True
    )
    if kind == "jj":
        subprocess.run(
            ["jj", "git", "import"], cwd=root, check=True, capture_output=True
        )


def test_drop_counts_a_tag_as_a_reacher(vcs_root: Path) -> None:
    """The plan's premise -- these commits leave history -- must hold. A tag
    on the line keeps the commit visible whatever happens to the bookmark, so
    it is not exclusive: nothing is abandoned, nothing is unpinned, and the
    branch is judged as `gc` would (its head is still pinned, so it goes for
    that reason -- not because a commit was dropped)."""
    repo, system = _baseline(vcs_root)
    commit, fork, pin = _probe(repo)
    repo.new("main")
    _tag(vcs_root, repo.vcs.kind, "v-probe", commit)

    plan = repo.plan_drop("probe")
    assert not [a for a in plan.actions if a.op == "abandon-commit"]
    assert not [a for a in plan.actions if a.op == "unpin"]
    branch = next(a for a in plan.actions if a.op == "delete-branch")
    assert "head is pinned" in branch.detail
    report = repo.apply_drop(plan)
    assert report.abandoned == []
    assert commit in repo.vcs.history_revs()  # the tag keeps it
    assert "probe" not in repo.vcs.bookmarks()
    store = default_store().system(system)
    assert f"tether.{pin}" in store.tags and fork not in store.branches


def test_drop_counts_a_remote_bookmark_as_a_reacher(vcs_root: Path) -> None:
    """A pushed bookmark outlives the local delete: `feature@origin` keeps the
    commits visible. The plan says so instead of promising an abandon."""
    repo, _system = _baseline(vcs_root)
    commit, _fork, _pin = _probe(repo)
    repo.new("main")
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/probe", commit],
        cwd=vcs_root,
        check=True,
        capture_output=True,
    )
    if repo.vcs.kind == "jj":
        subprocess.run(
            ["jj", "git", "import"], cwd=vcs_root, check=True, capture_output=True
        )
    expected = "probe@origin" if repo.vcs.kind == "jj" else "origin/probe"
    assert repo.vcs.remote_counterparts("probe") == [expected]

    plan = repo.plan_drop("probe")
    assert not [a for a in plan.actions if a.op == "abandon-commit"]
    assert any(expected in n and "still reach its commits" in n for n in plan.notes)
    report = repo.apply_drop(plan)
    assert report.abandoned == [] and commit in repo.vcs.history_revs()
    assert "probe" not in repo.vcs.bookmarks()


def test_drop_refuses_to_leave_for_a_bookmark_another_checkout_holds(
    vcs_root: Path, tmp_path: Path
) -> None:
    """`--to` must be a bookmark this checkout may join: `new` would refuse
    one another live checkout works on, and that refusal belongs in the plan,
    not half-way through the apply."""
    repo, _system = _baseline(vcs_root)
    repo.new(bookmark="side")
    repo.new("main")
    _other_checkout(repo, vcs_root, tmp_path).new("side")
    repo = Repo.find(vcs_root)
    _probe(repo)
    with pytest.raises(ConfigError, match="cannot leave for 'side'"):
        repo.plan_drop("probe", to="side")
    assert not [e for e in repo.ops() if e.command == "drop"]  # nothing began


def test_cli_drop(vcs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _system = _baseline(vcs_root)
    commit, fork, pin = _probe(repo)
    monkeypatch.chdir(vcs_root)
    r = runner.invoke(app, ["drop", "probe"])
    assert r.exit_code == 0, r.output
    assert "leave-bookmark" in r.output and "abandon-commit" in r.output
    assert "delete-branch" in r.output and "(not applied" in r.output
    assert "probe" in Repo.find(vcs_root).vcs.bookmarks()  # dry run by default
    r = runner.invoke(app, ["drop", "probe", "--no-dry-run", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["bookmark"] == "probe" and payload["left_for"] == "main"
    assert payload["abandoned"] == [commit]
    assert payload["gc"]["deleted_working_refs"] == {"db": [fork]}
    assert payload["gc"]["unpinned"] == {"memory": [pin]}
    r = runner.invoke(app, ["drop", "main"])
    assert r.exit_code == 1 and "trunk" in r.output
