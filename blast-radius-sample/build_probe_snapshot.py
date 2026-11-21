"""Bundle pinned test and source files for runtime verification."""

import argparse
import base64
import io
from pathlib import Path
import subprocess
import textwrap
import zipfile

BASELINE = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    checkout = args.checkout.resolve()
    source_id = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    if source_id != BASELINE:
        parser.error(f"checkout must be at {BASELINE}")
    if subprocess.check_output(["git", "diff", "HEAD", "--", "sqlglot", "tests", "LICENSE"], cwd=checkout):
        parser.error("tracked source and tests must match the pinned commit")
    files = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "sqlglot", "tests", "LICENSE"], cwd=checkout
    ).decode().split("\0")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as result:
        for name in sorted(files):
            path = checkout / name
            # The fast verifier does not run the executor's integration datasets.
            if name and not name.endswith(".csv.gz") and path.is_file() and not path.is_symlink():
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                result.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    encoded = base64.b85encode(archive.getvalue()).decode("ascii")
    args.output.write_text("\n".join(textwrap.wrap(encoded, width=100, break_on_hyphens=False)) + "\n")


if __name__ == "__main__":
    main()
