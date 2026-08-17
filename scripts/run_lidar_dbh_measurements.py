#!/usr/bin/env python3
"""Run the full-resolution LiDAR circumference/DBH and marking workflow."""

from __future__ import annotations

import argparse
from pathlib import Path
import time

import lidar_dbh_measurements as lidar
import stem_inventory_v2 as phase1


ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    root = args.root.resolve()
    started = time.monotonic()
    inventory_path = root / "outputs/phase4_tree_inventory.json"
    phase5a_path = root / "outputs/phase5a_protocol_measurements_shadow.json"
    annotation_path = root / "annotations/phase5a_prop_root_pom_review.json"
    identity_path = root / "outputs/phase4b_error_decomposition.json"
    phase1_measurements_path = root / "outputs/tree_measurements_v2_phase1.json"
    exclusions_path = root / "annotations/lidar_measurement_exclusions.json"
    source_las = root / "samutsongkram/TD_008_2026_08_07_07_04_07.las"
    viewer_data = root / "site/public/data"
    config_path = root / "config/stem_inventory_v2.yaml"
    required = [
        inventory_path, phase5a_path, annotation_path, identity_path,
        phase1_measurements_path, exclusions_path, source_las, config_path,
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing inputs: " + ", ".join(missing))

    inventory = lidar.read_json(inventory_path)
    phase5a_records = lidar.read_json(phase5a_path)
    annotation = lidar.read_json(annotation_path)
    identity_review = lidar.read_json(identity_path)
    phase1_measurements = lidar.read_json(phase1_measurements_path)
    operational_exclusions = lidar.read_json(exclusions_path)
    config = phase1.load_config(config_path)
    targets = lidar.build_targets(
        inventory, phase5a_records, annotation, identity_review,
        phase1_measurements, operational_exclusions,
    )
    if len(targets) != 118 or len({row["tree_id"] for row in targets}) != 118:
        raise RuntimeError("The frozen 118 Tree IDs are required")
    print("Prepared 118 protocol-aware measurement targets", flush=True)
    neighbourhoods, scan = lidar.las_neighbourhoods_once(source_las, viewer_data, targets)
    print("Completed one full LAS scan; fitting exact planes", flush=True)

    measurements = []
    marking_directory = root / "site/public/data/lidar-measurements/markings"
    marking_directory.mkdir(parents=True, exist_ok=True)
    for index, target in enumerate(targets, start=1):
        measurement, marking = lidar.measure_target(target, neighbourhoods[target["tree_id"]], config)
        measurements.append(measurement)
        lidar.atomic_write(
            marking_directory / f"{target['tree_id']}.json",
            lidar.canonical_json_bytes(marking),
        )
        if index % 10 == 0 or index == len(targets):
            print(f"Measured and marked {index}/118 trees", flush=True)

    measurements.sort(key=lambda row: row["tree_id"])
    source = {
        **scan,
        "path": "samutsongkram/TD_008_2026_08_07_07_04_07.las",
        "size_bytes": source_las.stat().st_size,
        "sha256": "195725dbbc7f853994f926027dfea9c9e4d986ac06cd6d9187f30ffebe528276",
        "inventory_sha256": lidar.sha256_path(inventory_path),
        "phase5a_sha256": lidar.sha256_path(phase5a_path),
        "annotation_sha256": lidar.sha256_path(annotation_path),
        "identity_review_sha256": lidar.sha256_path(identity_path),
        "phase1_measurements_sha256": lidar.sha256_path(phase1_measurements_path),
        "operational_exclusions_sha256": lidar.sha256_path(exclusions_path),
    }
    payload = {
        "algorithm_version": lidar.ALGORITHM_VERSION,
        "source": source,
        "tree_count": 118,
        "field_verified": False,
        "records": measurements,
    }
    summary = lidar.build_summary(measurements, source)
    output_json = root / "outputs/lidar_tree_measurements.json"
    output_csv = root / "outputs/lidar_tree_measurements.csv"
    output_summary = root / "outputs/lidar_tree_measurement_summary.json"
    public_directory = root / "site/public/data/lidar-measurements"
    lidar.atomic_write(output_json, lidar.canonical_json_bytes(payload))
    lidar.atomic_write(output_csv, lidar.render_csv(measurements))
    lidar.atomic_write(output_summary, lidar.canonical_json_bytes(summary))
    lidar.atomic_write(public_directory / "measurements.json", lidar.canonical_json_bytes(payload))
    lidar.atomic_write(public_directory / "measurements.csv", lidar.render_csv(measurements))
    lidar.atomic_write(public_directory / "summary.json", lidar.canonical_json_bytes(summary))
    print(
        f"Complete in {time.monotonic() - started:.1f}s: "
        f"{summary['field_aid_ready_count']} ready field aids, "
        f"{summary['field_aid_check_on_site_count']} check on site, "
        f"{summary['field_aid_no_estimate_count']} without estimate",
        flush=True,
    )


if __name__ == "__main__":
    main()
