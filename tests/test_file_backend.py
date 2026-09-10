from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from tether.backends.base import Capability, ObjectBackend, VerifyStatus
from tether.backends.file import FileBackend, _parse, _walk_files
from tether.errors import BackendError
from tether.handles import FileHandle
from tether.manifest import Locator
from tether.testing import run_conformance

obstore = pytest.importorskip("obstore")
from obstore.store import MemoryStore  # noqa: E402


# --------------------------------------------------------------------------- #
# Local paths
# --------------------------------------------------------------------------- #
def test_walk_files_scandir_matches_rglob(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b").mkdir()
    (tmp_path / "a" / "b" / "deep.txt").write_text("deep", encoding="utf-8")
    (tmp_path / "top.txt").write_text("top", encoding="utf-8")
    (tmp_path / "empty").mkdir()
    walked = {rel for rel, _, _ in _walk_files(tmp_path)}
    rglobbed = {
        p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()
    }
    assert walked == rglobbed == {"a/b/deep.txt", "top.txt"}


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_walk_files_does_not_follow_symlinked_dirs(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "f.txt").write_text("f", encoding="utf-8")
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    assert {rel for rel, _, _ in _walk_files(tmp_path)} == {"real/f.txt"}


def test_directory_fingerprint_tracks_content(tmp_path: Path) -> None:
    b = FileBackend()
    d = tmp_path / "data"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "x.bin").write_bytes(b"12345")
    s1 = b.fingerprint({"uri": str(d)}, None)
    assert s1["type"] == "dir" and s1["count"] == 1 and s1["size"] == 5
    assert b.fingerprint({"uri": str(d)}, None) == s1  # stable
    (d / "y.bin").write_bytes(b"6")
    s2 = b.fingerprint({"uri": str(d)}, None)
    assert s2["count"] == 2 and s2["digest"] != s1["digest"]
    assert b.verify({"uri": str(d)}, s1, None, deep=False).status is (
        VerifyStatus.DRIFTED
    )


def test_missing_local_path_is_backend_error(tmp_path: Path) -> None:
    with pytest.raises(BackendError):
        FileBackend().fingerprint({"uri": str(tmp_path / "nope")}, None)


def test_directory_listing_and_diff(tmp_path: Path) -> None:
    b = FileBackend()
    d = tmp_path / "data"
    (d / "sub").mkdir(parents=True)
    (d / "keep.bin").write_bytes(b"k")
    (d / "grow.bin").write_bytes(b"12")
    (d / "sub" / "gone.bin").write_bytes(b"gone")
    loc = {"uri": str(d)}
    s1 = b.fingerprint(loc, None)
    l1 = b.listing(loc, s1)
    assert l1 is not None
    rows = [json.loads(line) for line in l1.splitlines()]
    assert [r["p"] for r in rows] == ["grow.bin", "keep.bin", "sub/gone.bin"]
    assert rows[0]["s"] == 2 and rows[0]["k"] == hashlib.sha256(b"12").hexdigest()

    (d / "grow.bin").write_bytes(b"1234567")
    (d / "sub" / "gone.bin").unlink()
    (d / "new.bin").write_bytes(b"n" * 2048)
    s2 = b.fingerprint(loc, None)
    l2 = b.listing(loc, s2)

    diff = b.diff(loc, s1, s2, listings=(l1, l2))
    assert diff.unit == "files"
    assert {(e.path, e.change, e.detail) for e in diff.entries} == {
        ("grow.bin", "modified", "+5 B"),
        ("new.bin", "added", "+2.0 KiB"),
        ("sub/gone.bin", "removed", "-4 B"),
    }
    assert b.diff(loc, s2, s2, listings=(l2, l2)).is_empty

    # Without stored listings the diff degrades to a count/size summary.
    fallback = b.diff(loc, s1, s2)
    assert fallback.note == "no stored listing for side a and b"
    assert fallback.entries[0].detail == "3 -> 3 files, +2.0 KiB"

    # Listings are served from the fingerprint cache; a fresh backend can only
    # produce one for the state the object currently has.
    assert b.listing(loc, s1) == l1
    fresh = FileBackend()
    assert fresh.listing(loc, s1) is None  # stale state, not reproducible
    assert fresh.listing(loc, s2) == l2
    assert b.listing(loc, {"type": "file", "size": 1, "sha256": "00"}) is None


def test_single_object_diff(tmp_path: Path) -> None:
    b = FileBackend()
    f = tmp_path / "f.bin"
    f.write_bytes(b"1234")
    loc = {"uri": str(f)}
    s1 = b.fingerprint(loc, None)
    f.write_bytes(b"1234567890")
    s2 = b.fingerprint(loc, None)
    diff = b.diff(loc, s1, s2)
    assert diff.unit == "objects" and diff.modified == 1
    assert diff.entries[0].detail.startswith("size +6 B, sha256 ")
    remote = b.diff(
        {"uri": "s3://b/k"},
        {"type": "object", "size": 1, "etag": "a", "version_id": "v1"},
        {"type": "object", "size": 1, "etag": "b", "version_id": "v2"},
    )
    assert remote.entries[0].detail == "etag a -> b, version v1 -> v2"
    assert b.diff(loc, s2, s2).is_empty


# --------------------------------------------------------------------------- #
# Object stores (S3 / GCS / Azure through obstore; exercised on MemoryStore)
# --------------------------------------------------------------------------- #
def test_parse_remote_schemes() -> None:
    assert _parse("s3://bkt/some/key.bin") == ("s3", "s3://bkt", "some/key.bin")
    assert _parse("gs://bkt/prefix/") == ("gs", "gs://bkt", "prefix/")
    assert _parse("az://container/blob") == ("az", "az://container", "blob")
    assert _parse("abfs://c@acct.dfs.core.windows.net/p/x") == (
        "abfs",
        "abfs://c@acct.dfs.core.windows.net",
        "p/x",
    )
    assert _parse("/local/path")[0] == "local"
    with pytest.raises(BackendError):
        _parse("ftp://host/x")


class _MemoryStores:
    """Stand in for `FileBackend._open_store`: one MemoryStore per root URL."""

    def __init__(self) -> None:
        self.stores: dict[str, MemoryStore] = {}
        self.opened: list[str] = []

    def __call__(self, root: str, locator: Locator) -> MemoryStore:
        self.opened.append(root)
        return self.stores.setdefault(root, MemoryStore())


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> tuple[FileBackend, _MemoryStores]:
    b = FileBackend({"storage_options": {"region": "us-east-1"}})
    stores = _MemoryStores()
    monkeypatch.setattr(b, "_open_store", stores)
    return b, stores


@pytest.mark.parametrize("root", ["s3://bucket", "gs://bucket", "az://container"])
def test_remote_object_and_prefix_fingerprints(
    backend: tuple[FileBackend, _MemoryStores], root: str
) -> None:
    b, stores = backend
    store = stores(root, {})
    obstore.put(store, "raw/plate1/a.tif", b"aaaa")
    obstore.put(store, "raw/plate1/b.tif", b"bb")
    obstore.put(store, "raw/other.tif", b"x")

    obj = b.fingerprint({"uri": f"{root}/raw/plate1/a.tif"}, None)
    assert obj["type"] == "object" and obj["size"] == 4 and obj["etag"]
    assert "version_id" not in obj  # MemoryStore is unversioned

    pre = b.fingerprint({"uri": f"{root}/raw/plate1/"}, None)
    assert pre == {
        "type": "prefix",
        "count": 2,
        "size": 6,
        "digest": pre["digest"],
    }
    # The store is built once per root and reused across objects (the first
    # entry is this test seeding data).
    assert stores.opened == [root, root]

    # A listed prefix only sees its own keys, and drifts when they change.
    obstore.put(store, "raw/plate1/c.tif", b"ccc")
    pre2 = b.fingerprint({"uri": f"{root}/raw/plate1/"}, None)
    assert pre2["count"] == 3 and pre2["digest"] != pre["digest"]
    assert b.verify({"uri": f"{root}/raw/plate1/"}, pre, None, deep=False).status is (
        VerifyStatus.DRIFTED
    )
    assert b.verify({"uri": f"{root}/raw/plate1/"}, pre2, None, deep=False).ok


def test_remote_missing_object(backend: tuple[FileBackend, _MemoryStores]) -> None:
    b, _ = backend
    with pytest.raises(BackendError):
        b.fingerprint({"uri": "s3://bucket/absent"}, None)
    report = b.verify({"uri": "s3://bucket/absent"}, {"type": "object"}, None, False)
    assert report.status is VerifyStatus.MISSING


def test_versioned_object_verify_and_open(
    backend: tuple[FileBackend, _MemoryStores],
) -> None:
    b, stores = backend
    store = stores("s3://bucket", {})
    obstore.put(store, "k", b"payload")
    etag = b.fingerprint({"uri": "s3://bucket/k"}, None)["etag"]
    # Simulate a versioning-enabled bucket having reported a version id.
    state = {"type": "object", "size": 7, "etag": etag, "version_id": "v1"}
    assert b.verify({"uri": "s3://bucket/k"}, state, None, deep=False).status is (
        VerifyStatus.UNKNOWN
    )
    assert b.verify({"uri": "s3://bucket/k"}, state, None, deep=True).ok
    drifted = dict(state, etag="different")
    assert b.verify({"uri": "s3://bucket/k"}, drifted, None, deep=True).status is (
        VerifyStatus.DRIFTED
    )
    gone = dict(state)
    assert (
        b.verify({"uri": "s3://bucket/gone"}, gone, None, deep=True).status
        is VerifyStatus.MISSING
    )
    handle = b.open({"uri": "s3://bucket/k"}, state, read_only=True)
    assert isinstance(handle, FileHandle)
    assert handle.version_id == "v1" and handle.read_only


class RemoteObjectHarness:
    capabilities = Capability.FINGERPRINT | Capability.CHEAP_FINGERPRINT

    def __init__(self, backend: FileBackend, stores: _MemoryStores) -> None:
        self.backend: ObjectBackend = backend
        self.stores = stores
        self._n = 0

    def new_object(self) -> Locator:
        self._n += 1
        store = self.stores("s3://conformance", {})
        obstore.put(store, f"obj{self._n}", b"v0")
        return {"uri": f"s3://conformance/obj{self._n}"}

    def mutate(self, locator: Locator, working_ref: str | None) -> None:
        self._n += 1
        store = self.stores("s3://conformance", {})
        key = locator["uri"].split("s3://conformance/", 1)[1]
        obstore.put(store, key, b"v" * (self._n + 1))


def test_remote_file_conformance(backend: tuple[FileBackend, _MemoryStores]) -> None:
    b, stores = backend
    run_conformance(RemoteObjectHarness(b, stores))


def test_local_states_are_content_hashes_not_mtimes(tmp_path: Path) -> None:
    import hashlib
    import os

    b = FileBackend()
    f = tmp_path / "f.bin"
    f.write_bytes(b"1234")
    s1 = b.fingerprint({"uri": str(f)}, None)
    assert s1 == {
        "type": "file",
        "size": 4,
        "sha256": hashlib.sha256(b"1234").hexdigest(),
    }
    # touch / cp -p-less copies / checkouts change mtime, not content.
    os.utime(f, ns=(1, 1))
    assert b.fingerprint({"uri": str(f)}, None) == s1
    f.write_bytes(b"1235")  # same size, new bytes
    assert b.fingerprint({"uri": str(f)}, None)["sha256"] != s1["sha256"]

    d = tmp_path / "d"
    d.mkdir()
    (d / "a").write_bytes(b"aa")
    s_dir = b.fingerprint({"uri": str(d)}, None)
    os.utime(d / "a", ns=(5, 5))
    assert b.fingerprint({"uri": str(d)}, None) == s_dir  # digest is over hashes
    (d / "a").write_bytes(b"ab")
    assert b.fingerprint({"uri": str(d)}, None)["digest"] != s_dir["digest"]


def test_hash_cache_rereads_only_changed_files(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    b = FileBackend()
    b.configure_cache(cache_dir)
    d = tmp_path / "data"
    d.mkdir()
    for i in range(5):
        (d / f"{i}.bin").write_bytes(bytes([i]) * 10)
    loc = {"uri": str(d)}
    s1 = b.fingerprint(loc, None)
    assert b._hashes.hashed == 5
    assert (cache_dir / "file-hashes.json").is_file()

    # Nothing changed: no file is read.
    assert b.fingerprint(loc, None) == s1 and b._hashes.hashed == 5
    # One file rewritten: one read.
    (d / "2.bin").write_bytes(b"new")
    s2 = b.fingerprint(loc, None)
    assert s2["digest"] != s1["digest"] and b._hashes.hashed == 6
    # A fresh backend on the same workspace inherits the cache from disk.
    b2 = FileBackend()
    b2.configure_cache(cache_dir)
    assert b2.fingerprint(loc, None) == s2 and b2._hashes.hashed == 0
    # Without a cache dir the cache is per process only.
    b3 = FileBackend()
    assert b3.fingerprint(loc, None) == s2 and b3._hashes.hashed == 5
