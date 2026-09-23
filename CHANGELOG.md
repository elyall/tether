# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- `git`: a manifest's `path` must be absolute and outside the checkout; git
  runs no fsmonitor, hook, `ext::` transport or implicit bare repository, and
  ignores an inherited `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE` or
  `GIT_CONFIG_*`.
- `git`: a committed `remote` must name a configured remote; a URL goes in
  `.tether/secrets.toml` (`[objects."<key>"] remote`).
- `import`: `[import] query` is read from `.tether/secrets.toml`, not
  `tether.toml`, and runs as one read-only statement.
- `dolt`: credentials come only from the server's `[uris."mysql://host:port"]`
  entry in `.tether/secrets.toml` (`password_env` / `user_env` move there);
  `$DOLT_PASSWORD` went to any host a manifest named.
- Object keys may not contain `\` or a drive letter, and are checked when a
  manifest is read.
- `.tether/secrets.toml`, `workspace.toml` and `ops.jsonl` are refused while
  jj or git tracks them, with the command that untracks them. The committed
  `.tether/.gitignore` was all that kept them out, so a cloned
  `secrets.toml` naming `git_path` ran that program on `tether status`.
- git 2.38 is the minimum version, checked by the VCS adapter and the `git`
  backend; an older git ignores `safe.bareRepository` without a word.

### Added

- `ObjectBackend.fork`, `promote` and `merge` take `expected=`, the head the
  caller reviewed (or `ABSENT`): the ref moves only from there, else
  `RefMovedError` and nothing moves. Backends without the keyword work as
  before; the engine passes the heads its plans reviewed.
- `tether.plan.REQUIRED_PRECONDITIONS` per command, and a `workspace_bookmark`
  precondition: the checkout's bookmark as `workspace.toml` and the VCS see it.
- The conformance suite checks conditional forks, `PROMOTE`, `MERGE`,
  `ancestor_of` and opening an older recorded state.

- `gc --release-foreign` (`Repo.gc(release_foreign=)`): also release
  unreferenced pins this clone did not create.
- `icechunk`: `allow_http` and `force_path_style` in `.tether/secrets.toml`
  (per URI prefix or object) for an S3-compatible server such as SeaweedFS
  or MinIO; without them `add --create` could not reach one.

- `Capability.CONDITIONAL_REF` declares that a backend's ref moves honour
  `expected`; conformance fails a backend that claims it and ignores it.
- `new --shared` adopts a peer's uncommitted writes when they build on the
  bookmark's pin, instead of asking for `--discard`.

### Changed

- Partial success (an `undo`, `repair`, `upgrade` or `forget-workspace` that
  could not do everything) exits 3; 2 is Click's usage error.
- `--help` keeps bracketed text such as `[experimental]`; `add --kind` lists
  each kind with its maturity.

- A plan must carry the preconditions its command requires; one saved by an
  older tether, or edited, is refused as stale. Every plan binds to the
  checkout that made it, and `commit`, `restore`, `promote` and `drop` plans
  to its bookmark.
- `promote` lands committed states only (`tether commit` uncommitted writes
  first). Merges run before fast-forwards, which are held when a merge does
  not land; the trunk moves to the commit the plan reviewed.
- `restore` checks the head of every branch it would reset, deferred forks
  included, wants `--discard` for uncommitted writes, and refuses a head it
  cannot read.
- `undo` reverses the newest operation only and refuses one it cannot undo
  rather than reaching past it; `tether undo ID` restores only the
  `workspace.toml` fields that entry changed. The `new` and `gc` a `drop` runs
  are its steps (`(step of ID)` in `tether ops`) and cannot be undone alone.
- `new`, `restore` and the first writable `open` hold the repository lock
  while they check and fork, and fork only onto the head the plan saw.
- Without `fcntl` (Windows), writing commands are refused; `status`, `verify`,
  `diff`, `log`, `ops` and `gc --dry-run` work.

- `gc` and `drop` release only pins this clone created (recorded in
  `tether-pinned.jsonl` beside the repository lock, seeded from the op logs).
  Any other unreferenced pin is kept as informational `keep-pin` until it is
  fetched or `--release-foreign` is passed; `GcReport.kept_pins` and `gc
  --json` list them.
- jj 0.43 is the minimum version; an older one is refused.
- jj and git run with tether's own colour and pager settings, whatever the
  user's config says; tether tracks its own files by name, and its revsets
  use no name an alias can redefine. Colour forced on, `all()` aliased or
  auto-tracking off had corrupted commit ids, made empty commits, or shrunk
  the history `gc` walks.
- `file`, `icechunk`, `lance`, `delta`: every spelling of a local path --
  `/p`, `/p/`, `file:///p`, a path through a symlinked parent such as macOS's
  `/tmp` -- is one store to pin ids, listings and `gc`. New pins of an
  object registered under another spelling get new ids.

- `neon`: pins are unprotected unless `protected_pins = true`; Free has no
  protected branches, and paid plans allow a few.
- The dataset format is version 5: run `tether upgrade` once on a 0.1.0b3
  dataset. It gives the stores in `tether-touched.jsonl` and
  `tether-created.jsonl` their new identities, stores listings again under
  their new names, records Lance (`branch_id`), Neon (`commit_xid`) and
  directory (symlinks) states in the new form where the data is unchanged,
  and makes DuckLake paths absolute. 0.1.0b3 refuses a version 5 dataset,
  so clones on the two releases cannot take turns.

- Saved plans are format 3, with a digest binding their actions and context
  to their preconditions; re-run a plan saved in format 1 or 2.
- `gc` and `promote` print what they applied along with the failures, and
  exit 3 when part of the work was done.
- jj calls keep only your identity, signing, snapshot and git settings, and
  your `immutable_heads()` with the revset aliases it names.
- A new file of yours that jj has not snapshotted stays in the change it was
  made in when tether moves the working copy.
- Every `open` follows the checkout's current bookmark; on Windows a default
  `open` is read-only.

### Removed

- The `lakefs` backend, `tether add --repository`/`--prefix`, `LakeFSHandle`
  and the `lakefs` extra.

### Fixed

- Delta `history` and `diff` attached the wrong commit to each version below
  the head.
- Lance states off `main` carry the branch id, so a state from a re-created
  branch verifies as missing instead of opening another branch's data.
- `tether diff` with no arguments compares against jj's `@-`, not the working
  copy commit.
- `file`: `allow_http` and the other HTTP client options work on S3; symlinks
  to directories and dangling ones count by their target; the racy-timestamp
  guard covers every timestamp granularity.
- `tether status` labels an unreadable object `error`, reports the rest and
  exits 1 instead of aborting.
- `tether init --json` prints only JSON.

- Two `--shared` checkouts materializing one lazy fork could throw the first
  one's write away.
- Undoing an older `new` after a commit on its branch deleted the branch, for
  `pin = "record"` the only copy of that state; it now needs `--discard`, and
  the workspace no longer rolls back to that time's bookmark. Undoing an older
  `add` moved the checkout to `main`, so the next write went to the store's
  `main`.
- Two `undo`s after a `drop` revived the abandoned commit.
- jj: undoing a commit other commits were built on rewrote them; refused now
  (`jj backout` reverts in place).
- `promote` landed uncommitted writes and moved the trunk to a commit
  recording an older state; a conflicted merge left its fast-forwards landed;
  a saved plan applied on another bookmark moved the trunk there; the reset
  after a merge discarded a concurrent write.
- A long-lived `Repo`'s writable `open` ignored a `new` another process ran
  since it was constructed.
- `new` kept the previous bookmark's snapshot cache, so `status` and
  `commit --no-snapshot` used the old branch's head under the new bookmark.
- AWS profile or role credentials were never refreshed (now five minutes
  before expiry), and two prefixes of one bucket with different credential
  rules shared the first's.
- The locks were re-entrant per `Repo`, not per thread; a second thread could
  leave that `Repo` unable to lock again.
- jj: `drop` from a bookmark whose working copy had edits planned no leave and
  left `workspace.toml` naming the dropped bookmark.

- `gc` counts every live checkout's working-tree manifests and the pins of
  running or interrupted operations as references; `--prune-bookmarks` keeps
  every branch a live checkout works on or has pending.
- `gc` and `drop` refuse while jj reports a conflicted bookmark or a
  `.tether/` conflict no later commit resolved, and `promote` while its trunk
  is conflicted; `status` and `commit` name a conflicted bookmark instead of
  calling it gone.
- `commit` raises when the new commit's tree lacks a manifest (a dataset under
  an ignored directory made an empty commit `status` called clean).
- git: a hook-refused `git commit` left the manifests staged; the index is
  reset.
- jj: the history walk reads every side of a conflicted commit, so `gc`
  counts the pins each side names.
- `file`: a local path holding `#` or `?` was cut short there, and
  `add --create` on a `file://` URI made a stray `file:` directory.

- A saved `drop` plan applies only in the checkout that made it, and only
  while that checkout is still on (or off) the bookmark as the VCS sees it.
- A checkout writes its workspace id on first open, so a plan saved in a
  fresh clone or worktree applies there.
- `drop` closes its journal entry when the store half fails.
- An op-log entry appended after a torn line is no longer lost with it.
- `--from-plan` refuses `--dry-run` and `--plan`; `commit --from-plan p.json
  --dry-run` committed.
- An `[objects."<key>"]` entry in `.tether/secrets.toml` reaches the store
  `add --create` makes and, once the object is removed, the store
  `gc --delete-stores` reclaims; both used the default endpoint and ambient
  credentials.
- `icechunk`: a `profile` or `role_arn` entry hands Icechunk a refresh
  callback, so a handle held past the role's expiry keeps writing; only new
  opens used to get fresh keys.
- `gc` never releases a pin some manifest names, whichever spelling of the
  store that manifest uses; one store named two ways lost pins `HEAD`
  referenced. `gc --delete-stores` also matches an indexed store by the
  identity its locator has now, so a created store a manifest still names is
  no longer deleted.
- `gc` (a dry run too), `gc --delete-stores` and `verify --all-history` skip,
  with a note, the manifests history holds of a backend tether no longer
  has; a dataset that ever held a lakeFS object failed them even after
  `remove`. Their pins still count as references.

- `promote` landed uncommitted writes after `tether undo` or
  `commit --no-vcs`: it checks forks against the bookmark's commit.
- `tether undo ID` of an older `new -b` sent the checkout to `main`; it
  reverts only the fields nothing has changed since.
- Lance's conditional fork replaced a peer's branch, and git's moved a
  branch checked out in another worktree; `repair`'s refork is conditional.
- git: `status.showUntrackedFiles = no` hid new manifests from `commit`.
- The pin index claimed pins other clones had made, and kept released ones.
- A writable `open` re-read every manifest; only changed files are parsed.
- A plain `status` hid the errors a `status --snapshot` had cached.
- `tether diff` on a jj merge working copy showed every object as added.
- `file`: client options in another spelling (`AWS_ALLOW_HTTP`) panicked, and
  a non-string value (`timeout = 5`) raised.
- Threads resolving one AWS profile or role made one STS call each.
- git: `abandon` left a manifest the abandoned commit had added deleted in
  the worktree, so the next commit deleted it.

### Experimental

- `neon`: `add` requires `database` and `role`; connection URIs name their
  endpoint; busy answers (423, 429, 503) are retried with backoff; a compute
  restart no longer reads as a write.
- `dolt`: a merge with conflicts or constraint violations raises
  `MergeConflict` and writes nothing.
- `iceberg`: tables with no snapshot yet are accepted; requires
  `pyiceberg >= 0.11`.
- `ducklake`: a relative `metadata` path is stored absolute.
- `ducklake`: a leading `~` in `metadata` or `data_path` is expanded (it was
  stored as `<cwd>/~/...`), and a bare `sqlite:` or `duckdb:` metadata path
  is stored absolute too.
- `neon`: objects on one branch fingerprinted at once each created its
  endpoint; Neon allows one read-write endpoint per branch.

## [0.1.0b3] - 2026-09-18

### Experimental

- `neon`: `database` is out of the object identity, as `role` already was:
  two objects on two databases of one project share one pin per commit and
  one working branch per bookmark, and `open` connects each to its own
  database. No migration: existing pins keep their ids; the next `commit`
  re-pins their content under the new id.

### Fixed

- `commit` pins once per pin id: two objects with one identity and one content
  state share the pin, instead of the second being refused for a differing
  volatile address (a Neon LSN).

## [0.1.0b2] - 2026-09-17

### Added

- `tether drop BOOKMARK` (`Repo.drop`) throws a bookmark away: it leaves the
  bookmark if this checkout is on it (`--to`, default the trunk), drops the
  commits only it reaches, deletes it, then releases its branches and the
  pins nothing references (`--delete-stores` adds the created-store step).
- A branch whose head a dropped commit pinned or recorded is deleted, where
  `gc` would keep it as unpinned writes; writes no commit recorded still need
  `--force-prune`.
- `drop` is a dry run by default, refused for the trunk and for a bookmark
  another live checkout works on, and not undoable by tether. What "only the
  bookmark reaches" counts other bookmarks, tags, remote bookmarks and other
  workspaces, and is re-derived at apply. `abandon REV [--gc]` stays for
  commits off a bookmark you keep.

### Fixed

- `keep-store` is an informational plan action: a gc plan holding only
  `keep-store` lines is empty.

## [0.1.0b1] - 2026-09-15

The alpha-exit release and first beta: two security fixes, the engine bugs
an external review found, a hardened backend contract, one migration for
every alpha format (removed at 0.1.0; see Deprecated), a `tether.experimental`
import boundary, a narrower `undo`, and an experimental store lifecycle. What
still has to run against real services is in `ROADMAP.md`.

Upgrade with `tether upgrade`; move `[vcs] git_path`/`jj_path`,
`[backends.neon]`, `[backends.ducklake] init_sql`, `[backends.lakefs]` and any
endpoint or credential option out of `tether.toml` into `.tether/secrets.toml`.

### Security

- **A cloned dataset is untrusted input.** The committed `tether.toml` could
  choose the executables tether runs, the endpoints credentials go to, SQL to
  run, and the environment variable sent as a password. A committed key
  outside a backend's allowlist is refused, naming where it belongs: the
  untracked `.tether/secrets.toml` or the environment (`TETHER_GIT`,
  `TETHER_JJ`).
- **git argument injection.** Manifest and state values reached git
  positionally, so `at = "--output=FILE"` made `git log` write FILE. A `sha`
  must be hex, and a `ref`/`at`/`remote`/`path` beginning with `-` is refused.

### Fixed

- `promote` moved the trunk bookmark backwards or sideways when the trunk had
  commits the bookmark lacked. A full promotion needs the trunk to be an
  ancestor of the bookmark's commit; the trunk never moves backwards
  (`PromoteReport.trunk_held`).
- `new` on an existing bookmark overwrote the branch's fork point, so the next
  `promote` merged or refused where a fast-forward was right.
- Neon put the branch *name* in the content state, so an untouched fork read
  as modified and `new` demanded `--discard`.
- `status` spawned two VCS processes per op-log commit to detect drift.
- Opening a dataset rewrote `.tether/.gitignore`; it is written only when an
  existing untracked file is not yet ignored.
- The checkout lock failed outright when another command held it; it waits up
  to 30 s.
- A saved `gc` plan under jj went stale after any snapshot.
- `name.2` working refs (a Neon or Lance sibling) parse back to their
  bookmark; `new -b` refuses a name ending in `.<number>`.

### Deprecated

- **The alpha upgrade path.** `tether.upgrade`, the one migration from any
  0.1.0aN format, ships with the 0.1.0 betas and is removed at 0.1.0. After
  that an alpha-format dataset fails at open naming the last beta: install
  `tether-vcs==<last beta>`, run `tether upgrade`, reinstall.
  `tether.migrations` is now `tether.upgrade.migrations`.

### Changed

- **Plan preconditions.** What a plan saw is recorded as typed
  `Plan.preconditions` and checked before any apply acts. Saved plans are
  format 2; a format-1 plan is refused (re-run it).
- **`undo` reverses what an operation created and reports the rest.** A
  branch an operation *reset* is reported with its old head and the command
  that moves it, not re-pointed; `undo gc` no longer recreates deleted
  branches (`repair` does).
- **`tether.experimental`.** The backends tested only against fakes (Neon,
  lakeFS, Dolt, DuckLake, Iceberg) and the registry layer (`export`,
  `publish`, `import`) live under `tether.experimental`. Kind names, extras,
  commands, `Repo` methods and the `tether.*` re-exports are unchanged;
  `import tether` loads none of it; `export`/`publish`/`import` print the
  experimental note.
- **One migration.** `tether upgrade` brings any alpha dataset to the current
  version in one step, acting on what the dataset shows rather than its
  recorded version; store renames still fail closed before any history
  rewrite.
- **Backend contract.** Library exceptions surface as `BackendError`, never a
  raw traceback. The conformance suite checks that a fork already at the
  source is left alone, that re-pinning an id at another state raises, and
  that `open`/`unpin` of a missing ref raise `BackendError`. Iceberg is
  `experimental` until it runs against a real catalog.
- Committed option tables (`storage_options`, `catalog`) are screened by a
  per-backend allowlist of option keys; Icechunk, Delta and DuckLake validate
  locators at `add`.
- `tether open` redacts the password in a Neon connection URL unless
  `--with-password` (never under `--json`).
- Icechunk `pin` resolves the snapshot before spending a tag name; Delta
  `verify --deep` reads data files (a vacuumed version is `missing`); `file`
  refuses to fingerprint a prefix with no ETags; Dolt no longer reads a lost
  connection as "no such ref".

### Removed

- `undo --to` and `Repo.undo_to`: several slips are several `undo`s, newest
  first.
- `forget-workspace --force-prune` and its `delete-branch`/`keep-branch`
  actions: `gc --prune-bookmarks` judges branches.
- The alpha-era module paths
  `tether.backends.{neon,lakefs,dolt,ducklake,iceberg}`, `tether.export` and
  `tether.registry`, without a deprecation window.

### Added

- `.tether/secrets.toml` carries per-URI-prefix and per-object credentials
  (`profile`, `role_arn`, `endpoint_url`, `region` or literal keys) for
  Icechunk, `file`, Delta and Lance; resolution is object entry, longest URI
  prefix, kind section, then environment. tether warns when the file is
  readable by others and never prints it.

### Experimental

The store lifecycle lives in `tether.experimental.lifecycle`: the one
operation with no `repair` path, not yet run against real resources. The
commands say so.

- `tether add KEY LOCATOR --kind KIND --create` (`Repo.add(..., create=True)`;
  `Repo.create(key, kind, locator)` adds and opens) makes an empty store with
  an owner marker naming the dataset, recorded in an untracked index beside
  the repository lock; `status` shows `(created)`; `undo add` removes it while
  empty.
- `gc --delete-stores` (opt-in) reclaims a created store once no manifest in
  history or any live checkout names it, every branch in it belongs to a
  bookmark this clone can account for, and the backend confirms nothing else
  remains. A manifest never authorizes a delete: only the marker and your own
  repository's index do.
- Otherwise the plan says `keep-store` with what remains; a store already gone
  is `forget-store`. `gc --store KIND=LOCATOR` names a created store whose
  creator's clone is gone. `undo gc` reports a deleted store as irreversible.
- `gc` also releases this dataset's dead refs in every store this clone forked
  or pinned in (`tether-touched.jsonl`) once no manifest names the store.
- Backend contract: `Capability.CREATE` with `create`, `owner`, `is_ref_empty`
  and `delete_store`, implemented for `memory`, `icechunk`, `git` and `file`
  (Lance, Delta and Iceberg need a schema).

## [0.1.0a10] - 2026-09-13

0.1.0a9 was tagged in history but never published; a10 is the first release
carrying both sets of changes. Datasets created with a8 need `tether upgrade`
(working tree only, no history rewrite).

### Added

- **The op log is a journal.** Store-writing commands journal before the first
  side effect and mark completion after; an interrupted run leaves an
  `INCOMPLETE` entry that `ops` flags, `undo` skips, and `repair --dry-run`
  lists with what got done and what `gc` will collect. Running the command
  again finishes the rest.
- **Locks.** Writing commands hold `.tether/lock` (one writer per checkout) and
  re-read the workspace when they take it; `commit`, `pull`, `gc`, `undo` and
  `abandon` also hold a repository-wide `tether.lock`, so a `gc` in one
  workspace cannot race a `commit` in another.
- **Branch scope.** Objects in one native branch space (a Neon project, an
  Iceberg table) get one branch per bookmark: the first forks, the others
  `share` it, and `gc` counts pins per namespace. (`ObjectBackend.branch_scope`,
  `ref_namespace`.)
- `tether backends` lists kinds with maturity, tier and capabilities;
  `tether --version`. Backends declare `MATURITY`; `add` notes an experimental
  kind.
- `DiffEntry.why` says which of `state`, `pin`, `locator`, `policy` differ; a
  locator- or policy-only change is `changed`.
- `commit` records a state the backend cannot reopen (a `file` object in a
  bucket without versioning) as `recoverable = false` and says why.
- `Pin.created`, `ObjectBackend.LOCAL_PATH_KEYS`, and
  `VcsAdapter.history_digest()`, which `gc` plans bind to.

### Changed

- **Config v4** (`tether upgrade`; working tree only). Manifest paths append
  `.toml` to the key's last segment, so `foo` and `foo.bar` no longer share a
  file; keys are validated. Relative local paths in locators resolve against
  the dataset root; `add` and `import` store them absolute.
- **A pin is verified before it is read or forked.** A deleted pin falls back
  to the recorded state where the backend can address it; a moved pin raises
  `PinDriftError`; `repair` never overwrites a drifted pin.
- **Destructive steps re-check the ref they act on** just before acting and
  stop with `StalePlanError` when it moved. `gc` plans bind to a digest of all
  visible history and do nothing at all when stale; a branch that moves after
  the preflight is kept and reported.
- A lazy fork resets an existing branch only onto the head `new` reviewed,
  reuses one already at the pin, and otherwise refuses; `new` refuses when the
  backend cannot list branches.
- **`commit` compensates as a unit.** A failure after the pins releases only
  the pins this commit created and restores what it wrote; if the VCS commit
  landed first, the commit stands and the error is reported.
- **`restore` and `promote` are closed over the branch scope.** Naming only
  some of the objects that write through one branch is refused.
- `promote` fast-forwards and merges from the state the plan reviewed, not the
  source ref's current head. A bookmark is *planned* whole or not at all; once
  applying, each system's fast-forward stands on its own.
- The convenience forms (`tether commit`, `Repo.new()`, ...) plan and apply
  under one lock and re-verify at apply; `snapshot` and `pull` read the refs
  the on-disk workspace names.
- `new` writes `workspace.toml` before its first store write and `restore`
  after each reset, so a killed run leaves a consistent workspace.
- `set --pin record` on a pinned object drops the pin at the next commit;
  `--pin native` creates one again.
- `file`: `--file versioned` makes only a single *remote* object Addressable;
  a recorded state without a version id is refused by `open` and reported by
  `verify`.
- Backends: `unpin` and `delete_working_ref` raise when the ref is still there
  afterwards; `delta`/`iceberg` read-only `open` of an object registered `at`
  a version sits there; `neon` follows branch-list pagination and lifts
  protection before deleting a pin.

### Fixed

- `commit` rolled back pins it had not created when a later pin in the same
  commit failed.
- `open --rev`, `new` and `promote --rev` trusted a pin's native ref; a tag
  moved by hand returned the wrong data under a commit's name.
- Two keys in one Neon project each forked the shared branch from their own
  pin, so opening the second reset the first's writes; one object's `gc`
  sweep released its neighbours' pins.
- A saved `gc` plan deleted a branch that had gained writes since planning; a
  `promote` landed a source that moved after review.
- `Repo.diff` reported an object unchanged when only its locator or policy
  differed.
- Relative locator paths were resolved against each command's working
  directory.
- The v4 migration could leave two files for one key or a dirty tree that
  blocked the rerun; it checks every destination before moving anything.
- `snapshot` from a `Repo` constructed before another process moved the
  checkout cached states under the wrong bookmark.
- `--file versioned` recorded a state with no version id as recoverable.

## [0.1.0a9] - 2026-09-11

### Added

- **Bookmark-shaped branches.** The trunk bookmark (`main`; `[vcs] trunk`)
  stands for every object's upstream branch; any other bookmark is one branch
  per Forkable system, `tether.ws.<dataset>.<bookmark>`, forked from the pins
  of the commit it started at; a working copy on no bookmark is read-only.
- `tether new -b NAME [REV]` creates a bookmark and its branches, `new NAME`
  joins one, `new REV` takes the bookmark at that commit or goes read-only; a
  bookmark another live checkout works on is refused unless `--shared`.
  `init` creates the trunk bookmark (jj) or adopts HEAD's branch (git).
- `commit` moves the bookmark onto the new commit (one jj operation, so
  `jj undo` takes both back) and refuses when the working copy has left it;
  `new NAME --keep` puts the working copy back without touching branches.
- `tether pull [BOOKMARK]` reads the heads of the bookmark's branches (on the
  trunk, every upstream branch and branch-less object), pins what moved and
  commits it (`PullReport`). Replaces `commit --pull`, the `pulled` status and
  `[commit] pull`.
- `promote` also moves the trunk bookmark to the bookmark's commit when
  everything fast-forwarded (`PromoteReport.trunk_moved`); after a merge,
  commit and promote again.
- `gc --prune-bookmarks` (was `--prune-workspaces`; `--keep-bookmark NAME`)
  judges the branches of bookmarks the VCS no longer has and no live checkout
  works on. `forget-workspace` removes state files and forgets the checkout
  only.
- `status` names the bookmark and warns, with what to do, when the VCS
  deleted, renamed, moved or left it.
- **`tether set KEY... | --all [--file] [--pin]`** changes a registered
  object's policy in place: manifest-only, logged, undoable.
- A **Caveats and Performance** guide, and a **Use Cases** guide whose seven
  scenarios run as tests against local backends; the README is a third of its
  former length.

### Changed

- **`promote` lands a bookmark whole or not at all.** When any object is
  refused and no keys were named, the rest are `held` and nothing is written.
  `promote KEY...` lands a subset on purpose; the trunk bookmark never moves
  for a subset. Exit status is still 1 on refusals.
- **`tether status` is local by default.** It shows each object's last
  snapshot with its age and contacts nothing; `--snapshot` fingerprints first.
  `[snapshot] auto` defaults to `false`; `verify` always fingerprints;
  `commit` fingerprints the bookmark's branches.
- Local files are fingerprinted by **content hash**, not mtime, so `touch`,
  `cp`, a fresh checkout or an rsync no longer read as drift; hashes are
  cached in `.tether/cache/file-hashes.json`. **Breaking**: `[tether] version`
  is 3 and `tether upgrade` re-fingerprints every local `file` object in the
  working tree.

### Removed

- `[new] auto_fork`: since `new` reuses a branch already at the pin, it had
  stopped doing anything.
- The `write` policy (`write = fork | direct`) and `--write`, `set --write`,
  `[defaults] write`, the `policy_write` registry column: whether writes fork
  or land upstream is now which bookmark the working copy is on. A manifest
  carrying `write` is read and ignored; `tether upgrade` drops the line.

### Fixed

- `ForgetWorkspaceReport` was listed in `tether.__all__` but never imported.
- `tether diff REV` compared REV with nothing and reported every object
  `removed`; it compares REV with the working tree.
- `tether diff --content` between two Icechunk snapshots on different
  branches failed; it now diffs each side against the snapshot they diverged
  from.
- Fingerprinting several local `file` objects at once could fail on the shared
  hash cache's temp file.
- A deleted Icechunk pin could never come back (Icechunk tombstones every
  deleted tag). The pin id stays; its ref moves on through generations
  (`tether.<id>.2`, `.3`, ...) that every reader resolves and `gc` counts as
  one pin. Until a repair, `open` and `new` fall back to the recorded state.
- `tether new -b NEW` from a commit on bookmark `OLD` handed `NEW` the
  branches of `OLD` that sat at the pin, so `NEW`'s writes landed on `OLD`'s.
  `new` now touches only the branch named after its bookmark.

## [0.1.0a8] - 2026-09-09

### Added

- **Operation log.** Every store-writing command appends to the untracked,
  per-workspace, append-only `.tether/ops.jsonl` the plan it applied, what it
  did, and what it replaced. `tether ops` and `Repo.ops()` read it.
- **`tether undo [ID]`** (`Repo.undo`) reverses an op-log entry where the
  store still allows it and says what it could not: a `commit` is uncommitted
  (pins stay); a `new` or lazy fork has its branches deleted or re-pointed and
  `workspace.toml` restored; `gc`'s released pins are irreversible;
  `promote` is refused with the previous heads printed. A branch that gained
  writes since is refused without `--discard`.
- **`tether undo --to OP_ID`** (`Repo.undo_to`) undoes every operation newer
  than `OP_ID`, newest first.
- **`tether repair`** (`Repo.repair`) recreates pins whose native ref is
  missing (`--all-history` for every commit's) and working branches the store
  lost, from the recorded states; drifted pins are noted, never overwritten.
- **`tether upgrade`** (`Repo.upgrade`). `tether.toml` carries `[tether]
  version`; an older dataset is refused by every command except `upgrade`.
  The v2 migration gives the dataset an id, renames every pin and working
  branch in every store, and rewrites history to match: commit ids change and
  other clones must re-sync. It fails closed, and a re-run continues.
- **`status` and `ops` notice the VCS going around tether**: a `commit` whose
  commit left visible history is warned about in `status` and flagged `(vcs
  commit gone)` in `ops`.
- **`tether forget-workspace [ID]`** (`Repo.forget_workspace`): `jj workspace
  forget` / `git worktree remove` plus tether's half.
- **`tether restore KEY... --from REV`** (`Repo.restore`) resets one object's
  working branch to what `REV` pinned, leaving everything else alone; refused
  for unpinned writes unless `--discard`; undoable.
- **`tether abandon REV... [--gc]`** (`Repo.abandon`) drops dataset commits
  from history and shows, or with `--gc` applies, the `gc` plan for the pins
  only they referenced; descendants keep their manifests. Not undoable by
  tether.
- **`tether new --discard`.** `new` refuses, before touching anything, when a
  branch it would reset holds writes beyond what this workspace last
  committed; `--discard` opts in.

### Changed

- **Native refs are namespaced by dataset.** `tether.toml` carries an 8-hex
  `[dataset] id`; pin refs are `tether.<id>.<hash16>` and working branches
  `tether.ws.<id>.<workspace8>.<slug>-<key6>`. `gc` stays inside the
  namespace, `--force-prune` included; before, it deleted other datasets'
  pins in a shared store. Breaking for earlier alphas: run `tether upgrade`.
- `new` keeps a working branch that already sits exactly at the pin (the plan
  says `reuse`); on Neon this stops each commit-then-new cycle from leaving a
  sibling branch behind.
- `gc --prune-workspaces` and `forget-workspace` plan a branch the store
  refuses to delete (Neon: pins hang off it) as `keep-branch: cannot be
  deleted` instead of failing at apply.
- `commit` commits to the VCS when the manifests are dirty even if no state
  changed (an undone commit, an `add`, an `import`).
- git pins with a `remote` fail when the push fails, and `unpin` deletes on
  the remote first, so local and remote tags never diverge silently.
- Neon's identity no longer includes `role`; Neon API errors surface as
  `BackendError`. Iceberg's `metadata_location` is volatile, so pre-a7
  manifests keep their content identity.

## [0.1.0a7] - 2026-09-08

### Fixed

- States separate *content* from *address*: backends declare `VOLATILE_KEYS`
  (Neon's `lsn`, git's `change_id`), which drift detection, pin ids and
  `promote` ignore. Unrelated Iceberg commits and Neon checkpoints no longer
  show as drift or make duplicate pins, and one git sha pins the same with or
  without jj.
- git `dirty` is computed only for the checked-out ref.
- `fork()` onto an existing branch name resets it to the source on every
  backend; Neon `pin()` refuses an existing pin branch at another parent or
  LSN.
- Stale-workspace detection is per object: registering or removing other
  objects no longer un-stales a workspace, and `status` names the stale
  objects. Export schema_version 3.
- Working-ref names end in a 6-hex digest of the key, so keys that slugify
  alike no longer share a branch; pin ids are 16 hex chars. Pins and branches
  from earlier alphas are not recognised: re-commit and `new`.
- Neon `fork()` onto an existing working branch always restores it onto the
  source; when pins hang off the branch, the fork lands on a sibling name.
- A `new` in which some forks fail records the branches it did create, and a
  second `new` completes the job.
- `tether new REV` in git no longer leaves a detached HEAD: another revision
  is checked out onto a `tether/<rev12>` branch so later commits stay
  reachable by `gc`.
- `verify --all-history` checks recorded (pin-less) states as well as pins.

### Changed

- `gc --prune-workspaces` finds every live checkout (jj workspaces, git
  worktrees) and keeps their branches; `--keep-workspace` is only for ids live
  elsewhere.
- `export`, `publish`, `import` and the lakeFS, Dolt and DuckLake backends are
  labelled experimental in the CLI help, README and guide.
- `StatusReport.stale_keys` and `Repo.stale_keys()` list the stale objects.
- `tether import` updates that change an object's locator drop its working
  branch and fork point, so writes no longer go to the old system's branch.

## [0.1.0a6] - 2026-09-07

### Added

- `tether promote [KEY]... [--rev REV] [--strategy auto|ff|merge] [-m MSG]`
  (`Repo.promote`, `PromoteReport`) moves each system's base branch to what
  this workspace's fork holds. Base unchanged since the fork point:
  fast-forward (icechunk, iceberg, git, lakeFS, Dolt). Base moved: native
  three-way merge (git, lakeFS, Dolt; a `MergeConflict` writes nothing).
  Otherwise refused with the system's recipe. `--dry-run` / `--plan` /
  `--from-plan` as for other planned commands; exit 1 when anything was
  refused.

### Changed

- Working branches are forked lazily by default: `new` decides each branch
  (`defer-fork`) and the first writable `open` creates it, so a workspace that
  never writes to an object leaves no branch behind. `new --eager` /
  `[new] fork = "eager"` forks during `new`; `pin = "record"` objects always
  do. `Repo.materialize_fork(key)` creates a deferred branch on demand.

## [0.1.0a5] - 2026-09-06

### Added

- Registries and SQL. `tether export PATH` derives relational tables
  (`commits`, `commit_parents`, `refs`, `objects`, `object_states`, optional
  `listings` / `listing_entries` / `workspace`) from history into SQLite
  (default; `--append` upserts) or Parquet / CSV / JSONL directories.
- `tether publish --to DSN` upserts the same tables into a Postgres schema
  (`--schema`, default `tether`), skipping commits already present; the DSN
  comes from `--to` or `$TETHER_PUBLISH_DSN`. Needs the `postgres` extra.
- `tether import SOURCE` reads rows with the canonical object columns from
  Postgres, SQLite, `.csv` or `.jsonl` (`--table` / `--query`) and plans
  `add` / `update` / `remove` (`--sync`) like the other planned commands.
- Python: `Repo.export()` -> `ExportBundle`, `Repo.plan_import` /
  `apply_import` / `import_objects`. User-guide page "Registries and SQL".

## [0.1.0a4] - 2026-09-05

### Fixed

- Neon pins hang off the branch the state was fingerprinted on; a state read
  on a fork was pinned under the *source* branch, naming the wrong data.
  Manifests from earlier alphas lack the field: re-register and re-commit.
- The `git` backend accepts the CLI's positional locator as its `path`.

### Changed

- `gc` never deletes branches on its own again: a plain `gc` only forgets a
  removed object's ref, and `--prune-workspaces` deletes a stray branch only
  when its head is pinned by a commit or equals the base branch's head.
  Branches with unpinned writes, a `--pin record` state, or on a backend whose
  branches are the storage (`BRANCH_IS_STORAGE`: Neon) are `keep-branch`.
- `--force-prune` (`Repo.gc(force_prune=True)`) deletes kept branches anyway;
  the plan marks them `FORCED`. `GcReport` gains `kept_working_refs` and
  `forgotten_working_refs`.

## [0.1.0a3] - 2026-09-05

### Added

- Plans: `commit`, `new` and `gc` are a read-only plan followed by an apply
  (`Repo.plan_*` / `apply_*`; `tether.plan.Plan`). `--dry-run` prints the
  plan, `--plan FILE` saves it, `--from-plan FILE` applies it and refuses with
  `StalePlanError` if anything moved; `gc`'s default dry run prints the plan.
- `--pin record` (`policy.pin = "record"`) makes `commit` record the state
  without a native ref and `new` fork straight from it, on every backend.
- `tether gc --prune-workspaces [--keep-workspace ID]` deletes `tether.ws.*`
  branches left by workspaces that no longer exist.
- User guide page "Reclaiming storage".

### Changed

- `Repo.remove` keeps the object's working ref for `gc`; `Repo.add` clears a
  stale ref for a re-registered key. `tether new` prints the working refs it
  forked.

## [0.1.0a2] - 2026-09-05

### Added

- `tether add --at <id>` registers an object at a specific snapshot, version,
  commit or tag instead of a branch head; `--pick` chooses interactively. Neon
  and `file` refuse `at`.
- `tether log` lists a system's native history newest first with branches,
  tags and pins marked (`--kind` browses before registering), for every
  backend with `Capability.HISTORY`.

## [0.1.0a1] - 2026-09-05

Re-release of `0.1.0` under an alpha version. `0.1.0` was published to PyPI
without a pre-release marker and has been removed; its code is this release.

### Added

- Manifest model (`tether.toml`, per-object manifests, workspace state) with
  content-addressed pin ids; VCS adapters for `jj` and `git` with
  stale-working-copy detection.
- Backend protocol with capability tiers, an in-memory reference backend, and
  an importable conformance suite.
- The engine: fan-out snapshot, status, commit, new, open, verify, gc. Typer
  CLI (`tether`).
- Backends: `file` (local, S3, GCS and Azure through obstore; `--file
  versioned` records version ids), `icechunk`, `neon`, `git`, `iceberg`,
  `delta`, `lance`, `lakefs`, `ducklake` and `dolt`, with typed handles.
- Content diffs: `tether diff --content [--limit N]` asks each changed
  object's backend for its native diff.
- Listings: a per-file description of a directory or prefix state, stored
  content-addressed under `.tether/listings/`, so directories diff file by
  file; pruned by `gc`.
- Documentation site (Great Docs, GitHub Pages): API and CLI reference and a
  user guide. `[snapshot] auto` and `[new] auto_fork` take effect.

### Changed

- Versions are PEP 440 pre-releases (`0.1.0aN`); `tether.__version__` comes
  from the installed distribution.
- The distribution is `tether-vcs` (PyPI prohibits the bare name); the
  package and the CLI remain `tether`.
- The `s3` extra installs `obstore` instead of `boto3`; `objectstore`, `gcs`
  and `azure` are aliases.
- `gc` and `verify --all-history` verify each distinct `(system, state, pin)`
  once, concurrently (~280x faster on a 200-commit repo); local directory
  fingerprints are ~7x cheaper per file.

[Unreleased]: https://github.com/elyall/tether/compare/v0.1.0b3...HEAD
[0.1.0b3]: https://github.com/elyall/tether/compare/v0.1.0b2...v0.1.0b3
[0.1.0b2]: https://github.com/elyall/tether/compare/v0.1.0b1...v0.1.0b2
[0.1.0b1]: https://github.com/elyall/tether/compare/v0.1.0a10...v0.1.0b1
[0.1.0a10]: https://github.com/elyall/tether/compare/v0.1.0a9...v0.1.0a10
[0.1.0a9]: https://github.com/elyall/tether/compare/v0.1.0a8...v0.1.0a9
[0.1.0a8]: https://github.com/elyall/tether/compare/v0.1.0a7...v0.1.0a8
[0.1.0a7]: https://github.com/elyall/tether/compare/v0.1.0a6...v0.1.0a7
[0.1.0a6]: https://github.com/elyall/tether/compare/v0.1.0a5...v0.1.0a6
[0.1.0a5]: https://github.com/elyall/tether/compare/v0.1.0a4...v0.1.0a5
[0.1.0a4]: https://github.com/elyall/tether/compare/v0.1.0a3...v0.1.0a4
[0.1.0a3]: https://github.com/elyall/tether/compare/v0.1.0a2...v0.1.0a3
[0.1.0a2]: https://github.com/elyall/tether/compare/v0.1.0a1...v0.1.0a2
[0.1.0a1]: https://github.com/elyall/tether/releases/tag/v0.1.0a1
