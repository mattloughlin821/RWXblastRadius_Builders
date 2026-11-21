"""Run exact named unittest cases in a fresh interpreter."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest


def matched_pass(test_id):
    path, *names = test_id.split("::")
    if len(names) != 2 or not all(name.isidentifier() for name in names):
        return False
    mod = ".".join(Path(path).with_suffix("").parts)
    suite = unittest.defaultTestLoader.loadTestsFromName(".".join((mod, *names)))
    if suite.countTestCases() != 1:
        return False
    result = unittest.TestResult()
    suite.run(result)
    return result.testsRun == 1 and result.wasSuccessful() and not result.skipped and not result.expectedFailures


def optimizer_fingerprint():
    # These are the same inputs, schemas, and serialization boundary as the
    # executor's TPC-DS test. Execution after optimization stays unchanged for
    # the optimizer-only edits that are eligible for this probe.
    import pickle

    from sqlglot import optimizer, parse_one
    from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs

    digest = hashlib.sha256()
    for metadata, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"):
        if metadata.get("execute"):
            expression = pickle.loads(pickle.dumps(parse_one(sql)))
            optimized = optimizer.optimize(expression, TPCDS_SCHEMA, leave_tables_isolated=True)
            digest.update(pickle.dumps(optimized))
            digest.update(optimized.sql(pretty=True).encode())
    return digest.hexdigest()


def main():
    root, *test_ids = sys.argv[1:]
    sys.path.insert(0, root)
    if test_ids == ["--optimizer-fingerprint"]:
        with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
            digest = optimizer_fingerprint()
        print(json.dumps(digest))
        return
    confirmed = []
    with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
        for node in test_ids:
            try:
                if matched_pass(node):
                    confirmed.append(node)
            except Exception:
                continue
    print(json.dumps(confirmed))


if __name__ == "__main__":
    main()
