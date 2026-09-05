"""tether: jj-style version control for heterogeneous datasets.

Public API is intentionally small. The primary entry point is :class:`Repo`.
"""

from __future__ import annotations

from tether.errors import (
    BackendError,
    ImmutableObjectModified,
    MultiObjectError,
    PinDriftError,
    StaleWorkingCopyError,
    TetherError,
    UnpinnedStateError,
)
from tether.manifest import ObjectManifest, Pin, Policy, RepoConfig, WorkspaceState
from tether.repo import Repo

__all__ = [
    "BackendError",
    "ImmutableObjectModified",
    "MultiObjectError",
    "ObjectManifest",
    "Pin",
    "PinDriftError",
    "Policy",
    "Repo",
    "RepoConfig",
    "StaleWorkingCopyError",
    "TetherError",
    "UnpinnedStateError",
    "WorkspaceState",
]

__version__ = "0.1.0"
