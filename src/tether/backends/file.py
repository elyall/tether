"""File / object-store backend.

Handles plain artifacts on local disk or in an object store -- S3, GCS, or Azure
Blob, as a single object or a prefix -- through `obstore`_ (one Rust client for
all three clouds). It is Observed by default -- fingerprint and drift detection
only -- and Addressable when pointed at a single object in a versioning-enabled
bucket/container with ``--file versioned`` (the recorded ``version_id`` -- an S3
version id, GCS generation, or Azure version id -- can be read back later). It
never creates or forks refs.

Remote fingerprints are metadata-only -- one ``HEAD`` for an object, one paged
``LIST`` for a prefix (etag + size per object, no per-object round-trips) -- and
the etag is the store's own content hash. Local fingerprints are content hashes
too (sha256 per file), so ``touch``, ``cp``, a fresh checkout, or an rsync do
not read as changes: a stat cache under the workspace's ``.tether/cache/``
(``size``, ``mtime_ns``, inode -> hash) means only files whose stat changed are
re-read, the way git and DVC avoid rehashing.

A directory/prefix state is only a digest, so on its own it cannot be diffed.
The backend therefore hands the engine a per-file *listing* (JSON lines, sorted
by path) to store content-addressed in VCS at commit time; ``diff`` compares two
stored listings file by file.

.. _obstore: https://developmentseed.org/obstore/
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tether.backends.base import (
    Capability,
    Listings,
    ObjectBackend,
    ObjectDiff,
    VerifyReport,
    VerifyStatus,
    register_backend,
)
from tether.errors import BackendError, CapabilityError
from tether.handles import FileHandle, Handle
from tether.manifest import Locator, Pin, Policy, State

# URL schemes obstore understands, normalized to the store they select.
_REMOTE_SCHEMES = frozenset(
    {"s3", "s3a", "gs", "gcs", "az", "azure", "abfs", "abfss", "adl", "http", "https"}
)


def _parse(uri: str) -> tuple[str, str, str]:
    """Return ``(scheme, store_root_url, key_or_path)``.

    ``scheme`` is ``"local"`` for filesystem paths; otherwise the URL scheme. The
    store root is the URL without its path, so keys are always full object keys.
    """
    parsed = urlparse(uri)
    if parsed.scheme in ("", "file"):
        return "local", "", parsed.path or uri
    if parsed.scheme in _REMOTE_SCHEMES:
        root = f"{parsed.scheme}://{parsed.netloc}"
        return parsed.scheme, root, parsed.path.lstrip("/")
    raise BackendError(f"unsupported file URI scheme: {parsed.scheme!r}", kind="file")


def _digest_pairs(pairs: list[tuple[str, str]]) -> str:
    h = hashlib.blake2b(digest_size=16)
    for name, token in sorted(pairs):
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(token.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _strip_etag(etag: object) -> str:
    return str(etag or "").strip('"')


# A listing row: path -> (token, size). ``token`` is what "same content" means
# for the entry: the sha256 locally, the etag remotely.
ListingRows = dict[str, tuple[str, int]]

_HASH_CHUNK = 4 * 1024 * 1024
_HASH_WORKERS = 8


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb", buffering=0) as fh:
        while chunk := fh.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


class _HashCache:
    """Path -> (size, mtime_ns, inode, sha256), persisted as JSON when given a file.

    A hit requires all three stat fields to match; a miss hashes the file and
    records it. Without a path (a backend built outside a dataset) the cache
    lives for the process only.
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._entries: dict[str, list[Any]] | None = None
        self._dirty = False
        self.hashed = 0  # files read since construction (tests, diagnostics)

    def _load(self) -> dict[str, list[Any]]:
        if self._entries is None:
            self._entries = {}
            if self._path is not None and self._path.is_file():
                try:
                    self._entries = json.loads(self._path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    self._entries = {}
        return self._entries

    def lookup(self, path: Path, st: os.stat_result) -> str | None:
        hit = self._load().get(str(path))
        if (
            hit
            and hit[0] == st.st_size
            and hit[1] == st.st_mtime_ns
            and hit[2] == st.st_ino
        ):
            return str(hit[3])
        return None

    def record(self, path: Path, st: os.stat_result, digest: str) -> None:
        self._load()[str(path)] = [st.st_size, st.st_mtime_ns, st.st_ino, digest]
        self._dirty = True

    def hashes(self, files: list[tuple[Path, os.stat_result]]) -> dict[Path, str]:
        """Content hash per file, reading only the ones the cache cannot answer."""
        out: dict[Path, str] = {}
        misses: list[tuple[Path, os.stat_result]] = []
        for path, st in files:
            known = self.lookup(path, st)
            if known is None:
                misses.append((path, st))
            else:
                out[path] = known
        if misses:
            self.hashed += len(misses)
            if len(misses) == 1:
                path, st = misses[0]
                digest = _hash_file(path)
                self.record(path, st, digest)
                out[path] = digest
            else:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=_HASH_WORKERS) as pool:
                    digests = list(pool.map(lambda m: _hash_file(m[0]), misses))
                for (path, st), digest in zip(misses, digests, strict=True):
                    self.record(path, st, digest)
                    out[path] = digest
        self.save()
        return out

    def save(self) -> None:
        if not self._dirty or self._path is None or self._entries is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f".{self._path.name}.tmp")
        tmp.write_text(
            json.dumps(self._entries, separators=(",", ":")), encoding="utf-8"
        )
        tmp.replace(self._path)
        self._dirty = False


def _dump_listing(rows: ListingRows) -> str:
    return "".join(
        json.dumps({"p": path, "k": token, "s": size}, separators=(",", ":")) + "\n"
        for path, (token, size) in sorted(rows.items())
    )


def _load_listing(text: str) -> ListingRows:
    rows: ListingRows = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        rows[str(row["p"])] = (str(row.get("k", "")), int(row.get("s", 0)))
    return rows


def _human(n: int) -> str:
    sign = "-" if n < 0 else "+"
    value = float(abs(n))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{sign}{value:.0f} {unit}"
            return f"{sign}{value:.1f} {unit}"
        value /= 1024
    return f"{sign}{value}"  # pragma: no cover


def _walk_files(root: Path) -> list[tuple[str, int, int]]:
    """Return ``(relative posix path, size, mtime_ns)`` for every file under root.

    ``os.scandir`` reuses the directory entry's cached type information and is
    ~7x cheaper per entry than ``Path.rglob`` + ``stat``. Symlinked directories
    are not followed (matching ``rglob``); symlinked files are stat'ed through.
    """
    return [(rel, st.st_size, st.st_mtime_ns) for rel, st in _walk_stats(root)]


def _walk_stats(root: Path) -> list[tuple[str, os.stat_result]]:
    out: list[tuple[str, os.stat_result]] = []
    stack = [root]
    while stack:
        current = stack.pop()
        with os.scandir(current) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file():
                    rel = Path(entry.path).relative_to(root).as_posix()
                    out.append((rel, entry.stat()))
    return out


class FileBackend(ObjectBackend):
    kind = "file"
    capabilities = (
        Capability.FINGERPRINT
        | Capability.ADDRESSABLE
        | Capability.CHEAP_FINGERPRINT
        | Capability.DIFF
    )
    _LISTING_CACHE = 16  # recent directory/prefix listings kept, by digest

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}
        self._stores: dict[str, Any] = {}
        self._listings: OrderedDict[str, ListingRows] = OrderedDict()
        self._hashes = _HashCache(None)

    def configure_cache(self, cache_dir: Path) -> None:
        self._hashes = _HashCache(cache_dir / "file-hashes.json")

    # -- capability refinement ------------------------------------------ #
    def effective_capabilities(self, locator: Locator, policy: Policy) -> Capability:
        base = Capability.FINGERPRINT | Capability.CHEAP_FINGERPRINT | Capability.DIFF
        if getattr(policy, "file", "immutable") == "versioned":
            return base | Capability.ADDRESSABLE
        return base

    def _remember(self, digest: str, rows: ListingRows) -> None:
        self._listings[digest] = rows
        self._listings.move_to_end(digest)
        while len(self._listings) > self._LISTING_CACHE:
            self._listings.popitem(last=False)

    # -- helpers --------------------------------------------------------- #
    def _uri(self, locator: Locator) -> str:
        uri = locator.get("uri") or locator.get("path")
        if not uri:
            raise BackendError("file locator needs 'uri'", kind="file")
        return str(uri)

    def _obstore(self):
        try:
            import obstore
        except ImportError as exc:  # pragma: no cover - optional dep
            raise BackendError(
                "the objectstore extra is required for remote file objects "
                "(`pip install tether-vcs[objectstore]`)",
                kind="file",
            ) from exc
        return obstore

    def _open_store(self, root: str, locator: Locator) -> Any:
        """Build an obstore store for ``root`` (e.g. ``s3://bucket``).

        Credentials come from the environment / instance metadata, never from
        manifests. ``[backends.file] storage_options`` in ``tether.toml`` is
        passed through verbatim (region, endpoint, account name, ...); a locator
        ``region`` overrides it. Tests replace this seam with an in-memory store.
        """
        from obstore.store import from_url

        options: dict[str, Any] = dict(self._config.get("storage_options") or {})
        region = locator.get("region")
        if region:
            options["region"] = str(region)
        return from_url(root, **options)

    def _store(self, root: str, locator: Locator) -> Any:
        store = self._stores.get(root)
        if store is None:
            self._obstore()  # surface a clear error before touching the store
            store = self._open_store(root, locator)
            self._stores[root] = store
        return store

    # -- protocol -------------------------------------------------------- #
    def identity(self, locator: Locator) -> Locator:
        return {"uri": self._uri(locator)}

    def fingerprint(self, locator: Locator, working_ref: str | None) -> State:
        if locator.get("at"):
            raise BackendError(
                "file objects have no history to detach from (`at`); "
                "use `--file versioned` on a versioned bucket instead",
                kind="file",
            )
        scheme, root, path = _parse(self._uri(locator))
        if scheme == "local":
            return self._fingerprint_local(Path(path))
        return self._fingerprint_remote(self._store(root, locator), path)

    def _fingerprint_local(self, path: Path) -> State:
        if not path.exists():
            raise BackendError(f"path does not exist: {path}", kind="file")
        if path.is_dir():
            entries = _walk_stats(path)
            hashes = self._hashes.hashes([(path / rel, st) for rel, st in entries])
            rows: ListingRows = {
                rel: (hashes[path / rel], st.st_size) for rel, st in entries
            }
            digest = _digest_pairs([(p, tok) for p, (tok, _) in rows.items()])
            self._remember(digest, rows)
            return {
                "type": "dir",
                "count": len(entries),
                "size": sum(st.st_size for _, st in entries),
                "digest": digest,
            }
        st = path.stat()
        digest = self._hashes.hashes([(path, st)])[path]
        return {"type": "file", "size": st.st_size, "sha256": digest}

    def _fingerprint_remote(self, store: Any, key: str) -> State:
        obs = self._obstore()
        if key.endswith("/") or key == "":
            rows: ListingRows = {}
            total = 0
            for page in obs.list(store, prefix=key or None):
                for meta in page:
                    size = int(meta["size"])
                    rows[str(meta["path"])] = (_strip_etag(meta.get("e_tag")), size)
                    total += size
            digest = _digest_pairs([(p, tok) for p, (tok, _) in rows.items()])
            self._remember(digest, rows)
            return {
                "type": "prefix",
                "count": len(rows),
                "size": total,
                "digest": digest,
            }
        try:
            meta = obs.head(store, key)
        except FileNotFoundError as exc:
            raise BackendError(f"object does not exist: {key}", kind="file") from exc
        state: State = {
            "type": "object",
            "size": int(meta["size"]),
            "etag": _strip_etag(meta.get("e_tag")),
        }
        version = meta.get("version")
        if version and version != "null":
            state["version_id"] = str(version)
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
            scheme, root, key = _parse(self._uri(locator))
            if scheme == "local":  # pragma: no cover - defensive
                return VerifyReport(VerifyStatus.UNKNOWN)
            obs = self._obstore()
            try:
                result = obs.get(
                    self._store(root, locator),
                    key,
                    options={"version": str(version_id), "head": True},
                )
            except FileNotFoundError as exc:
                return VerifyReport(VerifyStatus.MISSING, str(exc))
            except Exception as exc:  # obstore.exceptions.BaseError and friends
                return VerifyReport(VerifyStatus.UNKNOWN, str(exc))
            etag = _strip_etag(result.meta.get("e_tag"))
            if state.get("etag") and etag and etag != state["etag"]:
                return VerifyReport(
                    VerifyStatus.DRIFTED, f"version {version_id} has etag {etag}"
                )
            return VerifyReport(VerifyStatus.OK)
        # Observed: compare the current fingerprint to the recorded one.
        try:
            current = self.fingerprint(locator, None)
        except BackendError as exc:
            return VerifyReport(VerifyStatus.MISSING, str(exc))
        if current == state:
            return VerifyReport(VerifyStatus.OK)
        return VerifyReport(VerifyStatus.DRIFTED, "content changed since commit")

    def fork(self, locator: Locator, source: Pin | State, name: str) -> str:
        raise CapabilityError("file backend cannot fork", kind="file")

    def delete_working_ref(self, locator: Locator, ref: str) -> None:
        return None

    # -- listings / diff ------------------------------------------------- #
    def listing(self, locator: Locator, state: State) -> str | None:
        if state.get("type") not in ("dir", "prefix"):
            return None  # a single object's state is already fully descriptive
        digest = str(state.get("digest", ""))
        rows = self._listings.get(digest)
        if rows is None:
            # Not cached (different process / evicted): re-read and use it only
            # if the object still has exactly the recorded state.
            current = self.fingerprint(locator, None)
            if current.get("digest") != digest:
                return None
            rows = self._listings[digest]
        return _dump_listing(rows)

    def diff(
        self,
        locator: Locator,
        a: State,
        b: State,
        *,
        listings: Listings = (None, None),
    ) -> ObjectDiff:
        if a.get("type") in ("dir", "prefix") or b.get("type") in ("dir", "prefix"):
            return self._diff_listings(a, b, listings)
        out = ObjectDiff(unit="objects")
        if a == b:
            return out
        details: list[str] = []
        for field_name, label in (
            ("size", "size"),
            ("etag", "etag"),
            ("sha256", "sha256"),
            ("version_id", "version"),
        ):
            va, vb = a.get(field_name), b.get(field_name)
            if va != vb:
                if field_name == "size" and va is not None and vb is not None:
                    shown = _human(int(vb) - int(va))
                elif field_name == "sha256":
                    shown = f"{str(va)[:12]} -> {str(vb)[:12]}"
                else:
                    shown = f"{va} -> {vb}"
                details.append(f"{label} {shown}")
        out.add(str(self._uri(locator)), "modified", ", ".join(details))
        return out

    def _diff_listings(self, a: State, b: State, listings: Listings) -> ObjectDiff:
        out = ObjectDiff(unit="files")
        text_a, text_b = listings
        if a.get("digest") == b.get("digest"):
            return out
        if text_a is None or text_b is None:
            missing = [s for s, t in (("a", text_a), ("b", text_b)) if t is None]
            out.note = f"no stored listing for side {' and '.join(missing)}"
            count_a, count_b = int(a.get("count", 0)), int(b.get("count", 0))
            size_a, size_b = int(a.get("size", 0)), int(b.get("size", 0))
            out.add(
                "(summary)",
                "modified",
                f"{count_a} -> {count_b} files, {_human(size_b - size_a)}",
            )
            return out
        rows_a, rows_b = _load_listing(text_a), _load_listing(text_b)
        for path in sorted(set(rows_a) | set(rows_b)):
            ra, rb = rows_a.get(path), rows_b.get(path)
            if ra is None and rb is not None:
                out.add(path, "added", _human(rb[1]))
            elif rb is None and ra is not None:
                out.add(path, "removed", _human(-ra[1]))
            elif ra != rb and ra is not None and rb is not None:
                out.add(path, "modified", _human(rb[1] - ra[1]))
        return out

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
