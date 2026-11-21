"""Index test modules and fixture readers in the pinned sqlglot source."""

import argparse
import ast
from collections import defaultdict
from pathlib import Path
import importlib.util
import json
import subprocess

BASELINE = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def module_name(path):
    parts = Path(path).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def imports(tree, package):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            name = node.module or ""
            if node.level:
                try:
                    name = importlib.util.resolve_name("." * node.level + name, package)
                except ImportError:
                    continue
            yield name
            yield from (f"{name}.{alias.name}" for alias in node.names)


def fixture_paths(node):
    if isinstance(node, ast.Call) and node.args:
        name = getattr(node.func, "attr", getattr(node.func, "id", ""))
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if name in {"load_sql_fixtures", "load_sql_fixture_pairs"}:
                yield first.value
            elif name == "check_file":
                yield f"optimizer/{first.value}.sql"
        if name == "join" and isinstance(first, ast.Name) and first.id == "FIXTURES_DIR":
            parts = node.args[1:]
            if parts and all(isinstance(part, ast.Constant) and isinstance(part.value, str) for part in parts):
                yield "/".join(part.value for part in parts)
    elif (
        isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
        and isinstance(node.left, ast.Name) and node.left.id == "FIXTURES_DIR"
        and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)
    ):
        yield node.right.value.lstrip("/")


def test_scopes(tree, roster):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            ids = {node_id for node_id in roster
                   if node_id.split("[", 1)[0].endswith("::" + node.name)}
            if ids:
                yield node.lineno, node.end_lineno, ids


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("test_roster", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.checkout.resolve()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if commit != BASELINE:
        parser.error(f"checkout must be at {BASELINE}")
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\x00")
    sources = {
        path: ast.parse((root / path).read_bytes())
        for path in tracked if path.startswith(("sqlglot/", "tests/")) and path.endswith(".py")
    }
    by_module = {module_name(path): path for path in sources}
    roster = defaultdict(set)
    for test in json.loads(args.test_roster.read_text())["tests"]:
        roster[test["file"]].add(test["id"])

    # Reverse import edges include packages, whose initializers run before their submodules.
    importers = defaultdict(set)
    for path, tree in sources.items():
        name = module_name(path)
        package = name if path.endswith("/__init__.py") else name.rpartition(".")[0]
        for imported in set(imports(tree, package)):
            parts = imported.split(".")
            for size in range(1, len(parts) + 1):
                dependency = by_module.get(".".join(parts[:size]))
                if dependency and dependency != path:
                    importers[dependency].add(path)

    dependents = {}
    for source in sorted(sources):
        pending = [source]
        seen = set()
        matched = set()
        while pending:
            path = pending.pop()
            if path in seen:
                continue
            seen.add(path)
            matched.update(roster[path])
            pending.extend(importers[path] - seen)
        if matched:
            dependents[source] = sorted(matched)

    fixture_root = "tests/fixtures/"
    fixtures = [path.removeprefix(fixture_root) for path in tracked if path.startswith(fixture_root)]
    fixture_readers = defaultdict(set)
    observers = defaultdict(set)
    for path, ids in sorted(roster.items()):
        if not ids or path not in sources:
            continue
        scopes = list(test_scopes(sources[path], ids))
        for node in ast.walk(sources[path]):
            for name in fixture_paths(node):
                consumers = next((tests for start, stop, tests in scopes if start <= node.lineno <= stop), ids)
                for fixture in fixtures:
                    if fixture == name or fixture.startswith(name.rstrip("/") + "/"):
                        fixture_readers[fixture_root + fixture].update(consumers)
        for node in ast.walk(sources[path]):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test_"):
                continue
            consumers = next((tests for start, _, tests in scopes if start == node.lineno), set())
            for child in ast.walk(node):
                # A package discovered through Path(package.__file__).parent is
                # subsequently read by tests that enumerate its source modules.
                if isinstance(child, ast.Attribute) and child.attr == "__file__":
                    module = ast.unparse(child.value)
                    package_file = by_module.get(module)
                    if package_file and package_file.endswith("/__init__.py") and any(
                        isinstance(call, ast.Attribute) and call.attr == "read_text"
                        for call in ast.walk(node)
                    ):
                        prefix = package_file.removesuffix("__init__.py")
                        for candidate in sources:
                            if candidate.startswith(prefix):
                                observers[candidate].update(consumers)
                # A subprocess import test observes module initialization even
                # when the parent coverage recorder never sees the subprocess.
                if isinstance(child, ast.Constant) and isinstance(child.value, str) and child.value.startswith("import sqlglot;"):
                    try:
                        code = ast.parse(child.value)
                    except SyntaxError:
                        continue
                    for imported in imports(code, ""):
                        if imported in by_module:
                            observers[by_module[imported]].update(consumers)
    test_ids = sorted(set().union(*roster.values()))
    weights = {node_id: 1 << index for index, node_id in enumerate(test_ids)}
    payload = {"test_ids": test_ids}
    for kind, paths in (("imports", dependents), ("fixtures", fixture_readers), ("observers", observers)):
        payload[kind] = {path: sum(weights[node_id] for node_id in ids) for path, ids in paths.items() if ids}
    args.output.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
