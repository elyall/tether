"""Bring an alpha-format dataset to the current `tether.toml` version.

**Removed at 0.1.0.** Every dataset made by a 0.1.0aN release can be brought
to the 0.1.0b1 format by `tether upgrade` from any 0.1.0 beta. The first
non-pre-release drops this package: `CONFIG_VERSION` stays where the last
beta left it, and a dataset whose `[tether] version` is older fails at open
with :func:`outdated_message` pointing at the last beta. A user on an alpha
dataset then installs that beta (`pip install "tether-vcs==0.1.0b<N>"`),
runs `tether upgrade`, and installs the release.

Everything alpha-format-specific lives here so `repo.py` and `vcs.py` read
without it; `Repo.plan_upgrade` / `apply_upgrade` / `upgrade` are thin
delegates that import this package on first use.

Removal checklist (for the 0.1.0 release):

- delete `tether/upgrade/` and the `upgrade` CLI command
- make `Repo.__init__` raise :func:`outdated_message`'s text for
  `version < CONFIG_VERSION` directly (a two-line `ConfigError`)
- delete `tests/upgrade/`
- delete the legacy per-workspace working-ref parsing
  (`manifest.working_ref_workspace`, the ``_working_ref_parts`` legacy
  branch) and gc's legacy-branch judgement in `repo.py`
- drop `UpgradeReport` from `tether.__all__`

Not part of the removal: `abandon` and the two `rewrite_history`
implementations in `vcs.py` (jj and git) it relies on. `abandon` drops dataset
commits while keeping every later commit's manifests exactly as they were
(snapshot, not patch, semantics), which "jj abandon, then tether gc" cannot
do -- a plain rebase would re-apply the dropped commit's manifest changes as
a diff. That is why the history rewriters outlive this package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tether import manifest as _m
from tether.errors import TetherError
from tether.manifest import CONFIG_VERSION, ensure_ignored, write_config
from tether.oplog import report_dict
from tether.plan import Plan
from tether.upgrade.migrations import (
    MIGRATIONS,
    Migration,
    UpgradeReport,
    pending,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tether.repo import Repo

__all__ = [
    "LAST_BETA_WITH_UPGRADE",
    "MIGRATIONS",
    "Migration",
    "UpgradeReport",
    "apply_upgrade",
    "outdated_message",
    "pending",
    "plan_upgrade",
    "upgrade",
]

LAST_BETA_WITH_UPGRADE = "0.1.0b3"
"""The newest release that can upgrade alpha datasets; bump with each beta
that still ships this package. The 0.1.0 message names it."""


def outdated_message(version: int) -> str:
    """Why a dataset at `version` cannot be opened, and what to do."""
    steps = ", ".join(f"v{m.version} {m.title}" for m in pending(version))
    return (
        f"tether.toml is version {version}; this tether expects {CONFIG_VERSION}. "
        f"Run `tether upgrade --dry-run`, then `tether upgrade` (pending: {steps}). "
        f"The alpha upgrade path is removed at 0.1.0: from then on, install "
        f"tether-vcs=={LAST_BETA_WITH_UPGRADE}, upgrade there, and reinstall"
    )


def plan_upgrade(repo: Repo, *, ignore_immutable: bool = False) -> Plan:
    """Compute what bringing this dataset to the current version would do.

    Runs the plan step of every pending `tether.upgrade.migrations.Migration`, in
    order. Actions: `rename-pin` / `rename-branch` (native refs in the
    stores), `rewrite-history` (historical manifests get the new names),
    `vcs-commit`. Nothing is written.

    Args:
        ignore_immutable: Let the history rewrite touch commits jj marks
            immutable (recorded in the plan; applied by `apply_upgrade`).
    """
    steps = pending(repo.config.version)
    plan = Plan(
        command="upgrade",
        context={
            "from": repo.config.version,
            "to": CONFIG_VERSION,
            "ignore_immutable": ignore_immutable,
            "steps": [f"v{m.version}: {m.title}" for m in steps],
        },
    )
    plan.require(
        "config_version",
        repo.config.version,
        detail=f"plan was made for version {repo.config.version}, the dataset is "
        "at {observed}; re-run the plan",
    )
    if not steps:
        plan.notes.append(f"already at version {repo.config.version}")
        return plan
    for m in steps:
        m.plan(repo, plan)
    return plan


def apply_upgrade(repo: Repo, plan: Plan) -> UpgradeReport:
    """Execute a plan from `plan_upgrade`.

    One migration brings any alpha format to the current version; its
    parts run on what the dataset shows, `tether.toml` records the version
    once at the end, and one VCS commit lands it. A part that renames
    native refs fails *closed*: if any store rename fails, it stops before
    rewriting history or the manifests, so both sides keep naming the old
    refs; the renames that did succeed are logged and skipped on the next
    run. Rewriting history changes commit ids: every other clone must
    re-sync.

    Raises:
        ConfigError: Not an upgrade plan, or the dataset's version differs
            from the plan's.
        TetherError: The dataset's manifests have uncommitted changes, or a
            store rename failed (nothing else was changed).
    """
    with repo._writer_lock():
        repo._verify_plan(plan, "upgrade")
        report = UpgradeReport(
            from_version=repo.config.version, to_version=CONFIG_VERSION, plan=plan
        )
        steps = pending(repo.config.version)
        if not steps:
            return report
        # Manifests must be committed. tether.toml and .tether/.gitignore may be
        # dirty for tether's own reasons (a stopped upgrade wrote the dataset
        # id; opening the dataset taught it about the op log).
        rel = repo._dataset_rel()
        if repo.vcs.dirty([(rel / _m.TETHER_DIR / _m.OBJECTS_DIR).as_posix()]):
            raise TetherError(
                "the dataset's manifests have uncommitted changes; commit or "
                "restore them before upgrading"
            )
        for m in steps:
            try:
                m.apply(repo, plan, report)
            except TetherError:
                # Record what did happen (store renames), then surface the stop.
                if report.renamed_pins or report.renamed_branches:
                    repo._log_op(
                        "upgrade",
                        plan=plan,
                        result={**report_dict(report), "stopped": True},
                    )
                raise
        write_config(repo.root, repo.config)
        ensure_ignored(repo.root)  # every untracked file this version knows
        if repo.vcs.dirty(repo._vcs_paths()):
            report.vcs_commit = repo.vcs.commit(
                repo._vcs_paths(),
                f"tether upgrade: v{report.from_version} -> v{report.to_version}",
            )
        repo._log_op("upgrade", plan=plan, result=report_dict(report))
        return report


def upgrade(repo: Repo, *, ignore_immutable: bool = False) -> UpgradeReport:
    """Bring the dataset to this tether's version.

    Equivalent to `apply_upgrade(plan_upgrade(...))`.
    """
    return apply_upgrade(repo, plan_upgrade(repo, ignore_immutable=ignore_immutable))
