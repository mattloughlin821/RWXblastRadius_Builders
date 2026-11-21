"""Select tests using file coverage from the pinned sqlglot baseline."""

import json
import re
import sys
from pathlib import Path

DATA = Path("/data")
MAP = Path(__file__).with_name("coverage-map.json")
FIXTURES = Path(__file__).with_name("optimizer-fixtures.json")
MAX_WORKERS = 8
WORKER_COST_MS = 2000
DIFF_HEADER = re.compile(r"diff --git a/(\S+) b/(\S+)")
HUNK_HEADER = re.compile(r"@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")


def changed_lines(patch: Path, fixture_paths: set[str]) -> dict[str, set[int]]:
    paths = {}
    current_path = ""
    old_line = None
    for line in patch.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("diff --git "):
            old_line = None
            header = DIFF_HEADER.fullmatch(line)
            if not header or header[1] != header[2]:
                return {}
            current_path = header[1]
            paths.setdefault(current_path, set())
        elif hunk := HUNK_HEADER.match(line):
            old_line = int(hunk[1])
        elif old_line is not None and line.startswith(" "):
            old_line += 1
        elif old_line is not None and line.startswith(("+", "-")):
            code = line[1:].expandtabs()
            if current_path not in fixture_paths:
                # File coverage can miss effects from module and class initialization.
                if code.strip() and len(code) - len(code.lstrip()) <= 4:
                    return {}
                if line.startswith("-"):
                    paths[current_path].add(old_line)
                elif code.strip():
                    paths[current_path].update((old_line - 1, old_line))
            if line.startswith("-"):
                old_line += 1
    return paths


def function_tests(lines: set[int], regions: list, measured: list[str]) -> set[str]:
    if not lines:
        return set()
    candidates = set()
    for line in lines:
        matches = [region for region in regions if region[0] < line <= region[1]]
        if not matches:
            return set()
        _, _, indices = min(matches, key=lambda region: region[1] - region[0])
        if not indices:
            return set()
        candidates.update(measured[index] for index in indices)
    return candidates


def best_schedule(tests: list[dict]) -> list[list[int]]:
    ordered = sorted(
        ((round(test["seconds"] * 1000), test["number"]) for test in tests),
        key=lambda item: item[0],
        reverse=True,
    )
    best_cost = float("inf")
    best_workers = []
    for worker_count in range(1, min(MAX_WORKERS, len(ordered)) + 1):
        workers = [[] for _ in range(worker_count)]
        loads = [0] * worker_count
        for duration, number in ordered:
            lightest = loads.index(min(loads))
            workers[lightest].append(number)
            loads[lightest] += duration
        cost = max(loads) + WORKER_COST_MS * worker_count
        if cost < best_cost:
            best_cost = cost
            best_workers = workers
    return best_workers


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    coverage = json.loads(MAP.read_text())
    tests_by_path = {path: set(ids) for path, ids in coverage.get("paths", {}).items()}
    measured_ids = coverage.get("measured", [])
    measured = set(measured_ids)
    functions = coverage.get("functions", {})
    files_by_test_id = {test["id"]: test["file"] for test in tests}
    fixtures = {
        path: set(ids) & files_by_test_id.keys()
        for path, ids in json.loads(FIXTURES.read_text()).items()
    }
    fixture_paths = {path for path, ids in fixtures.items() if ids}
    known_paths = tests_by_path.keys() | fixture_paths
    full_suite = best_schedule(tests)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        lines = changed_lines(patch, fixture_paths)
        paths = lines.keys()
        workers = full_suite
        if paths and paths <= known_paths:
            fixture_tests = set().union(*(fixtures[path] for path in paths & fixture_paths))
            direct_tests = set(fixture_tests)
            covered_files = set()
            for path in paths - fixture_paths:
                direct = function_tests(lines[path], functions.get(path, []), measured_ids)
                if direct:
                    direct_tests.update(direct)
                else:
                    covered_files.update(
                        files_by_test_id[node] for node in tests_by_path[path] if node in files_by_test_id
                    )
            selected = [
                test for test in tests
                if test["file"] in covered_files or test["id"] in direct_tests or test["id"] not in measured
            ]
            if selected:
                workers = best_schedule(selected)
        print(json.dumps({"commit": patch.stem, "workers": workers}))
        print(f"{patch.stem}: selected {sum(map(len, workers))} of {len(tests)} tests", file=sys.stderr)


if __name__ == "__main__":
    main()
