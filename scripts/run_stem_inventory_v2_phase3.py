#!/usr/bin/env python3
"""Generate Phase 3 tree-inventory outputs from locked existing evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

import yaml

import stem_inventory_v2_phase3 as phase3


ROOT = Path(__file__).resolve().parent.parent


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-viewer-copy", action="store_true")
    args = parser.parse_args()

    paths = {
        "phase1_candidates": ROOT / "outputs/tree_candidates_v2_phase1.json",
        "phase1_measurements": ROOT / "outputs/tree_measurements_v2_phase1.json",
        "tracks": ROOT / "outputs/tree_tracks_v2_phase1_5.json",
        "aliases": ROOT / "outputs/candidate_alias_map_v2_phase1_5.json",
        "annotations": ROOT / "annotations/phase1_75_pilot_review.json",
        "phase2": ROOT / "outputs/manual_seed_evaluations_v2_phase2_anchor_pilot.json",
        "phase2_recheck": ROOT / "outputs/phase2_manual_anchor_measurement_recheck.json",
        "phase1_config": ROOT / "config/stem_inventory_v2.yaml",
        "phase3_config": ROOT / "config/stem_inventory_v2_phase3.yaml",
    }
    inputs = {name: read_json(path) for name, path in paths.items() if path.suffix == ".json"}
    phase1_config = yaml.safe_load(paths["phase1_config"].read_text(encoding="utf-8"))
    phase3_config = phase3.load_config(paths["phase3_config"])
    registry_path = ROOT / "outputs/phase3_tree_id_registry.json"
    prior_registry = read_json(registry_path) if registry_path.exists() else None
    inventory, associations, summary, uncertainty = phase3.build_phase3(
        inputs["phase1_candidates"], inputs["phase1_measurements"], inputs["tracks"],
        inputs["aliases"], inputs["annotations"], inputs["phase2"],
        inputs["phase2_recheck"], phase1_config, phase3_config, prior_registry,
    )
    provenance = {
        name: {"path": str(path.relative_to(ROOT)), "sha256": sha256(path)}
        for name, path in paths.items()
    }
    for payload in (inventory, associations, summary, uncertainty):
        payload["input_provenance"] = provenance

    outputs = {
        "phase3_tree_inventory.json": inventory,
        "phase3_candidate_tree_associations.json": associations,
        "phase3_inventory_summary.json": summary,
        "phase3_failure_uncertainty_report.json": uncertainty,
        "phase3_tree_id_registry.json": {
            "algorithm_version": phase3.ALGORITHM_VERSION,
            "stability_scope": "UNCHANGED_INFERRED_TREE_KEY_ACROSS_RERUNS",
            "tree_key_to_tree_id": {tree["tree_key"]: tree["tree_id"] for tree in inventory["trees"]},
        },
    }
    for name, payload in outputs.items():
        write_json(ROOT / "outputs" / name, payload)
    if not args.no_viewer_copy:
        viewer_data = ROOT / "site/public/viewer-v2-review/data"
        viewer_data.mkdir(parents=True, exist_ok=True)
        for name in ("phase3_tree_inventory.json", "phase3_candidate_tree_associations.json"):
            shutil.copyfile(ROOT / "outputs" / name, viewer_data / name)

    print(json.dumps({
        "tree_count": inventory["tree_count"],
        "detection_status_counts": summary["detection_status_counts"],
        "measurement_status_counts": summary["measurement_status_counts"],
        "stage_counts": summary["stage_counts"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
