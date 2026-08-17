#!/usr/bin/env python3
"""Audit Phase 1 candidate fragmentation before Phase 1.5 tracking changes."""

from __future__ import annotations

from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from scipy.spatial import cKDTree

import stem_inventory_v2 as v2


ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
DOCS = ROOT / "docs"
PHASE1_CANDIDATES = OUTPUTS / "tree_candidates_v2_phase1.json"
PHASE1_MEASUREMENTS = OUTPUTS / "tree_measurements_v2_phase1.json"

NEAR_XY_M = 0.30
PAIR_SEARCH_M = 1.00
POINT_PAIR_SEARCH_M = 3.20
SOURCE_LEVEL_HALF_WIDTH_M = 0.125


def rounded(value: Any, digits: int = 6):
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), digits)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def source_observations(candidate: dict, config: dict) -> list[dict]:
    profile = candidate["diagnostics"]["profile"]
    result = []
    by_height: dict[float, list[dict]] = {}
    for relationship in candidate["seed_relationships"]:
        height = relationship.get("source_height_m")
        if height is not None:
            by_height.setdefault(float(height), []).append(relationship)
    for height in sorted(by_height):
        source_xy = np.median(
            np.asarray([[item["x"], item["y"]] for item in by_height[height]]),
            axis=0,
        )
        entry = min(profile, key=lambda item: abs(item["height_m"] - height))
        fits = entry.get("fits", [])
        selected = (
            min(fits, key=lambda fit: np.linalg.norm(np.asarray(fit["center"]) - source_xy))
            if fits
            else None
        )
        result.append(
            {
                "height_m": height,
                "source_xy": source_xy,
                "profile_height_m": entry["height_m"],
                "fit": selected,
            }
        )
    return result


def fit_candidate_track(candidate: dict, config: dict) -> dict:
    observations = source_observations(candidate, config)
    valid = [item for item in observations if item["fit"] is not None]
    if len(valid) >= 2:
        heights = np.asarray([item["height_m"] for item in valid])
        centers = np.asarray([item["fit"]["center"] for item in valid])
        radii = np.asarray([item["fit"]["radius_m"] for item in valid])
        coefficients, residuals = v2.robust_centreline(heights, centers, config)
        radius_coefficients, radius_residuals = v2.robust_scalar_line(
            heights,
            radii,
            config["tracking"]["robust_iterations"],
            config["tracking"]["huber_delta_m"],
        )
        center_p90 = float(np.percentile(residuals, 90))
        radius_mad = v2.mad(radius_residuals)
    elif valid:
        height = valid[0]["height_m"]
        center = np.asarray(valid[0]["fit"]["center"])
        radius = float(valid[0]["fit"]["radius_m"])
        coefficients = np.asarray([[0.0, center[0]], [0.0, center[1]]])
        radius_coefficients = np.asarray([0.0, radius])
        center_p90 = 0.0
        radius_mad = 0.0
    else:
        position = candidate["position"]
        coefficients = np.asarray([[0.0, position["x"]], [0.0, position["y"]]])
        radius_coefficients = np.asarray([0.0, np.nan])
        center_p90 = None
        radius_mad = None
    return {
        "observations": observations,
        "valid_observation_count": len(valid),
        "centreline_coefficients": coefficients,
        "radius_coefficients": radius_coefficients,
        "centre_residual_p90_m": center_p90,
        "radius_residual_mad_m": radius_mad,
    }


def predict(coefficients: np.ndarray, height: float) -> np.ndarray:
    return np.asarray(
        [
            coefficients[0, 0] * height + coefficients[0, 1],
            coefficients[1, 0] * height + coefficients[1, 1],
        ]
    )


def point_hashes(path: str | None) -> dict | None:
    if not path:
        return None
    data = np.load(path)
    accepted = np.asarray(data["accepted_points_xyz"], dtype="<f4")
    rejected = np.asarray(data["rejected_points_xyz"], dtype="<f4")
    all_points = np.concatenate((accepted, rejected)) if len(rejected) else accepted

    def hashes(points: np.ndarray) -> np.ndarray:
        if not len(points):
            return np.empty(0, dtype=np.uint64)
        quantized = np.rint(points.astype(np.float64) * 1000.0).astype(np.int64)
        values = (
            (quantized[:, 0].astype(np.uint64) * np.uint64(11400714819323198485))
            ^ (quantized[:, 1].astype(np.uint64) * np.uint64(14029467366897019727))
            ^ (quantized[:, 2].astype(np.uint64) * np.uint64(1609587929392839161))
        )
        return np.unique(values)

    return {
        "accepted": hashes(accepted),
        "all": hashes(all_points),
        "accepted_count": int(len(accepted)),
        "all_count": int(len(all_points)),
        "minimum": np.min(all_points, axis=0) if len(all_points) else None,
        "maximum": np.max(all_points, axis=0) if len(all_points) else None,
    }


def overlap_metrics(left: dict | None, right: dict | None) -> dict:
    empty = {
        "shared_accepted_point_count": 0,
        "accepted_point_jaccard": 0.0,
        "accepted_point_containment": 0.0,
        "shared_full_source_point_count": 0,
        "full_source_point_jaccard": 0.0,
        "full_source_point_containment": 0.0,
    }
    if left is None or right is None:
        return empty
    if left["minimum"] is None or right["minimum"] is None:
        return empty
    if np.any(np.maximum(left["minimum"], right["minimum"]) > np.minimum(left["maximum"], right["maximum"])):
        return empty

    def calculate(first: np.ndarray, second: np.ndarray) -> tuple[int, float, float]:
        shared = int(len(np.intersect1d(first, second, assume_unique=True)))
        union = len(first) + len(second) - shared
        containment = shared / max(1, min(len(first), len(second)))
        return shared, shared / max(1, union), containment

    accepted = calculate(left["accepted"], right["accepted"])
    all_points = calculate(left["all"], right["all"])
    return {
        "shared_accepted_point_count": accepted[0],
        "accepted_point_jaccard": accepted[1],
        "accepted_point_containment": accepted[2],
        "shared_full_source_point_count": all_points[0],
        "full_source_point_jaccard": all_points[1],
        "full_source_point_containment": all_points[2],
    }


def pair_metrics(
    left: dict,
    right: dict,
    left_summary: dict,
    right_summary: dict,
    left_track: dict,
    right_track: dict,
    point_overlap: dict,
) -> dict:
    start = max(left_summary["interval_start_m"], right_summary["interval_start_m"])
    end = min(left_summary["interval_end_m"], right_summary["interval_end_m"])
    overlap = max(0.0, end - start)
    minimum_span = max(
        min(
            left_summary["interval_end_m"] - left_summary["interval_start_m"],
            right_summary["interval_end_m"] - right_summary["interval_start_m"],
        ),
        1e-9,
    )
    overlap_ratio = overlap / minimum_span
    if overlap > 0:
        heights = np.linspace(start, end, 5)
        line_distances = np.asarray(
            [
                np.linalg.norm(
                    predict(left_track["centreline_coefficients"], height)
                    - predict(right_track["centreline_coefficients"], height)
                )
                for height in heights
            ]
        )
        line_mean = float(np.mean(line_distances))
        line_max = float(np.max(line_distances))
        left_radii = left_track["radius_coefficients"][0] * heights + left_track["radius_coefficients"][1]
        right_radii = right_track["radius_coefficients"][0] * heights + right_track["radius_coefficients"][1]
        radius_denominator = np.maximum((np.abs(left_radii) + np.abs(right_radii)) / 2, 0.02)
        radius_relative_difference = float(np.median(np.abs(left_radii - right_radii) / radius_denominator))
    else:
        line_mean = line_max = radius_relative_difference = None
    anchor_distance = math.hypot(
        left["position"]["x"] - right["position"]["x"],
        left["position"]["y"] - right["position"]["y"],
    )
    line_limit = max(
        0.12,
        0.60
        * max(
            left_summary.get("median_source_fit_radius_m") or 0,
            right_summary.get("median_source_fit_radius_m") or 0,
        ),
    )
    centreline_nearly_coincident = bool(
        line_mean is not None and line_mean <= line_limit and line_max <= max(0.20, line_limit * 1.5)
    )
    radius_similar = bool(radius_relative_difference is not None and radius_relative_difference <= 0.35)
    substantial_point_overlap = point_overlap["accepted_point_containment"] >= 0.20
    definite = bool(
        overlap_ratio >= 0.60
        and line_mean is not None
        and line_mean <= 0.08
        and radius_relative_difference is not None
        and radius_relative_difference <= 0.20
        and point_overlap["accepted_point_containment"] >= 0.50
    )
    probable = bool(
        not definite
        and overlap_ratio >= 0.40
        and centreline_nearly_coincident
        and radius_similar
        and (
            anchor_distance <= NEAR_XY_M
            or substantial_point_overlap
        )
    )
    review = bool(
        not definite
        and not probable
        and overlap > 0
        and (
            (anchor_distance <= NEAR_XY_M and centreline_nearly_coincident)
            or substantial_point_overlap
        )
    )
    classification = (
        "DEFINITE_ALIAS"
        if definite
        else "PROBABLE_ALIAS"
        if probable
        else "REVIEW_ALIAS"
        if review
        else "NOT_FLAGGED"
    )
    return {
        "candidate_a": left["candidate_id"],
        "candidate_b": right["candidate_id"],
        "candidate_a_measurement_status": left["measurement_status"],
        "candidate_b_measurement_status": right["measurement_status"],
        "candidate_a_final_measurement": left["measurement_status"].startswith("MEASURABLE_"),
        "candidate_b_final_measurement": right["measurement_status"].startswith("MEASURABLE_"),
        "anchor_xy_distance_m": anchor_distance,
        "source_height_overlap_m": overlap,
        "source_height_overlap_ratio": overlap_ratio,
        "centreline_mean_distance_m": line_mean,
        "centreline_max_distance_m": line_max,
        "centreline_nearly_coincident": centreline_nearly_coincident,
        "radius_relative_difference": radius_relative_difference,
        "radius_similar": radius_similar,
        "source_seed_overlap_count": len(set(left["source_seed_ids"]) & set(right["source_seed_ids"])),
        **point_overlap,
        "classification": classification,
        "criteria": "|".join(
            name
            for name, passed in (
                ("HEIGHT_OVERLAP", overlap > 0),
                ("NEAR_XY", anchor_distance <= NEAR_XY_M),
                ("CENTRELINE_COINCIDENT", centreline_nearly_coincident),
                ("RADIUS_SIMILAR", radius_similar),
                ("POINT_OVERLAP", substantial_point_overlap),
            )
            if passed
        ),
    }


def main() -> None:
    config = v2.load_config(ROOT / "config" / "stem_inventory_v2.yaml")
    candidate_payload = json.loads(PHASE1_CANDIDATES.read_text(encoding="utf-8"))
    candidates = candidate_payload["candidates"]
    measurements = json.loads(PHASE1_MEASUREMENTS.read_text(encoding="utf-8"))["measurements"]
    measurement_ids = {item["candidate_id"] for item in measurements}

    baseline_paths = [
        ROOT / "site" / "public" / "data" / "tree-measurements.json",
        ROOT / "scripts" / "analyze_samutsongkhram_trees.py",
        ROOT / "docs" / "lidar-v2-phase1-implementation.md",
        ROOT / "docs" / "lidar-v2-phase1-comparison.md",
        OUTPUTS / "v2_stage_counts.json",
        PHASE1_CANDIDATES,
        PHASE1_MEASUREMENTS,
        OUTPUTS / "v1_v2_crosswalk.json",
        OUTPUTS / "v2_seed_profiles.csv",
        ROOT / "config" / "stem_inventory_v2.yaml",
        ROOT / "scripts" / "stem_inventory_v2.py",
        ROOT / "scripts" / "run_stem_inventory_v2.py",
        ROOT / "tests" / "test_stem_inventory_v2.py",
    ]
    write_json(
        OUTPUTS / "v2_phase1_5_baseline_hashes.json",
        {"algorithm_version": "stem-inventory-v2-phase1_5", "files": {str(path.relative_to(ROOT)): sha256(path) for path in baseline_paths}},
    )

    tracks = {item["candidate_id"]: fit_candidate_track(item, config) for item in candidates}
    summaries = {}
    source_counts = Counter()
    multi_source_counts = Counter()
    for candidate in candidates:
        heights = sorted(set(float(value) for value in candidate["source_heights_m"]))
        gaps = np.diff(heights) if len(heights) >= 2 else np.asarray([])
        observations = tracks[candidate["candidate_id"]]["observations"]
        radii = [item["fit"]["radius_m"] for item in observations if item["fit"] is not None]
        for relationship in candidate["seed_relationships"]:
            height = relationship.get("source_height_m")
            if height is not None:
                source_counts[(relationship["source"], float(height))] += 1
                if relationship["source"] == "MULTI_HEIGHT_DENSITY":
                    multi_source_counts[float(height)] += 1
        interval_start = (heights[0] - SOURCE_LEVEL_HALF_WIDTH_M) if heights else 0.0
        interval_end = (heights[-1] + SOURCE_LEVEL_HALF_WIDTH_M) if heights else 0.0
        summaries[candidate["candidate_id"]] = {
            "candidate_id": candidate["candidate_id"],
            "source_seed_count": len(candidate["source_seed_ids"]),
            "distinct_source_height_count": len(heights),
            "source_heights_m": heights,
            "candidate_vertical_span_m": (heights[-1] - heights[0]) if heights else 0.0,
            "maximum_source_height_gap_m": float(np.max(gaps)) if len(gaps) else 0.0,
            "interval_start_m": interval_start,
            "interval_end_m": interval_end,
            "valid_source_fit_count": tracks[candidate["candidate_id"]]["valid_observation_count"],
            "median_source_fit_radius_m": float(np.median(radii)) if radii else None,
            "source_providers": "|".join(candidate["seed_sources"]),
            "has_v1_seed": "V1_DENSITY" in candidate["seed_sources"],
            "phase1_candidate_status": candidate["candidate_status"],
            "phase1_measurement_status": candidate["measurement_status"],
            "is_final_63_measurement": candidate["candidate_id"] in measurement_ids,
            "x": candidate["position"]["x"],
            "y": candidate["position"]["y"],
        }

    positions = np.asarray([[item["position"]["x"], item["position"]["y"]] for item in candidates])
    spatial_tree = cKDTree(positions)
    nearest_distances, _ = spatial_tree.query(positions, k=2)
    for candidate, distance in zip(candidates, nearest_distances[:, 1]):
        summaries[candidate["candidate_id"]]["nearest_candidate_distance_m"] = float(distance)

    histogram_fields = list(next(iter(summaries.values())).keys())
    with (OUTPUTS / "v2_phase1_candidate_support_histogram.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=histogram_fields)
        writer.writeheader()
        for candidate in candidates:
            row = dict(summaries[candidate["candidate_id"]])
            row["source_heights_m"] = "|".join(f"{value:.2f}" for value in row["source_heights_m"])
            writer.writerow(row)

    candidate_by_id = {item["candidate_id"]: item for item in candidates}
    candidate_index = {item["candidate_id"]: index for index, item in enumerate(candidates)}
    point_cache = {
        item["candidate_id"]: point_hashes(item.get("full_resolution_point_file"))
        for item in candidates
        if item.get("full_resolution_point_file")
    }
    point_candidate_pairs = spatial_tree.query_pairs(POINT_PAIR_SEARCH_M)
    point_overlaps = {}
    shared_point_pair_count = 0
    for left_index, right_index in point_candidate_pairs:
        left = candidates[left_index]
        right = candidates[right_index]
        if left["candidate_id"] not in point_cache or right["candidate_id"] not in point_cache:
            continue
        left_height = left.get("measurement_height_m")
        right_height = right.get("measurement_height_m")
        if left_height is not None and right_height is not None and abs(left_height - right_height) > 0.25:
            continue
        overlap = overlap_metrics(point_cache[left["candidate_id"]], point_cache[right["candidate_id"]])
        if overlap["shared_full_source_point_count"]:
            shared_point_pair_count += 1
            point_overlaps[(left_index, right_index)] = overlap

    all_near_pairs = spatial_tree.query_pairs(PAIR_SEARCH_M)
    alias_rows = []
    centreline_coincident_count = 0
    overlapping_height_pair_count = 0
    for left_index, right_index in sorted(all_near_pairs):
        left = candidates[left_index]
        right = candidates[right_index]
        metrics = pair_metrics(
            left,
            right,
            summaries[left["candidate_id"]],
            summaries[right["candidate_id"]],
            tracks[left["candidate_id"]],
            tracks[right["candidate_id"]],
            point_overlaps.get((left_index, right_index), overlap_metrics(None, None)),
        )
        overlapping_height_pair_count += int(metrics["source_height_overlap_m"] > 0)
        centreline_coincident_count += int(metrics["centreline_nearly_coincident"])
        if metrics["classification"] != "NOT_FLAGGED":
            alias_rows.append(metrics)

    alias_fields = list(alias_rows[0].keys()) if alias_rows else ["candidate_a", "candidate_b", "classification"]
    with (OUTPUTS / "v2_phase1_potential_alias_pairs.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=alias_fields)
        writer.writeheader()
        writer.writerows(alias_rows)

    support_distribution = Counter(
        "1" if item["distinct_source_height_count"] == 1 else
        "2" if item["distinct_source_height_count"] == 2 else
        "3" if item["distinct_source_height_count"] == 3 else
        "4+"
        for item in summaries.values()
    )
    source_seed_histogram = Counter(item["source_seed_count"] for item in summaries.values())
    near_xy_pairs = len(spatial_tree.query_pairs(NEAR_XY_M))
    alias_class_counts = Counter(row["classification"] for row in alias_rows)
    final_alias_rows = [
        row for row in alias_rows
        if row["candidate_a_final_measurement"] and row["candidate_b_final_measurement"]
    ]
    review_alias_rows = [
        row for row in alias_rows
        if row["candidate_a_measurement_status"] == "NEEDS_REVIEW"
        and row["candidate_b_measurement_status"] == "NEEDS_REVIEW"
    ]
    nearest = nearest_distances[:, 1]
    audit_summary = {
        "candidate_count": len(candidates),
        "source_seed_count": sum(item["source_seed_count"] for item in summaries.values()),
        "multi_height_seed_count_by_level": {f"{key:.2f}": value for key, value in sorted(multi_source_counts.items())},
        "support_distribution": dict(support_distribution),
        "source_seed_count_per_candidate_histogram": dict(sorted(source_seed_histogram.items())),
        "near_xy_pair_count": near_xy_pairs,
        "overlapping_height_pair_count_within_1m": overlapping_height_pair_count,
        "centreline_nearly_coincident_pair_count_within_1m": centreline_coincident_count,
        "shared_full_resolution_source_point_pair_count": shared_point_pair_count,
        "potential_alias_pair_count": len(alias_rows),
        "potential_alias_class_counts": dict(alias_class_counts),
        "potential_alias_pairs_among_final_63": len(final_alias_rows),
        "potential_alias_pairs_among_needs_review": len(review_alias_rows),
        "nearest_neighbour_distance_m": {
            "minimum": float(np.min(nearest)),
            "p10": float(np.percentile(nearest, 10)),
            "p25": float(np.percentile(nearest, 25)),
            "median": float(np.median(nearest)),
            "p75": float(np.percentile(nearest, 75)),
            "p90": float(np.percentile(nearest, 90)),
            "maximum": float(np.max(nearest)),
        },
        "audit_thresholds": {
            "near_xy_m": NEAR_XY_M,
            "pair_search_m": PAIR_SEARCH_M,
            "point_pair_search_m": POINT_PAIR_SEARCH_M,
            "source_level_half_width_m": SOURCE_LEVEL_HALF_WIDTH_M,
            "definite_alias": "overlap ratio >=0.60; mean centreline <=0.08 m; radius difference <=0.20; accepted-point containment >=0.50",
            "probable_alias": "overlap ratio >=0.40; centreline-compatible; radius difference <=0.35; and near XY or accepted-point containment >=0.20",
        },
    }
    write_json(OUTPUTS / "v2_phase1_5_fragmentation_audit_summary.json", audit_summary)

    source_level_lines = "\n".join(
        f"| {height:.2f} | {count} |"
        for height, count in sorted(multi_source_counts.items())
    )
    support_lines = "\n".join(
        f"| {label} | {support_distribution.get(label, 0)} |"
        for label in ("1", "2", "3", "4+")
    )
    seed_histogram_lines = "\n".join(
        f"| {seed_count} | {candidate_count} |"
        for seed_count, candidate_count in sorted(source_seed_histogram.items())
    )
    markdown = f"""# V2 Phase 1.5 fragmentation audit

This audit was generated before changing the Phase 1 grouping or implementing the Phase 1.5 tracker. It measures candidate support and alias evidence; it does not call the 940 candidates trees.

## Phase 1 baseline

- Source seed records: {audit_summary['source_seed_count']:,}
- Phase 1 grouped candidates: {len(candidates):,}
- Final Phase 1 measurements: {len(measurements):,}
- Needs-review candidates: {sum(item['measurement_status'] == 'NEEDS_REVIEW' for item in candidates):,}

## Multi-height seed count by source level

| Height (m) | Multi-height seeds |
|---:|---:|
{source_level_lines}

There are {len(multi_source_counts)} multi-height source levels from 0.75–3.25 m. The mean is {sum(multi_source_counts.values()) / len(multi_source_counts):.2f} seeds per level.

## Candidate source-height support

| Distinct source-height levels | Candidates |
|---:|---:|
{support_lines}

## Source seeds per candidate

| Source seeds | Candidates |
|---:|---:|
{seed_histogram_lines}

The per-candidate CSV also reports source heights, vertical span, maximum gap, provider mix, valid source-level fits, Phase 1 status, and nearest-neighbour distance.

## Pairwise evidence

- Candidate pairs with anchor XY distance ≤ {NEAR_XY_M:.2f} m: {near_xy_pairs:,}
- Pairs within 1.0 m whose source-level intervals overlap: {overlapping_height_pair_count:,}
- Pairs within 1.0 m whose fitted source-observation centrelines meet the audit coincidence rule: {centreline_coincident_count:,}
- Pairs sharing exact 1 mm-quantized full-resolution source points: {shared_point_pair_count:,}
- Flagged potential alias pairs: {len(alias_rows):,}
  - definite: {alias_class_counts.get('DEFINITE_ALIAS', 0):,}
  - probable: {alias_class_counts.get('PROBABLE_ALIAS', 0):,}
  - review: {alias_class_counts.get('REVIEW_ALIAS', 0):,}
- Flagged pairs among the final 63: {len(final_alias_rows):,}
- Flagged pairs where both candidates are `NEEDS_REVIEW`: {len(review_alias_rows):,}

## Nearest-neighbour distance

| Statistic | Distance (m) |
|---|---:|
| Minimum | {np.min(nearest):.4f} |
| P10 | {np.percentile(nearest, 10):.4f} |
| P25 | {np.percentile(nearest, 25):.4f} |
| Median | {np.median(nearest):.4f} |
| P75 | {np.percentile(nearest, 75):.4f} |
| P90 | {np.percentile(nearest, 90):.4f} |
| Maximum | {np.max(nearest):.4f} |

## Audit definitions

- A source-level interval extends 0.125 m on each side of the lowest/highest source seed height so a one-level candidate has a non-zero interval.
- “Nearly same XY” means Phase 1 anchor distance ≤ 0.30 m.
- Centreline coincidence is evaluated over five heights in the common source interval. Its mean limit is `max(0.12 m, 0.60 × larger median radius)` and its maximum limit is `max(0.20 m, 1.5 × mean limit)`.
- A definite alias requires common-height ratio ≥ 0.60, mean centreline distance ≤ 0.08 m, median relative radius difference ≤ 0.20, and accepted-point containment ≥ 0.50.
- A probable alias requires common-height ratio ≥ 0.40, compatible centrelines and radii, plus near anchor XY or accepted-point containment ≥ 0.20.
- Point overlap uses exact source points after 1 mm quantization from the Phase 1 NPZ accepted/rejected artifacts. It is evidence, not a biological identity label.

## Evidence-based conclusion

The support distribution and pair counts quantify whether Phase 1 grouping is fragmented. They do not prove that any candidate or alias pair is a biological tree. Phase 1.5 tracking must test whether compatible observations can be associated vertically without transitive XY chaining, and every consolidation must retain the original IDs and criteria.
"""
    (DOCS / "lidar-v2-phase1_5-fragmentation-audit.md").write_text(markdown, encoding="utf-8")
    print(json.dumps(audit_summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
