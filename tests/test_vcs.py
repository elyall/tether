from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tether.vcs import detect_vcs


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_adapter_roundtrip(vcs_root: Path) -> None:
    vcs = detect_vcs(vcs_root)

    _write(vcs_root, ".tether/objects/a.toml", "key='a'\n")
    _write(vcs_root, "tether.toml", "v=1\n")
    c1 = vcs.commit([".tether", "tether.toml"], "first")
    assert c1 and vcs.resolve(c1) == c1

    # Read the file back at the commit.
    text = vcs.read_file_at(c1, ".tether/objects/a.toml")
    assert text is not None and "key='a'" in text
    assert vcs.read_file_at(c1, ".tether/objects/missing.toml") is None

    files = vcs.list_files_at(c1, ".tether/objects")
    assert ".tether/objects/a.toml" in files

    # A second commit shows up in history.
    _write(vcs_root, ".tether/objects/b.toml", "key='b'\n")
    c2 = vcs.commit([".tether", "tether.toml"], "second")
    assert c2 != c1
    history = vcs.history_revs()
    assert c1 in history and c2 in history

    b_at_c2 = vcs.list_files_at(c2, ".tether/objects")
    assert ".tether/objects/b.toml" in b_at_c2
    assert ".tether/objects/b.toml" not in vcs.list_files_at(c1, ".tether/objects")


def test_commit_info_and_refs(vcs_root: Path) -> None:
    vcs = detect_vcs(vcs_root)
    _write(vcs_root, "tether.toml", "v=1\n")
    c1 = vcs.commit(["tether.toml"], "first\n\nbody line")
    _write(vcs_root, "tether.toml", "v=2\n")
    c2 = vcs.commit(["tether.toml"], "second")

    infos = {i.commit_id: i for i in vcs.commit_info(vcs.history_revs())}
    assert {c1, c2} <= set(infos)
    assert infos[c2].parents == (c1,)
    assert infos[c1].message == "first\n\nbody line"
    assert infos[c2].message == "second"
    assert infos[c2].author_email and infos[c2].author_name
    assert infos[c2].authored_at[:4].isdigit() and "T" in infos[c2].committed_at
    if vcs.kind == "jj":
        assert len(infos[c2].change_id or "") == 32
    else:
        assert infos[c2].change_id is None
    # A subset request returns exactly that subset, in one call.
    assert [i.commit_id for i in vcs.commit_info([c1])] == [c1]
    assert vcs.commit_info([]) == []

    refs = vcs.refs()
    kinds = {r.kind for r in refs}
    assert "head" in kinds
    head = next(r for r in refs if r.kind == "head")
    assert head.commit_id == vcs.current_rev()
    if vcs.kind == "git":
        assert any(r.kind == "branch" and r.commit_id == c2 for r in refs)


def test_batched_reads_match_per_file_reads(vcs_root: Path) -> None:
    vcs = detect_vcs(vcs_root)
    _write(vcs_root, ".tether/objects/a.toml", "key='a'\n")
    _write(vcs_root, ".tether/objects/nested/deep/c.toml", "key='nested/deep/c'\n")
    _write(
        vcs_root, ".tether/objects/notes.txt", "ignored by the engine, still a file\n"
    )
    _write(vcs_root, "tether.toml", "v=1\n")
    c1 = vcs.commit([".tether", "tether.toml"], "first")
    _write(vcs_root, ".tether/objects/a.toml", "key='a'\nchanged=true\n")
    c2 = vcs.commit([".tether", "tether.toml"], "second")

    # files_at streams the whole subtree (nested dirs included) via cat-file.
    at_c1 = vcs.files_at(c1, ".tether/objects")
    assert at_c1 == {
        ".tether/objects/a.toml": "key='a'\n",
        ".tether/objects/nested/deep/c.toml": "key='nested/deep/c'\n",
        ".tether/objects/notes.txt": "ignored by the engine, still a file\n",
    }
    assert vcs.files_at(c2, ".tether/objects")[".tether/objects/a.toml"].endswith(
        "changed=true\n"
    )
    # Symbolic revisions resolve too; a missing directory is simply empty.
    assert vcs.files_at(c2, ".tether/objects") == vcs.files_at(
        "@-" if vcs.kind == "jj" else "HEAD", ".tether/objects"
    )
    assert vcs.files_at(c1, "no/such/dir") == {}

    # iter_history_files covers every commit and agrees with files_at.
    history = dict(vcs.iter_history_files(".tether/objects"))
    assert set(vcs.history_revs()) == set(history)
    assert history[c1] == at_c1
    assert history[c2] == vcs.files_at(c2, ".tether/objects")


def test_an_inherited_git_environment_does_not_retarget_the_adapter(
    vcs_root: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tether run from a git hook (or anything else exporting `GIT_DIR` and
    friends) must still commit to, and read history from, the repository it
    found the dataset in."""
    vcs = detect_vcs(vcs_root)
    _write(vcs_root, ".tether/objects/a.toml", "key='a'\n")
    c1 = vcs.commit([".tether"], "first")
    other = tmp_path_factory.mktemp("other")
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    subprocess.run(
        ["git", "-C", str(other), "commit", "-q", "--allow-empty", "-m", "x"],
        check=True,
    )
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.bare'='true'")

    vcs = detect_vcs(vcs_root)
    assert vcs.files_at(c1, ".tether/objects") == {
        ".tether/objects/a.toml": "key='a'\n"
    }
    _write(vcs_root, ".tether/objects/b.toml", "key='b'\n")
    c2 = vcs.commit([".tether"], "second")
    assert c1 in vcs.history_revs() and c2 in vcs.history_revs()
    assert set(vcs.files_at(c2, ".tether/objects")) == {
        ".tether/objects/a.toml",
        ".tether/objects/b.toml",
    }


def test_a_hostile_user_config_does_not_reach_the_adapter(
    vcs_root: Path, hostile_vcs_config: Path
) -> None:
    """Colour forced on, `all()` aliased away, new files never auto-tracked
    and capped at 1 KiB: every id still parses, history is still every
    commit, and tether's own files -- a listing well over the cap included --
    still land in the commit."""
    vcs = detect_vcs(vcs_root)
    _write(vcs_root, ".tether/objects/a.toml", "key='a'\n")
    _write(vcs_root, ".tether/listings/big.jsonl", "x" * 4096 + "\n")
    assert vcs.dirty([".tether/objects", ".tether/listings"])
    c1 = vcs.commit([".tether/objects", ".tether/listings"], "first")
    assert len(c1) == 40 and int(c1, 16) >= 0
    _write(vcs_root, ".tether/objects/b.toml", "key='b'\n")
    c2 = vcs.commit([".tether/objects"], "second", advance="main")
    assert vcs.resolve(c2) == c2 and vcs.bookmarks()["main"] == c2
    assert set(vcs.files_at(c2, ".tether")) == {
        ".tether/objects/a.toml",
        ".tether/objects/b.toml",
        ".tether/listings/big.jsonl",
    }
    assert {c1, c2} <= set(vcs.history_revs())
    assert {c1, c2} <= set(vcs.alive_commits([c1, c2]))
    assert dict(vcs.iter_history_files(".tether/objects"))[c1] == {
        ".tether/objects/a.toml": "key='a'\n"
    }
    infos = {i.commit_id: i for i in vcs.commit_info([c1, c2])}
    assert infos[c2].parents == (c1,) and infos[c1].message == "first"
    assert not vcs.dirty([".tether/objects"])
    # Untracked new files count as dirty, as git's `status --porcelain` says.
    _write(vcs_root, ".tether/objects/c.toml", "key='c'\n")
    assert vcs.dirty([".tether/objects"])


def test_jj_below_the_minimum_version_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tether import vcs as vcs_module
    from tether.errors import VcsError
    from tether.vcs import MIN_JJ_VERSION, JjAdapter

    calls: list[list[str]] = []
    banner = {"text": "jj 0.30.0\n"}

    def fake_run(argv: list[str], **kwargs: object) -> vcs_module._Run:
        calls.append(argv)
        if argv[1:] == ["--version"]:
            return vcs_module._Run(0, banner["text"], "")
        return vcs_module._Run(0, "abc\n", "")

    monkeypatch.setattr(vcs_module, "_run", fake_run)
    adapter = JjAdapter(tmp_path)
    with pytest.raises(VcsError, match=r"jj 0\.30\.0 is too old"):
        adapter.resolve("@")
    assert calls == [["jj", "--version"]]
    # Checked once per adapter; a supported version passes through and every
    # later call carries the isolation flags.
    banner["text"] = f"jj {'.'.join(str(n) for n in MIN_JJ_VERSION)}\n"
    fresh = JjAdapter(tmp_path)
    assert fresh.resolve("@") == "abc" and fresh.current_rev() == "abc"
    version_calls = [c for c in calls if c[1:] == ["--version"]]
    assert len(version_calls) == 2
    assert all("--color=never" in c and "--no-pager" in c for c in calls[-2:]), calls[
        -2:
    ]


def test_jj_history_walk_reads_every_side_of_a_conflicted_commit(
    vcs_root: Path,
) -> None:
    """A rebase that conflicts on a manifest stores the inputs beside the tree
    jj shows; the history walk yields each of them, so nothing a resolution
    could keep is invisible to gc, and `conflicts()` names the commit."""
    vcs = detect_vcs(vcs_root)
    if vcs.kind != "jj":
        pytest.skip("jj conflicts")
    _write(vcs_root, ".tether/objects/db.toml", "state = 'base'\n")
    base = vcs.commit([".tether/objects"], "base")
    vcs.bookmark_set("main", base)
    assert vcs.conflicted_bookmarks() == [] and vcs.conflicted_commits() == []
    _write(vcs_root, ".tether/objects/db.toml", "state = 'feat'\n")
    feat = vcs.commit([".tether/objects"], "feat", advance="main")
    vcs.bookmark_set("feat", feat)
    vcs.new(base)
    _write(vcs_root, ".tether/objects/db.toml", "state = 'trunk'\n")
    trunk = vcs.commit([".tether/objects"], "trunk")
    vcs.bookmark_set("main", trunk)
    subprocess.run(
        ["jj", "rebase", "-b", "feat", "-d", "main"],
        cwd=vcs_root,
        check=True,
        capture_output=True,
    )
    conflicted = vcs.bookmarks()["feat"]
    assert conflicted != feat
    assert vcs.conflicted_commits() == [conflicted]
    texts = {
        files[".tether/objects/db.toml"]
        for rev, files in vcs.iter_history_files(".tether/objects")
        if rev == conflicted
    }
    assert texts == {"state = 'base'\n", "state = 'feat'\n", "state = 'trunk'\n"}
    # The single-commit read keeps showing the tree jj materializes.
    assert vcs.files_at(conflicted, ".tether/objects") == {
        ".tether/objects/db.toml": "state = 'trunk'\n"
    }
    # A bookmark with two targets (a divergent move) is reported too.
    op = subprocess.run(
        ["jj", "op", "log", "--no-graph", "-n1", "-T", "id"],
        cwd=vcs_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    vcs.bookmark_set("main", base)
    subprocess.run(
        ["jj", "--at-op", op, "bookmark", "set", "main", "-r", conflicted],
        cwd=vcs_root,
        check=True,
        capture_output=True,
    )
    assert vcs.conflicted_bookmarks() == ["main"]
    assert "main" not in vcs.bookmarks()  # what made the guard necessary


def test_a_refused_git_commit_leaves_nothing_staged(vcs_root: Path) -> None:
    """`git add` then `git commit`: when a hook refuses the commit, the index
    must not keep the manifests `add` staged, or the user's next plain
    `git commit` records them."""
    from tether.errors import VcsError

    vcs = detect_vcs(vcs_root)
    if vcs.kind != "git":
        pytest.skip("git hooks")
    _write(vcs_root, ".tether/objects/a.toml", "key='a'\n")
    vcs.commit([".tether/objects"], "first")
    hooks = vcs_root / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'lint failed' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(
        ["git", "config", "core.hooksPath", str(hooks)], cwd=vcs_root, check=True
    )
    _write(vcs_root, ".tether/objects/a.toml", "key='a'\npin='new'\n")
    with pytest.raises(VcsError, match="lint failed"):
        vcs.commit([".tether/objects"], "second")
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=vcs_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert staged == ""
    assert vcs.dirty([".tether/objects"])  # the working-tree edit is untouched


def test_git_object_reader_parses_trees(vcs_root: Path) -> None:
    from tether.vcs import GitObjectReader, _parse_tree

    vcs = detect_vcs(vcs_root)
    _write(vcs_root, "dir/x.txt", "x")
    _write(vcs_root, "dir/sub/y.txt", "y")
    c = vcs.commit(["dir"], "tree")
    with GitObjectReader("git", vcs_root) as reader:
        obj = reader.fetch(f"{c}:dir")
        assert obj is not None and obj[1] == "tree"
        entries = {name: mode for mode, name, _ in _parse_tree(obj[2])}
        assert entries == {"x.txt": "100644", "sub": "40000"}
        assert reader.files(f"{c}:dir") == {"x.txt": "x", "sub/y.txt": "y"}
        assert reader.fetch(f"{c}:dir/none") is None
        assert reader.files(f"{c}:dir/x.txt") == {}  # a blob has no children


def test_detect_prefers_jj_when_colocated(vcs_root: Path) -> None:
    vcs = detect_vcs(vcs_root)
    assert vcs.kind in ("git", "jj")
    assert Path(vcs.root) == vcs_root


def test_new_never_leaves_git_detached(vcs_root: Path) -> None:
    vcs = detect_vcs(vcs_root)
    _write(vcs_root, "tether.toml", "v=1\n")
    c1 = vcs.commit(["tether.toml"], "first")
    _write(vcs_root, "tether.toml", "v=2\n")
    c2 = vcs.commit(["tether.toml"], "second")

    vcs.new(None)  # jj: fresh empty change on top; git: no-op
    if vcs.kind == "git":
        assert vcs.resolve("HEAD") == c2

    # Back to the first commit, then commit on top of it.
    vcs.new(c1)
    _write(vcs_root, "tether.toml", "v=3\n")
    c3 = vcs.commit(["tether.toml"], "third, off the first")
    assert c3 not in (c1, c2)
    assert {c1, c2, c3} <= set(vcs.history_revs())  # all still reachable
    if vcs.kind == "git":
        import subprocess

        head = subprocess.run(
            ["git", "-C", str(vcs_root), "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head == f"tether/{c1[:12]}"  # attached, not detached

        def current_branch() -> str:
            return subprocess.run(
                ["git", "-C", str(vcs_root), "branch", "--show-current"],
                capture_output=True,
                text=True,
            ).stdout.strip()

        # tether/<c1> has moved on to c3, so new(c1) must not reset it (that
        # would orphan c3): HEAD lands on c1 via a numbered sibling.
        vcs.new(c1)
        assert vcs.resolve("HEAD") == c1
        assert current_branch() == f"tether/{c1[:12]}-2"
        assert vcs.resolve(f"tether/{c1[:12]}") == c3
        assert c3 in vcs.history_revs()
        # A branch that still sits at the commit is reused, not duplicated.
        vcs.new(c1)
        assert current_branch() == f"tether/{c1[:12]}-2"
        # A branch name switches to that branch rather than parking a new one.
        vcs.new(f"tether/{c1[:12]}")
        assert current_branch() == f"tether/{c1[:12]}"
        assert vcs.resolve("HEAD") == c3


def test_rewrite_history_rewrites_files_and_keeps_the_shape(vcs_root: Path) -> None:
    vcs = detect_vcs(vcs_root)
    _write(vcs_root, "ds/.tether/objects/db.toml", "pin = 'old-1'\n")
    _write(vcs_root, "other.txt", "keep me\n")
    c1 = vcs.commit(["ds/.tether/objects", "other.txt"], "first")
    _write(vcs_root, "ds/.tether/objects/db.toml", "pin = 'old-2'\n")
    c2 = vcs.commit(["ds/.tether/objects"], "second")
    _write(vcs_root, "other.txt", "changed\n")
    c3 = vcs.commit(["other.txt"], "third (no manifest change)")
    before = vcs.position()
    seen: list[str] = []

    def transform(commit: str, files: dict[str, str]) -> dict[str, str]:
        seen.append(commit)
        return {p: t.replace("old-", "new-") for p, t in files.items()}

    mapping = vcs.rewrite_history("ds/.tether/objects", transform)
    # Every commit with a manifest was rewritten; c3 only because its parent was.
    assert set(mapping) == {c1, c2, c3}
    # The transform ran for the commits that touch the directory; c3 did not
    # (jj: not in files(); git: same subtree as c2, answer cached).
    assert set(seen) == {c1, c2}
    n1, n2, n3 = mapping[c1], mapping[c2], mapping[c3]
    assert vcs.read_file_at(n1, "ds/.tether/objects/db.toml") == "pin = 'new-1'\n"
    assert vcs.read_file_at(n2, "ds/.tether/objects/db.toml") == "pin = 'new-2'\n"
    assert vcs.read_file_at(n3, "ds/.tether/objects/db.toml") == "pin = 'new-2'\n"
    assert vcs.read_file_at(n3, "other.txt") == "changed\n"
    assert vcs.read_file_at(n1, "other.txt") == "keep me\n"
    # The old commits are gone from reachable history; the working copy is
    # back on the (rewritten) tip and clean apart from the reldir itself.
    revs = set(vcs.history_revs())
    assert {n1, n2, n3} <= revs and not ({c1, c2, c3} & revs)
    after = vcs.position()
    if vcs.kind == "git":
        assert after["branch"] == before["branch"] and after["commit"] == n3
    else:
        assert (after["parent"] == n3 and after["id"] != before["id"]) or (
            after["parent"] == n3
        )
    # Idempotent: nothing left to rewrite.
    assert vcs.rewrite_history("ds/.tether/objects", transform) == {}


def test_abandon_keeps_descendant_manifests_as_snapshots(vcs_root: Path) -> None:
    vcs = detect_vcs(vcs_root)
    _write(vcs_root, "ds/.tether/objects/db.toml", "state = 1\n")
    _write(vcs_root, "notes.txt", "a\n")
    c1 = vcs.commit(["ds/.tether/objects", "notes.txt"], "one")
    _write(vcs_root, "ds/.tether/objects/db.toml", "state = 2\n")
    _write(vcs_root, "notes.txt", "a\nb\n")
    c2 = vcs.commit(["ds/.tether/objects", "notes.txt"], "two")
    _write(vcs_root, "ds/.tether/objects/db.toml", "state = 3\n")
    c3 = vcs.commit(["ds/.tether/objects"], "three")

    # Dropping the middle commit: a patch-rebase of `three` onto `one` would
    # conflict on db.toml (both rewrite the same line). The manifest must come
    # out exactly as `three` had it; notes.txt follows the VCS's own rebase.
    abandoned, rewritten = vcs.abandon([c2], "ds/.tether/objects")
    assert abandoned == [c2]
    revs = vcs.history_revs()
    assert c2 not in revs and c1 in revs and c3 not in revs  # c3 was rebased
    tip = vcs.resolve("@-" if vcs.kind == "jj" else "HEAD")
    assert rewritten == {c3: tip}  # the rebased descendant, old -> new
    assert vcs.commit_alive(c1) and vcs.commit_alive(tip)
    # jj follows the change: c3's change lives on as tip. git has no such
    # identity, so the old id is simply gone.
    assert vcs.commit_alive(c3) == (vcs.kind == "jj")
    assert not vcs.commit_alive(c2)
    assert vcs.read_file_at(tip, "ds/.tether/objects/db.toml") == "state = 3\n"
    assert vcs.read_file_at(tip, "notes.txt") == "a\n"  # two's edit is gone
    assert not vcs.dirty(["ds/.tether/objects"])
    assert (vcs_root / "ds/.tether/objects/db.toml").read_text() == "state = 3\n"

    # Dropping the tip: the working tree goes back to its parent's manifests.
    vcs.abandon([tip], "ds/.tether/objects")
    tip2 = vcs.resolve("@-" if vcs.kind == "jj" else "HEAD")
    assert tip2 == c1
    assert (vcs_root / "ds/.tether/objects/db.toml").read_text() == "state = 1\n"


def test_bookmarks(vcs_root: Path) -> None:
    vcs = detect_vcs(vcs_root)
    _write(vcs_root, "tether.toml", "v=1\n")
    c1 = vcs.commit(["tether.toml"], "first")
    # A fresh repo: git is on its default branch, jj has no bookmark yet.
    before = vcs.bookmarks()
    assert all(commit == c1 for commit in before.values())

    vcs.bookmark_set("main", c1)
    assert vcs.bookmarks()["main"] == c1
    if vcs.kind == "git":
        assert set(vcs.current_bookmarks()) <= {"main", *before}

    vcs.new_bookmark("feature", "main")
    assert vcs.bookmarks()["feature"] == c1
    # jj: both bookmarks sit on c1 until the first commit; git: HEAD is definite.
    assert "feature" in vcs.current_bookmarks()
    _write(vcs_root, "tether.toml", "v=2\n")
    c2 = vcs.commit(["tether.toml"], "second", advance="feature")
    assert vcs.bookmarks()["feature"] == c2 and vcs.bookmarks()["main"] == c1
    if vcs.kind == "jj":
        # One operation: the commit and the bookmark move undo together.
        import subprocess

        subprocess.run(["jj", "undo"], cwd=vcs_root, check=True, capture_output=True)
        assert vcs.bookmarks()["feature"] == c1 and c2 not in vcs.history_revs()
        c2 = vcs.commit(["tether.toml"], "second, again", advance="feature")
        assert vcs.bookmarks()["feature"] == c2
    assert vcs.current_bookmarks() == ["feature"]
    assert vcs.is_ancestor(c1, c2) and not vcs.is_ancestor(c2, c1)
    assert vcs.is_ancestor(c2, c2)

    # Moving backwards is allowed; deleting removes the name.
    vcs.bookmark_set("feature", c1)
    assert vcs.bookmarks()["feature"] == c1
    vcs.new("main")
    vcs.bookmark_delete("feature")
    assert "feature" not in vcs.bookmarks()
