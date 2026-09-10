# tether

[![PyPI](https://img.shields.io/pypi/v/tether-vcs)](https://pypi.org/project/tether-vcs/)
[![Python](https://img.shields.io/pypi/pyversions/tether-vcs)](https://pypi.org/project/tether-vcs/)
[![CI](https://github.com/elyall/tether/actions/workflows/ci.yml/badge.svg)](https://github.com/elyall/tether/actions/workflows/ci.yml)
[![Docs](https://github.com/elyall/tether/actions/workflows/docs.yml/badge.svg)](https://evanlyall.com/tether/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/elyall/tether/blob/main/LICENSE)

**Version control (fingerprints, pins, & forks) for heterogeneous datasets.**

> Status: alpha. See the [changelog](https://github.com/elyall/tether/blob/main/CHANGELOG.md).
>
> State: vibe coded with Claude Fable 5.1. **USE AT YOUR OWN RISK.**

Think of `tether` as **DVC for *branchable* systems**: like DVC it commits small
manifests into your git/jj repo, but where DVC only fingerprints files, tether
also *pins* and *forks* live systems. Every backend is fingerprinted; what
else tether can do with each depends on what the system offers:

| Backend | Recover | Pin | Fork | Promote | Merge | Diff | History |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| local files and directories; object-store prefixes (S3, GCS, Azure) | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ |
| single object-store objects with versioning enabled | 🟡 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| git / [jj](https://jj-vcs.dev) repositories | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| [Icechunk](https://icechunk.io) repositories | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ |
| [Neon](https://neon.com) Postgres databases | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| [Apache Iceberg](https://iceberg.apache.org) tables | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ |
| [Delta Lake](https://delta.io) tables | 🟡 | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |
| [Lance](https://lance.org) datasets | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ |
| [lakeFS](https://lakefs.io) repositories *(experimental)* | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| [DuckLake](https://ducklake.select) catalogs *(experimental)* | 🟡 | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |
| [Dolt](https://www.dolthub.com) databases *(experimental)* | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

**Recover**: can a committed state be opened again later? ✅ held by a pin;
🟡 reopenable by version id for as long as the system keeps that version
(object versioning, Delta/DuckLake snapshots) -- tether records it but has no
ref of its own to hold it; ❌ tether can only tell you it changed. **Pin**: a
durable native ref (tag, protected branch) tether creates at commit time.
**Fork**: a writable branch off a pin for `tether new`. **Promote**:
fast-forward the base branch to the fork; **Merge**: a native three-way merge
when it is not a fast-forward. **Diff**: `tether diff --content` describes
what changed inside the object. **History**: `tether log` lists the object's
own snapshots/versions/commits. The full matrix -- state fields, flags,
per-backend caveats -- is in the
[backends guide](https://evanlyall.com/tether/user-guide/backends.html).

Documentation: <https://evanlyall.com/tether/> --
[getting started](https://evanlyall.com/tether/user-guide/getting-started.html),
[concepts](https://evanlyall.com/tether/user-guide/concepts.html),
[pinning](https://evanlyall.com/tether/user-guide/pinning.html),
[branching and writing](https://evanlyall.com/tether/user-guide/branching-and-writing.html),
[reclaiming storage](https://evanlyall.com/tether/user-guide/reclaiming-storage.html),
[use cases](https://evanlyall.com/tether/user-guide/use-cases.html),
[CLI](https://evanlyall.com/tether/user-guide/cli.html),
[configuration](https://evanlyall.com/tether/user-guide/configuration.html),
[backends](https://evanlyall.com/tether/user-guide/backends.html),
[registries and SQL](https://evanlyall.com/tether/user-guide/registries-and-sql.html),
[writing a backend](https://evanlyall.com/tether/user-guide/extending.html),
[caveats and performance](https://evanlyall.com/tether/user-guide/caveats-and-performance.html),
plus the generated API and CLI reference.

## Why this exists (prior art)

Every existing tool versions a single layer:

| Tool | Scope | Relationship to tether |
| --- | --- | --- |
| lakeFS, Quilt, Oxen, DataChain | objects / files | analog of our `file` objects; lakeFS is also a backend |
| **DVC** (lakeFS-owned) | files/objects in git | closest structural analog; no pin/fork of live systems |
| Dolt, pgGit, Neon, Databricks Lakebase | one database | vendor-bound; no external-object pins; Dolt and Neon are backends |
| Nessie, Bauplan | Iceberg catalog branching | Iceberg-only |
| Icechunk, Lance, Delta, DuckLake | one dataset / table / catalog | we use them as backends |
| [Yggdrasil](https://github.com/replikativ/yggdrasil) (replikativ) | cross-system: Clojure protocol stack (snapshot / branch / merge / watch) over Git, ZFS, Btrfs, IPFS, Iceberg, Datahike, lakeFS, Dolt, Podman, with an HLC-coordinated workspace | the closest conceptual sibling; not adoptable from Python (JVM library; its own README marks the Python binding as unmaintained since the initial release) |
| Dagster observable assets | staleness detection | analog of our snapshot/drift step; no branching |

Nothing provides unified version control *across* files + Icechunk + Postgres +
Iceberg with pinning and forking. So tether borrows jj's working-copy model
(fingerprint, snapshot on every command, stale-working-copy detection), DVC's
manifests-in-VCS layout, and [Yggdrasil](https://github.com/replikativ/yggdrasil)'s
observe-then-record shape (a workspace that watches independent systems and
records their snapshots, rather than a store that holds the data).

## Install

```bash
pip install tether-vcs[cli]                 # core + CLI
pip install tether-vcs[cli,icechunk,neon]   # add backends you need
pip install tether-vcs[all]                 # everything
```

Extras: `cli`, `objectstore` (S3/GCS/Azure for `file`; `s3`/`gcs`/`azure` are
aliases), `icechunk`, `neon`, `iceberg`, `delta`, `lance`, `lakefs`, `ducklake`,
`dolt`, `postgres` (`tether publish` / `import` against Postgres), `all`.
`git`/`jj` must be on `PATH`.

## Quickstart

tether lives *inside* a git or jj repository and commits small manifests
there; that repository's history, branches, and workspaces are the dataset's
too. Nothing is contacted until you ask for a state.

```bash
jj git init my-dataset && cd my-dataset      # or: git init my-dataset
tether init
tether add zarr/imaging --kind icechunk s3://bucket/imaging.icechunk
tether add db/metrics   --kind neon --project-id prj-123 --database neondb --role runner
tether add raw/plate1   --kind file s3://bucket/raw/plate1/

tether status                       # fingerprint every object: clean / modified / drifted
tether commit -m "Baseline"         # pin each object natively, write the manifests, jj/git commit
tether new main                     # start working: a writable branch per object, created on first write
tether open db/metrics              # postgresql://... on this workspace's fork
tether commit -m "Relabel plate1"   # pins the forks
tether promote                      # move each system's main to the fork: fast-forward, native merge, or refuse with a recipe
tether verify --all-history         # every pin any commit ever named still resolves
```

```python
from tether import Repo

repo = Repo.find(".")
h = repo.open("zarr/imaging")  # writable IcechunkHandle on this workspace's fork
ro = repo.open(
    "zarr/imaging", rev="main"
)  # read-only at main's pin; TETHER_REV=<rev> makes this the default
```

Every command that writes to a store takes `--dry-run` (and `--plan FILE` /
`--from-plan FILE`) so the writes can be reviewed first. The
[getting started guide](https://evanlyall.com/tether/user-guide/getting-started.html)
walks through this with output; the
[CLI guide](https://evanlyall.com/tether/user-guide/cli.html) has every
command.

## jj or tether?

A tether command exists where an operation has two halves -- one in the VCS,
one in the stores -- that must happen together: `commit` (pin, then commit),
`new` (move the working copy, then decide working branches), `restore`,
`abandon`, `forget-workspace`, and `undo` / `repair` for what tether itself
did. Everything that only touches files and history -- describe, squash,
rebase, merge, bookmarks, push, `jj undo` of a non-tether operation -- is the
VCS's, and tether notices what it needs to: a moved working copy makes objects
stale, a vanished dataset commit shows up in `status`. The
[concepts guide](https://evanlyall.com/tether/user-guide/concepts.html#jj-or-tether)
has the table.

## Non-goals

- Sitting in the data path: `open` hands back the system's native handle and
  steps aside.
- Running between commands: no daemon, no watcher; states are compared when
  a command asks.
- Reimplementing history, branching, or sharing: that is the VCS's job.
  `tether ops` / `undo` / `repair` cover only what tether did to the stores,
  which the VCS cannot see.
- Cross-system transactions or a query layer.

## Development

```bash
uv sync --all-extras
uv run ruff format --check . && uv run ruff check .
uv run ty check
uv run pytest
```

The `publish` / `import` tests start an ephemeral PostgreSQL cluster through
`pytest-postgresql`; they need `pg_ctl` on `PATH` or a Homebrew / Debian
install (`brew install postgresql@16`; GitHub's Ubuntu runners ship it) and
skip otherwise.

## License

Apache-2.0. See [LICENSE](LICENSE).
