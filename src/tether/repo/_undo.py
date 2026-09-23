"""`undo` and `repair`: reversing an operation, and rebuilding what the
manifests promise."""

from __future__ import annotations

try:  # POSIX advisory locks; Windows has no fcntl and gets no writer lock
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from tether import manifest as _m
from tether.backends.base import (
    Capability,
    VerifyStatus,
    effective_capabilities,
)
from tether.errors import (
    TetherError,
)
from tether.manifest import (
    ObjectManifest,
    State,
    listings_dir,
    read_objects,
    read_workspace,
    remove_object,
    write_workspace,
)
from tether.oplog import (
    OpEntry,
    report_dict,
)
from tether.plan import Action, Plan

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


from tether.repo._core import RepoCore
from tether.repo._reports import (
    RepairReport,
    UndoReport,
    short_state,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tether.repo import Repo


class UndoOps(RepoCore):
    """`undo` and `repair`: reversing an operation, and rebuilding what the
    manifests promise."""

    # -- undo ------------------------------------------------------------------ #
    def undo(
        self: Repo, op_id: str | None = None, *, discard: bool = False
    ) -> UndoReport:
        """Reverse an operation from the op log, where the stores still allow it.

        Defaults to the newest entry that can be undone (not an `undo` or
        `repair`, not already undone). What "reverse" means per command:

        - `commit`: uncommit. The dataset commit becomes working-tree changes
          again (jj `squash --into @`, git `reset --soft`) if it is still the
          working copy's parent; the pins it made stay, still referenced by
          the working-tree manifests. Committed with `vcs=False`: the
          manifests are restored from before.
        - `new` / `fork` / `restore`: branches the op *created* are deleted;
          `workspace.toml` is restored; the VCS working copy returns to where
          it was if it has not moved since. A branch the op *reset* is not
          re-pointed -- the report names its old head and `restore` /
          `new --discard` put it where you want. A created branch that gained
          writes since the op is refused unless `discard`.
        - `gc`: forgotten working refs and deleted listings come back (the
          latter from the VCS); the workspace is restored. Deleted branches
          and pins are *irreversible* -- `repair` recreates a bookmark's
          branches from its manifests, and pins once a manifest references
          them again.
        - `import` / `add` / `remove`: manifests and workspace restored.
        - `promote`: refused; the base heads before the move are printed
          for a manual reset (backends only fast-forward).

        The undo is logged and the target marked `undone_by`. If nothing
        could be reversed the call raises instead.

        Args:
            op_id: Which entry; default the newest undoable one.
            discard: Delete or reset branches even if they gained writes.

        Raises:
            TetherError: Nothing to undo, an unknown id, a branch with writes
                (without `discard`), or an operation that cannot be reversed.
        """
        with self._writer_lock(), self._repo_lock():
            entries = self.ops()
            if op_id is None:
                target = next((e for e in entries if e.undoable), None)
                if target is None:
                    raise TetherError("nothing to undo")
            else:
                target = next((e for e in entries if e.id == op_id), None)
                if target is None:
                    raise TetherError(f"no operation {op_id!r} in this workspace's log")
                if target.undone_by:
                    raise TetherError(
                        f"{op_id} was already undone by {target.undone_by}"
                    )
                if target.incomplete:
                    raise TetherError(
                        f"{op_id} ({target.command}) never finished, so what it did "
                        "is not known; `tether verify` and `tether gc --dry-run` show "
                        "what it left behind"
                    )
                if target.undoes is not None or not target.undoable:
                    raise TetherError(f"cannot undo {target.command!r}")

            report = UndoReport(op=target)
            handler = {
                "commit": self._undo_commit,
                "new": self._undo_new,
                "restore": self._undo_new,
                "fork": self._undo_fork,
                "gc": self._undo_gc,
                "promote": self._undo_promote,
                "import": self._undo_manifests,
                "add": self._undo_add,
                "remove": self._undo_manifests,
                "pull": self._undo_commit,
                "set": self._undo_manifests,
            }.get(target.command)
            if handler is None:
                raise TetherError(f"cannot undo {target.command!r}")
            # Journal the undo before it touches anything: an undo that dies
            # half-way (a branch re-pointed, a manifest not yet restored) must
            # be visible like any other interrupted operation.
            entry = self._begin_op(
                "undo",
                pre={"workspace": self.workspace.to_toml(), "target": target.id},
                undoes=target.id,
            )

            def summary() -> dict[str, Any]:
                return {
                    "summary": f"{target.command} {target.summary()}",
                    "restored": list(report.restored),
                    "irreversible": list(report.irreversible),
                    "skipped": list(report.skipped),
                }

            try:
                handler(target, report, discard)
            except TetherError as exc:
                if not report.restored:
                    # Refused before touching anything (writes to discard, a
                    # moved working copy): a failed attempt, not an interrupted
                    # one. Something restored, then an error: leave it started.
                    self._end_op(entry, result={**summary(), "failed": str(exc)})
                raise
            if not report.restored and report.irreversible:
                self._end_op(entry, result={**summary(), "failed": "nothing restored"})
                raise TetherError(
                    f"cannot undo {target.id} ({target.command}):\n"
                    + "\n".join(f"  {line}" for line in report.irreversible)
                )
            # Completing the undo and marking its target undone is one record.
            self._end_op(entry, result=summary(), undone=target.id)
            report.undo_id = entry.id
            return report

    def _reload(self) -> None:
        self.objects = read_objects(self.root)
        self.workspace = read_workspace(self.root)

    def _restore_workspace(self, entry: OpEntry, report: UndoReport) -> None:
        text = entry.pre.get("workspace")
        if text is None:
            return
        _m.workspace_path(self.root).write_text(str(text), encoding="utf-8")
        self.workspace = read_workspace(self.root)
        report.restored.append("workspace.toml restored")

    def _restore_manifests(
        self, texts: Mapping[str, Any], report: UndoReport | None = None
    ) -> None:
        for key, text in sorted(texts.items()):
            if text is None:
                remove_object(self.root, key)
                if report is not None:
                    report.restored.append(f"{key}: manifest removed again")
            else:
                _m.object_path(self.root, key).parent.mkdir(parents=True, exist_ok=True)
                _m.object_path(self.root, key).write_text(str(text), encoding="utf-8")
                if report is not None:
                    report.restored.append(f"{key}: manifest restored")
        self.objects = read_objects(self.root)

    def _branch_has_new_writes(self, key: str, ref: str) -> State | None:
        """The head of `ref` if it moved past what this workspace knows; else None."""
        m = self.objects.get(key)
        if m is None:
            return None
        backend = self.backend_for(m.kind)
        try:
            head = backend.fingerprint(m.locator, ref)
        except TetherError:
            return None  # branch gone; nothing to lose
        known = [
            self.workspace.base_states.get(key),
            self.workspace.fork_points.get(key),
            m.state,
        ]
        if any(self._same(m.kind, head, k) for k in known if k is not None):
            return None
        return head

    def _undo_branches(
        self,
        entry: OpEntry,
        report: UndoReport,
        discard: bool,
        *,
        created: Mapping[str, str],
        reset: Mapping[str, str],
    ) -> None:
        """Delete the branches an op *created*. Branches it reset are reported
        with the head they had: putting a branch back at an old state is a
        `restore`/`new --discard` you choose, not something undo guesses at
        (the branch may have children, pins, or a store that cannot re-point).
        """
        # Refuse before touching anything if a created branch gained writes.
        if not discard:
            dirty = []
            for key, ref in created.items():
                head = self._branch_has_new_writes(key, ref)
                if head is not None:
                    dirty.append(f"{key}: {ref} has writes since ({short_state(head)})")
            if dirty:
                raise TetherError(
                    "refusing to undo: branches gained writes; commit them or pass "
                    "--discard\n" + "\n".join(f"  {d}" for d in dirty)
                )
        heads = entry.pre.get("heads") or {}
        for key, ref in sorted(created.items()):
            m = self.objects.get(key)
            if m is None:
                report.skipped.append(f"{key}: object no longer registered; {ref} left")
                continue
            try:
                self.backend_for(m.kind).delete_working_ref(m.locator, ref)
                report.restored.append(f"{key}: deleted {ref}")
            except TetherError as exc:
                report.irreversible.append(f"{key}: could not delete {ref}: {exc}")
        for key, ref in sorted(reset.items()):
            head = heads.get(key)
            was = f" (was {short_state(head)})" if head is not None else ""
            report.irreversible.append(
                f"{key}: {ref} was reset{was}; `tether restore {key} --from REV` or "
                f"`tether new --discard` puts it where you want"
            )

    def _undo_commit(self, entry: OpEntry, report: UndoReport, discard: bool) -> None:
        commit = entry.result.get("vcs_commit")
        if commit:
            if not self.vcs.uncommit(str(commit)):
                report.irreversible.append(
                    f"commit {str(commit)[:12]} is no longer the working copy's "
                    "parent; uncommit it with jj/git first"
                )
                return
            report.restored.append(
                f"uncommitted {str(commit)[:12]}; its manifests are working-tree "
                "changes again (pins kept)"
            )
            self.objects = read_objects(self.root)
            return
        # Committed with vcs=False: only the manifests were written.
        self._restore_manifests(entry.pre.get("objects") or {}, report)
        self._restore_workspace(entry, report)

    def _undo_new(self, entry: OpEntry, report: UndoReport, discard: bool) -> None:
        r = entry.result
        refs = r.get("working_refs") or {}
        self._undo_branches(
            entry,
            report,
            discard,
            created={k: refs[k] for k in r.get("created") or [] if k in refs},
            reset={k: refs[k] for k in r.get("reset") or [] if k in refs},
        )
        self._restore_workspace(entry, report)
        before, after = entry.pre.get("vcs"), r.get("vcs")
        plan_ctx = (entry.plan or {}).get("context") or {}
        made = r.get("created_bookmark")
        if (plan_ctx.get("rev") or made) and before and after:
            if self.vcs.position().get("id") == after.get("id"):
                self.vcs.goto(before)
                self.objects = read_objects(self.root)
                report.restored.append("VCS working copy back where it was")
            else:
                report.skipped.append(
                    "VCS working copy has moved since; not returning it"
                )
        if made and after:
            marks = self.vcs.bookmarks()
            if made in marks and marks[made] == (
                after.get("parent") or after.get("commit")
            ):
                self.vcs.bookmark_delete(str(made))
                report.restored.append(f"bookmark {made} deleted")
            elif made in marks:
                report.skipped.append(f"bookmark {made} has moved since; kept")

    def _undo_fork(self, entry: OpEntry, report: UndoReport, discard: bool) -> None:
        key, ref = str(entry.result.get("key")), str(entry.result.get("ref"))
        existed = key in (entry.pre.get("heads") or {})
        self._undo_branches(
            entry,
            report,
            discard,
            created={} if existed else {key: ref},
            reset={key: ref} if existed else {},
        )
        self._restore_workspace(entry, report)

    def _undo_gc(self, entry: OpEntry, report: UndoReport, discard: bool) -> None:
        r = entry.result
        deleted = sorted(
            ref
            for refs in (r.get("deleted_working_refs") or {}).values()
            for ref in refs
        )
        if deleted:
            # A branch gc judged dead is not recreated from a recorded head:
            # `repair` recreates missing branches from what the manifests say,
            # which is the state that matters.
            report.irreversible.append(
                f"{len(deleted)} branch(es) deleted ({', '.join(deleted[:3])}"
                f"{', ...' if len(deleted) > 3 else ''}); `tether repair` recreates "
                "a bookmark's branches from its manifests"
            )
        n_pins = sum(len(v) for v in (r.get("unpinned") or {}).values())
        if n_pins:
            report.irreversible.append(
                f"{n_pins} pin(s) deleted; if a manifest references them again, "
                "`tether repair` recreates them while the state is still reachable"
            )
        for key, where in sorted((r.get("deleted_stores") or {}).items()):
            report.irreversible.append(
                f"{key}: the created store at {where} was deleted; nothing "
                "referenced it and it held only tether's own refs"
            )
        for name in r.get("deleted_listings") or []:
            rel = self._listing_relpath(name)
            text = self.vcs.read_file_at("@-" if self.vcs.kind == "jj" else "HEAD", rel)
            if text is None:
                report.irreversible.append(f"listing {name}: not in the VCS either")
                continue
            (listings_dir(self.root) / name).parent.mkdir(parents=True, exist_ok=True)
            (listings_dir(self.root) / name).write_text(text, encoding="utf-8")
            report.restored.append(f"listing {name}: restored from the VCS")
        self._restore_workspace(entry, report)

    def _undo_promote(self, entry: OpEntry, report: UndoReport, discard: bool) -> None:
        before = entry.pre.get("base_states") or {}
        moved = {
            **(entry.result.get("fast_forwarded") or {}),
            **(entry.result.get("merged") or {}),
        }
        for key in sorted(moved):
            report.irreversible.append(
                f"{key}: base branch moved {short_state(before.get(key))} -> "
                f"{short_state(moved[key])}; tether only fast-forwards base "
                "branches, reset it in the store yourself"
            )
        if not moved:
            report.skipped.append("promote moved nothing")

    def _undo_manifests(
        self, entry: OpEntry, report: UndoReport, discard: bool
    ) -> None:
        self._restore_manifests(entry.pre.get("objects") or {}, report)
        self._restore_workspace(entry, report)

    def _undo_add(
        self: Repo, entry: OpEntry, report: UndoReport, discard: bool
    ) -> None:
        self._undo_manifests(entry, report, discard)
        if entry.result.get("created"):
            # Experimental: `add --create` made a store; take it away again
            # while it is still empty (see `tether.experimental.lifecycle`).
            from tether.experimental.lifecycle import undo_add_store

            undo_add_store(self, entry, report)

    # -- repair ---------------------------------------------------------------- #
    def plan_repair(self: Repo, *, all_history: bool = False) -> Plan:
        """Compute what `repair` would rebuild without writing anywhere.

        A manifest is a promise: "this pin exists and names this state". When
        the pin is gone -- a `gc` that was undone, another dataset's cleanup
        before namespaces, a ref deleted by hand -- the promise can be kept
        again as long as the state is still reachable in the store: `repin`
        recreates the native ref from the recorded state. Likewise `refork`
        recreates a working branch this workspace expects but the store no
        longer has. Pins that exist but point elsewhere (`DRIFTED`) are left
        alone and noted; that is a different problem.

        Args:
            all_history: Also check the pins of every manifest in VCS history,
                not just the working tree's.
        """
        history_digest = self.vcs.history_digest()
        plan = Plan(
            command="repair",
            context={
                "all_history": all_history,
                "workspace_id": self.workspace.workspace_id,
                "history_digest": history_digest,
            },
        )
        plan.require(
            "workspace_id",
            self.workspace.workspace_id,
            detail="this repair plan was made in another checkout; "
            "re-run the plan here",
        )
        # What the manifests promise -- in the working tree and, with
        # `all_history`, in every commit -- is what the plan recreates; a
        # commit added or removed since may promise something else.
        plan.require(
            "history_digest",
            history_digest,
            detail="history changed since the repair plan was made; re-run the plan",
        )
        for e in self.incomplete_ops():
            did = ", ".join(
                f"{r.get('action')} {r.get('key') or r.get('target') or ''}".strip()
                for r in e.progress
            )
            planned = len((e.plan or {}).get("actions") or [])
            plan.notes.append(
                f"operation {e.id} ({e.command}, {e.at}) never finished"
                + (
                    f"; done before it stopped: {did}"
                    if did
                    else "; no action had completed"
                )
                + (f" (of {planned} planned)" if planned else "")
                + "; re-running the command finishes what is left (creating an "
                "absent ref from a pinned state is idempotent; a reset needs the "
                "head reviewed again), pins it created and no commit names are "
                "released by `gc`, branches it made are judged by "
                "`gc --prune-bookmarks`"
            )
        targets: dict[str, ObjectManifest] = {}
        for key, m in self.objects.items():
            if m.pin is not None and m.state is not None:
                targets[key] = m
        if all_history:
            for rev, objects in self._iter_history_objects():
                for key, m in objects.items():
                    if m.pin is not None and m.state is not None:
                        targets.setdefault(f"{key}@{rev[:12]}", m)
        seen: set[str] = set()
        for label, report in self._verify_manifests(targets, deep=False).items():
            m = targets[label]
            assert m.pin is not None
            sig = f"{m.kind}:{m.pin.id}"
            if sig in seen:
                continue
            seen.add(sig)
            if report.status is VerifyStatus.MISSING:
                plan.actions.append(
                    Action(
                        "repin",
                        label,
                        m.kind,
                        target=m.pin.ref,
                        detail=f"pin missing ({report.message}); recreate at "
                        f"{short_state(m.state)}",
                        params={
                            "locator": m.locator,
                            "state": m.state,
                            "pin_id": m.pin.id,
                        },
                    )
                )
            elif report.status is VerifyStatus.DRIFTED:
                plan.notes.append(
                    f"{label}: pin drifted, not touched ({report.message})"
                )

        for key, ref in sorted(self.workspace.working_refs.items()):
            m = self.objects.get(key)
            if m is None or self.on_trunk() or m.state is None:
                continue
            backend = self.backend_for(m.kind)
            if Capability.FORK not in effective_capabilities(
                backend, m.locator, m.policy
            ):
                continue
            if ref in backend.list_working_refs(m.locator):
                continue
            plan.actions.append(
                Action(
                    "refork",
                    key,
                    m.kind,
                    target=ref,
                    detail="working branch missing; recreate from the manifest "
                    f"({'pin ' + m.pin.ref if m.pin else short_state(m.state)})",
                    params={"locator": m.locator},
                )
            )
            # Planned because the branch was missing; if it is back (another
            # checkout re-created it), do not reset it.
            plan.require(
                "ref_absent",
                key=key,
                backend=m.kind,
                locator=dict(m.locator),
                ref=ref,
                detail=f"{ref} exists again since the plan was made; re-run the plan",
            )
        if plan.is_empty:
            plan.notes.append("nothing to repair")
        return plan

    def apply_repair(self: Repo, plan: Plan) -> RepairReport:
        """Execute a plan from `plan_repair`; failures are reported, not raised."""
        with self._writer_lock():
            self._verify_plan(plan, "repair")
            report = RepairReport(plan=plan)
            pre = {"workspace": self.workspace.to_toml()}
            op = self._begin_op("repair", plan=plan, pre=pre) if plan.writes else None
            for a in plan.actions:
                try:
                    if a.op == "repin":
                        backend = self.backend_for(a.kind)
                        pin = backend.pin(
                            dict(a.params["locator"]),
                            dict(a.params["state"]),
                            str(a.params["pin_id"]),
                        )
                        self._note_touched(a.key, a.kind, dict(a.params["locator"]))
                        if pin.created:
                            self._note_pinned(a.key, a.kind, pin.id)
                        report.repinned[a.key] = str(a.params["pin_id"])
                        self._progress(op, "repin", key=a.key, target=a.target)
                    elif a.op == "refork":
                        m = self.objects[a.key]
                        ref = self._fork_from_manifest(m, a.target)
                        self._progress(op, "refork", key=a.key, ref=ref)
                        self.workspace.working_refs[a.key] = ref
                        if m.state is not None:
                            self.workspace.fork_points[a.key] = dict(m.state)
                        self._mark_base_states([a.key])
                        report.reforked[a.key] = ref
                except TetherError as exc:
                    report.failed[f"{a.op} {a.target}"] = str(exc)
            if report.reforked:
                write_workspace(self.root, self.workspace)
            if op is not None:
                self._end_op(op, result=report_dict(report))
            return report

    def repair(self: Repo, *, all_history: bool = False) -> RepairReport:
        """Recreate missing pins and working branches from the manifests.

        Equivalent to `apply_repair(plan_repair(...))`.
        """
        return self.apply_repair(self.plan_repair(all_history=all_history))
