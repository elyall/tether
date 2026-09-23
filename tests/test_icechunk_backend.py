from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from tether.backends.base import ObjectBackend
from tether.errors import BackendError
from tether.manifest import Locator, Pin
from tether.testing import run_conformance

ic = pytest.importorskip("icechunk")
zarr = pytest.importorskip("zarr")


def _new_repo(path: Path) -> str:
    """Create an Icechunk repo at ``path`` with one commit; return its URI."""
    path.mkdir(parents=True, exist_ok=True)
    storage = ic.local_filesystem_storage(str(path))
    repo = ic.Repository.create(storage)
    session = repo.writable_session("main")
    group = zarr.create_group(store=session.store)
    group.attrs["v"] = 0
    session.commit("init")
    return str(path)


def _commit(uri: str, branch: str, value: int) -> None:
    repo = ic.Repository.open(ic.local_filesystem_storage(uri))
    session = repo.writable_session(branch)
    group = zarr.open_group(store=session.store, mode="a")
    group.attrs["v"] = value
    session.commit(f"set v={value}")


class IcechunkHarness:
    def __init__(self, tmp: Path) -> None:
        from tether.backends.icechunk import IcechunkBackend

        self.backend: ObjectBackend = IcechunkBackend()
        self.tmp = tmp
        self._n = 0

    def new_object(self) -> Locator:
        uri = _new_repo(self.tmp / f"repo-{uuid.uuid4().hex[:8]}")
        return {"uri": uri, "branch": "main"}

    def mutate(self, locator: Locator, working_ref: str | None) -> None:
        self._n += 1
        _commit(locator["uri"], working_ref or "main", self._n)

    def fresh_locator(self) -> Locator:
        return {
            "uri": str(self.tmp / f"fresh-{uuid.uuid4().hex[:8]}"),
            "branch": "main",
        }


def _needs_lifecycle_api() -> None:
    from tether.backends.icechunk import IcechunkBackend

    why = IcechunkBackend._lifecycle_api()
    if why is not None:
        pytest.skip(why)  # icechunk 1.x: no repository metadata, no CREATE


def test_icechunk_created_store_is_empty_when_only_a_later_pin_generation_remains(
    tmp_path: Path,
) -> None:
    """A gc plan names the pin it unpins as `tether.<id>`; when that tag name
    was burnt by an earlier deletion the live tag is `tether.<id>.2`.
    `is_ref_empty` must match the ignored pin by id, not by tag name --
    otherwise the store is kept for a ref the same plan is releasing."""
    from tether.backends.icechunk import IcechunkBackend
    from tether.manifest import ref_for_pin

    _needs_lifecycle_api()
    backend = IcechunkBackend()
    loc = {"uri": str(tmp_path / "made"), "branch": "main"}
    initial = backend.create(loc, owner="0a1b2c3d")
    pid = "0a1b2c3d.0123456789abcdef"
    first = backend.pin(loc, initial, pid)
    backend.unpin(loc, first)  # burns the tag name
    again = backend.pin(loc, initial, pid)
    assert again.ref == f"{ref_for_pin(pid)}.2" and again.id == pid

    assert backend.is_ref_empty(loc) is False  # the pin is a ref of ours
    assert backend.is_ref_empty(loc, ignoring={ref_for_pin(pid)}) is True
    assert backend.is_ref_empty(loc, ignoring={again.ref}) is True
    # A pin the plan does not release still keeps the store.
    other = backend.pin(loc, initial, "0a1b2c3d.fedcba9876543210")
    assert backend.is_ref_empty(loc, ignoring={ref_for_pin(pid)}) is False
    backend.unpin(loc, other)
    backend.unpin(loc, again)
    assert backend.is_ref_empty(loc) is True


def test_icechunk_create_refuses_anything_already_there_and_delete_stays_inside(
    tmp_path: Path,
) -> None:
    """`create` refuses a path that exists at all (not only one that already
    holds a repository), and `delete_store` removes an Icechunk layout only:
    a stranger's file under the prefix keeps the store."""
    from tether.backends.icechunk import IcechunkBackend

    _needs_lifecycle_api()
    backend = IcechunkBackend()
    taken = tmp_path / "taken"
    taken.mkdir()
    (taken / "notes.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(BackendError, match="already exists"):
        backend.create({"uri": str(taken), "branch": "main"}, owner="0a1b2c3d")
    assert (taken / "notes.txt").exists()

    loc = {"uri": str(tmp_path / "made"), "branch": "main"}
    backend.create(loc, owner="0a1b2c3d")
    (tmp_path / "made" / "co-located.parquet").write_bytes(b"PAR1")
    assert backend.is_ref_empty(loc) is True  # refs say empty...
    with pytest.raises(BackendError, match=r"co-located\.parquet"):
        backend.delete_store(loc)  # ...but the delete stays inside the layout
    assert (tmp_path / "made" / "co-located.parquet").exists()
    (tmp_path / "made" / "co-located.parquet").unlink()
    backend.delete_store(loc)
    assert not (tmp_path / "made").exists()


def test_icechunk_s3_delete_removes_only_the_repository_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The S3 arm of `delete_store` (`_prefix_keys` -> `_foreign_keys` ->
    `obs.delete`) against an obstore in-memory store standing in for the
    bucket: a stranger's object under the prefix refuses the delete and
    keeps everything; without it, every key of the layout goes."""
    import obstore as obs
    from obstore.store import MemoryStore

    from tether.backends.icechunk import IcechunkBackend

    backend = IcechunkBackend()
    loc = {"uri": "s3://bucket/probes/emb.icechunk", "branch": "main"}
    bucket = MemoryStore()
    for key in (
        "repo",
        "snapshots/1CECHNKREP0F1RSTCMT0",
        "transactions/1CECHNKREP0F1RSTCMT0",
        "manifests/08T65T4WE8K1BMG8XFEG",
        "chunks/abc",
        "overwritten/repo.1.X",
        "refs/branch.main/ZZZ.json",  # a 1.x-layout name is layout too
    ):
        obs.put(bucket, key, b"x")

    def keys() -> list[str]:
        return sorted(str(m["path"]) for page in obs.list(bucket) for m in page)

    # The repository half is what the earlier checks establish; stand it in.
    monkeypatch.setattr(backend, "owner", lambda locator: "0a1b2c3d")
    monkeypatch.setattr(backend, "is_ref_empty", lambda locator, ignoring=(): True)
    monkeypatch.setattr(backend, "_prefix_store", lambda locator: bucket)

    obs.put(bucket, "co-located.parquet", b"PAR1")
    with pytest.raises(BackendError, match=r"co-located\.parquet"):
        backend.delete_store(loc)
    assert len(keys()) == 8  # nothing was touched
    obs.delete(bucket, "co-located.parquet")

    obs.put(bucket, ".DS_Store", b"")  # tolerated, removed with the store
    backend.delete_store(loc)
    assert keys() == []


_S3_SECRETS = (
    '[uris."s3://lab/"]\n'
    'endpoint_url = "http://127.0.0.1:{port}"\n'
    "allow_http = true\n"
    "force_path_style = true\n"
    'access_key_id = "AKIA_LAB"\n'
    'secret_access_key = "sk-lab"\n'
    'region = "us-east-1"\n'
    '[objects."b"]\n'
    'endpoint_url = "http://127.0.0.1:{port}"\n'
    'allow_http = "true"\n'
    "force_path_style = true\n"
    'access_key_id = "AKIA_B"\n'
    'secret_access_key = "sk-b"\n'
    'region = "us-east-1"\n'
)


def _create_snapshot_reclaim(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`add --create` two S3 stores through the CLI (one configured by URI
    prefix, one by key), fingerprint them, then remove them and have
    `gc --delete-stores` delete both."""
    typer_testing = pytest.importorskip("typer.testing")
    from tether.cli import app

    runner = typer_testing.CliRunner()
    monkeypatch.chdir(root)
    for key, uri in (("a", "s3://lab/a.icechunk"), ("b", "s3://lab2/b.icechunk")):
        r = runner.invoke(app, ["add", key, "--kind", "icechunk", uri, "--create"])
        assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["status", "--snapshot"])
    assert r.exit_code == 0, r.output
    for key in ("a", "b"):
        assert runner.invoke(app, ["remove", key]).exit_code == 0
    r = runner.invoke(app, ["gc", "--delete-stores", "--no-dry-run", "--json"])
    assert r.exit_code == 0, r.output
    assert set(json.loads(r.output)["deleted_stores"]) == {"a", "b"}


def test_s3_server_switches_reach_every_icechunk_call_through_the_cli(
    vcs_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain-HTTP, path-style S3 server (SeaweedFS, MinIO) needs
    `allow_http` and `force_path_style`; `Repository.create` against one
    worked only when both were passed, and tether passed neither. Set in
    secrets.toml by URI prefix or by key, they reach `s3_storage` on every
    path -- create, open, fingerprint, owner, delete_store -- and the obstore
    client that lists and deletes the prefix."""
    import obstore.store

    from tether.repo import Repo

    _needs_lifecycle_api()
    buckets = tmp_path / "buckets"
    storages: list[dict] = []
    stores: list[dict] = []
    real_storage = ic.local_filesystem_storage

    def s3_storage(**kw: object) -> object:
        storages.append(kw)
        return real_storage(str(buckets / str(kw["bucket"]) / str(kw["prefix"])))

    def from_url(url: str, **kw: object) -> object:
        stores.append({"url": url, **kw})
        root = buckets / url.removeprefix("s3://")
        root.mkdir(parents=True, exist_ok=True)
        return obstore.store.LocalStore(str(root))

    monkeypatch.setattr(ic, "s3_storage", s3_storage)
    monkeypatch.setattr(obstore.store, "from_url", from_url)
    Repo.init(vcs_root)
    secrets = vcs_root / ".tether" / "secrets.toml"
    secrets.write_text(_S3_SECRETS.format(port=8333))
    secrets.chmod(0o600)
    _create_snapshot_reclaim(vcs_root, monkeypatch)

    assert {s["bucket"] for s in storages} == {"lab", "lab2"}
    assert len(storages) >= 10  # create, open, fingerprint, owner, delete, x2
    for s in storages:
        assert s.get("allow_http") is True and s.get("force_path_style") is True, s
        assert s["endpoint_url"] == "http://127.0.0.1:8333", s
        want = "AKIA_LAB" if s["bucket"] == "lab" else "AKIA_B"
        assert s["access_key_id"] == want and "from_env" not in s, s
    assert {s["url"] for s in stores} == {"s3://lab/a.icechunk", "s3://lab2/b.icechunk"}
    for s in stores:
        assert s["client_options"] == {"allow_http": True}, s
        assert s["virtual_hosted_style_request"] is False, s
        assert s["AWS_ENDPOINT_URL"] == "http://127.0.0.1:8333", s
    for prefix in ("lab/a.icechunk", "lab2/b.icechunk"):
        assert not [p for p in (buckets / prefix).rglob("*") if p.is_file()]


def _free_port_pair() -> int:
    """A port that is free, and so is the one 10000 above it (where
    SeaweedFS puts each server's gRPC port)."""
    import random
    import socket

    for _ in range(200):
        port = random.randrange(20000, 30000)
        try:
            for p in (port, port + 10000):
                with socket.socket() as s:
                    s.bind(("127.0.0.1", p))
        except OSError:
            continue
        return port
    raise RuntimeError("no free port pair")


@pytest.fixture
def seaweedfs(tmp_path: Path) -> Iterator[str]:
    """A SeaweedFS server with its S3 gateway (plain HTTP, no auth), and
    buckets `lab` and `lab2`; its endpoint URL. Skips without `weed`."""
    import shutil
    import subprocess
    import time
    import urllib.error
    import urllib.request

    weed = shutil.which("weed")
    if weed is None:
        pytest.skip("SeaweedFS (`weed`) not on PATH")
    ports = {name: _free_port_pair() for name in ("master", "volume", "filer", "s3")}
    data = tmp_path / "weed"
    data.mkdir()
    log = (tmp_path / "weed.log").open("w")
    proc = subprocess.Popen(
        [
            weed,
            "server",
            f"-dir={data}",
            "-ip=127.0.0.1",
            "-ip.bind=127.0.0.1",
            f"-master.port={ports['master']}",
            f"-volume.port={ports['volume']}",
            f"-filer.port={ports['filer']}",
            "-s3",
            f"-s3.port={ports['s3']}",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    endpoint = f"http://127.0.0.1:{ports['s3']}"
    try:
        for bucket in ("lab", "lab2"):
            deadline = time.monotonic() + 90
            while True:
                request = urllib.request.Request(f"{endpoint}/{bucket}", method="PUT")
                try:
                    urllib.request.urlopen(request, timeout=5).close()
                    break
                except (urllib.error.URLError, ConnectionError, TimeoutError):
                    if proc.poll() is not None or time.monotonic() > deadline:
                        raise
                    time.sleep(0.5)
        yield endpoint
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def test_icechunk_add_create_and_reclaim_against_seaweedfs(
    vcs_root: Path, seaweedfs: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same CLI round trip against a real S3-compatible server: nothing
    stands in for Icechunk or obstore."""
    from tether.repo import Repo

    _needs_lifecycle_api()
    Repo.init(vcs_root)
    secrets = vcs_root / ".tether" / "secrets.toml"
    port = seaweedfs.rsplit(":", 1)[1]
    secrets.write_text(_S3_SECRETS.format(port=port))
    secrets.chmod(0o600)
    _create_snapshot_reclaim(vcs_root, monkeypatch)


def test_s3_server_switches_default_off_and_come_only_from_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent or false, neither switch is passed (Icechunk's defaults
    stand); a switch the installed icechunk lacks is refused by name."""
    from tether.backends.icechunk import IcechunkBackend

    calls: list[dict] = []
    monkeypatch.setattr(ic, "s3_storage", lambda **kw: calls.append(kw) or kw)
    b = IcechunkBackend()
    b.configure_secrets(
        {},
        {
            "s3://off/": {"allow_http": False, "force_path_style": "false"},
            "s3://on/": {"allow_http": "yes", "force_path_style": 1},
        },
    )
    for uri in ("s3://none/r", "s3://off/r", "s3://on/r"):
        b._storage({"uri": uri})
    none, off, on = calls
    for s in (none, off):
        assert "allow_http" not in s and "force_path_style" not in s
    assert on["allow_http"] is True and on["force_path_style"] is True

    def old_s3_storage(*, bucket: str, prefix: str | None, region=None, from_env=None):
        return None

    monkeypatch.setattr(ic, "s3_storage", old_s3_storage)
    b._storage({"uri": "s3://none/r"})  # nothing it lacks is asked of it
    with pytest.raises(BackendError, match=r"takes no allow_http, force_path_style"):
        b._storage({"uri": "s3://on/r"})


def test_icechunk_backend_conformance(tmp_path: Path) -> None:
    run_conformance(IcechunkHarness(tmp_path))


def test_icechunk_role_credentials_refresh_inside_a_long_lived_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile or role entry opens the repository with Icechunk's
    `get_credentials` callback, not static keys: a session held open past
    the role's expiry asks for fresh keys itself instead of failing
    mid-write, and the cached Repository is never rebuilt for a rotation.
    Literal keys stay static, and no entry stays `from_env`."""
    import pickle
    from datetime import UTC, datetime

    from tether import credentials
    from tether.backends.icechunk import IcechunkBackend

    clock = {"now": 1_000_000.0}
    issued: list[str] = []

    def resolve(options: dict, profile: object, role: object) -> tuple[dict, float]:
        issued.append(f"ASIA{len(issued) + 1}")
        creds = {
            "access_key_id": issued[-1],
            "secret_access_key": "s",
            "session_token": "t",
        }
        return creds, clock["now"] + 3600

    monkeypatch.setattr(credentials, "_resolve", resolve)
    monkeypatch.setattr(credentials, "_now", lambda: clock["now"])
    monkeypatch.setattr(credentials, "_cache", {})
    opened: list[dict] = []
    monkeypatch.setattr(ic, "s3_storage", lambda **kw: kw)
    monkeypatch.setattr(
        ic.Repository,
        "open",
        staticmethod(lambda storage: opened.append(storage) or object()),
    )
    b = IcechunkBackend()
    b.configure_secrets(
        {},
        {
            "s3://role/": {"role_arn": "arn:aws:iam::1:role/r"},
            "s3://profile/": {"profile": "lab"},
            "s3://literal/": {"access_key_id": "AKIA", "secret_access_key": "k"},
        },
    )
    loc = {"uri": "s3://role/repo", "branch": "main"}
    repo = b._repo(loc)
    (storage,) = opened
    assert "access_key_id" not in storage and "from_env" not in storage
    refresh = storage["get_credentials"]
    first = refresh()
    assert first.access_key_id == "ASIA1" and first.session_token == "t"
    assert first.expires_after == datetime.fromtimestamp(
        clock["now"] + 3600 - credentials.REFRESH_MARGIN, tz=UTC
    )
    clock["now"] += 3600 - credentials.REFRESH_MARGIN  # when Icechunk asks again
    second = pickle.loads(pickle.dumps(refresh))()  # a session sent to a worker
    assert second.access_key_id == "ASIA2"
    assert b._repo(loc) is repo and len(opened) == 1  # never rebuilt
    b._repo({"uri": "s3://profile/repo"})
    assert "get_credentials" in opened[-1]
    b._repo({"uri": "s3://literal/repo"})
    assert opened[-1]["access_key_id"] == "AKIA" and "get_credentials" not in opened[-1]
    b._repo({"uri": "s3://none/repo"})
    assert opened[-1].get("from_env") is True


def test_icechunk_repository_is_rebuilt_when_static_credentials_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where `s3_storage` takes no `get_credentials`, a role's keys are
    static: `_repo` caches one Repository per URI and rebuilds it when
    `aws_credentials` hands out a fresh set near expiry, and only then.
    Local storage has no credentials and is cached as before."""
    from tether import credentials
    from tether.backends.icechunk import IcechunkBackend

    opened: list[dict] = []

    def s3_storage(
        *,
        bucket: str,
        prefix: str | None,
        region: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
        from_env: bool | None = None,
    ) -> dict:
        return {
            k: v
            for k, v in locals().items()
            if v is not None and k not in ("bucket", "prefix", "region")
        }

    monkeypatch.setattr(ic, "s3_storage", s3_storage)
    monkeypatch.setattr(
        ic.Repository,
        "open",
        staticmethod(lambda storage: opened.append(storage) or object()),
    )
    current = {
        "access_key_id": "ASIA1",
        "secret_access_key": "s1",
        "session_token": "t1",
    }
    monkeypatch.setattr(
        credentials,
        "aws_credentials",
        lambda options: dict(current) if options.get("role_arn") else None,
    )
    b = IcechunkBackend()
    b.configure_secrets({}, {"s3://bucket/": {"role_arn": "arn:aws:iam::1:role/r"}})
    loc = {"uri": "s3://bucket/repo", "branch": "main"}
    b._repo(loc)
    b._repo(loc)
    assert len(opened) == 1 and opened[0]["access_key_id"] == "ASIA1"
    current = {
        "access_key_id": "ASIA2",
        "secret_access_key": "s2",
        "session_token": "t2",
    }
    b._repo(loc)
    b._repo(loc)
    assert len(opened) == 2 and opened[1]["access_key_id"] == "ASIA2"
    # No rule: the environment serves, and there is nothing to compare.
    b._repo({"uri": "s3://elsewhere/repo"})
    b._repo({"uri": "s3://elsewhere/repo"})
    assert len(opened) == 3 and opened[2].get("from_env") is True

    monkeypatch.undo()
    real = IcechunkBackend()
    uri = _new_repo(tmp_path / "repo")
    assert real._repo({"uri": uri}) is real._repo({"uri": uri})


def _main_history(uri: str) -> list[str]:
    repo = ic.Repository.open(ic.local_filesystem_storage(uri))
    return [str(info.id) for info in repo.ancestry(branch="main")]


def test_icechunk_promote_refuses_when_main_moved_after_the_ancestry_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Port of the review's `r_icechunk.py`: `promote` checked that main's
    head was an ancestor of the fork, then `reset_branch` moved main
    unconditionally, so a commit landing on main in between vanished from
    main's history. With the head the plan reviewed as `expected`, the reset
    is Icechunk's compare-and-swap (`from_snapshot_id`): refused as
    `RefMovedError`, and the concurrent commit stays main's head."""
    from tether.backends.icechunk import IcechunkBackend
    from tether.errors import RefMovedError

    uri = _new_repo(tmp_path / "repo")
    b = IcechunkBackend()
    loc = {"uri": uri, "branch": "main"}
    reviewed = b.fingerprint(loc, None)
    wref = b.fork(loc, reviewed, "tether.ws.c0fe5a1e.feat")
    _commit(uri, wref, 2)
    fork_head = b.fingerprint(loc, wref)

    landed: dict[str, str] = {}
    original = IcechunkBackend.ancestor_of

    def racing(self: IcechunkBackend, locator: dict, ancestor: dict, descendant: str):
        answer = original(self, locator, ancestor, descendant)
        _commit(uri, "main", 99)  # a concurrent writer, right after the check
        landed["sid"] = _main_history(uri)[0]
        return answer

    monkeypatch.setattr(IcechunkBackend, "ancestor_of", racing)
    with pytest.raises(RefMovedError, match="promote"):
        b.promote(loc, wref, expected=reviewed)
    monkeypatch.undo()
    history = _main_history(uri)
    assert history[0] == landed["sid"], "the concurrent commit must stay main's head"
    assert fork_head["snapshot_id"] not in history
    assert b.fingerprint(loc, None) == {"snapshot_id": landed["sid"]}

    # The plan is stale now: refused before anything is compared or reset;
    # without `expected`, the divergence itself is refused.
    with pytest.raises(RefMovedError):
        b.promote(loc, wref, expected=reviewed)
    with pytest.raises(BackendError, match="not an ancestor"):
        b.promote(loc, wref)
    # Reviewed at the current head: promoted, through the compare-and-swap.
    b.fork(loc, b.fingerprint(loc, None), wref)
    _commit(uri, wref, 3)
    promoted = b.promote(loc, wref, expected=b.fingerprint(loc, None))
    assert promoted == b.fingerprint(loc, wref) == b.fingerprint(loc, None)


def test_icechunk_fork_with_expected_is_a_compare_and_swap(tmp_path: Path) -> None:
    """A stale `expected` (the branch moved on) or ABSENT on an existing
    branch is refused and the branch left alone; ABSENT on a fresh name
    creates it; the right `expected` moves it."""
    from tether.backends.base import ABSENT
    from tether.backends.icechunk import IcechunkBackend
    from tether.errors import RefMovedError

    uri = _new_repo(tmp_path / "repo")
    b = IcechunkBackend()
    loc = {"uri": uri, "branch": "main"}
    source = b.fingerprint(loc, None)
    name = "tether.ws.c0fe5a1e.work"
    assert b.fork(loc, source, name, expected=ABSENT) == name
    _commit(uri, name, 5)
    head = b.fingerprint(loc, name)
    with pytest.raises(RefMovedError, match="expected"):
        b.fork(loc, source, name, expected=source)
    assert b.fingerprint(loc, name) == head
    with pytest.raises(RefMovedError, match="expected absent"):
        b.fork(loc, source, name, expected=ABSENT)
    assert b.fingerprint(loc, name) == head
    with pytest.raises(RefMovedError, match="gone"):
        b.fork(loc, source, "tether.ws.c0fe5a1e.gone", expected=source)
    with pytest.raises(RefMovedError, match="nothing"):
        b.fork(loc, source, "tether.ws.c0fe5a1e.gone", expected=head)
    assert "tether.ws.c0fe5a1e.gone" not in b.list_working_refs(loc)
    assert b.fork(loc, source, name, expected=head) == name
    assert b.fingerprint(loc, name) == source
    # At the source and matching `expected`: left alone.
    assert b.fork(loc, source, name, expected=source) == name
    assert b.fingerprint(loc, name) == source


def test_icechunk_without_from_snapshot_id_checks_before_the_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Icechunk 1.x's `reset_branch` has no `from_snapshot_id`: the backend
    then compares the head first and resets unconditionally (a check before
    the act, `check_expected`), with the same refusals."""
    from tether.backends.icechunk import IcechunkBackend
    from tether.errors import RefMovedError

    assert IcechunkBackend._conditional_reset() is True  # 2.2.0 in the venv
    monkeypatch.setattr(
        IcechunkBackend, "_conditional_reset", staticmethod(lambda: False)
    )
    uri = _new_repo(tmp_path / "repo")
    b = IcechunkBackend()
    loc = {"uri": uri, "branch": "main"}
    source = b.fingerprint(loc, None)
    wref = b.fork(loc, source, "tether.ws.c0fe5a1e.work")
    _commit(uri, wref, 7)
    head = b.fingerprint(loc, wref)
    with pytest.raises(RefMovedError):
        b.fork(loc, source, wref, expected=source)
    assert b.fingerprint(loc, wref) == head
    assert b.fork(loc, source, wref, expected=head) == wref
    assert b.fingerprint(loc, wref) == source

    _commit(uri, wref, 8)
    fork_head = b.fingerprint(loc, wref)
    _commit(uri, "main", 9)
    with pytest.raises(RefMovedError):
        b.promote(loc, wref, expected=source)
    assert b.fingerprint(loc, None) != fork_head
    b.fork(loc, b.fingerprint(loc, None), wref)
    _commit(uri, wref, 10)
    assert b.promote(loc, wref, expected=b.fingerprint(loc, None)) == b.fingerprint(
        loc, wref
    )


def test_icechunk_library_errors_become_backend_errors(tmp_path: Path) -> None:
    """Every protocol method re-raises icechunk's exceptions as BackendError,
    so the engine's `except TetherError` sites see a refusal rather than a
    traceback from inside the library; a pin of a snapshot that does not
    exist is refused before any tag name is spent."""
    import icechunk as ic

    from tether.backends.icechunk import IcechunkBackend

    b = IcechunkBackend()
    # 2.x names the error class; 1.x (the resolution on Python 3.11) does not.
    with pytest.raises(BackendError, match=r"RepositoryNotFoundError|doesn't exist"):
        b.fingerprint({"uri": str(tmp_path / "missing")}, None)
    uri = _new_repo(tmp_path / "repo")
    loc = {"uri": uri}
    state = b.fingerprint(loc, None)
    with pytest.raises(BackendError):
        b.pin(loc, {**state, "snapshot_id": "0" * 27}, "0" * 16)
    repo = ic.Repository.open(ic.local_filesystem_storage(str(tmp_path / "repo")))
    assert not repo.list_tags(), "a failed pin must not leave a tag behind"


def test_icechunk_deleted_tag_comes_back_as_a_new_generation(vcs_root: Path) -> None:
    """Icechunk never reuses a deleted tag name, so a pin whose tag was deleted
    is recreated under a generation suffix: same id, ref `tether.<id>.2`.
    Readers resolve the pin through whichever generation is live; `gc` sees
    one pin id and `unpin` clears every generation."""
    from tether.handles import IcechunkHandle
    from tether.repo import Repo

    uri = _new_repo(vcs_root / "imaging.icechunk")
    repo = Repo.init(vcs_root)
    repo.add("zarr/imaging", "icechunk", {"uri": uri, "branch": "main"})
    res = repo.commit("pin it")
    pin = res.pinned["zarr/imaging"]
    assert pin is not None and res.vcs_commit is not None
    state = repo.objects["zarr/imaging"].state
    assert state is not None
    ic_repo = ic.Repository.open(ic.local_filesystem_storage(uri))
    backend = repo.backend_for("icechunk")
    locator = {"uri": uri, "branch": "main"}

    ic_repo.delete_tag(pin.ref)
    assert not repo.verify()["zarr/imaging"].ok

    # Meanwhile, before repair: reads and forks still work, by snapshot.
    handle = repo.open("zarr/imaging", rev=res.vcs_commit)
    assert isinstance(handle, IcechunkHandle) and handle.read_only
    assert handle.snapshot_id == state["snapshot_id"] and handle.tag is None

    report = repo.apply_repair(repo.plan_repair())
    assert report.repinned == {"zarr/imaging": pin.id} and not report.failed
    assert ic_repo.lookup_tag(f"{pin.ref}.2") == state["snapshot_id"]
    verdict = repo.verify()["zarr/imaging"]
    assert verdict.ok and verdict.message == f"pinned as {pin.ref}.2"
    # The manifest still says `tether.<id>`; the backend resolves the live tag.
    assert repo.objects["zarr/imaging"].pin == pin
    handle = repo.open("zarr/imaging", rev=res.vcs_commit)
    assert isinstance(handle, IcechunkHandle) and handle.tag == f"{pin.ref}.2"

    # Re-committing the same state is idempotent against the live generation,
    # and gc counts it as the one pin it is.
    assert backend.pin(locator, state, pin.id) == Pin(id=pin.id, ref=f"{pin.ref}.2")
    assert backend.list_pins(locator) == {pin.id}
    repo.new(bookmark="work", eager=True)
    wref = repo.workspace.working_refs["zarr/imaging"]
    assert wref is not None and ic_repo.lookup_branch(wref) == state["snapshot_id"]

    # Burn the second name too: the third generation takes over. Unpin by the
    # manifest's original ref clears every generation.
    ic_repo.delete_tag(f"{pin.ref}.2")
    assert backend.pin(locator, state, pin.id).ref == f"{pin.ref}.3"
    backend.unpin(locator, pin)
    assert backend.list_pins(locator) == set()
    assert not repo.verify()["zarr/imaging"].ok


def test_icechunk_diff_across_branches_goes_through_the_common_base(
    tmp_path: Path,
) -> None:
    """Icechunk diffs along one line of history; two heads diff via their base."""
    from tether.backends.icechunk import IcechunkBackend

    uri = _new_repo(tmp_path / "repo")
    repo = ic.Repository.open(ic.local_filesystem_storage(uri))
    base = repo.lookup_branch("main")
    for branch, value in (("a", 1), ("b", 2)):
        repo.create_branch(branch, base)
        session = repo.writable_session(branch)
        group = zarr.open_group(store=session.store, mode="a")
        group.attrs["v"] = value
        if branch == "a":
            group.create_array("only_a", shape=(2,), dtype="u1")
        else:
            group.create_array("only_b", shape=(2,), dtype="u1")
        session.commit(f"{branch}: v={value}")
    head_a = {"snapshot_id": repo.lookup_branch("a")}
    head_b = {"snapshot_id": repo.lookup_branch("b")}

    d = IcechunkBackend().diff({"uri": uri, "branch": "main"}, head_a, head_b)
    changes = {e.path: e.change for e in d.entries}
    assert changes["/only_a"] == "removed"  # `a` has it, `b` does not
    assert changes["/only_b"] == "added"
    assert changes["/"] == "modified"  # both sides touched the root's metadata
    assert d.note.startswith(f"diverged at snapshot {base}")

    # Same line of history: the native diff, no note.
    straight = IcechunkBackend().diff(
        {"uri": uri, "branch": "main"}, {"snapshot_id": base}, head_a
    )
    assert {e.path: e.change for e in straight.entries}["/only_a"] == "added"
    assert not straight.note


def test_icechunk_fork_from_older_snapshot(vcs_root: Path) -> None:
    """Adopt a repo at a non-head snapshot chosen from `history`, then fork it."""
    from tether.backends.base import Capability
    from tether.handles import IcechunkHandle
    from tether.repo import Repo

    uri = _new_repo(vcs_root / "imaging.icechunk")
    _commit(uri, "main", 1)
    _commit(uri, "main", 2)
    repo = Repo.init(vcs_root)

    # Browse before registering: newest first, main marked, messages present.
    entries = repo.history_for("icechunk", {"uri": uri})
    assert [e.message for e in entries][:3] == ["set v=2", "set v=1", "init"]
    assert entries[0].refs == ["main"] and entries[0].when is not None
    older = entries[1]  # v=1, not the head

    repo.add("zarr/imaging", "icechunk", {"uri": uri, "branch": "main", "at": older.id})
    status = repo.status()
    obj = next(o for o in status.objects if o.key == "zarr/imaging")
    assert obj.current_state == {"snapshot_id": older.id}
    assert Capability.HISTORY in repo.backend_for("icechunk").capabilities

    # Read-only open at the detached base sees the old data, not the head.
    ro = repo.open("zarr/imaging", read_only=True)
    assert isinstance(ro, IcechunkHandle) and ro.snapshot_id == older.id
    assert zarr.open_group(store=ro.session.store, mode="r").attrs["v"] == 1

    # Commit pins the chosen snapshot; new forks from it; the fork starts at v=1.
    res = repo.commit("adopt imaging at v=1")
    pin = res.pinned["zarr/imaging"]
    assert pin is not None
    ic_repo = ic.Repository.open(ic.local_filesystem_storage(uri))
    assert ic_repo.lookup_tag(pin.ref) == older.id
    repo.new(bookmark="work", eager=True)
    wref = repo.workspace.working_refs["zarr/imaging"]
    assert ic_repo.lookup_branch(wref) == older.id
    handle = repo.open("zarr/imaging")
    assert isinstance(handle, IcechunkHandle) and not handle.read_only
    assert zarr.open_group(store=handle.session.store, mode="r").attrs["v"] == 1
    # main is untouched at v=2.
    assert repo.history("zarr/imaging", ref="main")[0].message == "set v=2"

    # After writing on the fork, history from the working ref shows it first
    # and the pin is listed as a ref on the base snapshot.
    _commit(uri, wref, 10)
    log = repo.history("zarr/imaging")
    assert log[0].message == "set v=10" and log[0].refs == [wref]
    assert pin.ref in log[1].refs

    # `at` also accepts a tag name; an unknown ref is a clear error.
    b = repo.backend_for("icechunk")
    assert b.fingerprint({"uri": uri, "at": pin.ref}, None) == {"snapshot_id": older.id}
    with pytest.raises(BackendError):
        b.fingerprint({"uri": uri, "at": "NOPE"}, None)


def test_icechunk_one_store_spelled_two_ways_is_one_namespace_to_gc(
    vcs_root: Path,
) -> None:
    """Two keys on one repository, `/p` and `file:///p`: one identity, so
    they share a pin and `gc` counts both keys' references against the store.
    Keyed on the raw string, each spelling released the other's pins."""
    from tether.repo import Repo

    repo = Repo.init(vcs_root)
    uri = _new_repo(vcs_root / "imaging.icechunk")
    repo.add("a", "icechunk", {"uri": uri, "branch": "main"})
    repo.add("b", "icechunk", {"uri": f"file://{uri}", "branch": "main"})
    backend = repo.backend_for("icechunk")
    assert backend.identity({"uri": f"file://{uri}"}) == {"uri": uri}
    res = repo.commit("baseline")
    pins = {k: p for k, p in res.pinned.items() if p is not None}
    assert set(pins) == {"a", "b"} and pins["a"].id == pins["b"].id
    assert not [x for x in repo.plan_gc().actions if x.op == "unpin"]
    repo.gc(dry_run=False)
    assert pins["a"].id in backend.list_pins({"uri": uri})
    assert all(r.ok for r in repo.verify().values())


def test_icechunk_engine_lifecycle(vcs_root: Path) -> None:
    from tether.handles import IcechunkHandle
    from tether.repo import Repo

    repo = Repo.init(vcs_root)
    uri = _new_repo(vcs_root / "imaging.icechunk")
    repo.add("zarr/imaging", "icechunk", {"uri": uri, "branch": "main"})

    res = repo.commit("baseline imaging")
    pin = res.pinned["zarr/imaging"]
    assert pin is not None
    backend = repo.backend_for("icechunk")
    assert pin.id in backend.list_pins({"uri": uri})

    # Fork a working branch and confirm it starts at the pinned snapshot.
    repo.new(bookmark="work", eager=True)
    wref = repo.workspace.working_refs["zarr/imaging"]
    assert wref and wref != "main"
    handle = repo.open("zarr/imaging")
    assert isinstance(handle, IcechunkHandle) and not handle.read_only

    # Advance the fork; status should report drift from the pin.
    _commit(uri, wref, 99)
    status = repo.status()
    obj = next(o for o in status.objects if o.key == "zarr/imaging")
    assert obj.changed

    res2 = repo.commit("imaging update")
    pin2 = res2.pinned["zarr/imaging"]
    assert pin2 is not None and pin2.id != pin.id

    # Read-only open at the first commit lands on the original snapshot/tag.
    old = repo.open("zarr/imaging", rev=res.vcs_commit)
    assert isinstance(old, IcechunkHandle) and old.read_only
    assert old.tag == pin.ref

    assert all(r.ok for r in repo.verify(deep=True).values())

    # Content diff between two commits: node-level changes from Icechunk
    # (written on the forked working branch, which is what tether tracks).
    ic_repo = ic.Repository.open(ic.local_filesystem_storage(uri))
    session = ic_repo.writable_session(wref)
    group = zarr.open_group(store=session.store, mode="a")
    arr = group.create_array("x", shape=(4,), chunks=(2,), dtype="i4")
    arr[:] = [1, 2, 3, 4]
    session.commit("add x")
    res3 = repo.commit("with array")
    entries = {
        e.key: e for e in repo.diff(res2.vcs_commit, res3.vcs_commit, content=True)
    }
    d = entries["zarr/imaging"].detail
    assert d is not None and d.unit == "nodes" and d.added == 1
    assert {(e.path, e.change, e.detail) for e in d.entries} >= {
        ("/x", "added", "array")
    }
