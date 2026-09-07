"""Apache Iceberg backend (Forkable; phase 3).

A branch is the working ref, a commit's ``snapshot_id`` is the state, and a pin is
either a native Iceberg **tag** (``pin = native``, the default) or just the
recorded snapshot id (``pin = record``). The ``record`` strategy exists for
catalogs such as S3 Tables where creating user refs disables automated
maintenance; recorded snapshots are only recoverable within the table's snapshot
retention window (hence ``RETENTION_BOUND``).
"""

from __future__ import annotations

from typing import Any

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
from tether.handles import Handle, IcebergHandle
from tether.manifest import WORKING_REF_PREFIX, Locator, Pin, State, ref_for_pin


class IcebergBackend(ObjectBackend):
    kind = "iceberg"
    capabilities = (
        Capability.FINGERPRINT
        | Capability.ADDRESSABLE
        | Capability.PIN
        | Capability.FORK
        | Capability.RETENTION_BOUND
        | Capability.DIFF
        | Capability.HISTORY
        | Capability.PROMOTE
    )

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}
        self._catalogs: dict[str, Any] = {}

    # -- capability refinement ------------------------------------------ #
    # -- catalog / table ------------------------------------------------- #
    def _catalog(self, locator: Locator):
        from pyiceberg.catalog import load_catalog

        props = dict(locator.get("catalog") or self._config.get("catalog") or {})
        name = str(locator.get("catalog_name", props.pop("name", "default")))
        cache_key = f"{name}:{sorted(props.items())}"
        cat = self._catalogs.get(cache_key)
        if cat is None:
            cat = load_catalog(name, **props)
            self._catalogs[cache_key] = cat
        return cat

    def _identifier(self, locator: Locator) -> str:
        ident = locator.get("identifier") or locator.get("table")
        if not ident:
            raise BackendError("iceberg locator needs 'identifier'", kind="iceberg")
        return str(ident)

    def _table(self, locator: Locator):
        return self._catalog(locator).load_table(self._identifier(locator))

    def _base_branch(self, locator: Locator) -> str:
        return str(locator.get("branch", "main"))

    @staticmethod
    def _refs(table) -> dict:
        refs = table.refs
        return refs() if callable(refs) else refs

    # -- protocol -------------------------------------------------------- #
    def identity(self, locator: Locator) -> Locator:
        return {
            "identifier": self._identifier(locator),
            "branch": self._base_branch(locator),
            "catalog_name": str(locator.get("catalog_name", "default")),
        }

    def _resolve(self, table: Any, ref: str) -> int:
        """Resolve a branch/tag name or a snapshot id to a snapshot id."""
        snap = table.snapshot_by_name(ref)
        if snap is not None:
            return int(snap.snapshot_id)
        if ref.isdigit():
            wanted = int(ref)
            for s in table.snapshots():
                if int(s.snapshot_id) == wanted:
                    return wanted
        raise BackendError(f"no snapshot for ref {ref!r}", kind="iceberg")

    def fingerprint(self, locator: Locator, working_ref: str | None) -> State:
        table = self._table(locator)
        ref = working_ref or base_at(locator) or self._base_branch(locator)
        return {
            "snapshot_id": self._resolve(table, ref),
            "metadata_location": table.metadata_location,
        }

    def history(
        self,
        locator: Locator,
        ref: str | None = None,
        limit: int = 20,
    ) -> list[HistoryEntry]:
        table = self._table(locator)
        start = self._resolve(
            table, ref or base_at(locator) or self._base_branch(locator)
        )
        by_id = {int(s.snapshot_id): s for s in table.snapshots()}
        pointing: dict[int, list[str]] = {}
        for name, r in self._refs(table).items():
            pointing.setdefault(int(r.snapshot_id), []).append(name)
        entries: list[HistoryEntry] = []
        cursor = by_id.get(start)
        while cursor is not None and len(entries) < limit:
            sid = int(cursor.snapshot_id)
            summary = _summary(cursor)
            op = summary.get("operation", "")
            counts = ", ".join(
                f"{sign}{summary[k]} {label}"
                for k, sign, label in (
                    ("added-records", "+", "rows"),
                    ("deleted-records", "-", "rows"),
                )
                if summary.get(k) not in (None, "0")
            )
            entries.append(
                HistoryEntry(
                    id=str(sid),
                    when=iso_utc(getattr(cursor, "timestamp_ms", None)),
                    message=f"{op}: {counts}" if counts else op,
                    refs=sorted(pointing.get(sid, [])),
                )
            )
            parent = getattr(cursor, "parent_snapshot_id", None)
            cursor = by_id.get(int(parent)) if parent is not None else None
        return entries

    def pin(self, locator: Locator, state: State, pin_id: str) -> Pin:
        table = self._table(locator)
        ref = ref_for_pin(pin_id)
        sid = int(state["snapshot_id"])
        existing = self._refs(table).get(ref)
        if existing is not None:
            if int(existing.snapshot_id) != sid:
                raise BackendError(
                    f"tag {ref} already points at {existing.snapshot_id}",
                    kind="iceberg",
                )
            return Pin(id=pin_id, ref=ref)
        with table.manage_snapshots() as ms:
            ms.create_tag(snapshot_id=sid, tag_name=ref)
        return Pin(id=pin_id, ref=ref)

    def unpin(self, locator: Locator, pin: Pin) -> None:
        table = self._table(locator)
        if pin.ref not in self._refs(table):
            return
        with table.manage_snapshots() as ms:
            ms.remove_tag(pin.ref)

    def list_pins(self, locator: Locator) -> set[str]:
        from pyiceberg.table.refs import SnapshotRefType

        prefix = ref_for_pin("")
        table = self._table(locator)
        out: set[str] = set()
        for name, ref in self._refs(table).items():
            if name.startswith(prefix) and ref.snapshot_ref_type == SnapshotRefType.TAG:
                out.add(name[len(prefix) :])
        return out

    def verify(
        self,
        locator: Locator,
        state: State,
        pin: Pin | None,
        deep: bool,
    ) -> VerifyReport:
        table = self._table(locator)
        sid = int(state["snapshot_id"])
        if pin is not None:
            ref = self._refs(table).get(pin.ref)
            if ref is None:
                return VerifyReport(VerifyStatus.MISSING, f"tag {pin.ref} missing")
            if int(ref.snapshot_id) != sid:
                return VerifyReport(
                    VerifyStatus.DRIFTED,
                    f"{pin.ref} -> {ref.snapshot_id}, expected {sid}",
                )
            return VerifyReport(VerifyStatus.OK)
        # record strategy: the snapshot must still be within retention.
        if any(int(s.snapshot_id) == sid for s in table.snapshots()):
            return VerifyReport(VerifyStatus.OK)
        return VerifyReport(VerifyStatus.MISSING, f"snapshot {sid} expired/absent")

    def fork(self, locator: Locator, source: Pin | State, name: str) -> str:
        table = self._table(locator)
        if isinstance(source, Pin):
            ref = self._refs(table).get(source.ref)
            if ref is None:
                raise BackendError(f"pin {source.ref} missing", kind="iceberg")
            sid = int(ref.snapshot_id)
        else:
            # Recorded state (no tag): the snapshot must not have been expired.
            sid = self._resolve(table, str(source["snapshot_id"]))
        if name in self._refs(table):
            return name
        with table.manage_snapshots() as ms:
            ms.create_branch(snapshot_id=sid, branch_name=name)
        return name

    def delete_working_ref(self, locator: Locator, ref: str) -> None:
        table = self._table(locator)
        if ref == self._base_branch(locator) or ref == "main":
            return
        if ref not in self._refs(table):
            return
        with table.manage_snapshots() as ms:
            ms.remove_branch(ref)

    def list_working_refs(self, locator: Locator) -> list[str]:
        from pyiceberg.table.refs import SnapshotRefType

        return sorted(
            name
            for name, ref in self._refs(self._table(locator)).items()
            if name.startswith(WORKING_REF_PREFIX)
            and ref.snapshot_ref_type == SnapshotRefType.BRANCH
        )

    # -- promote --------------------------------------------------------- #
    PROMOTE_HINT = (
        "pyiceberg has no branch merge; use your engine's fast_forward / "
        "cherrypick_snapshot procedure, or re-apply the writes on a fresh fork"
    )

    def _source_sid(self, table: Any, source: str | Pin | State) -> int:
        if isinstance(source, Pin):
            ref = self._refs(table).get(source.ref)
            if ref is None:
                raise BackendError(f"pin {source.ref} missing", kind="iceberg")
            return int(ref.snapshot_id)
        if isinstance(source, dict):
            return int(source["snapshot_id"])
        return self._resolve(table, source)

    def ancestor_of(
        self, locator: Locator, ancestor: State, descendant: str | Pin | State
    ) -> bool | None:
        table = self._table(locator)
        wanted = int(ancestor["snapshot_id"])
        sid: int | None = self._source_sid(table, descendant)
        seen: set[int] = set()
        while sid is not None and sid not in seen:
            if sid == wanted:
                return True
            seen.add(sid)
            snap = table.snapshot_by_id(sid)
            sid = (
                int(snap.parent_snapshot_id)
                if snap and snap.parent_snapshot_id
                else None
            )
        return False

    def promote(self, locator: Locator, source: str | Pin | State) -> State:
        table = self._table(locator)
        base = self._base_branch(locator)
        head = self._resolve(table, base)
        target = self._source_sid(table, source)
        if target == head:
            return self.fingerprint(locator, base)
        if not self.ancestor_of(
            locator, {"snapshot_id": head}, {"snapshot_id": target}
        ):
            raise BackendError(
                f"{base} moved to snapshot {head}, which is not an ancestor of "
                f"{target}; {self.PROMOTE_HINT}",
                kind="iceberg",
            )
        with table.manage_snapshots() as ms:
            if base == "main":
                ms.set_current_snapshot(snapshot_id=target)
            else:
                ms.create_branch(
                    snapshot_id=target, branch_name=base
                )  # set-snapshot-ref
        return self.fingerprint(locator, base)

    def open(
        self,
        locator: Locator,
        target: str | Pin | State | None,
        read_only: bool,
    ) -> Handle:
        table = self._table(locator)
        if isinstance(target, Pin):
            ref = self._refs(table).get(target.ref)
            return IcebergHandle(
                key=self._identifier(locator),
                read_only=True,
                table=table,
                ref=target.ref,
                snapshot_id=int(ref.snapshot_id) if ref else None,
            )
        if isinstance(target, dict):
            return IcebergHandle(
                key=self._identifier(locator),
                read_only=True,
                table=table,
                snapshot_id=int(target["snapshot_id"]),
            )
        branch = target or self._base_branch(locator)
        return IcebergHandle(
            key=self._identifier(locator),
            read_only=read_only,
            table=table,
            ref=str(branch),
        )

    def diff(
        self,
        locator: Locator,
        a: State,
        b: State,
        *,
        listings: Listings = (None, None),
    ) -> ObjectDiff:
        """Per-snapshot summaries along ``b``'s ancestry back to ``a``.

        Iceberg snapshot metadata carries the operation and record/file counts
        for every commit, so no data is read. When ``a`` is not an ancestor of
        ``b`` (diverged branches) the totals at each end are compared instead.
        """
        sid_a, sid_b = int(a["snapshot_id"]), int(b["snapshot_id"])
        out = ObjectDiff(unit="snapshots")
        if sid_a == sid_b:
            return out
        table = self._table(locator)
        by_id = {int(s.snapshot_id): s for s in table.snapshots()}
        chain = []
        cursor = by_id.get(sid_b)
        while cursor is not None and int(cursor.snapshot_id) != sid_a:
            chain.append(cursor)
            parent = getattr(cursor, "parent_snapshot_id", None)
            cursor = by_id.get(int(parent)) if parent is not None else None
        if cursor is None:  # a is not an ancestor of b (or expired)
            out.note = f"{sid_a} is not an ancestor of {sid_b}; comparing totals"
            ta = _summary(by_id.get(sid_a))
            tb = _summary(by_id.get(sid_b))
            for key in ("total-records", "total-data-files", "total-delete-files"):
                if ta.get(key) != tb.get(key):
                    out.add(
                        key, "modified", f"{ta.get(key, '?')} -> {tb.get(key, '?')}"
                    )
            return out
        for snap in reversed(chain):
            summary = _summary(snap)
            op = summary.get("operation", "commit")
            parts = [
                f"{sign}{summary[k]} {label}"
                for k, sign, label in (
                    ("added-records", "+", "rows"),
                    ("deleted-records", "-", "rows"),
                    ("added-data-files", "+", "files"),
                    ("deleted-data-files", "-", "files"),
                )
                if summary.get(k) not in (None, "0")
            ]
            out.add(
                str(snap.snapshot_id),
                "modified",
                f"{op}: {', '.join(parts) or 'metadata'}",
            )
        return out


def _summary(snapshot: Any) -> dict[str, str]:
    if snapshot is None or getattr(snapshot, "summary", None) is None:
        return {}
    summary = snapshot.summary
    props = dict(getattr(summary, "additional_properties", {}) or {})
    op = getattr(summary, "operation", None)
    if op is not None:
        props["operation"] = str(getattr(op, "value", op))
    return {str(k): str(v) for k, v in props.items()}


def _factory(config: dict) -> IcebergBackend:
    return IcebergBackend(config)


register_backend("iceberg", _factory)
