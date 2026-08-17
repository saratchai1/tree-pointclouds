#!/usr/bin/env python3
"""Run the separate V2 phase-1 stem inventory against sampled and full LAS data."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import resource
import sys
import time

import numpy as np
from scipy.spatial import cKDTree

import analyze_samutsongkhram_trees as v1
import stem_inventory_v2 as v2


ROOT = Path(__file__).resolve().parent.parent
V1_OUTPUT = ROOT / "site" / "public" / "data" / "tree-measurements.json"
EXPECTED_V1_SHA256 = "e9c741742ef00bf6bc0ca0e6551e37a835aacefdd0de7c50654ecf0e57755270"

_WORKER_POINTS: np.ndarray | None = None
_WORKER_TREE: cKDTree | None = None
_WORKER_CONFIG: dict | None = None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evaluate_candidate_worker(candidate: dict) -> v2.CandidateEvaluation:
    if _WORKER_POINTS is None or _WORKER_TREE is None or _WORKER_CONFIG is None:
        raise RuntimeError("V2 worker was not initialized")
    ground, profile = v2.build_candidate_profile(
        candidate,
        _WORKER_POINTS,
        _WORKER_TREE,
        _WORKER_CONFIG,
    )
    return v2.evaluate_candidate_profile(candidate, ground, profile, _WORKER_CONFIG)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(v2.json_ready(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_seed_profiles(path: Path, evaluations: list[v2.CandidateEvaluation]) -> None:
    fields = [
        "candidate_id",
        "source_seed_ids",
        "height_m",
        "point_count",
        "connected_component_count",
        "fit_validity",
        "slice_rejection_reasons",
    ]
    for rank in range(1, 4):
        fields.extend(
            [
                f"fit_{rank}_center_x",
                f"fit_{rank}_center_y",
                f"fit_{rank}_radius_m",
                f"fit_{rank}_circle_residual_m",
                f"fit_{rank}_ellipse_major_axis_m",
                f"fit_{rank}_ellipse_minor_axis_m",
                f"fit_{rank}_ellipse_residual_m",
                f"fit_{rank}_angular_coverage_deg",
                f"fit_{rank}_largest_missing_sector_deg",
                f"fit_{rank}_local_point_density_per_m2",
                f"fit_{rank}_inlier_count",
                f"fit_{rank}_rejection_reasons",
            ]
        )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for evaluation in evaluations:
            for entry in evaluation.diagnostics["profile"]:
                row = {
                    "candidate_id": evaluation.candidate_id,
                    "source_seed_ids": "|".join(evaluation.source_seed_ids),
                    "height_m": entry["height_m"],
                    "point_count": entry["point_count"],
                    "connected_component_count": entry["connected_component_count"],
                    "fit_validity": entry["fit_validity"],
                    "slice_rejection_reasons": "|".join(entry.get("rejection_reasons", [])),
                }
                for rank, fit in enumerate(entry.get("fits", [])[:3], start=1):
                    ellipse = fit.get("ellipse", {})
                    center = fit.get("center", [None, None])
                    row.update(
                        {
                            f"fit_{rank}_center_x": center[0],
                            f"fit_{rank}_center_y": center[1],
                            f"fit_{rank}_radius_m": fit.get("radius_m"),
                            f"fit_{rank}_circle_residual_m": fit.get("circle_residual_m"),
                            f"fit_{rank}_ellipse_major_axis_m": ellipse.get("semi_major_axis_m"),
                            f"fit_{rank}_ellipse_minor_axis_m": ellipse.get("semi_minor_axis_m"),
                            f"fit_{rank}_ellipse_residual_m": ellipse.get("ellipse_residual_m"),
                            f"fit_{rank}_angular_coverage_deg": fit.get("angular_coverage_deg"),
                            f"fit_{rank}_largest_missing_sector_deg": fit.get(
                                "largest_missing_angular_sector_deg"
                            ),
                            f"fit_{rank}_local_point_density_per_m2": fit.get(
                                "local_point_density_per_m2"
                            ),
                            f"fit_{rank}_inlier_count": fit.get("inlier_count"),
                            f"fit_{rank}_rejection_reasons": "|".join(fit.get("rejection_reasons", [])),
                        }
                    )
                writer.writerow(row)


MEASUREMENT_FIELDS = [
    "candidate_id",
    "source_seed_ids",
    "seed_sources",
    "candidate_status",
    "measurement_status",
    "measurement_rule",
    "measurement_height_m",
    "irregular_zone_top_height_m",
    "x",
    "y",
    "ground_z_m",
    "equivalent_diameter_cm",
    "diameter_uncertainty_cm",
    "circular_equivalent_girth_cm",
    "ellipse_major_axis_cm",
    "ellipse_minor_axis_cm",
    "ellipse_perimeter_cm",
    "observed_contour_girth_cm",
    "selected_model",
    "tree_presence_confidence",
    "stem_tracking_confidence",
    "measurement_confidence",
    "centreline_residual_p90_m",
    "radius_residual_mad_m",
    "raw_centre_spread_m",
    "raw_radius_cv",
    "angular_coverage_deg",
    "supporting_slice_count",
    "reason_codes",
    "full_resolution_point_file",
]


def measurement_row(evaluation: v2.CandidateEvaluation) -> dict:
    payload = evaluation.to_dict()
    row = {field: payload.get(field) for field in MEASUREMENT_FIELDS}
    row["source_seed_ids"] = "|".join(evaluation.source_seed_ids)
    row["seed_sources"] = "|".join(evaluation.seed_sources)
    row["reason_codes"] = "|".join(evaluation.reason_codes)
    row["x"] = evaluation.position["x"]
    row["y"] = evaluation.position["y"]
    return row


def write_measurements_csv(path: Path, measurements: list[v2.CandidateEvaluation]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MEASUREMENT_FIELDS)
        writer.writeheader()
        for evaluation in measurements:
            writer.writerow(measurement_row(evaluation))


def validate_full_resolution_measurement(
    evaluation: v2.CandidateEvaluation,
    config: dict,
) -> None:
    """Apply the configured automatic-quality criteria to the final LAS fits."""
    if not evaluation.measurement_status.startswith("MEASURABLE_"):
        return
    full = evaluation.diagnostics.get("full_resolution")
    if full is None:
        return
    entries = [
        entry
        for entry in full["perpendicular_slice_results"]
        if entry.get("selected_fit") is not None
    ]
    automatic = config["automatic_measurement_window"]
    if len(entries) >= 2:
        heights = np.asarray([entry["height_m"] for entry in entries])
        radii = np.asarray([entry["selected_fit"]["radius_m"] for entry in entries])
        _, radius_residuals = v2.robust_scalar_line(
            heights,
            radii,
            config["tracking"]["robust_iterations"],
            config["tracking"]["huber_delta_m"],
        )
        radius_residual_mad = v2.mad(radius_residuals)
        median_radius = float(np.median(radii))
    else:
        radius_residual_mad = float("inf")
        median_radius = 0.0
    median_coverage = float(
        np.median([entry["selected_fit"]["angular_coverage_deg"] for entry in entries])
    ) if entries else 0.0
    median_fit_residual = float(
        np.median([entry["selected_fit"]["circle_residual_m"] for entry in entries])
    ) if entries else float("inf")
    center_limit = max(
        automatic["centre_residual_base_m"],
        automatic["centre_residual_radius_fraction"] * median_radius,
    )
    radius_limit = max(
        automatic["radius_residual_base_m"],
        automatic["radius_residual_radius_fraction"] * median_radius,
    )
    fit_residual_limit = max(
        automatic["fit_residual_base_m"],
        automatic["fit_residual_radius_fraction"] * median_radius,
    )
    checks = {
        "minimum_valid_slices": len(entries) >= automatic["minimum_valid_slices"],
        "centreline_consistency": (
            evaluation.centreline_residual_p90_m is not None
            and evaluation.centreline_residual_p90_m <= center_limit
        ),
        "local_radius_stability": radius_residual_mad <= radius_limit,
        "median_angular_coverage": median_coverage >= automatic["minimum_median_angular_coverage_deg"],
        "selected_angular_coverage": (
            evaluation.angular_coverage_deg is not None
            and evaluation.angular_coverage_deg >= automatic["minimum_median_angular_coverage_deg"]
        ),
        "median_fit_residual": median_fit_residual <= fit_residual_limit,
    }
    validation = {
        "valid": all(checks.values()),
        "checks": checks,
        "valid_slice_count": len(entries),
        "centreline_residual_p90_m": evaluation.centreline_residual_p90_m,
        "centreline_residual_limit_m": center_limit,
        "radius_residual_mad_m": radius_residual_mad,
        "radius_residual_limit_m": radius_limit,
        "median_radius_m": median_radius,
        "median_angular_coverage_deg": median_coverage,
        "selected_angular_coverage_deg": evaluation.angular_coverage_deg,
        "minimum_angular_coverage_deg": automatic["minimum_median_angular_coverage_deg"],
        "median_fit_residual_m": median_fit_residual,
        "fit_residual_limit_m": fit_residual_limit,
    }
    evaluation.diagnostics["full_resolution_measurement_validation"] = v2.json_ready(validation)
    evaluation.radius_residual_mad_m = v2.rounded(radius_residual_mad)
    evaluation.supporting_slice_count = len(entries)
    if validation["valid"]:
        evaluation.reason_codes.append("FULL_RESOLUTION_MEASUREMENT_QUALITY_VALIDATED")
        evaluation.reason_codes = list(dict.fromkeys(evaluation.reason_codes))
        return

    evaluation.measurement_status = "NEEDS_REVIEW"
    evaluation.measurement_confidence = 0.0
    evaluation.reason_codes = [
        code for code in evaluation.reason_codes if code != "FULL_RESOLUTION_MEASUREMENT_ACCEPTED"
    ]
    evaluation.reason_codes.append("FULL_RESOLUTION_MEASUREMENT_QUALITY_NOT_MET")
    reason_by_check = {
        "minimum_valid_slices": "FULL_NEIGHBOURING_SLICE_SUPPORT_INSUFFICIENT",
        "centreline_consistency": "FULL_CENTRELINE_RESIDUAL_EXCEEDED",
        "local_radius_stability": "FULL_RADIUS_STABILITY_NOT_MET",
        "median_angular_coverage": "FULL_MEDIAN_ANGULAR_COVERAGE_INSUFFICIENT",
        "selected_angular_coverage": "FULL_SELECTED_ANGULAR_COVERAGE_INSUFFICIENT",
        "median_fit_residual": "FULL_FIT_RESIDUAL_EXCEEDED",
    }
    evaluation.reason_codes.extend(
        reason_by_check[name] for name, passed in checks.items() if not passed
    )
    for field_name in (
        "equivalent_diameter_cm",
        "diameter_uncertainty_cm",
        "circular_equivalent_girth_cm",
        "ellipse_major_axis_cm",
        "ellipse_minor_axis_cm",
        "ellipse_perimeter_cm",
        "observed_contour_girth_cm",
    ):
        setattr(evaluation, field_name, None)
    evaluation.selected_model = None
    evaluation.reason_codes = list(dict.fromkeys(evaluation.reason_codes))


def stage_counts(
    evaluations: list[v2.CandidateEvaluation],
    seed_counts: dict,
    grouped_count: int,
    sensitivity: dict,
    runtime_seconds: float,
    peak_memory_mb: float,
    source: Path,
    v1_hash: str,
    profile_cache_loaded_count: int,
) -> dict:
    candidate_statuses = Counter(item.candidate_status for item in evaluations)
    measurement_statuses = Counter(item.measurement_status for item in evaluations)
    detection_quality = sum(
        any(window.get("detection_quality") for window in item.diagnostics.get("stable_windows", []))
        for item in evaluations
    )
    automatic_quality = sum(
        any(window.get("automatic_measurement_quality") for window in item.diagnostics.get("stable_windows", []))
        for item in evaluations
    )
    retained = [item for item in evaluations if not item.candidate_status.startswith("REJECTED")]
    height_distribution = Counter(
        f"{item.measurement_height_m:.2f}"
        for item in evaluations
        if item.candidate_status == "CONFIRMED_STEM"
        and item.measurement_status.startswith("MEASURABLE_")
        and item.measurement_height_m is not None
    )
    recoveries = {
        code: sum(code in item.reason_codes for item in retained)
        for code in (
            "RECOVERED_OLD_RADIUS_CAP",
            "RECOVERED_CENTRELINE_RESIDUAL",
            "RECOVERED_LOCAL_RADIUS_STABILITY",
        )
    }
    final_measurements = [
        item
        for item in evaluations
        if item.candidate_status == "CONFIRMED_STEM"
        and item.measurement_status.startswith("MEASURABLE_")
    ]
    final_recoveries = {
        code: sum(code in item.reason_codes for item in final_measurements)
        for code in recoveries
    }
    return {
        "algorithm_version": "stem-inventory-v2-phase1",
        "source_las": str(source),
        "source_las_point_count": 67_177_038,
        "v1_reproduction": {
            "tree_count": 29,
            "expected_sha256": EXPECTED_V1_SHA256,
            "observed_sha256": v1_hash,
            "byte_for_byte_unchanged": v1_hash == EXPECTED_V1_SHA256,
        },
        "stage_counts": {
            **seed_counts,
            "grouped_candidate_count": grouped_count,
            "detection_quality_stable_window_count": detection_quality,
            "automatic_quality_stable_window_count": automatic_quality,
            "measurable_standard_1_30_count": measurement_statuses["MEASURABLE_STANDARD_1_30"],
            "measurable_adaptive_height_count": measurement_statuses["MEASURABLE_ADAPTIVE_HEIGHT"],
            "needs_review_count": measurement_statuses["NEEDS_REVIEW"],
            "insufficient_coverage_count": measurement_statuses["INSUFFICIENT_COVERAGE"],
            "rejected_count": sum(
                count for status, count in candidate_statuses.items() if status.startswith("REJECTED")
            ),
            "full_resolution_attempted_count": sum(
                item.full_resolution_point_file is not None for item in evaluations
            ),
            "full_resolution_validated_measurement_count": sum(
                "FULL_RESOLUTION_MEASUREMENT_QUALITY_VALIDATED" in item.reason_codes
                for item in evaluations
            ),
            "full_resolution_downgraded_to_review_count": sum(
                "FULL_RESOLUTION_MEASUREMENT_QUALITY_NOT_MET" in item.reason_codes
                for item in evaluations
            ),
        },
        "candidate_status_counts": dict(sorted(candidate_statuses.items())),
        "measurement_status_counts": dict(sorted(measurement_statuses.items())),
        "recovery_counts": recoveries,
        "final_measurement_recovery_counts": final_recoveries,
        "selected_height_distribution": dict(sorted(height_distribution.items(), key=lambda item: float(item[0]))),
        "sensitivity": sensitivity,
        "runtime_seconds": round(runtime_seconds, 3),
        "runtime_mode": (
            "sampled-profile-cache-assisted"
            if profile_cache_loaded_count
            else "cold-end-to-end"
        ),
        "sampled_profile_cache_loaded_count": profile_cache_loaded_count,
        "peak_memory_mb": round(peak_memory_mb, 2),
        "accuracy_statement": "No precision, recall, or accuracy is claimed without independent ground truth.",
    }


def peak_memory_mb() -> float:
    usage = max(
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
    )
    return usage / (1024 * 1024) if sys.platform == "darwin" else usage / 1024


def parse_args() -> argparse.Namespace:
    materialized_drive_source = Path(
        "/Users/kong/Library/CloudStorage/GoogleDrive-saratchai@gmail.com/My Drive/"
        "TD_008_2026_08_07_07_04_07.las"
    )
    default_source = (
        materialized_drive_source
        if materialized_drive_source.exists()
        else ROOT / "samutsongkram" / "TD_008_2026_08_07_07_04_07.las"
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "stem_inventory_v2.yaml")
    parser.add_argument("--source", type=Path, default=default_source)
    parser.add_argument("--viewer-data", type=Path, default=ROOT / "site" / "public" / "data")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs")
    parser.add_argument("--manual-seeds", type=Path)
    parser.add_argument("--workers", type=int, default=min(4, mp.cpu_count()))
    return parser.parse_args()


def main() -> None:
    global _WORKER_POINTS, _WORKER_TREE, _WORKER_CONFIG
    args = parse_args()
    started = time.perf_counter()
    before_hash = sha256(V1_OUTPUT)
    if before_hash != EXPECTED_V1_SHA256:
        raise RuntimeError(f"V1 output hash changed before V2 run: {before_hash}")
    if not args.source.exists() or args.source.stat().st_size < 1_000_000_000:
        raise FileNotFoundError(f"Full LAS is unavailable or not materialized: {args.source}")

    config = v2.load_config(args.config)
    points = v1.load_positions()
    xmin, xmax, ymin, ymax = config["analysis"]["bounds"]
    inside = (
        (points[:, 0] >= xmin)
        & (points[:, 0] <= xmax)
        & (points[:, 1] >= ymin)
        & (points[:, 1] <= ymax)
    )
    points = points[inside]
    print(f"V2 sampled points in analysis bounds: {len(points):,}", flush=True)

    v1_seeds = v2.V1DensitySeedProvider().generate(points, config)
    multi_height_seeds = v2.MultiHeightDensitySeedProvider().generate(points, config)
    manual_seeds = v2.load_manual_seeds(args.manual_seeds)
    all_seeds = v1_seeds + multi_height_seeds + manual_seeds
    candidates = v2.group_seeds_non_destructive(all_seeds, config)
    seed_counts = {
        "v1_density_seed_count": len(v1_seeds),
        "new_multi_height_seed_count": len(multi_height_seeds),
        "manual_seed_count": len(manual_seeds),
        "seed_record_count_before_grouping": len(all_seeds),
    }
    print(
        f"V2 seeds: V1={len(v1_seeds):,} multi-height={len(multi_height_seeds):,} "
        f"manual={len(manual_seeds):,}; grouped={len(candidates):,}",
        flush=True,
    )

    _WORKER_POINTS = points
    _WORKER_TREE = cKDTree(points[:, :2])
    _WORKER_CONFIG = config
    cache_digest = hashlib.sha256(
        args.config.read_bytes() + (ROOT / "scripts" / "stem_inventory_v2.py").read_bytes()
    ).hexdigest()[:16]
    cache_dir = args.output_dir / "debug" / "profile_cache" / cache_digest
    cache_dir.mkdir(parents=True, exist_ok=True)
    evaluations_by_id: dict[str, v2.CandidateEvaluation] = {}
    pending_candidates = []
    for candidate in candidates:
        cache_path = cache_dir / f"{candidate['candidate_id']}.json"
        expected_seed_ids = [seed.seed_id for seed in candidate["source_seeds"]]
        if cache_path.exists():
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if payload.get("source_seed_ids") == expected_seed_ids:
                evaluations_by_id[candidate["candidate_id"]] = v2.CandidateEvaluation(**payload)
                continue
        pending_candidates.append(candidate)
    if evaluations_by_id:
        print(
            f"V2 profile cache: loaded={len(evaluations_by_id):,}; "
            f"pending={len(pending_candidates):,}",
            flush=True,
        )
    profile_cache_loaded_count = len(evaluations_by_id)
    workers = max(1, args.workers)
    if not pending_candidates:
        iterator = iter(())
        pool = None
    elif workers == 1:
        iterator = map(evaluate_candidate_worker, pending_candidates)
        pool = None
    else:
        context = mp.get_context("fork")
        pool = context.Pool(processes=workers)
        iterator = pool.imap(evaluate_candidate_worker, pending_candidates, chunksize=1)
    try:
        for index, evaluation in enumerate(iterator, start=1):
            evaluations_by_id[evaluation.candidate_id] = evaluation
            write_json(cache_dir / f"{evaluation.candidate_id}.json", evaluation.to_dict())
            completed = len(evaluations_by_id)
            if index % 25 == 0 or index == len(pending_candidates):
                print(f"V2 sampled profiles: {completed:,}/{len(candidates):,}", flush=True)
    except BaseException:
        if pool is not None:
            pool.terminate()
            pool.join()
        raise
    else:
        if pool is not None:
            pool.close()
            pool.join()

    evaluations = [evaluations_by_id[candidate["candidate_id"]] for candidate in candidates]

    v2.apply_duplicate_statuses(evaluations, config)
    sensitivity = v2.sensitivity_counts(evaluations, config)
    neighborhoods = v2.extract_full_resolution_neighborhoods(
        args.source,
        evaluations,
        config,
        args.viewer_data,
    )
    point_dir = args.output_dir / "debug" / "full_resolution_points" / cache_digest
    measurable_before_full = [
        item
        for item in evaluations
        if item.candidate_status == "CONFIRMED_STEM"
        and item.measurement_status.startswith("MEASURABLE_")
    ]
    for index, evaluation in enumerate(measurable_before_full, start=1):
        v2.refine_candidate_full_resolution(
            evaluation,
            neighborhoods.get(evaluation.candidate_id, np.empty((0, 3))),
            config,
            point_dir,
        )
        validate_full_resolution_measurement(evaluation, config)
        if index % 10 == 0 or index == len(measurable_before_full):
            print(f"V2 full-resolution fits: {index:,}/{len(measurable_before_full):,}", flush=True)

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "algorithm_version": config["algorithm_version"],
        "configuration_file": str(args.config),
        "source_las": str(args.source),
        "source_las_point_count": 67_177_038,
        "sampled_point_count_in_analysis_bounds": len(points),
        "thresholds": config,
        "ground_height_reference": "candidate-local percentile; multi-height seeds use a local ground grid",
    }
    write_json(
        output_dir / "tree_candidates_v2_phase1.json",
        {**metadata, "candidate_count": len(evaluations), "candidates": [item.to_dict() for item in evaluations]},
    )
    measurements = [
        item
        for item in evaluations
        if item.candidate_status == "CONFIRMED_STEM"
        and item.measurement_status.startswith("MEASURABLE_")
        and item.equivalent_diameter_cm is not None
    ]
    write_json(
        output_dir / "tree_measurements_v2_phase1.json",
        {**metadata, "measurement_count": len(measurements), "measurements": [item.to_dict() for item in measurements]},
    )
    write_measurements_csv(output_dir / "tree_measurements_v2_phase1.csv", measurements)
    write_seed_profiles(output_dir / "v2_seed_profiles.csv", evaluations)

    v1_payload = json.loads(V1_OUTPUT.read_text(encoding="utf-8"))
    write_json(output_dir / "v1_v2_crosswalk.json", v2.build_v1_v2_crosswalk(v1_payload, evaluations))
    audit_targets = {
        21: (36.96, -10.08),
        162: (36.56, -11.28),
        169: (6.96, -9.68),
    }
    for seed_number, audit_location in audit_targets.items():
        seed_id = f"V1-{seed_number:04d}"
        matched = [item for item in evaluations if seed_id in item.source_seed_ids]
        write_json(
            output_dir / "debug" / f"seed_{seed_number}.json",
            {
                "algorithm_version": config["algorithm_version"],
                "audit_seed_id": seed_id,
                "audit_location": audit_location,
                "matched_candidate_count": len(matched),
                "candidates": [item.to_dict() for item in matched],
            },
        )

    after_hash = sha256(V1_OUTPUT)
    if after_hash != before_hash:
        raise RuntimeError("V1 production JSON changed during V2 run")
    counts = stage_counts(
        evaluations,
        seed_counts,
        len(candidates),
        sensitivity,
        time.perf_counter() - started,
        peak_memory_mb(),
        args.source,
        after_hash,
        profile_cache_loaded_count,
    )
    write_json(output_dir / "v2_stage_counts.json", counts)
    print(json.dumps(counts["stage_counts"], indent=2), flush=True)
    print(f"V2 runtime: {counts['runtime_seconds']:.1f}s; peak memory: {counts['peak_memory_mb']:.1f} MB", flush=True)


if __name__ == "__main__":
    main()
