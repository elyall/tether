"""Stacked migrations behind `tether upgrade`.

`tether.toml` carries `[tether] version`. Each `Migration` takes a dataset from
version ``n-1`` to ``n``; `tether upgrade` runs every migration newer than the
dataset's version, in order, writing the new version after each one so an
interrupted upgrade resumes where it stopped. A migration is a plan
(read-only: which refs would be renamed, how many commits rewritten) and an
apply.

When a migration changes how native refs are named it renames them in every
store *and* rewrites every historical manifest to match, so `gc` (which
subtracts what history references from what the stores hold) keeps seeing the
same pins from both sides. Rewriting history changes commit ids; every other
clone has to re-sync afterwards.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tether.backends.base import Capability, content_state, effective_capabilities
from tether.errors import TetherError
from tether.manifest import (
    WORKING_REF_PREFIX,
    ObjectManifest,
    Pin,
    canonical_bytes,
    compute_pin_id,
    is_dataset_id,
    key_digest6,
    new_dataset_id,
    pin_dataset,
    ref_for_pin,
    slugify_key,
)
from tether.plan import Action, Plan

if TYPE_CHECKING:
    from tether.repo import Repo

__all__ = ["MIGRATIONS", "Migration", "UpgradeReport", "pending"]


@dataclass
class UpgradeReport:
    """What `Repo.apply_upgrade` did.

    Attributes:
        from_version: `[tether] version` before.
        to_version: `[tether] version` after.
        renamed_pins: Old native ref -> new native ref, per store.
        renamed_branches: Old working branch -> new working branch.
        rewritten_commits: Old commit id -> new commit id (history rewrite).
        refingerprinted: Objects whose manifest state was recomputed (v3).
        rewritten_manifests: Objects whose manifest lost its write policy (v3).
        failed: Target -> why a step did not happen.
        vcs_commit: The commit that records the upgraded working tree.
        plan: The plan that was applied.
    """

    from_version: int = 0
    to_version: int = 0
    renamed_pins: dict[str, str] = field(default_factory=dict)
    renamed_branches: dict[str, str] = field(default_factory=dict)
    rewritten_commits: dict[str, str] = field(default_factory=dict)
    refingerprinted: list[str] = field(default_factory=list)
    rewritten_manifests: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    vcs_commit: str | None = None
    plan: Plan | None = None


@dataclass(frozen=True)
class Migration:
    """One step of `tether upgrade`: version ``n-1`` -> ``n``."""

    version: int
    title: str
    plan: Callable[[Repo, Plan], None]
    apply: Callable[[Repo, Plan, UpgradeReport], None]


def pending(version: int) -> list[Migration]:
    """Migrations a dataset at `version` still needs, oldest first."""
    return [m for m in MIGRATIONS if m.version > version]


# --------------------------------------------------------------------------- #
# v2: namespace native refs by dataset id
# --------------------------------------------------------------------------- #
def _old_pin(m: ObjectManifest) -> bool:
    return m.pin is not None and pin_dataset(m.pin.id) is None


def _new_pin_id(repo: Repo, m: ObjectManifest, dataset_id: str) -> str:
    backend = repo.backend_for(m.kind)
    assert m.state is not None
    content = content_state(backend, m.state)
    assert content is not None
    return compute_pin_id(m.kind, backend.identity(m.locator), content, dataset_id)


def _system_key(repo: Repo, m: ObjectManifest) -> str:
    backend = repo.backend_for(m.kind)
    return f"{m.kind}|{canonical_bytes(backend.identity(m.locator)).decode()}"


def _key6(key: str) -> str:
    return key_digest6(key)


def _is_v1_branch(
    ref: str, slugs: dict[str, str], foreign: frozenset[str] = frozenset()
) -> bool:
    """A pre-namespace working branch: ``tether.ws.<ws8>.<key slug>``.

    Bookmark-named branches of *other* datasets look the same
    (``tether.ws.<ds8>.<bookmark>``), so two checks: the tail must be one of
    this dataset's key slugs (v1 never produced anything else), and the 8-hex
    segment must not be a dataset id seen in that store's pins (``foreign``):
    a dataset that has a bookmark branch there has pinned there too.
    """
    if not ref.startswith(WORKING_REF_PREFIX):
        return False
    ws8, dot, tail = ref[len(WORKING_REF_PREFIX) :].partition(".")
    if not dot or not is_dataset_id(ws8) or "." in tail or ws8 in foreign:
        return False
    if tail in slugs:
        return True  # `tether.ws.<ws8>.<slug>` (a1-a5)
    slug, dash, digest = tail.rpartition("-")  # `<slug>-<key6>` (a6-a7)
    return bool(dash) and slug in slugs and bool(re.fullmatch(r"[0-9a-f]{6}", digest))


def _foreign_dataset_ids(repo: Repo, m: ObjectManifest, ours: str) -> frozenset[str]:
    """Dataset ids other than ``ours`` that have pinned in ``m``'s system.

    Namespaced pins are ``<ds8>.<hash16>``; a bookmark branch
    ``tether.ws.<ds8>.<name>`` whose ``ds8`` is one of these belongs to that
    dataset and is not a v1 branch of this one.
    """
    backend = repo.backend_for(m.kind)
    if Capability.PIN not in backend.capabilities:
        return frozenset()
    try:
        pins = backend.list_pins(m.locator)
    except TetherError:
        return frozenset()
    ids: set[str] = set()
    for pin_id in pins:
        ds, dot, _rest = pin_id.partition(".")
        if dot and is_dataset_id(ds) and ds != ours:
            ids.add(ds)
    return frozenset(ids)


def _new_branch_name(ref: str, dataset_id: str, slugs: dict[str, str]) -> str:
    """``tether.ws.<ws8>.<tail>`` -> ``tether.ws.<ds8>.<ws8>.<tail>[-<key6>]``."""
    rest = ref[len(WORKING_REF_PREFIX) :]
    ws8, _, tail = rest.partition(".")
    key = slugs.get(tail)
    if key is not None:  # a pre-key-digest name: add the digest too
        tail = f"{tail}-{_key6(key)}"
    return f"{WORKING_REF_PREFIX}{dataset_id}.{ws8}.{tail}"


def _plan_v2(repo: Repo, plan: Plan) -> None:
    # A stopped upgrade has already written the id it renamed under; a fresh
    # one gets a new id. Either way the plan carries it, and apply persists it
    # before the first store rename so a re-run continues under the same id.
    dataset_id = plan.context.setdefault(
        "dataset_id", repo.config.dataset_id or new_dataset_id()
    )
    plan.notes.append(f"dataset id: {dataset_id}")

    # Every pin history (or the working tree) names in the old format.
    renames: dict[tuple[str, str], tuple[ObjectManifest, str]] = {}
    commits = 0
    for _rev, objects in repo._iter_history_objects():
        old = [m for m in objects.values() if _old_pin(m)]
        if old:
            commits += 1
        for m in old:
            assert m.pin is not None
            renames.setdefault(
                (_system_key(repo, m), m.pin.id), (m, _new_pin_id(repo, m, dataset_id))
            )
    for m in repo.objects.values():
        if _old_pin(m):
            assert m.pin is not None
            renames.setdefault(
                (_system_key(repo, m), m.pin.id), (m, _new_pin_id(repo, m, dataset_id))
            )

    live: dict[str, set[str]] = {}
    for (sys_key, old_id), (m, new_id) in sorted(renames.items()):
        assert m.pin is not None
        backend = repo.backend_for(m.kind)
        if sys_key not in live:
            try:
                live[sys_key] = (
                    backend.list_pins(m.locator)
                    if Capability.PIN in backend.capabilities
                    else set()
                )
            except TetherError as exc:
                live[sys_key] = set()
                plan.notes.append(f"{m.key}: could not list pins ({exc})")
        if old_id not in live[sys_key]:
            plan.notes.append(
                f"{m.key}: pin {m.pin.ref} is not in the store; manifests are "
                "rewritten to the new name only"
            )
            continue
        plan.actions.append(
            Action(
                "rename-pin",
                m.key,
                m.kind,
                target=ref_for_pin(new_id),
                detail=f"from {m.pin.ref}",
                params={
                    "locator": m.locator,
                    "old": m.pin.to_dict(),
                    "state": m.state,
                    "new_id": new_id,
                    "migration": 2,
                },
            )
        )

    # Working branches in the old format, in every store the working tree names.
    slugs = {slugify_key(k): k for k in repo.objects}
    seen_systems: set[str] = set()
    for key, m in sorted(repo.objects.items()):
        backend = repo.backend_for(m.kind)
        if Capability.FORK not in effective_capabilities(backend, m.locator, m.policy):
            continue
        sys_key = _system_key(repo, m)
        if sys_key in seen_systems:
            continue
        seen_systems.add(sys_key)
        try:
            refs = backend.list_working_refs(m.locator)
        except TetherError as exc:
            plan.notes.append(f"{key}: could not list working branches ({exc})")
            continue
        foreign = _foreign_dataset_ids(repo, m, dataset_id)
        for ref in sorted(refs):
            if not _is_v1_branch(ref, slugs, foreign):
                continue
            plan.actions.append(
                Action(
                    "rename-branch",
                    key,
                    m.kind,
                    target=_new_branch_name(ref, dataset_id, slugs),
                    detail=f"from {ref}",
                    params={"locator": m.locator, "old": ref, "migration": 2},
                )
            )

    if commits:
        plan.actions.append(
            Action(
                "rewrite-history",
                target=f"{commits} commit(s)",
                detail="manifests with old-format pins get the new names; "
                "commit ids change, every clone must re-sync",
                params={"migration": 2},
            )
        )
    plan.actions.append(
        Action(
            "vcs-commit",
            target="tether upgrade: v1 -> v2 (dataset-namespaced refs)",
            detail="tether.toml gets [dataset] id and version = 2",
            params={"migration": 2},
        )
    )


def _apply_v2(repo: Repo, plan: Plan, report: UpgradeReport) -> None:
    from tether.manifest import (
        read_objects,
        write_config,
        write_object,
        write_workspace,
    )

    dataset_id = str(plan.context["dataset_id"])
    mine = [a for a in plan.actions if a.params.get("migration") == 2]
    if repo.config.dataset_id and repo.config.dataset_id != dataset_id:
        raise TetherError(
            f"this dataset already renamed refs under id {repo.config.dataset_id}; "
            f"the plan carries {dataset_id} -- re-run the plan"
        )

    # 0. Persist the namespace first: every rename below is made under it, and
    # a re-run after a failure must use the same one.
    if repo.config.dataset_id != dataset_id:
        repo.config.dataset_id = dataset_id
        write_config(repo.root, repo.config)

    # 1. Stores: rename pins, then working branches. Skip what already happened.
    for a in mine:
        if a.op == "rename-pin":
            backend = repo.backend_for(a.kind)
            locator = dict(a.params["locator"])
            old = Pin(**a.params["old"])
            new_id = str(a.params["new_id"])
            try:
                live = backend.list_pins(locator)
                if old.id not in live and new_id in live:
                    report.renamed_pins[old.ref] = ref_for_pin(new_id)  # already done
                    continue
                new = backend.rename_pin(locator, old, dict(a.params["state"]), new_id)
                report.renamed_pins[old.ref] = new.ref
            except TetherError as exc:
                report.failed[f"rename-pin {old.ref}"] = str(exc)
        elif a.op == "rename-branch":
            backend = repo.backend_for(a.kind)
            locator = dict(a.params["locator"])
            old = str(a.params["old"])
            try:
                refs = backend.list_working_refs(locator)
                if old not in refs and a.target in refs:
                    report.renamed_branches[old] = a.target
                    continue
                report.renamed_branches[old] = backend.rename_working_ref(
                    locator, old, a.target
                )
            except TetherError as exc:
                report.failed[f"rename-branch {old}"] = str(exc)

    # Fail closed: if any rename did not happen, leave the manifests naming the
    # old refs. Rewriting them now would make history point at names the store
    # does not have while the old ones -- outside the namespace -- became
    # invisible to gc. Re-running the upgrade skips the renames already done.
    if report.failed:
        raise TetherError(
            "upgrade stopped before rewriting history: "
            f"{len(report.failed)} store rename(s) failed:\n"
            + "\n".join(f"  {k}: {v}" for k, v in sorted(report.failed.items()))
            + "\nfix the cause and re-run `tether upgrade` (renames already made "
            "are skipped)"
        )

    # 2. History: every manifest with an old-format pin gets the new name.
    def transform(_commit: str, files: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for path, text in files.items():
            if not path.endswith(".toml"):
                out[path] = text
                continue
            m = ObjectManifest.from_toml(text)
            if not _old_pin(m):
                out[path] = text
                continue
            new_id = _new_pin_id(repo, m, dataset_id)
            out[path] = dataclasses.replace(
                m, pin=Pin(id=new_id, ref=ref_for_pin(new_id))
            ).to_toml()
        return out

    if any(a.op == "rewrite-history" for a in mine):
        report.rewritten_commits.update(
            repo.vcs.rewrite_history(
                repo._objects_reldir(),
                transform,
                ignore_immutable=bool(plan.context.get("ignore_immutable")),
            )
        )
        repo._manifest_cache.clear()

    # 3. Working tree, workspace, config.
    for key, m in list(repo.objects.items()):
        if _old_pin(m):
            new_id = _new_pin_id(repo, m, dataset_id)
            updated = dataclasses.replace(
                m, pin=Pin(id=new_id, ref=ref_for_pin(new_id))
            )
            write_object(repo.root, updated)
            repo.objects[key] = updated
    repo.objects = read_objects(repo.root)
    # Same transform as the store renames, so the two keep agreeing.
    slugs = {slugify_key(k): k for k in repo.objects}
    for table in ("working_refs", "pending_forks"):
        refs: dict[str, str] = getattr(repo.workspace, table)
        for key, ref in list(refs.items()):
            if _is_v1_branch(ref, slugs):
                refs[key] = _new_branch_name(ref, dataset_id, slugs)
    write_workspace(repo.root, repo.workspace)
    repo.config.version = 2
    write_config(repo.root, repo.config)


# --------------------------------------------------------------------------- #
# v3: local file states are content hashes; the write policy is gone
# --------------------------------------------------------------------------- #
def _local_file_objects(repo: Repo) -> list[ObjectManifest]:
    """Working-tree `file` objects on local disk with a committed state."""
    return [
        m
        for m in repo.objects.values()
        if m.kind == "file"
        and m.state is not None
        and m.state.get("type") in ("file", "dir")
    ]


_WRITE_LINE = re.compile(r'^write = "(?:fork|track|direct)"\n', re.M)


def _manifests_with_write(repo: Repo) -> list[str]:
    """Keys of working-tree manifests that still carry a ``write`` policy."""
    from tether.manifest import objects_dir, relpath_to_key

    root = objects_dir(repo.root)
    return [
        relpath_to_key(path.relative_to(root))
        for path in sorted(root.rglob("*.toml"))
        if _WRITE_LINE.search(path.read_text(encoding="utf-8"))
    ]


def _plan_v3(repo: Repo, plan: Plan) -> None:
    for key in _manifests_with_write(repo):
        plan.actions.append(
            Action(
                "rewrite-manifest",
                key,
                detail="drop the write policy: whether writes fork or land upstream "
                "is now the bookmark the working copy is on, not a per-object "
                "setting",
                params={"migration": 3},
            )
        )
    for m in _local_file_objects(repo):
        assert m.state is not None
        if m.state.get("type") == "file" and "sha256" in m.state:
            plan.notes.append(f"{m.key}: already content-hashed")
            continue
        what = "file" if m.state.get("type") == "file" else "directory"
        plan.actions.append(
            Action(
                "refingerprint",
                m.key,
                m.kind,
                target=str(m.locator.get("uri") or m.locator.get("path", "")),
                detail=f"re-read the {what} and record its content hash in place of "
                "the mtime-based state (reads every byte once; the stat cache "
                "remembers it)",
                params={"migration": 3},
            )
        )
    if not any(a.params.get("migration") == 3 for a in plan.actions):
        plan.notes.append(
            "v3: no local file objects to re-fingerprint and no manifests carrying "
            "a write policy"
        )
    if repo.workspace.bookmark is None:
        plan.notes.append(
            f"v3: this workspace will work on the trunk bookmark "
            f"{repo.config.trunk!r} (created if the VCS has none), or on the "
            "bookmark its working copy is on"
        )
    plan.actions.append(
        Action(
            "vcs-commit",
            target="tether upgrade: v2 -> v3 (content-hashed file states; no write "
            "policy)",
            detail="manifests of local file objects get {size, sha256} states and "
            "the write policy line goes; history keeps the old forms (it recorded "
            "what the bytes were then, and a write key is read and ignored)",
            params={"migration": 3},
        )
    )


def _apply_v3(repo: Repo, plan: Plan, report: UpgradeReport) -> None:
    from tether.manifest import (
        listing_name,
        objects_dir,
        read_objects,
        relpath_to_key,
        write_config,
        write_listing,
        write_object,
        write_workspace,
    )

    root = objects_dir(repo.root)
    for path in sorted(root.rglob("*.toml")):
        text = path.read_text(encoding="utf-8")
        if _WRITE_LINE.search(text):
            path.write_text(_WRITE_LINE.sub("", text), "utf-8")
            report.rewritten_manifests.append(relpath_to_key(path.relative_to(root)))
    if report.rewritten_manifests:
        repo.objects = read_objects(repo.root)

    backend = repo.backend_for("file")
    for a in plan.actions:
        if a.op != "refingerprint" or a.params.get("migration") != 3:
            continue
        m = repo.objects[a.key]
        try:
            state = backend.fingerprint(m.locator, None)
        except TetherError as exc:
            report.failed[f"refingerprint {a.key}"] = str(exc)
            continue
        updated = dataclasses.replace(m, state=state)
        write_object(repo.root, updated)
        repo.objects[a.key] = updated
        text = backend.listing(m.locator, state)
        if text is not None:
            content = content_state(backend, state)
            assert content is not None
            write_listing(
                repo.root,
                listing_name(m.kind, backend.identity(m.locator), content),
                text,
            )
        report.refingerprinted.append(a.key)
    if report.failed:
        raise TetherError(
            "upgrade stopped: could not re-fingerprint "
            + ", ".join(sorted(k.split(" ", 1)[1] for k in report.failed))
            + " (the paths must exist and be readable); fix and re-run `tether upgrade`"
        )
    # Before bookmarks, a workspace had no notion of which one it worked on.
    # Put it where `init` would: on the trunk (created if the VCS has no such
    # bookmark), or on the single bookmark the VCS working copy is on.
    if repo.workspace.bookmark is None:
        repo.workspace.bookmark = repo.adopt_trunk()
        write_workspace(repo.root, repo.workspace)
    repo.config.version = 3
    write_config(repo.root, repo.config)


MIGRATIONS: list[Migration] = [
    Migration(
        version=2,
        title="Namespace native refs by dataset id",
        plan=_plan_v2,
        apply=_apply_v2,
    ),
    Migration(
        version=3,
        title="Content-hash local file states; drop the write policy",
        plan=_plan_v3,
        apply=_apply_v3,
    ),
]
"""Every migration, oldest first. Append here when the format changes."""
