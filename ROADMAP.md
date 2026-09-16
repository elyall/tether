# Road to 0.1.0

What has to happen between the 0.1.0 betas and the first release that is
not one. Two lists: what has only ever run against a stand-in and needs a
real resource under it, and what is experimental today and has to either
graduate or be cut before 0.1.0. Everything here is a decision or a test,
not a feature.

The rule the lists follow: **stable** means the full lifecycle -- register,
commit, fork, write, commit, promote, gc -- has run against the real system,
in CI or by hand with the result recorded; **experimental** means it has run
against a fake, a local stand-in, or not at all. `tether backends` prints the
label per kind; `add`, `--create`, and `gc --delete-stores` print the note.

## 1. Test against actual resources

Each line names the resource, what to run, and what would move as a result.
The engine tests and conformance suites cover the logic; these cover the
service. Run them once by hand before the release and, where a credential
can live in CI, keep them running.

| Resource | What to run | What it decides |
| --- | --- | --- |
| **S3 bucket** (any region; versioning on for one prefix) | `file` prefix and versioned-object objects: `add`, `commit`, `status --snapshot`, `verify`, `open --rev`, drift after an overwrite. `icechunk` repository at `s3://`: the story of section 2 (fork, write, promote, gc). `add --create` / `gc --delete-stores` on an `s3://` prefix -- the `delete_store` arm has only run against an obstore in-memory store. | `file`'s Addressable tier for versioned objects is real; Icechunk-on-S3 is real; store-lifecycle graduation criterion 1. |
| **AWS credentials via `secrets.toml`** (a `profile`, a `role_arn`, literal keys, an `endpoint_url` to a MinIO) | Two Icechunk objects in two accounts pinned by one `commit` (the configuration guide's "two identities" section). `tether open` and `verify` with `region` overrides. | The per-object credential layer is real (it is core today; the reviews asked twice whether it should be). |
| **GCS and Azure containers** | `file` objects and prefixes through obstore (`gs://`, `az://`); a versioned object on each. | Whether the `file` backend's object-store claims hold beyond S3, or the docs narrow to S3. |
| **Neon project** (a free tier is enough) | The `neon` lifecycle against the control plane: `add`, `commit` (protected-branch pin), `new` + writable open (branch), `promote` refusal path, `gc --prune-bookmarks` keeping a `BRANCH_IS_STORAGE` branch, `--force-prune` deleting one, `verify` with a suspended compute. The lineage / xid caveat in the performance guide, observed. | `neon` graduates or stays experimental with a documented reason. |
| **Iceberg catalog** (a REST catalog -- Lakekeeper or Polaris in a container -- and one object-store warehouse) | The `iceberg` lifecycle: snapshot pins, branch forks, `promote` fast-forward, `log`, `add --pick`, retention expiring a recorded state. | `iceberg` graduates; the `RETENTION_BOUND` claim is observed rather than asserted. |
| **lakeFS server** (container) | The `lakefs` lifecycle including `merge` conflicts and `MergeConflict`. | `lakefs` graduates. |
| **Dolt server** (container, MySQL protocol) | The `dolt` lifecycle including `merge` and `HASHOF` pins. | `dolt` graduates. |
| **DuckLake catalog** (DuckDB with a Postgres or SQLite catalog and an object-store `data_path`) | Addressable reads by snapshot id; `log`; retention. | `ducklake` graduates. |
| **Postgres** (already in CI through `pytest-postgresql`) | Nothing new: the registry publish/import path runs against it. Run once against a managed Postgres (RDS, Neon) to catch permission and extension differences. | Registry graduation, together with a schema decision (below). |
| **A hosted git remote and a hosted jj remote** (GitHub; a jj-capable forge or a bare repository over SSH) | Two clones of one dataset: the two-clone scenario in `tests/test_store_lifecycle.py` against a real remote, including a `forget-workspace` on one side; `gc` in each clone before and after fetching; a bookmark pushed from one side and abandoned on the other. `tether abandon` under `git` with a remote-tracking branch. | Store-lifecycle graduation criterion 2; whether "gc only knows what this clone has fetched" needs more than documentation. |
| **A large local tree** (10^6 files) and **a large prefix** | `status` first and second fingerprint; `commit` with a listing; `diff --content`. Numbers into the performance guide. | The performance guide's claims are measured, not estimated. |

## 2. Experimental today; graduate or cut before 0.1.0

| Feature | Where | Graduates when | If it does not |
| --- | --- | --- | --- |
| **`neon`, `iceberg`, `lakefs`, `dolt`, `ducklake` backends** | `tether.experimental.backends` | The lifecycle above has run against the real service and the conformance suite passes against it (not a fake). Move the module to `tether/backends/`, set `MATURITY = "stable"`, update the backends guide and README. Nothing users type changes. | Ship at 0.1.0 as experimental with the note, or drop the kind from `known_kinds` and the extra from `pyproject.toml`. Each kind is a separate decision. |
| **Registry** (`export`, `publish`, `import`; `tether.experimental.registry`) | `tether.experimental.registry` | The export schema is declared frozen (a `schema_version` in the bundle and a documented compatibility promise), one round trip has run against a managed Postgres, and `import --sync` has been used on a real dataset. Move to `tether.registry`. | Stays experimental past 0.1.0 with a stated horizon. It does not block the release: nothing else depends on it. |
| **Store lifecycle** (`add --create`, `Repo.create`, `gc --delete-stores`, `--store`; `tether.experimental.lifecycle`) | `tether.experimental.lifecycle`; `Capability.CREATE` in `tether.backends.base` | The five criteria in the module docstring: S3 `delete_store` against a real bucket; the two-clone scenario against a real remote; one release cycle with no data-loss report; a second Forkable backend with `CREATE`; a decision on the touched index. Move the module to `tether/repo/_lifecycle.py`, drop the notes, re-home the CHANGELOG entry. | Stays experimental at 0.1.0. `Capability.CREATE` stays declared (it is a per-backend fact with conformance coverage). |
| **Per-URI and per-object credentials** (`secrets.toml` `[uris.*]` / `[objects.*]`; `ObjectBackend.configure_secrets` / `secrets_for`) | core (`tether.backends.base`, `Repo.backend_for`); the reviews asked twice whether it belongs in experimental | The two-identity run in section 1 has happened against real accounts (a `profile`, a `role_arn`, literal keys, an `endpoint_url`) for Icechunk and `file`, and no second resolution order was needed. Then it is simply core, and the configuration guide's "advanced" section is the contract. | Moves to `tether.experimental.credentials` behind `configure_secrets`, with the kind-level `[backends.<kind>]` secrets staying core; nothing users write in `secrets.toml` changes. |
| **The alpha upgrade path** (`tether.upgrade`) | `tether.upgrade` | Does not graduate: it is *removed* at 0.1.0 as announced. Before removal, publish the last beta, confirm `tether upgrade` from every alpha format against it once more, and write the "install the last beta, upgrade, reinstall" message into the open-time error. | -- |
| **`tether abandon`** | core (`Repo.abandon`) | Decided: it stays as the surgical form -- commits off a bookmark you are keeping, `--gc` to release their pins in one action -- while `drop` takes the whole line. The remaining question is only whether it behaves under `git` with remote-tracking branches (the two-clone and remote tests above). | Kept; documented next to `drop` as the pair they are. |

## 3. Before tagging 0.1.0

- Every backend the README matrix lists is either stable or marked
  *(experimental)* in the row; the matrix and `tether backends` agree.
- `tether.upgrade` is gone; `CONFIG_VERSION` is frozen for 0.1.x; a dataset
  from the last beta opens without `upgrade`.
- The performance guide's numbers come from section 1's runs.
- The changelog's `0.1.0` entry lists what graduated, what stayed
  experimental, and what was removed, in those words.
