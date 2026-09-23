from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import timedelta
from pathlib import Path

import pytest

from tether.backends.base import Capability, ObjectBackend, VerifyStatus
from tether.backends.file import FileBackend, _parse, _walk_files
from tether.errors import BackendError, ConfigError
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


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_directory_digest_records_unfollowed_symlinks_by_target(
    tmp_path: Path,
) -> None:
    b = FileBackend()
    d = tmp_path / "data"
    d.mkdir()
    (d / "a.bin").write_bytes(b"A")
    ext1, ext2 = tmp_path / "ext1", tmp_path / "ext2"
    for ext, body in ((ext1, b"one"), (ext2, b"two")):
        ext.mkdir()
        (ext / "big.bin").write_bytes(body)
    (d / "linked").symlink_to(ext1, target_is_directory=True)
    loc = {"uri": str(d)}
    s0 = b.fingerprint(loc, None)
    assert (s0["count"], s0["size"]) == (2, 1)
    rows = {r["p"]: r for r in map(json.loads, (b.listing(loc, s0) or "").splitlines())}
    assert rows["linked"]["k"].startswith("symlink:") and rows["linked"]["s"] == 0

    (d / "linked").unlink()
    (d / "linked").symlink_to(ext2, target_is_directory=True)
    s1 = b.fingerprint(loc, None)
    assert s1["digest"] != s0["digest"]
    (ext2 / "big.bin").write_bytes(b"not followed")
    assert b.fingerprint(loc, None) == s1

    (d / "dangling").symlink_to(tmp_path / "nowhere")
    s2 = b.fingerprint(loc, None)
    assert s2["digest"] != s1["digest"] and s2["count"] == 3
    diff = b.diff(loc, s1, s2, listings=(b.listing(loc, s1), b.listing(loc, s2)))
    assert [(e.path, e.change) for e in diff.entries] == [("dangling", "added")]

    # A symlink to a file is still read through: its content is what counts.
    (ext1 / "f.bin").write_bytes(b"x")
    (d / "file_link").symlink_to(ext1 / "f.bin")
    s3 = b.fingerprint(loc, None)
    (ext1 / "f.bin").write_bytes(b"y")
    assert b.fingerprint(loc, None)["digest"] != s3["digest"]


def test_hash_cache_distrusts_entries_hashed_near_a_change(tmp_path: Path) -> None:
    """On a filesystem whose timestamps tick coarsely, a same-size rewrite in
    the tick after hashing leaves every stat field as cached. Any entry hashed
    within the window of the file's last change is re-read, whatever the
    timestamp granularity."""
    from tether.backends.file import _HashCache

    f = tmp_path / "f.bin"
    f.write_bytes(b"old!")
    now = time.time_ns()
    fine = now // 1_000_000_000 * 1_000_000_000 + 123_456_789  # not whole seconds
    os.utime(f, ns=(fine, fine))
    st = f.stat()
    cache = _HashCache(None)
    cache.record(f, st, "hash-of-old")
    assert cache.lookup(f, st) is None  # hashed right after the change: racy
    entries = cache._load()
    entries[str(f)][5] = max(st.st_mtime_ns, st.st_ctime_ns) + 3_000_000_000
    assert cache.lookup(f, st) == "hash-of-old"


def test_open_store_passes_client_options_apart(tmp_path: Path) -> None:
    """obstore takes `allow_http` in `client_options`; as a store config key it
    panics (a BaseException that escapes error wrapping)."""
    b = FileBackend({"storage_options": {"region": "us-east-1", "allow_http": True}})
    store = b._open_store("s3://bucket", {})
    assert type(store).__name__ == "S3Store"


@pytest.mark.parametrize(
    ("root", "key"),
    [
        ("s3://bucket", "allow_http"),
        ("s3://bucket", "ALLOW_HTTP"),
        ("s3://bucket", "AWS_ALLOW_HTTP"),
        ("s3://bucket", "aws_allow_http"),
        ("s3://bucket", "Aws_Allow_Http"),
        ("gs://bucket", "GOOGLE_ALLOW_HTTP"),
        ("az://container", "azure_allow_http"),
    ],
)
@pytest.mark.parametrize("value", [True, "true", "TRUE", 1, "yes"])
def test_client_options_are_routed_in_any_spelling(
    root: str, key: str, value: object
) -> None:
    """Only the lowercase `allow_http` reached `client_options`; the other
    spellings obstore accepts elsewhere (`AWS_ALLOW_HTTP`) went to the store
    config, where they raised pyo3's `PanicException`."""
    options: dict[str, object] = {key: value}
    if root.startswith("s3"):
        options["region"] = "us-east-1"
    elif root.startswith("az"):
        options["account_name"] = "acct"
    store = FileBackend({"storage_options": options})._open_store(root, {})
    assert str(store.client_options["allow_http"]).lower() == "true"


@pytest.mark.parametrize(
    ("given", "passed"),
    [
        ({"timeout": 5}, {"timeout": "5000ms"}),
        ({"timeout": 2.5}, {"timeout": "2500ms"}),
        ({"TIMEOUT": "5"}, {"timeout": "5000ms"}),
        ({"timeout": "2h 37min"}, {"timeout": "2h 37min"}),
        ({"aws_connect_timeout": timedelta(seconds=3)}, {"connect_timeout": "3000ms"}),
        ({"http2_keep_alive_interval": 30}, {"http2_keep_alive_interval": "30000ms"}),
        ({"pool_max_idle_per_host": 10}, {"pool_max_idle_per_host": "10"}),
        ({"http1_only": 0}, {"http1_only": "false"}),
        (
            {"proxy_excludes": ["localhost", ".svc"]},
            {"proxy_excludes": "localhost,.svc"},
        ),
        (
            {"client_options": {"User_Agent": "tether", "TIMEOUT": 1}},
            {"user_agent": "tether", "timeout": "1000ms"},
        ),
    ],
)
def test_client_option_values_are_coerced_to_what_obstore_takes(
    given: dict[str, object], passed: dict[str, object]
) -> None:
    """`timeout = 5` in TOML is an integer; obstore wants a duration string
    (or a timedelta) and raised `TypeError`. Numbers are seconds."""
    store = FileBackend(
        {"storage_options": {"region": "us-east-1", **given}}
    )._open_store("s3://bucket", {})
    assert {k: store.client_options[k] for k in passed} == passed


@pytest.mark.parametrize(
    "options",
    [
        {"bogus": 1},
        {"client_options": {"bogus": 1}},
        {"timeout": "banana"},
        {"timeout": True},
        {"allow_http": "maybe"},
        {"default_headers": "x"},
    ],
)
def test_bad_storage_options_raise_config_error_not_a_panic(
    options: dict[str, object],
) -> None:
    b = FileBackend({"storage_options": {"region": "us-east-1", **options}})
    with pytest.raises(ConfigError, match="file"):
        b._open_store("s3://bucket", {})


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


def test_local_paths_with_url_characters_are_not_truncated(tmp_path: Path) -> None:
    """`#` and `?` are ordinary characters in a path; run through a URL parser
    they cut the path short, and `…/run#1` silently hashed `…/run`."""
    b = FileBackend()
    plain = tmp_path / "run"
    plain.mkdir()
    (plain / "a.bin").write_bytes(b"plain")
    for name in ("run#1", "run?x=1"):
        d = tmp_path / name
        d.mkdir()
        (d / "a.bin").write_bytes(name.encode())
        state = b.fingerprint({"uri": str(d)}, None)
        assert state["count"] == 1
        assert state["digest"] != b.fingerprint({"uri": str(plain)}, None)["digest"]
    assert _parse(str(tmp_path / "run#1")) == ("local", "", str(tmp_path / "run#1"))


def test_local_uri_spellings_share_one_identity(tmp_path: Path) -> None:
    """`/p` and `file:///p` are one directory, so they are one object to pin
    ids, listings and `gc`: the identity is the path form."""
    from tether.backends.base import canonical_uri, local_path

    b = FileBackend()
    d = tmp_path / "data"
    d.mkdir()
    (d / "a.bin").write_bytes(b"1")
    as_path, as_uri = {"uri": str(d)}, {"uri": f"file://{d}"}
    assert b.identity(as_path) == b.identity(as_uri) == {"uri": str(d)}
    assert b.fingerprint(as_path, None) == b.fingerprint(as_uri, None)
    assert local_path(f"file://{d}") == str(d)
    assert local_path(f"file://localhost{d}") == str(d)
    assert local_path("/data/run#1") == "/data/run#1"
    assert local_path("relative/dir") == "relative/dir"
    assert local_path("s3://bucket/key") is None
    assert local_path("file://other-host/share") is None
    assert canonical_uri("s3://bucket/key") == "s3://bucket/key"


def test_create_with_a_file_uri_makes_the_directory_it_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`create` on `file:///p` used to make a relative `file:` directory."""
    monkeypatch.chdir(tmp_path)
    b = FileBackend()
    target = tmp_path / "made"
    b.create({"uri": f"file://{target}"}, owner="0123abcd")
    assert target.is_dir() and b.owner({"uri": str(target)}) == "0123abcd"
    assert not (tmp_path / "file:").exists()
    assert b.is_ref_empty({"uri": f"file://{target}"}) is True
    b.delete_store({"uri": f"file://{target}"})
    assert not target.exists()


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


def test_store_cache_is_per_credential_rule_not_per_bucket(
    backend: tuple[FileBackend, _MemoryStores], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Port of the review's `r_creds.py`: the store cache was keyed on the
    bucket root alone, so two prefixes of one bucket with different
    `[uris."..."]` rules shared whichever store was built first -- the second
    prefix was read with the first prefix's credentials. One store per rule
    (and per locator region); the same rule still shares one store."""
    b, stores = backend
    store = stores("s3://bucket", {})
    obstore.put(store, "a/x", b"1")
    obstore.put(store, "a/y", b"11")
    obstore.put(store, "b/y", b"2")
    b.configure_secrets(
        {},
        {
            "s3://bucket/a/": {"access_key_id": "AKIA_A", "secret_access_key": "sk-a"},
            "s3://bucket/b/": {"access_key_id": "AKIA_B", "secret_access_key": "sk-b"},
        },
    )
    opened_with: list[tuple[str, str | None, object]] = []

    def recording(root: str, locator: Locator) -> MemoryStore:
        rule = b.secrets_for(locator).get("access_key_id")
        opened_with.append((root, rule, locator.get("region")))
        return stores(root, locator)

    monkeypatch.setattr(b, "_open_store", recording)
    b.fingerprint({"uri": "s3://bucket/a/x"}, None)
    b.fingerprint({"uri": "s3://bucket/b/y"}, None)
    b.fingerprint({"uri": "s3://bucket/a/y"}, None)  # same rule: same store
    b.fingerprint({"uri": "s3://bucket/b/y", "region": "eu-west-1"}, None)
    assert opened_with == [
        ("s3://bucket", "AKIA_A", None),
        ("s3://bucket", "AKIA_B", None),
        ("s3://bucket", "AKIA_B", "eu-west-1"),
    ]
    # An object no rule covers uses the environment: one more store, shared.
    obstore.put(store, "c/z", b"3")
    b.fingerprint({"uri": "s3://bucket/c/z"}, None)
    b.fingerprint({"uri": "s3://bucket/c/z"}, None)
    assert opened_with[-1] == ("s3://bucket", None, None) and len(opened_with) == 4


def test_store_is_rebuilt_when_the_rule_resolves_to_fresh_credentials(
    backend: tuple[FileBackend, _MemoryStores], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keys assumed from a role expire; `aws_credentials` hands out a fresh
    set near expiry, and a store signing with the old ones would start
    failing. The cache compares the resolved keys, not only the rule."""
    from tether import credentials

    b, stores = backend
    obstore.put(stores("s3://bucket", {}), "k", b"1")
    b.configure_secrets({}, {"s3://bucket/": {"role_arn": "arn:aws:iam::1:role/r"}})
    current = {
        "access_key_id": "ASIA1",
        "secret_access_key": "s1",
        "session_token": "t1",
    }
    monkeypatch.setattr(credentials, "aws_credentials", lambda options: dict(current))
    opened = 0

    def counting(root: str, locator: Locator) -> MemoryStore:
        nonlocal opened
        opened += 1
        return stores(root, locator)

    monkeypatch.setattr(b, "_open_store", counting)
    loc = {"uri": "s3://bucket/k"}
    b.fingerprint(loc, None)
    b.fingerprint(loc, None)
    assert opened == 1
    current = {
        "access_key_id": "ASIA2",
        "secret_access_key": "s2",
        "session_token": "t2",
    }
    b.fingerprint(loc, None)
    b.fingerprint(loc, None)
    assert opened == 2
    # One store per rule, replaced by each generation of keys -- not one kept
    # per refresh for the life of the backend.
    for n in range(3, 8):
        current = {"access_key_id": f"ASIA{n}", "secret_access_key": f"s{n}"}
        b.fingerprint(loc, None)
    assert opened == 7 and len(b._stores) == 1


def test_concurrent_fingerprints_build_one_store_per_generation(
    backend: tuple[FileBackend, _MemoryStores], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine fingerprints objects from 16 threads; each thread that
    found no store built its own."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    b, stores = backend
    for n in range(16):
        obstore.put(stores("s3://bucket", {}), f"k{n}", b"1")
    opened = 0
    gate = threading.Barrier(16, timeout=5)
    lock = threading.Lock()

    def counting(root: str, locator: Locator) -> MemoryStore:
        nonlocal opened
        with lock:
            opened += 1
        return stores(root, locator)

    monkeypatch.setattr(b, "_open_store", counting)

    def fp(n: int) -> dict:
        gate.wait()
        return b.fingerprint({"uri": f"s3://bucket/k{n}"}, None)

    with ThreadPoolExecutor(16) as ex:
        assert len(list(ex.map(fp, range(16)))) == 16
    assert opened == 1


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


@pytest.fixture
def no_racy_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trust entries for files written moments ago, so read counts are exact."""
    monkeypatch.setattr("tether.backends.file._RACY_WINDOW_NS", 0)


@pytest.mark.usefixtures("no_racy_window")
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


def test_hash_cache_sees_through_a_restored_mtime(tmp_path: Path) -> None:
    """Overwrite four bytes and put the mtime back: size and mtime match the
    cached entry, but ctime cannot be restored, so the bytes are re-read."""
    b = FileBackend()
    b.configure_cache(tmp_path / "cache")
    d = tmp_path / "data"
    d.mkdir()
    f = d / "a.bin"
    f.write_bytes(b"0000" + b"x" * 60)
    s1 = b.fingerprint({"uri": str(d)}, None)
    st = f.stat()
    f.write_bytes(b"1111" + b"x" * 60)  # same size
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))  # mtime as before
    s2 = b.fingerprint({"uri": str(d)}, None)
    assert s2["digest"] != s1["digest"]


def test_versioned_policy_addresses_only_remote_objects(tmp_path: Path) -> None:
    """`file = "versioned"` cannot make a local path or a prefix re-openable:
    only an object store hands out version ids. Such objects stay Observed,
    and a recorded object state without a version id is refused by `open`."""
    from tether.backends.base import effective_capabilities
    from tether.manifest import Policy

    b = FileBackend()
    versioned = Policy(file="versioned")
    local = tmp_path / "f.bin"
    local.write_bytes(b"x")
    for loc in ({"uri": str(local)}, {"uri": "s3://bucket/prefix/"}):
        assert Capability.ADDRESSABLE not in effective_capabilities(b, loc, versioned)
    assert Capability.ADDRESSABLE in effective_capabilities(
        b, {"uri": "s3://bucket/one.bin"}, versioned
    )
    with pytest.raises(BackendError, match="no version id"):
        b.open(
            {"uri": "s3://bucket/one.bin"},
            {"type": "object", "size": 1, "etag": "e"},
            read_only=True,
        )
    h = b.open(
        {"uri": "s3://bucket/one.bin"},
        {"type": "object", "size": 1, "etag": "e", "version_id": "v1"},
        read_only=True,
    )
    assert isinstance(h, FileHandle) and h.version_id == "v1"


@pytest.mark.usefixtures("no_racy_window")
def test_hash_cache_is_shared_safely_across_concurrent_fingerprints(
    tmp_path: Path,
) -> None:
    """The engine fingerprints objects concurrently; one cache serves them all.

    Unsynchronised, two objects finishing together raced on the cache's temp
    file (`rename` of a file the other thread had already moved).
    """
    from concurrent.futures import ThreadPoolExecutor

    b = FileBackend()
    b.configure_cache(tmp_path / "cache")
    dirs = []
    for n in range(8):
        d = tmp_path / f"plate{n}"
        d.mkdir()
        for i in range(4):
            (d / f"{i}.bin").write_bytes(bytes([n * 4 + i]) * 16)
        dirs.append({"uri": str(d)})
    for _ in range(3):  # a few rounds to give a race room to show
        with ThreadPoolExecutor(max_workers=8) as pool:
            states = list(pool.map(lambda loc: b.fingerprint(loc, None), dirs))
        assert len({s["digest"] for s in states}) == 8
    assert b._hashes.hashed == 32  # every file read once, then served from cache
    cache = json.loads((tmp_path / "cache" / "file-hashes.json").read_text())
    assert len(cache) == 32


def test_versionless_object_state_is_recorded_as_not_recoverable(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--file versioned` on a bucket that turns out not to version: the
    fingerprint carries no version id, so the commit records the state without
    promising a read it cannot do (recoverable = false), and `open` refuses."""
    from tether.manifest import Policy
    from tether.repo import Repo

    repo = Repo.init(vcs_root)
    repo.add(
        "obj", "file", {"uri": "s3://bucket/one.bin"}, policy=Policy(file="versioned")
    )
    backend = repo.backend_for("file")
    monkeypatch.setattr(
        backend,
        "fingerprint",
        lambda locator, working_ref: {"type": "object", "size": 1, "etag": "e"},
    )
    plan = repo.plan_commit("versionless")
    (record,) = [a for a in plan.actions if a.key == "obj"]
    assert record.op == "record" and record.params["recoverable"] is False
    assert "no address to reopen" in record.detail
    res = repo.apply_commit(plan, verify=False)
    assert "obj" in res.unrecoverable
    assert repo.objects["obj"].recoverable is False
