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
    # Another dataset's bookmark branch, named like one of our key slugs, in
    # the same store -- and that dataset's pin, which is how we tell.
    sys_.branches["tether.ws.0ther0ds.db"] = s1
    sys_.tags["tether.0ther0ds.0123456789abcdef"] = s1
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
    assert plan.context["from"] == 1 and plan.context["to"] == 3
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
    assert not any("0ther0ds" in a.target for a in plan.actions)  # not ours
    # Nothing has been written by planning.
    assert f"tether.{pin1}" in sys_.tags and repo.config.version == 1

    report = repo.apply_upgrade(plan)
    assert not report.failed, report.failed
    assert report.from_version == 1 and report.to_version == 3
    assert report.vcs_commit
    assert len(report.rewritten_commits) == 2  # the working copy is not "rewritten"
    assert set(report.renamed_pins) == {f"tether.{pin1}", f"tether.{pin2}"}
    assert set(report.renamed_branches) == {
        "tether.ws.7c1e0a4d.db-c0ffee",
        "tether.ws.deadbeef.db",
    }

    # Stores: old names gone, new names point where the old ones did; the
    # other dataset's bookmark branch and pin are untouched.
    assert f"tether.{pin1}" not in sys_.tags and f"tether.{pin2}" not in sys_.tags
    assert sys_.branches["tether.ws.0ther0ds.db"] == s1
    assert "tether.0ther0ds.0123456789abcdef" in sys_.tags
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
    assert repo.config.version == 3 and repo.config.dataset_id == ds
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
    gc_plan = repo.plan_gc(prune_bookmarks=True)
    assert not [a for a in gc_plan.actions if a.op == "unpin"]
    # The dead workspace's renamed branch is now judged like any other stray.
    (stray,) = [a for a in gc_plan.actions if a.target == br_dead.target]
    assert stray.op == "delete-branch" and "head is pinned" in stray.detail

    # Logged, not undoable, and a second upgrade has nothing to do.
    assert repo.ops()[0].command == "upgrade" and not repo.ops()[0].undoable
    again = repo.plan_upgrade()
    assert again.is_empty and any("already at version 3" in n for n in again.notes)
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


def test_upgrade_stops_before_rewriting_history_when_a_rename_fails(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tether.errors import BackendError, TetherError

    system, _s1, _s2, pin1, pin2 = _v1_dataset(vcs_root)
    sys_ = default_store().system(system)
    repo = Repo.find(vcs_root, allow_outdated=True)
    parent = "@-" if repo.vcs.kind == "jj" else "HEAD"
    tip_before = repo.vcs.resolve(parent)
    backend = repo.backend_for("memory")
    real = backend.rename_pin

    def flaky(locator, old, state, new_id):
        if old.id == pin2:
            raise BackendError("store unavailable", kind="memory")
        return real(locator, old, state, new_id)

    monkeypatch.setattr(backend, "rename_pin", flaky)
    plan = repo.plan_upgrade()
    with pytest.raises(TetherError, match="stopped before rewriting history") as exc:
        repo.apply_upgrade(plan)
    assert f"rename-pin tether.{pin2}" in str(exc.value)
    # The rename that worked is done; nothing else moved: history untouched,
    # manifests (in history and the working tree) still name the old pins.
    assert f"tether.{pin1}" not in sys_.tags and f"tether.{pin2}" in sys_.tags
    assert repo.vcs.resolve(parent) == tip_before
    assert {
        m.pin.id for _r, o in repo._iter_history_objects() for m in o.values() if m.pin
    } == {pin1, pin2}
    cfg = read_config(vcs_root)
    assert cfg.version == 1 and cfg.dataset_id == plan.context["dataset_id"]
    current = Repo.find(vcs_root, allow_outdated=True).objects["db"].pin
    assert current is not None and current.id == pin2
    assert repo.ops()[0].command == "upgrade" and repo.ops()[0].result["stopped"]

    # Re-running once the store is back finishes the job; the earlier rename
    # is recognised and skipped.
    monkeypatch.setattr(backend, "rename_pin", real)
    repo = Repo.find(vcs_root, allow_outdated=True)
    plan2 = repo.plan_upgrade()
    assert plan2.context["dataset_id"] == plan.context["dataset_id"]  # same namespace
    assert any(f"pin tether.{pin1} is not in the store" in n for n in plan2.notes)
    report = repo.apply_upgrade(plan2)
    assert not report.failed and report.to_version == 3
    assert f"tether.{pin2}" not in sys_.tags
    assert set(report.renamed_pins) == {f"tether.{pin2}"}  # pin1 was done last time
    repo = Repo.find(vcs_root)
    assert all(r.status.value == "ok" for r in repo.verify(all_history=True).values())


V2_CONFIG = """[tether]
version = 2

[dataset]
id = "0a1b2c3d"

[snapshot]
auto = false

[verify]
on_status = false

[new]
fork = "lazy"

[defaults]
write = "fork"
file = "immutable"
pin = "native"
"""


def test_upgrade_v2_to_v3_rehashes_local_file_states(vcs_root: Path) -> None:
    """a8 recorded local files by mtime; the hash-based fingerprint must not read
    an untouched file as an immutable-object change after upgrading."""
    import hashlib
    import json

    from tether.manifest import listing_name, write_listing

    data = vcs_root / "data"
    data.mkdir()
    (data / "a.bin").write_bytes(b"aaaa")
    single = vcs_root / "single.bin"
    single.write_bytes(b"1234")
    (vcs_root / ".tether" / "objects").mkdir(parents=True)
    (vcs_root / ".tether" / ".gitignore").write_text("/workspace.toml\n/ops.jsonl\n")
    (vcs_root / "tether.toml").write_text(V2_CONFIG)
    st = single.stat()
    write_object(
        vcs_root,
        ObjectManifest(
            key="raw/single",
            kind="file",
            locator={"uri": str(single)},
            policy=Policy(),
            state={"type": "file", "size": st.st_size, "mtime_ns": st.st_mtime_ns},
            recoverable=False,
        ),
    )
    a_st = (data / "a.bin").stat()
    old_rows = {"a.bin": (f"{a_st.st_size}:{a_st.st_mtime_ns}", a_st.st_size)}
    old_listing = "".join(
        json.dumps({"p": p_, "k": k, "s": s_}, separators=(",", ":")) + "\n"
        for p_, (k, s_) in old_rows.items()
    )
    old_digest = "0" * 32  # what a7 would have computed from the mtime tokens
    write_object(
        vcs_root,
        ObjectManifest(
            key="raw/dir",
            kind="file",
            locator={"uri": str(data)},
            policy=Policy(),
            state={"type": "dir", "count": 1, "size": 4, "digest": old_digest},
            recoverable=False,
        ),
    )
    write_listing(
        vcs_root,
        listing_name(
            "file",
            {"uri": str(data)},
            {"type": "dir", "count": 1, "size": 4, "digest": old_digest},
        ),
        old_listing,
    )
    # A remote object keeps its etag state untouched by the migration.
    write_object(
        vcs_root,
        ObjectManifest(
            key="raw/remote",
            kind="file",
            locator={"uri": "s3://bucket/k"},
            policy=Policy(),
            state={"type": "object", "size": 1, "etag": "e"},
            recoverable=False,
        ),
    )
    vcs = detect_vcs(vcs_root)
    vcs.commit(
        [".tether/objects", ".tether/.gitignore", ".tether/listings", "tether.toml"],
        "v2: files by mtime",
    )

    with pytest.raises(ConfigError, match="tether upgrade"):
        Repo.find(vcs_root)
    repo = Repo.find(vcs_root, allow_outdated=True)
    plan = repo.plan_upgrade()
    assert plan.context["from"] == 2 and plan.context["to"] == 3
    ops = [(a.op, a.key) for a in plan.actions]
    assert ("refingerprint", "raw/single") in ops and (
        "refingerprint",
        "raw/dir",
    ) in ops
    assert not any(k == "raw/remote" for _op, k in ops)
    assert not any(
        a.op in ("rename-pin", "rename-branch", "rewrite-history") for a in plan.actions
    )

    report = repo.apply_upgrade(plan)
    assert not report.failed and report.to_version == 3
    assert sorted(report.refingerprinted) == ["raw/dir", "raw/single"]
    assert report.vcs_commit

    repo = Repo.find(vcs_root)
    single_state = repo.objects["raw/single"].state
    assert single_state == {
        "type": "file",
        "size": 4,
        "sha256": hashlib.sha256(b"1234").hexdigest(),
    }
    dir_state = repo.objects["raw/dir"].state
    assert dir_state is not None and dir_state["digest"] != old_digest
    assert repo.objects["raw/remote"].state == {
        "type": "object",
        "size": 1,
        "etag": "e",
    }
    # The thing the migration exists for: an untouched immutable file is clean
    # even after its mtime moves (the remote object is skipped: no network here).
    import os

    os.utime(single, ns=(1, 1))
    backend = repo.backend_for("file")
    assert backend.fingerprint({"uri": str(single)}, None) == single_state
    assert backend.fingerprint({"uri": str(data)}, None) == dir_state
    assert repo.plan_upgrade().is_empty


def test_upgrade_v3_drops_the_write_policy(vcs_root: Path) -> None:
    """The v3 migration removes the `write` policy line: whether writes fork
    or land upstream is the bookmark now. A v2 dataset with no local files
    and one such manifest upgrades on that alone."""
    (vcs_root / ".tether" / "objects").mkdir(parents=True)
    (vcs_root / ".tether" / ".gitignore").write_text(
        "/workspace.toml\n/ops.jsonl\n/cache\n"
    )
    (vcs_root / "tether.toml").write_text(V2_CONFIG)
    system = f"sys-{uuid.uuid4().hex[:8]}"
    default_store().system(system)
    text = ObjectManifest(
        key="db/prod",
        kind="memory",
        locator={"system": system, "branch": "main"},
        policy=Policy(),
    ).to_toml()
    text = text.replace('file = "immutable"', 'write = "track"\nfile = "immutable"')
    assert 'write = "track"' in text
    (vcs_root / ".tether" / "objects" / "db").mkdir()
    (vcs_root / ".tether" / "objects" / "db" / "prod.toml").write_text(text)
    vcs = detect_vcs(vcs_root)
    vcs.commit([".tether/objects", ".tether/.gitignore", "tether.toml"], "v2: track")

    with pytest.raises(ConfigError, match="tether upgrade"):
        Repo.find(vcs_root)
    repo = Repo.find(vcs_root, allow_outdated=True)
    plan = repo.plan_upgrade()
    assert [(a.op, a.key) for a in plan.actions if a.op == "rewrite-manifest"] == [
        ("rewrite-manifest", "db/prod")
    ]
    assert repo.workspace.bookmark is None
    assert any("trunk bookmark" in n for n in plan.notes)
    report = repo.apply_upgrade(plan)
    assert report.rewritten_manifests == ["db/prod"] and report.to_version == 3
    repo = Repo.find(vcs_root)
    assert "write" not in (vcs_root / ".tether/objects/db/prod.toml").read_text()
    assert repo.objects["db/prod"].policy == Policy()
    assert repo.plan_upgrade().is_empty
    # The upgraded working copy works on the trunk, as a fresh init would.
    assert repo.workspace.bookmark == "main" and "main" in repo.vcs.bookmarks()
    assert repo.on_trunk()  # ...so commit works without a `tether new` first
    assert [a.op for a in repo.plan_commit("first pin").actions if a.key] == ["pin"]
