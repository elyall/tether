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
