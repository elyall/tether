# tether

**jj-style version control for heterogeneous datasets.**

`tether` ties heterogeneous data objects -- plain files, [Icechunk](https://icechunk.io)
repositories, [Neon](https://neon.com) Postgres databases, [Apache Iceberg](https://iceberg.apache.org)
tables, and git/[jj](https://jj-vcs.dev) repositories -- to the commits of an
existing git or jj repository.

A `tether commit` records each object's state and, for systems that support it,
creates a durable native reference (an Icechunk tag, a Neon child branch, a git
tag, an Iceberg tag) so the exact state can be recovered and branched from later.
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
| lakeFS, Quilt, Oxen, DataChain | objects / files | analog of our `file` objects |
| **DVC** (lakeFS-owned) | files/objects in git | closest structural analog; no pin/fork of live systems |
| Dolt, pgGit, Neon, Databricks Lakebase | one Postgres | vendor-bound; no external-object pins |
| Nessie, Bauplan | Iceberg catalog branching | Iceberg-only |
| Icechunk | one Zarr repo | we use it as a backend |
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

Extras: `cli`, `s3`, `icechunk`, `neon`, `iceberg`, `all`. `git`/`jj` must be on
`PATH`.

## Quickstart

```bash
tether init
tether add zarr/imaging --kind icechunk s3://bucket/imaging.zarr.icechunk --write fork
tether add db/rosebud   --kind neon --project-id prj-123 --database neondb --role runner
tether add raw/plate1   --kind file s3://bucket/raw/plate1/     # Observed unless versioned
tether add code         --kind git . --remote origin

tether status                       # fan-out: modified / unpinned / drifted per object
tether commit -m "Baseline imaging + metrics"   # pins, writes manifests, jj/git commit
tether new main                     # fork fresh writable branches off main's pins
tether open db/rosebud              # -> postgresql://...tether.ws.ab12cd34...
tether open zarr/imaging -r main    # read-only handle at main's pinned tag
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
| -- | **verify** (pins still resolve) and **gc** (drop unreferenced pins) |

## Repository layout

```
<dataset-root>/
  tether.toml                 # committed: snapshot.auto, verify.on_status, new.auto_fork,
                              #   default policies, backend options, credential *references*
  .tether/
    .gitignore                # ignores workspace.toml
    objects/<key>.toml        # committed, one per object: kind, locator, policy, state, pin
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
  versioned bucket.
- **Pinnable** (`+ PIN`): can create/delete a durable, GC-proof ref; `commit`
  pins, `verify`/`gc` apply. E.g. an Icechunk tag.
- **Forkable** (`+ FORK`): a writable branch can be created off a pin; `new`
  forks, `open` returns a writable handle. E.g. Icechunk, Neon, Iceberg, git/jj.

Orthogonal flags: `CHEAP_FINGERPRINT`, `RETENTION_BOUND`, `NEEDS_QUIESCENCE`,
`ATOMIC_REF`.

### Compatibility matrix

| Backend | Max tier | State | Pin | Fork | Notable flags / caveats |
| --- | --- | --- | --- | --- | --- |
| `file` (local) | Observed | size, mtime_ns (or dir digest) | -- | -- | `CHEAP`; immutable-by-default, drift is an error |
| `file` (S3 versioned) | Addressable | size, etag, version_id | -- | -- | `CHEAP`; `--file versioned` |
| `icechunk` | Forkable | snapshot_id | tag | branch | `ATOMIC_REF`; tags immutable, excluded from expiry |
| `neon` | Forkable | lsn, next_xid | protected child branch @ parent_lsn | child of pin | `NEEDS_QUIESCENCE`, `RETENTION_BOUND`; no merge/promote, leaf-only gc, quotas |
| `git` / `jj` | Forkable | sha, change_id, dirty | tag (pushed if `remote`) | branch | `CHEAP`, `ATOMIC_REF`; local path only for now |
| `iceberg` | Forkable | snapshot_id, metadata_location | tag (`native`) or recorded id (`record`) | branch | `RETENTION_BOUND`; `record` for S3 Tables (no native ref) |
| `memory` | Forkable | snapshot_id | tag | branch | reference impl for tests |

Command requirements: `status`/`snapshot`/`verify` need `FINGERPRINT`; `commit`
pins `PIN` objects (records `ADDRESSABLE`, records + warns for Observed, or fails
with `--strict`); `new` forks `FORK` objects; `gc` applies to `PIN` objects.

## Writing a backend

Implement the `ObjectBackend` protocol (`tether.backends.base`): declare
`kind` and `capabilities`, then `identity`, `fingerprint`, `pin`, `unpin`,
`list_pins`, `verify`, `fork`, `delete_working_ref`, and `open`. A backend whose
tier varies per object (like `file`) can override
`effective_capabilities(locator, policy)`. Register it with `register_backend`.

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
  drifted. S3 versioning promotes them to Addressable.
- **Iceberg catalog variance.** S3 Tables disables maintenance when user refs
  exist; use `--pin record` there.
- **Secrets** are never stored in manifests -- only credential *references* (env
  var / secret names).

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
