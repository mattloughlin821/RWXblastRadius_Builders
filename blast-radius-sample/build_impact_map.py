"""Build a compact dependency index using coverage from the pinned sqlglot checkout."""

import argparse
import ast
import io
import json
from collections import defaultdict
from pathlib import Path
import sqlite3
import subprocess
import tokenize

BASELINE = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("coverage_db", type=Path)
    parser.add_argument("worker_coverage", type=Path)
    parser.add_argument("--line-root", default="/baseline")
    parser.add_argument("--coarse-map", type=Path, default=Path("coverage-map.json"))
    parser.add_argument("--output", type=Path, default=Path("impact-map.json"))
    args = parser.parse_args()
    checkout = args.checkout.resolve()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    if commit != BASELINE:
        parser.error(f"checkout must be at {BASELINE}")

    test_ids = sorted(json.loads(args.coarse_map.read_text())["measured"])
    weights = {node_id: 1 << index for index, node_id in enumerate(test_ids)}
    lines = defaultdict(lambda: defaultdict(int))
    with sqlite3.connect(args.coverage_db.resolve().as_uri() + "?mode=ro", uri=True) as db:
        for filename, context, bits in db.execute(
            "SELECT path, context, numbits FROM line_bits "
            "JOIN file ON file.id = file_id JOIN context ON context.id = context_id"
        ):
            if context in weights:
                relative = Path(filename).relative_to(args.line_root).as_posix()
                for index, byte in enumerate(bits):
                    while byte:
                        bit = byte & -byte
                        lines[relative][index * 8 + bit.bit_length() - 1] |= weights[context]
                        byte ^= bit

    for relative, worker_lines in json.loads(args.worker_coverage.read_text()).items():
        for number, node_ids in worker_lines.items():
            for node_id in node_ids:
                lines[relative][int(number)] |= weights.get(node_id, 0)

    def union(masks):
        result = 0
        for mask in masks:
            result |= mask
        return result

    references = defaultdict(int)
    families = defaultdict(int)
    files = {}
    for relative, recorded in sorted(lines.items()):
        if not recorded or not relative.endswith(".py"):
            continue
        source = (checkout / relative).read_bytes()
        tree = ast.parse(source, type_comments=True)
        functions = []
        guards = defaultdict(int)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                mask = union(recorded.get(number, 0) for number in range(node.lineno + 1, node.end_lineno + 1))
                functions.append((node.lineno, node.end_lineno, mask))
            if isinstance(node, ast.BoolOp) and node.lineno != node.end_lineno:
                for number in range(node.lineno + 1, node.end_lineno + 1):
                    guards[number] |= recorded.get(node.lineno, 0)
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                references[node.attr] |= recorded.get(node.lineno, 0)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                references[node.id] |= recorded.get(node.lineno, 0)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
                references[node.value] |= recorded.get(node.lineno, 0)

        unsafe_trivia = {
            number
            for token in tokenize.tokenize(io.BytesIO(source).readline)
            if token.type == tokenize.STRING
            for number in range(token.start[0] + 1, token.end[0] + 1)
        }
        unsafe_trivia.update(
            number + 2 for number, line in enumerate(source.splitlines()) if line.rstrip().endswith(b"\\")
        )
        full_mask = union(recorded.values())
        families[Path(relative).stem] |= full_mask
        files[relative] = {
            "lines": dict(recorded),
            "functions": sorted(functions),
            "guards": {line: mask for line, mask in guards.items() if mask},
            "unsafe_trivia": sorted(unsafe_trivia),
            "full": full_mask,
        }

    masks = {0: 0}

    def index(mask):
        return masks.setdefault(mask, len(masks))

    for entry in files.values():
        entry["lines"] = {line: index(mask) for line, mask in entry["lines"].items()}
        entry["functions"] = [[first, last, index(mask)] for first, last, mask in entry["functions"]]
        entry["guards"] = {line: index(mask) for line, mask in entry["guards"].items()}
        entry["full"] = index(entry["full"])
    reference_indices = {name: index(mask) for name, mask in references.items() if mask}
    family_indices = {name: index(mask) for name, mask in families.items()}
    result = {
        "test_ids": test_ids,
        "files": files,
        "references": reference_indices,
        "families": family_indices,
        "masks": list(masks),
    }
    args.output.write_text(json.dumps(result, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
