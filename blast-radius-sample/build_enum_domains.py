"""Describe enum-valued fields inferred from pinned sqlglot source assignments."""

import argparse
import ast
from collections import defaultdict
import enum
import importlib
import json
from pathlib import Path
import subprocess
import sys

BASELINE = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
EQUALITY = (object.__eq__, str.__eq__, int.__eq__)


def assignments(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield target, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            yield node.target, node.value
        elif isinstance(node, ast.AugAssign):
            yield node.target, None


def member_type(value, enums):
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in enums:
        return value.func.id
    if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
        name = value.value.id
        if value.attr in enums.get(name, ()):
            return name
    return None


def inferred_type(values, resolve):
    types = {resolve(value) for value in values}
    if len(types) == 1:
        return next(iter(types))
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.checkout.resolve()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if commit != BASELINE:
        parser.error(f"checkout must be at {BASELINE}")
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\x00")
    sources = {
        path: ast.parse((root / path).read_bytes())
        for path in tracked if path.endswith(".py") and path.startswith(("sqlglot/", "tests/"))
    }
    sys.path.insert(0, str(root))

    class_constants = defaultdict(list)
    mutated_by_name = set()
    attribute_writes = defaultdict(list)
    enum_candidates = []
    for path, tree in sources.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if path.startswith("sqlglot/") and node in tree.body and any(
                    ast.unparse(base).split(".")[-1] in {"Enum", "IntEnum", "StrEnum", "AutoName"}
                    for base in node.bases
                ):
                    enum_candidates.append((path, node.name))
                for statement in node.body:
                    if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                        for target, value in assignments(statement):
                            if isinstance(target, ast.Name):
                                class_constants[target.id].append(value)
            elif isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                if name in {"setattr", "delattr", "object", "patch"}:
                    mutated_by_name.update(
                        argument.value.rsplit(".", 1)[-1]
                        for argument in node.args
                        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                    )
        for target, value in assignments(tree):
            if isinstance(target, ast.Attribute):
                attribute_writes[target.attr].append((target, value))

    domains = {}
    for path, name in enum_candidates:
        module_name = path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
        module = importlib.import_module(module_name)
        kind = getattr(module, name)
        if not isinstance(kind, type) or not issubclass(kind, enum.Enum) or kind.__eq__ not in EQUALITY:
            continue
        if all((left == right) == (left is right) and (left != right) == (left is not right)
               for left in kind for right in kind):
            domains.setdefault(path, {"enums": {}, "scopes": []})["enums"][name] = sorted(kind.__members__)

    for path, domain in domains.items():
        enums = domain["enums"]
        tree = sources[path]
        for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            constructor = next((node for node in cls.body
                                if isinstance(node, ast.FunctionDef) and node.name == "__init__"), None)
            if constructor is None or not constructor.args.args or constructor.args.args[0].arg != "self":
                continue
            if any(isinstance(node, ast.FunctionDef) and node.name in {"__getattr__", "__getattribute__", "__setattr__"}
                   for node in cls.body):
                continue
            writes = defaultdict(list)
            for target, value in assignments(constructor):
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                    writes[target.attr].append((target, value))

            def resolve(value):
                if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id == "self":
                    if value.attr not in mutated_by_name and class_constants[value.attr]:
                        defaults = class_constants[value.attr] + [new for _, new in attribute_writes[value.attr]]
                        return inferred_type(defaults, lambda item: member_type(item, enums))
                    return None
                return member_type(value, enums)

            fields = {}
            for name, occurrences in writes.items():
                if name in mutated_by_name or name in class_constants:
                    continue
                if len(occurrences) != len(attribute_writes[name]):
                    continue
                nominal = inferred_type((value for _, value in occurrences), resolve)
                if nominal:
                    fields[name] = nominal
            if not fields:
                continue
            for method in cls.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not method.args.args or method.args.args[0].arg != "self":
                    continue
                if any(isinstance(node, ast.Name) and node.id == "self" and isinstance(node.ctx, ast.Store)
                       for node in ast.walk(method)):
                    continue
                domain["scopes"].append([method.lineno, method.end_lineno, fields])
    eligible = {path: domain for path, domain in domains.items() if domain["scopes"]}
    args.output.write_text(json.dumps(eligible, sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
