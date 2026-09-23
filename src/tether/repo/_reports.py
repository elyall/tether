"""Result types every `Repo` operation returns, and the small renderers the
CLI shares with them."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from tether.backends.base import ObjectDiff, Tier, VerifyReport
from tether.manifest import Pin, State
from tether.oplog import OpEntry
from tether.plan import Plan


@dataclass
class ObjectStatus:
    """One object's classification in a `StatusReport`."""

    key: str
    """Object key."""
    kind: str
    """Backend kind."""
    tier: Tier
    """Effective capability tier for this object."""
    committed: bool
    """Whether the manifest has a committed state."""
    pinned: bool
    """Whether the manifest carries a native pin."""
    recoverable: bool
    """Whether the committed state can be reconstructed later."""
    changed: bool
    """Whether the current state differs from the committed one."""
    current_state: State | None
    """The fingerprint taken by this status (or the cached one)."""
    origin: str = "adopted"
    """`created` when `add --create` made the store (`ObjectManifest.origin`)."""
    verify: VerifyReport | None = None
    """Cheap verify result when `RepoConfig.verify_on_status` is set."""
    error: str | None = None
    """Fingerprint failure message, if any."""
    behind: bool = False
    """The object has no working branch and its upstream branch has moved past
    the committed state (seen by a fan-out snapshot); `tether pull` takes it."""

    @property
    def state_label(self) -> str:
        """`"new"`, `"modified"`, `"behind"`, `"clean"`, or `"error"`."""
        if self.error is not None:
            return "error"
        if not self.committed:
            return "new"
        if self.behind:
            return "behind"
        if self.changed:
            return "modified"
        return "clean"


@dataclass
class SetReport:
    """What `set` changed per object."""

    changed: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    """key -> {field: (old, new)} for the policy fields that changed."""
    unchanged: list[str] = field(default_factory=list)
    """Objects whose policy already had the requested values."""


@dataclass
class PullReport:
    """What `pull` did: the fetched heads of a bookmark's branches, committed."""

    bookmark: str = ""
    """The bookmark pulled (the one this workspace works on)."""
    committed: dict[str, tuple[State, State]] = field(default_factory=dict)
    """key -> (state at the bookmark's commit, head now) for objects that moved."""
    unchanged: list[str] = field(default_factory=list)
    """Objects whose head is what the bookmark's commit recorded."""
    skipped: dict[str, str] = field(default_factory=dict)
    """key -> why it was not fetched (no commit yet; no branch yet)."""
    vcs_commit: str | None = None
    """The dataset commit made on the bookmark, if anything moved."""
    pinned: dict[str, Pin | None] = field(default_factory=dict)
    """key -> pin created for the fetched state (None: recorded only)."""


@dataclass
class VcsDrift:
    """A dataset commit that is gone from VCS history without tether knowing.

    `jj undo`, `jj abandon`, `git reset` -- anything that removes a commit
    tether made -- leaves the op log believing the commit exists. Detected by
    `Repo.vcs_drift`, shown by `status` and `ops`.

    Attributes:
        op: The `commit` entry.
        commit: The commit id it recorded.
        referenced: Key -> whether the working tree's manifest still names the
            pin that commit made (an undone commit leaves the manifests as
            uncommitted edits: still referenced; an abandoned one reverts
            them: not referenced, so `gc` would release the pin).
    """

    op: OpEntry
    commit: str
    referenced: dict[str, bool] = field(default_factory=dict)

    @property
    def message(self) -> str:
        kept = sorted(k for k, v in self.referenced.items() if v)
        dropped = sorted(k for k, v in self.referenced.items() if not v)
        bits = [
            f"dataset commit {self.commit[:12]} (op {self.op.id}) is no longer in "
            "VCS history: undone or abandoned outside tether"
        ]
        if kept:
            bits.append(
                f"its pins for {', '.join(kept)} are still named by the working "
                "tree (commit again, or restore the manifests to drop them)"
            )
        if dropped:
            bits.append(
                f"its pins for {', '.join(dropped)} are unreferenced; `gc` will "
                "release them, `repair` recreates them if the commit comes back"
            )
        return "; ".join(bits)


@dataclass
class StatusReport:
    """Result of `Repo.status`."""

    manifest_hash: str
    """Hash of the committed object set (the dataset's "tree id")."""
    stale: bool
    """Some forked object's committed manifest changed underneath this workspace."""
    objects: list[ObjectStatus] = field(default_factory=list)
    """Per-object classifications, sorted by key."""
    stale_keys: list[str] = field(default_factory=list)
    """The objects that make the workspace stale (see `Repo.stale_keys`)."""
    bookmark: str | None = None
    """The bookmark this workspace works on (`None`: read-only working copy)."""
    trunk: bool = False
    """Whether that bookmark is the trunk (writes land on upstream branches)."""
    bookmark_drift: list[str] = field(default_factory=list)
    """Ways the VCS bookmark and this workspace have parted (see
    `Repo.bookmark_drift`), each with what to do about it."""
    vcs_drift: list[VcsDrift] = field(default_factory=list)
    """Dataset commits this workspace made that left VCS history behind
    tether's back (see `Repo.vcs_drift`)."""
    fresh: bool = True
    """Whether the states were fingerprinted by this call (`False`: the cached
    snapshot from `snapshot_at` was reused)."""
    snapshot_at: str | None = None
    """When the states shown were fingerprinted (UTC, ISO 8601)."""


@dataclass
class CommitResult:
    """Result of `Repo.commit`."""

    message: str
    """The commit message."""
    pinned: dict[str, Pin | None] = field(default_factory=dict)
    """Objects whose state was recorded: the `Pin` created, or `None` for
    Addressable objects (recorded without a native ref)."""
    unrecoverable: list[str] = field(default_factory=list)
    """Observed-tier objects recorded with `recoverable = False`."""
    unchanged: list[str] = field(default_factory=list)
    """Objects skipped because their state was already recorded."""
    vcs_commit: str | None = None
    """Commit id of the VCS commit, or `None` if nothing changed / `vcs=False`."""


@dataclass
class GcReport:
    """Result of `Repo.gc`."""

    unpinned: dict[str, list[str]] = field(default_factory=dict)
    """Backend kind -> pin ids released (or that would be, in a dry run)."""
    kept_pins: dict[str, list[str]] = field(default_factory=dict)
    """Backend kind -> unreferenced pin ids this clone did not create and so
    left alone (`keep-pin`; `gc --release-foreign` releases them)."""
    deleted_working_refs: dict[str, list[str]] = field(default_factory=dict)
    """Object key -> native working branches deleted (`--prune-bookmarks`)."""
    kept_working_refs: dict[str, list[str]] = field(default_factory=dict)
    """Object key -> stray branches kept because they hold unpinned data."""
    forgotten_working_refs: dict[str, list[str]] = field(default_factory=dict)
    """Object key -> refs dropped from the workspace state (object removed)."""
    deleted_listings: list[str] = field(default_factory=list)
    """`.tether/listings/` files no manifest references."""
    deleted_stores: dict[str, str] = field(default_factory=dict)
    """Object key (at creation) -> the store `add --create` made, removed now
    that nothing references it and only tether's refs remained."""
    kept_stores: dict[str, str] = field(default_factory=dict)
    """Object key -> why an unreferenced created store was left in place."""
    forgotten_stores: list[str] = field(default_factory=list)
    """Index entries dropped: a created store already gone (`forget-store`), or
    a touched store with nothing of this dataset's left in it
    (`forget-touched`)."""
    dry_run: bool = True
    """Whether anything was actually released."""
    plan: Plan | None = None
    """The plan that was (or would be) applied."""


@dataclass
class PromoteReport:
    """Result of `Repo.apply_promote`."""

    fast_forwarded: dict[str, State] = field(default_factory=dict)
    """Key -> the base branch's new state after a fast-forward."""
    merged: dict[str, State] = field(default_factory=dict)
    """Key -> the base branch's new state after a native merge."""
    skipped: list[str] = field(default_factory=list)
    """Keys whose base already matched the target, or that had nothing to promote."""
    refused: dict[str, str] = field(default_factory=dict)
    """Key -> why tether would not move the base (with the system's own recipe)."""
    held: dict[str, str] = field(default_factory=dict)
    """Key -> what would have happened: a fast-forward or merge not performed
    because another object was refused and no keys were named. A bookmark is
    planned whole or not at all; the plan is the atomic unit -- once applying,
    each system's fast-forward stands on its own (see the caveats)."""
    conflicts: dict[str, list[str]] = field(default_factory=dict)
    """Key -> conflicting units reported by a merge that was rolled back."""
    kept_forks: dict[str, str] = field(default_factory=dict)
    """Key -> why the working branch was not reset onto a merge result: it
    gained writes between the plan and the merge, and those are never
    discarded; commit them and promote again."""
    trunk_moved: str | None = None
    """The commit the trunk bookmark now points at, when every promoted object
    fast-forwarded and nothing was refused: the bookmark's commit describes
    what the upstream branches now hold, so `main` is set to it."""
    trunk_held: str | None = None
    """Why the trunk bookmark was left where it is although the data landed:
    it gained commits since the plan, and it is never moved backwards."""
    plan: Plan | None = None


@dataclass
class RepairReport:
    """What `Repo.repair` rebuilt.

    Attributes:
        repinned: Object key -> pin id recreated from the manifest's state.
        reforked: Object key -> working branch recreated from the manifest.
        failed: Target -> why it could not be rebuilt (state no longer reachable).
        plan: The plan that was applied.
    """

    repinned: dict[str, str] = field(default_factory=dict)
    reforked: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    plan: Plan | None = None


@dataclass
class ForgetWorkspaceReport:
    """What `Repo.apply_forget_workspace` did.

    Attributes:
        workspace: The 8-char workspace id that was forgotten.
        removed_files: Per-workspace files removed (`workspace.toml`, `ops.jsonl`).
        vcs: What the VCS did (`jj workspace forget` / `git worktree remove`),
            if anything.
        failed: Target -> why a step failed.
        plan: The plan that was applied.
    """

    workspace: str = ""
    removed_files: list[str] = field(default_factory=list)
    vcs: str | None = None
    failed: dict[str, str] = field(default_factory=dict)
    plan: Plan | None = None


@dataclass
class DropReport:
    """What `Repo.drop` did: a bookmark, its commits, and its store leftovers,
    gone in one step.

    Attributes:
        bookmark: The bookmark that was dropped.
        left_for: The bookmark this checkout moved to first, when it was on
            the dropped one (`None` otherwise).
        abandoned: Commit ids that left visible history with it.
        gc_report: The gc that released its pins, branches, and -- with
            `delete_stores` -- the stores created on it.
        plan: The plan that was applied.
    """

    bookmark: str
    left_for: str | None = None
    abandoned: list[str] = field(default_factory=list)
    gc_report: GcReport | None = None
    plan: Plan | None = None


@dataclass
class AbandonReport:
    """What `Repo.abandon` did.

    Attributes:
        abandoned: Commit ids dropped from VCS history.
        gc_plan: What `gc` would release now that those commits are gone (the
            pins only they referenced).
        gc_report: The applied gc, when `gc=True`.
    """

    abandoned: list[str] = field(default_factory=list)
    gc_plan: Plan | None = None
    gc_report: GcReport | None = None


@dataclass
class UndoReport:
    """What `Repo.undo` reversed, could not reverse, and left alone.

    Attributes:
        op: The operation that was undone.
        undo_id: Id of the `undo` entry appended to the op log.
        restored: What was put back (one line each).
        irreversible: What the stores no longer allow to be put back.
        skipped: Parts that no longer applied (e.g. the working copy had moved).
    """

    op: OpEntry
    undo_id: str = ""
    restored: list[str] = field(default_factory=list)
    irreversible: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.irreversible


@dataclass
class DiffEntry:
    """One object's row in `Repo.diff`."""

    key: str
    """Object key."""
    change: str
    """`"added"`, `"removed"`, `"changed"`, or `"unchanged"`."""
    a_pin: str | None = None
    """Pin id on side A."""
    b_pin: str | None = None
    """Pin id on side B."""
    detail: ObjectDiff | None = None
    """Native content diff (only with `content=True` and a `DIFF` backend)."""
    detail_error: str | None = None
    """Backend failure while computing `detail`, if any."""
    why: tuple[str, ...] = ()
    """For `changed`: which of `state`, `pin`, `locator`, `policy` differ. A
    locator or policy change alone is a change too -- the object is addressed
    or governed differently even though its state is the same."""


def _source_object(source: Mapping[str, Any]) -> str | Pin | State:
    """Rebuild a promote source (`{"ref"} | {"pin"} | {"state"}`) from plan params."""
    if "ref" in source:
        return str(source["ref"])
    if "pin" in source:
        return Pin.from_dict(dict(source["pin"]))
    return dict(source["state"])


def short_state(state: State | None) -> str:
    """Compact one-line rendering of a state for plan output."""
    if not state:
        return "?"
    parts = []
    for k, v in state.items():
        text = str(v)
        parts.append(f"{k}={text[:16]}{'…' if len(text) > 16 else ''}")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Repo
# --------------------------------------------------------------------------- #
