"""Icechunk backend (Forkable).

Maps tether onto an Icechunk repository: a branch is the working ref, a commit's
``snapshot_id`` is the state, an immutable tag is the pin, and a branch created
off a tag is a fork. Icechunk tags are immutable and are excluded from snapshot
expiry, which makes them ideal, GC-proof pins.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator
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
        | Capability.PROMOTE
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

    base_branch = _base_branch

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

    # Icechunk keeps a tombstone for every deleted tag and never lets the name
    # be reused. A pin id is content-addressed, so the same state would always
    # ask for the same name; when that name is burnt, the pin lives on under a
    # *generation*: `tether.<id>.2`, `.3`, ... The pin id is unchanged (gc and
    # manifests key on it); only the ref differs, and every reader resolves a
    # pin through `_live_tag` rather than trusting `pin.ref` alone.
    _MAX_GENERATIONS = 1000

    @staticmethod
    def _generations(pin_id: str) -> Iterator[str]:
        base = ref_for_pin(pin_id)
        yield base
        for n in range(2, IcechunkBackend._MAX_GENERATIONS + 1):
            yield f"{base}.{n}"

    @staticmethod
    def _pin_id_of(tag: str) -> str | None:
        """The pin id a `tether.*` tag belongs to, generation suffix dropped."""
        prefix = ref_for_pin("")
        if not tag.startswith(prefix):
            return None
        rest = tag[len(prefix) :]
        parts = rest.split(".")
        if len(parts) == 3 and parts[2].isdigit():
            rest = f"{parts[0]}.{parts[1]}"
        return rest

    def _live_tag(self, repo: Any, pin: Pin) -> tuple[str, str] | None:
        """`(tag, snapshot_id)` for the tag that carries `pin` now, or `None`.

        The manifest's `pin.ref` first; failing that, the newest generation of
        the same id that exists (a `repair` after someone deleted the tag).
        """
        import icechunk as ic

        with contextlib.suppress(ic.IcechunkError):
            return pin.ref, str(repo.lookup_tag(pin.ref))
        wanted = self._pin_id_of(pin.ref) or pin.id
        live = [
            t for t in repo.list_tags() if self._pin_id_of(t) == wanted and t != pin.ref
        ]
        for tag in sorted(live, key=_generation_number, reverse=True):
            with contextlib.suppress(ic.IcechunkError):
                return tag, str(repo.lookup_tag(tag))
        return None

    def pin(self, locator: Locator, state: State, pin_id: str) -> Pin:
        import icechunk as ic

        repo = self._repo(locator)
        sid = str(state["snapshot_id"])
        for ref in self._generations(pin_id):
            try:
                repo.create_tag(ref, sid)
                return Pin(id=pin_id, ref=ref)
            except ic.IcechunkError:
                pass
            try:
                existing = repo.lookup_tag(ref)
            except ic.IcechunkError:
                continue  # this name is a tombstone; try the next generation
            if existing == sid:
                return Pin(id=pin_id, ref=ref)  # idempotent commit
            # The name is taken by another snapshot: our state and the id's
            # earlier life disagree. Do not step over it silently.
            raise BackendError(
                f"tag {ref} already points at {existing}, not {sid}",
                kind="icechunk",
            )
        raise BackendError(
            f"pin {pin_id}: every tag name up to generation "
            f"{self._MAX_GENERATIONS} has been used and deleted",
            kind="icechunk",
        )

    def unpin(self, locator: Locator, pin: Pin) -> None:
        import icechunk as ic

        repo = self._repo(locator)
        # Every generation of this id: the plan may know only `tether.<id>`
        # while the live tag is a later one.
        wanted = self._pin_id_of(pin.ref) or pin.id
        for tag in {pin.ref, *repo.list_tags()}:
            if tag == pin.ref or self._pin_id_of(tag) == wanted:
                with contextlib.suppress(ic.IcechunkError):
                    repo.delete_tag(tag)  # ignore if already gone

    def list_pins(self, locator: Locator) -> set[str]:
        ids = (self._pin_id_of(t) for t in self._repo(locator).list_tags())
        return {i for i in ids if i is not None}

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
            live = self._live_tag(repo, pin)
            if live is None:
                return VerifyReport(VerifyStatus.MISSING, f"tag {pin.ref} missing")
            tag, actual = live
            if actual != sid:
                return VerifyReport(
                    VerifyStatus.DRIFTED, f"{tag} -> {actual}, expected {sid}"
                )
            if tag != pin.ref:
                return VerifyReport(VerifyStatus.OK, f"pinned as {tag}")
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
            sid = self._pin_sid(repo, source)
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

    PROMOTE_HINT = (
        "Icechunk has no merge; re-apply the writes on a fresh fork of the base "
        "branch, or reset the base with repo.reset_branch() if losing its newer "
        "snapshots is intended"
    )

    def _pin_sid(self, repo: Any, pin: Pin) -> str:
        live = self._live_tag(repo, pin)
        if live is None:
            raise BackendError(f"icechunk tag {pin.ref} not found", kind="icechunk")
        return live[1]

    def _source_sid(self, repo: Any, source: str | Pin | State) -> str:
        if isinstance(source, Pin):
            return self._pin_sid(repo, source)
        if isinstance(source, dict):
            return self._resolve(repo, str(source["snapshot_id"]))
        return self._resolve(repo, source)

    def ancestor_of(
        self, locator: Locator, ancestor: State, descendant: str | Pin | State
    ) -> bool | None:
        repo = self._repo(locator)
        target = self._source_sid(repo, descendant)
        wanted = str(ancestor["snapshot_id"])
        return any(str(info.id) == wanted for info in repo.ancestry(snapshot_id=target))

    def promote(self, locator: Locator, source: str | Pin | State) -> State:
        repo = self._repo(locator)
        base = self._base_branch(locator)
        head = str(repo.lookup_branch(base))
        target = self._source_sid(repo, source)
        if target == head:
            return {"snapshot_id": head}
        if not self.ancestor_of(locator, {"snapshot_id": head}, target):
            raise BackendError(
                f"{base} moved to {head}, which is not an ancestor of {target}; "
                f"{self.PROMOTE_HINT}",
                kind="icechunk",
            )
        repo.reset_branch(base, target)
        return {"snapshot_id": target}

    def open(
        self,
        locator: Locator,
        target: str | Pin | State | None,
        read_only: bool,
    ) -> Handle:

        repo = self._repo(locator)
        if isinstance(target, Pin):
            live = self._live_tag(repo, target)
            if live is None:
                raise BackendError(
                    f"icechunk tag {target.ref} not found", kind="icechunk"
                )
            tag, _sid = live
            session = repo.readonly_session(tag=tag)
            return IcechunkHandle(
                key=self._uri(locator),
                read_only=True,
                repository=repo,
                session=session,
                tag=tag,
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
        repo = self._repo(locator)
        try:
            d = repo.diff(from_snapshot_id=sid_a, to_snapshot_id=sid_b)
        except ic.IcechunkError as exc:
            # Icechunk only diffs along one line of history. Two branches'
            # heads share an ancestor: report what either side changed since it.
            base = self._common_ancestor(repo, sid_a, sid_b)
            if base is None:
                raise BackendError(
                    f"icechunk diff failed: {exc}", kind="icechunk"
                ) from exc
            return self._divergent_diff(repo, base, sid_a, sid_b)
        _collect(out, _entries(d))
        return out

    @staticmethod
    def _common_ancestor(repo: Any, sid_a: str, sid_b: str) -> str | None:
        """Newest snapshot in both histories, or `None` if they share none."""
        in_b = {str(info.id) for info in repo.ancestry(snapshot_id=sid_b)}
        for info in repo.ancestry(snapshot_id=sid_a):
            if str(info.id) in in_b:
                return str(info.id)
        return None

    @staticmethod
    def _divergent_diff(repo: Any, base: str, sid_a: str, sid_b: str) -> ObjectDiff:
        """`a -> b` for snapshots on different branches, via their common base.

        A node only `b` touched keeps `b`'s change; one only `a` touched is
        reported inverted (what `a` added is absent in `b`); one both touched
        is `modified`, with both sides' chunk counts summed.
        """
        out = ObjectDiff(unit="nodes")
        side_a = _entries(repo.diff(from_snapshot_id=base, to_snapshot_id=sid_a))
        side_b = _entries(repo.diff(from_snapshot_id=base, to_snapshot_id=sid_b))
        inverted = {"added": "removed", "removed": "added"}
        merged: dict[str, tuple[str, str]] = {}
        for path in set(side_a) | set(side_b):
            if path not in side_a:
                merged[path] = side_b[path]
            elif path not in side_b:
                change, detail = side_a[path]
                merged[path] = (inverted.get(change, change), detail)
            else:
                (change_a, detail_a), (change_b, detail_b) = side_a[path], side_b[path]
                if change_a == "removed" and change_b == "removed":
                    continue  # gone on both sides
                if change_b == "removed":
                    merged[path] = ("removed", detail_b)  # `a` still has it
                elif change_a == "removed":
                    merged[path] = ("added", detail_b)  # only `b` has it
                else:
                    merged[path] = ("modified", _join_details(detail_a, detail_b))
        _collect(out, merged)
        out.note = f"diverged at snapshot {base}; changes on either side"
        return out


def _generation_number(tag: str) -> int:
    """`tether.<id>` is generation 1; `tether.<id>.N` is N."""
    parts = tag.split(".")
    return int(parts[-1]) if len(parts) == 4 and parts[-1].isdigit() else 1


def _entries(d: Any) -> dict[str, tuple[str, str]]:
    """Flatten an `icechunk.Diff` into `path -> (change, detail)`."""
    chunks = dict(getattr(d, "updated_chunks", {}) or {})
    entries: dict[str, tuple[str, str]] = {}
    for path in d.new_groups:
        entries[path] = ("added", "group")
    for path in d.new_arrays:
        entries[path] = ("added", "array")
    for path in d.deleted_groups:
        entries[path] = ("removed", "group")
    for path in d.deleted_arrays:
        entries[path] = ("removed", "array")
    for path in d.updated_groups:
        entries[path] = ("modified", "group metadata")
    for path in set(d.updated_arrays) | set(chunks):
        if path in entries:  # a new array's chunks: it is added, not modified
            continue
        parts = []
        if path in d.updated_arrays:
            parts.append("array metadata")
        if path in chunks:
            parts.append(f"{len(chunks[path])} chunks")
        entries[path] = ("modified", ", ".join(parts))
    for moved in getattr(d, "moved_nodes", []) or []:
        entries[f"{moved[0]} -> {moved[1]}"] = ("renamed", "")
    return entries


def _collect(out: ObjectDiff, entries: dict[str, tuple[str, str]]) -> None:
    order = {"added": 0, "removed": 1, "modified": 2, "renamed": 3}
    for path, (change, detail) in sorted(
        entries.items(), key=lambda kv: (order.get(kv[1][0], 9), kv[0])
    ):
        out.add(path, change, detail)


def _join_details(a: str, b: str) -> str:
    """Sum `N chunks` across two sides; keep any other wording once."""
    total = 0
    words: list[str] = []
    for detail in (a, b):
        for part in filter(None, (p.strip() for p in detail.split(","))):
            m = re.fullmatch(r"(\d+) chunks", part)
            if m:
                total += int(m.group(1))
            elif part not in words:
                words.append(part)
    if total:
        words.append(f"{total} chunks")
    return ", ".join(words)


def _factory(config: dict) -> IcechunkBackend:
    return IcechunkBackend(config)


register_backend("icechunk", _factory)
