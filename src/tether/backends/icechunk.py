"""Icechunk backend (Forkable).

Maps tether onto an Icechunk repository: a branch is the working ref, a commit's
``snapshot_id`` is the state, an immutable tag is the pin, and a branch created
off a tag is a fork. Icechunk tags are immutable and are excluded from snapshot
expiry, which makes them ideal, GC-proof pins.
"""

from __future__ import annotations

import contextlib
from typing import Any
from urllib.parse import urlparse

from tether.backends.base import (
    Capability,
    HistoryEntry,
    Listings,
    ObjectBackend,
    ObjectDiff,
    VerifyReport,
    VerifyStatus,
    base_at,
    iso_utc,
    register_backend,
)
from tether.errors import BackendError
from tether.handles import Handle, IcechunkHandle
from tether.manifest import WORKING_REF_PREFIX, Locator, Pin, State, ref_for_pin


class IcechunkBackend(ObjectBackend):
    kind = "icechunk"
    capabilities = (
        Capability.FINGERPRINT
        | Capability.ADDRESSABLE
        | Capability.PIN
        | Capability.FORK
        | Capability.ATOMIC_REF
        | Capability.DIFF
        | Capability.HISTORY
    )

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}
        self._repos: dict[str, Any] = {}

    # -- storage / repo -------------------------------------------------- #
    def _uri(self, locator: Locator) -> str:
        uri = locator.get("uri") or locator.get("path")
        if not uri:
            raise BackendError("icechunk locator needs 'uri'", kind="icechunk")
        return str(uri)

    def _storage(self, locator: Locator):
        import icechunk as ic

        uri = self._uri(locator)
        parsed = urlparse(uri)
        if parsed.scheme in ("", "file"):
            return ic.local_filesystem_storage(parsed.path or uri)
        if parsed.scheme == "s3":
            return ic.s3_storage(
                bucket=parsed.netloc,
                prefix=parsed.path.lstrip("/") or None,
                region=locator.get("region"),
                from_env=True,
            )
        raise BackendError(
            f"unsupported icechunk storage scheme: {parsed.scheme!r}",
            kind="icechunk",
        )

    def _repo(self, locator: Locator):
        import icechunk as ic

        uri = self._uri(locator)
        repo = self._repos.get(uri)
        if repo is None:
            repo = ic.Repository.open(self._storage(locator))
            self._repos[uri] = repo
        return repo

    def _base_branch(self, locator: Locator) -> str:
        return str(locator.get("branch", "main"))

    def _resolve(self, repo: Any, ref: str) -> str:
        """Resolve a branch, tag, or snapshot id to a snapshot id."""
        import icechunk as ic

        with contextlib.suppress(ic.IcechunkError):
            return str(repo.lookup_branch(ref))
        with contextlib.suppress(ic.IcechunkError):
            return str(repo.lookup_tag(ref))
        try:
            info = next(iter(repo.ancestry(snapshot_id=ref)))
        except (ic.IcechunkError, StopIteration) as exc:
            raise BackendError(
                f"{ref!r} is not a branch, tag, or snapshot id", kind="icechunk"
            ) from exc
        return str(info.id)

    def _refs_by_snapshot(self, repo: Any) -> dict[str, list[str]]:
        import icechunk as ic

        out: dict[str, list[str]] = {}
        for branch in sorted(repo.list_branches()):
            with contextlib.suppress(ic.IcechunkError):
                out.setdefault(str(repo.lookup_branch(branch)), []).append(branch)
        for tag in sorted(repo.list_tags()):
            with contextlib.suppress(ic.IcechunkError):
                out.setdefault(str(repo.lookup_tag(tag)), []).append(tag)
        return out

    # -- protocol -------------------------------------------------------- #
    def identity(self, locator: Locator) -> Locator:
        return {"uri": self._uri(locator)}

    def fingerprint(self, locator: Locator, working_ref: str | None) -> State:
        repo = self._repo(locator)
        if working_ref is None and (at := base_at(locator)) is not None:
            return {"snapshot_id": self._resolve(repo, at)}
        branch = working_ref or self._base_branch(locator)
        return {"snapshot_id": repo.lookup_branch(branch)}

    def history(
        self,
        locator: Locator,
        ref: str | None = None,
        limit: int = 20,
    ) -> list[HistoryEntry]:
        repo = self._repo(locator)
        start = self._resolve(
            repo, ref or base_at(locator) or self._base_branch(locator)
        )
        refs = self._refs_by_snapshot(repo)
        entries: list[HistoryEntry] = []
        for info in repo.ancestry(snapshot_id=start):
            entries.append(
                HistoryEntry(
                    id=str(info.id),
                    when=iso_utc(info.written_at),
                    message=str(info.message or ""),
                    refs=refs.get(str(info.id), []),
                )
            )
            if len(entries) >= limit:
                break
        return entries

    def pin(self, locator: Locator, state: State, pin_id: str) -> Pin:
        import icechunk as ic

        repo = self._repo(locator)
        ref = ref_for_pin(pin_id)
        sid = str(state["snapshot_id"])
        try:
            repo.create_tag(ref, sid)
        except ic.IcechunkError:
            # Tag already exists (idempotent commit) -- confirm it matches.
            existing = repo.lookup_tag(ref)
            if existing != sid:
                raise BackendError(
                    f"tag {ref} already points at {existing}, not {sid}",
                    kind="icechunk",
                ) from None
        return Pin(id=pin_id, ref=ref)

    def unpin(self, locator: Locator, pin: Pin) -> None:
        import icechunk as ic

        with contextlib.suppress(ic.IcechunkError):
            self._repo(locator).delete_tag(pin.ref)  # ignore if already gone

    def list_pins(self, locator: Locator) -> set[str]:
        prefix = ref_for_pin("")
        tags = self._repo(locator).list_tags()
        return {t[len(prefix) :] for t in tags if t.startswith(prefix)}

    def verify(
        self,
        locator: Locator,
        state: State,
        pin: Pin | None,
        deep: bool,
    ) -> VerifyReport:
        import icechunk as ic

        repo = self._repo(locator)
        sid = str(state["snapshot_id"])
        if pin is not None:
            try:
                actual = repo.lookup_tag(pin.ref)
            except ic.IcechunkError:
                return VerifyReport(VerifyStatus.MISSING, f"tag {pin.ref} missing")
            if actual != sid:
                return VerifyReport(
                    VerifyStatus.DRIFTED, f"{pin.ref} -> {actual}, expected {sid}"
                )
            return VerifyReport(VerifyStatus.OK)
        if not deep:
            return VerifyReport(VerifyStatus.UNKNOWN, "pass --deep to read snapshot")
        try:
            repo.readonly_session(snapshot_id=sid)
            return VerifyReport(VerifyStatus.OK)
        except ic.IcechunkError as exc:
            return VerifyReport(VerifyStatus.MISSING, str(exc))

    def fork(self, locator: Locator, source: Pin | State, name: str) -> str:
        import icechunk as ic

        repo = self._repo(locator)
        if isinstance(source, Pin):
            sid = repo.lookup_tag(source.ref)
        else:
            # Recorded state (no tag): the snapshot must still be reachable.
            sid = self._resolve(repo, str(source["snapshot_id"]))
        try:
            repo.create_branch(name, sid)
        except ic.IcechunkError:
            repo.reset_branch(name, sid)
        return name

    def delete_working_ref(self, locator: Locator, ref: str) -> None:
        import icechunk as ic

        if ref == self._base_branch(locator) or ref == "main":
            return
        with contextlib.suppress(ic.IcechunkError):
            self._repo(locator).delete_branch(ref)

    def list_working_refs(self, locator: Locator) -> list[str]:
        branches = self._repo(locator).list_branches()
        return sorted(b for b in branches if b.startswith(WORKING_REF_PREFIX))

    def open(
        self,
        locator: Locator,
        target: str | Pin | State | None,
        read_only: bool,
    ) -> Handle:
        repo = self._repo(locator)
        if isinstance(target, Pin):
            session = repo.readonly_session(tag=target.ref)
            return IcechunkHandle(
                key=self._uri(locator),
                read_only=True,
                repository=repo,
                session=session,
                tag=target.ref,
                snapshot_id=session.snapshot_id,
            )
        if isinstance(target, dict):
            sid = str(target["snapshot_id"])
            session = repo.readonly_session(snapshot_id=sid)
            return IcechunkHandle(
                key=self._uri(locator),
                read_only=True,
                repository=repo,
                session=session,
                snapshot_id=sid,
            )
        if target is None and read_only and (at := base_at(locator)) is not None:
            sid = self._resolve(repo, at)
            session = repo.readonly_session(snapshot_id=sid)
            return IcechunkHandle(
                key=self._uri(locator),
                read_only=True,
                repository=repo,
                session=session,
                snapshot_id=sid,
            )
        branch = target or self._base_branch(locator)
        if read_only:
            session = repo.readonly_session(branch=branch)
        else:
            session = repo.writable_session(branch)
        return IcechunkHandle(
            key=self._uri(locator),
            read_only=read_only,
            repository=repo,
            session=session,
            branch=branch,
            snapshot_id=session.snapshot_id,
        )

    def diff(
        self,
        locator: Locator,
        a: State,
        b: State,
        *,
        listings: Listings = (None, None),
    ) -> ObjectDiff:
        import icechunk as ic

        sid_a, sid_b = str(a["snapshot_id"]), str(b["snapshot_id"])
        out = ObjectDiff(unit="nodes")
        if sid_a == sid_b:
            return out
        try:
            d = self._repo(locator).diff(from_snapshot_id=sid_a, to_snapshot_id=sid_b)
        except ic.IcechunkError as exc:
            raise BackendError(f"icechunk diff failed: {exc}", kind="icechunk") from exc
        chunks = dict(getattr(d, "updated_chunks", {}) or {})
        for path in sorted(d.new_groups):
            out.add(path, "added", "group")
        for path in sorted(d.new_arrays):
            out.add(path, "added", "array")
        for path in sorted(d.deleted_groups):
            out.add(path, "removed", "group")
        for path in sorted(d.deleted_arrays):
            out.add(path, "removed", "array")
        for path in sorted(d.updated_groups):
            out.add(path, "modified", "group metadata")
        touched = set(d.updated_arrays) | set(chunks)
        for path in sorted(touched):
            parts = []
            if path in d.updated_arrays:
                parts.append("array metadata")
            if path in chunks:
                parts.append(f"{len(chunks[path])} chunks")
            out.add(path, "modified", ", ".join(parts))
        for moved in getattr(d, "moved_nodes", []) or []:
            out.add(f"{moved[0]} -> {moved[1]}", "renamed")
        return out


def _factory(config: dict) -> IcechunkBackend:
    return IcechunkBackend(config)


register_backend("icechunk", _factory)
