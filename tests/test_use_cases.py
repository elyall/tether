"""The use-cases guide, executed.

Runs the setup and sections 1-3 of ``user_guide/06-use-cases.qmd`` against
local objects -- an Icechunk store, a Lance table, a directory of files, and a
git repository, all under ``tmp_path`` -- and writes every command and its
output to ``user_guide/_generated/06/``, which the guide includes. The test
fails when the fresh output differs from the committed files; run it with
``TETHER_UPDATE_DOCS=1`` to rewrite them::

    TETHER_UPDATE_DOCS=1 uv run pytest tests/test_use_cases.py

Ids that are random per run (jj change and commit ids, Icechunk snapshot
ids, pin hashes, workspace ids) are replaced with stable stand-ins in order
of first appearance, so the files only change when the behaviour does.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from tether.cli import app

ic = pytest.importorskip("icechunk")
zarr = pytest.importorskip("zarr")
lance = pytest.importorskip("lance")
pa = pytest.importorskip("pyarrow")
typer_testing = pytest.importorskip("typer.testing")

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATED = REPO_ROOT / "user_guide" / "_generated" / "06"
UPDATE = os.environ.get("TETHER_UPDATE_DOCS") == "1"

# Fixed clocks and author so commit ids depend only on content.
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Evan",
    "GIT_AUTHOR_EMAIL": "evan@example.com",
    "GIT_COMMITTER_NAME": "Evan",
    "GIT_COMMITTER_EMAIL": "evan@example.com",
    "GIT_AUTHOR_DATE": "2026-09-01T09:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-09-01T09:00:00+00:00",
}
_JJ_ENV = {
    "JJ_USER": "Evan",
    "JJ_EMAIL": "evan@example.com",
    "JJ_TIMESTAMP": "2026-09-01T09:00:00+00:00",
    "JJ_OP_TIMESTAMP": "2026-09-01T09:00:00+00:00",
    "JJ_OP_HOSTNAME": "laptop",
    "JJ_OP_USERNAME": "evan",
}
# `jj log` in the guide uses this compact template: change id, commit id,
# local bookmarks, other workspaces' working copies, (empty), description.
_JJ_CONFIG = """
[user]
name = "Evan"
email = "evan@example.com"

[templates]
log = '''
separate(" ",
  change_id.short(8),
  commit_id.short(12),
  local_bookmarks.map(|b| b.name()).join(" "),
  if(current_working_copy, "", working_copies),
  if(empty, "(empty)"),
  if(description, description.first_line(), "(no description set)"),
) ++ "\\n"
'''
"""

_HEX = re.compile(r"(?<![0-9a-zA-Z])[0-9a-f]{8,64}(?![0-9a-zA-Z])")
_ICECHUNK_ID = re.compile(r"(?<![0-9A-Za-z])[0-9A-Z]{20}(?![0-9A-Za-z])")
_ICECHUNK_PREFIX = re.compile(r"(?<![0-9A-Za-z])([0-9A-Z]{16})…")
_CHANGE_ID = re.compile(r"(?<![0-9a-zA-Z])[k-z]{8}(?![0-9a-zA-Z])")
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?"
)


class Normalizer:
    """Replace per-run identifiers with stable stand-ins.

    Hex ids (VCS commits, pin hashes, workspace ids, digests) are grouped by
    prefix so the 8-character form `jj status` prints and the 12-character
    form tether prints agree; each group gets one fake derived from its
    order of first appearance. Icechunk snapshot ids and jj change ids get
    the same treatment in their own alphabets.
    """

    def __init__(self, roots: list[Path]) -> None:
        self._roots = sorted({str(r) for r in roots}, key=len, reverse=True)
        self._hex_families: list[tuple[list[str], str]] = [(["0" * 64], "0" * 64)]
        self._snap: dict[str, str] = {}
        self._change: dict[str, str] = {"zzzzzzzz": "zzzzzzzz"}
        self._when: dict[str, str] = {}

    def fix_hex(self, real: str, fake: str) -> None:
        """Pin a hex id (the dataset id) to a chosen stand-in."""
        self._hex_families.append(([real], fake))

    def _hex(self, m: re.Match[str]) -> str:
        tok = m.group(0)
        for members, fake in self._hex_families:
            if any(a.startswith(b) or b.startswith(a) for a in members for b in [tok]):
                if tok not in members:
                    members.append(tok)
                return fake[: len(tok)]
        fake = hashlib.sha256(f"hex:{len(self._hex_families)}".encode()).hexdigest()
        self._hex_families.append(([tok], fake))
        return fake[: len(tok)]

    def _snapshot(self, m: re.Match[str]) -> str:
        tok = m.group(0)
        if tok not in self._snap:
            digest = hashlib.sha256(f"snap:{len(self._snap)}".encode()).digest()
            self._snap[tok] = base64.b32encode(digest).decode()[:20]
        return self._snap[tok]

    def _snapshot_prefix(self, m: re.Match[str]) -> str:
        """`short_state` shows 16 characters and an ellipsis; keep them in step."""
        prefix = m.group(1)
        for real, fake in self._snap.items():
            if real.startswith(prefix):
                return fake[:16] + "…"
        # Only ever seen truncated (a base head nothing else prints): mint a
        # stand-in for the prefix itself so it is at least stable.
        if prefix not in self._snap:
            digest = hashlib.sha256(f"snap:{len(self._snap)}".encode()).digest()
            self._snap[prefix] = base64.b32encode(digest).decode()[:20]
        return self._snap[prefix][:16] + "…"

    def _change_id(self, m: re.Match[str]) -> str:
        tok = m.group(0)
        if tok not in self._change:
            digest = hashlib.sha256(f"change:{len(self._change)}".encode()).digest()
            self._change[tok] = "".join(chr(ord("k") + b % 16) for b in digest[:8])
        return self._change[tok]

    def learn_timestamps(self, texts: list[str]) -> None:
        """Map every timestamp in the story to its clock, in chronological order.

        `tether ops` lists newest first, so first appearance would run the
        clock backwards; learn them all up front instead.
        """
        stamps = sorted(
            {m.group(0) for text in texts for m in _TIMESTAMP.finditer(text)}
        )
        for n, stamp in enumerate(stamps):
            self._when.setdefault(
                stamp, f"2026-09-01T09:{n // 60:02d}:{n % 60:02d}+00:00"
            )
        for text in texts:  # full snapshot ids first, so truncated ones resolve
            _ICECHUNK_ID.sub(self._snapshot, text)

    def _timestamp(self, m: re.Match[str]) -> str:
        return self._when.get(m.group(0), "2026-09-01T09:00:00+00:00")

    def __call__(self, text: str, *, jj: bool = False) -> str:
        for root in self._roots:
            text = text.replace(root, "~")
        text = _TIMESTAMP.sub(self._timestamp, text)
        text = _ICECHUNK_ID.sub(self._snapshot, text)
        text = _ICECHUNK_PREFIX.sub(self._snapshot_prefix, text)
        text = _HEX.sub(self._hex, text)
        if jj:
            text = _CHANGE_ID.sub(self._change_id, text)
        return _resort_pin_lists(text)


_PIN_LINE = re.compile(r"^  \w+: [0-9a-f]{8}\.[0-9a-f]{16}$")


def _resort_pin_lists(text: str) -> str:
    """Re-sort `unpinned N pin(s)` lists after mapping.

    tether prints released pins sorted by id. That order is real but
    meaningless once ids are stand-ins, and it would differ between runs;
    sorting the mapped lines keeps the file stable without changing content.
    """
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        if lines[i].startswith("unpinned ") and lines[i].endswith(" pin(s)"):
            j = i + 1
            while j < len(lines) and _PIN_LINE.match(lines[j]):
                j += 1
            lines[i + 1 : j] = sorted(lines[i + 1 : j])
            i = j
        else:
            i += 1
    return "\n".join(lines)


class Story:
    """Run the guide's commands and record each step as `.sh`/`.py` + `.txt`."""

    def __init__(self, root: Path, env: dict[str, str]) -> None:
        self.root = root
        self.env = env
        self.cwd = root
        self.norm = Normalizer([root, root.resolve()])
        self.raw: list[tuple[str, str, bool]] = []  # (file name, text, jj output?)
        self.runner = typer_testing.CliRunner()
        self.py_ns: dict[str, object] = {}
        self._n = 0

    # -- recording --------------------------------------------------------
    def _record(
        self, name: str, ext: str, command: str, output: str, *, jj: bool = False
    ) -> None:
        # Normalisation waits until `check()` so ids learnt later (the dataset
        # id `init` prints) apply to every step, in order of first appearance.
        self._n += 1
        stem = f"{self._n:02d}-{name}"
        self.raw.append((f"{stem}{ext}", command.rstrip() + "\n", jj))
        if output.strip():
            self.raw.append((f"{stem}.txt", output.rstrip() + "\n", jj))

    # -- runners ----------------------------------------------------------
    def tether(self, name: str, *args: str, display: str | None = None) -> str:
        output = self.tether_quiet(*args)
        shown = display or "tether " + " ".join(_quote(a) for a in args)
        self._record(name, ".sh", shown, output)
        return output

    def tether_quiet(self, *args: str) -> str:
        """Run a tether command in-process without recording it."""
        os.chdir(self.cwd)
        result = self.runner.invoke(
            app, list(args), env=self.env, catch_exceptions=False
        )
        return result.output

    def tether_batch(self, name: str, *argvs: list[str]) -> None:
        """Several commands recorded as one block (setup lists, mostly)."""
        shown, outputs = [], []
        for argv in argvs:
            shown.append("tether " + " ".join(_quote(a) for a in argv))
            outputs.append(self.tether_quiet(*argv).rstrip())
        self._record(name, ".sh", "\n".join(shown), "\n".join(outputs))

    def jj(self, name: str, *args: str, display: str | None = None) -> str:
        proc = self._jj(*args)
        shown = display or "jj " + " ".join(_quote(a) for a in args)
        self._record(name, ".sh", shown, proc.stdout + proc.stderr, jj=True)
        return proc.stdout

    def jj_value(self, *args: str) -> str:
        """Ask jj something the story needs (a change id) without recording it."""
        return self._jj(*args).stdout.strip()

    def _jj(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["jj", "--no-pager", "--color=never", *args],
            cwd=self.cwd,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )

    def sh(self, name: str, *argv: str, display: str | None = None) -> None:
        """A plain shell command: what someone does *outside* tether."""
        proc = subprocess.run(
            list(argv),
            cwd=self.cwd,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        shown = display or " ".join(_quote(a) for a in argv)
        self._record(name, ".sh", shown, proc.stdout + proc.stderr)

    def python(self, name: str, code: str, *, argv: list[str] | None = None) -> None:
        """Execute a snippet and record it verbatim, with what it printed.

        With `argv` the snippet runs as a script would (`sys.argv` set) and is
        not recorded: `script()` shows the source once, the caller the runs.
        """
        os.chdir(self.cwd)
        code = textwrap.dedent(code).strip()
        saved = sys.argv
        if argv is not None:
            sys.argv = argv
        printed = io.StringIO()
        try:
            with contextlib.redirect_stdout(printed):
                exec(compile(code, f"<{name}>", "exec"), dict(self.py_ns))
        finally:
            sys.argv = saved
        if argv is None:
            self._record(name, ".py", code, printed.getvalue())

    def script(self, name: str, code: str) -> None:
        """Record a script's source; `python(..., argv=...)` runs it."""
        self._record(name, ".py", textwrap.dedent(code).strip(), "")

    def loop(self, name: str, steps: list[tuple[str, Callable[[], str]]]) -> None:
        """Several shell lines recorded as one block, each run by `fn`."""
        outputs = []
        for _display, fn in steps:
            outputs.append(fn().rstrip())
        self._record(name, ".sh", "\n".join(d for d, _ in steps), "\n".join(outputs))

    # -- checking ---------------------------------------------------------
    def check(self, *, skipped_tail: bool = False) -> None:
        """Compare (or, with TETHER_UPDATE_DOCS=1, write) the generated files.

        `skipped_tail`: the story stopped early (no Postgres for section 7);
        committed files past the last generated step are left alone.
        """
        self.norm.learn_timestamps([text for _, text, _ in self.raw])
        # Commands may name jj change ids (`tether abandon CHANGE`); map those
        # the same way as in `jj log` output. Tether's own output never does.
        files = {
            fname: self.norm(text, jj=jj or fname.endswith(".sh"))
            for fname, text, jj in self.raw
        }
        if UPDATE:
            if skipped_tail:
                pytest.fail("cannot update the generated files from a partial run")
            if GENERATED.exists():
                shutil.rmtree(GENERATED)
            GENERATED.mkdir(parents=True)
            for fname, text in files.items():
                (GENERATED / fname).write_text(text, encoding="utf-8")
            return
        problems: list[str] = []
        committed = (
            {p.name for p in GENERATED.glob("*")} if GENERATED.exists() else set()
        )
        if skipped_tail:
            committed = {f for f in committed if int(f.split("-", 1)[0]) <= self._n}
        for fname in sorted(set(files) | committed):
            fresh = files.get(fname)
            path = GENERATED / fname
            old = path.read_text(encoding="utf-8") if path.exists() else None
            if fresh == old:
                continue
            if fresh is None:
                problems.append(f"{fname}: committed but no longer generated")
            elif old is None:
                problems.append(f"{fname}: generated but not committed")
            else:
                import difflib

                diff = difflib.unified_diff(
                    old.splitlines(),
                    fresh.splitlines(),
                    "committed",
                    "fresh",
                    lineterm="",
                )
                problems.append(f"{fname}:\n" + "\n".join(diff))
        assert not problems, (
            "user_guide/_generated/06 is stale; rerun with TETHER_UPDATE_DOCS=1\n\n"
            + "\n\n".join(problems)
        )


class _StoryClock:
    """Stand-in for `datetime` in tether.oplog: fixed start, +1 s per call."""

    _ticks = 0

    @classmethod
    def now(cls, tz: object = None) -> datetime.datetime:
        cls._ticks += 1
        return datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC) + (
            datetime.timedelta(seconds=cls._ticks)
        )


def _quote(arg: str) -> str:
    return f'"{arg}"' if (" " in arg or not arg) else arg


def _git(path: Path, env: dict[str, str], *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


# --------------------------------------------------------------------------- #
# The objects the story reads and writes
# --------------------------------------------------------------------------- #
def _make_imaging(path: Path) -> None:
    repo = ic.Repository.create(ic.local_filesystem_storage(str(path)))
    session = repo.writable_session("main")
    root = zarr.create_group(store=session.store)
    images = root.create_array("images", shape=(4, 8, 8), chunks=(1, 8, 8), dtype="u2")
    images[:] = 1000
    labels = root.create_array("labels", shape=(4, 8, 8), chunks=(1, 8, 8), dtype="u1")
    labels[:] = 0
    session.commit("plate1: images and empty labels")


def _make_features(path: Path) -> None:
    lance.write_dataset(
        pa.table({"cell_id": [1, 2, 3], "area": [110.0, 95.5, 130.25]}), str(path)
    )


def _make_raw(path: Path, seed: int = 0) -> None:
    path.mkdir(parents=True)
    for i in range(3):
        (path / f"well_{i}.tif").write_bytes(bytes([seed * 16 + i]) * 64)


def _make_lance_table(path: Path, column: str, values: list[float]) -> None:
    lance.write_dataset(
        pa.table({"cell_id": list(range(1, len(values) + 1)), column: values}),
        str(path),
    )


def _make_code(path: Path, env: dict[str, str]) -> None:
    path.mkdir(parents=True)
    _git(path, env, "init", "-q", "-b", "main")
    (path / "make_report.py").write_text(
        'from tether import Repo\n\nrepo = Repo.find(".")\n', encoding="utf-8"
    )
    _git(path, env, "add", "-A")
    _git(path, env, "commit", "-qm", "Q3 report")


# --------------------------------------------------------------------------- #
# The story
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(shutil.which("jj") is None, reason="jj not on PATH")
def test_use_cases_story(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    # Section 7 publishes to Postgres; without one the story stops after 6.
    try:
        registry_dsn: str | None = request.getfixturevalue("pg_dsn")
    except pytest.skip.Exception:
        registry_dsn = None
    jj_config = tmp_path / "jj-config.toml"
    jj_config.write_text(_JJ_CONFIG, encoding="utf-8")
    env = {**os.environ, **_GIT_ENV, **_JJ_ENV, "JJ_CONFIG": str(jj_config)}
    if registry_dsn is not None:
        env["REGISTRY_DSN"] = registry_dsn
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TETHER_REV", raising=False)
    monkeypatch.chdir(tmp_path)  # the story chdirs between checkouts; restore after
    # tether's operation log stamps entries with the wall clock; give it the
    # story's clock instead (one second per operation) so `tether ops` is stable.
    import tether.oplog

    monkeypatch.setattr(tether.oplog, "datetime", _StoryClock)

    data = tmp_path / "data"
    _make_imaging(data / "imaging.icechunk")
    _make_features(data / "features.lance")
    _make_raw(data / "raw" / "plate1")
    _make_code(tmp_path / "analysis-code", env)
    # Section 4's inventory, the instrument's re-export of one file, and
    # section 5's scratch table.
    _make_raw(data / "raw" / "plate2", seed=2)
    _make_raw(data / "raw" / "plate3", seed=3)
    _make_lance_table(data / "qc.lance", "focus", [0.91, 0.88, 0.95])
    exports = tmp_path / "exports" / "plate2"
    exports.mkdir(parents=True)
    (exports / "well_1.tif").write_bytes(bytes([99]) * 64)
    _make_lance_table(data / "scratch.lance", "embedding_0", [0.0, 0.0, 0.0])
    inventory = tmp_path / "inventory.csv"
    inventory.write_text(
        "key,kind,uri\n"
        f"raw/plate2,file,{data / 'raw' / 'plate2'}\n"
        f"raw/plate3,file,{data / 'raw' / 'plate3'}\n"
        f"qc/focus,lance,{data / 'qc.lance'}\n",
        encoding="utf-8",
    )

    story = Story(tmp_path, env)
    analysis = tmp_path / "analysis"

    # ---- 0. Set up ------------------------------------------------------
    story.jj(
        "init", "git", "init", "analysis", display="jj git init analysis && cd analysis"
    )
    story.cwd = analysis
    story.tether("init", "init")
    dataset_id = tomllib.loads((analysis / "tether.toml").read_text())["dataset"]["id"]
    story.norm.fix_hex(dataset_id, "0a1b2c3d")
    story.tether_batch(
        "add",
        [
            "add",
            "zarr/imaging",
            "--kind",
            "icechunk",
            str(data / "imaging.icechunk"),
            "--branch",
            "main",
        ],
        ["add", "features", "--kind", "lance", str(data / "features.lance")],
        ["add", "raw/plate1", "--kind", "file", str(data / "raw" / "plate1")],
        ["add", "code", "--kind", "git", str(tmp_path / "analysis-code")],
    )
    story.jj("status", "status")

    # ---- 1. Reproduce ---------------------------------------------------
    out = story.tether("commit", "commit", "-m", "Inputs for the Q3 report")
    committed = re.search(r"committed ([0-9a-f]+)", out)
    assert committed is not None, out
    q3 = committed.group(1)
    story.tether("open-rev", "open", "zarr/imaging", "--rev", q3)
    story.python(
        "open-rev-py",
        f"""
        from tether import Repo

        repo = Repo.find(".")
        h = repo.open("zarr/imaging", rev="{q3}")   # read-only, at the pinned snapshot
        """,
    )
    story.tether("verify", "verify", "--all-history")
    story.jj("log", "log")

    # ---- 2. Reprocess on a branch ---------------------------------------
    story.tether("new-relabel", "new", "-b", "relabel-v3")
    story.python(
        "relabel-job",
        """
        import lance
        import pyarrow as pa
        import zarr
        from tether import Repo

        repo = Repo.find(".")
        h = repo.open("zarr/imaging", read_only=False)  # creates the Icechunk branch
        root = zarr.open_group(store=h.session.store, mode="a")
        root["labels"][:] = 3  # model v3's labels
        h.session.commit("labels from model v3")

        f = repo.open("features", read_only=False)  # and the Lance branch
        areas = pa.table({"cell_id": [1, 2, 3], "area": [112.0, 96.0, 128.5]})
        lance.write_dataset(areas, f.dataset, mode="append")
        """,
    )
    story.tether("commit-relabel", "commit", "-m", "Relabel plate1 with model v3")
    story.tether("diff-relabel", "diff", "main", "--content")
    story.jj("log-relabel", "log")
    story.tether("promote-relabel", "promote")
    story.tether("promote-imaging", "promote", "zarr/imaging")
    story.tether("new-main", "new", "main")
    story.python(
        "finish-features",
        """
        import lance
        import pyarrow as pa
        from tether import Repo

        repo = Repo.find(".")
        f = repo.open("features", read_only=False)  # on the trunk: Lance's own main
        areas = pa.table({"cell_id": [1, 2, 3], "area": [112.0, 96.0, 128.5]})
        lance.write_dataset(areas, f.dataset, mode="append")
        """,
    )
    story.tether("commit-features", "commit", "-m", "Features rebuilt for model v3")
    story.jj("log-landed", "log")
    story.tether("abandon-relabel", "abandon", "relabel-v3", "--gc")
    story.jj("delete-relabel", "bookmark", "delete", "relabel-v3")
    story.tether("gc-relabel", "gc", "--prune-bookmarks", "--no-dry-run")
    story.jj("log-clean", "log")

    # ---- 3. A/B --------------------------------------------------------
    analysis_b = tmp_path / "analysis-b"
    story.jj(
        "workspace-add",
        "workspace",
        "add",
        str(analysis_b),
        display="jj workspace add ../analysis-b",
    )
    story.tether("new-a", "new", "-b", "a")
    story.cwd = analysis_b
    story.tether("new-b", "new", "-b", "b", display="cd ../analysis-b\ntether new -b b")
    ws_b = tomllib.loads((analysis_b / ".tether" / "workspace.toml").read_text())[
        "workspace_id"
    ]

    label = """
        import zarr
        from tether import Repo

        repo = Repo.find(".")
        h = repo.open("zarr/imaging", read_only=False)
        root = zarr.open_group(store=h.session.store, mode="a")
        root["labels"][:] = {value}
        h.session.commit("labels: candidate {name}")
        """
    story.cwd = analysis
    story.python("label-a", label.format(value=7, name="A"))
    story.tether("commit-a", "commit", "-m", "Labels: candidate A")
    story.cwd = analysis_b
    story.python("label-b", label.format(value=9, name="B"))
    story.tether("commit-b", "commit", "-m", "Labels: candidate B")
    story.cwd = analysis
    story.jj("log-ab", "log")
    story.tether("diff-ab", "diff", "a", "b", "--content")
    story.tether("promote-a", "promote")
    story.tether("forget-b", "forget-workspace", ws_b[:8])
    story.tether("abandon-b", "abandon", "b", "--gc")
    story.jj("delete-b", "bookmark", "delete", "b")
    story.tether("gc-b", "gc", "--prune-bookmarks", "--force-prune", "--no-dry-run")
    story.jj("log-final", "log")

    # ---- 4. Catch drift nightly ----------------------------------------
    story.tether("new-main-4", "new", "main")
    story.tether(
        "import", "import", str(inventory), display="tether import ../inventory.csv"
    )
    story.tether("commit-inventory", "commit", "-m", "Inventory as of 2026-09-01")
    story.jj("log-inventory", "log")
    story.tether_batch("nightly", ["status", "--snapshot"], ["verify"])
    # The instrument re-exports one file of an "immutable" plate, in place.
    story.sh(
        "overwrite",
        "cp",
        str(exports / "well_1.tif"),
        str(data / "raw" / "plate2" / "well_1.tif"),
        display="cp ~/exports/plate2/well_1.tif ~/data/raw/plate2/well_1.tif",
    )
    story.tether("status-immutable", "status", "--snapshot")
    story.tether_batch(
        "accept",
        ["remove", "raw/plate2"],
        ["add", "raw/plate2", "--kind", "file", str(data / "raw" / "plate2")],
        ["commit", "-m", "plate2 re-exported by the instrument"],
    )
    # An operator "tidies up" the tags in two stores.
    objects = analysis / ".tether" / "objects"
    imaging_pin = tomllib.loads((objects / "zarr" / "imaging.toml").read_text())
    features_pin = tomllib.loads((objects / "features.toml").read_text())
    story.python(
        "delete-tags",
        f"""
        import icechunk as ic
        import lance

        lance.dataset("{data}/features.lance").tags.delete("{features_pin["pin"]["ref"]}")
        storage = ic.local_filesystem_storage("{data}/imaging.icechunk")
        ic.Repository.open(storage).delete_tag("{imaging_pin["pin"]["ref"]}")
        """,
    )
    story.tether("verify-missing", "verify")
    story.tether("repair", "repair")
    story.tether("verify-repaired", "verify")
    inventory_commit = story.jj_value(
        "log", "--no-graph", "-r", "main", "-T", "commit_id.short(12)"
    )
    story.tether("open-after-repair", "open", "zarr/imaging", "--rev", inventory_commit)
    # Upstream moves on its own: a pipeline appends to the Lance table, a
    # colleague commits to the code repository.
    story.python(
        "upstream-moves",
        f"""
        import lance
        import pyarrow as pa

        qc = pa.table({{"cell_id": [4, 5], "area": [101.0, 99.5]}})
        lance.write_dataset(qc, "{data}/features.lance", mode="append")
        """,
    )
    (tmp_path / "analysis-code" / "make_report.py").write_text(
        'from tether import Repo\n\nrepo = Repo.find(".")\nprint(repo.status())\n',
        encoding="utf-8",
    )
    story.sh(
        "code-commit",
        "git",
        "-C",
        str(tmp_path / "analysis-code"),
        "commit",
        "-qam",
        "Print the status in the report",
        display='git -C ~/analysis-code commit -qam "Print the status in the report"',
    )
    story.tether("status-moved", "status", "--snapshot")
    story.tether("pull", "pull")
    story.jj("log-pull", "log")

    # ---- 5. Keep the data bill down ------------------------------------
    story.tether_batch(
        "add-scratch",
        [
            "add",
            "scratch/embeddings",
            "--kind",
            "lance",
            str(data / "scratch.lance"),
            "--pin",
            "record",
        ],
        ["commit", "-m", "Track the sweep's scratch table"],
    )
    story.tether("new-sweep", "new", "-b", "sweep")
    sweep = """
        import sys

        import lance
        import pyarrow as pa
        import zarr
        from tether import Repo

        trial = int(sys.argv[sys.argv.index("--trial") + 1])
        repo = Repo.find(".")
        h = repo.open("zarr/imaging", read_only=False)
        root = zarr.open_group(store=h.session.store, mode="a")
        root["labels"][:] = 10 + trial
        h.session.commit(f"labels: sweep trial {trial}")

        s = repo.open("scratch/embeddings", read_only=False)
        rows = pa.table({"cell_id": [1, 2, 3], "embedding_0": [trial / 10.0] * 3})
        lance.write_dataset(rows, s.dataset, mode="append")
        """
    story.script("sweep-py", sweep)

    def trial(n: int) -> Callable[[], str]:
        def run() -> str:
            story.python("sweep", sweep, argv=["sweep.py", "--trial", str(n)])
            return story.tether_quiet("commit", "-m", f"sweep: trial {n}")

        return run

    story.loop(
        "trials",
        [
            (
                f'python sweep.py --trial {n} && tether commit -m "sweep: trial {n}"',
                trial(n),
            )
            for n in range(1, 5)
        ],
    )
    story.jj("log-sweep", "log", "-r", "main::@")
    drop = [
        story.jj_value(
            "log",
            "--no-graph",
            "-r",
            f'subject("sweep: trial {n}")',
            "-T",
            "change_id.short(8)",
        )
        for n in (2, 3)
    ]
    story.tether("abandon-trials", "abandon", *drop, "--gc")
    story.jj("log-abandoned", "log", "-r", "main::@")

    # ---- 6. Recover ----------------------------------------------------
    story.tether("new-scratch", "new", "-b", "scratch", "--eager")
    story.tether("ops", "ops", "-n", "3")
    story.tether("undo-new", "undo")
    story.tether("restore", "restore", "zarr/imaging", "--from", q3)
    story.tether("status-restored", "status", "--snapshot")
    story.tether("undo-restore", "undo")
    # Two more slips, walked back together.
    story.tether("restore-features", "restore", "features", "--from", q3)
    story.tether("new-again", "new", "-b", "again", "--eager")
    ops = json.loads(story.tether_quiet("ops", "-n", "3", "--json"))
    story.tether("ops-again", "ops", "-n", "3")
    story.tether("undo-to", "undo", "--to", ops[2]["id"])
    story.jj("log-recovered", "log", "-r", "main::@")

    if registry_dsn is None:
        story.check(skipped_tail=True)
        return

    # ---- 7. Publish ----------------------------------------------------
    story.tether(
        "publish",
        "publish",
        "--to",
        registry_dsn,
        "--schema",
        "tether",
        display='tether publish --to "$REGISTRY_DSN" --schema tether',
    )
    trial4 = story.jj_value("log", "--no-graph", "-r", "sweep", "-T", "commit_id")
    story.python(
        "consumer-join",
        f"""
        import os

        import psycopg

        with psycopg.connect(os.environ["REGISTRY_DSN"]) as conn:
            conn.execute("create table runs (run_id text, dataset_commit text)")
            conn.execute(
                "insert into runs values ('train-2026-09-14', %s)",
                ("{trial4}",),
            )
            rows = conn.execute(
                \"\"\"
                select r.run_id, o.key, o.pin_id, o.state_json
                from   runs r
                join   tether.objects o on o.commit_id = r.dataset_commit
                where  r.run_id = 'train-2026-09-14'
                order  by o.key
                \"\"\"
            ).fetchall()
        for run_id, key, pin_id, state in rows:
            print(run_id, key, pin_id, state)
        """,
    )
    story.tether(
        "export",
        "export",
        str(tmp_path / "history"),
        "--format",
        "parquet",
        display="tether export ../history --format parquet",
    )
    story.tether("verify-rev", "verify", "--rev", trial4[:12], "--deep")
    story.jj("log-end", "log")

    story.check()
