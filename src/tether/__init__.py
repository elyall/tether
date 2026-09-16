"""tether: jj-style version control for heterogeneous datasets.

The primary entry point is `Repo`: open (or initialize) a dataset inside an
existing git/jj repository, register objects with `Repo.add`, then `commit`,
`new`, `open`, `verify`, `diff`, and `gc`. Everything the CLI does is a thin
wrapper over it.

Supporting modules:

- `tether.handles`: the typed native handles `Repo.open` returns.
- `tether.backends`: the `ObjectBackend` protocol, capability tiers, reports,
  and the backend registry (one module per system: stable kinds under
  `tether.backends.*`, kinds tested against fakes so far under
  `tether.experimental.backends.*`).
- `tether.testing`: the conformance suite for backend authors.
- `tether.vcs`: the jj/git adapters tether stores its history through.

Example:
    >>> from tether import Repo
    >>> repo = Repo.find(".")
    >>> repo.commit("baseline")          # pin every object, commit the manifests
    >>> repo.new()                       # fork writable branches off the pins
    >>> handle = repo.open("zarr/imaging")
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

from tether import backends, handles, oplog, testing, vcs
from tether.errors import (
    BackendError,
    CapabilityError,
    ConfigError,
    ImmutableObjectModified,
    MergeConflict,
    MultiObjectError,
    PinDriftError,
    StalePlanError,
    StaleWorkingCopyError,
    TetherError,
    UnpinnedStateError,
    VcsError,
)
from tether.manifest import (
    ObjectManifest,
    Pin,
    Policy,
    RepoConfig,
    WorkspaceState,
    compute_pin_id,
    listing_name,
    manifest_hash,
    ref_for_pin,
    working_ref_bookmark,
    working_ref_name,
    working_ref_workspace,
)
from tether.oplog import OpEntry
from tether.plan import Action, Plan, Precondition
from tether.repo import (
    TETHER_REV_ENV,
    AbandonReport,
    CommitResult,
    DiffEntry,
    DropReport,
    ForgetWorkspaceReport,
    GcReport,
    ObjectStatus,
    PromoteReport,
    PullReport,
    RepairReport,
    Repo,
    SetReport,
    StatusReport,
    UndoReport,
    VcsDrift,
)

__all__ = [
    "TETHER_REV_ENV",
    "AbandonReport",
    "Action",
    "BackendError",
    "CapabilityError",
    "CommitResult",
    "ConfigError",
    "DiffEntry",
    "DropReport",
    "ExportBundle",
    "ForgetWorkspaceReport",
    "GcReport",
    "ImmutableObjectModified",
    "ImportReport",
    "ImportSpec",
    "MergeConflict",
    "MultiObjectError",
    "ObjectManifest",
    "ObjectStatus",
    "OpEntry",
    "Pin",
    "PinDriftError",
    "Plan",
    "Policy",
    "Precondition",
    "PromoteReport",
    "PublishReport",
    "PullReport",
    "RepairReport",
    "Repo",
    "RepoConfig",
    "SetReport",
    "StalePlanError",
    "StaleWorkingCopyError",
    "StatusReport",
    "TetherError",
    "UndoReport",
    "UnpinnedStateError",
    "UpgradeReport",
    "VcsDrift",
    "VcsError",
    "WorkspaceState",
    "backends",
    "build_bundle",
    "compute_pin_id",
    "handles",
    "listing_name",
    "manifest_hash",
    "oplog",
    "ref_for_pin",
    "specs_from_rows",
    "testing",
    "vcs",
    "working_ref_bookmark",
    "working_ref_name",
    "working_ref_workspace",
]

# The registry layer (`export`, `publish`, `import`) is experimental and lives
# under `tether.experimental`; the alpha-format upgrade path lives under
# `tether.upgrade` until 0.1.0. Their public names stay importable from
# `tether` -- that is the stable surface -- but are resolved on first access,
# so `import tether` loads none of it.
_LAZY_EXPORTS = {
    "UpgradeReport": "tether.upgrade",
    "ExportBundle": "tether.experimental.registry",
    "ImportReport": "tether.experimental.registry",
    "ImportSpec": "tether.experimental.registry",
    "PublishReport": "tether.experimental.registry",
    "build_bundle": "tether.experimental.registry",
    "specs_from_rows": "tether.experimental.registry",
}


def __getattr__(name: str) -> object:
    module = _LAZY_EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'tether' has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module), name)
    globals()[name] = value  # resolve once
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


try:
    # Single source of truth is pyproject.toml (distribution `tether-vcs`).
    __version__ = _dist_version("tether-vcs")
except PackageNotFoundError:  # pragma: no cover - running from a bare checkout
    __version__ = "0+unknown"
