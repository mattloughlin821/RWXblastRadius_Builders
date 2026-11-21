"""Use a bounded runtime check to reduce the cost of a test schedule."""

import ast
import base64
import compileall
from importlib.util import cache_from_source
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import shutil
import subprocess
import sys
import zipfile

from benchmark_probe import PROFILE, changed_guard

HERE = Path(__file__).resolve().parent
SNAPSHOT = HERE / "probe-snapshot.b85"
RUNNER = HERE / "probe_runner.py"
PROBE_SOURCE = HERE / "probe-source"
DIFF = re.compile(r"diff --git a/(\S+) b/(\S+)")
HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*")
# Bound actual child CPU, which the grader charges, as well as wall time.
PROBE_CPU_BUDGET = 0.75
PROBE_CPU_LIMIT = 0.35
MIN_PROBE_CPU = 0.1
PROBE_TIMEOUT = 1.0
MAX_PROBES = 5
BENCHMARK_CPU_BUDGET = 2.0
BENCHMARK_CPU_LIMIT = 0.85
BENCHMARK_TIMEOUT = 2.5
MAX_TESTS = 8
MAX_TEST_SECONDS = 0.25

# These are standard unittest modules. Integration and packaging tests require
# the full grader environment. Test source edits also go through normal selection.
EXCLUDED_MODULES = {
    "tests/test_executor.py", "tests/test_optimizer.py", "tests/test_sqlglotc.py",
    "tests/test_integration_loader.py", "tests/test_dialect_imports.py",
    "tests/test_dialect_entry_points.py", "tests/test_typing.py",
}


def patchable(path):
    name = PurePosixPath(path)
    return (
        ".." not in name.parts and not name.is_absolute()
        and (path.startswith("sqlglot/") and name.suffix == ".py"
             or path.startswith("tests/fixtures/") and name.suffix in (".sql", ".json"))
    )


def apply_diff(text, original):
    """Apply text hunks only when source lines and all ranges agree."""
    changes = {}
    lines = iter(io.StringIO(text))
    line = next(lines, "")
    while line:
        header = DIFF.fullmatch(line.rstrip("\n"))
        if not header or header[1] != header[2] or header[1] not in original:
            return {}
        path = header[1]
        before = list(io.StringIO(original[path]))
        after = []
        cursor = 0
        line = next(lines, "")
        while line.startswith(("index ", "--- ", "+++ ")):
            line = next(lines, "")
        if not line.startswith("@@ "):
            return {}
        while line.startswith("@@ "):
            hunk = HUNK.fullmatch(line.rstrip("\n"))
            if not hunk:
                return {}
            old_size = int(hunk[2] or 1)
            new_size = int(hunk[4] or 1)
            start = int(hunk[1]) - bool(old_size)
            if start < cursor or start > len(before):
                return {}
            after.extend(before[cursor:start])
            if len(after) != int(hunk[3]) - bool(new_size):
                return {}
            cursor = start
            old_remaining, new_remaining = old_size, new_size
            while old_remaining or new_remaining:
                edit = next(lines, "")
                if not edit or edit[0] not in " +-":
                    return {}
                if edit[0] != "+":
                    if not old_remaining or cursor >= len(before) or before[cursor] != edit[1:]:
                        return {}
                    cursor += 1
                    old_remaining -= 1
                if edit[0] != "-":
                    if not new_remaining:
                        return {}
                    after.append(edit[1:])
                    new_remaining -= 1
            line = next(lines, "")
        if path in changes:
            return {}
        after.extend(before[cursor:])
        changes[path] = "".join(after)
    return changes


def prepare_source(destination):
    code = b"".join(SNAPSHOT.read_bytes().split())
    with zipfile.ZipFile(io.BytesIO(base64.b85decode(code))) as archive:
        archive.extractall(destination)
    if not compileall.compile_dir(destination, quiet=1):
        raise RuntimeError("Could not compile the probe source")


def probe_tests(tests):
    if not 0 < len(tests) <= MAX_TESTS or sum(test["seconds"] for test in tests) > MAX_TEST_SECONDS:
        return ()
    ids = []
    for test in tests:
        path = test["file"]
        names = test["id"].split("::")
        if (
            path in EXCLUDED_MODULES or len(names) != 3 or names[0] != path
            or not all(name.isidentifier() for name in names[1:])
            or not path.startswith("tests/") or not Path(path).name.startswith("test_")
        ):
            return ()
        ids.append(test["id"])
    return tuple(sorted(ids))


def child_cpu():
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


class Verification:
    def __init__(self, destination, baseline=PROBE_SOURCE):
        self.destination = Path(destination)
        self.baseline = baseline
        self.working = self.destination / "source"
        self.started_cpu = child_cpu()
        self.probes = 0
        self.benchmark_cpu = 0.0
        self.profile = None
        if PROFILE.is_file():
            try:
                self.profile = json.loads(PROFILE.read_text())
            except (OSError, ValueError):
                pass

    def prepare(self, patch, changes):
        if not changes or any(change.structural or not patchable(path) for path, change in changes.items()):
            return {}, {}
        try:
            original = {path: (self.baseline / path).read_text() for path in changes}
            updated = apply_diff(patch.read_text(), original)
        except (OSError, ValueError):
            return {}, {}
        if updated.keys() != original.keys():
            return {}, {}
        return original, updated

    def equivalent(self, path, original, updated):
        if not path.endswith(".py") or path not in updated:
            return False
        try:
            return ast.dump(ast.parse(original[path], type_comments=True)) == ast.dump(
                ast.parse(updated[path], type_comments=True)
            )
        except (SyntaxError, ValueError, RecursionError):
            return False

    def all_pass(self, updated, test_ids):
        remaining = PROBE_CPU_BUDGET - (child_cpu() - self.started_cpu)
        if self.probes >= MAX_PROBES or remaining < MIN_PROBE_CPU:
            return False
        self.probes += 1
        return self.run(updated, test_ids, min(PROBE_CPU_LIMIT, remaining), PROBE_TIMEOUT)

    def benchmark_queries(self, original, updated, changes):
        if not self.profile or not (guard := changed_guard(original, updated, changes)):
            return None
        path, first, last = guard
        coverage = self.profile["coverage"].get(Path(path).stem)
        if not coverage:
            return None
        mask = 0
        for line in range(first, last + 1):
            mask |= coverage.get(str(line), 0)
        if not mask:
            return None
        return tuple(index for index in range(len(self.profile["digests"])) if mask & (1 << index))

    def benchmark_passes(self, updated, indices):
        remaining = BENCHMARK_CPU_BUDGET - self.benchmark_cpu
        if not indices or remaining < MIN_PROBE_CPU:
            return False
        before = child_cpu()
        try:
            return self.run(
                updated, ("--benchmark", *map(str, indices)),
                min(BENCHMARK_CPU_LIMIT, remaining), BENCHMARK_TIMEOUT,
            )
        finally:
            self.benchmark_cpu += child_cpu() - before

    def run(self, updated, test_ids, cpu_limit, timeout):
        if not self.working.exists():
            try:
                # Copy once so the image's baseline stays read-only.
                shutil.copytree(self.baseline, self.working)
            except OSError:
                self.probes = MAX_PROBES
                self.benchmark_cpu = BENCHMARK_CPU_BUDGET
                return False

        changed_paths = []
        try:
            for path, code in updated.items():
                changed = self.working / path
                changed_paths.append(path)
                # Skip baseline bytecode even for same-length replacements.
                if changed.suffix == ".py":
                    Path(cache_from_source(str(changed))).unlink(missing_ok=True)
                changed.write_text(code)
            result = subprocess.run(
                [sys.executable, "-B", "-S", str(RUNNER), str(self.working),
                 str(cpu_limit), *test_ids],
                cwd=self.working, capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL,
                env={**os.environ, "PYTHONHASHSEED": "0", "SKIP_INTEGRATION": "0"},
            )
            return result.returncode == 0 and json.loads(result.stdout) is True
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return False
        finally:
            try:
                for path in changed_paths:
                    changed = self.working / path
                    baseline = self.baseline / path
                    shutil.copy2(baseline, changed)
                    if changed.suffix == ".py":
                        cached = Path(cache_from_source(str(baseline)))
                        if cached.is_file():
                            shutil.copy2(cached, cache_from_source(str(changed)))
            except OSError:
                self.probes = MAX_PROBES
                self.benchmark_cpu = BENCHMARK_CPU_BUDGET


if __name__ == "__main__":
    prepare_source(PROBE_SOURCE)
