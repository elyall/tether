"""Delta Lake backend (Addressable, retention-bound).

A Delta table's transaction log gives every commit a monotonically increasing
``version``; ``DeltaTable(uri, version=n)`` time-travels to it. Delta has no
native tags or branches, so tether can only *record* versions, never pin them:
``VACUUM`` (7-day file retention by default) and log cleanup (30 days) bound how
long a recorded version stays readable. The table's metadata id is recorded too,
so a dropped-and-recreated table is reported as drift rather than silently
re-addressed.

Reads are handed back as a ``deltalake.DeltaTable``; writes go through
``deltalake`` directly.
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
from tether.errors import BackendError, CapabilityError
from tether.handles import DeltaHandle, Handle
from tether.manifest import Locator, Pin, State


class DeltaBackend(ObjectBackend):
    kind = "delta"
    capabilities = (
        Capability.FINGERPRINT
        | Capability.ADDRESSABLE
        | Capability.CHEAP_FINGERPRINT
        | Capability.RETENTION_BOUND
    )

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}

    # -- helpers --------------------------------------------------------- #
    def _uri(self, locator: Locator) -> str:
        uri = locator.get("uri") or locator.get("path")
        if not uri:
            raise BackendError("delta locator needs 'uri'", kind="delta")
        return str(uri)

    def _storage_options(self, locator: Locator) -> dict[str, str] | None:
        options = dict(self._config.get("storage_options") or {})
        region = locator.get("region")
        if region:
            options.setdefault("AWS_REGION", str(region))
        return {str(k): str(v) for k, v in options.items()} or None

    def _table(
        self,
        locator: Locator,
        version: int | None = None,
        *,
        without_files: bool = False,
    ) -> Any:
        from deltalake import DeltaTable
        from deltalake.exceptions import DeltaError, TableNotFoundError

        uri = self._uri(locator)
        try:
            return DeltaTable(
                uri,
                version=version,
                storage_options=self._storage_options(locator),
                without_files=without_files,
            )
        except TableNotFoundError as exc:
            raise BackendError(f"delta table not found: {uri}", kind="delta") from exc
        except DeltaError as exc:
            where = f" at version {version}" if version is not None else ""
            raise BackendError(
                f"cannot load delta table {uri}{where}: {exc}", kind="delta"
            ) from exc

    # -- protocol -------------------------------------------------------- #
    def identity(self, locator: Locator) -> Locator:
        return {"uri": self._uri(locator)}

    def fingerprint(self, locator: Locator, working_ref: str | None) -> State:
        # The file list is not needed to read the version; skip loading it.
        dt = self._table(locator, without_files=True)
        return {"version": int(dt.version()), "table_id": str(dt.metadata().id)}

    def pin(self, locator: Locator, state: State, pin_id: str) -> Pin:
        raise CapabilityError("delta has no native tags; versions are recorded only")

    def unpin(self, locator: Locator, pin: Pin) -> None:
        raise CapabilityError("delta has no native tags; versions are recorded only")

    def list_pins(self, locator: Locator) -> set[str]:
        return set()

    def verify(
        self,
        locator: Locator,
        state: State,
        pin: Pin | None,
        deep: bool,
    ) -> VerifyReport:
        version = int(state["version"])
        table_id = state.get("table_id")
        try:
            head = self._table(locator, without_files=True)
        except BackendError as exc:
            return VerifyReport(VerifyStatus.MISSING, str(exc))
        if table_id and str(head.metadata().id) != str(table_id):
            return VerifyReport(
                VerifyStatus.DRIFTED, "table was recreated (metadata id changed)"
            )
        if int(head.version()) < version:
            return VerifyReport(
                VerifyStatus.MISSING, f"table head is v{head.version()} < v{version}"
            )
        if not deep:
            return VerifyReport(
                VerifyStatus.UNKNOWN, "pass --deep to load the version (vacuum check)"
            )
        try:
            self._table(locator, version=version, without_files=True)
        except BackendError as exc:
            return VerifyReport(VerifyStatus.MISSING, str(exc))
        return VerifyReport(VerifyStatus.OK)

    def fork(self, locator: Locator, pin: Pin, name: str) -> str:
        raise CapabilityError("delta cannot fork; write a new table instead")

    def delete_working_ref(self, locator: Locator, ref: str) -> None:
        return None

    def open(
        self,
        locator: Locator,
        target: str | Pin | State | None,
        read_only: bool,
    ) -> Handle:
        version = int(target["version"]) if isinstance(target, dict) else None
        dt = self._table(locator, version=version)
        return DeltaHandle(
            key=self._uri(locator),
            read_only=True,  # tether never writes Delta tables
            uri=self._uri(locator),
            version=int(dt.version()),
            table=dt,
        )


def _factory(config: dict) -> DeltaBackend:
    return DeltaBackend(config)


register_backend("delta", _factory)
