from __future__ import annotations

import uuid
from pathlib import Path

from tether.backends.base import Capability, ObjectBackend
from tether.backends.memory import MemoryBackend, MemoryStore
from tether.manifest import Locator
from tether.testing import run_conformance


class MemoryHarness:
    def __init__(self) -> None:
        self.store = MemoryStore()
        self.backend: ObjectBackend = MemoryBackend(self.store)
        self._n = 0

    def new_object(self) -> Locator:
        name = f"sys-{uuid.uuid4().hex[:8]}"
        self.store.system(name)  # materialize with a main branch
        return {"system": name, "branch": "main"}

    def mutate(self, locator: Locator, working_ref: str | None) -> None:
        self._n += 1
        branch = working_ref or locator.get("branch", "main")
        self.store.write(locator["system"], branch, {"n": self._n})


class LocalFileHarness:
    capabilities = Capability.FINGERPRINT | Capability.CHEAP_FINGERPRINT

    def __init__(self, tmp: Path) -> None:
        from tether.backends.file import FileBackend

        self.backend: ObjectBackend = FileBackend()
        self.tmp = tmp
        self._n = 0

    def new_object(self) -> Locator:
        p = self.tmp / f"f-{uuid.uuid4().hex[:8]}.txt"
        p.write_text("v0", encoding="utf-8")
        return {"uri": str(p)}

    def mutate(self, locator: Locator, working_ref: str | None) -> None:
        self._n += 1
        Path(locator["uri"]).write_text(f"v{self._n}" * (self._n + 1), encoding="utf-8")


def test_memory_backend_conformance() -> None:
    run_conformance(MemoryHarness())


def test_file_backend_conformance(tmp_path: Path) -> None:
    run_conformance(LocalFileHarness(tmp_path))
