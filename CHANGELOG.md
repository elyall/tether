# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Operation log.** Every command that writes to a store appends an entry to
  `.tether/ops.jsonl` (per workspace, untracked, append-only -- jj's own
  `op log` is the model): the plan it applied, what it did, and what it
  replaced (workspace state and VCS position before a `new`, the head of
  every working branch a fork resets, the manifests a `commit` / `import` /
  `add` / `remove` rewrote, the base heads `promote` moved from, the heads of
  branches `gc` deleted). `tether ops` and `Repo.ops()` read it;
  `tether.oplog` is the module. `Repo` re-asserts the `.tether/.gitignore`
  rules on open so jj never snapshots the log.
- **`tether undo [ID]`** (`Repo.undo`) reverses an op-log entry where the
  store still allows it, and says what it could not: a `commit` is
  uncommitted (jj `squash --into @`, git `reset --soft`; the manifests become
  working-tree changes and the pins stay); a `new` or lazy fork has the
  branches it created deleted, the ones it reset re-pointed to their recorded
  heads (backends that fork from a state), `workspace.toml` restored, and
  the VCS working copy returned if it has not moved; a `gc` gets its deleted
  branches and listings back while its released pins are reported as
  irreversible; `import` / `add` / `remove` restore the manifests;
  `promote` is refused with the previous base heads printed. A branch that
  gained writes since the operation is refused without `--discard`. The undo
  is logged and its target marked `undone_by`; the CLI exits 2 on a partial
  undo. `VcsAdapter` gained `position()`, `goto()`, `uncommit()`, `dirty()`.
- **`tether repair`** (`plan_repair` / `apply_repair` / `repair`) recreates
  pins whose native ref is missing from the manifest's recorded state (the
  working tree's manifests, or every commit's with `--all-history`) and
  working branches this workspace expects but the store lost. Drifted pins
  are noted, never overwritten; states the store no longer has are reported
  (exit 2), not raised.
- **`tether upgrade`** (`plan_upgrade` / `apply_upgrade` / `upgrade`;
  `tether.migrations`). `tether.toml` now carries `[tether] version = 2`; a
  dataset at an older version is refused by every command except `upgrade`,
  which runs the pending migrations in order, writing the version after each
  so an interrupted upgrade resumes. The v2 migration gives the dataset an
  id, renames every pin (`tether.<hash>` -> `tether.<id>.<hash16>`) and
  working branch (`tether.ws.<ws>.<slug>` -> `tether.ws.<id>.<ws>.<slug>-<key6>`)
  in every store, and rewrites every historical manifest to the new names so
  `gc` and `verify --all-history` keep agreeing with the stores -- history
  rewriting changes commit ids (jj keeps change ids; `--ignore-immutable` for
  pushed commits); every other clone must re-sync. `--dry-run` shows the
  renames and the number of commits; failed renames exit 2 and `repair`
  finishes the job. `VcsAdapter.rewrite_history()` (jj: `new` + `squash` per
  change; git: plumbing `commit-tree`, refs updated) and
  `ObjectBackend.rename_pin` / `rename_working_ref` (defaults built on
  `pin`/`unpin` and `fork`/`delete_working_ref`; Neon renames branches in
  place because pins are branches with children) are new.
- **`tether restore KEY... --from REV`** (`plan_restore` / `apply_restore` /
  `restore`): the per-object `jj restore --from` -- reset one object's working
  branch to what `REV` pinned, leaving the other branches, the manifests, and
  the VCS working copy alone. Not stale afterwards (the next `commit` pins
  the restore); the fork point moves to `REV`'s state so `promote` sees a
  moved base as a divergence. Refused for a branch with unpinned writes
  unless `--discard`; undoable.
- **`tether undo --to OP_ID`** (`Repo.undo_to`): undo every operation newer
  than `OP_ID`, newest first -- the per-delta counterpart of `jj op restore`.
  Stops, keeping what it reversed, at the first operation that cannot be
  undone or refuses; partial undos are recorded and the walk continues.
- **`tether abandon REV... [--gc]`** (`Repo.abandon`): drop dataset commits
  from VCS history and show -- or with `--gc` apply -- the `gc` plan for the
  pins only they referenced. Descendants keep their manifests exactly as they
  were (a manifest is a whole-state record; the VCS's own rebase would
  conflict on it). `VcsAdapter.abandon(revs, keep_dir)` is new: jj abandons
  then rewrites the descendants' manifests back; git rebases with conflicts
  under the dataset resolved to the original content, then a fix-up pass.
  Logged; not undoable by tether.
- **`tether new --discard`.** `new` now fingerprints every working branch it
  would reset and refuses -- before touching the VCS or any store -- when a
  head holds writes beyond what this workspace last committed or forked at.
  `--discard` opts in; the plan's fork action says what it throws away.

### Changed

- **Native refs are namespaced by dataset.** `tether.toml` carries an 8-hex
  `[dataset] id` (generated by `init`, committed, shared by every clone). Pin
  ids are `<id>.<hash16>` (refs `tether.<id>.<hash16>`) and working branches
  `tether.ws.<id>.<workspace8>.<slug>-<key6>`. `gc`'s pin sweep and branch
  prune stay inside the namespace, `--force-prune` included; refs of other
  datasets sharing the store are counted in the plan's notes and never
  touched. Before this, `gc` from one dataset deleted every pin another
  dataset had made in the same store. The same content pinned by two datasets
  is now two refs. `compute_pin_id` and `working_ref_name` take the dataset
  id; `pin_dataset()` and `working_ref_dataset()` parse it back. Breaking for
  earlier alphas: run `tether upgrade` (below), which renames the refs and
  rewrites history to match.
- `apply_commit` commits to the VCS when the manifests are dirty in the
  working tree even if no object's state changed (an undone commit, an
  `add`, an `import`); before, that `commit` was a silent no-op.
- The `memory` backend refuses to pin a snapshot that no longer exists, as
  real stores do.
- Iceberg's `metadata_location` is a `VOLATILE_KEY`: it was part of the state
  until 0.1.0a7 and still appears in old manifests, so it must not affect
  content identity (the upgrade hashes old states through `content_state`).

## [0.1.0a7] - 2026-09-08

### Fixed

- States now separate *content* from *address*. Backends declare
  `VOLATILE_KEYS` and `tether.backends.base.content_state()` strips them for
  drift detection, the unchanged check in `commit`, pin ids, listing names,
  export hashes, and `promote`'s comparisons; `open` / `pin` / `verify` still
  get the full state. Neon's `lsn` (moves on checkpoints) and git's
  `change_id` (present only with jj) are volatile; Iceberg's
  `metadata_location` (rewritten by every table commit on any branch) is no
  longer part of the state at all. Before this, unrelated Iceberg commits and
  Neon checkpoints showed as drift and created duplicate pins, and the same
  git sha pinned differently with and without jj installed.
- git `dirty` is computed only for the checked-out ref; a dirty worktree no
  longer marks every other branch of the repository as modified.
- `fork()` onto an existing branch name now resets it to the source on every
  backend (Iceberg via a `set-snapshot-ref` update, Neon via branch restore);
  the conformance suite checks it. Neon `pin()` refuses an existing pin branch
  that hangs off a different parent or LSN instead of reusing it.
- Stale-workspace detection is per object. Each working ref records the
  committed state it was forked from or last committed at
  (`WorkspaceState.base_states`); an object is stale exactly when its manifest
  says something else. Registering or removing other objects no longer
  silently un-stales a workspace, `track` objects are never stale, and
  `status` / `StaleWorkingCopyError` name the objects. `workspace.base` is
  gone; export schema_version 3 (`workspace.base_state_json`).
- Working-ref names end in a 6-hex digest of the key
  (`tether.ws.<ws8>.<slug>-<key6>`), so keys that slugify alike no longer
  share a branch. Pin ids are 16 hex chars (were 12). Both change native ref
  names; existing pins and working branches from earlier alphas are not
  recognised -- re-commit and `new`.
- `apply_new` (`tether new --from-plan`) checks the plan against the target
  before moving the VCS working copy.
- Neon `fork()` onto an existing working branch always restores it onto the
  source. It used to return early when the branch's `parent_id` already
  matched -- but `parent_id` is where a branch was created, not where its
  head is, so `new` back onto the same pin never discarded uncommitted
  writes. When pins hang off the working branch (they are its children and
  Neon will not restore a branch with children in place) the fork lands on a
  sibling name instead; the engine records the name `fork` returns. Neon
  time-travel reads (`open` at a recorded state) now ensure a read-only
  endpoint like pin reads do.
- A `new` in which some forks fail now records the branches that were created
  (working refs, fork points, base states) before raising, and the error says
  which objects failed and that a second `new` completes the job. Previously
  the successful branches existed in their systems but not in
  `workspace.toml`.
- `tether new REV` in git no longer leaves a detached HEAD: a branch name is
  switched to, any other revision is checked out onto a `tether/<rev12>` branch
  so the dataset commits that follow stay reachable by `gc`; `new` with no
  revision is a documented no-op in git (jj creates a fresh empty change).
- `verify --all-history` now checks recorded (pin-less) states as well as
  pins; Observed records are still skipped.
- The branching guide registered the dataset's own repository as a `git`
  object, which re-pins on every dataset commit; it now uses a separate repo.

### Changed

- `gc --prune-workspaces` finds every live checkout of the repository (jj
  workspaces via `jj workspace root --name`, git worktrees) and keeps their
  working branches automatically; `--keep-workspace` is now only for ids that
  are live elsewhere. `Repo.live_workspace_ids()` and
  `VcsAdapter.workspace_roots()` are new.
- `export`, `publish`, `import` and the lakeFS, Dolt, and DuckLake backends are
  labelled experimental in the CLI help (`add --kind`), the README (its own
  section; the compatibility matrix), and the guide.
- `StatusReport.stale_keys` and `Repo.stale_keys()` list the stale objects.
- `tether import` updates that change an object's locator now drop the
  workspace's working branch, pending fork, base state, and fork point for it;
  writes no longer go to the branch in the old system until the next `new`
  (the branch itself is left for `gc`).
- `promote`'s merge path records the working ref that `fork()` returns when
  it resets the fork onto the merge result (Neon may return a sibling name).
- The conformance suite computes pin ids from the content state, as the
  engine does; before, a backend with volatile keys would have minted
  different ids under the suite.

## [0.1.0a6] - 2026-09-07

### Added

- `tether promote [KEY]... [--rev REV] [--strategy auto|ff|merge] [-m MSG]`
  (`Repo.plan_promote` / `apply_promote` / `promote`, `PromoteReport`): move
  each system's base branch to what this workspace's fork holds. The base is
  compared to the **fork point** recorded when the branch was created
  (`WorkspaceState.fork_points`, also in the export `workspace` table as
  `fork_point_json`; export schema_version 2): unchanged -> fast-forward
  (`Capability.PROMOTE`: icechunk `reset_branch`, iceberg `set-snapshot-ref`,
  git `merge --ff-only`, lakeFS / Dolt merges), moved -> native three-way merge
  (`Capability.MERGE`: git, lakeFS `merge_into`, `DOLT_MERGE`; conflicts are
  reported as `MergeConflict` and nothing is written), otherwise `refuse` with
  the backend's `PROMOTE_HINT` (Icechunk/Iceberg have no merge; Lance cannot
  move a branch head; Neon cannot promote a child branch). After a merge the
  working branch is reset onto the merge result so the next `commit` pins it.
  `--dry-run` / `--plan` / `--from-plan` as for the other planned commands;
  exit status 1 when anything was refused. New protocol members `promote`,
  `merge`, `ancestor_of`, `PROMOTE_HINT`.

### Changed

- Working branches are forked lazily by default. `tether new` decides each
  Forkable object's `tether.ws.<workspace>.<key>` branch (plan action
  `defer-fork`) and the first writable `open` creates it from the pin;
  workspaces that never write to an object leave no branch behind. `new
  --eager` / `[new] fork = "eager"` restores creating every branch during
  `new`. Objects with `pin = "record"` always fork during `new`: their recorded
  state has no native ref, so the branch is what keeps it from expiring.
  `Repo.materialize_fork(key)` creates a deferred branch on demand;
  `WorkspaceState.pending_forks` records the decisions; `tether new --json`
  reports them. Existing workspaces are unaffected until their next `new`.

## [0.1.0a5] - 2026-09-06

### Added

- Registries and SQL. `tether export PATH` derives relational tables from the
  manifests in VCS history -- `commits`, `commit_parents`, `refs`, `objects`,
  `object_states` (distinct system/state pairs), optional `listings` /
  `listing_entries` and `workspace`, plus `objects_head` / `object_pins`
  views -- into SQLite (default, `--append` upserts), or Parquet / CSV / JSONL
  directories with a `schema.json`. `tether publish --to DSN` upserts the same
  tables into a Postgres schema (`--schema`, default `tether`), skipping
  commits already present and rewriting `refs` / `tether_meta`; `--dry-run`
  prints per-table counts; the DSN comes from `--to` or `$TETHER_PUBLISH_DSN`.
  `tether import SOURCE` reads rows with the canonical object columns (`key`,
  `kind`, `uri` / `locator_json`, `policy_*`, `at`) from a Postgres DSN, SQLite
  file, `.csv`, or `.jsonl` (`--table` / `--query`, or `[import] query` in
  `tether.toml`) and plans `add` / `update` / `remove` (`--sync`) on the
  manifests with `--dry-run` / `--plan` / `--from-plan` like the other planned
  commands; a kind change is refused. Python: `Repo.export()` ->
  `ExportBundle` (`to_sqlite`, `to_dir`, `to_arrow`, `to_postgres`,
  `row_counts`), `Repo.plan_import` / `apply_import` / `import_objects`,
  `tether.export.TABLES` as the single schema definition, and
  `tether.registry.read_source`. One table definition drives SQLite DDL,
  Postgres DDL (JSONB / TIMESTAMPTZ), and Arrow types.
- VCS adapters gained `commit_info(revs)` (one batched `git log --stdin`; jj
  change ids) and `refs()` (bookmarks / branches, tags, head).
- `postgres` extra (`psycopg`) for `publish` and Postgres `import` sources.
- Dev: `pytest-postgresql` runs the `publish` / Postgres `import` tests against
  an ephemeral cluster (skipped when `pg_ctl` is not installed).
- User-guide page "Registries and SQL".

## [0.1.0a4] - 2026-09-05

### Fixed

- Neon pins now hang off the branch the state was fingerprinted on. The state
  gained a `branch` field; previously a state read on a forked working branch
  was pinned as a child of the object's *source* branch at the fork's LSN,
  which named the wrong data (or failed) once the fork had writes. `pin`,
  pin-less `fork`, `verify` (which now also checks the pin's parent), and
  time-travel `open` all use the state's branch. Neon manifests committed by
  earlier alphas lack the field; re-register and re-commit those objects.
- The `git` backend accepts the CLI's positional locator as its `path`
  (`tether add code --kind git ../code`); only `--set path=` worked before.

### Changed

- `gc` never deletes branches on its own again. `0.1.0a3` deleted the working
  branch of a `tether remove`d object by default and, with
  `--prune-workspaces`, every stray `tether.ws.*` branch unconditionally. Now
  a plain `gc` only forgets the removed object's ref (the branch is left for
  `--prune-workspaces`), and `--prune-workspaces` fingerprints each stray
  branch and deletes it only when nothing on it would be lost: its head state
  is natively pinned by some commit, or equals the base branch's head.
  Branches with unpinned writes, with a pin-less (`--pin record`) state, or on
  a backend whose branches are the storage itself are reported as
  `keep-branch` instead.
- New `--force-prune` (`Repo.gc(force_prune=True)`, `plan_gc(force_prune=)`)
  deletes kept branches anyway; the plan marks them `FORCED`.
- New `Capability.BRANCH_IS_STORAGE` (declared by `neon`): deleting a branch
  reclaims its data immediately, so such branches are never pruned without
  force.
- `GcReport` gains `kept_working_refs` and `forgotten_working_refs`;
  `deleted_working_refs` now lists only native branches actually deleted.
  `keep-branch` joins `track` as an informational plan action (`Plan.writes`
  excludes both).

## [0.1.0a3] - 2026-09-05

### Added

- Plans for every store-writing command. `commit`, `new`, and `gc` are now a
  read-only plan followed by an apply: `Repo.plan_commit`/`apply_commit`,
  `plan_new`/`apply_new`, `plan_gc`/`apply_gc`, with `tether.plan.Plan` /
  `Action` serializing to JSON. CLI: `--dry-run` prints the plan, `--plan FILE`
  saves it, `--from-plan FILE` applies it; apply re-fingerprints the planned
  objects and refuses with `StalePlanError` if anything moved. `tether gc`'s
  default dry run now prints the plan.
- Pin-less forks for every backend: `--pin record` (`policy.pin = "record"`)
  removes `PIN` for any object, so `commit` records the state without a native
  ref and `new` forks straight from it (`ObjectBackend.fork` accepts a `Pin` or
  a recorded `State`). Implemented for icechunk, lance, iceberg, lakefs, dolt,
  git, neon (LSN on the base branch), and memory; conformance-tested.
- Working-branch cleanup: `tether gc --prune-workspaces [--keep-workspace ID]`
  deletes `tether.ws.*` branches left by workspaces that no longer exist (and
  this workspace's branches no object uses) via the new
  `ObjectBackend.list_working_refs`. `gc` now also deletes -- not just forgets
  -- the working branch of a `tether remove`d object, and `remove` no longer
  drops the ref so `gc` can find it.
- User guide page "Reclaiming storage": plans, `--pin record`, dropping history
  with `jj`/`git`, `gc`, and pruning dead workspaces.

### Changed

- `Repo.remove` keeps the object's working ref in the workspace state (for
  `gc`); `Repo.add` clears any stale ref for a re-registered key.
- `tether new` prints the working refs it forked; `--json` returns them.

## [0.1.0a2] - 2026-09-05

### Added

- Detached bases and native history: `tether add --at <id>` registers an
  object at a specific snapshot / version / commit / tag instead of a branch
  head (`commit` pins it, `new` forks from it, `open` reads it), `tether log`
  lists a system's native history newest first with branches, tags, and pins
  marked (`--kind` browses before registering), and `--pick` chooses the base
  interactively. Backed by `Capability.HISTORY`, `ObjectBackend.history`, and
  `HistoryEntry`; implemented for icechunk, lance, iceberg, delta, ducklake,
  lakefs, dolt, git, and memory (neon and file refuse `at`). The conformance
  suite checks `history` and `at` for every `HISTORY` backend.

## [0.1.0a1] - 2026-09-05

Re-release of `0.1.0` under an alpha version. `0.1.0` was published to PyPI
without a pre-release marker and has been removed; its code is this release.

### Added

- Initial project scaffold.
- Manifest model (`tether.toml`, per-object manifests, workspace state) with
  canonical hashing and content-addressed pin ids.
- VCS adapters for `jj` and `git` with stale-working-copy detection.
- Backend protocol with declared capability tiers, in-memory reference backend,
  and an importable, capability-parametrized conformance suite.
- Orchestration engine: fan-out snapshot, status, commit, new, open, verify, gc.
- Backends: `file`, `icechunk`, `neon`, `git`, `iceberg`.
- Typer CLI (`tether`).
- Backends: `delta` (Addressable, retention-bound), `lance` (Forkable: tags on
  `(branch, version)`, branches), `lakefs` (Forkable: tags, branches, dirty
  staging detection). Handles: `DeltaHandle`, `LanceHandle`, `LakeFSHandle`.
- `file` backend supports GCS (`gs://`) and Azure Blob (`az://`, `abfs://`)
  alongside S3 through obstore; `--file versioned` records GCS generations and
  Azure version ids like S3 version ids.
- `VcsAdapter.files_at` / `iter_history_files`: batched manifest reads through
  one `git cat-file --batch` process (also for jj repos).
- CLI: `tether add --repository/--prefix` for lakeFS locators.
- Backends: `ducklake` (Addressable, retention-bound: catalog-wide snapshot ids
  via DuckDB's `ducklake` extension, time travel through `SNAPSHOT_VERSION`) and
  `dolt` (Forkable over the MySQL protocol: `dolt_branches`/`dolt_tags` system
  tables, `DOLT_TAG`/`DOLT_BRANCH` procedures, dirty working-set detection).
  Handles: `DuckLakeHandle` (owns its attachment; context manager),
  `DoltHandle` (`db/ref` revision URL). CLI: `--host/--port/--table`.
- Content diffs: `Capability.DIFF`, `ObjectBackend.diff(locator, a, b, *,
  listings)` returning an `ObjectDiff` (unit, added/removed/modified counts,
  capped entries), and `tether diff --content [--limit N]` (`Repo.diff(...,
  content=True)`), fanned out across changed objects with per-object errors.
  Implemented natively for `file`, `git`, `icechunk`, `iceberg`, `delta`,
  `lance`, `lakefs`, `ducklake`, `dolt`, and `memory`; the conformance suite
  checks `diff` for every `DIFF` backend.
- Listings: `ObjectBackend.listing(locator, state)` lets a backend attach a
  detailed description of a state; the engine stores it content-addressed under
  `.tether/listings/<hash>.jsonl`, commits it with the manifests, reads it back
  (working tree or VCS) for diffs, and prunes unreferenced ones in `gc`. The
  `file` backend uses it for directory/prefix listings so Observed directories
  diff file by file.
- Documentation site built with [Great Docs](https://posit-dev.github.io/great-docs/)
  (`great-docs.yml`, `docs` dependency group pinning `great-docs` and
  `quarto-cli`, `.github/workflows/docs.yml` deploying to GitHub Pages). The
  API reference is generated from docstrings, the CLI reference from the Typer
  app (mirrored onto real Click objects by `tether._clickdoc`, since Typer
  vendors its own Click), and the user guide lives in `user_guide/`: getting
  started, concepts, pinning (worked example), branching and writing (worked
  example), CLI guide, configuration, backends, writing a backend.
- Docstrings: Google-style `Args`/`Returns`/`Raises` on every `Repo` method,
  attribute docstrings on result types, handles, `Capability`, `Tier`,
  `VerifyStatus`, and diff types; help text on every CLI option. The package
  root re-exports the result types, manifest helpers, and the `handles`,
  `backends`, `testing`, and `vcs` submodules.
- `tether.Policy` and `tether.Pin` are exported from the package root.
- `tether.toml`'s `[snapshot] auto` now sets the CLI default for
  `status`/`commit` snapshotting (`--no-snapshot` still wins) and
  `[new] auto_fork` makes `commit` re-fork working refs afterwards; both keys
  were previously parsed but unused.

### Changed

- Versions are PEP 440 pre-releases (`0.1.0aN`) while the project is alpha;
  `tether.__version__` now comes from the installed distribution metadata
  instead of a duplicated constant.
- The distribution is published as `tether-vcs` (PyPI prohibits the bare name
  `tether`); the importable package and the CLI remain `tether`. Install with
  `pip install tether-vcs[...]`.
- The `s3` extra now installs `obstore` instead of `boto3`; `objectstore`,
  `gcs`, and `azure` extras are aliases for the same dependency.
- `gc` and `verify --all-history` stream history through a single object
  reader, parse each distinct manifest once, and verify each distinct
  `(system, state, pin)` once, concurrently (~280x faster on a 200-commit repo).
- `verify` and the fork step of `new` fan out concurrently; the fan-out pool
  grew from 8 to 16 workers.
- Local directory fingerprints walk with `os.scandir` (~7x cheaper per file
  than `Path.rglob` + `stat`).
