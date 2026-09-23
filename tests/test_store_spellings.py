"""One store, every spelling: `/p`, `/p/`, `file:///p` and `/link/p` through a
symlinked parent (macOS's `/tmp` is `/private/tmp`) are one identity to pin
ids, listings, ref namespaces and `gc`; and `gc` never releases a pin some
manifest names, whatever store that manifest spells it in."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

from tether.backends.base import ObjectBackend, build_backend, canonical_uri
from tether.manifest import listing_name, write_object
from tether.repo import Repo


def _spellings(real: Path, link_parent: Path) -> list[str]:
    """Every way a manifest may name the directory `real` (which exists)."""
    out = [
        str(real),
        f"{real}/",
        f"{real}//",
        f"file://{real}",
        f"file://localhost{real}/",
        f"{real.parent}/./{real.name}",
        f"{real.parent}/nope/../{real.name}",
        str(link_parent / real.name),
        f"file://{link_parent / real.name}/",
    ]
    if sys.platform == "darwin" and str(real).startswith("/private/"):
        out.append(str(real)[len("/private") :])  # /var/... and /tmp/...
    return out


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    real = tmp_path.resolve() / "real" / "store"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real.parent, target_is_directory=True)
    return real, link


@pytest.mark.parametrize("kind", ["icechunk", "lance", "delta", "file"])
def test_every_spelling_of_a_local_store_is_one_identity(
    tmp_path: Path, kind: str
) -> None:
    pytest.importorskip({"lance": "lance", "delta": "deltalake"}.get(kind, "obstore"))
    if kind == "icechunk":
        pytest.importorskip("icechunk")
    real, link = _dirs(tmp_path)
    backend: ObjectBackend = build_backend(kind)
    want = {"uri": str(real)}
    for uri in _spellings(real, link):
        loc = {"uri": uri}
        assert canonical_uri(uri) == str(real), uri
        assert backend.identity(loc) == want, uri
        assert backend.ref_namespace(loc) == backend.ref_namespace({"uri": str(real)})
        assert backend.branch_scope(loc) == backend.branch_scope({"uri": str(real)})
        state = {"type": "dir", "digest": "d"}
        assert listing_name(kind, backend.identity(loc), state) == listing_name(
            kind, want, state
        )
    # Remote URLs are not paths: kept as written.
    for uri in ("s3://bucket/p", "s3://bucket/p/", "gs://b/x#1"):
        assert canonical_uri(uri) == uri


def test_a_relative_manifest_path_is_normalized_but_not_resolved() -> None:
    """Only a hand-written manifest holds one; which directory it means is
    the dataset root's business (`Repo._resolve_locator`), not the cwd's."""
    assert canonical_uri("data/p/") == "data/p"
    assert canonical_uri("./data//p") == "data/p"
    assert canonical_uri("file:data/p/") == "data/p"


def _icechunk_store(path: Path) -> None:
    ic = pytest.importorskip("icechunk")
    zarr = pytest.importorskip("zarr")
    repo = ic.Repository.create(ic.local_filesystem_storage(str(path)))
    session = repo.writable_session("main")
    zarr.create_group(store=session.store).attrs["v"] = 0
    session.commit("init")


def _icechunk_write(path: Path, value: int) -> None:
    import icechunk as ic
    import zarr

    repo = ic.Repository.open(ic.local_filesystem_storage(str(path)))
    session = repo.writable_session("main")
    zarr.open_group(store=session.store, mode="a").attrs["v"] = value
    session.commit(f"v={value}")


def _lance_store(path: Path) -> None:
    lance = pytest.importorskip("lance")
    pa = pytest.importorskip("pyarrow")
    lance.write_dataset(pa.table({"a": [0]}), str(path))


def _lance_write(path: Path, value: int) -> None:
    import lance
    import pyarrow as pa

    lance.write_dataset(pa.table({"a": [value]}), str(path), mode="append")


_STORES = {
    "icechunk": (_icechunk_store, _icechunk_write),
    "lance": (_lance_store, _lance_write),
}


@pytest.mark.parametrize("kind", sorted(_STORES))
def test_gc_keeps_every_pin_of_a_store_spelled_several_ways(
    vcs_root: Path, tmp_path_factory: pytest.TempPathFactory, kind: str
) -> None:
    """The review's repro, as a class: commits whose manifests name one
    store by each spelling -- a b3 `file://` manifest, a teammate's `add`
    with a trailing slash, a path through a symlinked parent. Plain `gc`
    and `gc --delete-stores` release none of their pins, and every one
    verifies."""
    make, write = _STORES[kind]
    real, link = _dirs(tmp_path_factory.mktemp("stores"))
    real.rmdir()
    make(real)
    repo = Repo.init(vcs_root)
    repo.add("obj", kind, {"uri": str(real)})
    pins = [repo.commit("spelled plainly").pinned["obj"]]
    for n, uri in enumerate(
        [f"{real}/", f"file://{real}", str(link / real.name)], start=1
    ):
        m = repo.objects["obj"]
        m.locator["uri"] = uri
        write_object(repo.root, m)  # a manifest spelled another way
        write(real, n)
        pins.append(Repo.find(vcs_root).commit(f"spelled {uri}").pinned["obj"])
    repo = Repo.find(vcs_root)
    ids = {p.id for p in pins if p is not None}
    assert len(ids) == 4
    backend = repo.backend_for(kind)
    assert ids <= backend.list_pins({"uri": str(real)})
    for plan in (repo.plan_gc(), repo.plan_gc(delete_stores=True)):
        assert not [a for a in plan.actions if a.op == "unpin"], plan.render()
    repo.gc(dry_run=False, delete_stores=True)
    assert ids <= backend.list_pins({"uri": str(real)})
    assert all(r.ok for r in repo.verify(all_history=True).values())


def test_gc_never_unpins_a_pin_any_manifest_names_whatever_its_namespace(
    vcs_root: Path,
) -> None:
    """Whatever normalization misses, one store listed under two namespaces
    is two views of one set of pins. A backend whose identity is the whole
    locator puts the same memory system in a new namespace when a locator
    field changes; each namespace's listing holds the other's pins, and gc
    used to release those."""
    from tether.backends.base import register_backend
    from tether.backends.memory import MemoryBackend, default_store
    from tether.handles import MemoryHandle

    class Spelled(MemoryBackend):
        kind = "spelled"

        def identity(self, locator: dict) -> dict:
            return dict(locator)

    register_backend("spelled", lambda config: Spelled(store=default_store()))
    system = f"sys-{uuid.uuid4().hex[:8]}"
    default_store().system(system)
    repo = Repo.init(vcs_root)
    repo.add("db", "spelled", {"system": system, "branch": "main"})
    first = repo.commit("one spelling").pinned["db"]
    m = repo.objects["db"]
    m.locator["note"] = "the same system"
    write_object(repo.root, m)
    handle = Repo.find(vcs_root).open("db")
    assert isinstance(handle, MemoryHandle)
    handle.write({"v": 2})
    second = Repo.find(vcs_root).commit("another").pinned["db"]
    assert first and second and first.id != second.id
    repo = Repo.find(vcs_root)
    b = repo.backend_for("spelled")
    assert b.ref_namespace({"system": system, "branch": "main"}) != b.ref_namespace(
        repo.objects["db"].locator
    )
    plan = repo.plan_gc()
    assert not [a for a in plan.actions if a.op == "unpin"], plan.render()
    tags = default_store().system(system).tags
    assert first.ref in tags and second.ref in tags


def test_delete_stores_knows_an_indexed_store_by_its_current_identity(
    vcs_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The created- and touched-store indexes record the identity a store
    had when it was indexed; identities have been normalized since. A store
    a manifest names under today's identity is in use, whatever the index
    recorded -- an empty created store used to be deleted from under it."""
    from tether.experimental.lifecycle import CreatedStore, append_created

    real = tmp_path_factory.mktemp("stores").resolve() / "made"
    repo = Repo.init(vcs_root)
    repo.add("files", "file", {"uri": str(real)}, create=True)
    repo.commit("the store")
    shared = repo.vcs.shared_dir()
    index = shared / "tether-created.jsonl"
    index.unlink()
    old = f"file://{real}/"  # the spelling, and identity, b3 recorded
    append_created(
        shared,
        CreatedStore(
            dataset_id=repo.config.dataset_id,
            kind="file",
            identity={"uri": old},
            locator={"uri": old},
            key="files",
            at="",
        ),
    )
    plan = Repo.find(vcs_root).plan_gc(delete_stores=True)
    assert not [a for a in plan.actions if a.op == "delete-store"], plan.render()
    Repo.find(vcs_root).gc(dry_run=False, delete_stores=True)
    assert real.is_dir()
