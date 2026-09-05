"""Git backend (Forkable); also covers jj colocated repositories.

Lets a dataset commit pin the *code* that produced it. A branch (or detached
sha) is the working ref, the commit ``sha`` is the state, a lightweight tag
``tether.<pin_id>`` is the pin, and a branch off that tag is a fork. When the
locator names a ``remote``, pins are pushed there so they survive beyond a single
clone; otherwise they are durable only locally (documented).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from tether.backends.base import (
    Capability,
    HistoryEntry,
    Listings,
    ObjectBackend,
    ObjectDiff,
    VerifyReport,
    VerifyStatus,
    base_at,
    register_backend,
)
from tether.errors import BackendError
from tether.handles import GitHandle, Handle
from tether.manifest import WORKING_REF_PREFIX, Locator, Pin, State, ref_for_pin


class GitBackend(ObjectBackend):
    kind = "git"
    capabilities = (
        Capability.FINGERPRINT
        | Capability.ADDRESSABLE
        | Capability.PIN
        | Capability.FORK
        | Capability.CHEAP_FINGERPRINT
        | Capability.ATOMIC_REF
        | Capability.DIFF
        | Capability.HISTORY
    )

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}
        configured = self._config.get("git_path")
        self._git: str = configured or shutil.which("git") or "git"

    # -- helpers --------------------------------------------------------- #
    def _path(self, locator: Locator) -> Path:
        path = locator.get("path")
        if not path:
            raise BackendError(
                "git backend needs a local 'path' (url-only clones are not "
                "supported yet)",
                kind="git",
            )
        return Path(str(path))

    def _run(self, locator: Locator, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            [self._git, "-C", str(self._path(locator)), *args],
            capture_output=True,
            text=True,
        )
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
        )
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    # -- protocol -------------------------------------------------------- #
    def identity(self, locator: Locator) -> Locator:
        # Resolve to an absolute path so equal repos dedupe.
        return {"path": str(self._path(locator).resolve())}

    def fingerprint(self, locator: Locator, working_ref: str | None) -> State:
        ref = working_ref or self._base_ref(locator)
        sha = self._run(locator, "rev-parse", "--verify", f"{ref}^{{commit}}")
        dirty = bool(self._run(locator, "status", "--porcelain"))
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
        sha = str(state["sha"])
        existing = self._run(
            locator,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
            check=False,
        )
        if not existing:
            self._run(locator, "tag", ref, sha)
        elif existing != sha:
            raise BackendError(
                f"tag {ref} already points at {existing}, not {sha}", kind="git"
            )
        remote = locator.get("remote")
        if remote:
            self._run(locator, "push", str(remote), f"refs/tags/{ref}", check=False)
        return Pin(id=pin_id, ref=ref)

    def unpin(self, locator: Locator, pin: Pin) -> None:
        self._run(locator, "tag", "-d", pin.ref, check=False)
        remote = locator.get("remote")
        if remote:
            self._run(
                locator,
                "push",
                str(remote),
                "--delete",
                f"refs/tags/{pin.ref}",
                check=False,
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
        sha = str(state["sha"])
        target = pin.ref if pin is not None else sha
        resolved = self._run(
            locator,
            "rev-parse",
            "--verify",
            "--quiet",
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

    def fork(self, locator: Locator, source: Pin | State, name: str) -> str:
        ref = source.ref if isinstance(source, Pin) else str(source["sha"])
        sha = self._run(locator, "rev-parse", "--verify", f"{ref}^{{commit}}")
        exists = self._run(
            locator,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{name}^{{commit}}",
            check=False,
        )
        if exists:
            self._run(locator, "branch", "-f", name, sha)
        else:
            self._run(locator, "branch", name, sha)
        return name

    def delete_working_ref(self, locator: Locator, ref: str) -> None:
        self._run(locator, "branch", "-D", ref, check=False)

    def list_working_refs(self, locator: Locator) -> list[str]:
        out = self._run(
            locator,
            "for-each-ref",
            "--format=%(refname:short)",
            f"refs/heads/{WORKING_REF_PREFIX}*",
        )
        return sorted(line.strip() for line in out.splitlines() if line.strip())

    def open(
        self,
        locator: Locator,
        target: str | Pin | State | None,
        read_only: bool,
    ) -> Handle:
        path = str(self._path(locator).resolve())
        if isinstance(target, Pin):
            sha = self._run(locator, "rev-parse", f"{target.ref}^{{commit}}")
            return GitHandle(key=path, read_only=True, path=path, sha=sha)
        if isinstance(target, dict):
            sha = str(target["sha"])
            return GitHandle(key=path, read_only=True, path=path, sha=sha)
        ref = target or self._base_ref(locator)
        sha = self._run(locator, "rev-parse", f"{ref}^{{commit}}")
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
            start,
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
        status = self._run(locator, "diff", "--name-status", "-M", sha_a, sha_b)
        numstat = self._run(locator, "diff", "--numstat", "-M", sha_a, sha_b)
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


def _factory(config: dict) -> GitBackend:
    return GitBackend(config)


register_backend("git", _factory)
