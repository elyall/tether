from __future__ import annotations

import subprocess
from pathlib import Path

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

    repo.new(eager=True)
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
