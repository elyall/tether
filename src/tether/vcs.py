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
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from tether.errors import VcsError

__all__ = [
    "MIN_GIT_VERSION",
    "MIN_JJ_VERSION",
    "CommitInfo",
    "GitAdapter",
    "GitObjectReader",
    "JjAdapter",
    "RefInfo",
    "VcsAdapter",
    "detect_vcs",
]

MIN_JJ_VERSION = (0, 43, 0)
"""The oldest jj tether runs on; the adapter checks once and refuses an older
one. CI runs 0.45.1."""

MIN_GIT_VERSION = (2, 38, 0)
"""The oldest git tether runs on: `safe.bareRepository`, which keeps the git
backend from taking a directory a clone ships for a bare repository, arrived
in 2.38, and an older git ignores it without a word. The git adapter and the
git backend each check once and refuse an older one."""

_TREE_MODE = "40000"
_BLOB_MODES = frozenset({"100644", "100755"})
_US = "\x1f"  # field separator for batched `git log` output
_RS = "\x1e"  # record separator

_JJ_ISOLATION = (
    "--color=never",
    "--no-pager",
    "--config",
    'ui.color="never"',
    "--config",
    'ui.paginate="never"',
)
"""What every jj call gets, ahead of the user's config: plain output (a
`ui.color = "always"` put escape codes into every commit id tether parsed).
Snapshots follow the user's `snapshot` settings: a new file of theirs lands
in the change it was made in before tether moves the working copy, as under
their own jj commands. tether's own paths are left out of that
(`_JJ_TETHER_PATHS`) and tracked by name (`JjAdapter._track`). Revsets use
operator forms (`::`, `x::`) where one exists, so a
`revset-aliases."all()"` cannot redefine what gc walks."""

_JJ_TETHER_PATHS = 'root-glob:"**/.tether/**"'
"""Taken out of the user's `snapshot.auto-track` for tether's calls. A
manifest `add` wrote goes wherever the checkout goes (an undo's return
included) until a commit tracks it by name, and a per-checkout file
(`secrets.toml`, the op log) is never tracked by tether, even where the
committed `.gitignore` misses it."""

_JJ_TRACK_ONLY = (
    "--config",
    'snapshot.auto-track="none()"',
    "--config",
    'snapshot.max-new-file-size="1GiB"',
)
"""For `jj file track` of tether's paths. That command's own snapshot tracks
nothing new, so lifting the new-file size limit (a listing may exceed the
user's) reaches the named paths only, never the user's files."""

_JJ_PARKED = ("--config", 'snapshot.auto-track="none()"')
"""For every call after tether has moved the working copy itself, until it
is back (`JjAdapter._parked`). An older commit's `.gitignore` may not ignore
what is on disk -- `secrets.toml`, the op log, a later-ignored `.venv` -- and
a snapshot there under the user's auto-track would commit it into history."""

_GIT_ISOLATION = (
    "-c",
    "color.ui=never",
    "-c",
    "log.showSignature=false",
    "-c",
    "core.quotePath=false",
    "-c",
    "status.showUntrackedFiles=all",
)
"""What every git call gets: `log.showSignature = true` put the verification
banner in front of the commit id `git log` printed; `core.quotePath` spelled
a non-ASCII manifest path in octal; `status.showUntrackedFiles = no` hid a
manifest no commit had tracked yet from `dirty`, so `commit` found nothing
to do."""


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


def _digest_revs(revs: list[str]) -> str:
    """Order-independent digest of a set of commit ids."""
    return hashlib.sha256("\n".join(sorted(revs)).encode()).hexdigest()[:16]


_GIT_REPO_ENV = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_GRAFT_FILE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    }
)
"""`git rev-parse --local-env-vars`: what git itself clears before it runs in
another repository (a submodule). A git hook exports them for *its*
repository; `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` choose the user's own
config files and stay."""


def git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for a git (or jj) subprocess: this process's, without
    the variables that point git at another repository or add config to it,
    plus `extra`."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _GIT_REPO_ENV
        and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    }
    return {**env, **(extra or {})}


def require_git_version(exe: str) -> None:
    """Refuse a git older than `MIN_GIT_VERSION`; an unparseable banner (a
    custom build) passes, as jj's does.

    Raises:
        VcsError: `exe` is too old, missing, or fails `--version`.
    """
    try:
        proc = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, env=git_env()
        )
    except FileNotFoundError as exc:
        raise VcsError(f"executable not found: {exe}") from exc
    if proc.returncode != 0:
        raise VcsError(f"command failed ({exe} --version): {proc.stderr.strip()}")
    found = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", proc.stdout)
    if found and tuple(int(n or 0) for n in found.groups()) < MIN_GIT_VERSION:
        wanted = ".".join(str(n) for n in MIN_GIT_VERSION)
        raise VcsError(
            f"git {found.group(0)} is too old: tether needs git {wanted} or newer "
            f"({exe})"
        )


def _run(
    argv: list[str],
    *,
    cwd: Path,
    check: bool = True,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> _Run:
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            input=input_text,
            env=git_env(env),
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
                env=git_env(),
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
    argv = [git_exe, *_GIT_ISOLATION]
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

    def history_digest(self) -> str:
        """A digest of every visible commit id: changes whenever any workspace
        or bookmark of this repository gains or loses a commit. Plans whose
        safety rests on what history references (``gc``) bind to it."""

    def shared_dir(self) -> Path:
        """The directory every checkout of this repository shares (git's common
        dir; jj's repo dir), where a lock that must be seen by all of them
        can live. For a single checkout it is the checkout's own store."""

    def iter_history_files(self, reldir: str) -> Iterator[tuple[str, dict[str, str]]]:
        """Yield ``(commit id, files_at(commit, reldir))`` across all history.

        A conflicted jj commit is yielded once per input of the conflict --
        every side and base of its tree -- and last with the tree jj shows
        for it, so a walk that collects references sees every manifest the
        conflict may resolve to, and one that keeps a single answer per
        commit keeps the same one as before.

        Implementations stream through one object-reader process rather than
        spawning per commit; callers should consume lazily.
        """

    def conflicted_bookmarks(self) -> list[str]:
        """Local bookmarks with several targets (jj, after a divergent move or
        fetch). They are left out of `bookmarks()`, which has one commit per
        name, so the commits they reach would otherwise count as unreferenced;
        commands that judge references refuse while there are any. Always
        empty under git, whose refs have one target."""

    def conflicted_commits(self) -> list[str]:
        """Visible commits whose tree holds a conflict (jj). Empty under git."""

    def commit_info(self, revs: list[str]) -> list[CommitInfo]:
        """Author, timestamps, message, and parents for many commits in one call."""

    def refs(self) -> list[RefInfo]:
        """Named pointers (bookmarks / branches, tags) plus the ``head`` entry."""

    def workspace_roots(self) -> list[Path]:
        """Root directories of every live checkout of this repository.

        jj workspaces (``jj workspace list`` + ``jj workspace root --name``) or
        git worktrees (``git worktree list``). Used by ``gc --prune-bookmarks``
        to keep the working branches of workspaces that still exist.
        """

    def forget_workspace(self, root: Path) -> str | None:
        """Stop tracking the checkout at ``root``: ``jj workspace forget`` by
        name, or ``git worktree remove`` (a clean, non-main worktree). Returns
        a one-line description of what was done, or ``None`` when the VCS has
        nothing to forget there (git's main worktree; an unknown path)."""

    def dirty(self, relpaths: list[str]) -> bool:
        """Whether any of ``relpaths`` differs from the last commit (jj: ``@``
        vs its parent; git: index or worktree vs ``HEAD``)."""

    def tracked(self, relpaths: list[str]) -> list[str]:
        """Which of ``relpaths`` (files) the VCS tracks: in jj's working-copy
        commit as last snapshotted, in git's index. One call and no snapshot,
        so `Repo.find` can afford it on every run: a per-checkout file the VCS
        tracks came with a clone (or was committed by hand) and is refused."""

    def commit_alive(self, commit: str) -> bool:
        """Whether ``commit`` -- or, in jj, the change it belonged to -- is still
        part of visible history. jj resolves the commit to its change id (hidden
        commits still resolve) and asks whether that change is visible, so a
        rewrite does not count as loss; git checks reachability from any ref."""

    def alive_commits(self, commits: list[str]) -> set[str]:
        """``commit_alive`` for many commits in a bounded number of calls --
        ``status`` asks about every commit the op log records, and must not
        spawn a process per entry."""

    def abandon(
        self, revs: list[str], keep_dir: str
    ) -> tuple[list[str], dict[str, str]]:
        """Drop ``revs`` from history, rebasing their descendants -- except that
        every descendant keeps the files under ``keep_dir`` exactly as they
        were (snapshot, not patch, semantics: a manifest records a whole state,
        so a later commit's manifest must survive the removal of an earlier
        one untouched instead of conflicting with it). Returns the commit ids
        that were abandoned and ``{old commit id: new commit id}`` for the
        descendants that were rebased.

        jj: ``jj abandon`` then rewrite the descendants' ``keep_dir`` files
        back (which also resolves the conflicts jj recorded there). git: the
        commits must be on the current branch and the tree clean; ``rebase
        --onto`` with conflicts under ``keep_dir`` resolved to the original
        content, then a fix-up pass so every descendant's ``keep_dir`` matches
        what it held before.
        """

    def rewrite_history(
        self,
        reldir: str,
        transform: Callable[[str, dict[str, str]], dict[str, str]],
        *,
        ignore_immutable: bool = False,
    ) -> dict[str, str]:
        """Rewrite the files under ``reldir`` in every commit.

        ``transform(commit, files)`` receives ``{root-relative path: text}``
        and returns the texts that should be there instead (same keys; a
        commit whose result equals its input is left alone, but still gets a
        new id if a parent changed). Descendants are rebased, bookmarks /
        branches and tags follow, change ids (jj) are preserved. Returns
        ``{old commit id: new commit id}`` for every commit that changed.
        The working-copy commit is not rewritten (edit the working tree
        instead); the working copy ends where it started.

        This is history rewriting: every other clone must re-sync.
        ``ignore_immutable`` (jj) rewrites commits jj considers immutable.
        """

    def commit(
        self, relpaths: list[str], message: str, *, advance: str | None = None
    ) -> str:
        """Commit the given paths with ``message``; return the new commit id.

        ``advance`` names a bookmark / branch to move onto the new commit as
        part of the same operation (jj: one op-log entry, so ``jj undo`` takes
        the commit and the bookmark move back together; git: the checked-out
        branch moves by itself, any other is updated afterwards).
        """

    def position(self) -> dict[str, Any]:
        """Where the working copy is, as data `goto` can return to.

        Always has ``kind`` and ``id`` (jj: the working-copy change id; git:
        the HEAD commit) and ``commit``; jj adds ``parent`` and ``empty``, git
        adds ``branch`` (``None`` when detached).
        """

    def goto(self, position: dict[str, Any]) -> None:
        """Return the working copy to a ``position()`` taken earlier.

        jj: edit the change if it still exists, or start a fresh empty change
        on its parent when the recorded working copy was empty (jj abandons
        those when the working copy moves away). git: switch to the branch,
        or detach at the commit.
        """

    def uncommit(self, commit: str) -> bool:
        """Turn ``commit`` back into working-copy changes if it is still the
        working copy's parent (jj) / ``HEAD`` (git); ``False`` if it is not.

        jj: ``jj squash --from COMMIT --into @``; git: ``git reset --soft``.
        The tree is unchanged either way -- the commit's edits stay in the
        working copy as uncommitted changes.
        """

    def dependants(self, commit: str) -> list[str]:
        """Visible commits built on ``commit`` other than the working copy:
        what :meth:`uncommit` would rewrite. jj rebases every child of a
        commit it squashes away, so a bookmark's line on top of it would be
        rewritten onto the parent; git's ``reset --soft`` moves one branch
        and leaves the commit in place, so the answer is always empty."""

    def new(self, rev: str | None) -> None:
        """Move the working copy so the next ``commit`` lands on top of ``rev``.

        jj: ``jj new REV`` (a fresh empty change; ``None`` means on top of the
        current one). git has no empty-commit primitive: a branch name is
        switched to; any other revision is checked out onto a new branch
        ``tether/<rev12>`` so the commits that follow stay reachable (a
        detached HEAD would let ``gc`` treat them as gone); ``None`` is a
        no-op -- the next commit lands on the current branch.
        """

    def bookmarks(self) -> dict[str, str]:
        """Local bookmarks (jj) / branches (git): name -> commit id."""

    def bookmark_set(self, name: str, rev: str) -> None:
        """Create ``name`` at ``rev`` or move it there (backwards allowed)."""

    def bookmark_delete(self, name: str) -> None:
        """Delete the bookmark / branch ``name``."""

    def exclusive_commits(self, bookmark: str) -> list[str]:
        """Commit ids reachable from ``bookmark`` and from nothing else that
        keeps a commit visible -- no other local bookmark, tag, remote
        bookmark, or another workspace's working copy: the line of work that
        leaves visible history with the bookmark. Oldest first."""

    def remote_counterparts(self, bookmark: str) -> list[str]:
        """Remote refs for ``bookmark`` (``origin/feature``, ``feature@origin``):
        what still reaches its commits after a local drop, and what a later
        push deletes (jj) or leaves (git)."""

    def files_at_many(self, revs: list[str], reldir: str) -> dict[str, dict[str, str]]:
        """``{rev: files_at(rev, reldir)}`` for several commits through one
        object reader."""

    def drop_bookmark(self, bookmark: str, commits: list[str]) -> None:
        """Make ``bookmark`` and its ``commits`` (from :meth:`exclusive_commits`)
        disappear from visible history. jj abandons the commits (which takes
        the bookmark with them); git deletes the branch, after which nothing
        reaches the commits. The working copy must not be on the bookmark."""

    def current_bookmarks(self) -> list[str]:
        """The bookmarks the working copy is on.

        jj: the bookmarks at ``@``, else -- when ``@`` is empty -- at ``@-``
        (the usual state right after ``jj new main`` or ``tether commit``);
        several when they share a commit, as right after ``new_bookmark``.
        git: the branch ``HEAD`` points at, or nothing when detached.
        """

    def new_bookmark(self, name: str, rev: str | None) -> None:
        """Start a bookmark ``name`` at ``rev`` (default: the current base) and
        move the working copy onto it.

        jj: ``jj new REV`` then the bookmark at ``@-``. git: ``switch -c NAME
        [REV]``.
        """

    def is_ancestor(self, ancestor: str, rev: str) -> bool:
        """Whether ``ancestor`` is ``rev`` or one of its ancestors."""


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
        self._version_checked = False
        self._parking = threading.local()
        self._auto_track: str | None = None

    def _jj(self, *args: str, check: bool = True) -> _Run:
        self._require_version()
        if getattr(self._parking, "depth", 0):
            track = _JJ_PARKED
        else:
            value = json.dumps(self._user_auto_track())
            track = ("--config", f"snapshot.auto-track={value}")
        return _run(
            [self._exe, *_JJ_ISOLATION, *track, *args], cwd=self.root, check=check
        )

    def _user_auto_track(self) -> str:
        """The user's `snapshot.auto-track` (jj's default: `all()`) without
        tether's paths (`_JJ_TETHER_PATHS`); read once per adapter."""
        if self._auto_track is None:
            out = _run(
                [self._exe, *_JJ_ISOLATION, "config", "get", "snapshot.auto-track"],
                cwd=self.root,
                check=False,
            )
            theirs = out.stdout.strip() if out.returncode == 0 else ""
            self._auto_track = f"({theirs or 'all()'}) ~ {_JJ_TETHER_PATHS}"
        return self._auto_track

    @contextlib.contextmanager
    def _parked(self) -> Iterator[None]:
        """Snapshot nothing new while tether has the working copy elsewhere
        (see `_JJ_PARKED`); enter once the user's working copy is snapshotted."""
        self._parking.depth = getattr(self._parking, "depth", 0) + 1
        try:
            yield
        finally:
            self._parking.depth -= 1

    def _require_version(self) -> None:
        """Refuse a jj older than `MIN_JJ_VERSION`, once per adapter."""
        if self._version_checked:
            return
        out = _run([self._exe, "--version"], cwd=self.root)
        found = re.search(r"(\d+)\.(\d+)\.(\d+)", out.stdout)
        # An unparseable banner (a custom build) is not refused.
        if found and tuple(int(n) for n in found.groups()) < MIN_JJ_VERSION:
            wanted = ".".join(str(n) for n in MIN_JJ_VERSION)
            raise VcsError(
                f"jj {found.group(0)} is too old: tether needs jj {wanted} or newer "
                f"({self._exe})"
            )
        self._version_checked = True

    def _track(self, relpaths: list[str]) -> None:
        """Track tether's paths by name: a user's `snapshot.auto-track` or
        size limit may leave them out, and a commit of untracked paths is an
        empty commit that jj reports as a success."""
        if relpaths:
            self._jj("file", "track", *_JJ_TRACK_ONLY, *relpaths)

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
            "::",
            "-T",
            'commit_id ++ "\\n"',
        )
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]

    def history_digest(self) -> str:
        # Not the working-copy commits: jj re-snapshots them on any command,
        # so a digest over `::` would stale a saved gc plan after `jj log`.
        # A commit that lands in any workspace still changes the set.
        out = self._jj(
            "log",
            "--no-graph",
            "--ignore-working-copy",
            "-r",
            ":: ~ working_copies()",
            "-T",
            'commit_id ++ "\\n"',
        )
        return _digest_revs([line.strip() for line in out.stdout.splitlines() if line])

    def shared_dir(self) -> Path:
        repo = self.root / ".jj" / "repo"
        if repo.is_file():
            # A secondary workspace: the file holds the path of the primary
            # workspace's repo dir, relative to this `.jj/`.
            target = repo.read_text(encoding="utf-8").strip()
            return (repo.parent / target).resolve()
        return repo

    def iter_history_files(self, reldir: str) -> Iterator[tuple[str, dict[str, str]]]:
        revs = self.history_revs()
        reader = self._reader()
        if reader is None:
            for rev in revs:
                yield rev, self._files_at_slow(rev, reldir)
            return
        with reader:
            for rev in revs:
                for files in self._conflict_inputs(reader, rev, reldir):
                    yield rev, files
                # jj's virtual root commit has no git object; it reads as missing.
                yield rev, _prefixed(reldir, reader.files(_tree_spec(rev, reldir)))

    _CONFLICT_TREES = (".jjconflict-side-", ".jjconflict-base-")
    """Where jj keeps the inputs of a conflicted commit in its git tree: one
    subtree per side and per base, beside the tree it shows for the commit
    (side 0, in the versions tether supports)."""

    def _conflict_inputs(
        self, reader: GitObjectReader, rev: str, reldir: str
    ) -> Iterator[dict[str, str]]:
        """`reldir` as each input of a conflicted commit holds it."""
        if reader.fetch(f"{rev}:{self._CONFLICT_TREES[0]}0") is None:
            return  # not conflicted: the common case, one cheap miss
        root = reader.fetch(f"{rev}^{{tree}}")
        if root is None:  # pragma: no cover - the probe above just read it
            return
        sub = reldir.strip("/")
        for mode, name, _sha in _parse_tree(root[2]):
            if mode != _TREE_MODE or not name.startswith(self._CONFLICT_TREES):
                continue
            spec = f"{rev}:{name}" if sub in ("", ".") else f"{rev}:{name}/{sub}"
            yield _prefixed(reldir, reader.files(spec))

    def _change_ids(self) -> dict[str, str]:
        out = self._jj(
            "log",
            "--no-graph",
            "--ignore-working-copy",
            "-r",
            "::",
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

    def bookmarks(self) -> dict[str, str]:
        # --ignore-working-copy keeps this consistent with `history_revs` and
        # `refs` inside one export; a bookmark that sits on the working-copy
        # change itself (only right after `init`) may therefore report the
        # pre-snapshot id -- callers that need the live answer pass the
        # bookmark *name* to `is_ancestor` instead of this id.
        out = self._jj(
            "bookmark",
            "list",
            "--ignore-working-copy",
            "-T",
            'if(normal_target, if(remote, "", '
            'name ++ " " ++ normal_target.commit_id() ++ "\\n"))',
            check=False,
        )
        found: dict[str, str] = {}
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2:
                found[parts[0]] = parts[1]
        return found

    def conflicted_bookmarks(self) -> list[str]:
        out = self._jj(
            "bookmark",
            "list",
            "--ignore-working-copy",
            "-T",
            'if(conflict, if(remote, "", name ++ "\\n"), "")',
            check=False,
        )
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]

    def conflicted_commits(self) -> list[str]:
        out = self._jj(
            "log",
            "--no-graph",
            "--ignore-working-copy",
            "-r",
            "conflicts()",
            "-T",
            'commit_id ++ "\\n"',
            check=False,
        )
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]

    def bookmark_set(self, name: str, rev: str) -> None:
        self._jj("bookmark", "set", name, "-r", rev, "--allow-backwards")

    def bookmark_delete(self, name: str) -> None:
        self._jj("bookmark", "delete", name)

    def exclusive_commits(self, bookmark: str) -> list[str]:
        marks = self.bookmarks()
        if bookmark not in marks:
            raise VcsError(f"no bookmark {bookmark!r}")
        others = sorted({c for n, c in marks.items() if n != bookmark})
        # Everything else that keeps a commit visible: other local bookmarks,
        # tags, remote bookmarks (a pushed `B@origin` outlives the local
        # delete), and other workspaces' working copies. Not this
        # workspace's: on the bookmark it sits on top of the line.
        keep = " | ".join(
            [*others, "tags()", "remote_bookmarks()", "working_copies() ~ @"]
        )
        out = self._jj(
            "log",
            "--no-graph",
            "--reversed",
            "-r",
            f"(::{marks[bookmark]}) ~ (::({keep})) ~ root()",
            "-T",
            'commit_id ++ "\\n"',
        )
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]

    def remote_counterparts(self, bookmark: str) -> list[str]:
        out = self._jj(
            "bookmark",
            "list",
            "--all-remotes",
            "--ignore-working-copy",
            "-T",
            'if(remote, name ++ "@" ++ remote ++ "\\n", "")',
            check=False,
        )
        found = [line.strip() for line in out.stdout.splitlines() if line.strip()]
        # `@git` is jj's own view of the colocated repo, not a remote.
        return sorted(
            r for r in found if r.startswith(f"{bookmark}@") and not r.endswith("@git")
        )

    def files_at_many(self, revs: list[str], reldir: str) -> dict[str, dict[str, str]]:
        reader = self._reader()
        if reader is None:
            return {rev: self._files_at_slow(rev, reldir) for rev in revs}
        with reader:
            return {
                rev: _prefixed(reldir, reader.files(_tree_spec(rev, reldir)))
                for rev in revs
            }

    def drop_bookmark(self, bookmark: str, commits: list[str]) -> None:
        if commits:
            self._jj("abandon", *commits)
        if bookmark in self.bookmarks():  # jj drops it with its commit; else here
            self._jj("bookmark", "delete", bookmark)

    def _bookmarks_at(self, rev: str) -> list[str]:
        out = self._jj(
            "log",
            "--no-graph",
            "-r",
            rev,
            "-T",
            'local_bookmarks.map(|b| b.name()).join("\\n") ++ "\\n"',
            check=False,
        )
        return [b for b in out.stdout.split() if b]

    def current_bookmarks(self) -> list[str]:
        at = self._bookmarks_at("@")
        if not at and self.position().get("empty"):
            at = self._bookmarks_at("@-")
        return sorted(at)

    def new_bookmark(self, name: str, rev: str | None) -> None:
        # Start from `rev`, or from where the working copy already is: an
        # empty @ is kept (a fresh one would leave an empty, bookmarked commit
        # in history), a non-empty one is finalized first by `jj new`.
        if rev is not None:
            self._jj("new", rev)
        elif not self.position().get("empty"):
            self._jj("new", "@")
        with self._parked():
            self._jj("bookmark", "set", name, "-r", "@-", "--allow-backwards")

    def is_ancestor(self, ancestor: str, rev: str) -> bool:
        out = self._jj(
            "log",
            "--no-graph",
            "-r",
            f"({ancestor})::({rev})",
            "--limit",
            "1",
            "-T",
            "commit_id",
            check=False,
        )
        return out.returncode == 0 and bool(out.stdout.strip())

    def refs(self) -> list[RefInfo]:
        refs: list[RefInfo] = [
            RefInfo(name, "bookmark", commit)
            for name, commit in self.bookmarks().items()
        ]
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

    def forget_workspace(self, root: Path) -> str | None:
        names = self._jj(
            "workspace", "list", "--ignore-working-copy", "-T", 'name ++ "\\n"'
        ).stdout.split()
        target = root.resolve()
        for name in names:
            ws_root = self._jj(
                "workspace",
                "root",
                "--ignore-working-copy",
                "--name",
                name,
                check=False,
            )
            if (
                ws_root.returncode == 0
                and Path(ws_root.stdout.strip()).resolve() == target
            ):
                self._jj("workspace", "forget", name)
                return (
                    f"jj workspace {name!r} forgotten (its directory is left in place)"
                )
        return None

    def dirty(self, relpaths: list[str]) -> bool:
        # A new manifest is untracked until tracked, and `diff` does not show
        # untracked files; git's `status --porcelain` counts them.
        self._track(relpaths)
        out = self._jj("diff", "--summary", "-r", "@", *relpaths)
        return bool(out.stdout.strip())

    def tracked(self, relpaths: list[str]) -> list[str]:
        if not relpaths:
            return []
        # `root-file:` matches exactly that file from the root, whatever the
        # path holds (spaces, fileset operators); a path absent from `@` is
        # a warning on stderr, not an error.
        patterns = [f"root-file:{json.dumps(p, ensure_ascii=False)}" for p in relpaths]
        out = self._jj(
            "file", "list", "--ignore-working-copy", "-r", "@", "--", *patterns
        )
        listed = {line.strip() for line in out.stdout.splitlines()}
        return [p for p in relpaths if p in listed]

    def commit(
        self, relpaths: list[str], message: str, *, advance: str | None = None
    ) -> str:
        # jj auto-snapshots the working copy; scope the commit to our paths so
        # unrelated working-copy edits stay put. `jj commit` finalizes the
        # current @ (which becomes @-) and opens a fresh empty @ on top. With
        # `advance`, jj's advance-bookmarks setting moves that bookmark from
        # the parent onto the new commit inside the same operation.
        self._track(relpaths)
        args = ["commit", "-m", message]
        if advance is not None:
            args += [
                "--config",
                f"experimental-advance-branches.enabled-branches={json.dumps([advance])}",
            ]
        self._jj(*args, *relpaths)
        commit = self.resolve("@-")
        if advance is not None and self.bookmarks().get(advance) != commit:
            # The bookmark was not on the parent (or the setting is gone in
            # this jj): move it explicitly.
            self.bookmark_set(advance, commit)
        return commit

    def position(self) -> dict[str, Any]:
        out = self._jj(
            "log",
            "--no-graph",
            "-r",
            "@",
            "-T",
            'change_id ++ "\\n" ++ commit_id ++ "\\n" ++ if(empty, "1", "0") ++ "\\n"'
            ' ++ parents.map(|c| c.commit_id()).join(",")',
        )
        fields = [*out.stdout.rstrip("\n").split("\n"), "", "", "", ""]
        change, commit, empty, parents = fields[:4]
        return {
            "kind": "jj",
            "id": change,
            "commit": commit,
            "parent": parents.split(",")[0] if parents else None,
            "empty": empty == "1",
        }

    def goto(self, position: dict[str, Any]) -> None:
        if position.get("kind") != "jj":
            raise VcsError("position was not recorded by jj")
        if position.get("empty") and position.get("parent"):
            self._jj("new", str(position["parent"]))
            return
        self._jj("edit", str(position["id"]))

    def uncommit(self, commit: str) -> bool:
        parents = self._jj(
            "log", "--no-graph", "-r", "@-", "-T", 'commit_id ++ "\\n"'
        ).stdout.split()
        if parents != [commit]:
            return False
        # Move the commit's changes into @ (which keeps its own, empty,
        # description); the emptied source is abandoned by jj.
        self._jj("squash", "--from", commit, "--into", "@", "-u")
        return True

    def dependants(self, commit: str) -> list[str]:
        # Operator forms only (`::`, `~`): a user's revset aliases must not
        # change what counts as built on the commit.
        out = self._jj(
            "log",
            "--no-graph",
            "-r",
            f"({commit}:: ~ ({commit} | @))",
            "-T",
            'commit_id ++ "\\n"',
        )
        return out.stdout.split()

    def new(self, rev: str | None) -> None:
        self._jj("new", rev if rev is not None else "@")

    def commit_alive(self, commit: str) -> bool:
        change = self._jj(
            "log",
            "--no-graph",
            "--ignore-working-copy",
            "-r",
            commit,
            "-T",
            "change_id",
            check=False,
        )
        if change.returncode != 0 or not change.stdout.strip():
            return False
        visible = self._jj(
            "log",
            "--no-graph",
            "--ignore-working-copy",
            "-r",
            change.stdout.strip(),
            "-T",
            "commit_id",
            check=False,
        )
        return visible.returncode == 0 and bool(visible.stdout.strip())

    def alive_commits(self, commits: list[str]) -> set[str]:
        if not commits:
            return set()
        # Two calls: every visible change id, and the change id of each asked
        # commit (hidden commits still resolve when named in full).
        visible = self._jj(
            "log",
            "--no-graph",
            "--ignore-working-copy",
            "-r",
            "::",
            "-T",
            'change_id ++ "\\n"',
            check=False,
        )
        if visible.returncode != 0:
            return {c for c in commits if self.commit_alive(c)}
        changes = set(visible.stdout.split())
        asked = self._jj(
            "log",
            "--no-graph",
            "--ignore-working-copy",
            "-r",
            " | ".join(commits),
            "-T",
            'commit_id ++ " " ++ change_id ++ "\\n"',
            check=False,
        )
        if asked.returncode != 0:
            # One of them does not resolve at all: fall back per commit.
            return {c for c in commits if self.commit_alive(c)}
        change_of = dict(line.split() for line in asked.stdout.splitlines() if line)
        return {c for c in commits if change_of.get(c) in changes}

    def abandon(
        self, revs: list[str], keep_dir: str
    ) -> tuple[list[str], dict[str, str]]:
        ids = [self.resolve(r) for r in revs]
        union = " | ".join(ids)
        out = self._jj(
            "log",
            "--no-graph",
            "--reversed",
            "-r",
            f"({union}):: ~ ({union})",
            "-T",
            'change_id ++ " " ++ commit_id ++ "\\n"',
        )
        pairs = [line.split() for line in out.stdout.splitlines() if line.strip()]
        descendants = [change for change, _commit in pairs]
        old_commits = dict(pairs)
        before = {c: self.files_at(c, keep_dir) for c in descendants}
        start = self.position()
        with self._parked():
            self._jj("abandon", *ids)
            # Snapshot semantics for keep_dir: put every descendant's files
            # back. The working-copy change is a descendant too, but an *empty*
            # one only inherited its files; it follows its new parent instead.
            wc = start["id"]
            for change in descendants:
                files = before[change]
                if change == wc:
                    if not start.get("empty"):
                        for path, text in files.items():
                            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
                            (self.root / path).write_text(text, encoding="utf-8")
                        self._track(list(files))
                    continue
                if self.files_at(change, keep_dir) == files:
                    continue
                self._jj("new", change)
                for path, text in files.items():
                    (self.root / path).parent.mkdir(parents=True, exist_ok=True)
                    (self.root / path).write_text(text, encoding="utf-8")
                self._track(list(files))
                self._jj("squash", "-u")
            if descendants and descendants[-1] != wc:
                # `jj new` moved the working copy; go back (the old @ may have
                # been abandoned as empty, so land on its rebased parent).
                self.goto({**self.position(), "empty": True})
        mapping = {}
        for change in descendants:
            try:
                now = self.resolve(change)
            except VcsError:
                continue  # the empty working-copy change was dropped along the way
            if now != old_commits[change]:
                mapping[old_commits[change]] = now
        return ids, mapping

    def rewrite_history(
        self,
        reldir: str,
        transform: Callable[[str, dict[str, str]], dict[str, str]],
        *,
        ignore_immutable: bool = False,
    ) -> dict[str, str]:
        start = self.position()
        # Only commits that touch reldir can need new content; every other
        # descendant inherits its parent's files when jj rebases it. A dataset
        # nested in a large repository therefore pays for its own commits, not
        # the whole history.
        touched = self._jj(
            "log",
            "--no-graph",
            "--reversed",
            "-r",
            f'files("{reldir}") ~ root()',
            "-T",
            'change_id ++ " " ++ commit_id ++ "\\n"',
        )
        changes = [line.split() for line in touched.stdout.splitlines() if line.strip()]
        affected = self._jj(
            "log",
            "--no-graph",
            "-r",
            f'files("{reldir}"):: ~ root()',
            "-T",
            'change_id ++ " " ++ commit_id ++ "\\n"',
        )
        descendants = [
            line.split() for line in affected.stdout.splitlines() if line.strip()
        ]
        mapping: dict[str, str] = {}
        flags = ["--ignore-immutable"] if ignore_immutable else []
        rewritten = False
        with self._parked():
            for change, commit in changes:
                if change == start["id"]:
                    continue  # the working copy: the caller edits it in place
                files = self.files_at(commit, reldir)
                new_files = transform(commit, files)
                if new_files == files:
                    continue
                # A child of the change holding the new texts, squashed into
                # it; jj rebases the descendants and keeps the change id.
                self._jj("new", *flags, change)
                for path, text in new_files.items():
                    (self.root / path).parent.mkdir(parents=True, exist_ok=True)
                    (self.root / path).write_text(text, encoding="utf-8")
                self._track(list(new_files))
                self._jj("squash", "-u", *flags)
                rewritten = True
            if rewritten:
                for change, commit in descendants:
                    if change == start["id"]:
                        continue
                    try:
                        now = self.resolve(change)
                    except VcsError:
                        continue  # an empty working-copy change dropped along the way
                    if now != commit:
                        mapping[commit] = now
            # The recorded parent may itself have been rewritten; a `jj new` on
            # the old commit id would revive the hidden pre-rewrite history.
            if start.get("parent") in mapping:
                start = {**start, "parent": mapping[start["parent"]]}
            self.goto(start)
        return mapping


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #
class GitAdapter:
    kind = "git"

    def __init__(self, root: Path, executable: str = "git") -> None:
        self.root = root
        self._exe_unchecked = executable
        self._version_checked = False

    @property
    def _exe(self) -> str:
        """The git executable, refused once per adapter if older than
        `MIN_GIT_VERSION`; every call goes through here."""
        if not self._version_checked:
            require_git_version(self._exe_unchecked)
            self._version_checked = True
        return self._exe_unchecked

    def _git(
        self,
        *args: str,
        check: bool = True,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> _Run:
        return _run(
            [self._exe, *_GIT_ISOLATION, *args],
            cwd=self.root,
            check=check,
            input_text=input_text,
            env=env,
        )

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

    def history_digest(self) -> str:
        return _digest_revs(self.history_revs())

    def shared_dir(self) -> Path:
        out = self._git("rev-parse", "--git-common-dir", check=False)
        common = out.stdout.strip() if out.returncode == 0 else ".git"
        return (self.root / common).resolve()

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

    def forget_workspace(self, root: Path) -> str | None:
        roots = self.workspace_roots()
        target = root.resolve()
        if not roots or roots[0].resolve() == target:
            return None  # the main worktree cannot be removed
        if not any(r.resolve() == target for r in roots):
            return None
        self._git("worktree", "remove", str(target))
        return f"git worktree {target} removed"

    def dirty(self, relpaths: list[str]) -> bool:
        out = self._git(
            "status", "--porcelain", "--untracked-files=all", "--", *relpaths
        )
        return bool(out.stdout.strip())

    def tracked(self, relpaths: list[str]) -> list[str]:
        if not relpaths:
            return []
        out = self._git("--literal-pathspecs", "ls-files", "-z", "--", *relpaths)
        listed = set(filter(None, out.stdout.split("\0")))
        return [p for p in relpaths if p in listed]

    def commit(
        self, relpaths: list[str], message: str, *, advance: str | None = None
    ) -> str:
        self._git("add", "--", *relpaths)
        try:
            self._git("commit", "-m", message, "--", *relpaths)
        except VcsError:
            # A hook or a missing identity refused the commit. The index still
            # holds what `add` staged: manifests the caller is about to roll
            # back, naming pins it is about to release. Left there, the user's
            # next plain `git commit` records them.
            self._git("reset", "-q", "--", *relpaths, check=False)
            raise
        commit = self.current_rev()
        if advance is not None and self.bookmarks().get(advance) != commit:
            self.bookmark_set(advance, commit)
        return commit

    def conflicted_bookmarks(self) -> list[str]:
        return []  # a git ref has one target

    def conflicted_commits(self) -> list[str]:
        return []  # a merge in progress is the index's, not history's

    def position(self) -> dict[str, Any]:
        # An unborn branch (no commits yet) has a symbolic HEAD but no commit.
        head = self._git("rev-parse", "--verify", "--quiet", "HEAD", check=False)
        commit = head.stdout.strip() or None
        branch = self._git("symbolic-ref", "--short", "-q", "HEAD", check=False)
        return {
            "kind": "git",
            "id": commit,
            "commit": commit,
            "branch": branch.stdout.strip() or None,
        }

    def goto(self, position: dict[str, Any]) -> None:
        if position.get("kind") != "git":
            raise VcsError("position was not recorded by git")
        if position.get("branch"):
            self._git("switch", str(position["branch"]))
        elif position.get("commit"):
            self._git("switch", "--detach", str(position["commit"]))

    def uncommit(self, commit: str) -> bool:
        head = self._git("rev-parse", "--verify", "--quiet", "HEAD", check=False)
        if head.stdout.strip() != commit:
            return False
        parent = self._git(
            "rev-parse", "--verify", "--quiet", f"{commit}^", check=False
        )
        if parent.returncode == 0 and parent.stdout.strip():
            self._git("reset", "--soft", parent.stdout.strip())
        else:
            # A root commit: leave the branch unborn with the tree in place.
            self._git("update-ref", "-d", "HEAD")
        return True

    def dependants(self, commit: str) -> list[str]:
        return []  # `reset --soft` moves the branch; the commit stays as it is

    def new(self, rev: str | None) -> None:
        if rev is None:
            return  # git has no empty working-copy commit; stay on the branch
        is_branch = self._git(
            "rev-parse", "--verify", "--quiet", f"refs/heads/{rev}", check=False
        )
        if is_branch.returncode == 0 and is_branch.stdout.strip():
            self._git("switch", rev)
            return
        # A commit, tag, or other commit-ish: never leave HEAD detached, or the
        # dataset commits made here become unreachable the moment the user
        # switches away. Park them on a branch named after the base commit.
        sha = self.resolve(rev)
        name = f"tether/{sha[:12]}"
        for n in range(2, 1000):
            existing = self._git(
                "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False
            )
            if existing.returncode != 0:
                self._git("switch", "-c", name, sha)
                return
            if existing.stdout.strip() == sha:
                self._git("switch", name)
                return
            name = f"tether/{sha[:12]}-{n}"  # taken and moved on; start a sibling
        raise VcsError(f"too many tether/{sha[:12]} branches")  # pragma: no cover

    def bookmarks(self) -> dict[str, str]:
        # `tether/<sha12>` branches only park a checked-out commit so it stays
        # reachable (see `new`); they are not bookmarks the dataset works on.
        return {
            r.name: r.commit_id
            for r in _git_refs(self._exe, self.root, None)
            if r.kind == "branch" and not r.name.startswith("tether/")
        }

    def bookmark_set(self, name: str, rev: str) -> None:
        commit = self.resolve(rev)
        if self.current_bookmarks() == [name]:
            # The checked-out branch: move HEAD *and* the tree (`--keep`
            # refuses rather than overwrite local edits).
            self._git("reset", "--keep", commit)
            return
        self._git("update-ref", f"refs/heads/{name}", commit)

    def bookmark_delete(self, name: str) -> None:
        self._git("branch", "-D", name)

    def exclusive_commits(self, bookmark: str) -> list[str]:
        marks = self.bookmarks()
        if bookmark not in marks:
            raise VcsError(f"no branch {bookmark!r}")
        # Every other ref under refs/ keeps what it reaches -- other branches
        # (parking branches included), tags, remote-tracking refs -- which is
        # what `rev-list --all` still sees after `branch -D`.
        mine = f"refs/heads/{bookmark}"
        refs = self._git("for-each-ref", "--format=%(refname)", "refs/").stdout.split()
        others = [f"^{ref}" for ref in refs if ref != mine]
        out = self._git("rev-list", "--reverse", marks[bookmark], *others)
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]

    def remote_counterparts(self, bookmark: str) -> list[str]:
        out = self._git(
            "for-each-ref", "--format=%(refname:short)", "refs/remotes/", check=False
        )
        return sorted(r for r in out.stdout.split() if r.partition("/")[2] == bookmark)

    def files_at_many(self, revs: list[str], reldir: str) -> dict[str, dict[str, str]]:
        with GitObjectReader(self._exe, self.root) as reader:
            return {
                rev: _prefixed(reldir, reader.files(_tree_spec(rev, reldir)))
                for rev in revs
            }

    def drop_bookmark(self, bookmark: str, commits: list[str]) -> None:
        # Deleting the branch is the whole operation: nothing else reaches
        # its exclusive commits, so `rev-list --all` stops seeing them.
        self._git("branch", "-D", bookmark)

    def current_bookmarks(self) -> list[str]:
        out = self._git("symbolic-ref", "--short", "-q", "HEAD", check=False)
        name = out.stdout.strip()
        return [name] if name and not name.startswith("tether/") else []

    def new_bookmark(self, name: str, rev: str | None) -> None:
        args = ["switch", "-c", name]
        if rev is not None:
            args.append(self.resolve(rev))
        self._git(*args)

    def is_ancestor(self, ancestor: str, rev: str) -> bool:
        out = self._git("merge-base", "--is-ancestor", ancestor, rev, check=False)
        return out.returncode == 0

    def commit_alive(self, commit: str) -> bool:
        out = self._git("rev-list", "--all", check=False)
        return commit in out.stdout.split()

    def alive_commits(self, commits: list[str]) -> set[str]:
        out = self._git("rev-list", "--all", check=False)
        reachable = set(out.stdout.split())
        return {c for c in commits if c in reachable}

    def abandon(
        self, revs: list[str], keep_dir: str
    ) -> tuple[list[str], dict[str, str]]:
        status = self._git("status", "--porcelain", "--untracked-files=no")
        if status.stdout.strip():
            raise VcsError("git: commit or stash your changes before abandoning")
        ids = [self.resolve(r) for r in revs]
        done: list[str] = []
        mapping: dict[str, str] = {}
        for commit in ids:
            head = self.current_rev()
            if commit in done:
                continue
            anc = self._git("merge-base", "--is-ancestor", commit, head, check=False)
            if anc.returncode != 0:
                raise VcsError(
                    f"git: {commit[:12]} is not on the current branch; tether can "
                    "only abandon commits of the checked-out history"
                )
            parent = self._git(
                "rev-parse", "--verify", "--quiet", f"{commit}^", check=False
            ).stdout.strip()
            if not parent:
                raise VcsError(f"git: cannot abandon the root commit {commit[:12]}")
            descendants = self._git(
                "rev-list", "--reverse", f"{commit}..{head}"
            ).stdout.split()
            before = {d: self.files_at(d, keep_dir) for d in descendants}
            if not descendants:
                self._git("reset", "--hard", parent)
                done.append(commit)
                continue
            self._rebase_dropping(commit, parent, before, keep_dir)
            # Fix-up: every descendant's keep_dir exactly as it was.
            new_descendants = self._git(
                "rev-list", "--reverse", f"{parent}..HEAD"
            ).stdout.split()
            desired = dict(
                zip(new_descendants, (before[d] for d in descendants), strict=True)
            )
            fixed = self.rewrite_history(
                keep_dir, lambda c, files, want=desired: want.get(c, files)
            )
            for old_d, new_d in zip(descendants, new_descendants, strict=True):
                mapping[old_d] = fixed.get(new_d, new_d)
            done.append(commit)
        return ids, mapping

    def _rebase_dropping(
        self, commit: str, parent: str, before: dict[str, dict[str, str]], keep_dir: str
    ) -> None:
        env = {"GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"}
        run = self._git("rebase", "--onto", parent, commit, check=False, env=env)
        while run.returncode != 0:
            in_progress = self._git(
                "rev-parse", "--verify", "--quiet", "REBASE_HEAD", check=False
            )
            if in_progress.returncode != 0:
                raise VcsError(f"git rebase failed: {run.stderr.strip()}")
            original = in_progress.stdout.strip()
            conflicted = self._git(
                "diff", "--name-only", "--diff-filter=U"
            ).stdout.split()
            outside = [f for f in conflicted if not f.startswith(f"{keep_dir}/")]
            if outside or original not in before:
                self._git("rebase", "--abort", check=False)
                raise VcsError(
                    "git: abandoning conflicts outside the dataset "
                    f"({', '.join(outside) or original[:12]}); resolve by hand"
                )
            for path, text in before[original].items():
                (self.root / path).parent.mkdir(parents=True, exist_ok=True)
                (self.root / path).write_text(text, encoding="utf-8")
            self._git("add", "--", keep_dir)
            run = self._git("rebase", "--continue", check=False, env=env)

    def rewrite_history(
        self,
        reldir: str,
        transform: Callable[[str, dict[str, str]], dict[str, str]],
        *,
        ignore_immutable: bool = False,
    ) -> dict[str, str]:
        start = self.position()
        out = self._git("rev-list", "--reverse", "--topo-order", "--all", check=False)
        commits = [c.strip() for c in out.stdout.splitlines() if c.strip()]
        mapping: dict[str, str] = {}
        git_dir = self._git("rev-parse", "--git-dir").stdout.strip()
        index_file = (
            self.root / git_dir / f"tether-rewrite-{os.getpid()}.index"
        ).resolve()
        env = {"GIT_INDEX_FILE": str(index_file)}
        # Commits sharing a reldir subtree share the transform's answer; in a
        # large repository most commits never touch the dataset at all.
        by_subtree: dict[str, tuple[dict[str, str], dict[str, str]]] = {}
        try:
            for commit in commits:
                parents = self._git(
                    "rev-list", "--parents", "-n1", commit
                ).stdout.split()[1:]
                new_parents = [mapping.get(p, p) for p in parents]
                sub = self._git(
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"{commit}:{reldir}",
                    check=False,
                ).stdout.strip()
                if sub in by_subtree:
                    files, new_files = by_subtree[sub]
                else:
                    files = self.files_at(commit, reldir) if sub else {}
                    new_files = transform(commit, files) if files else files
                    by_subtree[sub] = (files, new_files)
                tree = self._git("rev-parse", f"{commit}^{{tree}}").stdout.strip()
                if new_files != files:
                    self._git("read-tree", tree, env=env)
                    for path, text in new_files.items():
                        blob = self._git(
                            "hash-object", "-w", "--stdin", input_text=text
                        ).stdout.strip()
                        self._git(
                            "update-index",
                            "--add",
                            "--cacheinfo",
                            f"100644,{blob},{path}",
                            env=env,
                        )
                    tree = self._git("write-tree", env=env).stdout.strip()
                elif new_parents == parents:
                    continue
                meta = self._git(
                    "log",
                    "-1",
                    "--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B",
                    commit,
                ).stdout
                an, ae, ad, cn, ce, cd, message = meta.split("\x00", 6)
                args = ["commit-tree", tree]
                for p in new_parents:
                    args += ["-p", p]
                new_commit = self._git(
                    *args,
                    input_text=message,
                    env={
                        "GIT_AUTHOR_NAME": an,
                        "GIT_AUTHOR_EMAIL": ae,
                        "GIT_AUTHOR_DATE": ad,
                        "GIT_COMMITTER_NAME": cn,
                        "GIT_COMMITTER_EMAIL": ce,
                        "GIT_COMMITTER_DATE": cd,
                    },
                ).stdout.strip()
                mapping[commit] = new_commit
        finally:
            index_file.unlink(missing_ok=True)
        if not mapping:
            return mapping
        refs = self._git(
            "for-each-ref", "--format=%(refname) %(objectname) %(objecttype)"
        ).stdout.splitlines()
        for line in refs:
            name, sha, kind = line.split()
            if kind == "commit" and sha in mapping:
                self._git("update-ref", name, mapping[sha], sha)
        if start.get("branch"):
            # The branch ref moved under HEAD; refresh the index (worktree files
            # under reldir still hold the old text until the caller rewrites them).
            self._git("reset", "-q")
        elif start.get("commit") in mapping:
            self._git("update-ref", "--no-deref", "HEAD", mapping[start["commit"]])
            self._git("reset", "-q")
        return mapping


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
