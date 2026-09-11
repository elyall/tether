"""tether: jj-style version control for heterogeneous datasets.

The primary entry point is `Repo`: open (or initialize) a dataset inside an
existing git/jj repository, register objects with `Repo.add`, then `commit`,
`new`, `open`, `verify`, `diff`, and `gc`. Everything the CLI does is a thin
wrapper over it.

Supporting modules:

- `tether.handles`: the typed native handles `Repo.open` returns.
- `tether.backends`: the `ObjectBackend` protocol, capability tiers, reports,
  and the backend registry (one module per system under `tether.backends.*`).
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

from tether import backends, handles, migrations, oplog, testing, vcs
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
from tether.export import ExportBundle, PublishReport
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
from tether.migrations import UpgradeReport
from tether.oplog import OpEntry
from tether.plan import Action, Plan
from tether.registry import ImportSpec
from tether.repo import (
    TETHER_REV_ENV,
    AbandonReport,
    CommitResult,
    DiffEntry,
    ForgetWorkspaceReport,
    GcReport,
    ImportReport,
    ObjectStatus,
    PromoteReport,
    PullReport,
    RepairReport,
    Repo,
    SetReport,
    StatusReport,
    UndoReport,
    UndoToReport,
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
    "UndoToReport",
    "UnpinnedStateError",
    "UpgradeReport",
    "VcsDrift",
    "VcsError",
    "WorkspaceState",
    "backends",
    "compute_pin_id",
    "handles",
    "listing_name",
    "manifest_hash",
    "migrations",
    "oplog",
    "ref_for_pin",
    "testing",
    "vcs",
    "working_ref_bookmark",
    "working_ref_name",
    "working_ref_workspace",
]

try:
    # Single source of truth is pyproject.toml (distribution `tether-vcs`).
    __version__ = _dist_version("tether-vcs")
except PackageNotFoundError:  # pragma: no cover - running from a bare checkout
    __version__ = "0+unknown"
