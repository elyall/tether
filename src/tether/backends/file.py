"""File / object-store backend.

Handles plain artifacts on local disk or in S3 (single object or prefix). It is
Observed by default -- fingerprint and drift detection only -- and Addressable
when pointed at an S3 object in a versioning-enabled bucket (the recorded
``version_id`` can be read back later). It never creates or forks refs.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlparse

from tether.backends.base import (
    Capability,
    ObjectBackend,
    VerifyReport,
    VerifyStatus,
    register_backend,
)
from tether.errors import BackendError, CapabilityError
from tether.handles import FileHandle, Handle
from tether.manifest import Locator, Pin, Policy, State


def _parse(uri: str) -> tuple[str, str, str]:
    """Return ``(scheme, bucket_or_root, key_or_path)``."""
    parsed = urlparse(uri)
    if parsed.scheme in ("", "file"):
        return "local", "", parsed.path or uri
    if parsed.scheme == "s3":
        return "s3", parsed.netloc, parsed.path.lstrip("/")
    raise BackendError(f"unsupported file URI scheme: {parsed.scheme!r}", kind="file")


def _digest_pairs(pairs: list[tuple[str, str]]) -> str:
    h = hashlib.blake2b(digest_size=16)
    for name, token in sorted(pairs):
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(token.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


class FileBackend(ObjectBackend):
    kind = "file"
    capabilities = (
        Capability.FINGERPRINT | Capability.ADDRESSABLE | Capability.CHEAP_FINGERPRINT
    )

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}
        self._s3_client = None

    # -- capability refinement ------------------------------------------ #
    def effective_capabilities(self, locator: Locator, policy: Policy) -> Capability:
        base = Capability.FINGERPRINT | Capability.CHEAP_FINGERPRINT
        if getattr(policy, "file", "immutable") == "versioned":
            return base | Capability.ADDRESSABLE
        return base

    # -- helpers --------------------------------------------------------- #
    def _uri(self, locator: Locator) -> str:
        uri = locator.get("uri") or locator.get("path")
        if not uri:
            raise BackendError("file locator needs 'uri'", kind="file")
        return str(uri)

    def _s3(self):
        if self._s3_client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - optional dep
                raise BackendError(
                    "the s3 extra is required for s3:// file objects "
                    "(`pip install tether[s3]`)",
                    kind="file",
                ) from exc
            self._s3_client = boto3.client("s3")
        return self._s3_client

    # -- protocol -------------------------------------------------------- #
    def identity(self, locator: Locator) -> Locator:
        return {"uri": self._uri(locator)}

    def fingerprint(self, locator: Locator, working_ref: str | None) -> State:
        scheme, bucket, path = _parse(self._uri(locator))
        if scheme == "local":
            return self._fingerprint_local(Path(path))
        return self._fingerprint_s3(bucket, path)

    def _fingerprint_local(self, path: Path) -> State:
        if not path.exists():
            raise BackendError(f"path does not exist: {path}", kind="file")
        if path.is_dir():
            pairs: list[tuple[str, str]] = []
            total = 0
            for child in sorted(path.rglob("*")):
                if child.is_file():
                    st = child.stat()
                    rel = child.relative_to(path).as_posix()
                    pairs.append((rel, f"{st.st_size}:{st.st_mtime_ns}"))
                    total += st.st_size
            return {
                "type": "dir",
                "count": len(pairs),
                "size": total,
                "digest": _digest_pairs(pairs),
            }
        st = path.stat()
        return {"type": "file", "size": st.st_size, "mtime_ns": st.st_mtime_ns}

    def _fingerprint_s3(self, bucket: str, key: str) -> State:
        client = self._s3()
        if key.endswith("/") or key == "":
            paginator = client.get_paginator("list_objects_v2")
            pairs: list[tuple[str, str]] = []
            total = 0
            for page in paginator.paginate(Bucket=bucket, Prefix=key):
                for obj in page.get("Contents", []):
                    etag = obj["ETag"].strip('"')
                    pairs.append((obj["Key"], etag))
                    total += obj["Size"]
            return {
                "type": "prefix",
                "count": len(pairs),
                "size": total,
                "digest": _digest_pairs(pairs),
            }
        head = client.head_object(Bucket=bucket, Key=key)
        state: State = {
            "type": "object",
            "size": head["ContentLength"],
            "etag": head["ETag"].strip('"'),
        }
        version_id = head.get("VersionId")
        if version_id and version_id != "null":
            state["version_id"] = version_id
        return state

    def pin(self, locator: Locator, state: State, pin_id: str) -> Pin:
        raise CapabilityError("file backend cannot pin", kind="file")

    def unpin(self, locator: Locator, pin: Pin) -> None:
        raise CapabilityError("file backend cannot pin", kind="file")

    def list_pins(self, locator: Locator) -> set[str]:
        return set()

    def verify(
        self,
        locator: Locator,
        state: State,
        pin: Pin | None,
        deep: bool,
    ) -> VerifyReport:
        version_id = state.get("version_id")
        if version_id:
            if not deep:
                return VerifyReport(
                    VerifyStatus.UNKNOWN, "pass --deep to confirm the version"
                )
            scheme, bucket, key = _parse(self._uri(locator))
            if scheme != "s3":  # pragma: no cover - defensive
                return VerifyReport(VerifyStatus.UNKNOWN)
            try:
                self._s3().head_object(
                    Bucket=bucket, Key=key, VersionId=str(version_id)
                )
                return VerifyReport(VerifyStatus.OK)
            except Exception as exc:
                return VerifyReport(VerifyStatus.MISSING, str(exc))
        # Observed: compare the current fingerprint to the recorded one.
        try:
            current = self.fingerprint(locator, None)
        except BackendError as exc:
            return VerifyReport(VerifyStatus.MISSING, str(exc))
        if current == state:
            return VerifyReport(VerifyStatus.OK)
        return VerifyReport(VerifyStatus.DRIFTED, "content changed since commit")

    def fork(self, locator: Locator, pin: Pin, name: str) -> str:
        raise CapabilityError("file backend cannot fork", kind="file")

    def delete_working_ref(self, locator: Locator, ref: str) -> None:
        return None

    def open(
        self,
        locator: Locator,
        target: str | Pin | State | None,
        read_only: bool,
    ) -> Handle:
        uri = self._uri(locator)
        version_id = None
        if isinstance(target, dict):
            version_id = target.get("version_id")
        return FileHandle(
            key=uri,
            read_only=True,  # tether never writes through file handles
            uri=uri,
            version_id=str(version_id) if version_id else None,
        )


def _factory(config: dict) -> FileBackend:
    return FileBackend(config)


register_backend("file", _factory)
