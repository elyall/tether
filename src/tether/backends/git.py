"""Git backend (Forkable); also covers jj colocated repositories.

Lets a dataset commit pin the *code* that produced it. A branch (or detached
sha) is the working ref, the commit ``sha`` is the state, a lightweight tag
``tether.<pin_id>`` is the pin, and a branch off that tag is a fork. When the
locator names a ``remote`` (one the repository has configured, or any URL from
``.tether/secrets.toml``), pins are pushed there so they survive beyond a single
clone; otherwise they are durable only locally (documented).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Collection
from pathlib import Path
from types import MappingProxyType

from tether.backends.base import (
    Capability,
    HistoryEntry,
    Listings,
    ObjectBackend,
    ObjectDiff,
    VerifyReport,
    VerifyStatus,
    base_at,
    check_expected,
    register_backend,
)
from tether.errors import BackendError, MergeConflict, RefMovedError, VcsError
from tether.handles import GitHandle, Handle
from tether.manifest import WORKING_REF_PREFIX, Locator, Pin, State, ref_for_pin
from tether.vcs import git_env, require_git_version

_HARDENED = (
    "-c",
    "core.fsmonitor=",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "safe.bareRepository=explicit",
    "-c",
    "protocol.ext.allow=never",
)
"""Config for every call. A repository's config may name commands to run
(`core.fsmonitor`, hooks, `ext::` transports), and git takes any directory
holding `HEAD`, `objects/` and `refs/` for a bare repository, which a clone
can ship as plain files; tether needs none of it."""

_ZERO_SHA = "0" * 40
"""`update-ref`'s old value for "the ref must not exist yet" (git spells an
absent ref as the all-zero object id)."""


class GitBackend(ObjectBackend):
    kind = "git"
    LOCAL_PATH_KEYS = ("path", "uri")  # `uri` is the CLI's positional locator
    _LOCATOR_REFS = ("ref", "at", "remote")
    SAFE_CONFIG_KEYS = frozenset()  # git_path / jj_path: secrets.toml only
    # A change id is derived from the sha (and only present with jj); the same
    # sha must pin identically with or without jj installed.
    VOLATILE_KEYS = frozenset({"change_id", "dirty"})
    """`change_id` is jj's view of the same commit; `dirty` is the checkout's
    condition, not the commit's -- two branches at one sha are the same state
    whether or not one of them is checked out with stray files. `pin` still
    reads the full state and refuses a dirty tree."""
    capabilities = (
        Capability.FINGERPRINT
        | Capability.ADDRESSABLE
        | Capability.PIN
        | Capability.FORK
        | Capability.CHEAP_FINGERPRINT
        | Capability.ATOMIC_REF
        | Capability.DIFF
        | Capability.HISTORY
        | Capability.CREATE
        | Capability.PROMOTE
        | Capability.MERGE
        | Capability.CONDITIONAL_REF
    )
    """`CONDITIONAL_REF`: forks and promotes move refs with `update-ref` and
    an old value. A merge, and a promote onto a checked-out base, run in the
    checkout (`git merge`) and compare the head just before; a commit landing
    in between is merged or refused as not a fast-forward, never discarded."""

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}
        configured = self._config.get("git_path")
        self._git: str = configured or shutil.which("git") or "git"
        self._checkout: Path | None = None
        self._version_checked = False

    def _require_version(self) -> None:
        """Refuse a git older than `MIN_GIT_VERSION`, once per backend:
        `_HARDENED`'s `safe.bareRepository` means nothing to an older one."""
        if self._version_checked:
            return
        try:
            require_git_version(self._git)
        except VcsError as exc:
            raise BackendError(str(exc), kind="git") from exc
        self._version_checked = True

    def configure_checkout(self, root: Path) -> None:
        self._checkout = root.resolve()

    # -- helpers --------------------------------------------------------- #
    def _local(self, locator: Locator) -> Path:
        # `path`, or the CLI's positional locator (`uri`) when it is a local path.
        path = locator.get("path") or locator.get("uri")
        if not path or "://" in str(path):
            raise BackendError(
                "git backend needs a local 'path' (url-only clones are not "
                "supported yet)",
                kind="git",
            )
        return Path(str(path))

    def _path(self, locator: Locator) -> Path:
        """The repository to run git in. A manifest arrives with every clone,
        so it may not name a relative path (`add` stores an absolute one) or
        one inside the checkout the clone made: that directory is the clone's
        own files."""
        path = self._local(locator)
        if not path.is_absolute():
            raise BackendError(
                f"git path {str(path)!r} is not absolute; `add` stores an "
                "absolute path, so this manifest was written by hand",
                kind="git",
            )
        if self._checkout is not None and path.resolve().is_relative_to(self._checkout):
            raise BackendError(
                f"git path {path} is inside the dataset's checkout "
                f"({self._checkout}), whose files came with the clone; point "
                "it at a repository outside it",
                kind="git",
            )
        return path

    def validate_locator(self, locator: Locator) -> None:
        # Manifests are committed: a `ref` or `at` that starts with `-` would
        # reach git as an option. Every call site also says --end-of-options;
        # refusing here gives the error at `add`, not at the first read.
        for key in self._LOCATOR_REFS:
            value = locator.get(key)
            if value is not None:
                _guard(str(value), key)
        path = locator.get("path") or locator.get("uri")
        if path is not None:
            _guard(str(path), "path")
            self._path(locator)
        remote = locator.get("remote")
        if remote is not None:
            self._configured_remote(locator, str(remote))

    def _configured_remote(self, locator: Locator, remote: str) -> str:
        _guard(remote, "remote")
        if remote not in self._run(locator, "remote").splitlines():
            raise BackendError(
                f"{remote!r} is not a remote configured in {self._path(locator)}. "
                "A committed locator arrives with every clone, so it names a "
                "remote the repository already has (`git remote add`); a URL "
                'goes in .tether/secrets.toml, as [objects."<key>"] remote',
                kind="git",
            )
        return remote

    def _remote(self, locator: Locator) -> str | None:
        """Where pins are pushed: any URL `.tether/secrets.toml` gives the
        object, else a configured remote the committed locator names."""
        local = self.secrets_for(locator).get("remote")
        if local:
            return _guard(str(local), "remote")
        remote = locator.get("remote")
        return self._configured_remote(locator, str(remote)) if remote else None

    def _proc(
        self, locator: Locator, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        self._require_version()
        return subprocess.run(
            [self._git, *_HARDENED, "-C", str(self._path(locator)), *args],
            capture_output=True,
            text=True,
            env=git_env(env),
        )

    def _run(
        self,
        locator: Locator,
        *args: str,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> str:
        proc = self._proc(locator, *args, env=env)
        if check and proc.returncode != 0:
            raise BackendError(
                f"git {' '.join(args)} failed: {proc.stderr.strip()}",
                kind="git",
            )
        return proc.stdout.strip()

    def _base_ref(self, locator: Locator) -> str:
        # `at` (any commit-ish) is the cross-backend spelling of `ref`.
        return base_at(locator) or str(locator.get("ref", "HEAD"))

    def _change_id(self, locator: Locator, sha: str) -> str | None:
        if not (self._path(locator) / ".jj").exists():
            return None
        jj = self._config.get("jj_path") or shutil.which("jj") or "jj"
        proc = subprocess.run(
            [
                jj,
                "log",
                "--no-graph",
                "--ignore-working-copy",
                "-r",
                sha,
                "-T",
                "change_id",
            ],
            cwd=str(self._path(locator)),
            capture_output=True,
            text=True,
            env=git_env(),
        )
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    # -- protocol -------------------------------------------------------- #
    def identity(self, locator: Locator) -> Locator:
        # Resolve to an absolute path so equal repos dedupe. Bookkeeping only:
        # a path git may not run in still has an identity to group it by.
        return {"path": str(self._local(locator).resolve())}

    def fingerprint(self, locator: Locator, working_ref: str | None) -> State:
        ref = working_ref or self._base_ref(locator)
        sha = self._run(
            locator,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{_guard(ref)}^{{commit}}",
        )
        # Uncommitted changes belong to the checked-out ref only; a dirty
        # worktree says nothing about a branch that is not checked out.
        checked_out = self._checked_out(locator)
        on_ref = ref == "HEAD" or (checked_out is not None and ref == checked_out)
        dirty = on_ref and bool(self._run(locator, "status", "--porcelain"))
        state: State = {"sha": sha, "dirty": dirty}
        change_id = self._change_id(locator, sha)
        if change_id:
            state["change_id"] = change_id
        return state

    def pin(self, locator: Locator, state: State, pin_id: str) -> Pin:
        if state.get("dirty") and not locator.get("allow_dirty"):
            raise BackendError(
                "refusing to pin a dirty working tree (set allow_dirty)",
                kind="git",
            )
        ref = ref_for_pin(pin_id)
        sha = _sha(state["sha"])
        remote = self._remote(locator)
        existing = self._run(
            locator,
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{ref}^{{commit}}",
            check=False,
        )
        if not existing:
            self._run(locator, "tag", "--end-of-options", ref, sha)
        elif existing != sha:
            raise BackendError(
                f"tag {ref} already points at {existing}, not {sha}", kind="git"
            )
        if remote:
            # The pin is only durable once it is on the remote. A push that
            # fails must fail the pin -- and not leave a local tag that would
            # make the two diverge silently.
            try:
                self._run(
                    locator, "push", "--end-of-options", remote, f"refs/tags/{ref}"
                )
            except BackendError as exc:
                if not existing:
                    self._run(locator, "tag", "-d", ref, check=False)
                raise BackendError(
                    f"pin {ref} was not pushed to {remote}: {exc}", kind="git"
                ) from exc
        return Pin(id=pin_id, ref=ref, created=not existing)

    def unpin(self, locator: Locator, pin: Pin) -> None:
        remote = self._remote(locator)
        if remote:
            # Remote first: if the remote still has the tag the pin still
            # exists, and gc must hear about it rather than believe it gone.
            out = self._proc(
                locator,
                "push",
                "--delete",
                "--end-of-options",
                remote,
                f"refs/tags/{pin.ref}",
            )
            if out.returncode != 0 and "remote ref does not exist" not in out.stderr:
                raise BackendError(
                    f"pin {pin.ref} was not deleted on {remote}: {out.stderr.strip()}",
                    kind="git",
                )
        out = self._proc(locator, "tag", "-d", pin.ref)
        if out.returncode != 0 and self._run(
            locator,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/tags/{pin.ref}",
            check=False,
        ):
            # Still there: not "already gone" but a real failure (permissions,
            # a hook, a locked ref). Say so instead of reporting success.
            raise BackendError(
                f"tag {pin.ref} was not deleted: {out.stderr.strip()}", kind="git"
            )

    def list_pins(self, locator: Locator) -> set[str]:
        prefix = ref_for_pin("")
        out = self._run(locator, "tag", "--list", f"{prefix}*")
        return {
            line[len(prefix) :]
            for line in out.splitlines()
            if line.startswith(prefix) and not line.startswith(f"{prefix}ws.")
        }

    def verify(
        self,
        locator: Locator,
        state: State,
        pin: Pin | None,
        deep: bool,
    ) -> VerifyReport:
        sha = _sha(state["sha"])
        target = pin.ref if pin is not None else sha
        resolved = self._run(
            locator,
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{target}^{{commit}}",
            check=False,
        )
        if not resolved:
            return VerifyReport(VerifyStatus.MISSING, f"{target} not found")
        if resolved != sha:
            return VerifyReport(
                VerifyStatus.DRIFTED, f"{target} -> {resolved}, expected {sha}"
            )
        return VerifyReport(VerifyStatus.OK)

    def fork(
        self,
        locator: Locator,
        source: Pin | State,
        name: str,
        *,
        expected: State | None = None,
    ) -> str:
        ref = source.ref if isinstance(source, Pin) else str(source["sha"])
        sha = self._run(
            locator,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{_guard(ref, 'source')}^{{commit}}",
        )
        exists = self._branch_sha(locator, _guard(name, "branch"))
        if expected is None:
            if exists == sha:
                return name  # already at the source: left alone (no reflog entry)
            if exists:
                self._run(locator, "branch", "-f", "--end-of-options", name, sha)
            else:
                self._run(locator, "branch", "--end-of-options", name, sha)
            return name
        # `update-ref` with an old value is git's compare-and-swap: the branch
        # moves only if it still holds `expected` (or does not exist yet, for
        # ABSENT), in one ref transaction.
        old = _old_value(expected)
        if exists and exists == sha == old:
            return name  # already at the source: left alone
        if exists != sha and (where := self._worktree_of(locator, name)):
            # What `branch -f` refuses too, in every worktree: moving the ref
            # under a checkout leaves its index and working tree describing
            # another commit.
            raise BackendError(
                f"branch {name} is checked out in {where}; check out another "
                "branch there before it is reset",
                kind="git",
            )
        proc = self._proc(locator, "update-ref", f"refs/heads/{name}", sha, old)
        if proc.returncode != 0:
            raise self._moved_or_failed(locator, name, old, proc.stderr, what="fork")
        return name

    def _branch_sha(self, locator: Locator, branch: str) -> str:
        """The commit `refs/heads/<branch>` names, or `""` when there is no
        such branch (a tag of the same name does not count)."""
        return self._run(
            locator,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            check=False,
        )

    def _moved_or_failed(
        self, locator: Locator, branch: str, old: str, stderr: str, *, what: str
    ) -> BackendError:
        """Why `update-ref refs/heads/<branch> <new> <old>` failed.

        `RefMovedError` when the branch is not at `old` -- that is what the
        compare-and-swap refuses, and git's message does not say so in a
        form worth parsing; anything else (a locked ref, permissions) is the
        failure itself.
        """
        now = self._branch_sha(locator, branch)
        if old == _ZERO_SHA:
            if not now:
                return BackendError(
                    f"git update-ref {branch} failed: {stderr.strip()}", kind="git"
                )
            return RefMovedError(
                f"{what}: {branch} exists (at {now[:12]}), expected absent", kind="git"
            )
        if now == old:
            return BackendError(
                f"git update-ref {branch} failed: {stderr.strip()}", kind="git"
            )
        return RefMovedError(
            f"{what}: {branch} is at {now[:12] or 'nothing'}, expected {old[:12]}",
            kind="git",
        )

    def delete_working_ref(self, locator: Locator, ref: str) -> None:
        out = self._proc(
            locator, "branch", "-D", "--end-of-options", _guard(ref, "branch")
        )
        if out.returncode != 0 and self._run(
            locator,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{ref}",
            check=False,
        ):
            raise BackendError(
                f"branch {ref} was not deleted: {out.stderr.strip()}", kind="git"
            )

    def list_working_refs(self, locator: Locator) -> list[str]:
        out = self._run(
            locator,
            "for-each-ref",
            "--format=%(refname:short)",
            f"refs/heads/{WORKING_REF_PREFIX}*",
        )
        return sorted(line.strip() for line in out.splitlines() if line.strip())

    # -- store lifecycle ------------------------------------------------- #
    _OWNER_KEY = "tether.owner"
    """`git config` key holding the owning dataset id (`create`)."""

    def create(self, locator: Locator, *, owner: str) -> State:
        path = self._path(locator)
        if path.exists():
            raise BackendError(
                f"{path} already exists; `create` never adopts it", kind="git"
            )
        base = str(locator.get("ref", "main"))
        _guard(base, "ref")
        path.mkdir(parents=True)
        # One empty root commit so the base branch exists and can be pinned,
        # forked, and fingerprinted; a repository with an unborn HEAD cannot.
        # Author, dates, and tree are fixed, so every store tether creates
        # has the *same* root sha (`EMPTY_ROOT_SHA`): `is_ref_empty` compares
        # the base to it, and two creates fingerprint identically.
        self._run(locator, "init", "-q", "-b", base)
        self._run(locator, "config", self._OWNER_KEY, owner)
        root = self._run(
            locator,
            "commit-tree",
            self.EMPTY_TREE,
            "-m",
            "tether: created store",
            env=dict(self._ROOT_ENV),
        )
        if root != self.EMPTY_ROOT_SHA:  # pragma: no cover - git would have to change
            raise BackendError(
                f"unexpected root commit {root} (expected {self.EMPTY_ROOT_SHA})",
                kind="git",
            )
        self._run(locator, "update-ref", f"refs/heads/{base}", root)
        self._run(locator, "reset", "-q", "--hard", root)
        return self.fingerprint(locator, None)

    EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    """git's well-known empty tree object."""
    _ROOT_ENV = MappingProxyType(
        {
            "GIT_AUTHOR_NAME": "tether",
            "GIT_AUTHOR_EMAIL": "tether@localhost",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
            "GIT_COMMITTER_NAME": "tether",
            "GIT_COMMITTER_EMAIL": "tether@localhost",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
        }
    )
    EMPTY_ROOT_SHA = "5a2cfa770badaceb6c6de5171fdf82c871fa40de"
    """The root commit `create` makes: empty tree, fixed author and dates.
    Verified against git at create time; the value is a constant so a
    reviewer can check a created store by eye."""

    def owner(self, locator: Locator) -> str | None:
        path = self._path(locator)
        if not (path / ".git").exists():
            return None
        value = self._run(locator, "config", "--get", self._OWNER_KEY, check=False)
        return value or None

    def is_ref_empty(
        self, locator: Locator, *, ignoring: Collection[str] = ()
    ) -> bool | None:
        path = self._path(locator)
        if not (path / ".git").exists():
            return None
        base = str(locator.get("ref", "main"))
        refs = self._run(
            locator,
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads/",
            "refs/tags/",
        ).splitlines()
        stray = [r.strip() for r in refs if r.strip() and r.strip() not in ignoring]
        if stray != [base]:
            return False
        # The base branch still *at* the root commit `create` made -- the same
        # sha, not merely one commit deep: an amended root with files in it
        # is one commit too.
        head = self._run(
            locator,
            "rev-parse",
            "--verify",
            "-q",
            "--end-of-options",
            f"{_guard(base, 'ref')}^{{commit}}",
            check=False,
        )
        if head != self.EMPTY_ROOT_SHA:
            return False
        # Refs are not the whole repository: files someone put in the working
        # tree -- untracked, staged, even ignored -- are data too, and
        # `delete_store` would take them with the directory.
        dirty = self._run(
            locator, "status", "--porcelain", "--ignored", "--untracked-files=all"
        )
        return not dirty.strip()

    def delete_store(self, locator: Locator) -> None:
        path = self._path(locator)
        if not (path / ".git").exists() or self.owner(locator) is None:
            raise BackendError(
                f"{path} is not a repository tether created; not removing it",
                kind="git",
            )
        # The caller checked; check again here, where the rmtree is.
        if self.is_ref_empty(locator) is not True:
            raise BackendError(
                f"{path} is not empty (refs, commits, or files in the working "
                "tree); not removing it",
                kind="git",
            )
        shutil.rmtree(path)

    # -- promote / merge ------------------------------------------------- #
    def _checked_out(self, locator: Locator) -> str | None:
        out = self._run(locator, "symbolic-ref", "--short", "-q", "HEAD", check=False)
        return out or None

    def _worktree_of(
        self, locator: Locator, branch: str, *, other: bool = False
    ) -> str | None:
        """The worktree that has `branch` checked out (an unborn one too), or
        `None`; with `other`, only a worktree other than the locator's."""
        top = self._run(locator, "rev-parse", "--show-toplevel", check=False)
        here = Path(top).resolve() if top else None
        where: str | None = None
        for line in self._run(locator, "worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                where = line.removeprefix("worktree ")
            elif (
                line == f"branch refs/heads/{branch}"
                and where is not None
                and (not other or Path(where).resolve() != here)
            ):
                return where
        return None

    def _base_branch(self, locator: Locator) -> str:
        """The branch `promote`/`merge` move: `ref`, or the checked-out branch."""
        ref = str(locator.get("ref", "HEAD"))
        if ref == "HEAD":
            branch = self._checked_out(locator)
            if branch is None:
                raise BackendError(
                    "HEAD is detached; set the locator's `ref` to a branch to promote",
                    kind="git",
                )
            return branch
        if not self._run(
            locator,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{ref}",
            check=False,
        ):
            raise BackendError(f"{ref!r} is not a local branch", kind="git")
        return ref

    base_branch = _base_branch

    @staticmethod
    def _source_ref(source: str | Pin | State) -> str:
        if isinstance(source, Pin):
            return source.ref
        if isinstance(source, dict):
            return str(source["sha"])
        return source

    def ancestor_of(
        self, locator: Locator, ancestor: State, descendant: str | Pin | State
    ) -> bool | None:
        proc = self._proc(
            locator,
            "merge-base",
            "--is-ancestor",
            "--end-of-options",
            _sha(ancestor["sha"]),
            _guard(self._source_ref(descendant), "source"),
        )
        if proc.returncode in (0, 1):
            return proc.returncode == 0
        raise BackendError(f"git merge-base failed: {proc.stderr.strip()}", kind="git")

    def promote(
        self,
        locator: Locator,
        source: str | Pin | State,
        *,
        expected: State | None = None,
    ) -> State:
        base = self._base_branch(locator)
        target = self._run(
            locator,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{_guard(self._source_ref(source), 'source')}^{{commit}}",
        )
        head = self._run(locator, "rev-parse", "--verify", f"refs/heads/{base}")
        # The old value for `update-ref`: the head the caller reviewed, or the
        # one just read when no plan is involved.
        old = head if expected is None else _old_value(expected)
        if old != head:
            raise RefMovedError(
                f"promote: {base} is at {head[:12]}, expected "
                f"{'absent' if old == _ZERO_SHA else old[:12]}",
                kind="git",
            )
        if target == head:
            return self.fingerprint(locator, base)
        if not self.ancestor_of(locator, {"sha": head}, target):
            raise BackendError(
                f"{base} moved to {head[:12]}, which is not an ancestor of "
                f"{target[:12]}; merge instead",
                kind="git",
            )
        if self._checked_out(locator) == base:
            if self._run(locator, "status", "--porcelain"):
                raise BackendError(
                    f"{base} is checked out with uncommitted changes; commit or stash "
                    "them first",
                    kind="git",
                )
            # `merge --ff-only` moves HEAD, the index and the working tree
            # together and has no old-value form, so on this path the head
            # compared above is a check before the act: a commit landing on
            # `base` between the two is not caught the way `update-ref`
            # catches it below.
            self._run(locator, "merge", "--ff-only", "--end-of-options", target)
        elif where := self._worktree_of(locator, base, other=True):
            raise BackendError(
                f"{base} is checked out in {where}; moving it from here would "
                "leave that checkout describing another commit -- promote from "
                "there, or check out another branch there first",
                kind="git",
            )
        else:
            proc = self._proc(locator, "update-ref", f"refs/heads/{base}", target, old)
            if proc.returncode != 0:
                raise self._moved_or_failed(
                    locator, base, old, proc.stderr, what="promote"
                )
        return self.fingerprint(locator, base)

    def merge(
        self,
        locator: Locator,
        source: str | Pin | State,
        message: str,
        *,
        expected: State | None = None,
    ) -> State:
        source_ref = self._source_ref(source)
        base = self._base_branch(locator)
        if self._checked_out(locator) != base:
            raise BackendError(
                f"git merges into the checked-out branch; check out {base} first",
                kind="git",
            )
        if self._run(locator, "status", "--porcelain"):
            raise BackendError(
                f"{base} has uncommitted changes; commit or stash them first",
                kind="git",
            )
        # `git merge` works on the checkout and has no old-value form: the
        # reviewed head is compared right before it, a check rather than a
        # swap.
        check_expected(self, locator, expected, what="merge")
        proc = self._proc(
            locator,
            "merge",
            "--no-ff",
            "-m",
            message,
            "--end-of-options",
            _guard(source_ref, "source"),
        )
        if proc.returncode != 0:
            conflicts = self._run(
                locator, "diff", "--name-only", "--diff-filter=U", check=False
            ).splitlines()
            self._run(locator, "merge", "--abort", check=False)
            raise MergeConflict(
                f"merging {source_ref} into {base} conflicts in "
                f"{len(conflicts)} file(s)",
                conflicts=[c.strip() for c in conflicts if c.strip()],
                kind="git",
            )
        return self.fingerprint(locator, base)

    def open(
        self,
        locator: Locator,
        target: str | Pin | State | None,
        read_only: bool,
    ) -> Handle:
        path = str(self._path(locator).resolve())
        if isinstance(target, Pin):
            sha = self._run(
                locator,
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{target.ref}^{{commit}}",
            )
            return GitHandle(key=path, read_only=True, path=path, sha=sha)
        if isinstance(target, dict):
            sha = _sha(target["sha"])
            return GitHandle(key=path, read_only=True, path=path, sha=sha)
        ref = target or self._base_ref(locator)
        sha = self._run(
            locator,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{_guard(ref)}^{{commit}}",
        )
        return GitHandle(key=path, read_only=read_only, path=path, sha=sha)

    def history(
        self,
        locator: Locator,
        ref: str | None = None,
        limit: int = 20,
    ) -> list[HistoryEntry]:
        start = ref or self._base_ref(locator)
        out = self._run(
            locator,
            "log",
            f"--max-count={int(limit)}",
            "--format=%H%x1f%cI%x1f%D%x1f%s",
            "--end-of-options",
            _guard(start),
            "--",
        )
        entries: list[HistoryEntry] = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) < 4:
                continue
            sha, when, decorations, subject = parts[0], parts[1], parts[2], parts[3]
            refs = [
                d.strip().removeprefix("HEAD -> ").removeprefix("tag: ")
                for d in decorations.split(",")
                if d.strip() and d.strip() != "HEAD"
            ]
            entries.append(HistoryEntry(id=sha, when=when, message=subject, refs=refs))
        return entries

    def diff(
        self,
        locator: Locator,
        a: State,
        b: State,
        *,
        listings: Listings = (None, None),
    ) -> ObjectDiff:
        sha_a, sha_b = str(a["sha"]), str(b["sha"])
        out = ObjectDiff(unit="files")
        if sha_a == sha_b:
            if a.get("dirty") != b.get("dirty"):
                out.note = f"dirty {a.get('dirty')} -> {b.get('dirty')}"
            return out
        # One call for per-file status (with renames), one for line counts.
        sha_a, sha_b = _sha(sha_a), _sha(sha_b)
        status = self._run(
            locator, "diff", "--name-status", "-M", "--end-of-options", sha_a, sha_b
        )
        numstat = self._run(
            locator, "diff", "--numstat", "-M", "--end-of-options", sha_a, sha_b
        )
        lines: dict[str, str] = {}
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                added, deleted, path = parts[0], parts[1], parts[-1]
                lines[path] = f"+{added} -{deleted}"
        for line in status.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            code, path = parts[0], parts[-1]
            if code.startswith("A"):
                change = "added"
            elif code.startswith("D"):
                change = "removed"
            elif code.startswith("R"):
                change = "renamed"
                path = f"{parts[1]} -> {parts[2]}" if len(parts) >= 3 else path
            else:
                change = "modified"
            out.add(path, change, lines.get(parts[-1], ""))
        return out


_HEX = re.compile(r"^[0-9a-f]{4,64}$")


def _guard(value: str, what: str = "ref") -> str:
    """A git positional argument from a manifest, a state, or a plan.

    Values starting with `-` would be read as options; every call also passes
    `--end-of-options`, and this makes the refusal a clear BackendError
    instead of whatever git does with `--output=FILE`.
    """
    if not value or value.startswith("-"):
        raise BackendError(
            f"git {what} {value!r} looks like an option; refused", kind="git"
        )
    return value


def _sha(value: object) -> str:
    """A commit id from a state: hex only, so it can never be an option."""
    text = str(value)
    if not _HEX.match(text):
        raise BackendError(f"not a git commit id: {text!r}", kind="git")
    return text


def _old_value(expected: State) -> str:
    """`expected` as the old value of an `update-ref` compare-and-swap: its
    sha, or all zeros for `ABSENT` (the ref must not exist yet)."""
    return _sha(expected["sha"]) if expected else _ZERO_SHA


def _factory(config: dict) -> GitBackend:
    return GitBackend(config)


register_backend("git", _factory)
