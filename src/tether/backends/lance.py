"""Lance backend (Forkable).

Lance datasets have git-like refs: every write creates an immutable version,
``tags`` name a ``(branch, version)``, and ``branches`` are independent version
lines. Tagged versions are exempt from ``cleanup_old_versions()``, so a tag is a
GC-proof pin; a branch created from a tag is a fork.

Two Lance specifics shape the mapping:

* Version numbers are *branch-scoped* and a fresh branch starts at its parent's
  version number, so ``{"branch", "version"}`` is the state (a bare version
  would collide across branches). An untouched fork reports its *parent's*
  address, so a fork's fingerprint equals the pinned state and a no-op commit
  after ``tether new`` stays a no-op.
* Lance refuses to delete a branch that a tag references. A working branch that
  has been pinned therefore outlives the workspace (``delete_working_ref`` is a
  no-op for it) until ``gc`` drops the tag.
"""

from __future__ import annotations

import contextlib
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
from tether.handles import Handle, LanceHandle
from tether.manifest import WORKING_REF_PREFIX, Locator, Pin, State, ref_for_pin

MAIN = "main"
_LANCE_ERRORS: tuple[type[BaseException], ...] = (OSError, ValueError)

Ref = tuple[str | None, int | None]


def _tag_target(meta: dict[str, Any]) -> tuple[str, int]:
    """Normalize tag/branch metadata to ``(branch, version)`` with main explicit."""
    return str(meta.get("branch") or MAIN), int(meta["version"])


class LanceBackend(ObjectBackend):
    kind = "lance"
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

    # -- helpers --------------------------------------------------------- #
    def _uri(self, locator: Locator) -> str:
        uri = locator.get("uri") or locator.get("path")
        if not uri:
            raise BackendError("lance locator needs 'uri'", kind="lance")
        return str(uri)

    def _resolve_at(self, ds: Any, locator: Locator, at: str) -> tuple[str, int]:
        """Resolve an ``at`` value -- a tag name or a version number -- to a ref."""
        tags = ds.tags.list()
        if at in tags:
            return _tag_target(tags[at])
        if at.isdigit():
            return self._base_branch(locator), int(at)
        raise BackendError(
            f"{at!r} is neither a tag nor a version number", kind="lance"
        )

    def _base_branch(self, locator: Locator) -> str:
        return str(locator.get("branch", MAIN))

    base_branch = _base_branch

    def _dataset(self, locator: Locator) -> Any:
        """Open the dataset at its main head (never cached: heads move)."""
        import lance

        options = self._config.get("storage_options") or None
        try:
            return lance.dataset(self._uri(locator), storage_options=options)
        except _LANCE_ERRORS as exc:
            raise BackendError(
                f"cannot open lance dataset {self._uri(locator)}: {exc}", kind="lance"
            ) from exc

    @staticmethod
    def _checkout(ds: Any, ref: str | Ref) -> Any:
        """``ds.checkout_version`` with Lance errors mapped to BackendError."""
        try:
            return ds.checkout_version(ref)
        except _LANCE_ERRORS as exc:
            raise BackendError(
                f"lance ref {ref!r} not found: {exc}", kind="lance"
            ) from exc

    @staticmethod
    def _at_branch(ds: Any, branch: str) -> Any:
        if branch == MAIN:
            return ds
        return LanceBackend._checkout(ds, (branch, None))

    @staticmethod
    def _state_ref(state: State) -> Ref:
        return str(state.get("branch") or MAIN), int(state["version"])

    # -- protocol -------------------------------------------------------- #
    def identity(self, locator: Locator) -> Locator:
        return {"uri": self._uri(locator)}

    def fingerprint(self, locator: Locator, working_ref: str | None) -> State:
        ds = self._dataset(locator)
        if working_ref is None and (at := base_at(locator)) is not None:
            branch, version = self._resolve_at(ds, locator, at)
            self._checkout(ds, (branch, version))  # validate it exists
            return {"branch": branch, "version": version}
        branch = working_ref or self._base_branch(locator)
        version = int(self._at_branch(ds, branch).version)
        if branch != MAIN:
            meta = ds.branches.list().get(branch)
            if meta is not None and int(meta.get("parent_version", -1)) == version:
                # Untouched fork: same content as the parent's version, which is
                # the address tags point at. Report that so forks compare equal.
                return {
                    "branch": str(meta.get("parent_branch") or MAIN),
                    "version": version,
                }
        return {"branch": branch, "version": version}

    def pin(self, locator: Locator, state: State, pin_id: str) -> Pin:
        ds = self._dataset(locator)
        ref = ref_for_pin(pin_id)
        target = self._state_ref(state)
        try:
            ds.tags.create(ref, target)
        except _LANCE_ERRORS:
            # Tag already exists (idempotent commit) -- confirm it matches.
            existing = ds.tags.list().get(ref)
            if existing is None or _tag_target(existing) != target:
                raise BackendError(
                    f"tag {ref} exists and points elsewhere "
                    f"({_tag_target(existing) if existing else 'unknown'} != {target})",
                    kind="lance",
                ) from None
        return Pin(id=pin_id, ref=ref)

    def unpin(self, locator: Locator, pin: Pin) -> None:
        with contextlib.suppress(*_LANCE_ERRORS):
            self._dataset(locator).tags.delete(pin.ref)  # ignore if already gone

    def list_pins(self, locator: Locator) -> set[str]:
        prefix = ref_for_pin("")
        tags = self._dataset(locator).tags.list()
        return {t[len(prefix) :] for t in tags if t.startswith(prefix)}

    def verify(
        self,
        locator: Locator,
        state: State,
        pin: Pin | None,
        deep: bool,
    ) -> VerifyReport:
        ds = self._dataset(locator)
        target = self._state_ref(state)
        if pin is not None:
            meta = ds.tags.list().get(pin.ref)
            if meta is None:
                return VerifyReport(VerifyStatus.MISSING, f"tag {pin.ref} missing")
            actual = _tag_target(meta)
            if actual != target:
                return VerifyReport(
                    VerifyStatus.DRIFTED, f"{pin.ref} -> {actual}, expected {target}"
                )
            if not deep:
                return VerifyReport(VerifyStatus.OK)
            ref: str | Ref = pin.ref
        else:
            if not deep:
                return VerifyReport(VerifyStatus.UNKNOWN, "pass --deep to read version")
            ref = target
        try:
            self._checkout(ds, ref)
        except BackendError as exc:
            return VerifyReport(VerifyStatus.MISSING, str(exc))
        return VerifyReport(VerifyStatus.OK)

    def fork(self, locator: Locator, source: Pin | State, name: str) -> str:
        ds = self._dataset(locator)
        # A tag name, or the recorded (branch, version) for pin-less forks.
        origin: str | Ref = (
            source.ref if isinstance(source, Pin) else self._state_ref(source)
        )
        existing = ds.branches.list()
        target = name
        if name in existing:
            try:
                ds.branches.delete(name)  # reset semantics, like icechunk
            except _LANCE_ERRORS:
                # Tags reference the old working branch; leave it and pick a
                # sibling name rather than fail the fork.
                n = 2
                while f"{name}.{n}" in existing:
                    n += 1
                target = f"{name}.{n}"
        try:
            ds.create_branch(target, origin)
        except _LANCE_ERRORS as exc:
            raise BackendError(
                f"cannot create lance branch {target} from {origin!r}: {exc}",
                kind="lance",
            ) from exc
        return target

    def delete_working_ref(self, locator: Locator, ref: str) -> None:
        if ref == self._base_branch(locator) or ref == MAIN:
            return
        # Refused (and correctly kept) when a tether tag references the branch.
        with contextlib.suppress(*_LANCE_ERRORS):
            self._dataset(locator).branches.delete(ref)

    def list_working_refs(self, locator: Locator) -> list[str]:
        branches = self._dataset(locator).branches.list()
        return sorted(b for b in branches if str(b).startswith(WORKING_REF_PREFIX))

    PROMOTE_HINT = (
        "Lance cannot move a branch head; write the result onto the base branch "
        "(or keep reading the fork) -- there is no branch merge or fast-forward yet"
    )

    def open(
        self,
        locator: Locator,
        target: str | Pin | State | None,
        read_only: bool,
    ) -> Handle:
        uri = self._uri(locator)
        ds = self._dataset(locator)
        if isinstance(target, Pin):
            checked = self._checkout(ds, target.ref)
            return LanceHandle(
                key=uri,
                read_only=True,
                uri=uri,
                dataset=checked,
                version=int(checked.version),
                tag=target.ref,
            )
        if isinstance(target, dict):
            branch, version = self._state_ref(target)
            checked = self._checkout(ds, (branch, version))
            return LanceHandle(
                key=uri,
                read_only=True,
                uri=uri,
                dataset=checked,
                version=int(checked.version),
                branch=branch,
            )
        if target is None and read_only and (at := base_at(locator)) is not None:
            branch, version = self._resolve_at(ds, locator, at)
            checked = self._checkout(ds, (branch, version))
            return LanceHandle(
                key=uri,
                read_only=True,
                uri=uri,
                dataset=checked,
                version=int(checked.version),
                branch=branch,
            )
        branch = target or self._base_branch(locator)
        checked = self._at_branch(ds, branch)
        return LanceHandle(
            key=uri,
            read_only=read_only,
            uri=uri,
            dataset=checked,
            version=int(checked.version),
            branch=branch,
        )

    def history(
        self,
        locator: Locator,
        ref: str | None = None,
        limit: int = 20,
    ) -> list[HistoryEntry]:
        ds = self._dataset(locator)
        branch = ref or self._base_branch(locator)
        head: int | None = None
        if ref is None and (at := base_at(locator)) is not None:
            branch, head = self._resolve_at(ds, locator, at)
        bds = self._at_branch(ds, branch)
        tags_here: dict[int, list[str]] = {}
        for name, meta in ds.tags.list().items():
            tag_branch, tag_version = _tag_target(meta)
            if tag_branch == branch:
                tags_here.setdefault(tag_version, []).append(name)
        entries: list[HistoryEntry] = []
        versions = sorted(bds.versions(), key=lambda v: int(v["version"]), reverse=True)
        for v in versions:
            number = int(v["version"])
            if head is not None and number > head:
                continue
            refs = sorted(tags_here.get(number, []))
            if number == int(bds.version):
                refs.insert(0, branch)
            meta = v.get("metadata") or {}
            message = str(meta.get("message") or "")
            if not message and meta:
                message = ", ".join(f"{k}={val}" for k, val in sorted(meta.items()))
            entries.append(
                HistoryEntry(
                    id=str(number),
                    when=iso_utc(v.get("timestamp")),
                    message=message,
                    refs=refs,
                )
            )
            if len(entries) >= limit:
                break
        return entries

    def diff(
        self,
        locator: Locator,
        a: State,
        b: State,
        *,
        listings: Listings = (None, None),
    ) -> ObjectDiff:
        """Fragment- and schema-level diff from two manifests (no data read)."""
        out = ObjectDiff(unit="fragments")
        ref_a, ref_b = self._state_ref(a), self._state_ref(b)
        if ref_a == ref_b:
            return out
        ds = self._dataset(locator)
        da, db = self._checkout(ds, ref_a), self._checkout(ds, ref_b)
        fa = {int(f.fragment_id): f for f in da.get_fragments()}
        fb = {int(f.fragment_id): f for f in db.get_fragments()}

        def rows(frag: Any) -> tuple[int, bool]:
            meta = frag.metadata
            physical = int(getattr(meta, "physical_rows", 0) or 0)
            deleted = getattr(meta, "deletion_file", None) is not None
            return physical, deleted

        for fid in sorted(set(fa) | set(fb)):
            if fid not in fa:
                physical, _ = rows(fb[fid])
                out.add(f"fragment {fid}", "added", f"+{physical} rows")
            elif fid not in fb:
                physical, _ = rows(fa[fid])
                out.add(f"fragment {fid}", "removed", f"-{physical} rows")
            else:
                ra, rb = rows(fa[fid]), rows(fb[fid])
                if ra != rb:
                    detail = "deletions changed" if ra[1] != rb[1] else "rewritten"
                    out.add(f"fragment {fid}", "modified", detail)
        cols_a, cols_b = set(da.schema.names), set(db.schema.names)
        for name in sorted(cols_b - cols_a):
            out.add(f"column {name}", "added")
        for name in sorted(cols_a - cols_b):
            out.add(f"column {name}", "removed")
        return out


def _factory(config: dict) -> LanceBackend:
    return LanceBackend(config)


register_backend("lance", _factory)
