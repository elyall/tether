"""The engine: a `Repo` is manifests in a VCS working tree plus the systems
they name.

The class is assembled from one mixin per command family so each reads on
its own; the public surface is exactly what `tether.repo` exported before
the split (`Repo`, the report dataclasses, `TETHER_REV_ENV`, `short_state`).
"""

from __future__ import annotations

from tether.repo._commit import CommitOps
from tether.repo._core import TETHER_REV_ENV, RepoCore
from tether.repo._fork import ForkOps
from tether.repo._gc import GcOps
from tether.repo._objects import ObjectOps
from tether.repo._promote import PromoteOps
from tether.repo._reports import (
    AbandonReport,
    CommitResult,
    DiffEntry,
    ForgetWorkspaceReport,
    GcReport,
    ObjectStatus,
    PromoteReport,
    PullReport,
    RepairReport,
    SetReport,
    StatusReport,
    UndoReport,
    VcsDrift,
    short_state,
)
from tether.repo._undo import UndoOps

__all__ = [
    "TETHER_REV_ENV",
    "AbandonReport",
    "CommitResult",
    "DiffEntry",
    "ForgetWorkspaceReport",
    "GcReport",
    "ObjectStatus",
    "PromoteReport",
    "PullReport",
    "RepairReport",
    "Repo",
    "SetReport",
    "StatusReport",
    "UndoReport",
    "VcsDrift",
    "short_state",
]


class Repo(ObjectOps, CommitOps, ForkOps, PromoteOps, GcOps, UndoOps, RepoCore):
    """A tether dataset: manifests in a VCS working tree plus the systems they name.

    Construct with `Repo.init` (new dataset) or `Repo.find` (existing one).
    Every method that touches external systems fans out concurrently across
    objects and aggregates failures into `MultiObjectError`.

    Attributes:
        root: Dataset root (the directory holding `tether.toml`).
        config: The committed `RepoConfig`.
        vcs: Adapter for the enclosing jj or git repository.
        objects: Committed manifests in the working tree, by key.
        workspace: Untracked per-workspace state (working refs, cached snapshot).
    """
