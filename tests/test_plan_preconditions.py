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
from tether.manifest import Pin, compute_pin_id
from tether.plan import PLAN_FORMAT, PRECONDITION_KINDS, Plan, Precondition
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
    assert kinds(repo.plan_commit("m")) == ["manifest_hash"]
    repo.commit("baseline")
    assert "manifest_hash" in kinds(repo.plan_gc())
    assert "history_digest" in kinds(repo.plan_gc())
    new = repo.plan_new(bookmark="work", eager=True)
    assert set(kinds(new)) == {
        "manifest_hash",
        "workspace_id",
        "no_new_holders",
        "ref_absent",
    }
    repo.new(bookmark="work", eager=True)
    default_store().write(_system_of(repo), repo.workspace.working_refs["db"], {"b": 2})
    promote = repo.plan_promote(["db"])
    assert kinds(promote) == ["base_state", "ref_head"]
    assert kinds(repo.plan_upgrade()) == ["config_version"]


def _system_of(repo: Repo) -> str:
    return str(repo.objects["db"].locator["system"])


def test_verify_plan_checks_each_kind(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    store = default_store()
    repo.commit("baseline")
    locator = dict(repo.objects["db"].locator)
    backend = repo.backend_for("memory")

    def plan_with(kind: str, expected=None, **params) -> Plan:
        plan = Plan(command="gc")
        plan.require(kind, expected, detail=f"{kind} drifted ({{observed}})", **params)
        return plan

    # Command mismatch is a ConfigError, not staleness.
    with pytest.raises(ConfigError, match="expected a commit plan"):
        repo._verify_plan(Plan(command="gc"), "commit")

    # manifest_hash: current tree, and at a revision.
    repo._verify_plan(plan_with("manifest_hash", repo.current_manifest_hash()), "gc")
    with pytest.raises(StalePlanError, match="manifest_hash drifted"):
        repo._verify_plan(plan_with("manifest_hash", "nope"), "gc")
    head = repo._vcs_head_or_none()
    assert head
    repo._verify_plan(
        plan_with("manifest_hash", repo.current_manifest_hash(), rev=head), "gc"
    )

    # workspace_id: None means "any workspace".
    repo._verify_plan(plan_with("workspace_id", None), "gc")
    repo._verify_plan(plan_with("workspace_id", repo.workspace.workspace_id), "gc")
    with pytest.raises(StalePlanError, match="workspace_id drifted"):
        repo._verify_plan(plan_with("workspace_id", "other000"), "gc")

    # vcs_head / history_digest / config_version.
    repo._verify_plan(plan_with("vcs_head", head), "gc")
    with pytest.raises(StalePlanError, match="vcs_head drifted"):
        repo._verify_plan(plan_with("vcs_head", "0" * 40), "gc")
    repo._verify_plan(plan_with("history_digest", repo.vcs.history_digest()), "gc")
    with pytest.raises(StalePlanError, match="history_digest drifted"):
        repo._verify_plan(plan_with("history_digest", "x"), "gc")
    repo._verify_plan(plan_with("config_version", repo.config.version), "gc")
    with pytest.raises(StalePlanError, match=r"config_version drifted \(4\)"):
        repo._verify_plan(plan_with("config_version", 1), "gc")

    # ref_absent / ref_head against a real branch.
    ref = "tether.ws.00000000.probe"
    repo._verify_plan(
        plan_with("ref_absent", backend="memory", locator=locator, ref=ref), "gc"
    )
    s_main = store.system(system).branches["main"]
    backend.fork(locator, {"snapshot_id": s_main}, ref)
    with pytest.raises(StalePlanError, match="ref_absent drifted"):
        repo._verify_plan(
            plan_with("ref_absent", backend="memory", locator=locator, ref=ref), "gc"
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
        "gc",
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
            "gc",
        )

    # base_state: the base branch is where the plan saw it, or not.
    repo._verify_plan(
        plan_with(
            "base_state", {"snapshot_id": s_main}, backend="memory", locator=locator
        ),
        "gc",
    )
    store.write(system, "main", {"a": 2})
    with pytest.raises(StalePlanError, match="base_state drifted"):
        repo._verify_plan(
            plan_with(
                "base_state", {"snapshot_id": s_main}, backend="memory", locator=locator
            ),
            "gc",
        )

    # pin_state: the pin still names the reviewed state.
    state = backend.fingerprint(locator, None)
    pid = compute_pin_id("memory", backend.identity(locator), state, "0a1b2c3d")
    pin = backend.pin(locator, state, pid)
    repo._verify_plan(
        plan_with(
            "pin_state", state, backend="memory", locator=locator, pin=pin.to_dict()
        ),
        "gc",
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
            "gc",
        )

    # no_new_holders: nobody else is on the bookmark.
    repo._verify_plan(plan_with("no_new_holders", bookmark="work"), "gc")

    # verify=False runs the command check only.
    repo._verify_plan(plan_with("manifest_hash", "nope"), "gc", verify=False)
    assert {
        "manifest_hash",
        "workspace_id",
        "vcs_head",
        "history_digest",
        "config_version",
        "ref_absent",
        "ref_head",
        "base_state",
        "pin_state",
        "no_new_holders",
    } <= PRECONDITION_KINDS
