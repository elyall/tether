"""`gc`, `forget-workspace`, and `abandon`: releasing what nothing references."""

from __future__ import annotations

try:  # POSIX advisory locks; Windows has no fcntl and gets no writer lock
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from tether import manifest as _m
from tether.backends.base import (
    Capability,
    ObjectBackend,
    effective_capabilities,
)
from tether.errors import (
    MultiObjectError,
    StalePlanError,
    TetherError,
)
from tether.manifest import (
    ObjectManifest,
    Pin,
    State,
    bookmark_slug,
    listing_name,
    listings_dir,
    pin_dataset,
    read_objects,
    read_workspace,
    ref_for_pin,
    working_ref_bookmark,
    working_ref_dataset,
    working_ref_workspace,
    write_workspace,
)
from tether.oplog import (
    report_dict,
)
from tether.plan import Action, Plan

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


from tether.repo._core import RepoCore
from tether.repo._reports import (
    AbandonReport,
    ForgetWorkspaceReport,
    GcReport,
    short_state,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


class GcOps(RepoCore):
    """`gc`, `forget-workspace`, and `abandon`: releasing what nothing references."""

    # -- gc -------------------------------------------------------------------- #
    def plan_gc(
        self,
        *,
        prune_bookmarks: bool = False,
        keep_bookmarks: set[str] | None = None,
        force_prune: bool = False,
    ) -> Plan:
        """Compute what `gc` would release without writing anywhere.

        By default `gc` releases only tether's own *refs*: `unpin` native pins
        no manifest in VCS history (or the working tree) references,
        `forget-working-ref` for this workspace's refs whose object was
        removed (the native branch is left alone), and `delete-listing` for
        `.tether/listings/` files no manifest names.

        With `prune_bookmarks`, every `tether.ws.*` branch in each system is
        considered: branches of bookmarks that no longer exist in the VCS and
        that no live checkout works on (`keep_bookmarks` adds names to keep,
        e.g. a bookmark that only exists on another machine), legacy
        per-workspace branches from before bookmarks, and this workspace's
        branches no current object uses. A branch is planned for
        `delete-branch` only when nothing on it would be lost: its head state is
        natively pinned by some manifest in history, or equals the base
        branch's head. Otherwise it gets a `keep-branch` note (unpinned writes,
        a pin-less recorded state, or a `BRANCH_IS_STORAGE` backend such as
        Neon). `force_prune` deletes those too.
        """
        referenced: dict[str, set[str]] = {}

        def key_for(backend: ObjectBackend, locator: dict) -> str:
            # Pins are listed per *namespace* (a Neon project, an Iceberg
            # table), which may hold several objects; the references that
            # keep a pin alive must be collected over the whole namespace.
            return f"{backend.kind}|{backend.ref_namespace(locator)}"

        # The digest is taken *before* the walk. A commit that lands after it
        # -- during the walk or later -- changes the digest the apply compares
        # against and stales the plan; one that landed before it is in the
        # walk. Taken after, a commit in between would be missing from the
        # references yet present in the digest, and the plan would pass.
        history_digest = self.vcs.history_digest()
        vcs_head = self._vcs_head_or_none()
        history_manifests: list[ObjectManifest] = []
        all_manifests: list[ObjectManifest] = []
        seen: set[str] = set()
        for _rev, objects in self._iter_history_objects():
            for m in objects.values():
                all_manifests.append(m)
                if m.pin is None:
                    continue
                backend = self.backend_for(m.kind)
                referenced.setdefault(key_for(backend, m.locator), set()).add(m.pin.id)
                sig = f"{m.kind}:{m.pin.id}:{key_for(backend, m.locator)}"
                if sig not in seen:
                    seen.add(sig)
                    history_manifests.append(m)
        for m in self.objects.values():
            if m.pin is not None:
                backend = self.backend_for(m.kind)
                referenced.setdefault(key_for(backend, m.locator), set()).add(m.pin.id)

        plan = Plan(
            command="gc",
            context={
                "workspace_id": self.workspace.workspace_id,
                "prune_bookmarks": prune_bookmarks,
                "keep_bookmarks": sorted(keep_bookmarks or ()),
                "force_prune": force_prune,
                "manifest_hash": self.current_manifest_hash(),
                "vcs_head": vcs_head,
                # Every visible commit, not just this checkout's: a bookmark
                # committed in another workspace may reference a pin this plan
                # would release, and this checkout's head would not move.
                "history_digest": history_digest,
            },
        )
        # Unpins are justified by what history references; a commit made since
        # the plan (here or in another checkout) may reference one of them.
        if vcs_head is not None:
            plan.require(
                "vcs_head",
                vcs_head,
                detail="history moved since the gc plan was made (a new commit may "
                "reference a pin it would release); re-run the plan",
            )
        if history_digest is not None:
            plan.require(
                "history_digest",
                history_digest,
                detail="history changed since the gc plan was made -- a commit in "
                "this or another workspace may reference a pin it would release; "
                "re-run the plan",
            )
        plan.require(
            "manifest_hash",
            plan.context["manifest_hash"],
            detail="manifests changed since the gc plan was made; re-run the plan",
        )

        # Unpin native refs not referenced by any manifest.
        checked_systems: set[str] = set()
        for m in history_manifests:
            backend = self.backend_for(m.kind)
            if Capability.PIN not in backend.capabilities:
                continue
            sys_key = key_for(backend, m.locator)
            if sys_key in checked_systems:
                continue
            checked_systems.add(sys_key)
            live = backend.list_pins(m.locator)
            # Only this dataset's namespace: pins of other datasets sharing
            # the store (or refs that are not tether pins) are never touched.
            mine = {p for p in live if pin_dataset(p) == self.config.dataset_id}
            foreign = len(live) - len(mine)
            if foreign:
                plan.notes.append(
                    f"{m.key}: {foreign} pin(s) of other datasets left alone"
                )
            keep = referenced.get(sys_key, set())
            for pid in sorted(mine - keep):
                plan.actions.append(
                    Action(
                        "unpin",
                        m.key,
                        m.kind,
                        target=ref_for_pin(pid),
                        detail="no manifest in history references it",
                        params={"locator": m.locator, "pin_id": pid},
                    )
                )

        # This workspace's working refs whose object was removed: forget the
        # ref; the native branch is only deleted by --prune-workspaces.
        for key, ref in sorted(self.workspace.working_refs.items()):
            if key in self.objects:
                continue
            plan.actions.append(
                Action(
                    "forget-working-ref",
                    key,
                    target=ref,
                    detail="object removed; branch left in place "
                    "(gc --prune-bookmarks evaluates it)",
                )
            )

        if prune_bookmarks:
            live = self.live_bookmarks() | set(keep_bookmarks or ())
            plan.context["live_bookmarks"] = sorted(live)
            plan.notes.append(f"keeping live bookmarks: {', '.join(sorted(live))}")
            self._plan_prune_bookmarks(
                plan,
                [*all_manifests, *self.objects.values()],
                key_for,
                live,
                force_prune,
            )

        # Listings no manifest (in history or the working tree) names.
        wanted: set[str] = set()
        for m in [*all_manifests, *self.objects.values()]:
            if m.state is not None:
                backend = self.backend_for(m.kind)
                wanted.add(
                    listing_name(
                        m.kind,
                        backend.identity(m.locator),
                        self._content_of(m.kind, m.state),
                    )
                )
        for path in sorted(listings_dir(self.root).glob("*.jsonl")):
            if path.name not in wanted:
                plan.actions.append(
                    Action("delete-listing", target=path.name, detail="unreferenced")
                )
        return plan

    def _plan_prune_bookmarks(
        self,
        plan: Plan,
        manifests: list[ObjectManifest],
        key_for: Callable[[ObjectBackend, dict], str],
        keep_bookmarks: set[str],
        force: bool,
    ) -> None:
        """Add `delete-branch` / `keep-branch` actions for stray `tether.ws.*` branches.

        A branch is stray when its bookmark is gone (not in the VCS and not
        worked on by any live checkout), when it is a legacy per-workspace
        branch from before bookmarks, or when it is this workspace's and no
        current object uses it. It is safe to delete when its head state is
        natively pinned by some manifest (a tag holds everything on it) or
        equals the base branch's head (nothing was written). Anything else --
        unpinned writes, a state that is only *recorded* (`pin = "record"`), or
        a backend whose branches are the storage itself -- is kept unless
        ``force``.
        """
        keep_slugs = {bookmark_slug(b) for b in keep_bookmarks}
        live_ws = {w[:8] for w in self.live_workspace_ids()}
        in_use = {
            ref
            for key, ref in self.workspace.working_refs.items()
            if key in self.objects
        }

        # States that hold data per system: pinned (safe) vs merely recorded.
        pinned: dict[str, list[State | None]] = {}
        recorded: dict[str, list[State | None]] = {}
        for m in manifests:
            if m.state is None:
                continue
            sys_key = key_for(self.backend_for(m.kind), m.locator)
            bucket = pinned if m.pin is not None else recorded
            content = self._content(m.kind, m.state)
            if content not in bucket.setdefault(sys_key, []):
                bucket[sys_key].append(content)

        systems_seen: set[str] = set()
        for key in sorted(self.objects):
            m = self.objects[key]
            backend = self.backend_for(m.kind)
            eff = effective_capabilities(backend, m.locator, m.policy)
            if Capability.FORK not in eff:
                continue
            sys_key = key_for(backend, m.locator)
            if sys_key in systems_seen:
                continue
            systems_seen.add(sys_key)
            storage = Capability.BRANCH_IS_STORAGE in eff
            base_head: State | None = None
            foreign = 0
            for ref in sorted(backend.list_working_refs(m.locator)):
                ds = working_ref_dataset(ref)
                if ds is None:
                    continue
                if ds != self.config.dataset_id:
                    foreign += 1  # another dataset's branch; not ours to judge
                    continue
                slug = working_ref_bookmark(ref)
                legacy_ws = working_ref_workspace(ref)
                if ref in in_use:
                    continue
                if slug is not None and slug in keep_slugs:
                    if slug == bookmark_slug(self.workspace.bookmark or ""):
                        origin = "this bookmark's branch; no object uses it"
                    else:
                        continue
                elif slug is not None:
                    origin = f"bookmark {slug} (gone)"
                elif legacy_ws in live_ws:
                    continue
                else:
                    origin = f"legacy workspace {legacy_ws} branch"

                # Why deleting would be safe -- or why it would not.
                reason: str | None = None
                safe = ""
                full_head: State | None = None
                blocker = backend.working_ref_blockers(m.locator, ref)
                if blocker is not None:
                    # Not a judgement call: the store will refuse. Plan it as
                    # kept, with the reason, even under --force-prune.
                    plan.actions.append(
                        Action(
                            "keep-branch",
                            key,
                            m.kind,
                            target=ref,
                            detail=f"{origin}; cannot be deleted: {blocker}",
                            params={"locator": m.locator, "blocked": True},
                        )
                    )
                    continue
                if storage:
                    reason = "branch is storage; deleting reclaims its data"
                else:
                    try:
                        full_head = backend.fingerprint(m.locator, ref)
                        head = self._content(m.kind, full_head)
                        if base_head is None:
                            base_head = self._content(
                                m.kind, backend.fingerprint(m.locator, None)
                            )
                    except TetherError as exc:
                        head, reason = None, f"cannot read head: {exc}"
                    if head is not None:
                        if head in pinned.get(sys_key, []):
                            safe = "head is pinned"
                        elif head == base_head:
                            safe = "head equals the base branch"
                        elif head in recorded.get(sys_key, []):
                            reason = (
                                f"holds a pin-less recorded state ({short_state(head)})"
                            )
                        else:
                            reason = f"has unpinned writes ({short_state(head)})"

                if reason is None:
                    plan.actions.append(
                        Action(
                            "delete-branch",
                            key,
                            m.kind,
                            target=ref,
                            detail=f"{origin}; {safe}",
                            params={"locator": m.locator, "head": full_head},
                        )
                    )
                elif force:
                    plan.actions.append(
                        Action(
                            "delete-branch",
                            key,
                            m.kind,
                            target=ref,
                            detail=f"{origin}; FORCED although it {reason}",
                            params={
                                "locator": m.locator,
                                "forced": True,
                                "head": full_head,
                            },
                        )
                    )
                else:
                    plan.actions.append(
                        Action(
                            "keep-branch",
                            key,
                            m.kind,
                            target=ref,
                            detail=f"{origin}; kept: {reason} (--force-prune deletes)",
                        )
                    )

            if foreign:
                plan.notes.append(
                    f"{key}: {foreign} working branch(es) of other datasets left alone"
                )

    def apply_gc(self, plan: Plan) -> GcReport:
        """Execute a plan from `plan_gc`.

        Unpins, deletes branches, forgets working refs, and deletes listings as
        planned (`keep-branch` actions are reported, not executed). Backend
        failures are aggregated into `MultiObjectError` after every action has
        been attempted.
        """
        with self._writer_lock(), self._repo_lock():
            self._verify_plan(plan, "gc")
            report = GcReport(dry_run=False, plan=plan)
            errors: dict[str, Exception] = {}
            forgot = False
            # Preflight every branch the plan deletes *before* the first action:
            # a stale head discovered half-way would leave earlier unpins done
            # and the plan half-applied. Stale means nothing happens.
            for a in plan.actions:
                if a.op == "delete-branch":
                    self._require_head(
                        self.backend_for(a.kind),
                        dict(a.params["locator"]),
                        a.target,
                        a.params.get("head"),
                        what=f"gc {a.key}",
                    )
            pre = {"workspace": self.workspace.to_toml()}
            op = self._begin_op("gc", plan=plan, pre=pre) if plan.writes else None
            for a in plan.actions:
                try:
                    if a.op == "unpin":
                        backend = self.backend_for(a.kind)
                        pid = str(a.params["pin_id"])
                        backend.unpin(
                            dict(a.params["locator"]), Pin(id=pid, ref=a.target)
                        )
                        report.unpinned.setdefault(a.kind, []).append(pid)
                        self._progress(op, "unpin", target=a.target)
                    elif a.op == "forget-working-ref":
                        self.workspace.working_refs.pop(a.key, None)
                        forgot = True
                        report.forgotten_working_refs.setdefault(a.key, []).append(
                            a.target
                        )
                    elif a.op == "delete-branch":
                        backend = self.backend_for(a.kind)
                        locator = dict(a.params["locator"])
                        # Checked in the preflight; check again at the moment
                        # of deletion (other actions took time). The preflight
                        # validated the plan as a whole, so a move found *now*
                        # is a race, not a stale plan: keep this branch, say
                        # so, and finish the rest rather than stop half-way.
                        try:
                            self._require_head(
                                backend,
                                locator,
                                a.target,
                                a.params.get("head"),
                                what=f"gc {a.key}",
                            )
                        except StalePlanError as exc:
                            report.kept_working_refs.setdefault(a.key, []).append(
                                a.target
                            )
                            errors[f"{a.op} {a.target}"] = exc
                            continue
                        backend.delete_working_ref(locator, a.target)
                        report.deleted_working_refs.setdefault(a.key, []).append(
                            a.target
                        )
                        self._progress(op, "delete-branch", key=a.key, target=a.target)
                    elif a.op == "keep-branch":
                        report.kept_working_refs.setdefault(a.key, []).append(a.target)
                    elif a.op == "delete-listing":
                        (listings_dir(self.root) / a.target).unlink(missing_ok=True)
                        report.deleted_listings.append(a.target)
                except StalePlanError as exc:
                    # Stale after the preflight: stop here, but say in the
                    # journal what was already done before stopping.
                    if forgot:
                        write_workspace(self.root, self.workspace)
                    if op is not None:
                        self._end_op(
                            op,
                            result={
                                **report_dict(report),
                                "failed": {k: str(v) for k, v in errors.items()},
                                "stopped": str(exc),
                            },
                        )
                    raise
                except Exception as exc:
                    errors[f"{a.op} {a.target}"] = exc
            if forgot:
                write_workspace(self.root, self.workspace)
            if op is not None:
                self._end_op(
                    op,
                    result={
                        **report_dict(report),
                        "failed": {k: str(v) for k, v in errors.items()},
                    },
                )
            if errors:
                raise MultiObjectError("gc failed for some actions", errors)
            return report

    def gc(
        self,
        *,
        dry_run: bool = True,
        prune_bookmarks: bool = False,
        keep_bookmarks: set[str] | None = None,
        force_prune: bool = False,
    ) -> GcReport:
        """Release native pins that no manifest in VCS history references.

        Equivalent to `plan_gc` followed by `apply_gc` unless `dry_run`. Also
        forgets this workspace's working refs for removed objects and deletes
        `.tether/listings/` files no manifest names. With `prune_bookmarks`,
        stray `tether.ws.*` branches (of bookmarks that are gone, legacy
        per-workspace ones, and this workspace's unused ones) are deleted when
        their head is pinned or equals the base head, and kept otherwise unless
        `force_prune`.

        Args:
            dry_run: Only report what would be released (the report carries the plan).
            prune_bookmarks: Also evaluate stray working branches.
            keep_bookmarks: Bookmark names to leave alone beyond those the VCS
                and live checkouts know (e.g. one that exists only elsewhere).
            force_prune: Delete stray branches even if they hold unpinned data
                (or belong to a `BRANCH_IS_STORAGE` backend).
        """
        # Plan and apply under one lock: planning sees the state the lock
        # refreshed, and nothing in this checkout moves in between.
        with self._writer_lock():
            plan = self.plan_gc(
                prune_bookmarks=prune_bookmarks,
                keep_bookmarks=keep_bookmarks,
                force_prune=force_prune,
            )
            if dry_run:
                report = GcReport(dry_run=True, plan=plan)
                for a in plan.actions:
                    if a.op == "unpin":
                        report.unpinned.setdefault(a.kind, []).append(
                            str(a.params["pin_id"])
                        )
                    elif a.op == "forget-working-ref":
                        report.forgotten_working_refs.setdefault(a.key, []).append(
                            a.target
                        )
                    elif a.op == "delete-branch":
                        report.deleted_working_refs.setdefault(a.key, []).append(
                            a.target
                        )
                    elif a.op == "keep-branch":
                        report.kept_working_refs.setdefault(a.key, []).append(a.target)
                    elif a.op == "delete-listing":
                        report.deleted_listings.append(a.target)
                return report
            return self.apply_gc(plan)

    # -- forget-workspace ------------------------------------------------------ #
    def _workspace_root(self, workspace_id8: str) -> Path | None:
        """Where the checkout with that tether workspace id lives, if it is live."""
        for root, ws in self._iter_live_workspaces():
            if ws.workspace_id[:8] == workspace_id8:
                return root.resolve()
        return None

    def plan_forget_workspace(self, workspace_id: str | None = None) -> Plan:
        """Compute what forgetting a workspace would do (default: this one).

        `jj workspace forget` / `git worktree remove` plus tether's half: the
        workspace's `workspace.toml` and `ops.jsonl` are removed when its
        checkout is known, and the VCS stops tracking the checkout. Store
        branches belong to bookmarks, not workspaces, so none are touched:
        delete the bookmark and run `gc --prune-bookmarks` for that.
        Forgetting the current workspace leaves the directory in place; the
        next tether command there starts a fresh workspace.

        Args:
            workspace_id: Full or 8-char id (see `tether ops` / `status`);
                default the current workspace.
        """
        target = (workspace_id or self.workspace.workspace_id)[:8]
        plan = Plan(
            command="forget-workspace",
            context={
                "workspace": target,
                "current": target == self.workspace.workspace_id[:8],
            },
        )
        root = self._workspace_root(target)
        if root is not None:
            plan.context["root"] = str(root)
            for name in (_m.WORKSPACE_FILENAME, _m.UNTRACKED_FILES[1]):
                path = _m.tether_path(root) / name
                if path.exists():
                    plan.actions.append(
                        Action(
                            "delete-file",
                            target=str(path),
                            detail="per-workspace state",
                        )
                    )
            detail = (
                "jj workspace forget / git worktree remove (git's main worktree stays)"
            )
            if plan.context["current"]:
                detail += (
                    "; this checkout then has no working copy until `jj undo` "
                    "or a new `jj workspace add`"
                )
            plan.actions.append(
                Action("forget-vcs-workspace", target=str(root), detail=detail)
            )
        else:
            plan.notes.append(f"workspace {target}: no live checkout found")
        plan.notes.append(
            "store branches belong to bookmarks, not workspaces: none are touched "
            "(delete the bookmark and `gc --prune-bookmarks`)"
        )
        return plan

    def apply_forget_workspace(self, plan: Plan) -> ForgetWorkspaceReport:
        """Execute a plan from `plan_forget_workspace`; failures are reported."""
        with self._writer_lock():
            self._verify_plan(plan, "forget-workspace")
            report = ForgetWorkspaceReport(
                workspace=str(plan.context["workspace"]), plan=plan
            )
            # Log first: forgetting the current workspace removes its own log.
            self._log_op(
                "forget-workspace",
                plan=plan,
                result={"workspace": report.workspace},
                pre={"workspace": self.workspace.to_toml()},
            )
            for a in plan.actions:
                try:
                    if a.op == "delete-file":
                        Path(a.target).unlink(missing_ok=True)
                        report.removed_files.append(a.target)
                    elif a.op == "forget-vcs-workspace":
                        report.vcs = self.vcs.forget_workspace(Path(a.target))
                except (TetherError, OSError) as exc:
                    report.failed[f"{a.op} {a.target}"] = str(exc)
            if plan.context.get("current"):
                # This workspace no longer exists as such; drop its in-memory state.
                self.workspace = read_workspace(self.root)
            return report

    def forget_workspace(
        self, workspace_id: str | None = None
    ) -> ForgetWorkspaceReport:
        """Forget a workspace (see `plan_forget_workspace`)."""
        with self._writer_lock():
            return self.apply_forget_workspace(self.plan_forget_workspace(workspace_id))

    # -- abandon --------------------------------------------------------------- #
    def abandon(self, revs: Sequence[str], *, gc: bool = False) -> AbandonReport:
        """Drop dataset commits from VCS history and show (or release) what that frees.

        The VCS half is `jj abandon` / a git rebase that removes the commits;
        descendants keep their manifests exactly as they were (a manifest is a
        whole-state record, so removing an earlier commit must not change a
        later one). The store half is the `gc` plan afterwards: pins that only
        the dropped commits referenced are now unreferenced. With `gc`, that
        plan is applied in the same call.

        Logged as `abandon`; not undoable by tether (the VCS's own undo or
        reflog brings the commits back, and `repair` the pins).

        Args:
            revs: Revisions to drop (any VCS revset / revision syntax).
            gc: Also release the newly unreferenced pins.

        Raises:
            VcsError: The revision cannot be abandoned (git: not on the current
                branch, dirty tree, or a conflict outside the dataset).
        """
        with self._writer_lock(), self._repo_lock():
            pre = {"vcs": self.vcs.position(), "workspace": self.workspace.to_toml()}
            # A bookmark on a commit being abandoned must survive: jj deletes it,
            # git leaves it on the dropped commit. Move it to the nearest kept
            # ancestor, the way `jj abandon` treats the working copy.
            before = self.vcs.bookmarks()
            targets = {self.vcs.resolve(r) for r in revs}
            parents = {
                c.commit_id: c.parents for c in self.vcs.commit_info(sorted(targets))
            }
            moved: dict[str, str] = {}
            ids, rewritten = self.vcs.abandon(list(revs), self._objects_reldir())
            for name, commit in before.items():
                if commit not in targets:
                    continue
                dest = commit
                seen: set[str] = set()
                while dest in targets and dest not in seen:
                    seen.add(dest)
                    dest = (parents.get(dest) or [""])[0]
                dest = rewritten.get(dest, dest)
                if dest:
                    self.vcs.bookmark_set(name, dest)
                    moved[name] = dest
            self._manifest_cache.clear()
            self.objects = read_objects(self.root)
            gc_plan = self.plan_gc()
            report = AbandonReport(abandoned=ids, gc_plan=gc_plan)
            self._log_op(
                "abandon",
                result={
                    "abandoned": ids,
                    "rewritten_commits": rewritten,
                    "bookmarks_moved": moved,
                    "unreferenced": [
                        a.target for a in gc_plan.actions if a.op == "unpin"
                    ],
                },
                pre=pre,
            )
            if gc and not gc_plan.is_empty:
                report.gc_report = self.apply_gc(gc_plan)
            return report
