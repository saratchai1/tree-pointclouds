#!/usr/bin/env python3
"""Generate the standalone Samut Songkhram clean-stem POM V3 artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import clean_stem_pom_v3 as v3


ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output-directory", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    config = args.config.resolve() if args.config else None
    output = args.output_directory.resolve() if args.output_directory else None
    summary = v3.write_artifacts(root, output, config)
    print(json.dumps({
        "algorithm_version": summary["algorithm_version"],
        "status_counts": summary["status_counts"],
        "v2_coverage_comparison": summary["v2_coverage_comparison"],
        "field_verified": summary["field_verified"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
