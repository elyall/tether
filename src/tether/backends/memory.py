"""In-memory reference backend.

Implements the full :class:`~tether.backends.base.ObjectBackend` protocol at the
Forkable tier against a simple in-process store. It is the executable spec for
adapters: the conformance suite runs against it, and it makes the engine testable
without any external system.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
from tether.errors import BackendError, MergeConflict
from tether.handles import Handle, MemoryHandle
from tether.manifest import WORKING_REF_PREFIX, Locator, Pin, State, ref_for_pin


@dataclass
class _System:
    snapshots: dict[str, dict] = field(default_factory=dict)
    branches: dict[str, str] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    parents: dict[str, list[str]] = field(default_factory=dict)
    counter: int = 0


class MemoryStore:
    """A tiny versioned key-value store: snapshots, branches, tags."""

    def __init__(self) -> None:
        self.systems: dict[str, _System] = {}

    def system(self, name: str) -> _System:
        sys = self.systems.get(name)
        if sys is None:
            sys = _System()
            sid = f"{name}:s0"
            sys.snapshots[sid] = {}
            sys.branches["main"] = sid
            self.systems[name] = sys
        return sys

    def resolve(self, name: str, ref: str) -> str:
        sys = self.system(name)
        if ref in sys.branches:
            return sys.branches[ref]
        if ref in sys.tags:
            return sys.tags[ref]
        if ref in sys.snapshots:
            return ref
        raise BackendError(f"unknown ref {ref!r}", kind="memory")

    def read(self, name: str, ref: str) -> dict:
        return dict(self.system(name).snapshots[self.resolve(name, ref)])

    def write(
        self,
        name: str,
        branch: str,
        payload: dict,
        *,
        extra_parents: list[str] | None = None,
    ) -> str:
        sys = self.system(name)
        sys.counter += 1
        sid = f"{name}:s{sys.counter}"
        sys.snapshots[sid] = dict(payload)
        parents = []
        if branch in sys.branches:
            parents.append(sys.branches[branch])
        parents.extend(extra_parents or [])
        sys.parents[sid] = parents
        sys.branches[branch] = sid
        return sid

    def ancestors(self, name: str, sid: str) -> list[str]:
        """``sid`` and everything reachable through parent links (nearest first)."""
        sys = self.system(name)
        seen: list[str] = []
        queue = [sid]
        while queue:
            cur = queue.pop(0)
            if cur in seen:
                continue
            seen.append(cur)
            queue.extend(sys.parents.get(cur, []))
        return seen


class MemoryBackend(ObjectBackend):
    kind = "memory"
    capabilities = (
        Capability.FINGERPRINT
        | Capability.ADDRESSABLE
        | Capability.PIN
        | Capability.FORK
        | Capability.CHEAP_FINGERPRINT
        | Capability.ATOMIC_REF
        | Capability.DIFF
        | Capability.HISTORY
        | Capability.PROMOTE
        | Capability.MERGE
    )

    def __init__(self, store: MemoryStore | None = None) -> None:
        self.store = store or MemoryStore()

    # -- helpers --------------------------------------------------------- #
    def _system(self, locator: Locator) -> str:
        return str(locator["system"])

    def _base_branch(self, locator: Locator) -> str:
        return str(locator.get("branch", "main"))

    base_branch = _base_branch

    # -- protocol -------------------------------------------------------- #
    def identity(self, locator: Locator) -> Locator:
        return {"system": self._system(locator)}

    def fingerprint(self, locator: Locator, working_ref: str | None) -> State:
        name = self._system(locator)
        ref = working_ref or base_at(locator) or self._base_branch(locator)
        return {"snapshot_id": self.store.resolve(name, ref)}

    def history(
        self,
        locator: Locator,
        ref: str | None = None,
        limit: int = 20,
    ) -> list[HistoryEntry]:
        name = self._system(locator)
        sys = self.store.system(name)
        head = self.store.resolve(
            name, ref or base_at(locator) or self._base_branch(locator)
        )
        # Snapshots are created in order; everything up to the head is its past.
        ids = list(sys.snapshots)
        upto = ids[: ids.index(head) + 1]
        pointing: dict[str, list[str]] = {}
        for branch, sid in sys.branches.items():
            pointing.setdefault(sid, []).append(branch)
        for tag, sid in sys.tags.items():
            pointing.setdefault(sid, []).append(tag)
        return [
            HistoryEntry(
                id=sid, message=f"snapshot {sid}", refs=sorted(pointing.get(sid, []))
            )
            for sid in reversed(upto)
        ][:limit]

    def pin(self, locator: Locator, state: State, pin_id: str) -> Pin:
        name = self._system(locator)
        sys = self.store.system(name)
        ref = ref_for_pin(pin_id)
        sid = str(state["snapshot_id"])
        if sid not in sys.snapshots:
            raise BackendError(
                f"snapshot {sid} no longer exists", key=name, kind="memory"
            )
        existing = sys.tags.get(ref)
        if existing is not None and existing != sid:
            raise BackendError(
                f"pin {ref} already points elsewhere", key=name, kind="memory"
            )
        sys.tags[ref] = sid
        return Pin(id=pin_id, ref=ref)

    def unpin(self, locator: Locator, pin: Pin) -> None:
        self.store.system(self._system(locator)).tags.pop(pin.ref, None)

    def list_pins(self, locator: Locator) -> set[str]:
        sys = self.store.system(self._system(locator))
        prefix = ref_for_pin("")
        return {t[len(prefix) :] for t in sys.tags if t.startswith(prefix)}

    def verify(
        self,
        locator: Locator,
        state: State,
        pin: Pin | None,
        deep: bool,
    ) -> VerifyReport:
        name = self._system(locator)
        sys = self.store.system(name)
        sid = str(state["snapshot_id"])
        if pin is not None:
            actual = sys.tags.get(pin.ref)
            if actual is None:
                return VerifyReport(VerifyStatus.MISSING, f"pin {pin.ref} gone")
            if actual != sid:
                return VerifyReport(
                    VerifyStatus.DRIFTED, f"{pin.ref} -> {actual}, expected {sid}"
                )
            return VerifyReport(VerifyStatus.OK)
        # Addressable path: snapshot must still exist.
        if sid in sys.snapshots:
            return VerifyReport(VerifyStatus.OK)
        return VerifyReport(VerifyStatus.MISSING, f"snapshot {sid} gone")

    def fork(self, locator: Locator, source: Pin | State, name: str) -> str:
        system = self._system(locator)
        sys = self.store.system(system)
        if isinstance(source, Pin):
            if source.ref not in sys.tags:
                raise BackendError(
                    f"pin {source.ref} missing", key=system, kind="memory"
                )
            sid = sys.tags[source.ref]
        else:
            sid = str(source["snapshot_id"])
            if sid not in sys.snapshots:
                raise BackendError(f"snapshot {sid} gone", key=system, kind="memory")
        sys.branches[name] = sid
        return name

    def delete_working_ref(self, locator: Locator, ref: str) -> None:
        sys = self.store.system(self._system(locator))
        if ref != "main":
            sys.branches.pop(ref, None)

    def list_working_refs(self, locator: Locator) -> list[str]:
        sys = self.store.system(self._system(locator))
        return sorted(b for b in sys.branches if b.startswith(WORKING_REF_PREFIX))

    def _source_sid(self, name: str, source: str | Pin | State) -> str:
        sys = self.store.system(name)
        if isinstance(source, Pin):
            sid = sys.tags.get(source.ref)
            if sid is None:
                raise BackendError(f"pin {source.ref} missing", key=name, kind="memory")
            return sid
        if isinstance(source, dict):
            return str(source["snapshot_id"])
        return self.store.resolve(name, source)

    def ancestor_of(
        self, locator: Locator, ancestor: State, descendant: str | Pin | State
    ) -> bool | None:
        name = self._system(locator)
        target = self._source_sid(name, descendant)
        return str(ancestor["snapshot_id"]) in self.store.ancestors(name, target)

    def promote(self, locator: Locator, source: str | Pin | State) -> State:
        name = self._system(locator)
        sys = self.store.system(name)
        base = self._base_branch(locator)
        head = sys.branches[base]
        target = self._source_sid(name, source)
        if target == head:
            return {"snapshot_id": head}
        if head not in self.store.ancestors(name, target):
            raise BackendError(
                f"{base} moved to {head}, which is not an ancestor of {target}",
                key=name,
                kind="memory",
            )
        sys.branches[base] = target
        return {"snapshot_id": target}

    def merge(self, locator: Locator, source_ref: str, message: str) -> State:
        name = self._system(locator)
        sys = self.store.system(name)
        base = self._base_branch(locator)
        head = sys.branches[base]
        src = self.store.resolve(name, source_ref)
        if src == head or src in self.store.ancestors(name, head):
            return {"snapshot_id": head}
        if head in self.store.ancestors(name, src):
            sys.branches[base] = src  # fast-forward
            return {"snapshot_id": src}
        src_line = self.store.ancestors(name, src)
        common = next(
            (a for a in self.store.ancestors(name, head) if a in src_line), None
        )
        anc = sys.snapshots.get(common, {}) if common else {}
        ours, theirs = sys.snapshots[head], sys.snapshots[src]
        merged = dict(ours)
        conflicts: list[str] = []
        for k in sorted(set(ours) | set(theirs) | set(anc)):
            o, t, a = ours.get(k), theirs.get(k), anc.get(k)
            if o == t:
                continue
            if o == a:  # only they changed it
                if k in theirs:
                    merged[k] = t
                else:
                    merged.pop(k, None)
            elif t != a:  # both changed it differently
                conflicts.append(k)
        if conflicts:
            raise MergeConflict(
                f"{len(conflicts)} key(s) changed on both {base} and {source_ref}",
                conflicts=conflicts,
                key=name,
                kind="memory",
            )
        sid = self.store.write(name, base, merged, extra_parents=[src])
        return {"snapshot_id": sid}

    def open(
        self,
        locator: Locator,
        target: str | Pin | State | None,
        read_only: bool,
    ) -> Handle:
        name = self._system(locator)
        if isinstance(target, Pin):
            resolved = target.ref
        elif isinstance(target, dict):
            resolved = str(target["snapshot_id"])
        elif target is None:
            resolved = self._base_branch(locator)
        else:
            resolved = target
        return MemoryHandle(
            key=name,
            read_only=read_only,
            store=self.store,
            system=name,
            ref=resolved,
        )

    def diff(
        self,
        locator: Locator,
        a: State,
        b: State,
        *,
        listings: Listings = (None, None),
    ) -> ObjectDiff:
        name = self._system(locator)
        pa = self.store.read(name, str(a["snapshot_id"]))
        pb = self.store.read(name, str(b["snapshot_id"]))
        out = ObjectDiff(unit="keys")
        for key in sorted(set(pa) | set(pb)):
            if key not in pa:
                out.add(key, "added", repr(pb[key]))
            elif key not in pb:
                out.add(key, "removed", repr(pa[key]))
            elif pa[key] != pb[key]:
                out.add(key, "modified", f"{pa[key]!r} -> {pb[key]!r}")
        return out


# Process-global store so backends built independently by the engine (and by
# tests) share one in-memory system. Real backends carry no such global.
_GLOBAL_STORE = MemoryStore()


def default_store() -> MemoryStore:
    return _GLOBAL_STORE


def _factory(config: dict) -> MemoryBackend:
    store = config.get("store") if config else None
    return MemoryBackend(store=store or _GLOBAL_STORE)


register_backend("memory", _factory)
