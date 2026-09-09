"""`tether upgrade`: a v1 (pre-namespace) dataset comes forward, history included."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tether.backends.base import VerifyStatus
from tether.backends.memory import default_store
from tether.errors import ConfigError, StalePlanError
from tether.manifest import (
    ObjectManifest,
    Pin,
    Policy,
    WorkspaceState,
    pin_dataset,
    read_config,
    working_ref_dataset,
    working_ref_workspace,
    write_object,
    write_workspace,
)
from tether.repo import Repo
from tether.vcs import detect_vcs

V1_CONFIG = """[tether]
version = 1

[snapshot]
auto = true

[verify]
on_status = false

[new]
auto_fork = false
fork = "lazy"

[defaults]
write = "fork"
file = "immutable"
pin = "native"
"""


def _v1_dataset(root: Path) -> tuple[str, str, str, str, str]:
    """A dataset as 0.1.0a5..a7 would have left it: 12- and 16-hex pin ids,
    `tether.ws.<ws8>.<slug>` and `tether.ws.<ws8>.<slug>-<key6>` branches, no
    dataset id. Returns (system, s1, s2, old pin 1, old pin 2)."""
    store = default_store()
    system = f"sys-{uuid.uuid4().hex[:8]}"
    sys_ = store.system(system)
    (root / ".tether" / "objects").mkdir(parents=True)
    (root / ".tether" / ".gitignore").write_text("/workspace.toml\n")
    (root / "tether.toml").write_text(V1_CONFIG)
    vcs = detect_vcs(root)
    paths = [".tether/objects", ".tether/.gitignore", "tether.toml"]

    s1 = sys_.branches["main"]
    pin1 = "abcdef012345"  # a1..a5 style: 12 hex
    sys_.tags[f"tether.{pin1}"] = s1
    m = ObjectManifest(
        key="db",
        kind="memory",
        locator={"system": system, "branch": "main"},
        policy=Policy(),
        state={"snapshot_id": s1},
        pin=Pin(id=pin1, ref=f"tether.{pin1}"),
    )
    write_object(root, m)
    vcs.commit(paths, "v1: baseline")

    s2 = store.write(system, "main", {"v": 2})
    pin2 = "0123456789abcdef"  # a6..a7 style: 16 hex, no namespace
    sys_.tags[f"tether.{pin2}"] = s2
    write_object(
        root,
        ObjectManifest(
            key="db",
            kind="memory",
            locator={"system": system, "branch": "main"},
            policy=Policy(),
            state={"snapshot_id": s2},
            pin=Pin(id=pin2, ref=f"tether.{pin2}"),
        ),
    )
    vcs.commit(paths, "v1: second")

    # This workspace's branch (a6 style) and a dead workspace's (a5 style).
    ws = WorkspaceState(workspace_id="7c1e0a4d9b2e4f6a8c1d3e5f7a9b0c2d")
    ws.working_refs["db"] = "tether.ws.7c1e0a4d.db-c0ffee"
    ws.base_states["db"] = {"snapshot_id": s2}
    ws.fork_points["db"] = {"snapshot_id": s2}
    write_workspace(root, ws)
    sys_.branches["tether.ws.7c1e0a4d.db-c0ffee"] = s2
    sys_.branches["tether.ws.deadbeef.db"] = s1
    return system, s1, s2, pin1, pin2


def test_outdated_dataset_is_refused_until_upgraded(vcs_root: Path) -> None:
    _v1_dataset(vcs_root)
    with pytest.raises(ConfigError, match="tether upgrade"):
        Repo.find(vcs_root)
    repo = Repo.find(vcs_root, allow_outdated=True)
    assert repo.config.version == 1 and repo.config.dataset_id == ""


def test_upgrade_v1_to_v2_renames_refs_and_rewrites_history(vcs_root: Path) -> None:
    system, s1, s2, pin1, pin2 = _v1_dataset(vcs_root)
    store = default_store()
    sys_ = store.system(system)
    repo = Repo.find(vcs_root, allow_outdated=True)
    old_revs = repo.vcs.history_revs()

    plan = repo.plan_upgrade()
    ops = sorted((a.op, a.target) for a in plan.actions)
    ds = plan.context["dataset_id"]
    assert plan.context["from"] == 1 and plan.context["to"] == 2
    assert [a.op for a in plan.actions].count("rename-pin") == 2
    assert [a.op for a in plan.actions].count("rename-branch") == 2
    # jj's working-copy commit carries the manifests too (rewritten in place).
    expected = 3 if repo.vcs.kind == "jj" else 2
    assert ("rewrite-history", f"{expected} commit(s)") in ops
    assert any(a.op == "vcs-commit" for a in plan.actions)
    (br_mine,) = [
        a for a in plan.actions if a.op == "rename-branch" and "7c1e0a4d" in a.target
    ]
    (br_dead,) = [
        a for a in plan.actions if a.op == "rename-branch" and "deadbeef" in a.target
    ]
    assert br_mine.target == f"tether.ws.{ds}.7c1e0a4d.db-c0ffee"  # digest kept
    assert br_dead.target.startswith(f"tether.ws.{ds}.deadbeef.db-")  # digest added
    # Nothing has been written by planning.
    assert f"tether.{pin1}" in sys_.tags and repo.config.version == 1

    report = repo.apply_upgrade(plan)
    assert not report.failed, report.failed
    assert report.from_version == 1 and report.to_version == 2
    assert report.vcs_commit
    assert len(report.rewritten_commits) == 2  # the working copy is not "rewritten"
    assert set(report.renamed_pins) == {f"tether.{pin1}", f"tether.{pin2}"}
    assert set(report.renamed_branches) == {
        "tether.ws.7c1e0a4d.db-c0ffee",
        "tether.ws.deadbeef.db",
    }

    # Stores: old names gone, new names point where the old ones did.
    assert f"tether.{pin1}" not in sys_.tags and f"tether.{pin2}" not in sys_.tags
    new1, new2 = (
        report.renamed_pins[f"tether.{pin1}"],
        report.renamed_pins[f"tether.{pin2}"],
    )
    assert sys_.tags[new1] == s1 and sys_.tags[new2] == s2
    assert pin_dataset(new1.removeprefix("tether.")) == ds
    assert "tether.ws.7c1e0a4d.db-c0ffee" not in sys_.branches
    assert sys_.branches[f"tether.ws.{ds}.7c1e0a4d.db-c0ffee"] == s2
    assert sys_.branches[br_dead.target] == s1

    # The dataset opens normally now, at version 2 with the planned id.
    repo = Repo.find(vcs_root)
    assert repo.config.version == 2 and repo.config.dataset_id == ds
    assert read_config(vcs_root).dataset_id == ds
    assert repo.objects["db"].pin is not None
    assert repo.objects["db"].pin.ref == new2
    assert repo.workspace.working_refs["db"] == f"tether.ws.{ds}.7c1e0a4d.db-c0ffee"
    assert working_ref_dataset(repo.workspace.working_refs["db"]) == ds
    assert working_ref_workspace(repo.workspace.working_refs["db"]) == "7c1e0a4d"
    assert not repo.is_stale()

    # History: every commit's manifests carry namespaced pins; the old commits
    # are gone from reachable history.
    revs = repo.vcs.history_revs()
    assert not (set(old_revs) - {"0" * 40}) & set(revs) or repo.vcs.kind == "jj"
    seen_pins = set()
    for _rev, objects in repo._iter_history_objects():
        for m in objects.values():
            if m.pin is not None:
                assert pin_dataset(m.pin.id) == ds, m.pin
                seen_pins.add(m.pin.ref)
    assert seen_pins == {new1, new2}

    # Both sides agree: verify is clean across history, gc finds nothing to unpin.
    reports = repo.verify(all_history=True)
    assert all(r.status is VerifyStatus.OK for r in reports.values()), reports
    gc_plan = repo.plan_gc(prune_workspaces=True)
    assert not [a for a in gc_plan.actions if a.op == "unpin"]
    # The dead workspace's renamed branch is now judged like any other stray.
    (stray,) = [a for a in gc_plan.actions if a.target == br_dead.target]
    assert stray.op == "delete-branch" and "head is pinned" in stray.detail

    # Logged, not undoable, and a second upgrade has nothing to do.
    assert repo.ops()[0].command == "upgrade" and not repo.ops()[0].undoable
    again = repo.plan_upgrade()
    assert again.is_empty and any("already at version 2" in n for n in again.notes)
    with pytest.raises(StalePlanError):
        repo.apply_upgrade(plan)  # made for version 1


def test_upgrade_refuses_uncommitted_manifest_changes(vcs_root: Path) -> None:
    _v1_dataset(vcs_root)
    repo = Repo.find(vcs_root, allow_outdated=True)
    (vcs_root / ".tether" / "objects" / "db.toml").write_text(
        "key = 'db'\nkind = 'memory'\n"
    )
    with pytest.raises(Exception, match="uncommitted changes"):
        repo.apply_upgrade(repo.plan_upgrade())
