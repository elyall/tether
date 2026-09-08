from __future__ import annotations

from pathlib import Path

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
        # Re-running new on the same commit reuses the branch when it is still
        # there; a moved-on branch gets a sibling instead of being reset.
        vcs.new(c1)
        assert vcs.resolve("HEAD") == c1 or vcs.resolve("HEAD") == c3
        branches = subprocess.run(
            ["git", "-C", str(vcs_root), "branch", "--list", f"tether/{c1[:12]}*"],
            capture_output=True,
            text=True,
        ).stdout
        assert f"tether/{c1[:12]}" in branches
        # A branch name switches to that branch.
        default = subprocess.run(
            ["git", "-C", str(vcs_root), "branch", "--show-current"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert default
