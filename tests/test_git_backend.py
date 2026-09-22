from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tether.backends.base import ObjectBackend
from tether.backends.git import GitBackend
from tether.errors import BackendError, TetherError
from tether.handles import GitHandle
from tether.repo import Repo


def _git(path: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _init_code_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "c@o.de")
    _git(path, "config", "user.name", "coder")
    (path / "main.py").write_text("print(0)\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "v0")
    return _git(path, "rev-parse", "HEAD")


def _commit_on(path: Path, branch: str, text: str) -> str:
    _git(path, "checkout", "-q", branch)
    (path / "main.py").write_text(text, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "change")
    return _git(path, "rev-parse", "HEAD")


class GitHarness:
    """Conformance harness: one code repository per object, commits as writes."""

    def __init__(self, tmp: Path) -> None:
        self.backend: ObjectBackend = GitBackend()
        self.tmp = tmp
        self._n = 0

    def new_object(self) -> dict:
        self._n += 1
        path = self.tmp / f"code{self._n}"
        _init_code_repo(path)
        return {
            "path": str(path),
            "ref": "main" if _default_is_main(path) else "master",
        }

    def mutate(self, locator: dict, working_ref: str | None) -> None:
        self._n += 1
        path = Path(locator["path"])
        branch = working_ref or locator["ref"]
        _commit_on(path, branch, f"print({self._n})\n")
        # Leave HEAD on the base branch: git refuses to move a checked-out
        # branch (`branch -f`), which is what a fork reset does.
        _git(path, "checkout", "-q", locator["ref"])

    def fresh_locator(self) -> dict:
        self._n += 1
        return {"path": str(self.tmp / f"fresh{self._n}"), "ref": "main"}


def _default_is_main(path: Path) -> bool:
    return _git(path, "symbolic-ref", "--short", "HEAD") == "main"


def test_git_created_repo_with_files_in_the_working_tree_is_not_empty(
    tmp_path: Path,
) -> None:
    """Refs are not the whole repository: a file someone dropped into a
    created checkout -- untracked, staged, or ignored -- is data that
    `delete_store` would take with the directory. Any of them means keep."""
    backend = GitBackend()
    path = tmp_path / "made"
    loc = {"path": str(path), "ref": "main"}
    backend.create(loc, owner="0a1b2c3d")
    assert backend.is_ref_empty(loc) is True

    (path / "notes.txt").write_text("keep me\n", encoding="utf-8")  # untracked
    assert backend.is_ref_empty(loc) is False
    _git(path, "add", "notes.txt")  # staged, uncommitted
    assert backend.is_ref_empty(loc) is False
    _git(path, "rm", "-q", "--cached", "notes.txt")
    (path / "notes.txt").unlink()
    assert backend.is_ref_empty(loc) is True

    (path / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
    (path / "scratch.tmp").write_text("x", encoding="utf-8")  # ignored
    assert backend.is_ref_empty(loc) is False
    (path / "scratch.tmp").unlink()
    (path / ".gitignore").unlink()
    assert backend.is_ref_empty(loc) is True
    backend.delete_store(loc)
    assert not path.exists()


def test_git_created_repos_share_one_root_and_an_amended_root_is_not_empty(
    tmp_path: Path,
) -> None:
    """`create` makes the same root commit every time (fixed tree, author,
    dates), so two creates fingerprint identically and `is_ref_empty` can
    compare the base to that sha -- which also catches an amended root that
    smuggled files in while staying one commit deep."""
    backend = GitBackend()
    a = {"path": str(tmp_path / "a"), "ref": "main"}
    b = {"path": str(tmp_path / "b"), "ref": "main"}
    sa = backend.create(a, owner="0a1b2c3d")
    sb = backend.create(b, owner="0a1b2c3d")
    assert sa["sha"] == sb["sha"] == GitBackend.EMPTY_ROOT_SHA
    assert backend.is_ref_empty(a) is True

    path = Path(a["path"])
    (path / "data.csv").write_text("1,2,3\n", encoding="utf-8")
    _git(path, "add", "data.csv")
    _git(path, "commit", "-q", "--amend", "--no-edit")
    assert _git(path, "rev-list", "--count", "main") == "1"  # still one commit...
    assert backend.is_ref_empty(a) is False  # ...but not the one we made
    with pytest.raises(BackendError, match="not empty"):
        backend.delete_store(a)
    assert path.exists()


def test_git_dirty_checkout_is_the_same_state_as_its_sha(tmp_path: Path) -> None:
    """`dirty` describes the checkout, not the commit: a fork at the same sha
    as a base that has stray files is *equal* to it (so gc does not call the
    fork "unpinned writes"), while `pin` still refuses the dirty tree."""
    from tether.backends.base import content_state

    backend = GitBackend()
    loc = {"path": str(tmp_path / "made"), "ref": "main"}
    state = backend.create(loc, owner="0a1b2c3d")
    wref = backend.fork(loc, state, "tether.ws.0a1b2c3d.probe")
    (Path(loc["path"]) / "stray.txt").write_text("x", encoding="utf-8")
    base = backend.fingerprint(loc, None)
    fork = backend.fingerprint(loc, wref)
    assert base["dirty"] is True and fork.get("dirty") is not True
    assert content_state(backend, base) == content_state(backend, fork)
    with pytest.raises(BackendError, match="dirty"):
        backend.pin(loc, base, "0a1b2c3d.0123456789abcdef")


def test_git_fork_onto_a_branch_at_the_source_leaves_it_alone(tmp_path: Path) -> None:
    """The reset contract's other half: no `branch -f` (and no reflog entry)
    when the working branch already sits at the source."""
    import subprocess

    code = tmp_path / "code"
    sha0 = _init_code_repo(code)
    b = GitBackend()
    loc = {"path": str(code)}
    name = "tether.ws.0a1b2c3d.work"
    assert b.fork(loc, {"sha": sha0}, name) == name
    reflog = subprocess.run(
        ["git", "-C", str(code), "reflog", "show", name],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert b.fork(loc, {"sha": sha0}, name) == name
    again = subprocess.run(
        ["git", "-C", str(code), "reflog", "show", name],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert again == reflog


def test_git_refuses_option_shaped_refs_from_manifests(
    tmp_path_factory: pytest.TempPathFactory, vcs_root: Path
) -> None:
    """A manifest field that starts with `-` must never reach git as an option.
    `add` refuses it, and a manifest that arrives with a clone is refused at
    use (every call site passes --end-of-options and guards its positionals):
    `at = "--output=FILE"` writes no FILE."""
    from tether.manifest import ObjectManifest, Policy, write_object

    tmp_path = tmp_path_factory.mktemp("outside")
    code = tmp_path / "code"
    _init_code_repo(code)
    b = GitBackend()
    evil = tmp_path / "pwned"
    for field in ("ref", "at", "remote"):
        with pytest.raises(BackendError, match="looks like an option"):
            b.validate_locator({"path": str(code), field: f"--output={evil}"})
    repo = Repo.init(vcs_root)
    with pytest.raises(BackendError, match="looks like an option"):
        repo.add("code", "git", {"path": str(code), "at": f"--output={evil}"})
    # A hostile clone bypasses `add`: write the manifest directly.
    write_object(
        vcs_root,
        ObjectManifest(
            key="code",
            kind="git",
            locator={"path": str(code), "at": f"--output={evil}"},
            policy=Policy(),
        ),
    )
    fresh = Repo.find(vcs_root)
    with pytest.raises(BackendError, match="looks like an option"):
        fresh.history_for("git", fresh.objects["code"].locator, limit=5)
    with pytest.raises((BackendError, Exception)):
        fresh.snapshot()
    assert not evil.exists()
    # States are hex or nothing.
    with pytest.raises(BackendError, match="not a git commit id"):
        b.open({"path": str(code)}, {"sha": "--output=x"}, read_only=True)


def test_git_backend_conformance(tmp_path: Path) -> None:
    from tether.testing import run_conformance

    run_conformance(GitHarness(tmp_path))


def test_git_backend_lifecycle(
    vcs_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    repo = Repo.init(vcs_root)
    code = tmp_path_factory.mktemp("outside") / "code"
    sha0 = _init_code_repo(code)
    repo.add("code", "git", {"path": str(code)})

    res = repo.commit("pin code")
    pin = res.pinned["code"]
    assert pin is not None
    backend = repo.backend_for("git")
    assert pin.id in backend.list_pins({"path": str(code)})
    # The pin is a real git tag at sha0.
    assert _git(code, "rev-parse", f"{pin.ref}^{{commit}}") == sha0

    repo.new(bookmark="work", eager=True)
    wref = repo.workspace.working_refs["code"]
    assert wref and wref.startswith("tether.ws.")
    handle = repo.open("code")
    assert isinstance(handle, GitHandle) and not handle.read_only and handle.sha == sha0

    # Advance the fork branch and confirm drift is detected.
    sha1 = _commit_on(code, wref, "print(1)\n")
    assert sha1 != sha0
    status = repo.status()
    obj = next(o for o in status.objects if o.key == "code")
    assert obj.changed

    res2 = repo.commit("update code")
    pin2 = res2.pinned["code"]
    assert pin2 is not None and pin2.id != pin.id

    old = repo.open("code", rev=res.vcs_commit)
    assert isinstance(old, GitHandle) and old.read_only and old.sha == sha0

    assert all(r.ok for r in repo.verify().values())

    # Content diff: per-file status with line counts.
    (code / "util.py").write_text("x = 1\n", encoding="utf-8")
    _git(code, "add", "-A")
    _git(code, "commit", "-qm", "add util")
    sha2 = _git(code, "rev-parse", "HEAD")
    d = backend.diff({"path": str(code)}, {"sha": sha0}, {"sha": sha2})
    assert d.unit == "files" and (d.added, d.removed, d.modified) == (1, 0, 1)
    assert {(e.path, e.change, e.detail) for e in d.entries} == {
        ("main.py", "modified", "+1 -1"),
        ("util.py", "added", "+1 -0"),
    }
    assert backend.diff({"path": str(code)}, {"sha": sha2}, {"sha": sha2}).is_empty
    entries = {
        e.key: e for e in repo.diff(res.vcs_commit, res2.vcs_commit, content=True)
    }
    assert entries["code"].detail is not None and entries["code"].detail.modified == 1

    # History from a ref, with decorations; `at` is any commit-ish.
    log = backend.history({"path": str(code)}, "HEAD", 10)
    assert [e.id for e in log][:2] == [sha2, sha1]
    assert log[0].message == "add util" and "HEAD" not in log[0].refs
    assert pin.ref in backend.history({"path": str(code)}, sha0, 10)[0].refs
    assert backend.fingerprint({"path": str(code), "at": sha0}, None)["sha"] == sha0


def test_positional_locator_is_the_path(
    vcs_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    code = tmp_path_factory.mktemp("outside") / "code"
    sha0 = _init_code_repo(code)
    repo = Repo.init(vcs_root)
    backend = repo.backend_for("git")
    # `tether add code --kind git ../code` puts the path in `uri`.
    assert backend.fingerprint({"uri": str(code)}, None)["sha"] == sha0
    assert backend.identity({"uri": str(code)}) == backend.identity({"path": str(code)})
    with pytest.raises(BackendError):
        backend.fingerprint({"uri": "https://github.com/o/r.git"}, None)


def test_dirty_belongs_to_the_checked_out_ref_only(vcs_root: Path) -> None:
    from tether.backends.base import content_state
    from tether.backends.git import GitBackend

    code = vcs_root / "code"
    sha0 = _init_code_repo(code)
    b = GitBackend()
    loc = {"path": str(code)}
    _git(code, "branch", "tether.ws.x.code", sha0)
    (code / "main.py").write_text("print('uncommitted')\n", encoding="utf-8")
    dirty_head = b.fingerprint(loc, None)  # HEAD is checked out and dirty
    other = b.fingerprint(loc, "tether.ws.x.code")  # not checked out
    assert dirty_head["dirty"] is True and dirty_head["sha"] == sha0
    assert other["dirty"] is False and other["sha"] == sha0
    # change_id is an address, not content: the same sha pins identically.
    assert "change_id" not in (content_state(b, other) or {})


def test_pin_fails_when_the_remote_push_fails(tmp_path: Path) -> None:
    """A pin with a remote is only a pin once it is on the remote."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@e.com"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "a.txt").write_text("a\n")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "one"], check=True)
    b = GitBackend()

    # A remote that does not exist: the push fails, so the pin fails and no
    # local tag is left behind to diverge from it.
    bad = {"path": str(repo), "remote": str(tmp_path / "nowhere.git")}
    state = b.fingerprint(bad, None)
    with pytest.raises(BackendError, match="was not pushed"):
        b.pin(bad, state, "d5d5d5d5.0000000000000001")
    assert b.list_pins(bad) == set()

    # A real (bare) remote: pin pushes, unpin deletes there first.
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    good = {"path": str(repo), "remote": str(bare)}
    pin = b.pin(good, state, "d5d5d5d5.0000000000000002")
    remote_tags = subprocess.run(
        ["git", "-C", str(bare), "tag", "--list"], capture_output=True, text=True
    ).stdout.split()
    assert pin.ref in remote_tags
    b.unpin(good, pin)
    remote_tags = subprocess.run(
        ["git", "-C", str(bare), "tag", "--list"], capture_output=True, text=True
    ).stdout.split()
    assert pin.ref not in remote_tags and pin.id not in b.list_pins(good)
    b.unpin(good, pin)  # already gone everywhere: still fine


def _bare_shaped(where: Path, marker: Path) -> Path:
    """What a clone can ship: git refuses to track a `.git`, but a directory
    holding `HEAD`, `objects/`, `refs/` and a `config` is only files, and
    `git -C <it>` takes it for a repository and runs its `core.fsmonitor`."""
    src = where.parent / f"{where.name}-src"
    src.mkdir(parents=True)
    (src / "x").write_text("x\n")
    env = {**os.environ, "GIT_DIR": str(where), "GIT_WORK_TREE": str(src)}
    for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-qm", "c"]):
        subprocess.run(["git", *args], env=env, check=True, capture_output=True)
    (where / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
        f"\tworktree = {src}\n\tfsmonitor = \"touch '{marker}'; false\"\n"
    )
    return where


def test_a_hostile_clone_cannot_make_git_run_its_config(
    vcs_root: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed `path` must be absolute and outside the checkout: anything
    inside it arrived with the clone. And git never discovers a bare
    repository by itself, wherever one is."""
    from tether.manifest import ObjectManifest, Policy, write_object

    marker = tmp_path_factory.mktemp("marker") / "PWNED"
    repo = Repo.init(vcs_root)
    evil = _bare_shaped(vcs_root / "evil", marker)
    b = repo.backend_for("git")

    # Relative, as a hand-written manifest says it, run from the dataset root.
    monkeypatch.chdir(vcs_root)
    with pytest.raises(BackendError, match="absolute"):
        b.fingerprint({"path": "evil"}, None)
    # Absolute but inside the checkout: refused at `add`...
    with pytest.raises(BackendError, match="inside the dataset's checkout"):
        repo.add("code", "git", {"path": str(evil)})
    # ...and at use, when a clone brings the manifest.
    for path in (str(evil), "evil"):
        write_object(
            vcs_root,
            ObjectManifest(
                key="code", kind="git", locator={"path": path}, policy=Policy()
            ),
        )
        with pytest.raises(TetherError):
            Repo.find(vcs_root).snapshot()
    # Outside the checkout, git itself refuses the bare-shaped directory.
    outside = _bare_shaped(tmp_path_factory.mktemp("outside") / "evil", marker)
    with pytest.raises(BackendError, match="bare repository"):
        GitBackend().fingerprint({"path": str(outside)}, None)
    assert not marker.exists()


def test_git_runs_no_command_a_repository_config_names(tmp_path: Path) -> None:
    """tether reads and tags the repository a locator names; it has no use for
    its fsmonitor or hooks, and runs neither."""
    code = tmp_path / "code"
    _init_code_repo(code)
    marker = tmp_path / "ran"
    _git(code, "config", "core.fsmonitor", f"touch '{marker}'; false")
    hook = code / ".git" / "hooks" / "reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    b = GitBackend()
    loc = {"path": str(code)}
    state = b.fingerprint(loc, None)  # `git status`: fsmonitor
    b.pin(loc, state, "0a1b2c3d.0000000000000001")  # a ref update: the hook
    assert not marker.exists()


def test_an_inherited_git_environment_does_not_retarget_the_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run from a git hook, or anything else that exports `GIT_DIR`, every git
    call would read and write that repository instead of the one the locator
    names; `GIT_CONFIG_*` would add config of its own."""
    code = tmp_path / "code"
    sha = _init_code_repo(code)
    other = tmp_path / "other"
    _init_code_repo(other)
    other_sha = _commit_on(other, _git(other, "branch", "--show-current"), "x\n")
    marker = tmp_path / "ran"
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"touch '{marker}'; false")
    b = GitBackend()
    loc = {"path": str(code)}
    state = b.fingerprint(loc, None)
    assert state["sha"] == sha != other_sha and state["dirty"] is False
    pin = b.pin(loc, state, "0a1b2c3d.0000000000000001")
    assert b.list_pins(loc) == {pin.id}
    assert b.list_pins({"path": str(other)}) == set()
    assert not marker.exists()
