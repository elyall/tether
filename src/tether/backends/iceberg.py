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
    ObjectBackend,
    VerifyReport,
    VerifyStatus,
    register_backend,
)
from tether.errors import BackendError
from tether.handles import Handle, IcebergHandle
from tether.manifest import Locator, Pin, Policy, State, ref_for_pin


class IcebergBackend(ObjectBackend):
    kind = "iceberg"
    capabilities = (
        Capability.FINGERPRINT
        | Capability.ADDRESSABLE
        | Capability.PIN
        | Capability.FORK
        | Capability.RETENTION_BOUND
    )

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}
        self._catalogs: dict[str, Any] = {}

    # -- capability refinement ------------------------------------------ #
    def effective_capabilities(self, locator: Locator, policy: Policy) -> Capability:
        caps = self.capabilities
        if getattr(policy, "pin", "native") == "record":
            caps &= ~Capability.PIN  # record strategy creates no native ref
        return caps

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

    def fingerprint(self, locator: Locator, working_ref: str | None) -> State:
        table = self._table(locator)
        branch = working_ref or self._base_branch(locator)
        snap = table.snapshot_by_name(branch)
        if snap is None:
            raise BackendError(f"no snapshot for ref {branch!r}", kind="iceberg")
        return {
            "snapshot_id": int(snap.snapshot_id),
            "metadata_location": table.metadata_location,
        }

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

    def fork(self, locator: Locator, pin: Pin, name: str) -> str:
        table = self._table(locator)
        ref = self._refs(table).get(pin.ref)
        if ref is None:
            raise BackendError(f"pin {pin.ref} missing", kind="iceberg")
        sid = int(ref.snapshot_id)
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


def _factory(config: dict) -> IcebergBackend:
    return IcebergBackend(config)


register_backend("iceberg", _factory)
