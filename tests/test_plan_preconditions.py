"""The drift contract in one place: `Plan.preconditions` and `Repo._verify_plan`.

Every `plan_*` records what it saw; every `apply_*` runs `_verify_plan`
first and refuses with `StalePlanError` on the first mismatch. These tests
exercise each precondition kind directly, and the plan format that carries
them.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from tether.backends.memory import default_store
from tether.errors import ConfigError, StalePlanError
from tether.manifest import CONFIG_VERSION, Pin, compute_pin_id
from tether.plan import (
    PLAN_FORMAT,
    PRECONDITION_KINDS,
    REQUIRED_PRECONDITIONS,
    Plan,
    Precondition,
)
from tether.repo import Repo


def _mem(repo: Repo, key: str = "db") -> str:
    store = default_store()
    name = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(name)
    store.write(name, "main", {"a": 1})
    repo.add(key, "memory", {"system": name, "branch": "main"})
    return name


def test_plan_round_trips_preconditions_and_refuses_format_1() -> None:
    plan = Plan(command="gc")
    plan.require("manifest_hash", "abc", detail="manifests changed")
    plan.require("ref_head", {"snapshot_id": "s1"}, key="db", backend="memory", ref="r")
    data = json.loads(plan.to_json())
    assert data["format"] == PLAN_FORMAT == 2
    again = Plan.from_dict(data)
    assert again.preconditions == plan.preconditions
    assert again.preconditions[1].params == {"backend": "memory", "ref": "r"}
    with pytest.raises(ValueError, match="unknown precondition kind"):
        plan.require("not-a-kind")
    with pytest.raises(ConfigError, match="unknown plan precondition"):
        Precondition.from_dict({"kind": "nope"})
    # A plan saved before 0.1.0b1 carries no preconditions: refused, re-plan.
    data["format"] = 1
    with pytest.raises(StalePlanError, match=r"format 1 predates 0\.1\.0b1"):
        Plan.from_dict(data)
    data["format"] = 99
    with pytest.raises(ConfigError, match="unsupported plan format"):
        Plan.from_dict(data)


def test_every_command_plan_carries_its_preconditions(vcs_root: Path) -> None:
    """The plan-level checks each command used to re-implement inline are
    now declared on the plan, where a reviewer of a saved plan can see them."""
    repo = Repo.init(vcs_root)
    _mem(repo)
    kinds = lambda plan: sorted(p.kind for p in plan.preconditions)  # noqa: E731
    assert kinds(repo.plan_commit("m")) == [
        "manifest_hash",
        "workspace_bookmark",
        "workspace_id",
    ]
    c1 = repo.commit("baseline").vcs_commit
    assert c1 is not None
    assert {"manifest_hash", "history_digest", "workspace_id"} <= set(
        kinds(repo.plan_gc())
    )
    assert set(kinds(repo.plan_repair())) == {"workspace_id", "history_digest"}
    new = repo.plan_new(bookmark="work", eager=True)
    assert set(kinds(new)) == {
        "manifest_hash",
        "workspace_id",
        "no_new_holders",
        "ref_absent",
    }
    repo.new(bookmark="work", eager=True)
    default_store().write(_system_of(repo), repo.workspace.working_refs["db"], {"b": 2})
    repo.commit("b")  # promote lands committed states only
    promote = repo.plan_promote(["db"])
    assert kinds(promote) == [
        "base_state",
        "bookmark_head",
        "manifest_hash",
        "ref_head",
        "workspace_bookmark",
        "workspace_id",
    ]
    assert promote.context["bookmark"] == "work"
    assert promote.context["bookmark_commit"] == repo.vcs.bookmarks()["work"]
    restore = repo.plan_restore(["db"], c1, discard=True)  # the branch has writes
    assert kinds(restore) == [
        "manifest_hash",
        "ref_head",
        "workspace_bookmark",
        "workspace_id",
    ]
    assert kinds(repo.plan_upgrade()) == ["config_version"]
    # Every plan carries what its command requires (nothing is "missing").
    for plan in (repo.plan_commit("m"), repo.plan_gc(), new, promote, restore):
        assert plan.missing_preconditions() == [], plan.command


def _system_of(repo: Repo) -> str:
    return str(repo.objects["db"].locator["system"])


def test_verify_plan_checks_each_kind(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    store = default_store()
    repo.commit("baseline")
    locator = dict(repo.objects["db"].locator)
    backend = repo.backend_for("memory")

    # A command with no required set, so one precondition at a time can be
    # exercised (a gc plan with a single one would be refused as incomplete).
    cmd = "probe"
    assert cmd not in REQUIRED_PRECONDITIONS

    def plan_with(kind: str, expected=None, **params) -> Plan:
        plan = Plan(command=cmd)
        plan.require(kind, expected, detail=f"{kind} drifted ({{observed}})", **params)
        return plan

    # Command mismatch is a ConfigError, not staleness.
    with pytest.raises(ConfigError, match="expected a commit plan"):
        repo._verify_plan(Plan(command="gc"), "commit")

    # manifest_hash: current tree, and at a revision.
    repo._verify_plan(plan_with("manifest_hash", repo.current_manifest_hash()), cmd)
    with pytest.raises(StalePlanError, match="manifest_hash drifted"):
        repo._verify_plan(plan_with("manifest_hash", "nope"), cmd)
    head = repo._vcs_head_or_none()
    assert head
    repo._verify_plan(
        plan_with("manifest_hash", repo.current_manifest_hash(), rev=head), cmd
    )

    # workspace_id: None means "any workspace".
    repo._verify_plan(plan_with("workspace_id", None), cmd)
    repo._verify_plan(plan_with("workspace_id", repo.workspace.workspace_id), cmd)
    with pytest.raises(StalePlanError, match="workspace_id drifted"):
        repo._verify_plan(plan_with("workspace_id", "other000"), cmd)

    # workspace_bookmark: the workspace file and the VCS must both agree.
    repo._verify_plan(plan_with("workspace_bookmark", "main"), cmd)
    with pytest.raises(StalePlanError, match=r"workspace_bookmark drifted \(main\)"):
        repo._verify_plan(plan_with("workspace_bookmark", "work"), cmd)
    with pytest.raises(StalePlanError, match=r"workspace_bookmark drifted \(main\)"):
        repo._verify_plan(plan_with("workspace_bookmark", None), cmd)

    # vcs_head / history_digest / config_version.
    repo._verify_plan(plan_with("vcs_head", head), cmd)
    with pytest.raises(StalePlanError, match="vcs_head drifted"):
        repo._verify_plan(plan_with("vcs_head", "0" * 40), cmd)
    repo._verify_plan(plan_with("history_digest", repo.vcs.history_digest()), cmd)
    with pytest.raises(StalePlanError, match="history_digest drifted"):
        repo._verify_plan(plan_with("history_digest", "x"), cmd)
    repo._verify_plan(plan_with("config_version", repo.config.version), cmd)
    with pytest.raises(
        StalePlanError, match=rf"config_version drifted \({CONFIG_VERSION}\)"
    ):
        repo._verify_plan(plan_with("config_version", 1), cmd)

    # ref_absent / ref_head against a real branch.
    ref = "tether.ws.00000000.probe"
    repo._verify_plan(
        plan_with("ref_absent", backend="memory", locator=locator, ref=ref), cmd
    )
    s_main = store.system(system).branches["main"]
    backend.fork(locator, {"snapshot_id": s_main}, ref)
    with pytest.raises(StalePlanError, match="ref_absent drifted"):
        repo._verify_plan(
            plan_with("ref_absent", backend="memory", locator=locator, ref=ref), cmd
        )
    repo._verify_plan(
        plan_with(
            "ref_head",
            {"snapshot_id": s_main},
            backend="memory",
            locator=locator,
            ref=ref,
            what="probe",
        ),
        cmd,
    )
    store.write(system, ref, {"moved": True})
    with pytest.raises(StalePlanError, match="probe"):
        repo._verify_plan(
            plan_with(
                "ref_head",
                {"snapshot_id": s_main},
                backend="memory",
                locator=locator,
                ref=ref,
                what="probe",
            ),
            cmd,
        )

    # base_state: the base branch is where the plan saw it, or not.
    repo._verify_plan(
        plan_with(
            "base_state", {"snapshot_id": s_main}, backend="memory", locator=locator
        ),
        cmd,
    )
    store.write(system, "main", {"a": 2})
    with pytest.raises(StalePlanError, match="base_state drifted"):
        repo._verify_plan(
            plan_with(
                "base_state", {"snapshot_id": s_main}, backend="memory", locator=locator
            ),
            cmd,
        )

    # pin_state: the pin still names the reviewed state.
    state = backend.fingerprint(locator, None)
    pid = compute_pin_id("memory", backend.identity(locator), state, "0a1b2c3d")
    pin = backend.pin(locator, state, pid)
    repo._verify_plan(
        plan_with(
            "pin_state", state, backend="memory", locator=locator, pin=pin.to_dict()
        ),
        cmd,
    )
    other = Pin(id=pid, ref="tether.0a1b2c3d.ghost")
    with pytest.raises(StalePlanError, match="pin_state drifted"):
        repo._verify_plan(
            plan_with(
                "pin_state",
                state,
                backend="memory",
                locator=locator,
                pin=other.to_dict(),
            ),
            cmd,
        )

    # no_new_holders: nobody else is on the bookmark.
    repo._verify_plan(plan_with("no_new_holders", bookmark="work"), cmd)

    # bookmark_head: where the bookmark is, or `None` for no bookmark.
    repo._verify_plan(
        plan_with("bookmark_head", repo.vcs.bookmarks()["main"], bookmark="main"), cmd
    )
    with pytest.raises(StalePlanError, match="bookmark_head drifted"):
        repo._verify_plan(plan_with("bookmark_head", "0" * 40, bookmark="main"), cmd)
    repo._verify_plan(plan_with("bookmark_head", None, bookmark=None), cmd)

    # verify=False runs the command check only.
    repo._verify_plan(plan_with("manifest_hash", "nope"), cmd, verify=False)
    assert {
        "manifest_hash",
        "workspace_id",
        "workspace_bookmark",
        "vcs_head",
        "history_digest",
        "config_version",
        "ref_absent",
        "ref_head",
        "base_state",
        "pin_state",
        "no_new_holders",
        "bookmark_head",
    } <= PRECONDITION_KINDS


def test_a_plan_missing_a_required_precondition_is_refused(vcs_root: Path) -> None:
    """A plan supplies its own preconditions, so one saved by an older tether
    (or edited) could apply anywhere. Every command has a required set, per
    action for the per-object kinds; a plan short of it is stale, and the
    checkout its context names is checked even when the list omits it."""
    repo = Repo.init(vcs_root)
    _mem(repo)
    plan = repo.plan_commit("m")
    assert plan.missing_preconditions() == []
    data = plan.to_dict()
    data["preconditions"] = [
        p for p in data["preconditions"] if p["kind"] != "workspace_bookmark"
    ]
    old = Plan.from_dict(data)
    assert old.missing_preconditions() == ["workspace_bookmark"]
    with pytest.raises(StalePlanError, match="predates the workspace_bookmark"):
        repo.apply_commit(old)
    assert repo.objects["db"].state is None  # nothing was pinned

    # Per-action kinds: a `new` plan whose fork lost its `ref_absent`.
    repo.commit("baseline")
    new = repo.plan_new(bookmark="work", eager=True).to_dict()
    new["preconditions"] = [p for p in new["preconditions"] if p["key"] != "db"]
    stripped = Plan.from_dict(new)
    assert stripped.missing_preconditions() == ["ref_absent/ref_head for 'db'"]
    with pytest.raises(StalePlanError, match=r"ref_absent/ref_head for 'db'"):
        repo.apply_new(stripped)
    assert "work" not in repo.vcs.bookmarks()

    # The context's workspace id binds even a plan that does not require it.
    gc = repo.plan_gc().to_dict()
    gc["preconditions"] = [
        p for p in gc["preconditions"] if p["kind"] != "workspace_id"
    ]
    gc["context"]["workspace_id"] = "0" * 32
    with pytest.raises(StalePlanError, match="made in another checkout"):
        repo.apply_gc(Plan.from_dict(gc))
    # `verify=False` (plan and apply in one call) skips the requirement.
    repo.apply_commit(old, verify=False)


def test_saved_plans_are_bound_to_the_bookmark_they_were_made_on(
    vcs_root: Path,
) -> None:
    """A commit, restore or promote plan reviewed on one bookmark must not
    land on another: applied after `new` moved the checkout it is stale, and
    nothing is written. (Reviewed as e7 and e13: a restore plan applied
    after `new -b feat2` at the same commit reset feat's branch and wired it
    in as feat2's working ref; a promote plan applied after `new other`
    landed feat's data and moved the trunk to other's commit.)"""
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    store = default_store()
    repo.commit("baseline")
    base = repo._vcs_head_or_none()
    assert base

    repo.new(bookmark="other", eager=True)
    store.write(system, repo.workspace.working_refs["db"], {"from": "other"})
    repo.commit("other work")
    repo.new("main")
    repo.new(bookmark="feat", eager=True)
    feat_ref = repo.workspace.working_refs["db"]
    store.write(system, feat_ref, {"from": "feat"})
    feat_commit = repo.commit("feat work").vcs_commit
    feat_head = store.system(system).branches[feat_ref]

    commit_plan = Plan.from_json(repo.plan_commit("later").to_json())
    restore_plan = Plan.from_json(repo.plan_restore(["db"], base).to_json())
    promote_plan = Plan.from_json(repo.plan_promote().to_json())
    assert [a.op for a in promote_plan.actions] == ["fast-forward"]
    assert promote_plan.context["bookmark_commit"] == feat_commit

    # Same commit, another bookmark: the workspace file moved, the VCS did not.
    repo.new(bookmark="feat2")
    with pytest.raises(StalePlanError, match="made on feat; the checkout is on feat2"):
        repo.apply_restore(restore_plan)
    assert store.system(system).branches[feat_ref] == feat_head
    assert repo.workspace.working_refs == {} and repo.workspace.bookmark == "feat2"

    # Another bookmark's commit: feat's data must not land under other's name.
    # Its manifests differ too; the bookmark is what the refusal names.
    repo.new("other")
    main_before = repo.vcs.bookmarks()["main"]
    for plan, apply in (
        (commit_plan, repo.apply_commit),
        (restore_plan, repo.apply_restore),
        (promote_plan, repo.apply_promote),
    ):
        with pytest.raises(StalePlanError, match="made on feat; the checkout is on"):
            apply(plan)
    assert store.read(system, "main") == {"a": 1}
    assert repo.vcs.bookmarks()["main"] == main_before
    assert not [e for e in repo.ops() if e.command in ("promote", "restore")]

    # Back on feat, the same saved plans apply.
    repo.new("feat")
    report = repo.apply_promote(promote_plan)
    assert report.fast_forwarded and report.trunk_moved == feat_commit
    assert store.read(system, "main") == {"from": "feat"}


def test_a_bookmark_the_vcs_left_by_hand_is_a_stale_plan(vcs_root: Path) -> None:
    """The VCS side of `workspace_bookmark`: `workspace.toml` still says the
    bookmark, but a `jj new` / `git switch` moved the working copy. Under jj a
    working copy with edits counts as on its parent's bookmarks, so a hand
    edit after `jj new` does not read as "no bookmark" and pass."""
    import subprocess

    repo = Repo.init(vcs_root)
    system = _mem(repo)
    repo.commit("baseline")
    repo.new(bookmark="feat", eager=True)
    default_store().write(system, repo.workspace.working_refs["db"], {"feat": 1})
    repo.commit("feat work")  # feat has a commit of its own
    repo.new("main")
    plan = Plan.from_json(repo.plan_commit("m").to_json())
    assert plan.context["workspace_id"] == repo.workspace.workspace_id
    cmd = (
        ["git", "switch", "-q", "feat"]
        if repo.vcs.kind == "git"
        else ["jj", "new", "feat"]
    )
    subprocess.run(cmd, cwd=vcs_root, check=True, capture_output=True)
    if repo.vcs.kind == "jj":
        (vcs_root / "notes.txt").write_text("uncommitted hand edits\n")
    fresh = Repo.find(vcs_root)
    assert fresh.workspace.bookmark == "main"
    assert fresh._vcs_bookmarks_here() == ["feat"]
    with pytest.raises(StalePlanError, match="the checkout is on feat now"):
        fresh.apply_commit(plan)


def test_saving_a_plan_needs_a_persisted_workspace_id(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--plan` binds the file to the checkout's id; if the id cannot be
    written, say so rather than let every later `--from-plan` be refused as
    made in another checkout."""
    from tether import manifest as _m
    from tether.repo import _core

    repo = Repo.init(vcs_root)
    ws = _m.workspace_path(vcs_root)
    ws.unlink()

    def unwritable(root: Path, workspace: object) -> object:
        raise PermissionError("read-only")

    monkeypatch.setattr(_core, "claim_workspace", unwritable)
    with pytest.raises(ConfigError, match="not writable"):
        repo.require_persisted_workspace()
    monkeypatch.undo()
    repo.require_persisted_workspace()
    assert ws.is_file()
    assert Repo.find(vcs_root).workspace.workspace_id == repo.workspace.workspace_id
    repo.require_persisted_workspace()  # present: nothing to do


def test_precondition_detail_is_literal_text(vcs_root: Path) -> None:
    """The detail is plan-authored and may quote an object key with braces in
    it; the refusal must not read it as a format field."""
    repo = Repo.init(vcs_root)
    plan = Plan(command="probe")
    plan.require(
        "manifest_hash",
        "nope",
        key="odd{key}",
        detail="'odd{key}' changed ({observed}); re-run the plan",
    )
    with pytest.raises(StalePlanError, match=r"'odd\{key\}' changed \([0-9a-f]+\)"):
        repo._verify_plan(plan, "probe")
    # Command mismatch is its own helper, used without a plan re-check.
    with pytest.raises(ConfigError, match="expected a commit plan"):
        Repo._require_command(plan, "commit")
