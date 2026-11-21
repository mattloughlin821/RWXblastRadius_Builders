"""Select tests using file coverage from the pinned sqlglot baseline."""

import json
import re
import sys
from pathlib import Path

DATA = Path("/data")
MAP = Path(__file__).with_name("coverage-map.json")
WORKERS = 3
DIFF_HEADER = re.compile(r"diff --git a/(\S+) b/(\S+)")


def changed_paths(patch: Path) -> set[str]:
    paths = set()
    in_hunk = False
    for line in patch.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            header = DIFF_HEADER.fullmatch(line)
            if not header or header[1] != header[2]:
                return set()
            paths.add(header[1])
        elif line.startswith("@@ "):
            in_hunk = True
        elif in_hunk and line.startswith(("+", "-")):
            code = line[1:].expandtabs()
            # File coverage can miss effects from module and class initialization.
            if code.strip() and len(code) - len(code.lstrip()) <= 4:
                return set()
    return paths


def split(tests: list[dict], worker_count: int) -> list[list[int]]:
    """Longest test first, each onto the worker with the least to do so far."""
    workers: list[list[int]] = [[] for _ in range(worker_count)]
    loads = [0.0] * worker_count
    for test in sorted(tests, key=lambda test: test["seconds"], reverse=True):
        lightest = loads.index(min(loads))
        workers[lightest].append(test["number"])
        loads[lightest] += test["seconds"]
    return workers


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    coverage = json.loads(MAP.read_text())
    tests_by_path = {path: set(ids) for path, ids in coverage.get("paths", {}).items()}
    measured = set(coverage.get("measured", []))
    files_by_test_id = {test["id"]: test["file"] for test in tests}
    full_suite = split(tests, WORKERS)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        paths = changed_paths(patch)
        workers = full_suite
        if paths and paths <= tests_by_path.keys():
            covered = set().union(*(tests_by_path[path] for path in paths))
            # Sibling tests can reuse setup or data populated by a different test.
            covered_files = {files_by_test_id[node] for node in covered if node in files_by_test_id}
            selected = [
                test for test in tests
                if test["file"] in covered_files or test["id"] not in measured
            ]
            if selected:
                workers = split(selected, min(WORKERS, len(selected)))
        print(json.dumps({"commit": patch.stem, "workers": workers}))
        print(f"{patch.stem}: selected {sum(map(len, workers))} of {len(tests)} tests", file=sys.stderr)


if __name__ == "__main__":
    main()
