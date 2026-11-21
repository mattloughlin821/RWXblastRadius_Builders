"""Build class-scope dependencies from the pinned sqlglot source."""

import argparse
import ast
from collections import defaultdict
import json
from pathlib import Path
import subprocess

BASELINE = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def dictionary_assignments(body, attribute):
    return [
        node for node in body if isinstance(node, ast.Assign) and len(node.targets) == 1
        and name(node.targets[0]) == attribute and isinstance(node.value, ast.Dict)
    ]


def descendants(parent, inheritance):
    result = {parent}
    pending = [parent]
    while pending:
        new = inheritance[pending.pop()] - result
        result.update(new)
        pending.extend(new)
    return sorted(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.checkout.resolve()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    dirty = subprocess.check_output(["git", "diff", "HEAD", "--", "sqlglot", "tests"], cwd=root)
    if commit != BASELINE or dirty:
        parser.error(f"checkout must match {BASELINE}")

    files = subprocess.check_output(["git", "ls-files", "-z", "--", "sqlglot"], cwd=root).decode().split("\0")
    inheritance = defaultdict(set)
    class_modules = defaultdict(set)
    dictionaries = []
    escape_reads = []
    for path in sorted(path for path in files if path.endswith(".py")):
        tree = ast.parse((root / path).read_bytes())
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                inheritance[name(base)].add(node.name)
            class_modules[node.name].add(Path(path).stem)

            if path.startswith("sqlglot/expressions/"):
                for assignment in dictionary_assignments(node.body, "arg_types"):
                    dictionaries.append((path, [node.name], node.name, "arg_types", assignment))

            if path.startswith("sqlglot/dialects/"):
                for nested in node.body:
                    if isinstance(nested, ast.ClassDef) and nested.name == "Tokenizer":
                        for assignment in dictionary_assignments(nested.body, "NUMERIC_ESCAPES"):
                            dictionaries.append((path, [node.name, nested.name], node.name, "NUMERIC_ESCAPES", assignment))

        if path == "sqlglot/tokenizer_core.py":
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                    and name(node.func.value) == "numeric_escapes"
                ):
                    escape_reads.append([path, node.lineno])

    domains = defaultdict(list)
    for path, scope, owner, attribute, assignment in dictionaries:
        children = descendants(owner, inheritance)
        targets = children
        if attribute == "NUMERIC_ESCAPES":
            targets = sorted({module for child in children for module in class_modules[child]})
        domains[path].append({
            "scope": scope, "attribute": attribute, "targets": targets,
            "lines": [assignment.lineno, assignment.end_lineno],
        })
    args.output.write_text(json.dumps({"domains": domains, "escape_reads": sorted(escape_reads)},
                                      sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
