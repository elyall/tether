from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tether.backends.base import ObjectBackend
from tether.backends.git import GitBackend
from tether.errors import BackendError
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


def _default_is_main(path: Path) -> bool:
    return _git(path, "symbolic-ref", "--short", "HEAD") == "main"


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
    tmp_path: Path, vcs_root: Path
) -> None:
    """A manifest field that starts with `-` must never reach git as an option.
    `add` refuses it, and a manifest that arrives with a clone is refused at
    use (every call site passes --end-of-options and guards its positionals):
    `at = "--output=FILE"` writes no FILE."""
    from tether.manifest import ObjectManifest, Policy, write_object

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


def test_git_backend_lifecycle(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    code = vcs_root / "code"
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


def test_positional_locator_is_the_path(vcs_root: Path) -> None:
    import pytest

    from tether.errors import BackendError

    code = vcs_root / "code"
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
