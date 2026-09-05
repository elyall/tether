# tether

**jj-style version control for heterogeneous datasets.**

`tether` ties heterogeneous data objects -- files and object-store prefixes (S3,
GCS, Azure), [Icechunk](https://icechunk.io) repositories, [Neon](https://neon.com)
Postgres databases, [Apache Iceberg](https://iceberg.apache.org) tables,
[Delta Lake](https://delta.io) tables, [Lance](https://lance.org) datasets,
[lakeFS](https://lakefs.io) repositories, [DuckLake](https://ducklake.select)
catalogs, [Dolt](https://www.dolthub.com) databases, and git/[jj](https://jj-vcs.dev)
repositories -- to the commits of an existing git or jj repository.

A `tether commit` records each object's state and, for systems that support it,
creates a durable native reference (an Icechunk tag, a Neon child branch, a git
tag, an Iceberg tag, a Lance tag, a lakeFS tag, a Dolt tag) so the exact state
can be recovered and branched from later.
`tether new` forks fresh writable branches off any committed state. The VCS
supplies history, undo, bookmarks, workspaces, and sharing; `tether` only
implements what is novel: cross-system fan-out, drift detection, and the
pin/fork lifecycle.

Think of it as **DVC for *branchable* systems**: like DVC it commits small
manifests into your git/jj repo, but where DVC only fingerprints files, tether
also *pins* and *forks* live systems.

> Status: alpha. See [CHANGELOG.md](CHANGELOG.md).

## Why this exists (prior art)

Every existing tool versions a single layer:

| Tool | Scope | Relationship to tether |
| --- | --- | --- |
| lakeFS, Quilt, Oxen, DataChain | objects / files | analog of our `file` objects; lakeFS is also a backend |
| **DVC** (lakeFS-owned) | files/objects in git | closest structural analog; no pin/fork of live systems |
| Dolt, pgGit, Neon, Databricks Lakebase | one database | vendor-bound; no external-object pins; Dolt and Neon are backends |
| Nessie, Bauplan | Iceberg catalog branching | Iceberg-only |
| Icechunk, Lance, Delta, DuckLake | one dataset / table / catalog | we use them as backends |
| replikativ/yggdrasil | cross-system (Clojure) | right shape, not adoptable (immature, stale Python binding) |
| Dagster observable assets | staleness detection | analog of our snapshot/drift step; no branching |

Nothing provides unified version control *across* files + Icechunk + Postgres +
Iceberg with pinning and forking. So tether borrows jj's working-copy model
(fingerprint, snapshot on every command, stale-working-copy detection), DVC's
manifests-in-VCS layout, and Yggdrasil's observe-then-record shape.

## Install

```bash
pip install tether[cli]                 # core + CLI
pip install tether[cli,icechunk,neon]   # add backends you need
pip install tether[all]                 # everything
```

Extras: `cli`, `objectstore` (S3/GCS/Azure for `file`; `s3`/`gcs`/`azure` are
aliases), `icechunk`, `neon`, `iceberg`, `delta`, `lance`, `lakefs`, `ducklake`,
`dolt`, `all`. `git`/`jj` must be on `PATH`.

## Quickstart

```bash
tether init
tether add zarr/imaging --kind icechunk s3://bucket/imaging.zarr.icechunk --write fork
tether add db/rosebud   --kind neon --project-id prj-123 --database neondb --role runner
tether add raw/plate1   --kind file s3://bucket/raw/plate1/     # Observed unless versioned
tether add raw/manifest --kind file gs://bucket/manifest.csv --file versioned   # Addressable
tether add features     --kind lance s3://bucket/features.lance
tether add events       --kind delta s3://bucket/events        # Addressable (retention-bound)
tether add lake         --kind lakefs --repository analytics --branch main --prefix raw/
tether add warehouse    --kind ducklake ducklake:postgres:dbname=lake --table events
tether add ledger       --kind dolt --host dolt.internal --database ledger --branch main
tether add code         --kind git . --remote origin

tether status                       # fan-out: modified / unpinned / drifted per object
tether commit -m "Baseline imaging + metrics"   # pins, writes manifests, jj/git commit
tether new main                     # fork fresh writable branches off main's pins
tether open db/rosebud              # -> postgresql://...tether.ws.ab12cd34...
tether open zarr/imaging -r main    # read-only handle at main's pinned tag
tether diff main @ --content         # what changed inside each object, natively
tether verify --all-history --deep
tether gc --dry-run
```

Python:

```python
from tether import Repo

repo = Repo.find(".")
repo.status()
repo.commit("baseline")
repo.new("main")
handle = repo.open("zarr/imaging")  # writable IcechunkHandle
session = handle.session  # native icechunk session
ro = repo.open("zarr/imaging", rev="main")  # read-only at the pinned tag
```

Set `TETHER_REV=<vcs rev>` and `repo.open(key)` defaults to a read-only handle at
that commit's pinned state -- convenient for downstream, reproducible reads.

## Concept map (jj / DVC -> tether)

| jj / DVC | tether |
| --- | --- |
| working copy | the VCS working dir; the committed thing is the manifests |
| `TreeState.snapshot()` | **snapshot**: concurrent fan-out fingerprint -> `workspace.toml` |
| `commit` | **pin** fan-out + write state into manifests + `jj/git commit` |
| `new <rev>` | checkout + **fork** fan-out (fresh writable branch per object) |
| stale working copy | manifest hash recorded at fork; differs from HEAD => refuse writes |
| bookmarks / op log / undo / workspaces / push | delegated to the VCS |
| `.dvc` files | `.tether/objects/<key>.toml` (committed) |
| `jj diff` | **diff**: object-level manifest diff; `--content` asks each backend for its native diff (files, tables, arrays, fragments, commits) |
| -- | **verify** (pins still resolve) and **gc** (drop unreferenced pins) |

## Repository layout

```
<dataset-root>/
  tether.toml                 # committed: snapshot.auto, verify.on_status, new.auto_fork,
                              #   default policies, backend options, credential *references*
  .tether/
    .gitignore                # ignores workspace.toml
    objects/<key>.toml        # committed, one per object: kind, locator, policy, state, pin
    listings/<hash>.jsonl     # committed, content-addressed per-file listings for directory/prefix
                              #   states (so `diff --content` can compare them); pruned by gc
    workspace.toml            # untracked: workspace_id, base manifest hash, working refs, last snapshot
```

`pin_id = blake2b(kind, locator identity, state)[:12]`; the native ref is
`tether.<pin_id>`. Identical state yields the same pin, so re-committing an
unchanged object is a no-op and identical states dedupe to one pin.

## Capability tiers

Backends declare capabilities; commands degrade explicitly by tier.

- **Observed** (`FINGERPRINT`): drift detection only. `commit` records state and
  marks it `recoverable = false`. E.g. a local file by mtime/size.
- **Addressable** (`+ ADDRESSABLE`): an immutable version id can be read later;
  `open -r <rev>` works, nothing to create or GC. E.g. an S3 object in a
  versioned bucket, a Delta table version.
- **Pinnable** (`+ PIN`): can create/delete a durable, GC-proof ref; `commit`
  pins, `verify`/`gc` apply. E.g. an Icechunk tag.
- **Forkable** (`+ FORK`): a writable branch can be created off a pin; `new`
  forks, `open` returns a writable handle. E.g. Icechunk, Neon, Iceberg, Lance,
  lakeFS, Dolt, git/jj.

Orthogonal flags: `CHEAP_FINGERPRINT`, `RETENTION_BOUND`, `NEEDS_QUIESCENCE`,
`ATOMIC_REF`, `DIFF` (the backend can describe what changed between two
recorded states; see [Content diffs](#content-diffs)).

### Compatibility matrix

| Backend | Max tier | State | Pin | Fork | Notable flags / caveats |
| --- | --- | --- | --- | --- | --- |
| `file` (local) | Observed | size, mtime_ns (or dir digest) | -- | -- | `CHEAP`; immutable-by-default, drift is an error |
| `file` (S3 / GCS / Azure object) | Addressable | size, etag, version_id | -- | -- | `CHEAP`; `--file versioned` on a versioning-enabled bucket; one `HEAD` |
| `file` (S3 / GCS / Azure prefix) | Observed | count, size, etag digest | -- | -- | `CHEAP`; one paged `LIST`, no per-object calls |
| `icechunk` | Forkable | snapshot_id | tag | branch | `ATOMIC_REF`; tags immutable, excluded from expiry |
| `neon` | Forkable | lsn, next_xid | protected child branch @ parent_lsn | child of pin | `NEEDS_QUIESCENCE`, `RETENTION_BOUND`; no merge/promote, leaf-only gc, quotas |
| `git` / `jj` | Forkable | sha, change_id, dirty | tag (pushed if `remote`) | branch | `CHEAP`, `ATOMIC_REF`; local path only for now |
| `iceberg` | Forkable | snapshot_id, metadata_location | tag (`native`) or recorded id (`record`) | branch | `RETENTION_BOUND`; `record` for S3 Tables (no native ref) |
| `delta` | Addressable | version, table_id | -- | -- | `CHEAP`, `RETENTION_BOUND`; no native tags; `VACUUM`/log retention bound readability |
| `lance` | Forkable | branch, version | tag on (branch, version) | branch | `ATOMIC_REF`; tagged versions exempt from cleanup; version numbers are branch-scoped |
| `lakefs` | Forkable | commit_id (+ dirty) | tag | branch | `CHEAP`, `ATOMIC_REF`; repo-wide pins, `prefix` scopes the handle |
| `ducklake` | Addressable | snapshot_id, snapshot_time_us | -- | -- | `CHEAP`, `RETENTION_BOUND`; catalog-wide snapshots; `ducklake_expire_snapshots` bounds readability |
| `dolt` | Forkable | commit (+ dirty) | tag | branch | `ATOMIC_REF`; over MySQL protocol to `dolt sql-server`; handles are `db/ref` revision URLs |
| `memory` | Forkable | snapshot_id | tag | branch | reference impl for tests |

Command requirements: `status`/`snapshot`/`verify` need `FINGERPRINT`; `commit`
pins `PIN` objects (records `ADDRESSABLE`, records + warns for Observed, or fails
with `--strict`); `new` forks `FORK` objects; `gc` applies to `PIN` objects;
`diff --content` applies to `DIFF` objects.

### Content diffs

`tether diff A B` compares manifests: which objects were added, removed, or
changed (with pin ids). `--content` additionally asks each changed object's
backend for its *native* diff, using metadata only (no data is read), run
concurrently across objects:

| Backend | Unit | Source | Example detail |
| --- | --- | --- | --- |
| `file` (dir / prefix) | files | two stored listings (`.tether/listings/`) | `modified a.tif +4.0 KiB` |
| `file` (object) | objects | recorded size / etag / version | `etag a -> b, version v1 -> v2` |
| `git` / `jj` | files | `git diff --name-status` + `--numstat` | `modified main.py +1 -1` |
| `icechunk` | nodes | `Repository.diff(from, to)` | `modified /x 2 chunks` |
| `iceberg` | snapshots | snapshot summaries along `b`'s ancestry to `a` | `append: +5 rows, +1 files` |
| `delta` | commits | transaction-log `history()` in `(a, b]` | `WRITE: +2 rows, +1 files` |
| `lance` | fragments | fragment ids / deletions / schema at both versions | `added fragment 1 +1 rows` |
| `lakefs` | objects | server-side `Reference.diff`, scoped to `prefix` | `added raw/c.bin 1 B` |
| `ducklake` | tables | snapshot `changes` + `ducklake_table_changes` | `modified main.t +5000 rows, -1 rows` |
| `dolt` | tables | `dolt_diff_summary` + `dolt_diff_stat` | `modified t +6 rows` |
| `neon` | -- | no native diff; not supported (yet) | |

Directory and prefix states are digests, so the `file` backend hands the
engine a per-file listing at commit time; it is stored content-addressed under
`.tether/listings/` and committed with the manifests (the same trick as DVC's
`.dir` files). Diffs of commits made before listings existed fall back to a
count/size summary. `gc` removes listings no manifest references. Entries are
capped at 2000 per object; counts are exact.

## Writing a backend

Implement the `ObjectBackend` protocol (`tether.backends.base`): declare
`kind` and `capabilities`, then `identity`, `fingerprint`, `pin`, `unpin`,
`list_pins`, `verify`, `fork`, `delete_working_ref`, and `open`. A backend whose
tier varies per object (like `file`) can override
`effective_capabilities(locator, policy)`. To support `diff --content`, add
`DIFF` and implement `diff(locator, a, b, *, listings)` returning an
`ObjectDiff`; if your state is a digest, also implement `listing(locator,
state)` and the engine will store and hand back the text. Register it with
`register_backend`.

Validate it against the importable conformance suite:

```python
from tether.testing import run_conformance


class MyHarness:
    capabilities = ...  # optional; defaults to backend.capabilities
    backend = MyBackend()

    def new_object(self): ...  # create a fresh system, return a locator
    def mutate(self, locator, working_ref): ...  # change current state


run_conformance(MyHarness())  # runs only the tier-appropriate checks
```

## Caveats

- **No cross-system atomicity.** A commit records each object's state within a
  capture window; point-consistency requires quiesced writers (the Neon check
  helps; `tether commit` refuses while writers are active unless `--force`).
- **Pin-then-commit ordering.** Pins are created before the VCS commit; if the
  commit never lands, pins leak until `gc` (which scans VCS history + the current
  workspace). Re-run `tether verify` after merging manifests across branches.
- **Neon lineage.** Pins are child branches, so a working branch can't be deleted
  while its pins exist, `gc` only drops leaf pins, and fork-per-`new` deepens the
  tree. Neon cannot merge/promote a child into `main`; use `--write track` on
  `main` for production. `protected` needs a paid plan; branch quotas vary.
- **Neon fingerprint noise/cost.** LSN moves without user writes (`next_xid` is
  the real change signal); snapshot may wake a suspended compute. Use
  `--no-snapshot` to skip.
- **Files on unversioned storage** are Observed: detectably but not recoverably
  drifted. Bucket/container versioning promotes single objects to Addressable;
  prefixes stay Observed (tether does not record per-object version ids).
  Object-store credentials come from the environment; `[backends.file]
  storage_options` in `tether.toml` passes non-secret options (region,
  endpoint, account name) to obstore.
- **Iceberg catalog variance.** S3 Tables disables maintenance when user refs
  exist; use `--pin record` there.
- **Delta has no refs.** Versions are recorded, not pinned; `VACUUM` (7-day
  default) and log retention (30 days) bound how long `open -r` works. A
  recreated table (new metadata id) verifies as drifted, not re-addressed.
- **Lance versions are branch-scoped.** State is `(branch, version)`; an
  untouched fork reports its parent's address so no-op commits stay no-ops.
  Lance refuses to delete a branch a tag references, so a pinned working branch
  outlives its workspace until `gc` drops the tag (a re-fork picks `name.2`).
- **lakeFS staging.** Uncommitted lakeFS changes are not in any commit; a dirty
  branch is reported in `status` and refused at `commit`.
- **DuckLake attachments.** A DuckDB-file metadata catalog can be attached only
  once per process, so tether attaches per operation and a `DuckLakeHandle` owns
  its attachment: `close()` it (or use it as a context manager) before the next
  `status`, and don't hold your own writer connection open in the same process.
  Postgres/SQLite/MySQL catalogs have no such limit. Snapshots are catalog-wide;
  `table` only scopes the handle. Put `CREATE SECRET` statements in
  `[backends.ducklake] init_sql` rather than credentials in the locator.
- **Dolt working sets.** Uncommitted changes on a branch are not in any commit;
  a dirty branch is reported and refused at `commit`. The password comes from
  `$DOLT_PASSWORD` (configurable via `[backends.dolt] password_env`), never the
  manifest. Requires a Dolt with the `dirty` column on `dolt_branches` for dirty
  detection (older servers report clean).
- **Secrets** are never stored in manifests -- only credential *references* (env
  var / secret names).

## Performance notes

tether is an orchestrator; its cost is the network round-trips it fans out, not
Python.

- **Fan-out uses threads, not asyncio.** Every backend SDK tether wraps (obstore,
  psycopg, pyiceberg, deltalake, lance, lakefs, duckdb, pymysql, `git`) is synchronous, the
  per-object Python work is microseconds, and a thread pool gives the same
  wall-clock concurrency without forcing an async protocol on backend authors.
  `snapshot`, `verify`, and the fork step of `new` all run concurrently.
- **History walks are batched.** `gc` and `verify --all-history` stream every
  manifest at every commit through one `git cat-file --batch` process (jj repos
  are git-backed, so the same plumbing applies), caching trees and blobs by
  object id and verifying each distinct `(system, state, pin)` once. On a repo
  with 200 commits and 5 objects this is ~20-40 ms end to end versus 6-12 s
  with one process per read.
- **Fingerprints are metadata-only.** Local trees use `os.scandir`
  (~4 us/file); a remote object is one `HEAD`; a prefix is one paged `LIST`.
- **Content diffs are metadata-only too.** Each backend reads manifests, logs,
  or catalog tables -- never data -- so `diff --content` costs about one
  fingerprint per changed object (two manifest reads for Icechunk/Lance, one log
  walk for Delta, one catalog query for DuckLake, one prolly-tree diff for
  Dolt). Two stored file listings diff in pure Python at ~50 ms per 100k paths.

## Non-goals

- Being in the data path (tether hands back native handles and steps aside).
- Reimplementing history/undo/branching/sharing (that is the VCS's job).
- Cross-system transactions or a query layer.

## Development

```bash
uv sync --all-extras
uv run ruff format --check . && uv run ruff check .
uv run ty check
uv run pytest
```

## License

Apache-2.0. See [LICENSE](LICENSE).
