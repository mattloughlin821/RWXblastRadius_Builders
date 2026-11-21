"""Run exact named unittest cases in a fresh interpreter."""

from contextlib import redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import signal
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


def main():
    root, cpu_limit, *test_ids = sys.argv[1:]
    signal.setitimer(signal.ITIMER_PROF, float(cpu_limit))
    sys.path.insert(0, root)
    with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
        if test_ids and test_ids[0] == "--benchmark":
            from benchmark_probe import PROFILE, matched_queries

            profile = json.loads(PROFILE.read_text())
            passed = matched_queries(profile, [int(index) for index in test_ids[1:]])
        else:
            passed = all(matched_pass(node) for node in test_ids)
    print(json.dumps(passed))


if __name__ == "__main__":
    main()
