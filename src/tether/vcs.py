"""Version-control adapters.

tether stores its history *inside* an existing git or jj repository: the
committed manifests are ordinary tracked files. This module wraps the ``jj`` and
``git`` CLIs with the small surface tether needs -- resolve a revision, read a
file's content at a revision, list files at a revision, list history, commit the
manifest files, and start a new working-copy commit.

We deliberately shell out rather than link a library: it matches how the user's
environment is set up (``jj`` lives on ``PATH`` / at a configured path) and keeps
the dependency footprint at zero for the core package.

History walks (``gc``, ``verify --all-history``) would otherwise cost one process
per commit per manifest. Both adapters instead stream every object out of a
single ``git cat-file --batch`` process (jj repos are git-backed, so the same
plumbing works there), which is ~50x cheaper per read.
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from tether.errors import VcsError

__all__ = [
    "CommitInfo",
    "GitAdapter",
    "GitObjectReader",
    "JjAdapter",
    "RefInfo",
    "VcsAdapter",
    "detect_vcs",
]

_TREE_MODE = "40000"
_BLOB_MODES = frozenset({"100644", "100755"})
_US = "\x1f"  # field separator for batched `git log` output
_RS = "\x1e"  # record separator


@dataclass(frozen=True)
class CommitInfo:
    """Metadata of one VCS commit, as exported to registries."""

    commit_id: str
    """Full commit hash."""
    parents: tuple[str, ...]
    """Parent commit ids, first parent first."""
    author_name: str
    author_email: str
    authored_at: str
    """ISO-8601 author timestamp."""
    committed_at: str
    """ISO-8601 committer timestamp."""
    message: str
    """Full commit message."""
    change_id: str | None = None
    """jj change id (``None`` in plain git repositories)."""


@dataclass(frozen=True)
class RefInfo:
    """A named pointer into history: a jj bookmark, git branch, tag, or the head."""

    name: str
    kind: str
    """``bookmark`` (jj), ``branch`` (git), ``tag``, or ``head`` (the working copy)."""
    commit_id: str


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


# --------------------------------------------------------------------------- #
# Batched object reads (git plumbing; shared by both adapters)
# --------------------------------------------------------------------------- #
def _parse_tree(data: bytes) -> Iterator[tuple[str, str, str]]:
    """Yield ``(mode, name, hex_sha)`` from a raw git tree object."""
    i = 0
    n = len(data)
    while i < n:
        sp = data.index(b" ", i)
        nul = data.index(b"\0", sp)
        mode = data[i:sp].decode("ascii")
        name = data[sp + 1 : nul].decode("utf-8", "surrogateescape")
        sha = data[nul + 1 : nul + 21].hex()
        i = nul + 21
        yield mode, name, sha


class GitObjectReader:
    """Read many objects through one ``git cat-file --batch`` process.

    Trees and blobs are cached by object id, so walking a long history whose
    manifests rarely change costs one round-trip per commit plus one per
    *distinct* manifest -- not one process per commit per file.
    """

    def __init__(self, git_exe: str, cwd: Path, git_dir: Path | None = None) -> None:
        argv = [git_exe]
        if git_dir is not None:
            argv += ["--git-dir", str(git_dir)]
        argv += ["cat-file", "--batch"]
        try:
            self._proc = subprocess.Popen(
                argv,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:  # pragma: no cover - env dependent
            raise VcsError(f"executable not found: {git_exe}") from exc
        self._trees: dict[str, dict[str, str]] = {}
        self._blobs: dict[str, str] = {}

    def __enter__(self) -> GitObjectReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        proc = self._proc
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
            proc.wait()

    def fetch(self, spec: str) -> tuple[str, str, bytes] | None:
        """Return ``(oid, type, data)`` for ``spec`` or ``None`` if missing."""
        proc = self._proc
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(spec.encode("utf-8") + b"\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise VcsError("git cat-file --batch exited unexpectedly") from exc
        header = proc.stdout.readline()
        if not header:
            raise VcsError("git cat-file --batch produced no output")
        parts = header.split()
        if len(parts) < 3:  # "<spec> missing" / "<spec> ambiguous"
            return None
        oid = parts[0].decode("ascii")
        typ = parts[1].decode("ascii")
        size = int(parts[2])
        data = proc.stdout.read(size)
        proc.stdout.read(1)  # trailing newline
        return oid, typ, data

    def files(self, spec: str) -> dict[str, str]:
        """Return ``{path-under-spec: text}`` for every blob under tree-ish ``spec``."""
        obj = self.fetch(spec)
        if obj is None:
            return {}
        oid, typ, data = obj
        if typ != "tree":
            return {}
        return self._tree(oid, data)

    def _tree(self, oid: str, data: bytes) -> dict[str, str]:
        cached = self._trees.get(oid)
        if cached is not None:
            return cached
        result: dict[str, str] = {}
        for mode, name, sha in _parse_tree(data):
            if mode == _TREE_MODE:
                sub = self._trees.get(sha)
                if sub is None:
                    obj = self.fetch(sha)
                    if obj is None:  # pragma: no cover - corrupt repo
                        continue
                    sub = self._tree(sha, obj[2])
                for path, text in sub.items():
                    result[f"{name}/{path}"] = text
            elif mode in _BLOB_MODES:
                result[name] = self._blob(sha)
        self._trees[oid] = result
        return result

    def _blob(self, sha: str) -> str:
        text = self._blobs.get(sha)
        if text is None:
            obj = self.fetch(sha)
            text = obj[2].decode("utf-8", "replace") if obj is not None else ""
            self._blobs[sha] = text
        return text


def _is_commit_id(rev: str) -> bool:
    return len(rev) == 40 and all(c in "0123456789abcdef" for c in rev)


def _tree_spec(rev: str, reldir: str) -> str:
    reldir = reldir.strip("/")
    if reldir in ("", "."):
        return f"{rev}^{{tree}}"
    return f"{rev}:{reldir}"


def _prefixed(reldir: str, files: dict[str, str]) -> dict[str, str]:
    reldir = reldir.strip("/")
    if reldir in ("", "."):
        return dict(files)
    return {f"{reldir}/{path}": text for path, text in files.items()}


def _git_commit_info(
    git_exe: str, cwd: Path, git_dir: Path | None, revs: list[str]
) -> list[CommitInfo]:
    """Metadata for many commits through one ``git log --stdin`` call.

    Commit ids are fed on stdin so the batch is not bounded by the argument
    list; ``--no-walk`` keeps the output to exactly the requested commits.
    """
    # jj's virtual root commit (all zeros) has no git object and would abort the batch.
    revs = [r for r in revs if r.strip("0")]
    if not revs:
        return []
    argv = [git_exe]
    if git_dir is not None:
        argv += ["--git-dir", str(git_dir)]
    argv += [
        "log",
        "--no-walk=unsorted",
        "--stdin",
        f"--format=%H{_US}%P{_US}%an{_US}%ae{_US}%aI{_US}%cI{_US}%B{_RS}",
    ]
    out = _run(argv, cwd=cwd, input_text="\n".join(revs) + "\n")
    infos: list[CommitInfo] = []
    for record in out.stdout.split(_RS):
        if not record.strip():
            continue
        fields = record.lstrip("\n").split(_US, 6)
        if len(fields) != 7:
            continue
        sha, parents, an, ae, ai, ci, body = fields
        infos.append(
            CommitInfo(
                commit_id=sha,
                parents=tuple(p for p in parents.split() if p),
                author_name=an,
                author_email=ae,
                authored_at=ai,
                committed_at=ci,
                message=body.rstrip("\n"),
            )
        )
    return infos


def _git_refs(git_exe: str, cwd: Path, git_dir: Path | None) -> list[RefInfo]:
    """Local branches and tags via one ``git for-each-ref`` (tags peeled)."""
    argv = [git_exe]
    if git_dir is not None:
        argv += ["--git-dir", str(git_dir)]
    argv += [
        "for-each-ref",
        # for-each-ref spells control characters as %xx (two hex digits).
        "--format=%(refname)%1f%(objectname)%1f%(*objectname)",
        "refs/heads",
        "refs/tags",
    ]
    out = _run(argv, cwd=cwd, check=False)
    refs: list[RefInfo] = []
    for line in out.stdout.splitlines():
        parts = line.split(_US)
        if len(parts) != 3:
            continue
        refname, sha, peeled = parts
        if refname.startswith("refs/heads/"):
            refs.append(RefInfo(refname[len("refs/heads/") :], "branch", sha))
        elif refname.startswith("refs/tags/"):
            refs.append(RefInfo(refname[len("refs/tags/") :], "tag", peeled or sha))
    return refs


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

    def files_at(self, rev: str, reldir: str) -> dict[str, str]:
        """Return ``{root-relative path: text}`` for files under ``reldir``."""

    def history_revs(self) -> list[str]:
        """Return commit ids reachable in the repository."""

    def iter_history_files(self, reldir: str) -> Iterator[tuple[str, dict[str, str]]]:
        """Yield ``(commit id, files_at(commit, reldir))`` across all history.

        Implementations stream through one object-reader process rather than
        spawning per commit; callers should consume lazily.
        """

    def commit_info(self, revs: list[str]) -> list[CommitInfo]:
        """Author, timestamps, message, and parents for many commits in one call."""

    def refs(self) -> list[RefInfo]:
        """Named pointers (bookmarks / branches, tags) plus the ``head`` entry."""

    def workspace_roots(self) -> list[Path]:
        """Root directories of every live checkout of this repository.

        jj workspaces (``jj workspace list`` + ``jj workspace root --name``) or
        git worktrees (``git worktree list``). Used by ``gc --prune-workspaces``
        to keep the working branches of workspaces that still exist.
        """

    def commit(self, relpaths: list[str], message: str) -> str:
        """Commit the given paths with ``message``; return the new commit id."""

    def new(self, rev: str | None) -> None:
        """Start a fresh working-copy commit on ``rev`` (or the current tip)."""


# --------------------------------------------------------------------------- #
# jj
# --------------------------------------------------------------------------- #
class JjAdapter:
    kind = "jj"

    def __init__(
        self,
        root: Path,
        executable: str = "jj",
        git_executable: str | None = None,
    ) -> None:
        self.root = root
        self._exe = executable
        self._git_exe = git_executable or shutil.which("git")

    def _jj(self, *args: str, check: bool = True) -> _Run:
        return _run([self._exe, *args], cwd=self.root, check=check)

    def _git_store(self) -> tuple[Path, Path | None] | None:
        """Locate the git object store backing this jj repo.

        Returns ``(cwd, git_dir)`` for :class:`GitObjectReader`, or ``None`` when
        git plumbing is unavailable (no ``git`` binary or a non-git jj backend).
        """
        if self._git_exe is None:
            return None
        if (self.root / ".git").exists():  # colocated
            return self.root, None
        store = self.root / ".jj" / "repo" / "store" / "git"
        if store.is_dir():
            return self.root, store
        return None

    def _reader(self) -> GitObjectReader | None:
        store = self._git_store()
        if store is None:
            return None
        cwd, git_dir = store
        assert self._git_exe is not None
        return GitObjectReader(self._git_exe, cwd, git_dir)

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

    def _files_at_slow(self, rev: str, reldir: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in self.list_files_at(rev, reldir):
            text = self.read_file_at(rev, path)
            if text is not None:
                result[path] = text
        return result

    def files_at(self, rev: str, reldir: str) -> dict[str, str]:
        reader = self._reader()
        if reader is None:
            return self._files_at_slow(rev, reldir)
        # git plumbing only understands commit ids, not jj revsets.
        commit = rev if _is_commit_id(rev) else self.resolve(rev)
        with reader:
            return _prefixed(reldir, reader.files(_tree_spec(commit, reldir)))

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

    def iter_history_files(self, reldir: str) -> Iterator[tuple[str, dict[str, str]]]:
        revs = self.history_revs()
        reader = self._reader()
        if reader is None:
            for rev in revs:
                yield rev, self._files_at_slow(rev, reldir)
            return
        with reader:
            for rev in revs:
                # jj's virtual root commit has no git object; it reads as missing.
                yield rev, _prefixed(reldir, reader.files(_tree_spec(rev, reldir)))

    def _change_ids(self) -> dict[str, str]:
        out = self._jj(
            "log",
            "--no-graph",
            "--ignore-working-copy",
            "-r",
            "all()",
            "-T",
            'commit_id ++ " " ++ change_id ++ "\\n"',
        )
        pairs = (line.split() for line in out.stdout.splitlines() if line.strip())
        return {commit: change for commit, change in pairs}

    def commit_info(self, revs: list[str]) -> list[CommitInfo]:
        store = self._git_store()
        change_ids = self._change_ids()
        if store is not None:
            assert self._git_exe is not None
            cwd, git_dir = store
            return [
                dataclasses.replace(info, change_id=change_ids.get(info.commit_id))
                for info in _git_commit_info(self._git_exe, cwd, git_dir, revs)
            ]
        # No git plumbing: one templated `jj log` for the requested revisions.
        template = (
            'commit_id ++ "\\x1f" ++ parents.map(|p| p.commit_id()).join(" ")'
            ' ++ "\\x1f" ++ author.name() ++ "\\x1f" ++ author.email() ++ "\\x1f"'
            ' ++ author.timestamp().format("%+") ++ "\\x1f"'
            ' ++ committer.timestamp().format("%+") ++ "\\x1f"'
            ' ++ description ++ "\\x1e"'
        )
        out = self._jj(
            "log",
            "--no-graph",
            "--ignore-working-copy",
            "-r",
            " | ".join(revs),
            "-T",
            template,
        )
        infos = []
        for record in out.stdout.split(_RS):
            fields = record.lstrip("\n").split(_US, 6)
            if len(fields) != 7:
                continue
            sha, parents, an, ae, ai, ci, body = fields
            infos.append(
                CommitInfo(
                    commit_id=sha,
                    parents=tuple(parents.split()),
                    author_name=an,
                    author_email=ae,
                    authored_at=ai,
                    committed_at=ci,
                    message=body.rstrip("\n"),
                    change_id=change_ids.get(sha),
                )
            )
        return infos

    def refs(self) -> list[RefInfo]:
        refs: list[RefInfo] = []
        out = self._jj(
            "bookmark",
            "list",
            "--ignore-working-copy",
            "-T",
            'if(normal_target, if(remote, "", '
            'name ++ " " ++ normal_target.commit_id() ++ "\\n"))',
            check=False,
        )
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2:
                refs.append(RefInfo(parts[0], "bookmark", parts[1]))
        store = self._git_store()
        if store is not None:
            assert self._git_exe is not None
            cwd, git_dir = store
            refs.extend(
                r for r in _git_refs(self._git_exe, cwd, git_dir) if r.kind == "tag"
            )
        refs.append(RefInfo("@", "head", self.current_rev()))
        return refs

    def workspace_roots(self) -> list[Path]:
        out = self._jj(
            "workspace",
            "list",
            "--ignore-working-copy",
            "-T",
            'name ++ "\n"',
            check=False,
        )
        roots: list[Path] = []
        for name in (n.strip() for n in out.stdout.splitlines()):
            if not name:
                continue
            root = self._jj(
                "workspace",
                "root",
                "--ignore-working-copy",
                "--name",
                name,
                check=False,
            )
            if root.returncode == 0 and root.stdout.strip():
                roots.append(Path(root.stdout.strip()))
        return roots or [self.root]

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

    def files_at(self, rev: str, reldir: str) -> dict[str, str]:
        with GitObjectReader(self._exe, self.root) as reader:
            return _prefixed(reldir, reader.files(_tree_spec(rev, reldir)))

    def history_revs(self) -> list[str]:
        out = self._git("rev-list", "--all", check=False)
        if out.returncode != 0:
            return []
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]

    def iter_history_files(self, reldir: str) -> Iterator[tuple[str, dict[str, str]]]:
        revs = self.history_revs()
        with GitObjectReader(self._exe, self.root) as reader:
            for rev in revs:
                yield rev, _prefixed(reldir, reader.files(_tree_spec(rev, reldir)))

    def commit_info(self, revs: list[str]) -> list[CommitInfo]:
        return _git_commit_info(self._exe, self.root, None, revs)

    def refs(self) -> list[RefInfo]:
        refs = _git_refs(self._exe, self.root, None)
        head = self._git("rev-parse", "--verify", "--quiet", "HEAD", check=False)
        if head.returncode == 0 and head.stdout.strip():
            refs.append(RefInfo("HEAD", "head", head.stdout.strip()))
        return refs

    def workspace_roots(self) -> list[Path]:
        out = self._git("worktree", "list", "--porcelain", check=False)
        roots = [
            Path(line[len("worktree ") :].strip())
            for line in out.stdout.splitlines()
            if line.startswith("worktree ")
        ]
        return roots or [self.root]

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
        return JjAdapter(
            jj_root, jj_exe, git_executable=git_path or shutil.which("git")
        )
    if git_root is not None:
        return GitAdapter(git_root, git_exe)
    if jj_root is not None:
        return JjAdapter(
            jj_root, jj_exe, git_executable=git_path or shutil.which("git")
        )
    raise VcsError(f"no git or jj repository found at or above {start}")
