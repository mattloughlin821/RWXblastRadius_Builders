"""Replay executor optimizer inputs affected by an isolated rule edit."""

import ast
import hashlib
import json
from pathlib import Path
import pickle
import sys

HERE = Path(__file__).resolve().parent
PROFILE = HERE / "benchmark-profile.json"
EXECUTOR_TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
RULES_DIR = "sqlglot/optimizer"
# These rule modules have no consumers in executor setup, the reference
# transpilation, the planner, or the execution after optimize() returns.
RULE_MODULES = frozenset({
    "eliminate_ctes", "eliminate_joins", "eliminate_subqueries", "isolate_table_selects",
    "merge_subqueries", "optimize_joins", "pushdown_predicates", "pushdown_projections",
    "unnest_subqueries",
})


class PredicateShape(ast.NodeTransformer):
    """Normalize comparison operators and integer bounds in a predicate."""

    def visit_Compare(self, node):
        self.generic_visit(node)
        node.ops = [ast.Eq() for _ in node.ops]
        node.comparators = [
            ast.Constant(value=0) if isinstance(operand, ast.Constant) and type(operand.value) is int else operand
            for operand in node.comparators
        ]
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        node.op = ast.And()
        return node


def changed_guard(original, updated, changes):
    if len(updated) != 1:
        return None
    path, source = next(iter(updated.items()))
    module = Path(path)
    if module.parent.as_posix() != RULES_DIR or module.stem not in RULE_MODULES:
        return None
    change = changes[path]
    if change.edit_runs != 1 or len(change.old_code) != 1 or len(change.new_code) != 1:
        return None
    line, _ = change.old_code[0]
    if "#" in change.old_code[0][1] or "#" in change.new_code[0]:
        return None

    try:
        before, after = (
            ast.parse(code, type_comments=True) for code in (original[path], source)
        )
        serialized = ast.dump(before)
        if serialized == ast.dump(after):
            return None
        guards = []
        for tree in (before, after):
            containing = [
                node for function in tree.body
                if isinstance(function, ast.FunctionDef)
                and function.lineno < line <= function.end_lineno
                for node in ast.walk(function) if isinstance(node, ast.If)
                and node.test.lineno <= line <= node.test.end_lineno
            ]
            if len(containing) != 1:
                return None
            guard, = containing
            guards.append((guard.lineno, guard.test.end_lineno))
            guard.test = PredicateShape().visit(guard.test)
        if guards[0] == guards[1] and ast.dump(before) == ast.dump(after):
            return path, *guards[0]
    except (SyntaxError, ValueError, RecursionError):
        pass
    return None


def executor_inputs():
    from tests.helpers import load_sql_fixture_pairs

    # test_execute_tpcds parses all queries; the optimizer is called only
    # for the queries that are marked execute in the pinned fixtures.
    return [
        sql for metadata, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")
        if metadata.get("execute")
    ]


def optimizer_digest(sql):
    from sqlglot import optimizer, parse_one
    from tests.helpers import TPCDS_SCHEMA

    # The executor passes this tree through a multiprocessing pickle round trip.
    expression = pickle.loads(pickle.dumps(parse_one(sql)))
    optimized = optimizer.optimize(expression, TPCDS_SCHEMA, leave_tables_isolated=True)
    digest = hashlib.sha256(pickle.dumps(optimized))
    digest.update(optimized.sql(pretty=True).encode())
    return digest.hexdigest()


def build_profile(root):
    modules = {str(root / RULES_DIR / (name + ".py")): name for name in RULE_MODULES}
    coverage = {name: {} for name in modules.values()}
    digests = []
    for index, sql in enumerate(executor_inputs()):
        visited = set()

        def collect(frame, event, arg):
            if event == "line":
                visited.add((modules[frame.f_code.co_filename], frame.f_lineno))
            return collect

        def trace(frame, event, arg):
            if frame.f_code.co_filename in modules:
                return collect
            return None

        sys.settrace(trace)
        try:
            digests.append(optimizer_digest(sql))
        finally:
            sys.settrace(None)
        for name, line in visited:
            module_coverage = coverage[name]
            module_coverage[line] = module_coverage.get(line, 0) | (1 << index)
    profile = {"digests": digests, "coverage": coverage}
    PROFILE.write_text(json.dumps(profile, sort_keys=True, separators=(",", ":")) + "\n")


def matched_queries(profile, indices):
    queries = executor_inputs()
    digests = profile["digests"]
    return len(queries) == len(digests) and all(
        optimizer_digest(queries[index]) == digests[index] for index in indices
    )


if __name__ == "__main__":
    baseline = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(baseline))
    build_profile(baseline)
