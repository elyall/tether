from __future__ import annotations

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
