"""Generate the coverage map from a clean checkout of the pinned baseline."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from coverage import Coverage
import pytest

BASELINE = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


class RecordCoverage:
    def __init__(self, coverage):
        self.coverage = coverage
        self.measured = set()

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item):
        self.coverage.switch_context(item.nodeid)
        yield
        self.coverage.switch_context("")

    def pytest_runtest_logreport(self, report):
        if report.when == "call" and report.passed:
            self.measured.add(report.nodeid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    args = parser.parse_args()
    checkout = args.checkout.resolve()
    output = Path(__file__).resolve().with_name("coverage-map.json")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=checkout, text=True)
    if commit != BASELINE or status:
        parser.error(f"checkout must be clean and at {BASELINE}")

    os.chdir(checkout)
    sys.path.insert(0, str(checkout))
    os.environ["SKIP_INTEGRATION"] = "0"
    coverage = Coverage(source=[str(checkout / "sqlglot")], config_file=False)
    recorder = RecordCoverage(coverage)
    coverage.start()
    try:
        result = pytest.main(["-q", "-o", "addopts=", "tests"], plugins=[recorder])
    finally:
        coverage.stop()
    if result != pytest.ExitCode.OK:
        return int(result)

    data = coverage.get_data()
    paths = {}
    for filename in data.measured_files():
        path = Path(filename).relative_to(checkout).as_posix()
        ids = {
            context
            for contexts in data.contexts_by_lineno(filename).values()
            for context in contexts
            if context
        }
        if ids:
            paths[path] = sorted(ids)
    output.write_text(json.dumps({"measured": sorted(recorder.measured), "paths": paths}, sort_keys=True) + "\n")
    print(f"Mapped {len(paths)} source files across {len(recorder.measured)} tests to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
