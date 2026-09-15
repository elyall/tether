"""`new` and `restore`: where each object's working branch comes from."""

from __future__ import annotations

import contextlib

try:  # POSIX advisory locks; Windows has no fcntl and gets no writer lock
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from tether import manifest as _m
from tether.backends.base import (
    Capability,
    VerifyStatus,
    effective_capabilities,
)
from tether.errors import (
    ConfigError,
    MultiObjectError,
    StaleWorkingCopyError,
    TetherError,
)
from tether.manifest import (
    ObjectManifest,
    Pin,
    State,
    manifest_hash,
    read_objects,
    working_ref_name,
    write_workspace,
)
from tether.plan import Action, Plan

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


from tether.repo._core import RepoCore
from tether.repo._reports import (
    short_state,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tether.repo import Repo


class ForkOps(RepoCore):
    """`new` and `restore`: where each object's working branch comes from."""

    # -- new (fork working refs) ----------------------------------------------- #
    def plan_new(
        self,
        rev: str | None = None,
        *,
        bookmark: str | None = None,
        shared: bool = False,
        keep: bool = False,
        eager: bool | None = None,
        discard: bool = False,
    ) -> Plan:
        """Compute what `new` would do without writing anywhere.

        Decides which dataset *bookmark* this workspace works on, then per
        `FORK`-capable object what its working ref is. On the trunk bookmark
        (`config.trunk`, default `main`) every object's working ref is its
        upstream branch (`trunk`). On any other bookmark it is the store
        branch named after the bookmark (`working_ref_name`): `fork` now,
        `defer-fork` (create it on the first writable `open`), or `reuse` when
        it already sits at the pin. With no bookmark the working copy is
        read-only. `apply_new` moves the VCS working copy first.

        The bookmark is `bookmark` when given (`-b NAME`: created at `rev`;
        refused if it exists), else `rev` when `rev` names a bookmark, else
        one at `rev`'s commit -- the one this workspace already works on,
        else the trunk, else the only one -- else, with no `rev`, the same
        choice among the bookmarks the VCS working copy is on. A bookmark
        another live workspace holds is refused unless `shared`: two checkouts
        writing one store branch is a choice, not a default. The trunk is
        never refused.

        Forks are deferred by default (`config.new_fork == "lazy"`) when the
        source is a native pin: a pin is durable, so the branch can be created
        whenever a write actually happens, and workspaces that never write
        leave nothing behind. A `pin = "record"` object forks immediately even
        in lazy mode: its recorded state has no native ref, so the branch
        itself is what keeps the snapshot/version from expiring.

        A working branch this workspace already has is *reset* by the fork
        (now, or when it is materialized). If its head holds writes that were
        never committed -- its content differs from the state this workspace
        last committed or forked at -- the plan carries a `refuse` action
        instead and `apply_new` raises, unless `discard` is set. Committing
        first pins the writes; `discard` throws them away on purpose.

        Args:
            rev: Revision whose manifests to fork from (`None`: working tree).
            keep: Only refresh the baseline; keep working refs.
            eager: Fork every object during `new`; default from `config.new_fork`.
            discard: Reset working branches even if they hold unpinned writes.
        """
        objects = self._objects_at(self.vcs.resolve(rev)) if rev else self.objects
        eager = (self.config.new_fork == "eager") if eager is None else eager
        known = self.vcs.bookmarks()
        create = False
        if bookmark is not None:
            if bookmark in known:
                raise ConfigError(
                    f"bookmark {bookmark!r} exists; `tether new {bookmark}` joins it"
                )
            if _m.working_ref_generation(working_ref_name("00000000", bookmark)):
                raise ConfigError(
                    f"bookmark {bookmark!r} ends in `.<number>`, which is how a store "
                    "names a sibling of a branch it cannot reset; pick another name"
                )
            create = True
        elif rev is not None and rev in known:
            bookmark = rev
        elif rev is not None:
            at = [b for b, c in known.items() if c == self.vcs.resolve(rev)]
            bookmark = self._pick_bookmark(at)
        elif keep:
            bookmark = self.workspace.bookmark
        else:
            here = self.vcs.current_bookmarks()
            if self.workspace.bookmark is not None and not here:
                bookmark = self.workspace.bookmark
            else:
                bookmark = self._pick_bookmark(here)
        plan = Plan(
            command="new",
            context={
                "rev": rev,
                "bookmark": bookmark,
                "create": create,
                "shared": shared,
                "keep": keep,
                "eager": eager,
                "discard": discard,
                "manifest_hash": manifest_hash(objects),
                "workspace_id": self.workspace.workspace_id,
            },
        )
        if keep:
            plan.notes.append("keep: refresh the baseline only; working refs unchanged")
            return self._with_new_preconditions(plan)
        if bookmark is None:
            plan.notes.append(
                "no bookmark: read-only working copy (`tether new -b NAME` to write)"
            )
            return self._with_new_preconditions(plan)
        trunk = bookmark == self.config.trunk
        if not trunk and not shared:
            holders = self.bookmark_holders(bookmark)
            if holders:
                plan.actions.append(
                    Action(
                        "refuse",
                        detail=(
                            f"bookmark {bookmark!r} is held by live workspace(s) "
                            f"{', '.join(holders)}; pass --shared to write the same "
                            "store branches from here too"
                        ),
                        params={"bookmark": bookmark, "holders": holders},
                    )
                )
                return self._with_new_preconditions(plan)
        if trunk:
            plan.notes.append(
                f"on trunk {bookmark!r}: writes land on each object's upstream branch"
            )
        # One branch per (kind, scope): objects that live in the same native
        # branch space -- two databases of one Neon project -- share the fork.
        # The first member plans it; later members `share` it, provided they
        # pin the same state of that branch (a branch is at one point).
        scoped: dict[tuple[str, str], str] = {}  # (kind, scope) -> first key
        for key in sorted(objects):
            m = objects[key]
            backend = self.backend_for(m.kind)
            eff = effective_capabilities(backend, m.locator, m.policy)
            if Capability.FORK not in eff:
                continue
            if trunk:
                try:
                    branch = backend.base_branch(m.locator)
                except TetherError as exc:
                    plan.notes.append(f"{key}: no upstream branch to write to ({exc})")
                    continue
                plan.actions.append(
                    Action(
                        "trunk", key, m.kind, target=branch, detail="upstream branch"
                    )
                )
                continue
            if m.state is None:
                plan.notes.append(
                    f"{key}: nothing committed yet; fork after first commit"
                )
                continue
            name = working_ref_name(self.config.dataset_id, bookmark)
            scope = (m.kind, backend.branch_scope(m.locator))
            if scope in scoped:
                first_key = scoped[scope]
                first = objects[first_key]
                first_action = next(
                    (a for a in plan.actions if a.key == first_key), None
                )
                if first_action is None or first_action.op == "refuse":
                    plan.notes.append(
                        f"{key}: shares a branch with {first_key}, which cannot fork"
                    )
                elif not self._same(m.kind, m.state, first.state):
                    plan.actions.append(
                        Action(
                            "refuse",
                            key,
                            m.kind,
                            target=name,
                            detail=(
                                f"shares branch {name} with {first_key!r} but pins "
                                f"{short_state(m.state)} where it pins "
                                f"{short_state(first.state)}; a branch is at one "
                                "point -- commit them together (`tether pull` on "
                                "the trunk) before forking"
                            ),
                            params={"with": first_key},
                        )
                    )
                else:
                    plan.actions.append(
                        Action(
                            "share",
                            key,
                            m.kind,
                            target=name,
                            detail=f"same branch as {first_key} ({first_action.op})",
                            params={"with": first_key, "state": m.state},
                        )
                    )
                continue
            scoped[scope] = key
            # The bookmark's branch may already exist in this system -- joining
            # a bookmark, or `new` again on the one we are on. Its head decides
            # whether it is kept, reset (recorded so the op log can restore it),
            # or refused. Another bookmark's branch, whatever this workspace
            # wrote through before, is never touched: branches belong to
            # bookmarks, not to checkouts.
            existing: str | None = None
            head: State | None = None
            try:
                if name in backend.list_working_refs(m.locator):
                    existing = name
            except TetherError as exc:
                # Not knowing whether the branch exists is not the same as it
                # being absent: a fork of an existing branch resets it. Refuse.
                plan.actions.append(
                    Action(
                        "refuse",
                        key,
                        m.kind,
                        target=name,
                        detail=(
                            f"could not list branches ({exc}); whether {name} "
                            "exists is unknown and a fork would reset it blind"
                        ),
                    )
                )
                continue
            if existing is not None:
                try:
                    head = backend.fingerprint(m.locator, existing)
                except TetherError as exc:
                    # The branch is there but its head is unknown; a fork would
                    # reset it blind. Refuse rather than guess -- `--discard`
                    # does not apply, since what would be lost is unknown too.
                    plan.actions.append(
                        Action(
                            "refuse",
                            key,
                            m.kind,
                            target=existing,
                            detail=(
                                f"{existing} exists but its head could not be "
                                f"read ({exc}); a fork would reset it blind"
                            ),
                            params={"existing": existing},
                        )
                    )
                    continue
            if m.pin is not None:
                source = {"pin": m.pin.to_dict()}
                detail = f"from pin {m.pin.ref}"
                durable = True
            elif Capability.ADDRESSABLE in eff:
                source = {"state": m.state}
                detail = f"from recorded state {short_state(m.state)} (no pin)"
                durable = False
            else:
                plan.notes.append(f"{key}: no pin and not addressable; cannot fork")
                continue
            params: dict[str, Any] = dict(source)
            if (
                existing is not None
                and head is not None
                and self._same(m.kind, head, m.state)
            ):
                # The branch already holds exactly the pinned state (the usual
                # commit-then-new). Nothing to reset -- and on Neon a reset of
                # a branch with pin children would have meant a sibling.
                plan.actions.append(
                    Action(
                        "reuse",
                        key,
                        m.kind,
                        target=existing,
                        detail=f"already at {detail.removeprefix('from ')}; kept",
                        params={"then_state": m.state},
                    )
                )
                continue
            if existing is not None:
                params["existing"] = existing
                params["head"] = head
                detail += f"; resets {existing}"
                # Anything on the branch beyond what this workspace last
                # committed (or forked from) is about to be thrown away.
                recorded = [
                    self.workspace.base_states.get(key),
                    self.objects[key].state if key in self.objects else None,
                    self.workspace.fork_points.get(key),
                ]
                unpinned = head is not None and not any(
                    self._same(m.kind, head, k) for k in recorded if k is not None
                )
                if unpinned and not discard:
                    plan.actions.append(
                        Action(
                            "refuse",
                            key,
                            m.kind,
                            target=existing,
                            detail=(
                                f"{existing} has writes since this workspace last "
                                f"committed ({short_state(head)}); commit them, or "
                                "pass --discard to throw them away"
                            ),
                            params=params,
                        )
                    )
                    continue
                if unpinned:
                    detail += f", discarding its writes ({short_state(head)})"
            if eager or not durable:
                if not durable and not eager:
                    detail += "; forked now so the state cannot expire"
                plan.actions.append(
                    Action(
                        "fork", key, m.kind, target=name, detail=detail, params=params
                    )
                )
            else:
                plan.actions.append(
                    Action(
                        "defer-fork",
                        key,
                        m.kind,
                        target=name,
                        detail=f"{detail}; created on first writable open",
                        params=params,
                    )
                )
        return self._with_new_preconditions(plan)

    def _with_new_preconditions(self, plan: Plan) -> Plan:
        """What `apply_new` must find unchanged: the manifests at the target,
        this workspace, no new holder of the bookmark, and every branch the
        plan keeps, resets, or creates afresh."""
        ctx = plan.context
        plan.require(
            "manifest_hash",
            ctx.get("manifest_hash"),
            rev=ctx.get("rev"),
            detail="manifests at the target differ from the plan; re-run the plan",
        )
        plan.require(
            "workspace_id",
            ctx.get("workspace_id"),
            detail="this plan was made in another workspace; re-run the plan here",
        )
        bookmark = ctx.get("bookmark")
        if bookmark and bookmark != self.config.trunk and not ctx.get("shared"):
            plan.require(
                "no_new_holders",
                bookmark=bookmark,
                detail=f"bookmark {bookmark!r} is now held by live workspace(s) "
                "{observed}; re-run the plan (or pass --shared)",
            )
        for a in plan.actions:
            if a.key not in self.objects:
                continue
            locator = dict(self.objects[a.key].locator)
            if a.op == "reuse":
                plan.require(
                    "ref_head",
                    a.params.get("then_state"),
                    key=a.key,
                    backend=a.kind,
                    locator=locator,
                    ref=a.target,
                    what=f"new {a.key}",
                )
            elif a.op == "fork" and a.params.get("existing"):
                plan.require(
                    "ref_head",
                    a.params.get("head"),
                    key=a.key,
                    backend=a.kind,
                    locator=locator,
                    ref=str(a.params["existing"]),
                    what=f"new {a.key}",
                )
            elif a.op == "fork":
                plan.require(
                    "ref_absent",
                    key=a.key,
                    backend=a.kind,
                    locator=locator,
                    ref=a.target,
                    detail=f"new {a.key}: {a.target} exists since the plan was made; "
                    "re-run the plan",
                )
        return plan

    def apply_new(self: Repo, plan: Plan, *, verify: bool = True) -> None:
        """Execute a plan from `plan_new`: move the VCS working copy, fork, record refs.

        Raises:
            StalePlanError: The manifests at the target differ from the plan's.
            MultiObjectError: A pin is missing or a fork failed.
        """
        with self._writer_lock():
            self._verify_plan(plan, "new", verify=verify)
            refused = [a for a in plan.actions if a.op == "refuse"]
            if refused:
                if all(a.key for a in refused):
                    raise TetherError(
                        "refusing to reset working branches with unpinned writes:\n"
                        + "\n".join(f"  {a.key}: {a.detail}" for a in refused)
                    )
                raise TetherError("; ".join(a.detail for a in refused))
            rev = plan.context.get("rev")
            bookmark = plan.context.get("bookmark")
            create = bool(plan.context.get("create"))
            pre = {"workspace": self.workspace.to_toml(), "vcs": self.vcs.position()}
            # The heads `new` will reset are known from the plan; journal them now
            # so an interrupted run still says what it was about to replace.
            pre["heads"] = {
                a.key: a.params.get("head")
                for a in plan.actions
                if a.op == "fork" and a.params.get("existing")
            }
            op = self._begin_op("new", plan=plan, pre=pre)
            if create:
                self.vcs.new_bookmark(str(bookmark), str(rev) if rev else None)
                self.objects = read_objects(self.root)
            elif rev:
                self.vcs.new(str(rev))
                self.objects = read_objects(self.root)
            self.workspace.bookmark = str(bookmark) if bookmark else None
            if plan.context.get("keep"):
                self._mark_base_states(
                    set(self.workspace.working_refs) | set(self.workspace.pending_forks)
                )
                write_workspace(self.root, self.workspace)
                self._end_op(op, result={"vcs": self.vcs.position()})
                return

            working_refs: dict[str, str] = {}
            pending: dict[str, str] = {}
            pending_resets: dict[str, State] = {}
            reused: dict[str, State] = {}
            forks = {a.key: a for a in plan.actions if a.op == "fork"}
            for a in plan.actions:
                if a.op == "trunk":
                    working_refs[a.key] = a.target
                elif a.op == "defer-fork":
                    pending[a.key] = a.target
                    if a.params.get("existing") and a.params.get("head") is not None:
                        pending_resets[a.key] = dict(a.params["head"])
                elif a.op == "reuse":
                    working_refs[a.key] = a.target
                    # The branch is kept, not forked: where it diverged from
                    # the base is unchanged. Only a branch this workspace never
                    # knew (joining a bookmark) takes the pin as its best guess.
                    known = self.workspace.fork_points.get(a.key)
                    reused[a.key] = (
                        dict(known)
                        if known is not None
                        else dict(a.params["then_state"])
                    )

            # The VCS has moved; before the first store write, make the
            # workspace agree with it. Every planned fork is recorded as
            # *pending* (with the reset `new` agreed to, where the branch
            # exists), so a process killed during the fan-out leaves exactly a
            # lazy `new`: same bookmark on both sides, branches created so far
            # found at the pin by the next open, the rest created on demand.
            leftovers = {
                k: v
                for k, v in self.workspace.working_refs.items()
                if k not in self.objects
            }
            interim_pending = dict(pending)
            interim_resets = dict(pending_resets)
            for key, a in forks.items():
                interim_pending[key] = a.target
                if a.params.get("existing") and a.params.get("head") is not None:
                    interim_resets[key] = dict(a.params["head"])
            for a in plan.actions:
                if a.op == "share" and str(a.params["with"]) in interim_pending:
                    first = str(a.params["with"])
                    interim_pending[a.key] = interim_pending[first]
                    if first in interim_resets:
                        interim_resets[a.key] = interim_resets[first]
            self.workspace.working_refs = {**leftovers, **working_refs}
            self.workspace.pending_forks = interim_pending
            self.workspace.pending_resets = interim_resets
            self.workspace.fork_points = {
                **{
                    k: v
                    for k, v in self.workspace.fork_points.items()
                    if k in leftovers
                },
                **reused,
            }
            self.workspace.base_states = {
                k: v for k, v in self.workspace.base_states.items() if k in leftovers
            }
            self._mark_base_states(set(working_refs) | set(interim_pending))
            write_workspace(self.root, self.workspace)

            def fork_one(key: str) -> str:
                m = self.objects[key]
                ref = self._fork_from_manifest(m, forks[key].target)
                self._progress(op, "fork", key=key, ref=ref)
                return ref

            # Fork concurrently; a failure for one object must not hide the branches
            # created for the others, so record everything that succeeded before
            # reporting what did not. A second `new` completes the job (existing
            # branches are reset onto the pin, not duplicated).
            forked, errors = self._fanout_collect(fork_one, list(forks))
            working_refs.update(forked)
            # A fork that failed stays pending, as the interim state had it: the
            # next `new` or writable open creates it (a branch that did get
            # created is found at the pin and reused).
            for key, a in forks.items():
                if key not in forked:
                    pending[key] = a.target
                    if a.params.get("existing") and a.params.get("head") is not None:
                        pending_resets[key] = dict(a.params["head"])
            # Objects sharing a branch follow the member that planned it.
            for a in plan.actions:
                if a.op != "share":
                    continue
                first = str(a.params["with"])
                if first in forked:
                    working_refs[a.key] = forked[first]
                elif first in pending:
                    pending[a.key] = pending[first]
                    if first in pending_resets:
                        pending_resets[a.key] = pending_resets[first]
                elif first in reused:
                    working_refs[a.key] = a.target
                    known = self.workspace.fork_points.get(a.key)
                    reused[a.key] = (
                        dict(known) if known is not None else dict(a.params["state"])
                    )

            # Keep refs of removed objects around until `gc` deletes their branches.
            self.workspace.working_refs = {**leftovers, **working_refs}
            self.workspace.pending_forks = pending
            self.workspace.pending_resets = pending_resets
            # Fork points: what each branch was created from (promote's baseline).
            fork_points = {
                k: v for k, v in self.workspace.fork_points.items() if k in leftovers
            }
            shared = {
                a.key for a in plan.actions if a.op == "share" and a.key in working_refs
            }
            for key in set(forked) | shared:
                state = self.objects[key].state
                if state is not None:
                    fork_points[key] = dict(state)
            fork_points.update(reused)  # kept branches keep their fork point
            self.workspace.fork_points = fork_points
            self.workspace.base_states = {
                k: v for k, v in self.workspace.base_states.items() if k in leftovers
            }
            self._mark_base_states(set(working_refs) | set(pending))
            write_workspace(self.root, self.workspace)
            # What this op did to the stores, and what it replaced, for undo.
            reset = {
                k: a.params
                for k, a in forks.items()
                if k in forked and a.params.get("existing")
            }
            self._end_op(
                op,
                result={
                    "vcs": self.vcs.position(),
                    "bookmark": bookmark,
                    "created_bookmark": bookmark if create else None,
                    "created": sorted(k for k in forked if k not in reset),
                    "reset": sorted(reset),
                    "reused": sorted(reused),
                    "working_refs": dict(working_refs),
                    "pending_forks": dict(pending),
                    "failed": sorted(errors),
                },
                pre={"heads": {k: p.get("head") for k, p in reset.items()}},
            )
            if errors:
                raise MultiObjectError(
                    f"could not fork working refs for {', '.join(sorted(errors))} "
                    f"({len(forked)} of {len(forks)} forked and recorded; run `new` "
                    "again -- it resets those branches too, so do not write to them "
                    "first)",
                    errors,
                )

    def _adopt_into_bookmark(self: Repo, key: str, initial: State) -> str | None:
        """Give a just-created store its working branch on the current
        bookmark, so the first writable `open` needs no `new`.

        On the trunk there is nothing to do: a Forkable object's working ref
        *is* its base branch. Elsewhere the branch `new` would have planned is
        forked now from the store's initial state (a `new` cannot plan it: the
        object has no committed state to fork from yet), and the fork point
        recorded, so `promote` later sees a clean fast-forward.
        """
        bookmark = self.workspace.bookmark
        if not bookmark or self.on_trunk():
            return None
        m = self.objects[key]
        backend = self.backend_for(m.kind)
        if Capability.FORK not in effective_capabilities(backend, m.locator, m.policy):
            return None
        name = working_ref_name(self.config.dataset_id, bookmark)
        ref = backend.fork(m.locator, dict(initial), name)
        self.workspace.working_refs[key] = ref
        self.workspace.pending_forks.pop(key, None)
        self.workspace.fork_points[key] = dict(initial)
        write_workspace(self.root, self.workspace)
        return ref

    def _fork_from_manifest(self: Repo, m: ObjectManifest, name: str) -> str:
        """Create working branch `name` from a manifest's pin (or recorded state)."""
        backend = self.backend_for(m.kind)
        assert m.state is not None
        eff = effective_capabilities(backend, m.locator, m.policy)
        source = self._pinned_source(m, backend, eff)
        if isinstance(source, Pin):
            return backend.fork(m.locator, source, name)
        # No usable pin: the recorded state is the fork point while the store
        # still has it (between a deletion and `repair`, or a record-only pin).
        report = backend.verify(m.locator, m.state, None, deep=True)
        if report.status is VerifyStatus.MISSING:
            gone = "" if m.pin is None else "pin missing and "
            raise TetherError(f"{gone}recorded state is gone: {report.message}")
        return backend.fork(m.locator, source, name)

    def _forget_working_state(self, key: str) -> None:
        """Drop everything this workspace knows about `key`'s working branch."""
        for table in (
            "working_refs",
            "pending_forks",
            "pending_resets",
            "base_states",
            "fork_points",
        ):
            getattr(self.workspace, table).pop(key, None)

    def materialize_fork(self: Repo, key: str) -> str:
        """Create the deferred working branch for `key` now and return it.

        Called by `open` on the first writable handle; also useful to
        pre-create branches for a job. No-op when the branch already exists.

        Raises:
            ConfigError: Unknown key, or no fork is pending for it.
            StaleWorkingCopyError: The workspace is stale; run `new` first.
            TetherError: The pin (or recorded state) to fork from is gone.
        """
        with self._writer_lock():
            existing = self.workspace.working_refs.get(key)
            if existing is not None:
                return existing
            name = self.workspace.pending_forks.get(key)
            if name is None:
                raise ConfigError(f"no working branch pending for {key!r}; run `new`")
            stale = self.stale_keys()
            if stale:
                raise StaleWorkingCopyError(
                    f"working copy is stale: the committed state of {', '.join(stale)} "
                    "changed since `new`; run `tether new` before writing"
                )
            m = self.objects[key]
            backend = self.backend_for(m.kind)
            pre: dict[str, Any] = {"workspace": self.workspace.to_toml()}
            scope = (m.kind, backend.branch_scope(m.locator))

            def siblings() -> list[str]:
                """Other keys whose pending fork is this very branch."""
                return [
                    k
                    for k, n in self.workspace.pending_forks.items()
                    if k != key
                    and n == name
                    and k in self.objects
                    and (
                        self.objects[k].kind,
                        self.backend_for(self.objects[k].kind).branch_scope(
                            self.objects[k].locator
                        ),
                    )
                    == scope
                ]

            def adopt(ref: str, keys: list[str]) -> None:
                for k in keys:
                    self.workspace.working_refs[k] = ref
                    self.workspace.pending_forks.pop(k, None)
                    self.workspace.pending_resets.pop(k, None)
                    state = self.objects[k].state
                    if state is not None:
                        self.workspace.fork_points[k] = dict(state)
                self._mark_base_states(keys)

            if name in backend.list_working_refs(m.locator):
                # The branch exists. A writable open resets it only when `new`
                # reviewed exactly this head and agreed (`pending_resets`); a
                # branch already at this object's pin, or one a sibling of the
                # same scope already writes through, is reused as it is; any
                # other head -- it moved since `new` -- stops here and `new`
                # decides again.
                head = backend.fingerprint(m.locator, name)
                owned = any(
                    r == name
                    and k != key
                    and k in self.objects
                    and (
                        self.objects[k].kind,
                        self.backend_for(self.objects[k].kind).branch_scope(
                            self.objects[k].locator
                        ),
                    )
                    == scope
                    for k, r in self.workspace.working_refs.items()
                )
                if owned or (m.state is not None and self._same(m.kind, head, m.state)):
                    adopt(name, [key, *siblings()])
                    write_workspace(self.root, self.workspace)
                    return name
                agreed = self.workspace.pending_resets.get(key)
                if agreed is None or not self._same(m.kind, head, agreed):
                    raise StaleWorkingCopyError(
                        f"branch {name} holds writes ({short_state(head)}) that `new` "
                        f"did not see; a writable open never resets a branch -- run "
                        "`tether new` to decide (it refuses while the branch holds "
                        "uncommitted writes; --discard throws them away)"
                    )
                pre["heads"] = {key: head}
            op = self._begin_op("fork", pre=pre)
            ref = self._fork_from_manifest(m, name)
            adopt(ref, [key, *siblings()])
            write_workspace(self.root, self.workspace)
            self._end_op(
                op,
                result={
                    "key": key,
                    "ref": ref,
                    "kind": m.kind,
                    "locator": dict(m.locator),
                },
            )
            return ref

    def new(
        self: Repo,
        rev: str | None = None,
        *,
        bookmark: str | None = None,
        shared: bool = False,
        keep: bool = False,
        eager: bool | None = None,
        discard: bool = False,
    ) -> None:
        """Start working on a bookmark: set up writable refs off its pins.

        Equivalent to `apply_new(plan_new(...))`. On the trunk bookmark every
        `FORK`-capable object's working ref is its upstream branch; on any
        other bookmark it is the store branch `working_ref_name(dataset_id,
        bookmark)`, forked from the pin -- by default *lazily*, on the first
        writable `open` (see `plan_new`), or during `new` with `eager`.
        `pin = "record"` objects always fork now, from their recorded state.
        Objects with no committed state yet are skipped. Forks run
        concurrently. With no bookmark the working copy is read-only.

        Args:
            rev: Move the VCS working copy here first (`jj new`; in git, `switch`
                to a branch or onto a `tether/<rev12>` branch for a commit)
                and reload the manifests; `None` keeps the current commit.
            bookmark: Create this bookmark at `rev` and work on it (`-b`).
            shared: Work on a bookmark another live workspace holds.
            keep: Only refresh the stale-detection baseline; keep working refs.
            eager: Create every branch now; default `config.new_fork == "eager"`.
            discard: Reset working branches that hold unpinned writes (see
                `plan_new`); without it such a `new` is refused.

        Raises:
            TetherError: A working branch holds unpinned writes and `discard`
                is not set.
            MultiObjectError: A pin is missing or a fork failed.
        """
        # Plan and apply under one lock: planning sees the state the lock
        # refreshed, and nothing in this checkout moves in between.
        with self._writer_lock():
            self.apply_new(
                self.plan_new(
                    rev,
                    bookmark=bookmark,
                    shared=shared,
                    keep=keep,
                    eager=eager,
                    discard=discard,
                ),
            )

    # -- restore --------------------------------------------------------------- #
    def plan_restore(
        self: Repo, keys: Sequence[str], rev: str, *, discard: bool = False
    ) -> Plan:
        """Compute what re-forking `keys` from the pins at `rev` would do.

        The per-object `jj restore --from REV`: the object's working branch is
        reset onto (or created from) what `rev`'s manifest pinned, while the
        rest of the workspace and the working-tree manifests stay put. The
        object is *not* stale afterwards -- the next `commit` pins what was
        restored -- but `promote` sees the branch's fork point move to `rev`'s
        state, so a moved base is a divergence, not a fast-forward. A branch
        holding unpinned writes is refused unless `discard`.

        Args:
            keys: Objects to restore.
            rev: Revision whose manifests to take the pins from.
            discard: Reset a branch even if it holds unpinned writes.

        Raises:
            ConfigError: A key is not registered.
        """
        commit = self.vcs.resolve(rev)
        then = self._objects_at(commit)
        plan = Plan(
            command="restore",
            context={
                "from_rev": rev,
                "from_commit": commit,
                "discard": discard,
                "manifest_hash": self.current_manifest_hash(),
                "workspace_id": self.workspace.workspace_id,
            },
        )
        plan.require(
            "manifest_hash",
            plan.context["manifest_hash"],
            detail="manifests changed since the plan was made; re-run the plan",
        )
        for key in keys:
            now = self.objects.get(key)
            if now is None:
                raise ConfigError(f"no such object: {key}")
            m = then.get(key)
            backend = self.backend_for(now.kind)
            eff = effective_capabilities(backend, now.locator, now.policy)
            refuse: str | None = None
            if m is None:
                refuse = f"not registered at {rev}"
            elif Capability.FORK not in eff or self.on_trunk():
                refuse = "no working branch to restore (not Forkable, or on trunk)"
            elif m.state is None:
                refuse = f"nothing committed at {rev}"
            elif m.pin is None and Capability.ADDRESSABLE not in eff:
                refuse = f"no pin at {rev} and the backend cannot fork from a state"
            if refuse is not None:
                plan.actions.append(Action("refuse", key, now.kind, detail=refuse))
                continue
            assert m is not None and m.state is not None
            existing = self.workspace.working_refs.get(key)
            name = (
                existing
                or self.workspace.pending_forks.get(key)
                or self._working_ref_for(key)
            )
            params: dict[str, Any] = {"then": m.to_toml(), "then_state": m.state}
            detail = f"from {rev}: {m.pin.ref if m.pin else short_state(m.state)}"
            if existing is not None:
                head = self._branch_has_new_writes(key, existing)
                params["existing"] = existing
                params["head"] = self.workspace.last_snapshot.get(key)
                with contextlib.suppress(TetherError):
                    params["head"] = backend.fingerprint(now.locator, existing)
                detail += f"; resets {existing}"
                if head is not None and not discard:
                    plan.actions.append(
                        Action(
                            "refuse",
                            key,
                            now.kind,
                            target=existing,
                            detail=f"{existing} has writes since this workspace last "
                            f"committed ({short_state(head)}); commit them, or pass "
                            "--discard to throw them away",
                        )
                    )
                    continue
                if head is not None:
                    detail += f", discarding its writes ({short_state(head)})"
            plan.actions.append(
                Action("fork", key, now.kind, target=name, detail=detail, params=params)
            )
        self._close_restore_over_scopes(plan, set(keys), then, rev)
        return plan

    def _close_restore_over_scopes(
        self, plan: Plan, keys: set[str], then: Mapping[str, ObjectManifest], rev: str
    ) -> None:
        """A restore resets a native branch; every object writing through that
        branch is restored with it, whether named or not.

        Unnamed siblings make the plan a refusal (name them, so the plan says
        what moves); named siblings must pin the same state of the branch at
        `rev` (a branch is at one point), and only the first of them forks --
        the rest `share` the reset.
        """

        def scope_of(key: str) -> tuple[str, str] | None:
            m = self.objects.get(key)
            if m is None:
                return None
            return (m.kind, self.backend_for(m.kind).branch_scope(m.locator))

        forks = {a.key: a for a in plan.actions if a.op == "fork"}
        first_by_scope: dict[tuple[str, str], str] = {}
        for i, a in enumerate(plan.actions):
            if a.op != "fork":
                continue
            scope = scope_of(a.key)
            if scope is None:
                continue
            # Siblings writing through this very branch.
            siblings = [
                k
                for k in self.objects
                if k != a.key
                and scope_of(k) == scope
                and (
                    self.workspace.working_refs.get(k) == a.target
                    or self.workspace.pending_forks.get(k) == a.target
                )
            ]
            unnamed = sorted(k for k in siblings if k not in keys)
            if unnamed:
                plan.actions[i] = Action(
                    "refuse",
                    a.key,
                    a.kind,
                    target=a.target,
                    detail=(
                        f"{a.target} is also {', '.join(unnamed)}'s working branch; "
                        f"restoring {a.key} alone would move theirs too -- name them: "
                        f"`tether restore {' '.join(sorted(keys | set(unnamed)))} "
                        f"--from {rev}`"
                    ),
                )
                continue
            first = first_by_scope.get(scope)
            if first is None:
                first_by_scope[scope] = a.key
                continue
            first_action = forks[first]
            mine = then.get(a.key)
            if mine is None or not self._same(
                a.kind, mine.state, first_action.params.get("then_state")
            ):
                plan.actions[i] = Action(
                    "refuse",
                    a.key,
                    a.kind,
                    target=a.target,
                    detail=(
                        f"shares {a.target} with {first!r} but pins a different "
                        f"state of it at {rev}; a branch is at one point"
                    ),
                )
                continue
            plan.actions[i] = Action(
                "share",
                a.key,
                a.kind,
                target=a.target,
                detail=f"same branch as {first}: reset once, for both",
                params={**a.params, "with": first},
            )

    def apply_restore(self: Repo, plan: Plan, *, verify: bool = True) -> dict[str, str]:
        """Execute a plan from `plan_restore`; returns key -> working ref.

        Raises:
            TetherError: The plan carries a `refuse`, or a pin at the source
                revision is gone.
            StalePlanError: The manifests changed since the plan was made.
        """
        with self._writer_lock():
            self._require_command(plan, "restore")
            refused = [a for a in plan.actions if a.op == "refuse"]
            if refused:
                raise TetherError(
                    "cannot restore:\n"
                    + "\n".join(f"  {a.key}: {a.detail}" for a in refused)
                )
            self._verify_plan(plan, "restore", verify=verify)
            forks = [a for a in plan.actions if a.op == "fork"]
            pre = {
                "workspace": self.workspace.to_toml(),
                "heads": {
                    a.key: a.params.get("head")
                    for a in forks
                    if a.params.get("existing")
                },
            }
            op = self._begin_op("restore", plan=plan, pre=pre)
            shares = {
                a.key: a for a in plan.actions if a.op == "share" and "with" in a.params
            }
            done: dict[str, str] = {}

            def adopt(key: str, ref: str, then_state: State) -> None:
                done[key] = ref
                self.workspace.working_refs[key] = ref
                self.workspace.pending_forks.pop(key, None)
                self.workspace.pending_resets.pop(key, None)
                self.workspace.fork_points[key] = dict(then_state)
                self.workspace.last_snapshot[key] = dict(then_state)

            # Every head is checked before the first reset (stale means nothing
            # happens), and the workspace is written after *each* reset -- with
            # the siblings that share the branch -- so a process killed between
            # two resets leaves every branch that was reset described as such.
            if verify:
                for a in forks:
                    if a.params.get("existing"):
                        self._require_head(
                            self.backend_for(a.kind),
                            self.objects[a.key].locator,
                            str(a.params["existing"]),
                            a.params.get("head"),
                            what=f"restore {a.key}",
                        )
            for a in forks:
                m = ObjectManifest.from_toml(str(a.params["then"]))
                ref = self._fork_from_manifest(m, a.target)
                self._progress(op, "fork", key=a.key, ref=ref)
                adopt(a.key, ref, dict(a.params["then_state"]))
                for sibling in shares.values():
                    if str(sibling.params["with"]) == a.key:
                        adopt(sibling.key, ref, dict(sibling.params["then_state"]))
                # What the branch now holds is deliberate: it corresponds to the
                # working tree's manifest as far as staleness is concerned.
                self._mark_base_states(done)
                write_workspace(self.root, self.workspace)
            self._end_op(
                op,
                result={
                    "created": sorted(
                        k
                        for k, a in ((a.key, a) for a in forks)
                        if not a.params.get("existing")
                    ),
                    "reset": sorted(a.key for a in forks if a.params.get("existing")),
                    "working_refs": done,
                    "from_commit": plan.context.get("from_commit"),
                },
            )
            return done

    def restore(
        self: Repo, keys: Sequence[str], rev: str, *, discard: bool = False
    ) -> dict[str, str]:
        """Re-fork `keys` from the pins at `rev` (see `plan_restore`)."""
        # Plan and apply under one lock: planning sees the state the lock
        # refreshed, and nothing in this checkout moves in between.
        with self._writer_lock():
            return self.apply_restore(self.plan_restore(keys, rev, discard=discard))
