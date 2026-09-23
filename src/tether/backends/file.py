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
import re
import threading
from collections import OrderedDict
from collections.abc import Collection, Mapping
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlparse

from tether.backends.base import (
    Capability,
    Listings,
    ObjectBackend,
    ObjectDiff,
    VerifyReport,
    VerifyStatus,
    canonical_uri,
    local_path,
    register_backend,
    wrap_library_errors,
)
from tether.errors import BackendError, CapabilityError, ConfigError
from tether.handles import FileHandle, Handle
from tether.manifest import Locator, Pin, Policy, State

# URL schemes obstore understands, normalized to the store they select.
_REMOTE_SCHEMES = frozenset(
    {"s3", "s3a", "gs", "gcs", "az", "azure", "abfs", "abfss", "adl", "http", "https"}
)

# obstore's `ClientConfig`: passed as `client_options`, not as store config.
_CLIENT_BOOL_KEYS = frozenset(
    {
        "allow_http",
        "allow_invalid_certificates",
        "http1_only",
        "http2_keep_alive_while_idle",
        "http2_only",
        "randomize_addresses",
    }
)
_CLIENT_DURATION_KEYS = frozenset(
    {
        "connect_timeout",
        "http2_keep_alive_interval",
        "http2_keep_alive_timeout",
        "pool_idle_timeout",
        "read_timeout",
        "timeout",
    }
)
_CLIENT_OPTION_KEYS = (
    _CLIENT_BOOL_KEYS
    | _CLIENT_DURATION_KEYS
    | frozenset(
        {
            "default_content_type",
            "default_headers",
            "pool_max_idle_per_host",
            "proxy_url",
            "proxy_ca_certificate",
            "proxy_excludes",
            "root_certificate",
            "user_agent",
        }
    )
)
_STORE_PREFIXES = ("aws_", "google_", "azure_")
"""How store config spells a key (`AWS_ALLOW_HTTP`); obstore parses a client
key given that way as a store key and panics."""

_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})


def _client_key(key: str) -> str | None:
    """The `ClientConfig` key `key` names, in any case or store prefix."""
    name = key.lower()
    for prefix in _STORE_PREFIXES:
        if name.startswith(prefix) and name[len(prefix) :] in _CLIENT_OPTION_KEYS:
            return name[len(prefix) :]
    return name if name in _CLIENT_OPTION_KEYS else None


def _client_value(key: str, value: Any) -> Any:
    """`value` as obstore's `ClientConfig` takes it for `key`: booleans (from
    `"true"` or `1` too), durations as strings (a number is seconds: `5` ->
    `"5000ms"`), counts and text as strings.

    Raises:
        ConfigError: A value that cannot mean what `key` needs.
    """
    if key in _CLIENT_BOOL_KEYS:
        text = str(value).strip().lower()
        if isinstance(value, bool) or text in _TRUE | _FALSE:
            return value if isinstance(value, bool) else text in _TRUE
    elif key in _CLIENT_DURATION_KEYS:
        if isinstance(value, timedelta):
            value = value.total_seconds()
        if isinstance(value, str) and re.fullmatch(r"\s*\d+(\.\d+)?\s*", value):
            value = float(value)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return f"{round(value * 1000)}ms"
        if isinstance(value, str) and value.strip():
            return value
    elif key == "default_headers":
        if isinstance(value, Mapping):
            return {str(k): str(v) for k, v in value.items()}
    elif key == "root_certificate":
        if isinstance(value, str | bytes):
            return value
    elif key == "proxy_excludes" and isinstance(value, list | tuple):
        return ",".join(str(v) for v in value)
    elif not isinstance(value, bool | Mapping | list | tuple):
        return str(value)
    raise ConfigError(f"file client option {key}: cannot use {value!r}")


def _parse(uri: str) -> tuple[str, str, str]:
    """Return ``(scheme, store_root_url, key_or_path)``.

    ``scheme`` is ``"local"`` for filesystem paths; otherwise the URL scheme. The
    store root is the URL without its path, so keys are always full object keys.
    """
    path = local_path(uri)
    if path is not None:
        return "local", "", path
    parsed = urlparse(uri)
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


_RACY_WINDOW_NS = 2_000_000_000
"""A write that lands in the same timestamp tick as the read can leave size,
mtime and ctime unchanged (git's "racily clean" case). Ticks are coarser than
the nanoseconds they are reported in -- a whole second on some filesystems,
milliseconds on Linux before multigrain timestamps, up to two seconds on
NFS or FAT -- so an entry hashed this close to the file's last change is not
trusted next time."""


def _racy(st: os.stat_result, hashed_at_ns: int) -> bool:
    """Whether a cache entry made at `hashed_at_ns` could hide a later write."""
    changed_ns = max(st.st_mtime_ns, st.st_ctime_ns)
    return hashed_at_ns - changed_ns < _RACY_WINDOW_NS


class _HashCache:
    """Path -> (size, mtime_ns, ctime_ns, inode, sha256), persisted as JSON.

    A hit requires every stat field to match; a miss hashes the file and
    records it. ``ctime`` is in the key because a process that overwrites a
    file and restores its mtime (a sync tool, `touch -r`) cannot restore the
    inode change time; and an entry hashed within a couple of seconds of the
    file's last change is re-read next time (see :func:`_racy`). Without a
    path (a backend built outside a dataset)
    the cache lives for the process only.

    One cache serves every `file` object of a dataset, and the engine
    fingerprints objects concurrently, so the table and its file are guarded
    by a lock; only the hashing itself runs in parallel.
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._entries: dict[str, list[Any]] | None = None
        self._dirty = False
        self._lock = threading.RLock()
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
            and len(hit) == 6
            and hit[0] == st.st_size
            and hit[1] == st.st_mtime_ns
            and hit[2] == st.st_ctime_ns
            and hit[3] == st.st_ino
            and not _racy(st, int(hit[5]))
        ):
            return str(hit[4])
        return None

    def record(self, path: Path, st: os.stat_result, digest: str) -> None:
        import time

        self._load()[str(path)] = [
            st.st_size,
            st.st_mtime_ns,
            st.st_ctime_ns,
            st.st_ino,
            digest,
            time.time_ns(),
        ]
        self._dirty = True

    def hashes(self, files: list[tuple[Path, os.stat_result]]) -> dict[Path, str]:
        """Content hash per file, reading only the ones the cache cannot answer."""
        out: dict[Path, str] = {}
        misses: list[tuple[Path, os.stat_result]] = []
        with self._lock:
            for path, st in files:
                known = self.lookup(path, st)
                if known is None:
                    misses.append((path, st))
                else:
                    out[path] = known
        if misses:
            if len(misses) == 1:
                digests = [_hash_file(misses[0][0])]
            else:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=_HASH_WORKERS) as pool:
                    digests = list(pool.map(lambda m: _hash_file(m[0]), misses))
            with self._lock:
                self.hashed += len(misses)
                for (path, st), digest in zip(misses, digests, strict=True):
                    self.record(path, st, digest)
                    out[path] = digest
        self.save()
        return out

    def save(self) -> None:
        with self._lock:
            if not self._dirty or self._path is None or self._entries is None:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
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
    Other symlinks are not files; see :func:`_walk`.
    """
    return [(rel, st.st_size, st.st_mtime_ns) for rel, st in _walk_stats(root)]


OWNER_MARKER = ".tether-owner"
"""File at the root of a directory tether *created* (`create`), naming the
owning dataset. Not content: the directory walk skips it, so a created
directory fingerprints as empty until something is written into it."""


def _walk_stats(root: Path) -> list[tuple[str, os.stat_result]]:
    return _walk(root)[0]


def _walk(
    root: Path,
) -> tuple[list[tuple[str, os.stat_result]], list[tuple[str, str]]]:
    """Files under root (symlinked files stat'ed through), and every other
    symlink -- to a directory, which is not followed, or dangling -- with its
    target, which is what such an entry holds (as git records a symlink)."""
    files: list[tuple[str, os.stat_result]] = []
    links: list[tuple[str, str]] = []
    stack = [root]
    while stack:
        current = stack.pop()
        with os.scandir(current) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                rel = Path(entry.path).relative_to(root).as_posix()
                if entry.is_file():
                    if rel != OWNER_MARKER:
                        files.append((rel, entry.stat()))
                elif entry.is_symlink():
                    links.append((rel, os.readlink(entry.path)))
    return files, links


def _link_token(target: str) -> str:
    return "symlink:" + hashlib.sha256(os.fsencode(target)).hexdigest()


@wrap_library_errors
class FileBackend(ObjectBackend):
    kind = "file"
    LOCAL_PATH_KEYS = ("uri", "path")
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
        | Capability.DIFF
        | Capability.CREATE
    )
    _LISTING_CACHE = 16  # recent directory/prefix listings kept, by digest

    @staticmethod
    def _library_errors() -> tuple[type[BaseException], ...]:
        try:
            from obstore.exceptions import BaseError
        except ImportError:  # pragma: no cover - optional dep
            return (OSError,)
        return (BaseError, OSError)

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}
        self._stores: dict[tuple[str, str, str], tuple[str, Any]] = {}
        """Open stores by :meth:`_store_key`, each with the credential
        generation it was built for."""
        self._stores_lock = threading.Lock()
        self._listings: OrderedDict[str, ListingRows] = OrderedDict()
        self._hashes = _HashCache(None)

    def configure_cache(self, cache_dir: Path) -> None:
        self._hashes = _HashCache(cache_dir / "file-hashes.json")

    # -- capability refinement ------------------------------------------ #
    def effective_capabilities(self, locator: Locator, policy: Policy) -> Capability:
        """`file = "versioned"` makes a *single remote object* Addressable.

        A version id is what makes a recorded state re-openable, and only an
        object store hands one out, per object. A local path or a prefix under
        that policy is still Observed: the policy then only says how drift is
        treated (accepted, not an error), not that history can be read back.
        Whether the bucket actually versions is known once a fingerprint
        carries `version_id`; `open` and `verify` refuse a state without one.
        """
        base = Capability.FINGERPRINT | Capability.CHEAP_FINGERPRINT | Capability.DIFF
        try:
            scheme, _root, key = _parse(self._uri(locator))
        except BackendError:
            return base
        if scheme == "local":
            # tether can make (and remove) a local directory; not a remote prefix.
            base |= Capability.CREATE
        if getattr(policy, "file", "immutable") != "versioned":
            return base
        if scheme == "local" or not key or key.endswith("/"):
            return base
        return base | Capability.ADDRESSABLE

    def state_addressable(self, locator: Locator, state: State) -> bool:
        # A remote object is re-openable only through its version id.
        return not (state.get("type") == "object" and not state.get("version_id"))

    def _remember(self, digest: str, rows: ListingRows) -> None:
        with self._hashes._lock:  # shared with the hash cache; one lock per backend
            self._listings[digest] = rows
            self._listings.move_to_end(digest)
            while len(self._listings) > self._LISTING_CACHE:
                self._listings.popitem(last=False)

    def _recall(self, digest: str) -> ListingRows | None:
        with self._hashes._lock:
            rows = self._listings.get(digest)
            if rows is not None:
                self._listings.move_to_end(digest)
            return rows

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

        Raises:
            ConfigError: An option obstore does not know, or a value it cannot
                take.
        """
        from obstore.store import from_url

        from tether.credentials import storage_options

        options: dict[str, Any] = dict(self._config.get("storage_options") or {})
        region = locator.get("region")
        if region:
            options["region"] = str(region)
        # Per-object credentials from secrets.toml, resolved to static keys.
        options.update(storage_options(self.secrets_for(locator)))
        # HTTP client settings are a separate argument, however they are
        # spelled; as store config keys obstore panics (a BaseException,
        # past `wrap_library_errors`).
        client: dict[str, Any] = {}
        for key, value in dict(options.pop("client_options", None) or {}).items():
            name = _client_key(str(key))
            if name is None:
                raise ConfigError(f"file storage_options: unknown client option {key}")
            client[name] = _client_value(name, value)
        for key in list(options):
            name = _client_key(str(key))
            if name is not None:
                client[name] = _client_value(name, options.pop(key))
        if client:
            options["client_options"] = client
        try:
            return from_url(root, **options)
        except Exception as exc:  # unknown keys, unparseable values
            raise ConfigError(f"file storage_options for {root}: {exc}") from exc
        except BaseException as exc:
            if type(exc).__name__ != "PanicException":  # pyo3's, not importable
                raise
            raise ConfigError(f"file storage_options for {root}: {exc}") from None

    def _store_key(
        self, root: str, locator: Locator
    ) -> tuple[tuple[str, str, str], str]:
        """What a store is built from, beyond `root`: the object's credential
        rule (two prefixes of one bucket may have different `[uris."..."]`
        entries) and its region -- and, apart, the generation of keys the
        rule resolves to right now (a role's expire; a fresh set replaces the
        store built for the last one)."""
        from tether.credentials import aws_credentials

        secrets = self.secrets_for(locator)
        creds = aws_credentials(secrets) or {}
        return (
            root,
            json.dumps(secrets, sort_keys=True, default=str),
            str(locator.get("region") or ""),
        ), json.dumps(creds, sort_keys=True)

    def _store(self, root: str, locator: Locator) -> Any:
        key, generation = self._store_key(root, locator)
        with self._stores_lock:  # fingerprints fan out: build each store once
            held = self._stores.get(key)
            if held is not None and held[0] == generation:
                return held[1]
            self._obstore()  # surface a clear error before touching the store
            store = self._open_store(root, locator)
            self._stores[key] = (generation, store)
            return store

    # -- store lifecycle (local directories) ----------------------------- #
    def _local_dir(self, locator: Locator, what: str) -> Path:
        scheme, _root, path = _parse(self._uri(locator))
        if scheme != "local":
            raise BackendError(
                f"file {what}: only a local directory can be created or removed by "
                "tether; a remote prefix is managed by its bucket",
                kind="file",
            )
        return Path(path)

    def create(self, locator: Locator, *, owner: str) -> State:
        path = self._local_dir(locator, "create")
        if path.exists():
            raise BackendError(
                f"{path} already exists; `create` never adopts it", kind="file"
            )
        path.mkdir(parents=True)
        (path / OWNER_MARKER).write_text(owner + "\n", encoding="utf-8")
        return self.fingerprint(locator, None)

    def owner(self, locator: Locator) -> str | None:
        path = self._local_dir(locator, "owner") / OWNER_MARKER
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8").strip() or None

    def is_ref_empty(
        self, locator: Locator, *, ignoring: Collection[str] = ()
    ) -> bool | None:
        path = self._local_dir(locator, "is_ref_empty")
        if not path.is_dir():
            return None
        # No branches to ignore here: a directory is empty when only the
        # marker is in it.
        return all(entry.name == OWNER_MARKER for entry in path.iterdir())

    def delete_store(self, locator: Locator) -> None:
        path = self._local_dir(locator, "delete_store")
        if self.is_ref_empty(locator) is not True:
            raise BackendError(f"{path} is not empty; not removing it", kind="file")
        marker = path / OWNER_MARKER
        owner = marker.read_text(encoding="utf-8") if marker.is_file() else None
        marker.unlink(missing_ok=True)
        try:
            path.rmdir()  # refuses anything but an empty directory
        except OSError:
            # A file appeared in between: put the marker back so the store
            # stays ours to reclaim next time, rather than an unowned leftover.
            if owner is not None:
                marker.write_text(owner, encoding="utf-8")
            raise

    # -- protocol -------------------------------------------------------- #
    def identity(self, locator: Locator) -> Locator:
        return {"uri": canonical_uri(self._uri(locator))}

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
            entries, links = _walk(path)
            hashes = self._hashes.hashes([(path / rel, st) for rel, st in entries])
            rows: ListingRows = {
                rel: (hashes[path / rel], st.st_size) for rel, st in entries
            }
            rows.update({rel: (_link_token(target), 0) for rel, target in links})
            digest = _digest_pairs([(p, tok) for p, (tok, _) in rows.items()])
            self._remember(digest, rows)
            return {
                "type": "dir",
                "count": len(rows),
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
            without_etag: list[str] = []
            for page in obs.list(store, prefix=key or None):
                for meta in page:
                    size = int(meta["size"])
                    etag = _strip_etag(meta.get("e_tag"))
                    if not etag:
                        without_etag.append(str(meta["path"]))
                    rows[str(meta["path"])] = (etag, size)
                    total += size
            if without_etag:
                # A digest over names alone would call any rewrite "unchanged".
                # Say so rather than degrade silently.
                sample = ", ".join(without_etag[:3])
                raise BackendError(
                    f"{len(without_etag)} object(s) under {key or '/'} report no ETag "
                    f"({sample}{', ...' if len(without_etag) > 3 else ''}); the store "
                    "cannot be fingerprinted by listing -- pin single objects "
                    "with a versioned policy instead",
                    kind="file",
                )
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
        if state.get("type") == "object" and not version_id and pin is None:
            # Recorded as re-openable (`file = "versioned"`) but the store gave
            # no version id: the bucket is not versioned, so this state names
            # nothing that can be read back once the object changes.
            try:
                current = self.fingerprint(locator, None)
            except BackendError as exc:
                return VerifyReport(VerifyStatus.MISSING, str(exc))
            if current == state:
                return VerifyReport(
                    VerifyStatus.OK, "no version id: the bucket is not versioned"
                )
            return VerifyReport(
                VerifyStatus.MISSING,
                "object changed and no version id was recorded (bucket not "
                "versioned); the state cannot be read back",
            )
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

    def fork(
        self,
        locator: Locator,
        source: Pin | State,
        name: str,
        *,
        expected: State | None = None,
    ) -> str:
        raise CapabilityError("file backend cannot fork", kind="file")

    def delete_working_ref(self, locator: Locator, ref: str) -> None:
        return None

    # -- listings / diff ------------------------------------------------- #
    def listing(self, locator: Locator, state: State) -> str | None:
        if state.get("type") not in ("dir", "prefix"):
            return None  # a single object's state is already fully descriptive
        digest = str(state.get("digest", ""))
        rows = self._recall(digest)
        if rows is None:
            # Not cached (different process / evicted): re-read and use it only
            # if the object still has exactly the recorded state.
            current = self.fingerprint(locator, None)
            if current.get("digest") != digest:
                return None
            rows = self._recall(digest)
            if rows is None:  # pragma: no cover - evicted between the two calls
                return None
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
            if not version_id and target.get("type") == "object":
                # Asked for a recorded state, but the store recorded no version
                # id for it: there is no historical coordinate to open. Say so
                # rather than hand back whatever the object holds now.
                raise BackendError(
                    f"{uri}: the recorded state has no version id (the bucket is "
                    "not versioned), so it cannot be opened at that state",
                    kind="file",
                )
        return FileHandle(
            key=uri,
            read_only=True,  # tether never writes through file handles
            uri=uri,
            version_id=str(version_id) if version_id else None,
        )


def _factory(config: dict) -> FileBackend:
    return FileBackend(config)


register_backend("file", _factory)
