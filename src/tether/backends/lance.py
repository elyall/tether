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
    ObjectBackend,
    VerifyReport,
    VerifyStatus,
    register_backend,
)
from tether.errors import BackendError
from tether.handles import Handle, LanceHandle
from tether.manifest import Locator, Pin, State, ref_for_pin

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
    )

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}

    # -- helpers --------------------------------------------------------- #
    def _uri(self, locator: Locator) -> str:
        uri = locator.get("uri") or locator.get("path")
        if not uri:
            raise BackendError("lance locator needs 'uri'", kind="lance")
        return str(uri)

    def _base_branch(self, locator: Locator) -> str:
        return str(locator.get("branch", MAIN))

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
        branch = working_ref or self._base_branch(locator)
        ds = self._dataset(locator)
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

    def fork(self, locator: Locator, pin: Pin, name: str) -> str:
        ds = self._dataset(locator)
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
            ds.create_branch(target, pin.ref)
        except _LANCE_ERRORS as exc:
            raise BackendError(
                f"cannot create lance branch {target} from {pin.ref}: {exc}",
                kind="lance",
            ) from exc
        return target

    def delete_working_ref(self, locator: Locator, ref: str) -> None:
        if ref == self._base_branch(locator) or ref == MAIN:
            return
        # Refused (and correctly kept) when a tether tag references the branch.
        with contextlib.suppress(*_LANCE_ERRORS):
            self._dataset(locator).branches.delete(ref)

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


def _factory(config: dict) -> LanceBackend:
    return LanceBackend(config)


register_backend("lance", _factory)
