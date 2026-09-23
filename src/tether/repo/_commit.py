"""`commit`: pin every changed object and record the manifests."""

from __future__ import annotations

import contextlib
import dataclasses

try:  # POSIX advisory locks; Windows has no fcntl and gets no writer lock
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tether.backends.base import (
    Capability,
    Tier,
    effective_capabilities,
    tier_of,
)
from tether.errors import (
    StalePlanError,
    UnpinnedStateError,
    VcsError,
)
from tether.manifest import (
    Pin,
    State,
    compute_pin_id,
    key_to_relpath,
    listing_name,
    listings_dir,
    read_objects,
    ref_for_pin,
    write_listing,
    write_object,
    write_workspace,
)
from tether.plan import Action, Plan

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tether.repo import Repo

    pass


from tether.repo._core import RepoCore
from tether.repo._reports import (
    CommitResult,
    short_state,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


class CommitOps(RepoCore):
    """`commit`: pin every changed object and record the manifests."""

    # -- commit ---------------------------------------------------------------- #
    def plan_commit(
        self: Repo,
        message: str,
        *,
        strict: bool = False,
        force: bool = False,
        do_snapshot: bool = True,
        fetched: dict[str, State] | None = None,
    ) -> Plan:
        """Compute what `commit` would do without writing anywhere.

        Snapshots (read-only), runs quiescence checks, and decides per object:
        `pin` (create `tether.<pin_id>`), `record` (Addressable / `pin =
        "record"`: state only), `record` with `recoverable = false` (Observed),
        or unchanged (a note). The plan carries the captured states and the
        manifest hash so `apply_commit` can refuse a stale plan.

        Args:
            message: VCS commit message (stored in the plan).
            strict: Fail instead of recording Observed objects unrecoverably.
            force: Skip quiescence checks (`NEEDS_QUIESCENCE` backends).
            do_snapshot: Take a fresh `snapshot` first.
            fetched: States `pull` read from the bookmark's branches; used in
                place of a snapshot and pinned as they are (the plan is a
                `pull` plan). Internal.

        Raises:
            UnpinnedStateError: `strict` and an Observed object changed.
            BackendError: A quiescence check failed.
            MultiObjectError: The snapshot failed for one or more objects.
        """
        moving = set(self.moving_keys())
        if fetched is not None:
            states = dict(fetched)
        elif do_snapshot:
            states = dict(self.snapshot(upstream=False))
        else:
            states = dict(self.workspace.last_snapshot)
        if fetched is None:
            # Positions: an object with no working branch commits at its
            # previous pin, whatever a cached fan-out says about its upstream.
            for key, m in self.objects.items():
                if key not in moving and m.state is not None:
                    states[key] = dict(m.state)
        keys = list(self.objects)

        if not force:
            for key in keys:
                m = self.objects[key]
                backend = self.backend_for(m.kind)
                eff = effective_capabilities(backend, m.locator, m.policy)
                if Capability.NEEDS_QUIESCENCE in eff:
                    check = getattr(backend, "check_quiescence", None)
                    if callable(check):
                        check(m.locator, self._working_ref(key))

        plan = Plan(
            command="commit",
            context={
                "message": message,
                "manifest_hash": self.current_manifest_hash(),
                "states": {k: states[k] for k in keys if k in states},
                "workspace_id": self.workspace.workspace_id,
                "pull": fetched is not None,
            },
        )
        plan.require(
            "manifest_hash",
            plan.context["manifest_hash"],
            detail="manifests changed since the plan was made; re-run the plan",
        )
        for key in keys:
            m = self.objects[key]
            backend = self.backend_for(m.kind)
            eff = effective_capabilities(backend, m.locator, m.policy)
            state = states.get(key)
            if state is None:
                plan.notes.append(f"{key}: no fingerprint; skipped")
                continue
            needs_pin = Capability.PIN in eff
            if self._same(m.kind, m.state, state) and (m.pin is not None) == needs_pin:
                plan.notes.append(f"{key}: unchanged")
                continue
            if self._same(m.kind, m.state, state):
                # Same state, different policy: `pin = "record"` now drops the
                # native ref (gc releases it), `native` again creates one.
                plan.notes.append(
                    f"{key}: policy changed; "
                    + ("pin released" if not needs_pin else "pin created")
                )
            if fetched is not None:
                plan.notes.append(f"{key}: fetched {short_state(state)}")
            if tier_of(eff) is Tier.OBSERVED:
                if strict:
                    raise UnpinnedStateError(
                        f"{key!r} is Observed-tier and cannot be pinned",
                        key=key,
                        kind=m.kind,
                    )
                plan.actions.append(
                    Action(
                        "record",
                        key,
                        m.kind,
                        detail="Observed: recorded, not recoverable",
                        params={"state": state, "recoverable": False},
                    )
                )
            elif needs_pin:
                pin_id = compute_pin_id(
                    m.kind,
                    backend.identity(m.locator),
                    self._content_of(m.kind, state),
                    self.config.dataset_id,
                )
                plan.actions.append(
                    Action(
                        "pin",
                        key,
                        m.kind,
                        target=ref_for_pin(pin_id),
                        detail=f"native ref at {short_state(state)}",
                        params={"state": state, "pin_id": pin_id},
                    )
                )
            else:
                why = "pin=record" if m.policy.pin == "record" else "Addressable"
                recoverable = backend.state_addressable(m.locator, state)
                if not recoverable:
                    why += ", but this state carries no address to reopen it by"
                plan.actions.append(
                    Action(
                        "record",
                        key,
                        m.kind,
                        detail=f"{why}: state {short_state(state)}, no native ref"
                        + ("" if recoverable else "; recorded, not recoverable"),
                        params={"state": state, "recoverable": recoverable},
                    )
                )
        if plan.actions:
            plan.actions.append(
                Action("vcs-commit", detail=f"{self.vcs.kind} commit: {message!r}")
            )
        return plan

    def apply_commit(
        self: Repo,
        plan: Plan,
        *,
        vcs: bool = True,
        verify: bool = True,
    ) -> CommitResult:
        """Execute a plan from `plan_commit`.

        Args:
            plan: The plan to apply.
            vcs: Commit `.tether/` and `tether.toml` to the enclosing repository.
            verify: Re-fingerprint the planned objects and refuse the plan if
                any state or the manifest set changed since it was computed.

        Raises:
            StalePlanError: The plan was computed for a different world.
            BackendError: A pin failed (pins created by this call are released
                best-effort).
        """
        with self._writer_lock(), self._repo_lock():
            self._verify_plan(plan, "commit", verify=verify)
            message = str(plan.context.get("message", ""))
            if vcs:
                self._check_on_bookmark()
            object_actions = [a for a in plan.actions if a.op in ("pin", "record")]
            if verify:
                # Per object, against one snapshot: a race during apply, not a
                # plan precondition (see `_verify_plan`).
                current = self.snapshot()
                for a in object_actions:
                    if not self._same(
                        a.kind, current.get(a.key), a.params.get("state")
                    ):
                        raise StalePlanError(
                            f"{a.key!r} changed since the plan was made "
                            f"({short_state(a.params.get('state'))} -> "
                            f"{short_state(current.get(a.key))}); re-run the plan"
                        )

            result = CommitResult(message=message)
            for note in plan.notes:
                key, _, why = note.partition(": ")
                if why == "unchanged":
                    result.unchanged.append(key)

            created_pins: list[tuple[str, Pin]] = []
            outcomes: dict[str, tuple[State, Pin | None, bool]] = {}
            is_pull = bool(plan.context.get("pull"))
            will_write = bool(object_actions) or (
                vcs and self.vcs.dirty(self._vcs_paths())
            )
            if not will_write:
                return result
            pre = {
                "vcs": self.vcs.position() if vcs else None,
                "objects": self._manifest_texts(a.key for a in object_actions),
                "workspace": self.workspace.to_toml(),
            }
            op = self._begin_op("pull" if is_pull else "commit", plan=plan, pre=pre)
            written_listings: list[str] = []
            # One pin per pin id per commit. Two objects with one identity
            # and one content state (two databases of a Neon project, two
            # keys on one Icechunk store) name one snapshot; the second is
            # recorded at the state the pin was actually cut at, not its own
            # fingerprint of the same content, whose volatile address (a
            # Neon LSN) may differ and would not match the pin.
            pinned_by_id: dict[str, tuple[dict[str, Any], Pin]] = {}
            try:
                for a in object_actions:
                    m = self.objects.get(a.key)
                    if m is None:
                        # Registered when the plan was made, gone now (a
                        # `remove` in between): a stale plan, not a crash.
                        raise StalePlanError(
                            f"{a.key!r} is no longer registered; re-run the plan"
                        )
                    backend = self.backend_for(m.kind)
                    state = dict(a.params["state"])
                    if a.op == "pin" and str(a.params["pin_id"]) in pinned_by_id:
                        state, pin = pinned_by_id[str(a.params["pin_id"])]
                        outcomes[a.key] = (dict(state), pin, True)
                        result.pinned[a.key] = pin
                    elif a.op == "pin":
                        pin = backend.pin(m.locator, state, str(a.params["pin_id"]))
                        pinned_by_id[str(a.params["pin_id"])] = (state, pin)
                        self._note_touched(a.key, m.kind, m.locator)
                        # Roll back only what this commit created: a pin the
                        # backend found already carrying the state belongs to
                        # the commit (or the sibling key) that made it.
                        if pin.created:
                            created_pins.append((a.key, pin))
                            self._note_pinned(a.key, m.kind, pin.id)
                            self._progress(op, "pin", key=a.key, ref=pin.ref)
                        outcomes[a.key] = (state, pin, True)
                        result.pinned[a.key] = pin
                    else:
                        recoverable = bool(a.params.get("recoverable", True))
                        outcomes[a.key] = (state, None, recoverable)
                        if recoverable:
                            result.pinned[a.key] = None
                        else:
                            result.unrecoverable.append(a.key)

                # Persist manifests (and listings for backends that provide them).
                for key, (state, pin, recoverable) in outcomes.items():
                    m = self.objects[key]
                    if is_pull and "at" in m.locator:
                        # Pulled onto the branch: the state it was first registered
                        # at is history now, not where it sits.
                        m = dataclasses.replace(m, locator=self._upstream_locator(m))
                    updated = m.with_pin(state=state, pin=pin, recoverable=recoverable)
                    self.objects[key] = updated
                    write_object(self.root, updated)
                    backend = self.backend_for(m.kind)
                    if Capability.DIFF in effective_capabilities(
                        backend, m.locator, m.policy
                    ):
                        text = backend.listing(m.locator, state)
                        if text is not None:
                            name = listing_name(
                                m.kind,
                                backend.identity(m.locator),
                                self._content_of(m.kind, state),
                            )
                            if not (listings_dir(self.root) / name).exists():
                                written_listings.append(name)
                            write_listing(self.root, name, text)

                if outcomes:
                    self._progress(op, "manifests", keys=sorted(outcomes))
                # Commit when something was pinned, and also when the manifests are
                # already dirty in the working tree (an undone commit, an `add`, an
                # `import`): the dataset commit is what makes them history.
                if vcs and (outcomes or self.vcs.dirty(self._vcs_paths())):
                    # The bookmark follows the commit: its store branches' heads
                    # are what the commit pinned.
                    result.vcs_commit = self.vcs.commit(
                        self._vcs_paths(), message, advance=self.workspace.bookmark
                    )
                    self._progress(op, "vcs-commit", commit=result.vcs_commit)
                    self._require_committed(result.vcs_commit)
            except Exception as exc:
                landed = self._vcs_commit_landed(pre["vcs"]) if vcs else None
                if landed is not None:
                    # The dataset commit exists: it names the pins and carries
                    # the manifests, so releasing them would break history.
                    # Keep everything, finish the operation as a commit that
                    # succeeded, and surface the trailing error.
                    result.vcs_commit = landed
                    self.objects = read_objects(self.root)
                    self._mark_base_states(
                        k
                        for k in outcomes
                        if k in self.workspace.working_refs
                        or k in self.workspace.pending_forks
                    )
                    write_workspace(self.root, self.workspace)
                    self._end_op(
                        op,
                        result={
                            "vcs_commit": landed,
                            "pinned": {
                                k: (p.id if p else None)
                                for k, p in result.pinned.items()
                            },
                            "unrecoverable": list(result.unrecoverable),
                            "failed_after_commit": str(exc),
                        },
                    )
                    raise
                # Compensate everything this call did: pins it created, manifests
                # and listings it wrote, so the working tree is as before and the
                # journal says what was attempted and that it did not finish.
                self._rollback_pins(created_pins)
                self._restore_manifests(pre["objects"])
                for name in written_listings:
                    (listings_dir(self.root) / name).unlink(missing_ok=True)
                self.objects = read_objects(self.root)
                self._end_op(op, result={"failed": str(exc), "rolled_back": True})
                raise

            # What this workspace just committed is, by definition, not stale.
            self._mark_base_states(
                k
                for k in outcomes
                if k in self.workspace.working_refs or k in self.workspace.pending_forks
            )
            write_workspace(self.root, self.workspace)
            self._end_op(
                op,
                result={
                    "vcs_commit": result.vcs_commit,
                    "pinned": {
                        k: (p.id if p else None) for k, p in result.pinned.items()
                    },
                    "unrecoverable": list(result.unrecoverable),
                },
            )
            return result

    def commit(
        self: Repo,
        message: str,
        *,
        vcs: bool = True,
        strict: bool = False,
        force: bool = False,
        do_snapshot: bool = True,
    ) -> CommitResult:
        """Pin and record every object's current state, then commit the manifests.

        Equivalent to `apply_commit(plan_commit(...))`. For each object whose
        state changed: `PIN`-capable backends create the native ref
        `tether.<pin_id>` (idempotent), Addressable and `pin = "record"`
        objects have their state recorded, and Observed objects are recorded
        with `recoverable = False`. Backends that provide a listing have it
        stored under `.tether/listings/`. If any pin fails, pins created by this
        call are released best-effort. Working branches stay where they are:
        the next write lands on the same branch, and `new` afterwards reuses
        it (`commit` is not `jj commit`, which implies `jj new`).

        Args:
            message: VCS commit message.
            vcs: Commit `.tether/` and `tether.toml` to the enclosing repository.
            strict: Fail instead of recording Observed objects unrecoverably.
            force: Skip quiescence checks (`NEEDS_QUIESCENCE` backends).
            do_snapshot: Take a fresh `snapshot` first.

        Returns:
            What was pinned, recorded, or skipped, and the VCS commit id.

        Raises:
            UnpinnedStateError: `strict` and an Observed object changed.
            BackendError: A quiescence check or pin failed.
            MultiObjectError: The snapshot failed for one or more objects.
        """
        # Plan and apply under one lock: planning sees the state the lock
        # refreshed, and nothing in this checkout moves in between.
        with self._writer_lock():
            plan = self.plan_commit(
                message,
                strict=strict,
                force=force,
                do_snapshot=do_snapshot,
            )
            # No re-verification: a pin names the *state* the plan captured, so a
            # branch that moved since changes nothing about what lands.
            return self.apply_commit(plan, vcs=vcs, verify=False)

    def _require_committed(self: Repo, commit: str) -> None:
        """Every manifest in the working tree must be in the commit's tree.

        jj commits what it tracks and reports success either way: a dataset
        under an ignored directory made an empty dataset commit that `status`
        then called clean and `verify` called ok. Raised after the commit
        landed, so `apply_commit` keeps the pins and journals the failure.
        """
        reldir = self._objects_reldir()
        committed = self.vcs.files_at(commit, reldir)
        missing = sorted(
            key
            for key in self.objects
            if f"{reldir}/{Path(*key_to_relpath(key).parts[1:]).as_posix()}"
            not in committed
        )
        if missing:
            shown = ", ".join(missing[:3]) + (", ..." if len(missing) > 3 else "")
            raise VcsError(
                f"{self.vcs.kind} commit {commit[:12]} does not contain "
                f"{len(missing)} of the dataset's manifest(s) ({shown}); is the "
                "dataset directory ignored by the VCS? Fix that and commit again"
            )

    def _rollback_pins(self, created: list[tuple[str, Pin]]) -> None:
        for key, pin in created:
            m = self.objects.get(key)
            if m is None:
                continue
            # Best-effort compensation; a failed unpin is reconciled by gc.
            with contextlib.suppress(Exception):
                self.backend_for(m.kind).unpin(m.locator, pin)
