"""A dataset the released 0.1.0b3 made, brought to 0.1.0b4 by `tether upgrade`.

`fixtures/b3/` is what 0.1.0b3 left behind (`make_b3_fixture.py` regenerates
it): an Icechunk store named `file://...`, a Lance store through a symlinked
parent with a trailing slash and a commit on a working branch, an immutable
directory holding a directory symlink and a dangling one, a directory named
`file://.../`, a created store named with a trailing slash, and DuckLake
catalogs named `ducklake:~/...` and `ducklake:rel.ducklake`. `_materialize`
puts it anywhere; nothing here needs the network.
"""

from __future__ import annotations

import gzip
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tether.errors import ConfigError
from tether.manifest import CONFIG_VERSION, ObjectManifest, listing_name
from tether.oplog import read_jsonl
from tether.repo import Repo
from tether.vcs import detect_vcs

pytest.importorskip("icechunk")
pytest.importorskip("lance")

FIXTURE = Path(__file__).parent / "fixtures" / "b3"


def _relocate(stream: bytes, old: str, new: str) -> bytes:
    """A `git fast-export` stream with `old` replaced by `new` in every blob
    (each `data` length recomputed), and every `file` listing renamed to the
    name 0.1.0b3 would have given it there: it named listings from the URI
    as written, so moving the paths renames them."""
    tokens: list[tuple[bool, bytes]] = []  # (is data, bytes)
    i = 0
    while i < len(stream):
        nl = stream.index(b"\n", i)
        line, i = stream[i : nl + 1], nl + 1
        if line.startswith(b"data "):
            n = int(line[5:])
            tokens.append((True, stream[i : i + n]))
            i += n
        else:
            tokens.append((False, line))
    renames: dict[bytes, bytes] = {}
    for is_data, body in tokens:
        if not (is_data and body.startswith(b"key = ")):
            continue
        m = ObjectManifest.from_toml(body.decode())
        if m.kind == "file" and m.state is not None:
            uri = str(m.locator["uri"])
            was = listing_name("file", {"uri": uri}, m.state)
            now = listing_name("file", {"uri": uri.replace(old, new)}, m.state)
            renames[was.encode()] = now.encode()
    out = bytearray()
    for is_data, body in tokens:
        if is_data:
            body = body.replace(old.encode(), new.encode())
            out += b"data %d\n" % len(body) + body
        else:
            for was, now in renames.items():
                body = body.replace(was, now)
            out += body
    return bytes(out)


def _marks(path: Path) -> dict[str, str]:
    return dict(line.split() for line in path.read_text().splitlines() if line)


def _materialize(dest: Path, vcs: str) -> Path:
    """The fixture's dataset, stores and all, at `dest` (resolved): a fresh
    git (or colocated jj) repository with 0.1.0b3's history, checked out on
    the bookmark its workspace was on. Returns the dataset root."""
    root = dest.resolve()
    old = (FIXTURE / "ROOT").read_text().strip()
    new = str(root)
    shutil.copytree(FIXTURE / "stores", root / "stores", symlinks=True)
    (root / "stores-link").symlink_to("stores")
    (root / "home").mkdir()
    (root / "home" / "lake.ducklake").write_bytes(
        gzip.decompress((FIXTURE / "home" / "lake.ducklake.gz").read_bytes())
    )
    ds = root / "ds"
    ds.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=ds, check=True)
    stream = _relocate((FIXTURE / "history.fast-export").read_bytes(), old, new)
    marks = root / "marks"
    subprocess.run(
        ["git", "fast-import", "--quiet", f"--export-marks={marks}"],
        cwd=ds,
        input=stream,
        check=True,
    )
    subprocess.run(["git", "checkout", "-q", "-f", "feat"], cwd=ds, check=True)
    commits = {
        sha: _marks(marks)[mark] for mark, sha in _marks(FIXTURE / "marks").items()
    }
    for name in ("workspace.toml", "ops.jsonl"):
        text = (FIXTURE / "checkout" / ".tether" / name).read_text().replace(old, new)
        for was, now in commits.items():
            text = text.replace(was, now)
        (ds / ".tether" / name).write_text(text)
    if vcs == "jj":
        subprocess.run(
            ["jj", "git", "init", "--colocate"], cwd=ds, check=True, capture_output=True
        )
    shared = detect_vcs(ds).shared_dir()
    for name in ("tether-touched.jsonl", "tether-created.jsonl"):
        text = (FIXTURE / "shared" / name).read_text().replace(old, new)
        (shared / name).write_text(text)
    (ds / "rel.ducklake").write_bytes(
        gzip.decompress((FIXTURE / "checkout" / "rel.ducklake.gz").read_bytes())
    )
    return ds


def _ducklake_loads() -> bool:
    """Whether DuckLake can attach a catalog here without the network."""
    try:
        import duckdb

        con = duckdb.connect()
        con.execute("LOAD ducklake")
        con.close()
    except Exception:
        return False
    return True


@pytest.fixture(params=["git", "jj"])
def b3(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    if shutil.which(request.param) is None:
        pytest.skip(f"{request.param} not on PATH")
    ds = _materialize(tmp_path / "b3", request.param)
    monkeypatch.setenv("HOME", str(ds.parent / "home"))  # `ducklake:~/...`
    monkeypatch.chdir(ds)
    return ds


def test_a_b3_dataset_upgrades_and_keeps_every_pin_listing_and_state(
    b3: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = b3.parent
    stores = root / "stores"
    with pytest.raises(ConfigError, match="tether upgrade"):
        Repo.find(b3)  # 0.1.0b4 refuses a b3 dataset until it is upgraded
    repo = Repo.find(b3, allow_outdated=True)
    first = repo.vcs.resolve("main~1" if repo.vcs.kind == "git" else "main-")
    plan = repo.plan_upgrade()
    assert plan.context["from"] == 4 and plan.context["to"] == CONFIG_VERSION == 5
    assert plan.context["parts"] == [
        "one manifest file per key; absolute local paths",
        "0.1.0b3 identities and states",
    ]
    ops: dict[str, set[str]] = {}
    for a in plan.actions:
        ops.setdefault(a.op, set()).add(a.key or a.target)
    assert ops["rewrite-index"] == {"tether-touched.jsonl", "tether-created.jsonl"}
    assert ops["rewrite-state"] == {"plates", "cells"}
    assert ops["rewrite-locator"] == {"lake", "lake_rel"}
    assert len(ops["copy-listing"]) >= 2  # `raw` (file://), `made` (a slash)

    report = repo.apply_upgrade(plan)
    assert not report.failed
    assert report.rewritten_indexes == {
        "tether-touched.jsonl": 2,
        "tether-created.jsonl": 1,
    }
    repo = Repo.find(b3)
    assert repo.config.version == CONFIG_VERSION
    assert repo.plan_upgrade().is_empty

    # U1: the indexes name each store by its resolved path now.
    shared = repo.vcs.shared_dir()
    touched = {
        d["key"]: d["identity"] for d in read_jsonl(shared / "tether-touched.jsonl")
    }
    assert touched == {
        "ice": {"uri": str(stores / "ice.icechunk")},
        "cells": {"uri": str(stores / "cells.lance")},
    }
    (created,) = read_jsonl(shared / "tether-created.jsonl")
    assert created["identity"] == {"uri": str(stores / "made")}

    # U1: every listing any manifest names -- history included -- is found.
    manifests = [
        (rev, m)
        for rev, objects in repo._iter_history_objects()
        for m in objects.values()
    ] + [(None, m) for m in repo.objects.values()]
    for rev, m in manifests:
        if m.kind == "file" and m.state is not None:
            assert repo._listing_for(m, rev) is not None, (rev, m.key)

    # U1: neither gc releases a pin history names, or the created store.
    for p in (repo.plan_gc(), repo.plan_gc(delete_stores=True)):
        assert not [a for a in p.actions if a.op in ("unpin", "delete-store")], (
            p.render()
        )
    pins = {
        m.pin.ref for _rev, m in manifests if m.pin is not None and m.kind != "ducklake"
    }
    assert len(pins) == 4  # ice x2, cells x2
    repo.gc(dry_run=False, delete_stores=True)
    assert (stores / "made").is_dir()
    ducklake = _ducklake_loads()
    reports = Repo.find(b3).verify(all_history=True)
    for label, r in reports.items():
        if ducklake or ":lake" not in label:
            assert r.ok, (label, r)
    assert len({label.split(":", 1)[1] for label in reports}) >= 2

    # U2 and U5: states carried forward, so nothing reads as changed.
    plates = repo.objects["plates"].state
    assert plates is not None and plates["count"] == 4  # two files, two links
    cells = repo.objects["cells"].state
    assert cells is not None and "branch_id" in cells
    ws = repo.workspace
    assert ws.base_states["cells"] == cells and ws.last_snapshot["cells"] == cells
    status = Repo.find(b3).status(do_snapshot=True)
    for o in status.objects:
        if o.key.startswith("lake") and not ducklake:
            continue
        assert not o.changed and o.error is None, o

    # U4: DuckLake paths are absolute, and history reads the same paths.
    assert repo.objects["lake"].locator["metadata"] == (
        f"ducklake:{root / 'home' / 'lake.ducklake'}"
    )
    assert repo.objects["lake_rel"].locator["metadata"] == (
        f"ducklake:{b3 / 'rel.ducklake'}"
    )
    moved = {e.key: e.why for e in repo.diff(first)}
    assert "locator" not in moved["lake"] and "locator" not in moved["lake_rel"]
    if ducklake:
        monkeypatch.chdir(root)
        lake = repo.objects["lake"]
        state = repo.backend_for("ducklake").fingerprint(lake.locator, None)
        assert state == lake.state

    # A commit afterwards is a no-op for every object 0.1.0b3 committed.
    if ducklake:
        result = Repo.find(b3).commit("after the upgrade")
        assert not [k for k, pin in result.pinned.items() if pin is not None]


def test_a_b3_dataset_upgrades_through_the_cli(b3: Path) -> None:
    typer_testing = pytest.importorskip("typer.testing")
    from tether.cli import app

    runner = typer_testing.CliRunner()
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 1 and "tether upgrade" in r.output
    r = runner.invoke(app, ["upgrade", "--dry-run"])
    assert r.exit_code == 0 and "rewrite-index" in r.output, r.output
    r = runner.invoke(app, ["upgrade", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["from_version"] == 4 and payload["to_version"] == CONFIG_VERSION
    assert payload["rewritten_indexes"] == {
        "tether-touched.jsonl": 2,
        "tether-created.jsonl": 1,
    }
    assert set(payload["rewritten_manifests"]) == {
        "lake",
        "lake_rel",
        "cells",
        "plates",
    }
    assert runner.invoke(app, ["status"]).exit_code == 0
    r = runner.invoke(app, ["upgrade"])
    assert r.exit_code == 0 and f"already at version {CONFIG_VERSION}" in r.output
