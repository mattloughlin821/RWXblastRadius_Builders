"""Use a bounded runtime check to reduce the cost of a test schedule."""

import ast
import base64
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import time
import zipfile

HERE = Path(__file__).resolve().parent
SNAPSHOT = HERE / "probe-snapshot.b85"
RUNNER = HERE / "probe_runner.py"
FINGERPRINT = HERE / "optimizer-fingerprint.json"
DIFF = re.compile(r"diff --git a/(\S+) b/(\S+)")
HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*")
REPLAY_BUDGET = 8.0
TIMEOUT = 0.6
COST_CAP = 0.25
MAX_REPLAY = 24
HEAVY_TIMEOUT = 1.6
FINGERPRINT_TIMEOUT = 3.0
GENERATOR_MEMORY = "tests/test_generator.py::TestGenerator::test_generator_memory"
EXECUTOR_TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"

# These rule modules do not supply functions called by the executor or planner
# after optimize() has returned. Shared scope, annotation, and join modules
# deliberately stay outside this set.
RULE_ONLY_MODULES = frozenset({
    "eliminate_ctes", "eliminate_subqueries", "isolate_table_selects", "merge_subqueries",
    "optimize_joins", "pushdown_predicates", "pushdown_projections", "unnest_subqueries",
})

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


def eligible(test):
    path = test["file"]
    names = test["id"].split("::")
    return (
        test["seconds"] <= COST_CAP and path not in EXCLUDED_MODULES
        and len(names) == 3 and all(name.isidentifier() for name in names[1:])
        and path.startswith("tests/") and Path(path).name.startswith("test_")
    )


class CompareShape(ast.NodeTransformer):
    """Normalize comparison changes without normalizing their operands."""

    def visit_Compare(self, node):
        self.generic_visit(node)
        node.ops = [ast.Eq() for _ in node.ops]
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        node.op = ast.And()
        return node


def replayable_optimizer_change(original, updated, changes):
    if len(updated) != 1:
        return False
    path, source = next(iter(updated.items()))
    module = PurePosixPath(path)
    if module.parent.as_posix() != "sqlglot/optimizer" or module.stem not in RULE_ONLY_MODULES:
        return False
    change = changes[path]
    if change.edit_runs != 1 or len(change.old_code) != 1 or len(change.new_code) != 1:
        return False
    old_line, _ = change.old_code[0]
    try:
        before = ast.parse(original[path], type_comments=True)
        after = ast.parse(source, type_comments=True)
    except (SyntaxError, ValueError, RecursionError):
        return False
    if not any(
        isinstance(function, ast.FunctionDef) and function.lineno < old_line <= function.end_lineno
        and any(isinstance(statement, ast.If) and statement.test.lineno <= old_line <= statement.test.end_lineno
                for statement in ast.walk(function))
        for function in before.body
    ):
        return False
    return ast.dump(CompareShape().visit(before)) == ast.dump(CompareShape().visit(after))


class Verification:
    def __init__(self, destination):
        self.destination = Path(destination)
        self.elapsed = 0.0
        self.ready = False
        self.baseline_fingerprint = None
        self.fingerprint_failed = False
        if FINGERPRINT.is_file():
            try:
                digest = json.loads(FINGERPRINT.read_text())
            except (OSError, ValueError):
                digest = None
            if isinstance(digest, str) and len(digest) == 64:
                self.baseline_fingerprint = digest
            else:
                self.fingerprint_failed = True

    def extract(self):
        if not self.ready:
            code = b"".join(SNAPSHOT.read_bytes().split())
            with zipfile.ZipFile(io.BytesIO(base64.b85decode(code))) as archive:
                archive.extractall(self.destination)
            self.ready = True

    def prepare(self, patch, changes):
        if not changes or any(change.structural or not patchable(path) for path, change in changes.items()):
            return {}, {}
        try:
            self.extract()
            original = {path: (self.destination / path).read_text() for path in changes}
            updated = apply_diff(patch.read_text(), original)
        except (OSError, ValueError, zipfile.BadZipFile):
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

    def remove_passing(self, original, updated, tests, schedule_cost):
        if not updated or self.elapsed + TIMEOUT > REPLAY_BUDGET or not tests:
            return tests
        candidates = [test for test in tests if eligible(test)][:MAX_REPLAY]
        memory = next((test for test in tests if test["id"] == GENERATOR_MEMORY), None)
        test_timeout = TIMEOUT
        if memory:
            candidates.insert(0, memory)
            test_timeout = HEAVY_TIMEOUT
        if self.elapsed + test_timeout > REPLAY_BUDGET:
            return tests
        candidate_ids = {test["id"] for test in candidates}
        rest = [test for test in tests if test["id"] not in candidate_ids]
        if not candidates or schedule_cost(tests) - schedule_cost(rest) < 0.35:
            return tests

        passed = self.run(original, updated, sorted(candidate_ids), test_timeout)
        if not isinstance(passed, list) or not all(isinstance(node_id, str) for node_id in passed):
            return tests
        return [test for test in tests if test["id"] not in candidate_ids.intersection(passed)]

    def remove_equivalent_benchmark(self, original, updated, changes, tests, schedule_cost):
        if self.fingerprint_failed or not any(test["id"] == EXECUTOR_TPCDS for test in tests):
            return tests
        rest = [test for test in tests if test["id"] != EXECUTOR_TPCDS]
        needed = FINGERPRINT_TIMEOUT * (1 if self.baseline_fingerprint else 2)
        if self.elapsed + needed > REPLAY_BUDGET or schedule_cost(tests) - schedule_cost(rest) < needed:
            return tests
        if not replayable_optimizer_change(original, updated, changes):
            return tests
        arguments = ["--optimizer-fingerprint"]
        if self.baseline_fingerprint is None:
            self.baseline_fingerprint = self.run({}, {}, arguments, FINGERPRINT_TIMEOUT)
            if not isinstance(self.baseline_fingerprint, str):
                self.fingerprint_failed = True
                return tests
        actual = self.run(original, updated, arguments, FINGERPRINT_TIMEOUT)
        return rest if actual == self.baseline_fingerprint else tests

    def run(self, original, updated, args, timeout):
        started = time.monotonic()
        try:
            for path, code in updated.items():
                (self.destination / path).write_text(code)
            result = subprocess.run(
                [sys.executable, "-B", "-S", str(RUNNER), str(self.destination), *args],
                cwd=self.destination, capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL,
                env={**os.environ, "PYTHONHASHSEED": "0", "SKIP_INTEGRATION": "0"},
            )
            return json.loads(result.stdout) if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        finally:
            self.elapsed += time.monotonic() - started
            for path, code in original.items():
                (self.destination / path).write_text(code)


if __name__ == "__main__":
    # Run during the Docker build with the grader's Python. This takes the
    # baseline work out of the scored invocation.
    with tempfile.TemporaryDirectory(prefix="blast-radius-prime-") as destination:
        verification = Verification(destination)
        verification.extract()
        digest = verification.run({}, {}, ["--optimizer-fingerprint"], 20)
        FINGERPRINT.write_text(json.dumps(digest) + "\n")
