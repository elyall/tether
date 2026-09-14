"""The registry operations, as functions of a :class:`~tether.repo.Repo`.

`Repo.export` / `plan_import` / `apply_import` / `import_objects` are thin
delegates to these, imported on first use: importing `tether` never loads the
registry layer, and a reader of `repo.py` sees the core loop only. They use
the engine's private helpers (`_writer_lock`, `_add`, `_log_op`, ...) the way
:func:`build_bundle` always has.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tether.backends.base import absolutize_locator
from tether.errors import ConfigError, StalePlanError
from tether.experimental.registry.export import ExportBundle, build_bundle
from tether.experimental.registry.registry import ImportSpec, specs_from_rows
from tether.manifest import Policy, write_object, write_workspace
from tether.plan import Action, Plan
from tether.repo import _report_dict

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tether.repo import Repo

__all__ = [
    "ImportReport",
    "apply_import",
    "export",
    "import_objects",
    "plan_import",
]


@dataclass
class ImportReport:
    """Result of :func:`apply_import` (`Repo.apply_import`)."""

    added: list[str] = field(default_factory=list)
    """Keys registered."""
    updated: list[str] = field(default_factory=list)
    """Keys whose locator or policy changed (committed state kept)."""
    removed: list[str] = field(default_factory=list)
    """Keys unregistered (`sync=True` only)."""
    unchanged: list[str] = field(default_factory=list)
    """Keys the source listed identically."""
    plan: Plan | None = None


# -- export (manifests -> tables) ------------------------------------ #
def export(
    repo: Repo,
    revs: Sequence[str] | None = None,
    *,
    listings: bool = False,
    workspace: bool = False,
) -> ExportBundle:
    """Derive relational tables from the repository's history.

    The tables (`commits`, `commit_parents`, `refs`, `objects`,
    `object_states`, optional `listings` / `listing_entries` and
    `workspace`) are what `tether export` writes and `tether publish`
    upserts; see `tether.experimental.registry.export.TABLES`.

    Args:
        revs: Revisions to include (jj revsets / git revisions). `None`
            exports every reachable commit.
        listings: Include per-file listings from `.tether/listings/`.
        workspace: Include this checkout's working refs and last snapshot.
    """
    return build_bundle(repo, revs=revs, listings=listings, workspace=workspace)


# -- import (registry rows -> manifests) ----------------------------- #
def plan_import(
    repo: Repo,
    specs: Sequence[ImportSpec],
    *,
    sync: bool = False,
    notes: Sequence[str] = (),
) -> Plan:
    """Diff desired objects against the working tree without writing.

    Actions: `add` for new keys, `update` when a registered key's locator
    or policy differs (its committed state and pin are kept), and, with
    `sync`, `remove` for registered keys the source no longer lists.

    Raises:
        ConfigError: A key's `kind` would change; remove and re-add it
            explicitly instead.
    """
    plan = Plan(
        command="import",
        context={
            "manifest_hash": repo.current_manifest_hash(),
            "sync": sync,
            "rows": len(specs),
        },
        notes=list(notes),
    )
    wanted = {s.key: s for s in specs}
    for key in sorted(wanted):
        spec = wanted[key]
        current = repo.objects.get(key)
        locator = absolutize_locator(
            repo.backend_for(spec.kind), dict(spec.locator), Path.cwd()
        )
        repo.backend_for(spec.kind).validate_locator(locator)
        params = {"locator": locator, "policy": spec.policy.to_dict()}
        if current is None:
            plan.actions.append(
                Action(
                    "add",
                    key,
                    spec.kind,
                    target=str(spec.locator.get("uri", "")),
                    detail="register",
                    params=params,
                )
            )
            continue
        if current.kind != spec.kind:
            raise ConfigError(
                f"{key!r} is registered as {current.kind!r} but the source says "
                f"{spec.kind!r}; remove and re-add it to change kinds"
            )
        changed = []
        if dict(current.locator) != locator:
            changed.append("locator")
        if current.policy != spec.policy:
            changed.append("policy")
        if changed:
            plan.actions.append(
                Action(
                    "update",
                    key,
                    spec.kind,
                    target=str(spec.locator.get("uri", "")),
                    detail=f"{' and '.join(changed)} changed; committed state kept",
                    params=params,
                )
            )
        else:
            plan.notes.append(f"{key}: unchanged")
    if sync:
        for key in sorted(set(repo.objects) - set(wanted)):
            plan.actions.append(
                Action(
                    "remove",
                    key,
                    repo.objects[key].kind,
                    detail="not listed by the source (sync)",
                )
            )
    return plan


def apply_import(repo: Repo, plan: Plan, *, verify: bool = True) -> ImportReport:
    """Write the manifests a `plan_import` plan describes.

    Touches only `.tether/objects/` and the workspace state; commit the
    result with `commit` as usual.

    Raises:
        StalePlanError: The working tree's manifests changed since planning.
    """
    with repo._writer_lock():
        if plan.command != "import":
            raise ConfigError(f"expected an import plan, got {plan.command!r}")
        if verify and plan.context.get("manifest_hash") != repo.current_manifest_hash():
            raise StalePlanError(
                "manifests changed since the plan was made; re-run the plan"
            )
        report = ImportReport(plan=plan)
        for note in plan.notes:
            key, _, why = note.partition(": ")
            if why == "unchanged":
                report.unchanged.append(key)
        pre = {
            "objects": repo._manifest_texts(a.key for a in plan.actions),
            "workspace": repo.workspace.to_toml(),
        }
        for a in plan.actions:
            if a.op == "add":
                repo._add(
                    a.key,
                    a.kind,
                    dict(a.params["locator"]),
                    policy=Policy.from_dict(a.params["policy"]),
                )
                report.added.append(a.key)
            elif a.op == "update":
                current = repo.objects[a.key]
                updated = dataclasses.replace(
                    current,
                    locator=dict(a.params["locator"]),
                    policy=Policy.from_dict(a.params["policy"]),
                )
                backend = repo.backend_for(updated.kind)
                if backend.identity(current.locator) != backend.identity(
                    updated.locator
                ):
                    # Another system: the committed state and pin describe the
                    # old one. The next commit reads the new one afresh.
                    updated = dataclasses.replace(
                        updated, state=None, pin=None, recoverable=True
                    )
                write_object(repo.root, updated)
                repo.objects[a.key] = updated
                if dict(current.locator) != dict(updated.locator):
                    # The working branch lives in the *old* system; keeping it
                    # would send writes there until the next `new`. Drop the
                    # workspace's hold (the branch itself is left for `gc`).
                    repo._forget_working_state(a.key)
                report.updated.append(a.key)
            elif a.op == "remove":
                repo._remove(a.key)
                report.removed.append(a.key)
        write_workspace(repo.root, repo.workspace)
        if plan.actions:
            repo._log_op("import", plan=plan, result=_report_dict(report), pre=pre)
        return report


def import_objects(
    repo: Repo, rows: Iterable[Mapping[str, Any]], *, sync: bool = False
) -> ImportReport:
    """Register / update / remove objects from canonical rows in one step.

    Rows carry `key`, `kind`, and any of `uri`, `locator_json`,
    `policy_file`, `policy_pin`, `at` (see
    `tether.experimental.registry.CANONICAL_COLUMNS`); missing policy fields take
    `config.defaults`. Equivalent to `apply_import(plan_import(...))`.
    """
    # Plan and apply under one lock: planning sees the state the lock
    # refreshed, and nothing in this checkout moves in between.
    with repo._writer_lock():
        specs, notes = specs_from_rows(rows, repo.config.defaults)
        return apply_import(repo, plan_import(repo, specs, sync=sync, notes=notes))
