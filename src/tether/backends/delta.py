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

from types import MappingProxyType
from typing import Any

try:
    from pyarrow import ArrowException as ArrowError
except ImportError:  # pragma: no cover - optional dep
    ArrowError = Exception

from tether.backends.base import (
    Capability,
    HistoryEntry,
    Listings,
    ObjectBackend,
    ObjectDiff,
    VerifyReport,
    VerifyStatus,
    base_at,
    canonical_uri,
    iso_utc,
    register_backend,
    wrap_library_errors,
)
from tether.errors import BackendError, CapabilityError
from tether.handles import DeltaHandle, Handle
from tether.manifest import Locator, Pin, State


@wrap_library_errors
class DeltaBackend(ObjectBackend):
    kind = "delta"
    LOCAL_PATH_KEYS = ("uri",)
    SAFE_CONFIG_KEYS = frozenset({"storage_options"})
    SAFE_OPTION_KEYS = MappingProxyType(
        {
            "storage_options": frozenset(
                {
                    "region",
                    "allow_http",
                    "virtual_hosted_style_request",
                    "conditional_put",
                }
            )
        }
    )
    capabilities = (
        Capability.FINGERPRINT
        | Capability.ADDRESSABLE
        | Capability.CHEAP_FINGERPRINT
        | Capability.RETENTION_BOUND
        | Capability.DIFF
        | Capability.HISTORY
    )

    @staticmethod
    def _library_errors() -> tuple[type[BaseException], ...]:
        from deltalake.exceptions import DeltaError

        return (DeltaError, OSError)

    def validate_locator(self, locator: Locator) -> None:
        at = base_at(locator)
        if at is not None and not at.isdigit():
            raise BackendError(
                f"delta `at` must be a table version number, got {at!r}", kind="delta"
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
        from tether.credentials import storage_options

        options = dict(self._config.get("storage_options") or {})
        region = locator.get("region")
        if region:
            options.setdefault("AWS_REGION", str(region))
        options.update(storage_options(self.secrets_for(locator)))
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

    def _log(self, locator: Locator, hi: int | None, count: int) -> list[dict]:
        """Up to ``count`` commits at or below version ``hi``, newest first.

        ``DeltaTable(version=n).history()`` returns the head's commits but
        numbers them from ``n``, so the log is always read from the head.
        """
        dt = self._table(locator, without_files=True)
        head = int(dt.version())
        if hi is None:
            hi = head
        elif hi > head:
            raise BackendError(
                f"delta table {self._uri(locator)} has no version {hi} "
                f"(head is v{head})",
                kind="delta",
            )
        commits = dt.history(head - hi + count)
        return [c for c in commits if int(c.get("version", -1)) <= hi][:count]

    # -- protocol -------------------------------------------------------- #
    def identity(self, locator: Locator) -> Locator:
        return {"uri": canonical_uri(self._uri(locator))}

    def fingerprint(self, locator: Locator, working_ref: str | None) -> State:
        # The file list is not needed to read the version; skip loading it.
        at = base_at(locator)
        version = int(at) if at is not None and at.isdigit() else None
        if at is not None and version is None:
            raise BackendError(
                f"delta `at` must be a version number, got {at!r}", kind="delta"
            )
        dt = self._table(locator, version=version, without_files=True)
        return {"version": int(dt.version()), "table_id": str(dt.metadata().id)}

    def history(
        self,
        locator: Locator,
        ref: str | None = None,
        limit: int = 20,
    ) -> list[HistoryEntry]:
        start = ref if ref is not None else base_at(locator)
        version = int(start) if start is not None and str(start).isdigit() else None
        entries: list[HistoryEntry] = []
        for commit in self._log(locator, version, limit):
            metrics = commit.get("operationMetrics") or {}
            parts = [
                f"{sign}{metrics[k]} {label}"
                for k, sign, label in (
                    ("num_added_rows", "+", "rows"),
                    ("num_deleted_rows", "-", "rows"),
                    ("num_updated_rows", "~", "rows"),
                )
                if metrics.get(k) not in (None, 0, "0")
            ]
            op = str(commit.get("operation", "COMMIT"))
            entries.append(
                HistoryEntry(
                    id=str(commit.get("version")),
                    when=iso_utc(commit.get("timestamp")),
                    message=f"{op}: {', '.join(parts)}" if parts else op,
                )
            )
        return entries[:limit]

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
            # `without_files` skips the very check --deep is for: a vacuumed
            # version keeps its log but not its data files. Reading every
            # file's footer touches each one.
            table = self._table(locator, version=version)
            table.to_pyarrow_dataset().count_rows()
        except BackendError as exc:
            return VerifyReport(VerifyStatus.MISSING, str(exc))
        except (*self._library_errors(), ValueError, ArrowError) as exc:
            return VerifyReport(
                VerifyStatus.MISSING, f"version {version} cannot be read: {exc}"
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
        if version is None and target is None and (at := base_at(locator)) is not None:
            # A read-only open at the object's registered position honours
            # `at`, as fingerprint does, instead of showing the table's head.
            version = int(at)
        dt = self._table(locator, version=version)
        return DeltaHandle(
            key=self._uri(locator),
            read_only=True,  # tether never writes Delta tables
            uri=self._uri(locator),
            version=int(dt.version()),
            table=dt,
        )

    def diff(
        self,
        locator: Locator,
        a: State,
        b: State,
        *,
        listings: Listings = (None, None),
    ) -> ObjectDiff:
        """One entry per transaction-log commit in ``(a, b]`` with its metrics.

        Reads only the log (no data). Row-level changes would need Change Data
        Feed enabled on the table (``DeltaTable.load_cdf``), which is opt-in.
        """
        va, vb = int(a["version"]), int(b["version"])
        out = ObjectDiff(unit="commits")
        if a.get("table_id") != b.get("table_id"):
            out.note = "table was recreated between the two states"
        if va == vb:
            return out
        lo, hi = min(va, vb), max(va, vb)
        if vb < va:
            out.note = (out.note + "; " if out.note else "") + "b is older than a"
        for commit in reversed(self._log(locator, hi, hi - lo)):
            version = int(commit.get("version", -1))
            if version <= lo:
                continue
            metrics = commit.get("operationMetrics") or {}
            parts = [
                f"{sign}{metrics[k]} {label}"
                for k, sign, label in (
                    ("num_added_rows", "+", "rows"),
                    ("num_deleted_rows", "-", "rows"),
                    ("num_updated_rows", "~", "rows"),
                    ("num_added_files", "+", "files"),
                    ("num_removed_files", "-", "files"),
                )
                if metrics.get(k) not in (None, 0, "0")
            ]
            operation = commit.get("operation", "COMMIT")
            out.add(
                f"v{version}",
                "modified",
                f"{operation}: {', '.join(parts) or 'metadata'}",
            )
        return out


def _factory(config: dict) -> DeltaBackend:
    return DeltaBackend(config)


register_backend("delta", _factory)
