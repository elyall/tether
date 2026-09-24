# tether

[![PyPI](https://img.shields.io/pypi/v/tether-vcs)](https://pypi.org/project/tether-vcs/)
[![Python](https://img.shields.io/pypi/pyversions/tether-vcs)](https://pypi.org/project/tether-vcs/)
[![CI](https://github.com/elyall/tether/actions/workflows/ci.yml/badge.svg)](https://github.com/elyall/tether/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/elyall/tether/graph/badge.svg)](https://codecov.io/gh/elyall/tether)
[![Docs](https://github.com/elyall/tether/actions/workflows/docs.yml/badge.svg)](https://evanlyall.com/tether/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/elyall/tether/blob/main/LICENSE)

**Version control (fingerprints, pins, & forks) for heterogeneous datasets.**

A dataset rarely lives in one place: an Icechunk repository, a directory of raw
files, a Postgres database, an Iceberg table, and the code that produced them.
tether tracks all of them from one git or jj repository. Each commit records
every object's exact state -- and, where the system allows it, *pins* that
state with a native ref so it stays readable, and *forks* a writable branch
off it when you want to change things without touching `main`.

Think of it as **DVC for branchable systems**: like DVC it commits small
manifests to your repository, but where DVC only fingerprints files, tether
also pins and forks live systems.

> Status: pre-release (`0.1.0` betas).
> [ROADMAP.md](https://github.com/elyall/tether/blob/main/ROADMAP.md) says
> what has run against real services, what is still experimental, and what
> blocks 0.1.0; the
> [changelog](https://github.com/elyall/tether/blob/main/CHANGELOG.md) says
> what changed. Vibe coded with Claude Fable 5.1. **USE AT YOUR OWN RISK.**

## Install

```bash
pip install "tether-vcs[cli]"                 # core + CLI
pip install "tether-vcs[cli,icechunk,lance]"  # add the backends you use
pip install "tether-vcs[all]"                 # everything
```

The distribution is `tether-vcs`; the package you import and the command you
run are both `tether`. Python 3.11 or newer; `git` 2.38+ and/or `jj` 0.43+
on `PATH` (see the
[CLI guide](https://evanlyall.com/tether/user-guide/cli.html#versions-and-environment)).
Extras: `objectstore` (S3/GCS/Azure for `file`), `icechunk`, `neon`,
`iceberg`, `delta`, `lance`, `ducklake`, `dolt`, `postgres`, `all`.

**Windows:** commands that write are refused, since the locks need `fcntl`;
`status`, `verify`, `diff`, `log`, `ops` and `gc --dry-run` work, and a
default `open` is read-only. Use WSL to write.

## Quickstart

tether lives *inside* a git or jj repository. Nothing below needs a cloud
account: point it at an Icechunk repository and a directory you have on disk.

```bash
jj git init my-dataset && cd my-dataset      # or: git init my-dataset
tether init
tether add zarr/imaging --kind icechunk ../data/imaging.icechunk
tether add raw/plate1   --kind file     ../data/raw/plate1/

tether status                       # new / clean / modified per object
tether commit -m "Baseline"         # pin the Icechunk snapshot; record the directory; jj/git commit
```

```
  pinned zarr/imaging -> tether.4e9503fc.73e341e966c83d38
  recorded raw/plate1 (not recoverable)
committed b55326ca55a0
```

That commit id names the exact state of every object, forever:
`tether open zarr/imaging --rev b55326ca55a0` opens the pinned snapshot
read-only, and `TETHER_REV=b55326ca55a0 python report.py` makes every
`repo.open()` in a script do the same.

To change data without touching `main`, work on a bookmark:

```bash
tether new -b relabel               # one branch per system, created on first write
python relabel.py                   # writes through repo.open("zarr/imaging")
tether commit -m "Relabel plate1"   # pin the branch head; move the bookmark
tether promote                      # fast-forward main in every system, then move the main bookmark
tether verify --all-history         # every pin any commit ever named still resolves
```

```python
from tether import Repo

repo = Repo.find(".")
h = repo.open("zarr/imaging")  # writable IcechunkHandle on this bookmark's branch
ro = repo.open("zarr/imaging", rev="main")  # read-only at main's pin
```

The commands that plan before they write -- `commit`, `new`, `restore`,
`promote`, `gc`, `drop`, `forget-workspace`, `import`, `repair`, `upgrade` --
take `--dry-run` (and `--plan FILE` / `--from-plan FILE`) so the store writes
can be reviewed first. The
[Getting Started guide](https://evanlyall.com/tether/user-guide/getting-started.html)
runs this walkthrough with full output; the
[user guide](https://evanlyall.com/tether/user-guide/) takes it from there.

## How it works

- **A dataset is a git/jj repository** with one small manifest per object.
  History, branching, workspaces, and sharing are the VCS's.
- **Commits hold pins.** Each dataset commit records, per object, an exact
  state and -- where the system allows -- a *pin*: a native, GC-proof ref (an
  Icechunk tag, a Neon branch, a git tag) that holds that state.
  `tether open KEY --rev C` reads it back.
- **Bookmarks hold branches.** The trunk bookmark (`main`) stands for every
  object's upstream branch; working on it writes there. Any other bookmark
  stands for one branch per system, `tether.ws.<dataset>.<bookmark>`, forked
  from the pins where it started and created on the first write.
- **Three verbs.** `commit` pins the bookmark's branch heads and moves the
  bookmark. `promote` moves each system's upstream branch to those heads,
  then moves the `main` bookmark. `pull` reads what upstream has now and
  commits it.
- **tether is never in the data path.** `open` returns the system's native
  handle -- an Icechunk session, a Postgres URL, a `DeltaTable` -- and steps
  aside.

What the VCS cannot see -- what tether did to the *stores* -- is in tether's
own operation log (`tether ops`, `tether undo`, `tether repair`).

## What each backend can do

Every backend is fingerprinted; the rest depends on what the system offers.

| Backend | Recover | Pin | Fork | Promote | Merge | Diff | History |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| local files and directories; object-store prefixes (S3, GCS, Azure) | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ |
| single object-store objects with versioning enabled | 🟡 | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ |
| git / [jj](https://jj-vcs.dev) repositories | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| [Icechunk](https://icechunk.io) repositories | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ |
| [Lance](https://lance.org) datasets | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ |
| [Delta Lake](https://delta.io) tables | 🟡 | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |
| [Neon](https://neon.com) Postgres databases *(experimental)* | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| [Apache Iceberg](https://iceberg.apache.org) tables *(experimental)* | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ |
| [DuckLake](https://ducklake.select) catalogs *(experimental)* | 🟡 | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |
| [Dolt](https://www.dolthub.com) databases *(experimental)* | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

**Recover**: can a committed state be opened again later? ✅ held by a pin;
🟡 re-openable by version id for as long as the system keeps that version;
❌ tether can only tell you it changed. **Pin**: a durable native ref created
at commit time. **Fork**: a writable branch off a pin for `tether new`.
**Promote** / **Merge**: fast-forward, or three-way merge, the upstream branch
to the fork. **Diff**: `tether diff --content` describes what changed inside
the object. **History**: `tether log` lists the object's own snapshots.
*Experimental* backends have run against a fake of the service, not the
service itself. Stable ones run their full lifecycle in CI; for `file` and
Icechunk that means local storage, and their S3, GCS and Azure paths have not
run against a cloud service yet (see the
[roadmap](https://github.com/elyall/tether/blob/main/ROADMAP.md)). The full
matrix -- state fields, flags, per-backend caveats -- is in the
[backends guide](https://evanlyall.com/tether/user-guide/backends.html).

## jj or tether?

A tether command exists where an operation has two halves -- one in the VCS,
one in the stores -- that must happen together: `commit`, `new`, `pull`,
`promote`, `restore`, `abandon`, `drop`, `forget-workspace`, and `undo` /
`repair` for what tether itself did. Everything that only touches files and
history -- describe, squash, rebase, push -- is the VCS's, and tether notices
what it needs to: a bookmark deleted, renamed, or moved by hand shows up in
`status` with what to do. The
[concepts guide](https://evanlyall.com/tether/user-guide/concepts.html#jj-or-tether)
has the table.

## Non-goals

- Sitting in the data path: `open` hands back the system's native handle and
  steps aside.
- Running between commands: no daemon, no watcher; states are compared when
  a command asks.
- Reimplementing history, branching, or sharing: that is the VCS's job.
  `tether ops` / `undo` / `repair` cover only what tether did to the stores.
- Cross-system transactions or a query layer.

## Documentation

<https://evanlyall.com/tether/> --
[getting started](https://evanlyall.com/tether/user-guide/getting-started.html),
[concepts](https://evanlyall.com/tether/user-guide/concepts.html),
[worked examples](https://evanlyall.com/tether/user-guide/use-cases.html),
[CLI guide](https://evanlyall.com/tether/user-guide/cli.html),
[backends](https://evanlyall.com/tether/user-guide/backends.html),
[sharing and CI](https://evanlyall.com/tether/user-guide/sharing-and-ci.html),
[troubleshooting](https://evanlyall.com/tether/user-guide/troubleshooting.html),
and the generated API and CLI reference.

## Why this exists

Every existing tool versions a single layer:

| Tool | Scope | Relationship to tether |
| --- | --- | --- |
| **DVC** (lakeFS-owned) | files/objects in git | closest structural analog; no pin/fork of live systems |
| lakeFS, Quilt, Oxen, DataChain | objects / files | analog of our `file` objects |
| Dolt, pgGit, Neon, Databricks Lakebase | one database | vendor-bound; no external-object pins; Dolt and Neon are backends |
| Nessie, Bauplan | Iceberg catalog branching | Iceberg-only |
| Icechunk, Lance, Delta, DuckLake | one dataset / table / catalog | we use them as backends |
| [Yggdrasil](https://github.com/replikativ/yggdrasil) | cross-system snapshot / branch / merge over Git, ZFS, IPFS, Iceberg, lakeFS, Dolt, ... | the closest conceptual sibling; a JVM library, not adoptable from Python |
| Dagster observable assets | staleness detection | analog of our snapshot/drift step; no branching |

Nothing provides unified version control *across* files + Icechunk + Postgres +
Iceberg with pinning and forking. So tether borrows jj's working-copy and
operation-log ideas (`new REV`, stale-working-copy detection, `undo`), DVC's
manifests-in-VCS layout, and Yggdrasil's observe-then-record shape: a
workspace that watches independent systems and records their snapshots,
rather than a store that holds the data.

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
skip otherwise. The use-cases story (`tests/test_use_cases.py`) regenerates
the guide's command output with `TETHER_UPDATE_DOCS=1`.

## License

Apache-2.0. See [LICENSE](https://github.com/elyall/tether/blob/main/LICENSE).
