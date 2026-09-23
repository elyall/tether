"""Icechunk backend (Forkable).

Maps tether onto an Icechunk repository: a branch is the working ref, a commit's
``snapshot_id`` is the state, an immutable tag is the pin, and a branch created
off a tag is a fork. Icechunk tags are immutable and are excluded from snapshot
expiry, which makes them ideal, GC-proof pins.
"""

from __future__ import annotations

import contextlib
import functools
import json
import re
import shutil
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tether.backends.base import (
    Capability,
    HistoryEntry,
    Listings,
    ObjectBackend,
    ObjectDiff,
    VerifyReport,
    VerifyStatus,
    base_at,
    canonical_uri,
    check_expected,
    iso_utc,
    local_path,
    register_backend,
    wrap_library_errors,
)
from tether.errors import BackendError, CapabilityError, RefMovedError
from tether.handles import Handle, IcechunkHandle
from tether.manifest import (
    WORKING_REF_PREFIX,
    Locator,
    Pin,
    Policy,
    State,
    ref_for_pin,
)


@wrap_library_errors
class IcechunkBackend(ObjectBackend):
    kind = "icechunk"
    LOCAL_PATH_KEYS = ("uri",)
    SAFE_CONFIG_KEYS = frozenset()  # credentials and endpoints: secrets.toml only
    capabilities = (
        Capability.FINGERPRINT
        | Capability.ADDRESSABLE
        | Capability.PIN
        | Capability.FORK
        | Capability.ATOMIC_REF
        | Capability.DIFF
        | Capability.HISTORY
        | Capability.PROMOTE
        | Capability.CREATE
    )
    _OWNER_KEY = "tether.owner"
    """Repository metadata key holding the owning dataset id (`create`)."""

    @staticmethod
    def _library_errors() -> tuple[type[BaseException], ...]:
        import icechunk as ic

        return (ic.IcechunkError, OSError)

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}
        self._repos: dict[str, Any] = {}
        self._repo_creds: dict[str, dict[str, str] | None] = {}
        """The credentials each cached `Repository` was opened with."""

    # -- storage / repo -------------------------------------------------- #
    def _uri(self, locator: Locator) -> str:
        uri = locator.get("uri") or locator.get("path")
        if not uri:
            raise BackendError("icechunk locator needs 'uri'", kind="icechunk")
        return str(uri)

    _SCHEMES = ("", "file", "s3")

    def validate_locator(self, locator: Locator) -> None:
        # Refused at `add`, not at the first read of a clone.
        uri = self._uri(locator)
        scheme = urlparse(uri).scheme
        if local_path(uri) is None and scheme != "s3":
            raise BackendError(
                f"unsupported icechunk storage scheme: {scheme!r} (one of "
                f"{', '.join(repr(s) for s in self._SCHEMES)})",
                kind="icechunk",
            )

    def _storage(self, locator: Locator):
        import icechunk as ic

        uri = self._uri(locator)
        path = local_path(uri)
        if path is not None:
            return ic.local_filesystem_storage(path)
        parsed = urlparse(uri)
        if parsed.scheme == "s3":
            # One identity per object: a `secrets.toml` entry (profile, role,
            # or literal keys) resolves to explicit credentials; without one
            # the ambient environment serves, as it always did.
            from tether.credentials import aws_credentials

            secrets = self.secrets_for(locator)
            accepted = _keywords(ic.s3_storage)
            kwargs: dict[str, Any] = {
                "bucket": parsed.netloc,
                "prefix": parsed.path.lstrip("/") or None,
                "region": secrets.get("region") or locator.get("region"),
            }
            if secrets.get("endpoint_url"):
                kwargs["endpoint_url"] = str(secrets["endpoint_url"])
            kwargs.update(self._s3_flags(secrets))
            if self._refreshes(secrets):
                kwargs["get_credentials"] = _RefreshedCredentials(_as_json(secrets))
            elif creds := aws_credentials(secrets):
                kwargs.update(creds)
            else:
                kwargs["from_env"] = True
            missing = sorted(set(kwargs) - accepted) if accepted is not None else []
            if missing:
                raise BackendError(
                    f"this icechunk's s3_storage takes no {', '.join(missing)} "
                    "(set in .tether/secrets.toml); upgrade icechunk",
                    kind="icechunk",
                )
            return ic.s3_storage(**kwargs)
        raise BackendError(
            f"unsupported icechunk storage scheme: {parsed.scheme!r}",
            kind="icechunk",
        )

    _S3_FLAGS = ("allow_http", "force_path_style")
    """`secrets.toml` switches for an S3-compatible server (SeaweedFS, MinIO):
    plain HTTP to its `endpoint_url`, and the bucket in the path rather than
    the host name. Only the untracked file may set them: a clone that could
    would downgrade where your credentials go to HTTP."""

    def _s3_flags(self, secrets: dict[str, Any]) -> dict[str, bool]:
        return {k: True for k in self._S3_FLAGS if _truthy(secrets.get(k))}

    @staticmethod
    def _refreshes(secrets: dict[str, Any]) -> bool:
        """Whether the repository is opened with a `get_credentials` callback:
        a profile or role (keys that expire) and an icechunk that takes one.
        Everything opened from the repository -- a writable session held for
        hours included -- then asks for fresh keys before the old ones
        expire, instead of failing mid-write."""
        import icechunk as ic

        from tether.credentials import refreshable

        accepted = _keywords(ic.s3_storage)
        takes = accepted is None or "get_credentials" in accepted
        return takes and refreshable(secrets)

    def _credentials(self, locator: Locator) -> dict[str, str] | None:
        """What the repository at `locator` is opened with, as `_repo`
        compares it: the static keys; the secrets entry a refresh callback
        resolves (keys it rotates itself do not reopen the repository);
        `None` for local storage or when the ambient environment serves."""
        if local_path(self._uri(locator)) is not None:
            return None
        from tether.credentials import aws_credentials

        secrets = self.secrets_for(locator)
        if self._refreshes(secrets):
            return {"get_credentials": _as_json(secrets)}
        return aws_credentials(secrets)

    def _repo(self, locator: Locator):
        import icechunk as ic

        uri = self._uri(locator)
        creds = self._credentials(locator)
        repo = self._repos.get(uri)
        # Static keys assumed from a role expire (an icechunk without
        # `get_credentials`): `aws_credentials` hands out a fresh set near
        # expiry, and a Repository holding the old ones would fail its next
        # request, so it is rebuilt when the keys change.
        if repo is None or self._repo_creds.get(uri) != creds:
            repo = ic.Repository.open(self._storage(locator))
            self._remember(uri, repo, creds)
        return repo

    def _remember(self, uri: str, repo: Any, creds: dict[str, str] | None) -> None:
        self._repos[uri] = repo
        self._repo_creds[uri] = creds

    def _forget(self, uri: str) -> None:
        self._repos.pop(uri, None)
        self._repo_creds.pop(uri, None)

    def _base_branch(self, locator: Locator) -> str:
        return str(locator.get("branch", "main"))

    base_branch = _base_branch

    def _resolve(self, repo: Any, ref: str) -> str:
        """Resolve a branch, tag, or snapshot id to a snapshot id."""
        import icechunk as ic

        with contextlib.suppress(ic.IcechunkError):
            return str(repo.lookup_branch(ref))
        with contextlib.suppress(ic.IcechunkError):
            return str(repo.lookup_tag(ref))
        try:
            info = next(iter(repo.ancestry(snapshot_id=ref)))
        except (ic.IcechunkError, StopIteration) as exc:
            raise BackendError(
                f"{ref!r} is not a branch, tag, or snapshot id", kind="icechunk"
            ) from exc
        return str(info.id)

    def _refs_by_snapshot(self, repo: Any) -> dict[str, list[str]]:
        import icechunk as ic

        out: dict[str, list[str]] = {}
        for branch in sorted(repo.list_branches()):
            with contextlib.suppress(ic.IcechunkError):
                out.setdefault(str(repo.lookup_branch(branch)), []).append(branch)
        for tag in sorted(repo.list_tags()):
            with contextlib.suppress(ic.IcechunkError):
                out.setdefault(str(repo.lookup_tag(tag)), []).append(tag)
        return out

    # -- protocol -------------------------------------------------------- #
    def identity(self, locator: Locator) -> Locator:
        return {"uri": canonical_uri(self._uri(locator))}

    def fingerprint(self, locator: Locator, working_ref: str | None) -> State:
        repo = self._repo(locator)
        if working_ref is None and (at := base_at(locator)) is not None:
            return {"snapshot_id": self._resolve(repo, at)}
        branch = working_ref or self._base_branch(locator)
        return {"snapshot_id": repo.lookup_branch(branch)}

    def history(
        self,
        locator: Locator,
        ref: str | None = None,
        limit: int = 20,
    ) -> list[HistoryEntry]:
        repo = self._repo(locator)
        start = self._resolve(
            repo, ref or base_at(locator) or self._base_branch(locator)
        )
        refs = self._refs_by_snapshot(repo)
        entries: list[HistoryEntry] = []
        for info in repo.ancestry(snapshot_id=start):
            entries.append(
                HistoryEntry(
                    id=str(info.id),
                    when=iso_utc(info.written_at),
                    message=str(info.message or ""),
                    refs=refs.get(str(info.id), []),
                )
            )
            if len(entries) >= limit:
                break
        return entries

    # Icechunk keeps a tombstone for every deleted tag and never lets the name
    # be reused. A pin id is content-addressed, so the same state would always
    # ask for the same name; when that name is burnt, the pin lives on under a
    # *generation*: `tether.<id>.2`, `.3`, ... The pin id is unchanged (gc and
    # manifests key on it); only the ref differs, and every reader resolves a
    # pin through `_live_tag` rather than trusting `pin.ref` alone.
    _MAX_GENERATIONS = 1000

    @staticmethod
    def _generations(pin_id: str) -> Iterator[str]:
        base = ref_for_pin(pin_id)
        yield base
        for n in range(2, IcechunkBackend._MAX_GENERATIONS + 1):
            yield f"{base}.{n}"

    @staticmethod
    def _pin_id_of(tag: str) -> str | None:
        """The pin id a `tether.*` tag belongs to, generation suffix dropped."""
        prefix = ref_for_pin("")
        if not tag.startswith(prefix):
            return None
        rest = tag[len(prefix) :]
        parts = rest.split(".")
        if len(parts) == 3 and parts[2].isdigit():
            rest = f"{parts[0]}.{parts[1]}"
        return rest

    def _live_tag(self, repo: Any, pin: Pin) -> tuple[str, str] | None:
        """`(tag, snapshot_id)` for the tag that carries `pin` now, or `None`.

        The manifest's `pin.ref` first; failing that, the newest generation of
        the same id that exists (a `repair` after someone deleted the tag).
        """
        import icechunk as ic

        with contextlib.suppress(ic.IcechunkError):
            return pin.ref, str(repo.lookup_tag(pin.ref))
        wanted = self._pin_id_of(pin.ref) or pin.id
        live = [
            t for t in repo.list_tags() if self._pin_id_of(t) == wanted and t != pin.ref
        ]
        for tag in sorted(live, key=_generation_number, reverse=True):
            with contextlib.suppress(ic.IcechunkError):
                return tag, str(repo.lookup_tag(tag))
        return None

    def pin(self, locator: Locator, state: State, pin_id: str) -> Pin:
        import icechunk as ic

        repo = self._repo(locator)
        # The snapshot must exist before any tag name is spent on it: a
        # create_tag failure below then means "name taken", not "no such
        # snapshot", and a missing snapshot does not burn generations.
        sid = self._resolve(repo, str(state["snapshot_id"]))
        for ref in self._generations(pin_id):
            try:
                repo.create_tag(ref, sid)
                return Pin(id=pin_id, ref=ref)
            except ic.IcechunkError:
                pass
            try:
                existing = repo.lookup_tag(ref)
            except ic.IcechunkError:
                continue  # this name is a tombstone; try the next generation
            if existing == sid:
                return Pin(id=pin_id, ref=ref, created=False)  # idempotent commit
            # The name is taken by another snapshot: our state and the id's
            # earlier life disagree. Do not step over it silently.
            raise BackendError(
                f"tag {ref} already points at {existing}, not {sid}",
                kind="icechunk",
            )
        raise BackendError(
            f"pin {pin_id}: every tag name up to generation "
            f"{self._MAX_GENERATIONS} has been used and deleted",
            kind="icechunk",
        )

    def unpin(self, locator: Locator, pin: Pin) -> None:
        import icechunk as ic

        repo = self._repo(locator)
        # Every generation of this id: the plan may know only `tether.<id>`
        # while the live tag is a later one.
        wanted = self._pin_id_of(pin.ref) or pin.id
        errors: list[str] = []
        for tag in {pin.ref, *repo.list_tags()}:
            if tag == pin.ref or self._pin_id_of(tag) == wanted:
                try:
                    repo.delete_tag(tag)
                except ic.IcechunkError as exc:
                    errors.append(f"{tag}: {exc}")
        left = [t for t in repo.list_tags() if self._pin_id_of(t) == wanted]
        if left:  # not "already gone": the store refused
            raise BackendError(
                f"pin {pin.id} was not released ({', '.join(left)} remain): "
                + "; ".join(errors),
                kind="icechunk",
            )

    def list_pins(self, locator: Locator) -> set[str]:
        ids = (self._pin_id_of(t) for t in self._repo(locator).list_tags())
        return {i for i in ids if i is not None}

    def verify(
        self,
        locator: Locator,
        state: State,
        pin: Pin | None,
        deep: bool,
    ) -> VerifyReport:
        import icechunk as ic

        repo = self._repo(locator)
        sid = str(state["snapshot_id"])
        if pin is not None:
            live = self._live_tag(repo, pin)
            if live is None:
                return VerifyReport(VerifyStatus.MISSING, f"tag {pin.ref} missing")
            tag, actual = live
            if actual != sid:
                return VerifyReport(
                    VerifyStatus.DRIFTED, f"{tag} -> {actual}, expected {sid}"
                )
            if tag != pin.ref:
                return VerifyReport(VerifyStatus.OK, f"pinned as {tag}")
            return VerifyReport(VerifyStatus.OK)
        if not deep:
            return VerifyReport(VerifyStatus.UNKNOWN, "pass --deep to read snapshot")
        try:
            repo.readonly_session(snapshot_id=sid)
            return VerifyReport(VerifyStatus.OK)
        except ic.IcechunkError as exc:
            return VerifyReport(VerifyStatus.MISSING, str(exc))

    @staticmethod
    @functools.cache
    def _conditional_reset() -> bool:
        """Whether `Repository.reset_branch` takes `from_snapshot_id` -- the
        compare-and-swap `fork` and `promote` move a branch with. Icechunk
        1.x (the resolution on Python 3.11) lacks it; there the reviewed head
        is compared before an unconditional reset (:func:`check_expected`)."""
        import inspect

        import icechunk as ic

        try:
            params = inspect.signature(ic.Repository.reset_branch).parameters
        except (TypeError, ValueError):  # a builtin with no signature data
            return False
        return "from_snapshot_id" in params

    def _moved_or_failed(
        self, repo: Any, branch: str, from_sid: str, exc: BaseException, *, what: str
    ) -> BackendError:
        """Why `reset_branch(..., from_snapshot_id=from_sid)` failed:
        `RefMovedError` when the branch is not at `from_sid` -- that is what
        the compare-and-swap refuses, and Icechunk's own message does not say
        which snapshot it found -- else the failure itself."""
        import icechunk as ic

        try:
            now: str | None = str(repo.lookup_branch(branch))
        except ic.IcechunkError:
            now = None
        if now == from_sid:
            return BackendError(
                f"{what}: cannot reset branch {branch}: {exc}", kind="icechunk"
            )
        return RefMovedError(
            f"{what}: {branch} is at {now or 'nothing'}, expected {from_sid}",
            kind="icechunk",
        )

    def fork(
        self,
        locator: Locator,
        source: Pin | State,
        name: str,
        *,
        expected: State | None = None,
    ) -> str:
        import icechunk as ic

        repo = self._repo(locator)
        if isinstance(source, Pin):
            sid = self._pin_sid(repo, source)
        else:
            # Recorded state (no tag): the snapshot must still be reachable.
            sid = self._resolve(repo, str(source["snapshot_id"]))
        if expected is None:
            try:
                repo.create_branch(name, sid)
                return name
            except ic.IcechunkError:
                pass
            return self._reset(repo, name, sid)
        if not expected:
            # ABSENT: `create_branch` is itself the conditional write -- it
            # refuses a branch that exists.
            try:
                repo.create_branch(name, sid)
            except ic.IcechunkError as exc:
                if name in repo.list_branches():
                    raise RefMovedError(
                        f"fork: {name} exists, expected absent", kind="icechunk"
                    ) from exc
                raise BackendError(
                    f"cannot create branch {name}: {exc}", kind="icechunk"
                ) from exc
            return name
        from_sid = str(expected["snapshot_id"])
        if from_sid == sid or not self._conditional_reset():
            # Nothing to move (expected at the source already), or no
            # compare-and-swap in this icechunk: a check before the act.
            check_expected(self, locator, expected, ref=name)
            return name if from_sid == sid else self._reset(repo, name, sid)
        try:
            repo.reset_branch(name, sid, from_snapshot_id=from_sid)
        except ic.IcechunkError as exc:
            raise self._moved_or_failed(repo, name, from_sid, exc, what="fork") from exc
        return name

    def _reset(self, repo: Any, name: str, sid: str) -> str:
        """Move branch `name` onto `sid` unconditionally; a branch already
        there is left alone."""
        import icechunk as ic

        try:
            if repo.lookup_branch(name) == sid:
                return name  # already at the source: left alone
            repo.reset_branch(name, sid)
        except ic.IcechunkError as exc:
            raise BackendError(
                f"cannot reset branch {name} to {sid[:12]}: {exc}", kind="icechunk"
            ) from exc
        return name

    def delete_working_ref(self, locator: Locator, ref: str) -> None:
        import icechunk as ic

        if ref == self._base_branch(locator) or ref == "main":
            return
        repo = self._repo(locator)
        error: BaseException | None = None
        try:
            repo.delete_branch(ref)
        except ic.IcechunkError as exc:
            error = exc
        if ref in repo.list_branches():
            raise BackendError(
                f"branch {ref} was not deleted: {error}", kind="icechunk"
            ) from error

    def list_working_refs(self, locator: Locator) -> list[str]:
        branches = self._repo(locator).list_branches()
        return sorted(b for b in branches if b.startswith(WORKING_REF_PREFIX))

    # -- store lifecycle ------------------------------------------------- #
    LAYOUT = frozenset(
        {
            # icechunk 1.x
            "config.yaml",
            "refs",
            # 2.x
            "repo",
            "overwritten",
            # both
            "snapshots",
            "transactions",
            "manifests",
            "chunks",
        }
    )
    """Top-level names an Icechunk repository writes under its prefix (1.x and
    2.x layouts). `delete_store` removes nothing else: a key outside this set
    means something other than this repository lives under the prefix."""

    @staticmethod
    def _lifecycle_api() -> str | None:
        """Why this icechunk cannot do CREATE, or `None` when it can."""
        import icechunk as ic

        missing = [
            name
            for name in ("exists", "create", "metadata", "update_metadata")
            if not hasattr(ic.Repository, name)
        ]
        if missing:
            return (
                "this icechunk lacks Repository."
                + ", Repository.".join(missing)
                + " (2.2.0 is known to work); upgrade to create stores"
            )
        return None

    def effective_capabilities(self, locator: Locator, policy: Policy) -> Capability:
        caps = self.capabilities
        if self._lifecycle_api() is not None:
            caps &= ~Capability.CREATE
        return caps

    def _require_lifecycle_api(self) -> None:
        why = self._lifecycle_api()
        if why is not None:
            raise CapabilityError(why, kind="icechunk")

    def _prefix_store(self, locator: Locator) -> Any:
        """An obstore store rooted at an `s3://` URI, with the object's
        credentials and the server switches `_storage` passes Icechunk."""
        from obstore.store import from_url

        from tether.credentials import storage_options

        secrets = self.secrets_for(locator)
        options: dict[str, Any] = dict(storage_options(secrets))
        flags = self._s3_flags(secrets)
        if flags.get("allow_http"):
            options["client_options"] = {"allow_http": True}
        if flags.get("force_path_style"):
            options["virtual_hosted_style_request"] = False
        return from_url(self._uri(locator), **options)

    def _prefix_keys(self, locator: Locator) -> list[str]:
        """Every key under the URI's prefix (`s3://`), relative to it."""
        import obstore as obs

        store = self._prefix_store(locator)
        return [str(meta["path"]) for page in obs.list(store) for meta in page]

    def create(self, locator: Locator, *, owner: str) -> State:
        import icechunk as ic

        self._require_lifecycle_api()
        uri = self._uri(locator)
        path = local_path(uri)
        # "Anything there" is the refusal, not just "a repository there": a
        # directory with files or a prefix with objects is someone's data, and
        # `delete_store` would later take the whole prefix.
        if path is not None and Path(path).exists():
            raise BackendError(
                f"{path} already exists; `create` never adopts it",
                kind="icechunk",
            )
        if path is None and urlparse(uri).scheme == "s3" and self._prefix_keys(locator):
            raise BackendError(
                f"objects already exist under {uri}; `create` never adopts them",
                kind="icechunk",
            )
        storage = self._storage(locator)
        if ic.Repository.exists(storage):  # pragma: no cover - covered above
            raise BackendError(
                f"an icechunk repository already exists at {uri}; `create` never "
                "adopts one",
                kind="icechunk",
            )
        repo = ic.Repository.create(storage)
        # The marker lives in the repository's own metadata: only a tether that
        # made the store writes it, and a clone's manifest cannot forge it.
        repo.update_metadata({self._OWNER_KEY: owner})
        self._remember(uri, repo, self._credentials(locator))
        return self.fingerprint(locator, None)

    def owner(self, locator: Locator) -> str | None:
        import icechunk as ic

        self._require_lifecycle_api()
        if not ic.Repository.exists(self._storage(locator)):
            return None
        value = self._repo(locator).metadata.get(self._OWNER_KEY)
        return str(value) if value else None

    def is_ref_empty(
        self, locator: Locator, *, ignoring: Collection[str] = ()
    ) -> bool | None:
        import icechunk as ic

        self._require_lifecycle_api()
        if not ic.Repository.exists(self._storage(locator)):
            return None
        repo = self._repo(locator)
        base = self._base_branch(locator)
        stray = [b for b in repo.list_branches() if b not in ignoring and b != base]
        # A plan names a pin as `tether.<id>`; the live tag may be a later
        # generation (`tether.<id>.2`) if that name was burnt. Match on the
        # pin id, as `unpin` does.
        ignored_pins = {self._pin_id_of(r) for r in ignoring}
        stray += [
            t
            for t in repo.list_tags()
            if t not in ignoring and self._pin_id_of(t) not in ignored_pins
        ]
        if stray:
            return False
        # The base branch still at the repository's first snapshot: nothing
        # was ever committed to it.
        return len(list(repo.ancestry(branch=base))) == 1

    _TOLERATED = frozenset({".DS_Store"})
    """Names that are not Icechunk's but not anyone's data either (a Finder
    visit); removed with the store rather than keeping it forever."""

    def _foreign_keys(self, names: Collection[str]) -> list[str]:
        """Top-level names under the prefix that are not Icechunk's."""
        return sorted(
            {
                n
                for n in names
                if n.split("/", 1)[0] not in self.LAYOUT
                and n.split("/", 1)[0] not in self._TOLERATED
            }
        )

    def delete_store(self, locator: Locator) -> None:
        uri = self._uri(locator)
        path = local_path(uri)
        parsed = urlparse(uri)
        # The caller checked; check again here, where the delete is.
        if self.owner(locator) is None or self.is_ref_empty(locator) is not True:
            raise BackendError(
                f"{uri} is not an empty repository tether created; not removing it",
                kind="icechunk",
            )
        if path is not None:
            root = Path(path)
            foreign = self._foreign_keys([p.name for p in root.iterdir()])
            if foreign:
                raise BackendError(
                    f"{uri} holds more than an icechunk repository "
                    f"({', '.join(foreign)}); not removing it",
                    kind="icechunk",
                )
            self._forget(uri)
            shutil.rmtree(root)
            return
        if parsed.scheme == "s3":
            # Icechunk has no "delete repository"; remove the repository's
            # objects under the prefix through obstore -- and only those. A
            # nested or co-located store under the same prefix stops this.
            import obstore as obs

            keys = self._prefix_keys(locator)
            foreign = self._foreign_keys(keys)
            if foreign:
                raise BackendError(
                    f"{uri} holds objects that are not this repository's "
                    f"({', '.join(foreign[:5])}); not removing it",
                    kind="icechunk",
                )
            self._forget(uri)
            if keys:
                obs.delete(self._prefix_store(locator), keys)
            return
        raise BackendError(
            f"unsupported icechunk storage scheme: {parsed.scheme!r}", kind="icechunk"
        )

    PROMOTE_HINT = (
        "Icechunk has no merge; re-apply the writes on a fresh fork of the base "
        "branch, or reset the base with repo.reset_branch() if losing its newer "
        "snapshots is intended"
    )

    def _pin_sid(self, repo: Any, pin: Pin) -> str:
        live = self._live_tag(repo, pin)
        if live is None:
            raise BackendError(f"icechunk tag {pin.ref} not found", kind="icechunk")
        return live[1]

    def _source_sid(self, repo: Any, source: str | Pin | State) -> str:
        if isinstance(source, Pin):
            return self._pin_sid(repo, source)
        if isinstance(source, dict):
            return self._resolve(repo, str(source["snapshot_id"]))
        return self._resolve(repo, source)

    def ancestor_of(
        self, locator: Locator, ancestor: State, descendant: str | Pin | State
    ) -> bool | None:
        repo = self._repo(locator)
        target = self._source_sid(repo, descendant)
        wanted = str(ancestor["snapshot_id"])
        return any(str(info.id) == wanted for info in repo.ancestry(snapshot_id=target))

    def promote(
        self,
        locator: Locator,
        source: str | Pin | State,
        *,
        expected: State | None = None,
    ) -> State:
        import icechunk as ic

        repo = self._repo(locator)
        base = self._base_branch(locator)
        head = str(repo.lookup_branch(base))
        from_sid = None if expected is None else str(expected.get("snapshot_id", ""))
        if from_sid is not None and head != from_sid:
            raise RefMovedError(
                f"promote: {base} is at {head}, expected {from_sid or 'absent'}",
                kind="icechunk",
            )
        target = self._source_sid(repo, source)
        if target == head:
            return {"snapshot_id": head}
        if not self.ancestor_of(locator, {"snapshot_id": head}, target):
            raise BackendError(
                f"{base} moved to {head}, which is not an ancestor of {target}; "
                f"{self.PROMOTE_HINT}",
                kind="icechunk",
            )
        if from_sid is None or not self._conditional_reset():
            # No reviewed head to hold the base to (or no compare-and-swap in
            # this icechunk): the ancestry check above is the whole guard.
            repo.reset_branch(base, target)
            return {"snapshot_id": target}
        # The base moves only if it still holds the head the ancestry was
        # checked against; a commit that landed in between fails the reset
        # instead of vanishing from the base's history.
        try:
            repo.reset_branch(base, target, from_snapshot_id=from_sid)
        except ic.IcechunkError as exc:
            raise self._moved_or_failed(
                repo, base, from_sid, exc, what="promote"
            ) from exc
        return {"snapshot_id": target}

    def open(
        self,
        locator: Locator,
        target: str | Pin | State | None,
        read_only: bool,
    ) -> Handle:

        repo = self._repo(locator)
        if isinstance(target, Pin):
            live = self._live_tag(repo, target)
            if live is None:
                raise BackendError(
                    f"icechunk tag {target.ref} not found", kind="icechunk"
                )
            tag, _sid = live
            session = repo.readonly_session(tag=tag)
            return IcechunkHandle(
                key=self._uri(locator),
                read_only=True,
                repository=repo,
                session=session,
                tag=tag,
                snapshot_id=session.snapshot_id,
            )
        if isinstance(target, dict):
            sid = str(target["snapshot_id"])
            session = repo.readonly_session(snapshot_id=sid)
            return IcechunkHandle(
                key=self._uri(locator),
                read_only=True,
                repository=repo,
                session=session,
                snapshot_id=sid,
            )
        if target is None and read_only and (at := base_at(locator)) is not None:
            sid = self._resolve(repo, at)
            session = repo.readonly_session(snapshot_id=sid)
            return IcechunkHandle(
                key=self._uri(locator),
                read_only=True,
                repository=repo,
                session=session,
                snapshot_id=sid,
            )
        branch = target or self._base_branch(locator)
        if read_only:
            session = repo.readonly_session(branch=branch)
        else:
            session = repo.writable_session(branch)
        return IcechunkHandle(
            key=self._uri(locator),
            read_only=read_only,
            repository=repo,
            session=session,
            branch=branch,
            snapshot_id=session.snapshot_id,
        )

    def diff(
        self,
        locator: Locator,
        a: State,
        b: State,
        *,
        listings: Listings = (None, None),
    ) -> ObjectDiff:
        import icechunk as ic

        sid_a, sid_b = str(a["snapshot_id"]), str(b["snapshot_id"])
        out = ObjectDiff(unit="nodes")
        if sid_a == sid_b:
            return out
        repo = self._repo(locator)
        try:
            d = repo.diff(from_snapshot_id=sid_a, to_snapshot_id=sid_b)
        except ic.IcechunkError as exc:
            # Icechunk only diffs along one line of history. Two branches'
            # heads share an ancestor: report what either side changed since it.
            base = self._common_ancestor(repo, sid_a, sid_b)
            if base is None:
                raise BackendError(
                    f"icechunk diff failed: {exc}", kind="icechunk"
                ) from exc
            return self._divergent_diff(repo, base, sid_a, sid_b)
        _collect(out, _entries(d))
        return out

    @staticmethod
    def _common_ancestor(repo: Any, sid_a: str, sid_b: str) -> str | None:
        """Newest snapshot in both histories, or `None` if they share none."""
        in_b = {str(info.id) for info in repo.ancestry(snapshot_id=sid_b)}
        for info in repo.ancestry(snapshot_id=sid_a):
            if str(info.id) in in_b:
                return str(info.id)
        return None

    @staticmethod
    def _divergent_diff(repo: Any, base: str, sid_a: str, sid_b: str) -> ObjectDiff:
        """`a -> b` for snapshots on different branches, via their common base.

        A node only `b` touched keeps `b`'s change; one only `a` touched is
        reported inverted (what `a` added is absent in `b`); one both touched
        is `modified`, with both sides' chunk counts summed.
        """
        out = ObjectDiff(unit="nodes")
        side_a = _entries(repo.diff(from_snapshot_id=base, to_snapshot_id=sid_a))
        side_b = _entries(repo.diff(from_snapshot_id=base, to_snapshot_id=sid_b))
        inverted = {"added": "removed", "removed": "added"}
        merged: dict[str, tuple[str, str]] = {}
        for path in set(side_a) | set(side_b):
            if path not in side_a:
                merged[path] = side_b[path]
            elif path not in side_b:
                change, detail = side_a[path]
                merged[path] = (inverted.get(change, change), detail)
            else:
                (change_a, detail_a), (change_b, detail_b) = side_a[path], side_b[path]
                if change_a == "removed" and change_b == "removed":
                    continue  # gone on both sides
                if change_b == "removed":
                    merged[path] = ("removed", detail_b)  # `a` still has it
                elif change_a == "removed":
                    merged[path] = ("added", detail_b)  # only `b` has it
                else:
                    merged[path] = ("modified", _join_details(detail_a, detail_b))
        _collect(out, merged)
        out.note = f"diverged at snapshot {base}; changes on either side"
        return out


@functools.cache
def _keywords(fn: Any) -> frozenset[str] | None:
    """The keyword arguments `fn` takes; `None` when it takes any. Icechunk
    adds `s3_storage` keywords from release to release (the lock pins 1.1 on
    Python 3.11 and 2.2 above), so what the installed one lacks is named in
    a refusal rather than left to a `TypeError`."""
    import inspect

    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):  # no signature data: let the call decide
        return None
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return None
    return frozenset(p.name for p in params)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _as_json(options: dict[str, Any]) -> str:
    return json.dumps(options, sort_keys=True, default=str)


@dataclass(frozen=True)
class _RefreshedCredentials:
    """Icechunk's `get_credentials`: the keys an object's `secrets.toml`
    profile or role resolves to now (`tether.credentials`, cached until
    near expiry), and when to ask again -- `REFRESH_MARGIN` before they
    expire. Plain data, so it pickles with the storage when Icechunk sends a
    session to another process."""

    options: str
    """The object's secrets entry, as JSON."""

    def __call__(self) -> Any:
        import icechunk as ic

        from tether.credentials import REFRESH_MARGIN, aws_credentials_expiring

        creds, expires = aws_credentials_expiring(json.loads(self.options))
        if creds is None:  # pragma: no cover - only built for a profile or role
            raise BackendError("no credentials to refresh", kind="icechunk")
        return ic.S3StaticCredentials(
            access_key_id=creds["access_key_id"],
            secret_access_key=creds["secret_access_key"],
            session_token=creds.get("session_token"),
            expires_after=(
                None
                if expires is None
                else datetime.fromtimestamp(expires - REFRESH_MARGIN, tz=UTC)
            ),
        )


def _generation_number(tag: str) -> int:
    """`tether.<id>` is generation 1; `tether.<id>.N` is N."""
    parts = tag.split(".")
    return int(parts[-1]) if len(parts) == 4 and parts[-1].isdigit() else 1


def _entries(d: Any) -> dict[str, tuple[str, str]]:
    """Flatten an `icechunk.Diff` into `path -> (change, detail)`."""
    chunks = dict(getattr(d, "updated_chunks", {}) or {})
    entries: dict[str, tuple[str, str]] = {}
    for path in d.new_groups:
        entries[path] = ("added", "group")
    for path in d.new_arrays:
        entries[path] = ("added", "array")
    for path in d.deleted_groups:
        entries[path] = ("removed", "group")
    for path in d.deleted_arrays:
        entries[path] = ("removed", "array")
    for path in d.updated_groups:
        entries[path] = ("modified", "group metadata")
    for path in set(d.updated_arrays) | set(chunks):
        if path in entries:  # a new array's chunks: it is added, not modified
            continue
        parts = []
        if path in d.updated_arrays:
            parts.append("array metadata")
        if path in chunks:
            parts.append(f"{len(chunks[path])} chunks")
        entries[path] = ("modified", ", ".join(parts))
    for moved in getattr(d, "moved_nodes", []) or []:
        entries[f"{moved[0]} -> {moved[1]}"] = ("renamed", "")
    return entries


def _collect(out: ObjectDiff, entries: dict[str, tuple[str, str]]) -> None:
    order = {"added": 0, "removed": 1, "modified": 2, "renamed": 3}
    for path, (change, detail) in sorted(
        entries.items(), key=lambda kv: (order.get(kv[1][0], 9), kv[0])
    ):
        out.add(path, change, detail)


def _join_details(a: str, b: str) -> str:
    """Sum `N chunks` across two sides; keep any other wording once."""
    total = 0
    words: list[str] = []
    for detail in (a, b):
        for part in filter(None, (p.strip() for p in detail.split(","))):
            m = re.fullmatch(r"(\d+) chunks", part)
            if m:
                total += int(m.group(1))
            elif part not in words:
                words.append(part)
    if total:
        words.append(f"{total} chunks")
    return ", ".join(words)


def _factory(config: dict) -> IcechunkBackend:
    return IcechunkBackend(config)


register_backend("icechunk", _factory)
