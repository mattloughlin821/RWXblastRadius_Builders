"""Record sqlglot dependencies in baseline optimizer jobs and executor inputs."""

import argparse
import ast
from collections import defaultdict
from concurrent.futures import Future
import json
from pathlib import Path
import pickle
import subprocess
import sys
from types import ModuleType
from unittest.mock import Mock

BASELINE = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


class WorkerCoverage:
    def __init__(self, checkout: Path):
        self.checkout = checkout
        self.source_prefix = str(checkout / "sqlglot") + "/"
        self.relative_files = {}
        self.lines = defaultdict(lambda: defaultdict(set))
        self.test_id = ""

    def trace(self, frame, event, arg):
        if event != "call":
            return None
        filename = frame.f_code.co_filename
        if filename not in self.relative_files:
            relative = filename[len(str(self.checkout)) + 1:]
            self.relative_files[filename] = relative if filename.startswith(self.source_prefix) else ""
        relative = self.relative_files[filename]
        if not relative:
            return None

        def record_line(frame, event, arg):
            if event == "line":
                self.lines[relative][frame.f_lineno].add(self.test_id)
            return record_line

        return record_line

    def submit(self, function, *args, **kwargs):
        future = Future()
        sys.settrace(self.trace)
        try:
            # ProcessPoolExecutor serializes arguments and results in addition to running the job.
            for value in args:
                if value.__class__.__module__.startswith("sqlglot"):
                    pickle.dumps(value)
            value = function(*args, **kwargs)
            pickle.dumps(value)
            future.set_result(value)
        except Exception as error:
            future.set_exception(error)
        finally:
            sys.settrace(None)
        return future

    def shutdown(self):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    checkout = args.checkout.resolve()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    if commit != BASELINE:
        parser.error(f"checkout must be at {BASELINE}")
    sys.path.insert(0, str(checkout))

    # The optional database comparison runs in the parent. Replace it so this
    # profiler can collect worker activity using the standard library alone.
    duckdb = ModuleType("duckdb")
    duckdb.connect = Mock()
    pandas = ModuleType("pandas")
    pandas_testing = ModuleType("pandas.testing")
    pandas_testing.assert_frame_equal = lambda *args, **kwargs: None
    sys.modules.update({"duckdb": duckdb, "pandas": pandas, "pandas.testing": pandas_testing})
    from sqlglot import optimizer, parse_one
    from tests import test_optimizer
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs

    coverage = WorkerCoverage(checkout)
    test_optimizer.ProcessPoolExecutor = lambda **kwargs: coverage
    test_class = test_optimizer.TestOptimizer
    test_class.setUpClass()
    source = ast.parse((checkout / "tests/test_optimizer.py").read_bytes())
    for node in ast.walk(source):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        if not any(isinstance(child, ast.Attribute) and child.attr == "check_file" for child in ast.walk(node)):
            continue
        instance = test_class(node.name)
        instance.setUp()
        coverage.test_id = f"tests/test_optimizer.py::TestOptimizer::{node.name}"
        try:
            getattr(instance, node.name)()
        except Exception as error:
            # Jobs queued before a parent comparison failed have already been recorded.
            print(f"{node.name}: parent comparison stopped ({type(error).__name__})", file=sys.stderr)

    # The two executor benchmarks call the optimizer on their baseline fixture
    # inputs. Record that dependency without running the database comparisons.
    for name, schema, fixture in (
        ("test_execute_tpch", TPCH_SCHEMA, "optimizer/tpc-h/tpc-h.sql"),
        ("test_execute_tpcds", TPCDS_SCHEMA, "optimizer/tpc-ds/tpc-ds.sql"),
    ):
        coverage.test_id = f"tests/test_executor.py::TestExecutor::{name}"
        for meta, sql, _ in load_sql_fixture_pairs(fixture):
            if not meta.get("execute"):
                continue
            sys.settrace(coverage.trace)
            try:
                expression = parse_one(sql)
                pickle.dumps(expression)
                optimizer.optimize(expression, schema=schema, leave_tables_isolated=True)
            except Exception as error:
                print(f"{name}: query stopped ({type(error).__name__})", file=sys.stderr)
            finally:
                sys.settrace(None)

    result = {
        path: {number: sorted(ids) for number, ids in lines.items()}
        for path, lines in coverage.lines.items()
    }
    args.output.write_text(json.dumps(result))


if __name__ == "__main__":
    main()
