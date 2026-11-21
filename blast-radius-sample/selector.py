"""Select tests from baseline coverage, source structure, and fixture dependencies."""

import ast
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sys
import tempfile

from benchmark_probe import EXECUTOR_TPCDS
from class_dependencies import class_dependency_mask
from verification import Verification, probe_tests

DATA = Path("/data")
IMPACT_MAP = Path(__file__).with_name("impact-map.json")
FIXTURES = Path(__file__).with_name("optimizer-fixtures.json")
DEPENDENCIES = Path(__file__).with_name("test-dependencies.json")
ENUM_DOMAINS = Path(__file__).with_name("enum-domains.json")
CLASS_DOMAINS = Path(__file__).with_name("class-domains.json")
MAX_WORKERS = 8
WORKER_COST_MS = 2000
DIFF_HEADER = re.compile(r"diff --git a/(\S+) b/(\S+)")
HUNK_HEADER = re.compile(r"@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")
ASSIGNMENT = re.compile(r"([A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)")
DIRECTIVE = re.compile(r"coding[:=]|type:|noqa|pragma:|fmt:", re.IGNORECASE)
CONDITION = re.compile(r"(\s*)(if |elif |return )(.+?)(:?)")


@dataclass
class FileChange:
    removed: set[int] = field(default_factory=set)
    inserted: set[int] = field(default_factory=set)
    touched: set[int] = field(default_factory=set)
    text: list[str] = field(default_factory=list)
    structural: bool = False
    old_code: list[tuple[int, str]] = field(default_factory=list)
    new_code: list[str] = field(default_factory=list)
    edit_runs: int = 0


def read_patch(patch: Path) -> dict[str, FileChange]:
    changes = {}
    old_line = None
    current = None
    removed_run = set()
    inserted_run = set()

    def finish_run():
        if current is not None and (removed_run or inserted_run):
            current.touched.update(removed_run or inserted_run)
            current.edit_runs += 1
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
            current.old_code.append((old_line, line[1:]))
            old_line += 1
        elif old_line is not None and line.startswith("+"):
            inserted_run.update((old_line - 1, old_line))
            current.inserted.update((old_line - 1, old_line))
            current.text.append(line[1:])
            current.new_code.append(line[1:])
    finish_run()
    return changes


def union(masks):
    result = 0
    for mask in masks:
        result |= mask
    return result


def enum_operand(node, members, fields):
    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
        return None
    name = node.value.id
    if name == "self":
        return fields.get(node.attr)
    if node.attr in members.get(name, ()):
        return name
    return None


def same_enum_comparison(before, after, members, fields):
    if "#" in before or "#" in after:
        return False
    old = CONDITION.fullmatch(before)
    new = CONDITION.fullmatch(after)
    if not old or not new or (old[1], old[2], old[4]) != (new[1], new[2], new[4]):
        return False
    try:
        original, replacement = (ast.parse(match[3], mode="eval").body for match in (old, new))
    except SyntaxError:
        return False
    if not all(isinstance(node, ast.Compare) and len(node.ops) == 1 for node in (original, replacement)):
        return False
    operators = frozenset((type(original.ops[0]), type(replacement.ops[0])))
    if operators not in (frozenset((ast.Is, ast.Eq)), frozenset((ast.IsNot, ast.NotEq))):
        return False
    if any(ast.dump(a) != ast.dump(b) for a, b in zip(
        (original.left, *original.comparators), (replacement.left, *replacement.comparators)
    )):
        return False
    left, right = (enum_operand(node, members, fields) for node in (original.left, original.comparators[0]))
    return left is not None and left == right


def equivalent_enum_edit(change, entry, domain):
    if change.structural or change.edit_runs != 1 or len(change.old_code) != 1 or len(change.new_code) != 1:
        return False
    if (change.removed | change.inserted) & set(entry["unsafe_trivia"]):
        return False
    (line, before), = change.old_code
    after, = change.new_code
    scopes = [fields for start, end, fields in domain["scopes"] if start < line <= end]
    return len(scopes) == 1 and same_enum_comparison(before, after, domain["enums"], scopes[0])


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


def possible_trivia(change):
    if not change.text or any(line.lstrip().startswith("#!") or DIRECTIVE.search(line) for line in change.text):
        return False
    # This is just a cheap prefilter. An AST comparison checks string contents,
    # type comments, and indentation before treating an edit as trivia.
    old = [line.partition("#")[0].strip() for _, line in change.old_code]
    new = [line.partition("#")[0].strip() for line in change.new_code]
    return list(filter(None, old)) == list(filter(None, new))


def select(verification) -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    impact = json.loads(IMPACT_MAP.read_text())
    weights = {node_id: 1 << index for index, node_id in enumerate(impact["test_ids"])}
    dependencies = json.loads(DEPENDENCIES.read_text())
    enum_domains = json.loads(ENUM_DOMAINS.read_text())
    class_domains = json.loads(CLASS_DOMAINS.read_text())
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
    durations = {test["number"]: test["seconds"] for test in tests}
    def cost(schedule):
        return WORKER_COST_MS / 1000 * len(schedule) + max(
            (sum(durations[number] for number in worker) for worker in schedule),
            default=0,
        )

    schedules = {}
    probes = []
    benchmark_probes = []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        changes = read_patch(patch)
        if changes and changes.keys() <= known:
            original, updated = verification.prepare(patch, changes)
            trivia_paths = {
                path for path, change in changes.items()
                if path in impact["files"] and possible_trivia(change)
                and verification.equivalent(path, original, updated)
            }
            isolated = len(changes) - len(trivia_paths) == 1
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
                    elif path in trivia_paths:
                        pass  # Source observers below can still see source-level changes.
                    elif not isolated or path not in enum_domains or not equivalent_enum_edit(
                        change, impact["files"][path], enum_domains[path]
                    ):
                        narrowed = None
                        if isolated:
                            narrowed = class_dependency_mask(path, change, original, updated, class_domains, impact)
                        selected |= narrowed if narrowed is not None else source_tests(path, change, impact)
                else:
                    indirect |= imports.get(path, 0)
                indirect |= observers.get(path, 0)
            relevant = [
                test for direct_bit, indirect_bit, test in weighted_tests
                if not direct_bit or direct_bit & selected or indirect_bit & indirect
            ]
            workers = best_schedule(relevant)
            if any(test["id"] == EXECUTOR_TPCDS for test in relevant):
                indices = verification.benchmark_queries(original, updated, changes)
                if indices is not None:
                    reduced = best_schedule([test for test in relevant if test["id"] != EXECUTOR_TPCDS])
                    savings = cost(workers) - cost(reduced)
                    if savings >= 3.0:
                        benchmark_probes.append((
                            savings / (1 + len(indices)), patch.stem, updated, indices, reduced,
                        ))
            if updated and (test_ids := probe_tests(relevant)):
                estimate = 0.1 + sum(test["seconds"] for test in relevant)
                probes.append((cost(workers) / estimate, patch.stem, updated, test_ids))
        else:
            workers = full_suite
        schedules[patch.stem] = workers

    for _, commit, updated, test_ids in sorted(probes, key=lambda probe: -probe[0]):
        if verification.all_pass(updated, test_ids):
            schedules[commit] = []

    for _, commit, updated, indices, reduced in sorted(benchmark_probes, key=lambda probe: -probe[0]):
        if verification.benchmark_passes(updated, indices):
            schedules[commit] = reduced

    for commit, workers in schedules.items():
        print(json.dumps({"commit": commit, "workers": workers}))
        print(f"{commit}: selected {sum(map(len, workers))} of {len(tests)} tests", file=sys.stderr)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="blast-radius-verify-") as destination:
        select(Verification(destination))


if __name__ == "__main__":
    main()
