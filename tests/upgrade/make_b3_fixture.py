"""Regenerate `tests/upgrade/fixtures/b3/` with the released tether-vcs 0.1.0b3.

    python tests/upgrade/make_b3_fixture.py        # needs `uv` and the network

Installs 0.1.0b3 into a throwaway venv (Python 3.11 and the library versions
the lock pins there, so every Python CI runs can read the stores), runs
`SCENARIO` with it, and writes what the dataset left behind: its history as
a `git fast-export` stream (with the marks naming each commit), the
untracked files of its checkout, the indexes beside its repository lock, and
the stores its manifests name. Absolute paths in all of it start with the
directory recorded in `ROOT`; the test relocates them
(`test_upgrade_b3._materialize`).
"""

from __future__ import annotations

import gzip
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "fixtures" / "b3"

PACKAGES = [
    "tether-vcs[cli,objectstore,icechunk,lance,ducklake]==0.1.0b3",
    "icechunk==1.1.21",
    "zarr==3.1.6",
    "pylance==11.0.0",
    "duckdb==1.5.5",
    "pyarrow",
]

SCENARIO = r"""
import os, subprocess, sys
from pathlib import Path

import duckdb, icechunk as ic, lance, pyarrow as pa, zarr
from tether import Repo

root = Path(sys.argv[1])
stores, home, ds = root / "stores", root / "home", root / "ds"
for d in (stores, home, ds):
    d.mkdir()
(root / "stores-link").symlink_to("stores")  # a symlinked parent

def ice_write(value):
    repo = ic.Repository.open(ic.local_filesystem_storage(str(stores / "ice.icechunk")))
    session = repo.writable_session("main")
    zarr.open_group(store=session.store, mode="a").attrs["v"] = value
    session.commit(f"v={value}")

repo = ic.Repository.create(ic.local_filesystem_storage(str(stores / "ice.icechunk")))
session = repo.writable_session("main")
zarr.create_group(store=session.store).attrs["v"] = 0
session.commit("init")
lance.write_dataset(pa.table({"cell": [1, 2]}), str(stores / "cells.lance"))
plates = stores / "plates"
(plates / "sub").mkdir(parents=True)
(plates / "a.csv").write_text("a\n")
(plates / "sub" / "c.csv").write_text("c\n")
(plates / "latest").symlink_to("sub")        # a directory symlink
(plates / "gone").symlink_to("missing.csv")  # a dangling one
(stores / "raw").mkdir()
(stores / "raw" / "x.bin").write_bytes(b"x")
for path in (home / "lake.ducklake", ds / "rel.ducklake"):
    con = duckdb.connect()
    con.execute("INSTALL ducklake; LOAD ducklake")
    con.execute(f"ATTACH 'ducklake:{path}' AS l")
    con.execute("CREATE TABLE l.t (a INTEGER)")
    con.execute("DETACH l")
    con.close()

subprocess.run(["git", "init", "-q", "-b", "main"], cwd=ds, check=True)
os.chdir(ds)
repo = Repo.init(ds)
repo.add("ice", "icechunk", {"uri": f"file://{stores}/ice.icechunk"})
repo.add("cells", "lance", {"uri": f"{root}/stores-link/cells.lance/"})
repo.add("plates", "file", {"uri": str(plates)})
repo.add("raw", "file", {"uri": f"file://{stores}/raw/"})
repo.add("made", "file", {"uri": f"{stores}/made/"}, create=True)
repo.add("lake", "ducklake", {"metadata": "ducklake:~/lake.ducklake"})
repo.add("lake_rel", "ducklake", {"metadata": "ducklake:rel.ducklake"})
repo.commit("baseline")
ice_write(1)
repo.commit("ice v1")
repo.new(bookmark="feat")
handle = repo.open("cells")
lance.write_dataset(pa.table({"cell": [3]}), handle.dataset, mode="append")
repo.commit("cells on feat")
"""


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp).resolve()
        root = work / "root"
        root.mkdir()
        venv = work / "venv"
        subprocess.run(["uv", "venv", "-q", "-p", "3.11", str(venv)], check=True)
        python = venv / "bin" / "python"
        subprocess.run(
            ["uv", "pip", "install", "-q", "-p", str(python), *PACKAGES], check=True
        )
        env: dict[str, str] = {
            **os.environ,
            "HOME": str(root / "home"),
            "GIT_AUTHOR_NAME": "tether b3",
            "GIT_AUTHOR_EMAIL": "b3@tether.dev",
            "GIT_COMMITTER_NAME": "tether b3",
            "GIT_COMMITTER_EMAIL": "b3@tether.dev",
            "GIT_AUTHOR_DATE": "2026-09-18T12:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-09-18T12:00:00+00:00",
        }
        env.pop("JJ_CONFIG", None)
        subprocess.run(
            [str(python), "-c", SCENARIO, str(root)], check=True, env=env, cwd=work
        )
        ds = root / "ds"
        if OUT.exists():
            shutil.rmtree(OUT)
        (OUT / "checkout" / ".tether").mkdir(parents=True)
        (OUT / "shared").mkdir()
        with (OUT / "history.fast-export").open("wb") as fh:
            subprocess.run(
                [
                    "git",
                    "fast-export",
                    "--all",
                    "--export-marks",
                    str(OUT / "marks"),
                ],
                cwd=ds,
                check=True,
                stdout=fh,
            )
        for name in ("workspace.toml", "ops.jsonl"):
            shutil.copy2(ds / ".tether" / name, OUT / "checkout" / ".tether" / name)
        for name in ("tether-touched.jsonl", "tether-created.jsonl"):
            shutil.copy2(ds / ".git" / name, OUT / "shared" / name)
        shutil.copytree(root / "stores", OUT / "stores", symlinks=True)
        (OUT / "stores-link").symlink_to("stores")
        (OUT / "home").mkdir()
        # DuckDB catalogs are megabytes of empty pages; kilobytes gzipped.
        for src, dst in (
            (ds / "rel.ducklake", OUT / "checkout" / "rel.ducklake.gz"),
            (root / "home" / "lake.ducklake", OUT / "home" / "lake.ducklake.gz"),
        ):
            dst.write_bytes(gzip.compress(src.read_bytes(), mtime=0))
        (OUT / "ROOT").write_text(f"{root}\n", encoding="utf-8")
    print(f"wrote {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
