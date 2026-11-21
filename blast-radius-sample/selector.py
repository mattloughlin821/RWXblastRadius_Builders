"""Select tests from baseline coverage, source structure, and fixture dependencies."""

import ast
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sys

DATA = Path("/data")
IMPACT_MAP = Path(__file__).with_name("impact-map.json")
FIXTURES = Path(__file__).with_name("optimizer-fixtures.json")
DEPENDENCIES = Path(__file__).with_name("test-dependencies.json")
MAX_WORKERS = 8
WORKER_COST_MS = 2000
DIFF_HEADER = re.compile(r"diff --git a/(\S+) b/(\S+)")
HUNK_HEADER = re.compile(r"@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")
ASSIGNMENT = re.compile(r"([A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)")
DIRECTIVE = re.compile(r"coding[:=]|type:|noqa|pragma:|fmt:", re.IGNORECASE)


@dataclass
class FileChange:
    removed: set[int] = field(default_factory=set)
    inserted: set[int] = field(default_factory=set)
    touched: set[int] = field(default_factory=set)
    text: list[str] = field(default_factory=list)
    structural: bool = False


def read_patch(patch: Path) -> dict[str, FileChange]:
    changes = {}
    old_line = None
    current = None
    removed_run = set()
    inserted_run = set()

    def finish_run():
        if current is not None:
            current.touched.update(removed_run or inserted_run)
        removed_run.clear()
        inserted_run.clear()

    for line in patch.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("diff --git "):
            finish_run()
            header = DIFF_HEADER.fullmatch(line)
            if not header or header[1] != header[2]:
                return {}
            current = changes.setdefault(header[1], FileChange())
            old_line = None
        elif line.startswith(("new file mode ", "deleted file mode ", "GIT binary patch", "Binary files ")):
            if current is None:
                return {}
            current.structural = True
        elif hunk := HUNK_HEADER.match(line):
            if current is None:
                return {}
            finish_run()
            old_line = int(hunk[1])
        elif old_line is not None and line.startswith(" "):
            finish_run()
            old_line += 1
        elif old_line is not None and line.startswith("-"):
            removed_run.add(old_line)
            current.removed.add(old_line)
            current.text.append(line[1:])
            old_line += 1
        elif old_line is not None and line.startswith("+"):
            inserted_run.update((old_line - 1, old_line))
            current.inserted.update((old_line - 1, old_line))
            current.text.append(line[1:])
    finish_run()
    return changes


def union(masks):
    result = 0
    for mask in masks:
        result |= mask
    return result


def changed_bindings(change: FileChange):
    for text in change.text:
        code = text.strip()
        if match := ASSIGNMENT.match(code):
            yield match[1]
        if code.startswith(("import ", "from ")):
            try:
                statement = ast.parse(code).body[0]
            except (SyntaxError, IndexError):
                continue
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                for alias in statement.names:
                    yield alias.asname or alias.name.split(".", maxsplit=1)[0]


def source_tests(path, change, impact):
    entry = impact["files"][path]
    masks = impact["masks"]
    touched = change.touched
    if not touched:
        return masks[entry["full"]]

    scopes = {
        min(
            (region for region in entry["functions"] if region[0] < line <= region[1]),
            key=lambda region: region[1] - region[0],
            default=(0, 0, 0),
        )[0]
        for line in touched
    }
    def scoped_tests():
        return union(masks[index] for first, _, index in entry["functions"] if first in scopes)

    # Source tokenization rules out comment-looking lines inside strings and continuations.
    if (change.text and all(
        text.lstrip().startswith("#") and not text.startswith("#!") and not DIRECTIVE.search(text)
        for text in change.text
    ) and not (change.removed | change.inserted) & set(entry["unsafe_trivia"])):
        return scoped_tests()

    selected = union(masks[entry["lines"].get(str(line), 0)] for line in touched)
    selected |= union(masks[entry["guards"].get(str(line), 0)] for line in touched)
    if 0 in scopes:
        references = impact["references"]
        related = union(masks[references.get(symbol, 0)] for symbol in changed_bindings(change))
        source = Path(path)
        if source.parent.name == "dialects" and source.stem != "dialect":
            related &= masks[impact["families"].get(source.stem, 0)]
        selected |= related
    return selected or scoped_tests() or masks[entry["full"]]


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
    impact = json.loads(IMPACT_MAP.read_text())
    weights = {node_id: 1 << index for index, node_id in enumerate(impact["test_ids"])}
    dependencies = json.loads(DEPENDENCIES.read_text())
    dependency_weights = {node_id: 1 << index for index, node_id in enumerate(dependencies["test_ids"])}
    weighted_tests = [(weights.get(test["id"], 0), dependency_weights.get(test["id"], 0), test) for test in tests]
    fixtures = {
        path: union(weights.get(node_id, 0) for node_id in node_ids)
        for path, node_ids in json.loads(FIXTURES.read_text()).items()
    }
    fixtures = {path: mask for path, mask in fixtures.items() if mask}
    file_readers = dependencies["fixtures"]
    imports = dependencies["imports"]
    observers = dependencies["observers"]
    known = impact["files"].keys() | fixtures.keys() | file_readers.keys() | imports.keys() | observers.keys()
    full_suite = best_schedule(tests)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        changes = read_patch(patch)
        if changes and changes.keys() <= known:
            selected = 0
            indirect = 0
            for path, change in changes.items():
                if path in fixtures or path in file_readers:
                    selected |= fixtures.get(path, 0)
                    indirect |= file_readers.get(path, 0)
                elif path in impact["files"]:
                    if change.structural:
                        selected |= impact["masks"][impact["files"][path]["full"]]
                        indirect |= imports.get(path, 0)
                    else:
                        selected |= source_tests(path, change, impact)
                else:
                    indirect |= imports.get(path, 0)
                indirect |= observers.get(path, 0)
            relevant = [
                test for direct_bit, indirect_bit, test in weighted_tests
                if not direct_bit or direct_bit & selected or indirect_bit & indirect
            ]
            workers = best_schedule(relevant)
        else:
            workers = full_suite
        print(json.dumps({"commit": patch.stem, "workers": workers}))
        print(f"{patch.stem}: selected {sum(map(len, workers))} of {len(tests)} tests", file=sys.stderr)


if __name__ == "__main__":
    main()
