"""Version-control adapters.

tether stores its history *inside* an existing git or jj repository: the
committed manifests are ordinary tracked files. This module wraps the ``jj`` and
``git`` CLIs with the small surface tether needs -- resolve a revision, read a
file's content at a revision, list files at a revision, list history, commit the
manifest files, and start a new working-copy commit.

We deliberately shell out rather than link a library: it matches how the user's
environment is set up (``jj`` lives on ``PATH`` / at a configured path) and keeps
the dependency footprint at zero for the core package.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from tether.errors import VcsError


@dataclass
class _Run:
    returncode: int
    stdout: str
    stderr: str


def _run(
    argv: list[str],
    *,
    cwd: Path,
    check: bool = True,
    input_text: str | None = None,
) -> _Run:
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            input=input_text,
        )
    except FileNotFoundError as exc:  # pragma: no cover - env dependent
        raise VcsError(f"executable not found: {argv[0]}") from exc
    result = _Run(proc.returncode, proc.stdout, proc.stderr)
    if check and proc.returncode != 0:
        raise VcsError(
            f"command failed ({' '.join(argv)}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return result


@runtime_checkable
class VcsAdapter(Protocol):
    """The VCS surface tether relies on. Paths are relative to :attr:`root`."""

    kind: str
    root: Path

    def resolve(self, rev: str) -> str:
        """Return a stable commit id for ``rev``."""

    def current_rev(self) -> str:
        """Return the commit id of the current working-copy / HEAD commit."""

    def read_file_at(self, rev: str, relpath: str) -> str | None:
        """Return file content at ``rev`` or ``None`` if the path is absent."""

    def list_files_at(self, rev: str, reldir: str) -> list[str]:
        """List tracked file paths under ``reldir`` at ``rev`` (root-relative)."""

    def history_revs(self) -> list[str]:
        """Return commit ids reachable in the repository."""

    def commit(self, relpaths: list[str], message: str) -> str:
        """Commit the given paths with ``message``; return the new commit id."""

    def new(self, rev: str | None) -> None:
        """Start a fresh working-copy commit on ``rev`` (or the current tip)."""


# --------------------------------------------------------------------------- #
# jj
# --------------------------------------------------------------------------- #
class JjAdapter:
    kind = "jj"

    def __init__(self, root: Path, executable: str = "jj") -> None:
        self.root = root
        self._exe = executable

    def _jj(self, *args: str, check: bool = True) -> _Run:
        return _run([self._exe, *args], cwd=self.root, check=check)

    def resolve(self, rev: str) -> str:
        out = self._jj(
            "log",
            "--no-graph",
            "--ignore-working-copy",
            "-r",
            rev,
            "-T",
            "commit_id",
        )
        commit = out.stdout.strip()
        if not commit:
            raise VcsError(f"could not resolve revision: {rev}")
        return commit

    def current_rev(self) -> str:
        return self.resolve("@")

    def read_file_at(self, rev: str, relpath: str) -> str | None:
        out = self._jj(
            "file",
            "show",
            "--ignore-working-copy",
            "-r",
            rev,
            relpath,
            check=False,
        )
        if out.returncode != 0:
            return None
        return out.stdout

    def list_files_at(self, rev: str, reldir: str) -> list[str]:
        out = self._jj(
            "file",
            "list",
            "--ignore-working-copy",
            "-r",
            rev,
            reldir,
            check=False,
        )
        if out.returncode != 0:
            return []
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]

    def history_revs(self) -> list[str]:
        out = self._jj(
            "log",
            "--no-graph",
            "--ignore-working-copy",
            "-r",
            "all()",
            "-T",
            'commit_id ++ "\\n"',
        )
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]

    def commit(self, relpaths: list[str], message: str) -> str:
        # jj auto-snapshots the working copy; scope the commit to our paths so
        # unrelated working-copy edits stay put. `jj commit` finalizes the
        # current @ (which becomes @-) and opens a fresh empty @ on top.
        self._jj("commit", "-m", message, *relpaths)
        return self.resolve("@-")

    def new(self, rev: str | None) -> None:
        self._jj("new", rev if rev is not None else "@")


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #
class GitAdapter:
    kind = "git"

    def __init__(self, root: Path, executable: str = "git") -> None:
        self.root = root
        self._exe = executable

    def _git(self, *args: str, check: bool = True) -> _Run:
        return _run([self._exe, *args], cwd=self.root, check=check)

    def resolve(self, rev: str) -> str:
        out = self._git(
            "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", check=False
        )
        commit = out.stdout.strip()
        if out.returncode != 0 or not commit:
            raise VcsError(f"could not resolve revision: {rev}")
        return commit

    def current_rev(self) -> str:
        return self.resolve("HEAD")

    def read_file_at(self, rev: str, relpath: str) -> str | None:
        out = self._git("show", f"{rev}:{relpath}", check=False)
        if out.returncode != 0:
            return None
        return out.stdout

    def list_files_at(self, rev: str, reldir: str) -> list[str]:
        out = self._git("ls-tree", "-r", "--name-only", rev, "--", reldir, check=False)
        if out.returncode != 0:
            return []
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]

    def history_revs(self) -> list[str]:
        out = self._git("rev-list", "--all", check=False)
        if out.returncode != 0:
            return []
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]

    def commit(self, relpaths: list[str], message: str) -> str:
        self._git("add", "--", *relpaths)
        self._git("commit", "-m", message, "--", *relpaths)
        return self.current_rev()

    def new(self, rev: str | None) -> None:
        if rev is not None:
            self._git("checkout", rev)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def _find_up(start: Path, marker: str) -> Path | None:
    start = start.resolve()
    for candidate in (start, *start.parents):
        if (candidate / marker).exists():
            return candidate
    return None


def detect_vcs(
    start: Path,
    *,
    jj_path: str | None = None,
    git_path: str | None = None,
    prefer: str = "jj",
) -> VcsAdapter:
    """Discover the enclosing VCS, preferring jj when repos are colocated."""
    jj_exe = jj_path or shutil.which("jj") or "jj"
    git_exe = git_path or shutil.which("git") or "git"
    jj_root = _find_up(start, ".jj")
    git_root = _find_up(start, ".git")

    if prefer == "jj" and jj_root is not None:
        return JjAdapter(jj_root, jj_exe)
    if git_root is not None:
        return GitAdapter(git_root, git_exe)
    if jj_root is not None:
        return JjAdapter(jj_root, jj_exe)
    raise VcsError(f"no git or jj repository found at or above {start}")
