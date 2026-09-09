"""On-disk manifest model: config, per-object manifests, workspace state.

Layout inside a dataset root (see the project plan)::

    tether.toml                 # committed repo config
    .tether/
      .gitignore                # ignores workspace.toml
      objects/<key>.toml        # committed, one per object; key path may nest
      workspace.toml            # untracked working state

Only *pinned* state is written into ``objects/*.toml``; live snapshots live in
the untracked ``workspace.toml``. Everything in this module is pure data +
(de)serialization -- no network, no VCS, no backends.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import tomlkit

from tether.errors import ConfigError

CONFIG_FILENAME = "tether.toml"
TETHER_DIR = ".tether"
OBJECTS_DIR = "objects"
LISTINGS_DIR = "listings"
WORKSPACE_FILENAME = "workspace.toml"
GITIGNORE_FILENAME = ".gitignore"

REF_PREFIX = "tether."
CONFIG_VERSION = 1

WriteMode = Literal["fork", "track"]
FileMode = Literal["immutable", "versioned"]
PinMode = Literal["native", "record"]

JsonValue = Any
State = dict[str, JsonValue]
Locator = dict[str, JsonValue]


# --------------------------------------------------------------------------- #
# Canonical hashing helpers
# --------------------------------------------------------------------------- #
def canonical_bytes(obj: Any) -> bytes:
    """Return a deterministic JSON encoding used for hashing.

    Keys are sorted; separators are compact; non-ASCII is preserved. ``None``
    values are permitted here (canonicalization only), unlike the TOML writers.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _blake(*parts: bytes, size: int = 32) -> str:
    h = hashlib.blake2b(digest_size=size)
    for p in parts:
        h.update(len(p).to_bytes(8, "big"))
        h.update(p)
    return h.hexdigest()


DATASET_ID_LEN = 8


def new_dataset_id() -> str:
    """A fresh 8-hex dataset id (the namespace for a dataset's native refs)."""
    return uuid.uuid4().hex[:DATASET_ID_LEN]


def is_dataset_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == DATASET_ID_LEN
        and all(c in "0123456789abcdef" for c in value)
    )


def compute_pin_id(kind: str, identity: Locator, state: State, dataset_id: str) -> str:
    """Content-address a pin: ``<dataset8>.<hash16>``.

    The hash covers ``(kind, locator identity, state)`` and is stable across
    processes and machines, so re-committing an unchanged object is a no-op and
    identical states dedupe to one pin *within a dataset*. The dataset id in
    front is the namespace: several datasets can pin the same store and each
    one's ``gc`` only ever sees its own pins. Callers pass the *content* state
    (see :func:`tether.backends.base.content_state`).
    """
    digest = _blake(
        canonical_bytes(kind),
        canonical_bytes(identity),
        canonical_bytes(state),
        size=16,
    )[:16]
    return f"{dataset_id[:DATASET_ID_LEN]}.{digest}"


def pin_dataset(pin_id: str) -> str | None:
    """The dataset id a pin id belongs to, or ``None`` if it is not one of ours."""
    ds, sep, rest = pin_id.partition(".")
    if sep and is_dataset_id(ds) and rest and "." not in rest:
        return ds
    return None


def ref_for_pin(pin_id: str) -> str:
    """Native reference name for a pin id (dot-delimited; no ``/``)."""
    return f"{REF_PREFIX}{pin_id}"


def listing_name(kind: str, identity: Locator, state: State) -> str:
    """Content-addressed file name for a state's listing.

    Derived from the same inputs as the pin id (see ``ObjectBackend.listing``),
    so the engine can locate a listing from a manifest alone, at commit time and
    at diff time.
    """
    digest = _blake(
        canonical_bytes(kind),
        canonical_bytes(identity),
        canonical_bytes(state),
        size=16,
    )[:20]
    return f"{digest}.jsonl"


def slugify_key(key: str) -> str:
    """Turn an object key into a safe, dot-free ref fragment."""
    slug = re.sub(r"[^0-9A-Za-z]+", "-", key).strip("-").lower()
    return slug or "obj"


WORKING_REF_PREFIX = f"{REF_PREFIX}ws."


def working_ref_name(dataset_id: str, workspace_id: str, key: str) -> str:
    """Working-branch name; datasets, workspaces, and keys never collide.

    ``tether.ws.<dataset8>.<workspace8>.<slug>-<key6>``: the dataset id is the
    namespace ``gc`` stays inside, the slug keeps the name readable, the 6-hex
    key digest keeps ``zarr/imaging`` and ``zarr-imaging`` apart.
    """
    digest = _blake(canonical_bytes(key), size=8)[:6]
    return (
        f"{WORKING_REF_PREFIX}{dataset_id[:DATASET_ID_LEN]}.{workspace_id[:8]}."
        f"{slugify_key(key)}-{digest}"
    )


def _working_ref_parts(ref: str) -> tuple[str, str] | None:
    if not ref.startswith(WORKING_REF_PREFIX):
        return None
    rest = ref[len(WORKING_REF_PREFIX) :]
    ds, _, rest = rest.partition(".")
    ws, _, _ = rest.partition(".")
    if not is_dataset_id(ds) or not ws:
        return None
    return ds, ws


def working_ref_dataset(ref: str) -> str | None:
    """The dataset id embedded in a working ref name, if it is one."""
    parts = _working_ref_parts(ref)
    return parts[0] if parts else None


def working_ref_workspace(ref: str) -> str | None:
    """The 8-char workspace id embedded in a working ref name, if it is one."""
    parts = _working_ref_parts(ref)
    return parts[1] if parts else None


def _drop_nulls(obj: Any) -> Any:
    """Recursively drop ``None`` values so the result is TOML-writable."""
    if isinstance(obj, dict):
        return {k: _drop_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_drop_nulls(v) for v in obj]
    return obj


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Policy:
    """Per-object behavioral knobs."""

    write: WriteMode = "fork"
    file: FileMode = "immutable"
    pin: PinMode = "native"

    def to_dict(self) -> dict[str, str]:
        return {"write": self.write, "file": self.file, "pin": self.pin}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Policy:
        data = data or {}
        write = data.get("write", "fork")
        file = data.get("file", "immutable")
        pin = data.get("pin", "native")
        if write not in ("fork", "track"):
            raise ConfigError(f"invalid policy.write: {write!r}")
        if file not in ("immutable", "versioned"):
            raise ConfigError(f"invalid policy.file: {file!r}")
        if pin not in ("native", "record"):
            raise ConfigError(f"invalid policy.pin: {pin!r}")
        return cls(write=write, file=file, pin=pin)


@dataclass(frozen=True)
class Pin:
    """A durable native reference created for a committed state."""

    id: str
    ref: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "ref": self.ref}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Pin:
        return cls(id=str(data["id"]), ref=str(data["ref"]))


# --------------------------------------------------------------------------- #
# Object manifest
# --------------------------------------------------------------------------- #
@dataclass
class ObjectManifest:
    """The committed record for one object.

    ``state``/``pin`` are ``None`` until the object has been committed at least
    once. ``recoverable`` is ``False`` for Observed-tier objects whose committed
    state cannot be reconstructed later (e.g. a mutable file by mtime).
    """

    key: str
    kind: str
    locator: Locator
    policy: Policy = field(default_factory=Policy)
    state: State | None = None
    pin: Pin | None = None
    captured_at: str | None = None
    recoverable: bool = True

    def to_toml(self) -> str:
        doc = tomlkit.document()
        doc["key"] = self.key
        doc["kind"] = self.kind
        doc["recoverable"] = self.recoverable
        if self.captured_at is not None:
            doc["captured_at"] = self.captured_at
        doc["locator"] = _drop_nulls(self.locator)
        doc["policy"] = self.policy.to_dict()
        if self.state is not None:
            doc["state"] = _drop_nulls(self.state)
        if self.pin is not None:
            doc["pin"] = self.pin.to_dict()
        return tomlkit.dumps(doc)

    @classmethod
    def from_toml(cls, text: str) -> ObjectManifest:
        data = _loads_plain(text)
        try:
            key = str(data["key"])
            kind = str(data["kind"])
        except KeyError as exc:  # pragma: no cover - defensive
            raise ConfigError(f"object manifest missing {exc}") from exc
        state = data.get("state")
        pin = data.get("pin")
        return cls(
            key=key,
            kind=kind,
            locator=dict(data.get("locator", {})),
            policy=Policy.from_dict(data.get("policy")),
            state=dict(state) if state is not None else None,
            pin=Pin.from_dict(dict(pin)) if pin is not None else None,
            captured_at=(str(data["captured_at"]) if "captured_at" in data else None),
            recoverable=bool(data.get("recoverable", True)),
        )

    def canonical(self) -> bytes:
        """Deterministic encoding used for the manifest hash."""
        payload = {
            "key": self.key,
            "kind": self.kind,
            "locator": self.locator,
            "policy": self.policy.to_dict(),
            "state": self.state,
            "pin": self.pin.to_dict() if self.pin else None,
            "recoverable": self.recoverable,
        }
        return canonical_bytes(payload)

    def with_pin(
        self,
        *,
        state: State,
        pin: Pin | None,
        recoverable: bool = True,
    ) -> ObjectManifest:
        return replace(
            self,
            state=state,
            pin=pin,
            recoverable=recoverable,
            captured_at=_now(),
        )


def _loads_plain(text: str) -> dict[str, Any]:
    """Parse TOML into plain Python structures (no tomlkit wrappers)."""
    return tomlkit.loads(text).unwrap()


# --------------------------------------------------------------------------- #
# Repo config (tether.toml)
# --------------------------------------------------------------------------- #
@dataclass
class RepoConfig:
    """Committed repository configuration."""

    version: int = CONFIG_VERSION
    dataset_id: str = field(default_factory=new_dataset_id)
    """`[dataset] id`: 8 hex chars naming this dataset in every store it pins.
    Pins are `tether.<id>.<hash>`, working branches `tether.ws.<id>.<ws>...`;
    `gc` only touches refs in this namespace. Committed, so all clones share
    it."""
    snapshot_auto: bool = True
    verify_on_status: bool = False
    new_auto_fork: bool = False
    new_fork: str = "lazy"
    """`[new] fork`: `lazy` (default) creates a working branch on the first
    writable `open`; `eager` creates every branch during `new`."""
    defaults: Policy = field(default_factory=Policy)
    vcs: dict[str, Any] = field(default_factory=dict)
    backends: dict[str, dict[str, Any]] = field(default_factory=dict)
    import_query: str | None = None
    """`[import] query`: default SQL for `tether import` (holds no credentials)."""

    def to_toml(self) -> str:
        doc = tomlkit.document()
        tether_tbl = tomlkit.table()
        tether_tbl["version"] = self.version
        doc["tether"] = tether_tbl
        doc["dataset"] = {"id": self.dataset_id}
        doc["snapshot"] = {"auto": self.snapshot_auto}
        doc["verify"] = {"on_status": self.verify_on_status}
        doc["new"] = {"auto_fork": self.new_auto_fork, "fork": self.new_fork}
        doc["defaults"] = self.defaults.to_dict()
        if self.vcs:
            doc["vcs"] = _drop_nulls(self.vcs)
        if self.backends:
            doc["backends"] = _drop_nulls(self.backends)
        if self.import_query:
            doc["import"] = {"query": self.import_query}
        return tomlkit.dumps(doc)

    @classmethod
    def from_toml(cls, text: str) -> RepoConfig:
        data = _loads_plain(text)
        tether_tbl = data.get("tether") or {}
        snapshot = data.get("snapshot") or {}
        verify = data.get("verify") or {}
        new = data.get("new") or {}
        import_tbl = data.get("import") or {}
        query = import_tbl.get("query")
        fork = str(new.get("fork", "lazy"))
        if fork not in ("lazy", "eager"):
            raise ConfigError(f"invalid [new] fork: {fork!r} (lazy or eager)")
        dataset_id = (data.get("dataset") or {}).get("id")
        if not is_dataset_id(dataset_id):
            raise ConfigError(
                "tether.toml has no valid [dataset] id (8 hex chars). It namespaces "
                "this dataset's pins and working branches in every store; add\n"
                f'  [dataset]\n  id = "{new_dataset_id()}"\n'
                "(pins and branches made before it was set are not recognised: "
                "re-commit and `new`)"
            )
        return cls(
            version=int(tether_tbl.get("version", CONFIG_VERSION)),
            dataset_id=str(dataset_id),
            snapshot_auto=bool(snapshot.get("auto", True)),
            verify_on_status=bool(verify.get("on_status", False)),
            new_auto_fork=bool(new.get("auto_fork", False)),
            new_fork=fork,
            defaults=Policy.from_dict(data.get("defaults")),
            vcs=dict(data.get("vcs") or {}),
            backends=dict(data.get("backends") or {}),
            import_query=str(query) if query else None,
        )


# --------------------------------------------------------------------------- #
# Workspace state (untracked)
# --------------------------------------------------------------------------- #
@dataclass
class WorkspaceState:
    """Untracked per-workspace working state.

    ``base_states`` maps object key -> the committed state this workspace's
    working ref corresponds to (set when the branch is forked and whenever this
    workspace commits the object); a manifest that says something else was
    changed by someone else, and the workspace is *stale* for that object.
    ``working_refs`` maps object key -> native working ref that exists.
    ``pending_forks`` maps object key -> the branch
    name ``new`` decided on but has not created yet (lazy forking: it is
    created on the first writable ``open``). ``fork_points`` maps object key ->
    the state its working branch was created from; ``promote`` compares the
    base branch against it to tell a fast-forward from a divergence.
    ``last_snapshot`` caches the most recent fan-out fingerprints.
    """

    workspace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    base_states: dict[str, State] = field(default_factory=dict)
    working_refs: dict[str, str] = field(default_factory=dict)
    pending_forks: dict[str, str] = field(default_factory=dict)
    fork_points: dict[str, State] = field(default_factory=dict)
    last_snapshot: dict[str, State] = field(default_factory=dict)
    last_snapshot_at: str | None = None

    def to_toml(self) -> str:
        doc = tomlkit.document()
        doc["workspace_id"] = self.workspace_id
        if self.last_snapshot_at is not None:
            doc["last_snapshot_at"] = self.last_snapshot_at
        if self.working_refs:
            doc["working_refs"] = dict(self.working_refs)
        if self.pending_forks:
            doc["pending_forks"] = dict(self.pending_forks)
        if self.fork_points:
            doc["fork_points"] = {
                k: _drop_nulls(v) for k, v in self.fork_points.items()
            }
        if self.base_states:
            doc["base_states"] = {
                k: _drop_nulls(v) for k, v in self.base_states.items()
            }
        if self.last_snapshot:
            doc["last_snapshot"] = {
                k: _drop_nulls(v) for k, v in self.last_snapshot.items()
            }
        return tomlkit.dumps(doc)

    @classmethod
    def from_toml(cls, text: str) -> WorkspaceState:
        data = _loads_plain(text)
        return cls(
            workspace_id=str(data.get("workspace_id", uuid.uuid4().hex)),
            base_states={
                str(k): dict(v) for k, v in (data.get("base_states") or {}).items()
            },
            working_refs=dict(data.get("working_refs") or {}),
            pending_forks=dict(data.get("pending_forks") or {}),
            fork_points={
                str(k): dict(v) for k, v in (data.get("fork_points") or {}).items()
            },
            last_snapshot={
                str(k): dict(v) for k, v in (data.get("last_snapshot") or {}).items()
            },
            last_snapshot_at=(
                str(data["last_snapshot_at"]) if "last_snapshot_at" in data else None
            ),
        )

    def touch_snapshot(self, snapshot: dict[str, State]) -> None:
        self.last_snapshot = snapshot
        self.last_snapshot_at = _now()


# --------------------------------------------------------------------------- #
# Manifest set + hashing
# --------------------------------------------------------------------------- #
def manifest_hash(objects: dict[str, ObjectManifest]) -> str:
    """Hash the full committed object set (order-independent).

    Serves as the dataset "tree id" and the anchor for stale detection.
    """
    parts = [
        canonical_bytes([key, objects[key].canonical().decode("utf-8")])
        for key in sorted(objects)
    ]
    return _blake(*parts, size=32)


def key_to_relpath(key: str) -> Path:
    """Map an object key to its manifest path under ``objects/``."""
    if key.startswith("/") or ".." in key.split("/"):
        raise ConfigError(f"unsafe object key: {key!r}")
    return Path(OBJECTS_DIR, *key.split("/")).with_suffix(".toml")


def relpath_to_key(relpath: Path) -> str:
    """Inverse of :func:`key_to_relpath` (relative to ``objects/``)."""
    rel = relpath.with_suffix("")
    return "/".join(rel.parts)


# --------------------------------------------------------------------------- #
# Filesystem layout (working tree)
# --------------------------------------------------------------------------- #
def config_path(root: Path) -> Path:
    return root / CONFIG_FILENAME


def tether_path(root: Path) -> Path:
    return root / TETHER_DIR


def objects_dir(root: Path) -> Path:
    return root / TETHER_DIR / OBJECTS_DIR


def workspace_path(root: Path) -> Path:
    return root / TETHER_DIR / WORKSPACE_FILENAME


def listings_dir(root: Path) -> Path:
    return root / TETHER_DIR / LISTINGS_DIR


def listing_path(root: Path, name: str) -> Path:
    return listings_dir(root) / name


def write_listing(root: Path, name: str, text: str) -> Path:
    """Store a listing (idempotent: content-addressed names never change)."""
    path = listing_path(root, name)
    if not path.exists():
        _atomic_write(path, text)
    return path


def read_listing(root: Path, name: str) -> str | None:
    path = listing_path(root, name)
    return path.read_text(encoding="utf-8") if path.is_file() else None


def object_path(root: Path, key: str) -> Path:
    return root / TETHER_DIR / key_to_relpath(key)


def find_dataset_root(start: Path) -> Path | None:
    """Walk up from ``start`` looking for a ``tether.toml``."""
    start = start.resolve()
    for candidate in (start, *start.parents):
        if config_path(candidate).is_file():
            return candidate
    return None


def ensure_layout(root: Path) -> None:
    """Create ``.tether/`` and its ``.gitignore`` (ignoring workspace.toml)."""
    objects_dir(root).mkdir(parents=True, exist_ok=True)
    gitignore = tether_path(root) / GITIGNORE_FILENAME
    if not gitignore.exists():
        gitignore.write_text(f"/{WORKSPACE_FILENAME}\n", encoding="utf-8")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def read_config(root: Path) -> RepoConfig:
    text = config_path(root).read_text(encoding="utf-8")
    return RepoConfig.from_toml(text)


def write_config(root: Path, config: RepoConfig) -> None:
    _atomic_write(config_path(root), config.to_toml())


def read_objects(root: Path) -> dict[str, ObjectManifest]:
    """Load every committed object manifest from the working tree."""
    result: dict[str, ObjectManifest] = {}
    base = objects_dir(root)
    if not base.is_dir():
        return result
    for path in sorted(base.rglob("*.toml")):
        manifest = ObjectManifest.from_toml(path.read_text(encoding="utf-8"))
        result[manifest.key] = manifest
    return result


def write_object(root: Path, manifest: ObjectManifest) -> None:
    _atomic_write(object_path(root, manifest.key), manifest.to_toml())


def remove_object(root: Path, key: str) -> None:
    path = object_path(root, key)
    path.unlink(missing_ok=True)
    # Prune now-empty parent directories up to objects/.
    base = objects_dir(root)
    parent = path.parent
    while parent != base and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def read_workspace(root: Path) -> WorkspaceState:
    path = workspace_path(root)
    if not path.is_file():
        return WorkspaceState()
    return WorkspaceState.from_toml(path.read_text(encoding="utf-8"))


def write_workspace(root: Path, workspace: WorkspaceState) -> None:
    _atomic_write(workspace_path(root), workspace.to_toml())
