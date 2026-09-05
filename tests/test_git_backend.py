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

    repo.new()
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
