"""`promote`: move each system's base branch to what a fork holds."""

from __future__ import annotations

try:  # POSIX advisory locks; Windows has no fcntl and gets no writer lock
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from tether.backends.base import (
    Capability,
    effective_capabilities,
)
from tether.backends.base import fork_ref as _fork_ref
from tether.backends.base import merge_ref as _merge_ref
from tether.backends.base import promote_ref as _promote_ref
from tether.errors import (
    BackendError,
    ConfigError,
    MergeConflict,
    MultiObjectError,
    PinDriftError,
    RefMovedError,
    TetherError,
)
from tether.manifest import (
    ObjectManifest,
    Pin,
    State,
    manifest_hash,
    write_workspace,
)
from tether.oplog import (
    report_dict,
)
from tether.plan import Action, Plan

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tether.repo import Repo

    pass


from tether.repo._core import RepoCore
from tether.repo._reports import (
    PromoteReport,
    _source_object,
    short_state,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


class PromoteOps(RepoCore):
    """`promote`: move each system's base branch to what a fork holds."""

    # -- promote (fork -> base branch) ----------------------------------------- #
    def plan_promote(
        self: Repo,
        keys: Sequence[str] | None = None,
        *,
        rev: str | None = None,
        strategy: str = "auto",
        message: str | None = None,
    ) -> Plan:
        """Compute how each object's base branch could be moved to its fork.

        Promotion moves the *system's* base branch (the locator's `branch`) to
        the state a fork holds; the dataset commit already pins that state, so
        nothing tether-side needs re-committing after a fast-forward. Per
        Forkable object, the target is this workspace's working branch head
        (or, with `rev`, the state pinned at that dataset commit) and the base
        is compared to the **fork point** recorded when the branch was created
        (falling back to the backend's ancestry check):

        - base unchanged and `PROMOTE`: `fast-forward`
        - base moved (or unknown) and `MERGE`: `merge` (native 3-way merge)
        - otherwise: `refuse`, with the system's own recipe in the detail

        Args:
            keys: Objects to consider (default: all Forkable ones).
            rev: Promote the states pinned at this dataset commit instead of the
                working branches.
            strategy: `auto` (table above), `ff` (refuse anything that is not a
                fast-forward), or `merge` (refuse anything that is not a merge).
            message: Merge commit message for backends that record one.

        Raises:
            ConfigError: Unknown key or strategy.
        """
        if strategy not in ("auto", "ff", "merge"):
            raise ConfigError(
                f"unknown promote strategy {strategy!r} (auto, ff, merge)"
            )
        objects = self._objects_at(self.vcs.resolve(rev)) if rev else self.objects
        selected = list(keys) if keys else sorted(objects)
        for key in selected:
            if key not in objects:
                raise ConfigError(f"no such object: {key}")
        message = message or f"tether promote {rev or self.workspace.workspace_id[:8]}"
        bookmark = self.workspace.bookmark
        marks = self.vcs.bookmarks()
        plan = Plan(
            command="promote",
            context={
                "rev": rev,
                "strategy": strategy,
                "message": message,
                "subset": bool(keys),
                "manifest_hash": manifest_hash(objects),
                "workspace_id": self.workspace.workspace_id,
                "bookmark": bookmark,
                # The commit a full promotion moves the trunk to: this
                # bookmark's, as reviewed -- never whatever the checkout is on
                # by the time the plan is applied.
                "bookmark_commit": marks.get(bookmark) if bookmark else None,
            },
        )
        plan.require(
            "workspace_id",
            self.workspace.workspace_id,
            detail="this promote plan was made in another checkout; "
            "re-run the plan here",
        )
        plan.require(
            "workspace_bookmark",
            bookmark,
            detail=f"this promote plan was made on {bookmark or 'no bookmark'}; the "
            "checkout is on {observed} now; re-run the plan",
        )
        plan.require(
            "bookmark_head",
            plan.context["bookmark_commit"],
            bookmark=bookmark,
            detail=f"bookmark {bookmark} moved since the promote plan was made (now "
            "at {observed}); re-run the plan",
        )
        plan.require(
            "manifest_hash",
            plan.context["manifest_hash"],
            rev=rev,
            detail="manifests changed since the promote plan was made; re-run the plan",
        )
        # Every forkable object needs its base branch read, and off the trunk
        # its working branch too: two store round trips per object. Take them
        # all at once, like `snapshot` does, instead of one object at a time.
        forkable = [
            key
            for key in selected
            if Capability.FORK
            in effective_capabilities(
                self.backend_for(objects[key].kind),
                objects[key].locator,
                objects[key].policy,
            )
        ]

        def probe(key: str) -> tuple[State, State | None]:
            m = objects[key]
            backend = self.backend_for(m.kind)
            base_locator = {k: v for k, v in m.locator.items() if k != "at"}
            base = backend.fingerprint(base_locator, None)
            target: State | None = None
            if not rev and (working_ref := self._working_ref(key)) is not None:
                target = backend.fingerprint(m.locator, working_ref)
            return base, target

        probed, errors = self._fanout_collect(
            probe, [] if self.on_trunk() else forkable
        )
        if errors:
            raise MultiObjectError("promote: could not read branch heads", errors)

        current: list[str] = []
        for key in selected:
            m = objects[key]
            backend = self.backend_for(m.kind)
            eff = effective_capabilities(backend, m.locator, m.policy)
            if Capability.FORK not in eff:
                plan.notes.append(f"{key}: not forkable; nothing to promote")
                continue
            if self.on_trunk():
                plan.notes.append(f"{key}: on trunk; already on the base branch")
                continue

            base_state, probed_target = probed[key]
            base_txt = f"{m.locator.get('branch', 'main')}"

            # What to promote, and what the base looked like when it was forked.
            source: dict[str, Any]
            fork_point: State | None
            if rev:
                if m.state is None:
                    plan.notes.append(f"{key}: nothing committed at {rev}")
                    continue
                target_state = m.state
                try:
                    src = self._pinned_source(m, backend, eff)
                except PinDriftError as exc:
                    plan.actions.append(
                        Action(
                            "refuse",
                            key,
                            m.kind,
                            target=str(m.locator.get("branch", "main")),
                            detail=str(exc),
                            params={"locator": dict(m.locator)},
                        )
                    )
                    continue
                except BackendError as exc:
                    plan.notes.append(f"{key}: not recoverable at {rev} ({exc})")
                    continue
                source = (
                    {"pin": src.to_dict()} if isinstance(src, Pin) else {"state": src}
                )
                fork_point = None
            else:
                working_ref = self._working_ref(key)
                if working_ref is None or probed_target is None:
                    plan.notes.append(
                        f"{key}: no working branch yet; nothing to promote"
                    )
                    continue
                if m.state is None:
                    plan.notes.append(
                        f"{key}: nothing committed on this bookmark yet; commit first"
                    )
                    continue
                target_state = probed_target
                source = {"ref": working_ref}
                fork_point = self.workspace.fork_points.get(key)
                if not self._same(m.kind, target_state, m.state):
                    # The trunk moves to this bookmark's commit, whose manifest
                    # must describe what landed: a write the commit does not
                    # record would reach the base under a commit that says
                    # something else.
                    plan.actions.append(
                        Action(
                            "refuse",
                            key,
                            m.kind,
                            target=base_txt,
                            detail=f"{working_ref} has writes since the last commit "
                            f"({short_state(m.state)} committed, "
                            f"{short_state(target_state)} on the branch); `tether "
                            "commit` them first",
                            params={"locator": dict(m.locator)},
                        )
                    )
                    continue

            if self._same(m.kind, target_state, base_state):
                plan.notes.append(f"{key}: base already at the target")
                current.append(key)
                continue

            # Is the base still behind the source? The store's own history
            # answers exactly where it can (`ancestor_of`: git, icechunk,
            # Dolt, memory); the recorded fork point is the fallback
            # for backends without a DAG -- a heuristic that lies after a
            # bookmark is joined from elsewhere or reset by hand.
            unchanged: bool | None = backend.ancestor_of(
                m.locator, base_state, _source_object(source)
            )
            if unchanged is None and fork_point is not None:
                unchanged = self._same(m.kind, fork_point, base_state)

            can_ff = Capability.PROMOTE in eff
            can_merge = Capability.MERGE in eff and "state" not in source
            params = {
                "locator": dict(m.locator),
                "source": source,
                "base_state": base_state,
                "target_state": target_state,
                "fork_point": fork_point,
            }
            hint = f"; {backend.PROMOTE_HINT}" if backend.PROMOTE_HINT else ""

            if unchanged is True and strategy != "merge" and can_ff:
                plan.actions.append(
                    Action(
                        "fast-forward",
                        key,
                        m.kind,
                        target=base_txt,
                        detail=f"{base_txt} {short_state(base_state)} -> "
                        f"{short_state(target_state)} (base unchanged since fork)",
                        params=params,
                    )
                )
            elif (
                can_merge
                and strategy != "ff"
                and (unchanged is not True or not can_ff or strategy == "merge")
            ):
                why = (
                    "base unchanged since fork"
                    if unchanged is True
                    else "base moved since fork"
                    if unchanged is False
                    else "no fork point recorded; ancestry unknown"
                )
                src_name = source.get("ref") or source.get("pin", {}).get("ref")
                plan.actions.append(
                    Action(
                        "merge",
                        key,
                        m.kind,
                        target=base_txt,
                        detail=f"merge {src_name} into {base_txt} ({why})",
                        params=params,
                    )
                )
            else:
                if unchanged is True:
                    reason = (
                        f"strategy={strategy} but the backend cannot merge"
                        if strategy == "merge"
                        else f"the {m.kind} backend cannot move a branch"
                    )
                elif unchanged is False:
                    reason = (
                        f"{base_txt} moved since the fork "
                        f"({short_state(fork_point)} -> {short_state(base_state)})"
                        + (
                            f" and strategy={strategy}"
                            if strategy == "ff" and Capability.MERGE in eff
                            else " and the backend cannot merge"
                        )
                    )
                else:
                    reason = (
                        "cannot tell whether the base moved (no fork point recorded)"
                    )
                plan.actions.append(
                    Action(
                        "refuse",
                        key,
                        m.kind,
                        target=base_txt,
                        detail=f"{reason}{hint}",
                        params=params,
                    )
                )
        # The bookmark lands whole or not at all. Moving some systems' `main`
        # while others are refused would leave readers of `main` a mix of new
        # and old states -- the half-done state promotion exists to avoid --
        # and the trunk bookmark could not follow. Naming keys is the way to
        # land a subset on purpose.
        refused = [a.key for a in plan.actions if a.op == "refuse"]
        if refused and not keys:
            names = ", ".join(refused)
            plan.actions = [
                a
                if a.op == "refuse"
                else Action(
                    "hold",
                    a.key,
                    a.kind,
                    target=a.target,
                    detail=f"would {a.op}: {a.detail}; held because {names} "
                    "refused, and a bookmark is planned whole or not at all",
                    params=a.params,
                )
                for a in plan.actions
            ]
            plan.notes.append(
                f"nothing moved: {names} refused; land the rest on purpose with "
                f"`tether promote KEY...`, or write those systems' base branches "
                "by hand on the trunk (see the refusals) and promote again"
            )
        if keys:
            self._refuse_partial_scopes(plan, set(selected), ("fast-forward", "merge"))
        self._share_scope_writes(plan, ("fast-forward", "merge"))
        # Base branches already holding what this bookmark's commit records:
        # with nothing left to write, that commit describes the upstream and
        # the trunk can move to it (the second promote after a merge).
        plan.context["current"] = current
        self._refuse_trunk_regression(plan, keys, rev, current=bool(current))
        # Everything the check needs travels in the precondition: the object
        # may have been removed since a `--rev` plan named it, and its base
        # branch must still be where the plan saw it; what lands must be what
        # was reviewed.
        for a in plan.actions:
            if a.op not in ("fast-forward", "merge"):
                continue
            locator = dict(a.params["locator"])
            plan.require(
                "base_state",
                a.params["base_state"],
                key=a.key,
                backend=a.kind,
                locator=locator,
                detail=f"{a.key!r}: base branch moved since the plan was made "
                f"({short_state(a.params['base_state'])} -> {{observed}}); "
                "re-run the plan",
            )
            source = a.params.get("source") or {}
            target_state = a.params.get("target_state")
            if "ref" in source:
                plan.require(
                    "ref_head",
                    target_state,
                    key=a.key,
                    backend=a.kind,
                    locator=locator,
                    ref=str(source["ref"]),
                    what=f"promote {a.key}",
                )
            elif "pin" in source and target_state is not None:
                plan.require(
                    "pin_state",
                    target_state,
                    key=a.key,
                    backend=a.kind,
                    locator=locator,
                    pin=dict(source["pin"]),
                    detail=f"promote {a.key}: pin {source['pin'].get('ref')} no "
                    "longer names the reviewed state ({observed}); re-run the plan",
                )
        return plan

    def _refuse_trunk_regression(
        self,
        plan: Plan,
        keys: Sequence[str] | None,
        rev: str | None,
        *,
        current: bool = False,
    ) -> None:
        """A full promotion moves the trunk bookmark onto this bookmark's
        commit. That is a fast-forward only when the trunk is an ancestor of
        it; otherwise `main` would drop commits (`jj bookmark set` without
        `--allow-backwards` refuses the same move). Land the data anyway and
        `main` would describe a different upstream than the stores hold, so
        the whole plan is refused: merge or rebase the manifests first, or
        name keys (a subset never moves the trunk). `current` says the plan
        would move the trunk with nothing to write (every base already holds
        what the commit records), which needs the same guard.
        """
        bookmark = self.workspace.bookmark
        if keys or rev is not None or not bookmark or self.on_trunk():
            return
        writes = [a for a in plan.actions if a.op in ("fast-forward", "merge")]
        if not writes and not current:
            return
        marks = self.vcs.bookmarks()
        trunk_commit = marks.get(self.config.trunk)
        here = marks.get(bookmark)
        if self.config.trunk in self.vcs.conflicted_bookmarks():
            # Several targets, none of them in `marks`: the guard below would
            # read that as "no trunk yet" and let the move pick one side.
            why = (
                f"{self.config.trunk} has conflicting targets (a divergent move or "
                f"fetch; `jj bookmark list`); landing would settle it on this "
                f"bookmark and drop the other side. `jj bookmark set "
                f"{self.config.trunk} -r REV` first"
            )
        elif not trunk_commit or not here or self.vcs.is_ancestor(trunk_commit, here):
            return
        else:
            why = (
                f"{self.config.trunk} ({trunk_commit[:12]}) has commits this "
                f"bookmark ({bookmark} at {here[:12]}) does not; landing would move "
                f"{self.config.trunk} backwards or sideways and drop them. Merge or "
                f"rebase the manifests onto {self.config.trunk} first, or name keys "
                "to land a subset (which never moves the trunk)"
            )
        plan.actions = [
            Action(
                "refuse",
                a.key,
                a.kind,
                target=a.target,
                detail=why,
                params={"locator": a.params.get("locator", {})},
            )
            if a.op in ("fast-forward", "merge", "share", "hold")
            else a
            for a in plan.actions
        ]
        plan.context["current"] = []  # nothing to write and the trunk stays
        plan.notes.append(f"nothing moved: {why}")

    def _action_scope(self, a: Action) -> tuple[str, str] | None:
        """The branch scope of a write action, from what the *plan* captured.

        A `--rev` plan may name an object that has since been removed from the
        working tree; its kind and locator travel in the action, so the
        checks that protect its scope siblings and its base branch must not
        depend on `self.objects` still having it.
        """
        locator = a.params.get("locator")
        if not a.kind or not isinstance(locator, dict):
            return None
        return (a.kind, self.backend_for(a.kind).branch_scope(dict(locator)))

    def _refuse_partial_scopes(
        self, plan: Plan, keys: set[str], ops: tuple[str, ...]
    ) -> None:
        """A write to a native branch moves every object writing through it;
        naming only some of them is refused, with the rest to name."""
        for i, a in enumerate(plan.actions):
            if a.op not in ops:
                continue
            scope = self._action_scope(a)
            if scope is None:
                continue
            # What a promote moves is the scope's *base* branch (`a.target`),
            # whatever the source -- a working ref, a pin, a state. Every
            # object of the scope whose base branch that is moves with it.
            base = str(a.target)

            def shares_base(other: ObjectManifest, scope=scope, base=base) -> bool:
                backend = self.backend_for(other.kind)
                if (other.kind, backend.branch_scope(other.locator)) != scope:
                    return False
                try:
                    return backend.base_branch(other.locator) == base
                except TetherError:
                    return False

            unnamed = sorted(
                k
                for k, other in self.objects.items()
                if k != a.key and k not in keys and shares_base(other)
            )
            if unnamed:
                plan.actions[i] = Action(
                    "refuse",
                    a.key,
                    a.kind,
                    target=a.target,
                    detail=(
                        f"{base} is also {', '.join(unnamed)}'s base branch; "
                        f"landing {a.key} alone would land theirs too -- name them: "
                        f"`tether promote {' '.join(sorted(keys | set(unnamed)))}`"
                    ),
                    params={"locator": a.params.get("locator", {})},
                )

    def _share_scope_writes(self, plan: Plan, ops: tuple[str, ...]) -> None:
        """Collapse the writes of one native branch to a single action.

        Objects in one branch scope write through one branch, so promoting or
        restoring each of them would move the same ref several times -- and
        concurrently, in the fan-out. The first member keeps the write; the
        others become `share` actions that take its result.
        """
        seen: dict[tuple[str, str, str], str] = {}
        for i, a in enumerate(plan.actions):
            if a.op not in ops:
                continue
            action_scope = self._action_scope(a)
            if action_scope is None:
                continue
            scope = (*action_scope, str(a.target))
            first = seen.get(scope)
            if first is None:
                seen[scope] = a.key
                continue
            plan.actions[i] = Action(
                "share",
                a.key,
                a.kind,
                target=a.target,
                detail=f"same branch as {first}: {a.op} once, for both",
                params={**a.params, "with": first, "would": a.op},
            )

    def apply_promote(self, plan: Plan, *, verify: bool = True) -> PromoteReport:
        """Execute a plan from `plan_promote`.

        Fast-forwards and merges run concurrently. A merge that conflicts is
        rolled back by the backend and reported under `conflicts`. After a
        merge the working branch is reset to the merge result and the fork
        point updated, so the next `commit` pins what the base now holds.

        Raises:
            StalePlanError: A base branch moved since the plan was made.
            MultiObjectError: A backend failed for reasons other than conflicts.
        """
        with self._writer_lock():
            self._require_command(plan, "promote")
            report = PromoteReport(plan=plan)
            writes = [a for a in plan.actions if a.op in ("fast-forward", "merge")]
            for a in plan.actions:
                if a.op == "refuse":
                    report.refused[a.key] = a.detail
                elif a.op == "hold":
                    report.held[a.key] = a.detail
            for note in plan.notes:
                key, _, why = note.partition(": ")
                if "already at the target" in why or "nothing to promote" in why:
                    report.skipped.append(key)
            self._verify_plan(plan, "promote", verify=verify)

            message = str(plan.context.get("message") or "tether promote")
            by_key = {a.key: a for a in writes}
            pre = {
                "workspace": self.workspace.to_toml(),
                "base_states": {a.key: a.params.get("base_state") for a in writes},
            }
            op = self._begin_op("promote", plan=plan, pre=pre) if writes else None

            def run_one(key: str) -> tuple[str, State]:
                a = by_key[key]
                backend = self.backend_for(a.kind)
                locator = dict(a.params["locator"])
                source = _source_object(a.params["source"])
                # Land what was reviewed: the target state the plan captured,
                # not whatever the ref's head is by now. Every Forkable backend
                # promotes and merges from a state -- and moves the base only
                # from the head the plan saw (`expected`), so a commit that
                # lands on it in between is refused, not overwritten.
                reviewed = a.params.get("target_state")
                what: str | Pin | State = dict(reviewed) if reviewed else source
                base = a.params.get("base_state")
                expected = dict(base) if base is not None else None
                if a.op == "fast-forward":
                    new_state = _promote_ref(backend, locator, what, expected)
                    self._progress(op, "fast-forward", key=key, state=new_state)
                    return "ff", new_state
                new_state = _merge_ref(backend, locator, what, message, expected)
                self._progress(op, "merge", key=key, state=new_state)
                return "merge", new_state

            # Merges first, then fast-forwards: a merge is what can still stop
            # (a conflict, a moved base), and a fast-forward that has landed
            # cannot be taken back. When a merge stops, the fast-forwards are
            # held rather than landed beside a system that did not move.
            merges = [k for k, a in by_key.items() if a.op == "merge"]
            ffs = [k for k in by_key if k not in merges]
            results, errors = self._fanout_collect(run_one, merges)
            for key, exc in list(errors.items()):
                if isinstance(exc, MergeConflict):
                    report.conflicts[key] = list(exc.conflicts)
                    report.refused[key] = str(exc)
                    errors.pop(key)
                elif isinstance(exc, RefMovedError):
                    report.refused[key] = f"{exc}; re-run the plan"
                    errors.pop(key)
            if ffs and (report.refused or errors) and merges:
                stopped = ", ".join(sorted(set(report.refused) | set(errors)))
                for key in ffs:
                    report.held[key] = (
                        f"would fast-forward: held because the merge of {stopped} "
                        "did not land, and a bookmark lands whole or not at all"
                    )
            elif ffs:
                more, more_errors = self._fanout_collect(run_one, ffs)
                results.update(more)
                for key, exc in more_errors.items():
                    if isinstance(exc, RefMovedError):
                        report.refused[key] = f"{exc}; re-run the plan"
                    else:
                        errors[key] = exc

            # Siblings sharing the branch take the member's result.
            for a in plan.actions:
                if a.op == "share" and str(a.params.get("with")) in results:
                    results[a.key] = results[str(a.params["with"])]
            touched = False
            for key, (how, new_state) in results.items():
                (report.fast_forwarded if how == "ff" else report.merged)[key] = (
                    new_state
                )
                m = self.objects.get(key)
                working_ref = self.workspace.working_refs.get(key)
                if how == "merge" and m is not None and working_ref is not None:
                    # The fork now lags the base; reset it onto the merge result
                    # so the next commit pins what the base holds -- unless it
                    # gained writes since the plan read it: those stay, and the
                    # next commit pins them for another merge.
                    reviewed = by_key[key].params.get("target_state")
                    try:
                        self.workspace.working_refs[key] = _fork_ref(
                            self.backend_for(m.kind),
                            m.locator,
                            new_state,
                            working_ref,
                            dict(reviewed) if reviewed else None,
                        )
                    except RefMovedError as exc:
                        report.kept_forks[key] = (
                            f"{working_ref} gained writes during the merge "
                            f"({exc}); left as it is -- commit them and promote again"
                        )
                        continue
                if (
                    key in self.workspace.working_refs
                    or key in self.workspace.fork_points
                ):
                    self.workspace.fork_points[key] = dict(new_state)
                    self.workspace.last_snapshot[key] = dict(new_state)
                    touched = True
            if touched:
                write_workspace(self.root, self.workspace)
            # The dataset side of the promotion: when the whole bookmark landed
            # cleanly -- or every base already held what its commit records --
            # that commit describes the upstream branches, so the trunk
            # bookmark moves to it: `jj bookmark set main -r feature`, at the
            # commit the plan reviewed. A merge leaves states the commit does
            # not describe; commit first, then promote again to move the
            # trunk. A subset (`promote KEY...`) never moves it: the rest has
            # not landed.
            bookmark = self.workspace.bookmark
            if (
                (results or plan.context.get("current"))
                and bookmark
                and not self.on_trunk()
                and plan.context.get("rev") is None
                and not plan.context.get("subset")
                and not report.refused
                and not report.held
                and not report.merged
                and not errors
            ):
                marks = self.vcs.bookmarks()
                commit = plan.context.get("bookmark_commit") or marks.get(bookmark)
                trunk_commit = marks.get(self.config.trunk)
                if commit == trunk_commit:
                    commit = None  # already there: nothing to move
                if commit and self.config.trunk in self.vcs.conflicted_bookmarks():
                    # Absent from `marks` because it has several targets, not
                    # because there is no trunk yet: moving it would drop the
                    # other side.
                    report.trunk_held = (
                        f"{self.config.trunk} has conflicting targets since the "
                        f"plan; not moved -- `jj bookmark set {self.config.trunk} "
                        "-r REV`, merge the manifests and promote again"
                    )
                elif commit and (
                    not trunk_commit or self.vcs.is_ancestor(trunk_commit, commit)
                ):
                    self.vcs.bookmark_set(self.config.trunk, commit)
                    report.trunk_moved = commit
                elif commit:
                    # The trunk gained a commit since the plan: the data landed,
                    # the bookmark stays where it is rather than going backwards.
                    report.trunk_held = (
                        f"{self.config.trunk} moved to {str(trunk_commit)[:12]} since "
                        "the plan; not moved backwards -- merge the manifests and "
                        "promote again"
                    )
            if op is not None:
                self._end_op(
                    op,
                    result={
                        **report_dict(report),
                        "failed": {k: str(v) for k, v in errors.items()},
                    },
                )
            if errors:
                raise MultiObjectError("promote failed for some objects", errors)
            return report

    def promote(
        self: Repo,
        keys: Sequence[str] | None = None,
        *,
        rev: str | None = None,
        strategy: str = "auto",
        message: str | None = None,
    ) -> PromoteReport:
        """Move base branches to this workspace's forks.

        Equivalent to `apply_promote(plan_promote(...))`.
        """
        # Plan and apply under one lock: planning sees the state the lock
        # refreshed, and nothing in this checkout moves in between.
        with self._writer_lock():
            plan = self.plan_promote(keys, rev=rev, strategy=strategy, message=message)
            return self.apply_promote(plan)
