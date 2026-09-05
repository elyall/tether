"""lakeFS backend (Forkable).

lakeFS is git for object storage, so the mapping is one-to-one: a branch is the
working ref, its head commit id is the state, a tag is the pin, and a branch
created from the tag is a fork. Pins are repository-wide (a tag names a commit
of the whole repo); an optional ``prefix`` in the locator only scopes the
handle's URI.

Uncommitted changes in lakeFS's staging area are not part of any commit, so a
dirty branch is reported in the state and refused at pin time -- commit or reset
in lakeFS first.

Authentication follows the lakeFS SDK: ``LAKECTL_*`` environment variables or
``~/.lakectl.yaml``; ``[backends.lakefs]`` in ``tether.toml`` may set ``host``
(and other ``lakefs.Client`` keyword arguments) for non-default endpoints.
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
from tether.handles import Handle, LakeFSHandle
from tether.manifest import Locator, Pin, State, ref_for_pin

MAIN = "main"


class LakeFSBackend(ObjectBackend):
    kind = "lakefs"
    capabilities = (
        Capability.FINGERPRINT
        | Capability.ADDRESSABLE
        | Capability.PIN
        | Capability.FORK
        | Capability.CHEAP_FINGERPRINT
        | Capability.ATOMIC_REF
    )

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}
        self._client: Any = None

    # -- helpers --------------------------------------------------------- #
    def _repo_id(self, locator: Locator) -> str:
        repo = locator.get("repository") or locator.get("repo")
        if not repo:
            raise BackendError("lakefs locator needs 'repository'", kind="lakefs")
        return str(repo)

    def _base_branch(self, locator: Locator) -> str:
        return str(locator.get("branch", MAIN))

    def _prefix(self, locator: Locator) -> str:
        return str(locator.get("prefix") or "").strip("/")

    def _errors(self) -> tuple[type[BaseException], ...]:
        try:
            from lakefs.exceptions import LakeFSException
        except ImportError:  # pragma: no cover - optional dep
            return (Exception,)
        return (LakeFSException,)

    def _repo(self, locator: Locator) -> Any:
        """Return a ``lakefs.Repository``. Tests replace this seam with a fake."""
        try:
            import lakefs
        except ImportError as exc:  # pragma: no cover - optional dep
            raise BackendError(
                "the lakefs extra is required (`pip install tether[lakefs]`)",
                kind="lakefs",
            ) from exc
        client_kwargs = {k: v for k, v in self._config.items() if k != "kind"}
        if client_kwargs and self._client is None:
            self._client = lakefs.Client(**client_kwargs)
        return lakefs.Repository(self._repo_id(locator), client=self._client)

    def _tag_commit(self, repo: Any, ref: str) -> str | None:
        try:
            return str(repo.tag(ref).get_commit().id)
        except self._errors():
            return None

    # -- protocol -------------------------------------------------------- #
    def identity(self, locator: Locator) -> Locator:
        return {"repository": self._repo_id(locator)}

    def fingerprint(self, locator: Locator, working_ref: str | None) -> State:
        branch_name = working_ref or self._base_branch(locator)
        repo = self._repo(locator)
        try:
            branch = repo.branch(branch_name)
            state: State = {"commit_id": str(branch.get_commit().id)}
            if any(True for _ in branch.uncommitted(max_amount=1)):
                state["dirty"] = True
        except self._errors() as exc:
            raise BackendError(
                f"cannot read lakefs branch {branch_name}: {exc}", kind="lakefs"
            ) from exc
        return state

    def pin(self, locator: Locator, state: State, pin_id: str) -> Pin:
        if state.get("dirty"):
            raise BackendError(
                "lakefs branch has uncommitted changes; commit or reset them first",
                kind="lakefs",
            )
        repo = self._repo(locator)
        ref = ref_for_pin(pin_id)
        commit_id = str(state["commit_id"])
        try:
            repo.tag(ref).create(source_ref=commit_id, exist_ok=True)
        except self._errors() as exc:
            raise BackendError(
                f"cannot create tag {ref}: {exc}", kind="lakefs"
            ) from exc
        existing = self._tag_commit(repo, ref)
        if existing != commit_id:
            raise BackendError(
                f"tag {ref} already points at {existing}, not {commit_id}",
                kind="lakefs",
            )
        return Pin(id=pin_id, ref=ref)

    def unpin(self, locator: Locator, pin: Pin) -> None:
        with contextlib.suppress(*self._errors()):
            self._repo(locator).tag(pin.ref).delete()

    def list_pins(self, locator: Locator) -> set[str]:
        prefix = ref_for_pin("")
        repo = self._repo(locator)
        return {
            str(t.id)[len(prefix) :]
            for t in repo.tags(prefix=prefix)
            if str(t.id).startswith(prefix)
        }

    def verify(
        self,
        locator: Locator,
        state: State,
        pin: Pin | None,
        deep: bool,
    ) -> VerifyReport:
        repo = self._repo(locator)
        commit_id = str(state["commit_id"])
        if pin is not None:
            actual = self._tag_commit(repo, pin.ref)
            if actual is None:
                return VerifyReport(VerifyStatus.MISSING, f"tag {pin.ref} missing")
            if actual != commit_id:
                return VerifyReport(
                    VerifyStatus.DRIFTED, f"{pin.ref} -> {actual}, expected {commit_id}"
                )
            return VerifyReport(VerifyStatus.OK)
        if not deep:
            return VerifyReport(VerifyStatus.UNKNOWN, "pass --deep to read the commit")
        try:
            repo.commit(commit_id).get_commit()
        except self._errors() as exc:
            return VerifyReport(VerifyStatus.MISSING, str(exc))
        return VerifyReport(VerifyStatus.OK)

    def fork(self, locator: Locator, pin: Pin, name: str) -> str:
        repo = self._repo(locator)
        target = self._tag_commit(repo, pin.ref)
        if target is None:
            raise BackendError(f"tag {pin.ref} missing; cannot fork", kind="lakefs")
        branch = repo.branch(name)
        try:
            branch.create(source_reference=pin.ref, exist_ok=True)
            if str(branch.get_commit().id) != target:
                # Reset semantics, like icechunk: recreate at the pin.
                branch.delete()
                branch.create(source_reference=pin.ref)
        except self._errors() as exc:
            raise BackendError(
                f"cannot create lakefs branch {name} from {pin.ref}: {exc}",
                kind="lakefs",
            ) from exc
        return name

    def delete_working_ref(self, locator: Locator, ref: str) -> None:
        if ref == self._base_branch(locator) or ref == MAIN:
            return
        with contextlib.suppress(*self._errors()):
            self._repo(locator).branch(ref).delete()

    def _uri(self, locator: Locator, ref: str) -> str:
        prefix = self._prefix(locator)
        base = f"lakefs://{self._repo_id(locator)}/{ref}/"
        return base + (prefix + "/" if prefix else "")

    def open(
        self,
        locator: Locator,
        target: str | Pin | State | None,
        read_only: bool,
    ) -> Handle:
        repo_id = self._repo_id(locator)
        prefix = self._prefix(locator)
        if isinstance(target, Pin):
            commit = self._tag_commit(self._repo(locator), target.ref)
            if commit is None:
                raise BackendError(f"tag {target.ref} missing", kind="lakefs")
            return LakeFSHandle(
                key=repo_id,
                read_only=True,
                uri=self._uri(locator, target.ref),
                repository=repo_id,
                ref=target.ref,
                commit_id=commit,
                prefix=prefix,
            )
        if isinstance(target, dict):
            commit = str(target["commit_id"])
            return LakeFSHandle(
                key=repo_id,
                read_only=True,
                uri=self._uri(locator, commit),
                repository=repo_id,
                ref=commit,
                commit_id=commit,
                prefix=prefix,
            )
        branch = target or self._base_branch(locator)
        return LakeFSHandle(
            key=repo_id,
            read_only=read_only,
            uri=self._uri(locator, branch),
            repository=repo_id,
            ref=branch,
            prefix=prefix,
        )


def _factory(config: dict) -> LakeFSBackend:
    return LakeFSBackend(config)


register_backend("lakefs", _factory)
