#!/usr/bin/env python3
"""Build Phase 1.5 tracks and evidence audits without changing Phase 1."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from typing import Any

import numpy as np

import audit_v2_phase1_5_fragmentation as fragmentation
import stem_inventory_v2 as phase1
import stem_inventory_v2_phase1_5 as phase15


ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
DOCS = ROOT / "docs"
PHASE1_CONFIG = ROOT / "config" / "stem_inventory_v2.yaml"
PHASE15_CONFIG = ROOT / "config" / "stem_inventory_v2_phase1_5.yaml"
PHASE1_CANDIDATES = OUTPUTS / "tree_candidates_v2_phase1.json"
PHASE1_MEASUREMENTS = OUTPUTS / "tree_measurements_v2_phase1.json"
PHASE1_ALIASES = OUTPUTS / "v2_phase1_potential_alias_pairs.csv"
BASELINE_HASHES = OUTPUTS / "v2_phase1_5_baseline_hashes.json"
V1_OUTPUT = ROOT / "site" / "public" / "data" / "tree-measurements.json"
EXPECTED_V1_SHA256 = "e9c741742ef00bf6bc0ca0e6551e37a835aacefdd0de7c50654ecf0e57755270"

PRIMARY_FULL_FAILURE_ORDER = [
    ("minimum_valid_slices", "FULL_NEIGHBOURING_SLICE_SUPPORT_INSUFFICIENT"),
    ("centreline_consistency", "FULL_CENTRELINE_RESIDUAL_EXCEEDED"),
    ("local_radius_stability", "FULL_RADIUS_STABILITY_NOT_MET"),
    ("median_angular_coverage", "FULL_MEDIAN_ANGULAR_COVERAGE_INSUFFICIENT"),
    ("selected_angular_coverage", "FULL_SELECTED_ANGULAR_COVERAGE_INSUFFICIENT"),
    ("median_fit_residual", "FULL_FIT_RESIDUAL_EXCEEDED"),
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(phase1.json_ready(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_phase1_alias_rows() -> list[dict]:
    with PHASE1_ALIASES.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def verify_locked_baseline() -> dict:
    payload = read_json(BASELINE_HASHES)
    mismatches = []
    for relative_path, expected_hash in payload["files"].items():
        path = ROOT / relative_path
        actual = sha256(path)
        if actual != expected_hash:
            mismatches.append({"path": str(path), "expected": expected_hash, "actual": actual})
    v1_hash = sha256(V1_OUTPUT)
    if v1_hash != EXPECTED_V1_SHA256:
        mismatches.append({"path": str(V1_OUTPUT), "expected": EXPECTED_V1_SHA256, "actual": v1_hash})
    if mismatches:
        raise RuntimeError(f"Locked baseline changed: {mismatches}")
    return {"verified_file_count": len(payload["files"]), "v1_sha256": v1_hash}


def radius_band(radius_m: float | None) -> str:
    if radius_m is None:
        return "UNKNOWN"
    if radius_m < 0.10:
        return "<0.10"
    if radius_m < 0.20:
        return "0.10-<0.20"
    if radius_m < 0.30:
        return "0.20-<0.30"
    return ">=0.30"


def coverage_band(coverage: float | None) -> str:
    if coverage is None:
        return "UNKNOWN"
    if coverage < 90:
        return "<90"
    if coverage < 140:
        return "90-<140"
    if coverage < 240:
        return "140-<240"
    return ">=240"


def selected_slice(candidate: dict, height: float | None, full: bool = False) -> dict | None:
    if height is None:
        return None
    if full:
        entries = candidate["diagnostics"].get("full_resolution", {}).get(
            "perpendicular_slice_results", []
        )
        matches = [item for item in entries if item.get("selected_fit") is not None]
    else:
        entries = candidate["diagnostics"].get("profile", [])
        matches = [item for item in entries if item.get("fits")]
    if not matches:
        return None
    return min(matches, key=lambda item: abs(float(item["height_m"]) - float(height)))


def sampled_metrics(candidate: dict) -> dict:
    window = candidate["diagnostics"].get("selected_window")
    if not window:
        return {}
    height = candidate.get("measurement_height_m")
    if height is None:
        selected = window.get("selected_slices", [])
        height = float(np.median([item["height_m"] for item in selected])) if selected else None
    entry = selected_slice(candidate, height, full=False)
    fit = min(
        entry.get("fits", []),
        key=lambda item: abs((item.get("radius_m") or 0) - window["median_radius_m"]),
    ) if entry and entry.get("fits") else None
    coefficients = np.asarray(window["centreline_coefficients"], dtype=float)
    center = (
        [
            float(coefficients[0, 0] * height + coefficients[0, 1]),
            float(coefficients[1, 0] * height + coefficients[1, 1]),
        ]
        if height is not None
        else None
    )
    return {
        "selected_height_m": height,
        "center_x": center[0] if center else None,
        "center_y": center[1] if center else None,
        "radius_m": fit.get("radius_m") if fit else window.get("median_radius_m"),
        "centreline_residual_p90_m": window.get("centre_residual_p90_m"),
        "radius_residual_mad_m": window.get("radius_residual_mad_m"),
        "angular_coverage_deg": fit.get("angular_coverage_deg") if fit else window.get("median_angular_coverage_deg"),
        "median_angular_coverage_deg": window.get("median_angular_coverage_deg"),
        "fit_residual_m": fit.get("circle_residual_m") if fit else window.get("median_fit_residual_m"),
        "point_count": entry.get("point_count") if entry else None,
        "connected_component_count": entry.get("connected_component_count") if entry else None,
        "fit_model": "CIRCLE_SAMPLED_WINDOW",
    }


def full_metrics(candidate: dict) -> dict:
    full = candidate["diagnostics"].get("full_resolution")
    validation = candidate["diagnostics"].get("full_resolution_measurement_validation")
    if not full or not validation:
        return {}
    height = full["selected_height_m"]
    coefficients = np.asarray(full["centreline_coefficients"], dtype=float)
    center = [
        float(coefficients[0, 0] * height + coefficients[0, 1]),
        float(coefficients[1, 0] * height + coefficients[1, 1]),
    ]
    fit = full["circle_model"]
    entry = selected_slice(candidate, height, full=True)
    components = None
    if entry:
        components = entry.get("slice", {}).get("connected_component_count")
    return {
        "selected_height_m": height,
        "center_x": center[0],
        "center_y": center[1],
        "radius_m": fit.get("radius_m"),
        "centreline_residual_p90_m": validation.get("centreline_residual_p90_m"),
        "radius_residual_mad_m": validation.get("radius_residual_mad_m"),
        "angular_coverage_deg": validation.get("selected_angular_coverage_deg"),
        "median_angular_coverage_deg": validation.get("median_angular_coverage_deg"),
        "fit_residual_m": validation.get("median_fit_residual_m"),
        "point_count": full.get("accepted_point_count", 0) + full.get("rejected_point_count", 0),
        "accepted_point_count": full.get("accepted_point_count", 0),
        "rejected_point_count": full.get("rejected_point_count", 0),
        "connected_component_count": components,
        "fit_model": candidate.get("selected_model") or "CIRCLE_FULL_RESOLUTION",
        "valid_slice_count": validation.get("valid_slice_count"),
    }


def metric_delta(sampled: dict, full: dict, key: str) -> float | None:
    left, right = sampled.get(key), full.get(key)
    return float(right - left) if left is not None and right is not None else None


def primary_full_failure(candidate: dict) -> str:
    checks = candidate["diagnostics"]["full_resolution_measurement_validation"]["checks"]
    for check, reason in PRIMARY_FULL_FAILURE_ORDER:
        if not checks[check]:
            return reason
    raise AssertionError(f"No failed full-resolution check for {candidate['candidate_id']}")


def track_indexes(tracks: list[dict], alias_map: dict) -> tuple[dict, dict]:
    by_id = {track["track_id"]: track for track in tracks}
    by_candidate = {row["phase1_candidate_id"]: row for row in alias_map["candidate_aliases"]}
    return by_id, by_candidate


def build_failure_audit(
    candidates: list[dict], tracks: list[dict], alias_map: dict, phase1_config: dict
) -> tuple[list[dict], list[dict], dict]:
    by_track, by_candidate = track_indexes(tracks, alias_map)
    failure_rows = []
    comparison_rows = []
    for candidate in candidates:
        automatic_windows = [
            item
            for item in candidate["diagnostics"].get("stable_windows", [])
            if item.get("automatic_measurement_quality")
        ]
        attempted = candidate.get("full_resolution_point_file") is not None
        validation = candidate["diagnostics"].get("full_resolution_measurement_validation")
        accepted = bool(attempted and validation and validation.get("valid"))
        not_attempted = bool(automatic_windows and not attempted)
        failed = bool(attempted and not accepted)
        if not (not_attempted or failed):
            if attempted:
                sampled = sampled_metrics(candidate)
                full = full_metrics(candidate)
            else:
                continue
        else:
            sampled = sampled_metrics(candidate)
            full = full_metrics(candidate)
        alias = by_candidate[candidate["candidate_id"]]
        track = by_track.get(alias.get("canonical_track_id"))
        track_support = track.get("source_height_count", 0) if track else 0
        track_span = track.get("vertical_span_m", 0.0) if track else 0.0
        selected_height = candidate.get("measurement_height_m") or sampled.get("selected_height_m")
        providers = "|".join(candidate["seed_sources"])
        provider_class = "V1_AND_MULTI_HEIGHT" if "V1_DENSITY" in providers and "MULTI_HEIGHT_DENSITY" in providers else "V1_SEED_PRESENT" if "V1_DENSITY" in providers else "MULTI_HEIGHT_ONLY"
        sampled_radius = sampled.get("radius_m")
        row = {
            "candidate_id": candidate["candidate_id"],
            "primary_failure_stage": (
                "SAMPLED_TO_FULL_ATTEMPT_GATE" if not_attempted else "FULL_RESOLUTION_VALIDATION" if failed else "NONE_ACCEPTED"
            ),
            "primary_failure_reason": (
                "SAMPLED_AUTOMATIC_WINDOW_BELOW_ADAPTIVE_MINIMUM_HEIGHT"
                if not_attempted
                else primary_full_failure(candidate) if failed else "NONE_ACCEPTED"
            ),
            "all_failure_reason_codes": "|".join(candidate["reason_codes"]),
            "full_resolution_attempted": attempted,
            "full_resolution_accepted": accepted,
            "phase1_measurement_status": candidate["measurement_status"],
            "pom_class": (
                "STANDARD" if candidate.get("measurement_rule") == "STANDARD_1_30"
                else "ADAPTIVE" if candidate.get("measurement_rule") == "ADAPTIVE_STABLE_STEM"
                else "NO_POM_BELOW_RANGE"
            ),
            "selected_measurement_height_m": selected_height,
            "source_provider": providers,
            "source_provider_class": provider_class,
            "source_height_support_count_phase1": len(candidate["source_heights_m"]),
            "track_support_count_phase1_5": track_support,
            "candidate_track_length_m": track_span,
            "track_id": alias.get("canonical_track_id"),
            "alias_status": track.get("identity_status") if track else "UNASSIGNED",
            "fit_model": full.get("fit_model") or sampled.get("fit_model"),
            "radius_band": radius_band(sampled_radius),
            "angular_coverage_band": coverage_band(sampled.get("angular_coverage_deg")),
            "ground_confidence": "UNKNOWN_PHASE1_NOT_INSTRUMENTED",
            "ground_z_m": candidate.get("ground_z_m"),
            "sampled_center_x": sampled.get("center_x"),
            "sampled_center_y": sampled.get("center_y"),
            "sampled_radius_m": sampled_radius,
            "sampled_centreline_residual_p90_m": sampled.get("centreline_residual_p90_m"),
            "sampled_radius_residual_mad_m": sampled.get("radius_residual_mad_m"),
            "sampled_angular_coverage_deg": sampled.get("angular_coverage_deg"),
            "sampled_median_angular_coverage_deg": sampled.get("median_angular_coverage_deg"),
            "sampled_fit_residual_m": sampled.get("fit_residual_m"),
            "sampled_point_count": sampled.get("point_count"),
            "sampled_connected_component_count": sampled.get("connected_component_count"),
            "full_center_x": full.get("center_x"),
            "full_center_y": full.get("center_y"),
            "full_radius_m": full.get("radius_m"),
            "full_centreline_residual_p90_m": full.get("centreline_residual_p90_m"),
            "full_radius_residual_mad_m": full.get("radius_residual_mad_m"),
            "full_angular_coverage_deg": full.get("angular_coverage_deg"),
            "full_median_angular_coverage_deg": full.get("median_angular_coverage_deg"),
            "full_fit_residual_m": full.get("fit_residual_m"),
            "full_point_count": full.get("point_count"),
            "full_accepted_point_count": full.get("accepted_point_count"),
            "full_rejected_point_count": full.get("rejected_point_count"),
            "full_connected_component_count": full.get("connected_component_count"),
            "full_valid_slice_count": full.get("valid_slice_count"),
            "center_shift_m": (
                math.hypot(full["center_x"] - sampled["center_x"], full["center_y"] - sampled["center_y"])
                if sampled.get("center_x") is not None and full.get("center_x") is not None
                else None
            ),
            "radius_delta_m": metric_delta(sampled, full, "radius_m"),
            "centreline_residual_delta_m": metric_delta(sampled, full, "centreline_residual_p90_m"),
            "radius_mad_delta_m": metric_delta(sampled, full, "radius_residual_mad_m"),
            "angular_coverage_delta_deg": metric_delta(sampled, full, "angular_coverage_deg"),
            "fit_residual_delta_m": metric_delta(sampled, full, "fit_residual_m"),
            "point_count_delta": metric_delta(sampled, full, "point_count"),
            "connected_component_count_delta": metric_delta(sampled, full, "connected_component_count"),
            "full_rejected_point_fraction": (
                full.get("rejected_point_count", 0) / max(full.get("point_count", 0), 1)
                if full.get("point_count") is not None else None
            ),
            "full_to_sampled_point_count_ratio": (
                full.get("point_count") / sampled.get("point_count")
                if full.get("point_count") is not None and sampled.get("point_count")
                else None
            ),
            "selected_height_delta_m": metric_delta(sampled, full, "selected_height_m"),
            "sampled_extraction_radius_m": phase1_config["analysis"]["sampled_neighborhood_radius_m"],
            "full_extraction_radius_minimum_m": phase1_config["full_resolution"]["extraction_radius_minimum_m"],
            "full_extraction_radius_maximum_m": phase1_config["full_resolution"]["extraction_radius_maximum_m"],
            "sampled_ransac_trials": phase1_config["slice_fit"]["sampled_ransac_trials"],
            "full_ransac_trials": phase1_config["slice_fit"]["full_resolution_ransac_trials"],
            "height_normalization_changed": False,
        }
        if not_attempted or failed:
            failure_rows.append(row)
        if attempted:
            comparison_rows.append(row)

    not_attempted_rows = [row for row in failure_rows if not row["full_resolution_attempted"]]
    failed_rows = [row for row in failure_rows if row["full_resolution_attempted"]]
    if len(not_attempted_rows) != 27 or len(failed_rows) != 450:
        raise AssertionError(
            f"Failure reconciliation changed: non-attempted={len(not_attempted_rows)}, failed={len(failed_rows)}"
        )
    if len(comparison_rows) != 513:
        raise AssertionError(f"Expected 513 sampled/full comparisons, got {len(comparison_rows)}")
    primary_counts = Counter(row["primary_failure_reason"] for row in failure_rows)
    all_check_failures = Counter()
    for candidate in candidates:
        validation = candidate["diagnostics"].get("full_resolution_measurement_validation")
        if candidate.get("full_resolution_point_file") and validation and not validation["valid"]:
            all_check_failures.update(name for name, passed in validation["checks"].items() if not passed)

    def numeric_summary(key: str) -> dict:
        values = np.asarray([row[key] for row in comparison_rows if row.get(key) is not None], dtype=float)
        return {
            "count": len(values),
            "median": float(np.median(values)) if len(values) else None,
            "p10": float(np.percentile(values, 10)) if len(values) else None,
            "p90": float(np.percentile(values, 90)) if len(values) else None,
            "positive_fraction": float(np.mean(values > 0)) if len(values) else None,
        }

    def band(value: float | None, cuts: list[float], labels: list[str], missing: str = "UNKNOWN") -> str:
        if value is None:
            return missing
        for cut, label in zip(cuts, labels):
            if float(value) < cut:
                return label
        return labels[-1]

    breakdown_values = {
        "pom_class": lambda row: row["pom_class"],
        "radius_band": lambda row: row["radius_band"],
        "selected_measurement_height_m": lambda row: (
            f"{float(row['selected_measurement_height_m']):.2f}" if row["selected_measurement_height_m"] is not None else "NONE"
        ),
        "angular_coverage_band": lambda row: row["angular_coverage_band"],
        "source_height_support": lambda row: str(row["source_height_support_count_phase1"]),
        "source_provider_class": lambda row: row["source_provider_class"],
        "candidate_track_length_band_m": lambda row: band(
            row["candidate_track_length_m"], [0.25, 0.75, 1.50, math.inf], ["<0.25", "0.25-<0.75", "0.75-<1.50", ">=1.50"]
        ),
        "fit_model": lambda row: row["fit_model"] or "UNKNOWN",
        "full_resolution_point_count_band": lambda row: band(
            row["full_point_count"], [5000, 10000, 25000, math.inf], ["<5000", "5000-<10000", "10000-<25000", ">=25000"], "NOT_ATTEMPTED"
        ),
        "full_centreline_residual_band_m": lambda row: band(
            row["full_centreline_residual_p90_m"], [0.05, 0.10, 0.25, math.inf], ["<0.05", "0.05-<0.10", "0.10-<0.25", ">=0.25"], "NOT_ATTEMPTED"
        ),
        "full_radius_residual_band_m": lambda row: band(
            row["full_radius_residual_mad_m"], [0.025, 0.05, 0.10, math.inf], ["<0.025", "0.025-<0.05", "0.05-<0.10", ">=0.10"], "NOT_ATTEMPTED"
        ),
        "ground_confidence": lambda row: row["ground_confidence"],
    }
    breakdowns = {}
    for name, key_fn in breakdown_values.items():
        table: dict[str, Counter] = defaultdict(Counter)
        for row in failure_rows:
            outcome = "NOT_ATTEMPTED" if not row["full_resolution_attempted"] else "ATTEMPTED_NOT_ACCEPTED"
            table[key_fn(row)].update([outcome, "TOTAL"])
        breakdowns[name] = {key: dict(sorted(counts.items())) for key, counts in sorted(table.items())}

    summary = {
        "algorithm_version": phase15.ALGORITHM_VERSION,
        "sampled_automatic_window_count": 540,
        "full_resolution_attempt_count": len(comparison_rows),
        "not_attempted_count": len(not_attempted_rows),
        "attempted_not_accepted_count": len(failed_rows),
        "accepted_count": sum(row["full_resolution_accepted"] for row in comparison_rows),
        "primary_failure_reason_counts": dict(sorted(primary_counts.items())),
        "all_failed_check_counts": dict(sorted(all_check_failures.items())),
        "metric_delta_summaries": {
            key: numeric_summary(key)
            for key in [
                "center_shift_m",
                "radius_delta_m",
                "centreline_residual_delta_m",
                "radius_mad_delta_m",
                "angular_coverage_delta_deg",
                "fit_residual_delta_m",
                "point_count_delta",
                "connected_component_count_delta",
                "full_rejected_point_fraction",
                "full_to_sampled_point_count_ratio",
                "selected_height_delta_m",
            ]
        },
        "failure_breakdowns": breakdowns,
        "instrumented_representation_differences": {
            "same_local_height_reference": True,
            "sampled_ransac_trials": phase1_config["slice_fit"]["sampled_ransac_trials"],
            "full_ransac_trials": phase1_config["slice_fit"]["full_resolution_ransac_trials"],
            "sampled_neighborhood_radius_m": phase1_config["analysis"]["sampled_neighborhood_radius_m"],
            "full_extraction_radius_range_m": [
                phase1_config["full_resolution"]["extraction_radius_minimum_m"],
                phase1_config["full_resolution"]["extraction_radius_maximum_m"],
            ],
            "causal_attribution": "NOT_IDENTIFIABLE_FROM_THIS_OBSERVATIONAL_AUDIT",
        },
    }
    return failure_rows, comparison_rows, summary


def track_distance_at_height(track: dict, height: float) -> np.ndarray:
    coefficients = np.asarray(track["centreline_coefficients"], dtype=float)
    return np.asarray(
        [coefficients[0, 0] * height + coefficients[0, 1], coefficients[1, 0] * height + coefficients[1, 1]]
    )


def build_final_measurement_pair_review(
    measurements: list[dict], tracks: list[dict], alias_map: dict
) -> list[dict]:
    by_track, by_candidate = track_indexes(tracks, alias_map)
    point_cache = {
        item["candidate_id"]: fragmentation.point_hashes(item.get("full_resolution_point_file"))
        for item in measurements
    }
    rows = []
    for left_index, left in enumerate(measurements):
        for right in measurements[left_index + 1 :]:
            left_alias = by_candidate[left["candidate_id"]]
            right_alias = by_candidate[right["candidate_id"]]
            left_track = by_track.get(left_alias.get("canonical_track_id"))
            right_track = by_track.get(right_alias.get("canonical_track_id"))
            horizontal = math.hypot(
                left["position"]["x"] - right["position"]["x"],
                left["position"]["y"] - right["position"]["y"],
            )
            common_start = common_end = line_distance = None
            if left_track and right_track:
                common_start = max(min(left_track["source_heights_m"]), min(right_track["source_heights_m"]))
                common_end = min(max(left_track["source_heights_m"]), max(right_track["source_heights_m"]))
                if common_end >= common_start:
                    heights = np.linspace(common_start, common_end, 5)
                    line_distance = float(
                        np.mean(
                            [
                                np.linalg.norm(track_distance_at_height(left_track, height) - track_distance_at_height(right_track, height))
                                for height in heights
                            ]
                        )
                    )
            left_radius = left["diagnostics"]["full_resolution"]["circle_model"]["radius_m"]
            right_radius = right["diagnostics"]["full_resolution"]["circle_model"]["radius_m"]
            radius_difference = phase15.relative_difference(left_radius, right_radius)
            left_full = full_metrics(left)
            right_full = full_metrics(right)
            measurement_center_distance = math.hypot(
                left_full["center_x"] - right_full["center_x"],
                left_full["center_y"] - right_full["center_y"],
            )
            overlap = fragmentation.overlap_metrics(
                point_cache[left["candidate_id"]], point_cache[right["candidate_id"]]
            )
            left_tracks = {item["track_id"] for item in left_alias["contributing_tracks"]}
            right_tracks = {item["track_id"] for item in right_alias["contributing_tracks"]}
            common_alias_tracks = sorted(left_tracks & right_tracks)
            source_overlap = sorted(set(left["source_seed_ids"]) & set(right["source_seed_ids"]))
            same_canonical = bool(
                left_alias.get("canonical_track_id")
                and left_alias.get("canonical_track_id") == right_alias.get("canonical_track_id")
            )
            definite = bool(
                measurement_center_distance <= 0.12
                and line_distance is not None
                and line_distance <= 0.08
                and radius_difference <= 0.20
                and overlap["accepted_point_containment"] >= 0.50
            )
            probable = bool(
                not definite
                and measurement_center_distance <= 0.20
                and line_distance is not None
                and line_distance <= 0.15
                and radius_difference <= 0.35
                and overlap["accepted_point_containment"] >= 0.20
            )
            clearly_independent_nearby = bool(
                not definite
                and not probable
                and horizontal <= 1.0
                and measurement_center_distance >= 0.25
            )
            classification = (
                "DEFINITE_ALIAS" if definite else "PROBABLE_ALIAS_REQUIRES_REVIEW" if probable else "CLEARLY_INDEPENDENT_NEARBY" if clearly_independent_nearby else "NOT_FLAGGED"
            )
            rows.append(
                {
                    "candidate_a": left["candidate_id"],
                    "candidate_b": right["candidate_id"],
                    "horizontal_distance_m": horizontal,
                    "full_measurement_center_distance_m": measurement_center_distance,
                    "common_height_start_m": common_start,
                    "common_height_end_m": common_end,
                    "centreline_distance_m": line_distance,
                    "measurement_height_difference_m": abs(left["measurement_height_m"] - right["measurement_height_m"]),
                    "radius_a_m": left_radius,
                    "radius_b_m": right_radius,
                    "radius_relative_difference": radius_difference,
                    **overlap,
                    "source_seed_overlap_count": len(source_overlap),
                    "source_seed_overlap_ids": "|".join(source_overlap),
                    "common_alias_track_count": len(common_alias_tracks),
                    "common_alias_tracks": "|".join(common_alias_tracks),
                    "same_canonical_track": same_canonical,
                    "classification": classification,
                    "deterministic_rule": "definite full_center<=0.12,line<=0.08,radius_delta<=0.20,accepted_containment>=0.50; probable full_center<=0.20,line<=0.15,radius_delta<=0.35,accepted_containment>=0.20",
                }
            )
    if len(rows) != 63 * 62 // 2:
        raise AssertionError(f"Expected 1,953 final-measurement pairs, got {len(rows)}")
    return rows


def build_candidate_consolidations(pair_rows: list[dict], candidates: list[dict]) -> dict:
    """Create non-transitive canonical/alias records only from definite full-LAS pairs."""
    candidate_by_id = {item["candidate_id"]: item for item in candidates}
    adjacency: dict[str, list[dict]] = defaultdict(list)
    for row in pair_rows:
        if row["classification"] != "DEFINITE_ALIAS":
            continue
        adjacency[row["candidate_a"]].append(row)
        adjacency[row["candidate_b"]].append(row)

    def rank(candidate_id: str) -> tuple:
        item = candidate_by_id[candidate_id]
        return (
            -(item.get("measurement_confidence") or 0.0),
            -(item.get("supporting_slice_count") or 0),
            candidate_id,
        )

    claimed = set()
    consolidations = []
    for canonical_id in sorted(adjacency, key=rank):
        if canonical_id in claimed:
            continue
        direct = []
        for evidence in sorted(
            adjacency[canonical_id],
            key=lambda row: (row["candidate_a"], row["candidate_b"]),
        ):
            alias_id = evidence["candidate_b"] if evidence["candidate_a"] == canonical_id else evidence["candidate_a"]
            if alias_id in claimed:
                continue
            direct.append(
                {
                    "alias_candidate_id": alias_id,
                    "classification": evidence["classification"],
                    "full_measurement_center_distance_m": evidence["full_measurement_center_distance_m"],
                    "centreline_distance_m": evidence["centreline_distance_m"],
                    "radius_relative_difference": evidence["radius_relative_difference"],
                    "accepted_point_containment": evidence["accepted_point_containment"],
                    "reason": "ALL_DETERMINISTIC_FULL_RESOLUTION_ALIAS_CRITERIA_MET",
                }
            )
            claimed.add(alias_id)
        claimed.add(canonical_id)
        if direct:
            consolidations.append(
                {
                    "canonical_candidate_id": canonical_id,
                    "aliases": direct,
                    "non_transitive": True,
                    "automatic_measurements_removed": False,
                }
            )
    alias_count = sum(len(item["aliases"]) for item in consolidations)
    return {
        "rule_scope": "FINAL_63_WITH_FULL_RESOLUTION_POINT_EVIDENCE_ONLY",
        "canonical_group_count": len(consolidations),
        "alias_candidate_count": alias_count,
        "final_63_unique_after_definite_alias_consolidation": 63 - alias_count,
        "consolidations": consolidations,
    }


def build_target_diagnostics(
    seed_number: int,
    candidates: list[dict],
    tracks: list[dict],
    alias_map: dict,
    phase1_alias_rows: list[dict],
    failure_rows: list[dict],
) -> dict:
    seed_id = f"V1-{seed_number:04d}"
    matches = [item for item in candidates if seed_id in item["source_seed_ids"]]
    if len(matches) != 1:
        raise AssertionError(f"Expected one Phase 1 candidate for {seed_id}, got {len(matches)}")
    candidate = matches[0]
    by_track, by_candidate = track_indexes(tracks, alias_map)
    alias = by_candidate[candidate["candidate_id"]]
    contributing_tracks = [
        by_track[item["track_id"]] for item in alias["contributing_tracks"] if item["track_id"] in by_track
    ]
    source_seed_ids = set(candidate["source_seed_ids"])
    observations = sorted(
        [
            {
                "track_id": track["track_id"],
                "canonical_track_id": track["canonical_track_id"],
                **observation,
            }
            for track in tracks
            for observation in track["observations"]
            if source_seed_ids & set(observation["source_seed_ids"])
        ],
        key=lambda item: (
            item.get("source_height_m") if item.get("source_height_m") is not None else math.inf,
            item["node_id"],
        ),
    )
    aliases = [
        row
        for row in phase1_alias_rows
        if candidate["candidate_id"] in {row["candidate_a"], row["candidate_b"]}
    ]
    failure = next(
        (row for row in failure_rows if row["candidate_id"] == candidate["candidate_id"]), None
    )
    full = candidate["diagnostics"].get("full_resolution")
    sampled_full = {
        "sampled": sampled_metrics(candidate),
        "full": full_metrics(candidate),
        "failure_audit": failure,
    }
    return {
        "algorithm_version": phase15.ALGORITHM_VERSION,
        "interpretation": "TRACEABLE GEOMETRY EVIDENCE; NO FORCED TREE IDENTITY OR MEASUREMENT",
        "audit_seed_id": seed_id,
        "phase1_candidate_id": candidate["candidate_id"],
        "phase1_position": candidate["position"],
        "all_source_observations_across_heights": observations,
        "phase1_potential_aliases": aliases,
        "candidate_alias_mapping": alias,
        "phase1_candidate_fragmented_across_tracks": alias["phase1_candidate_fragmented_across_tracks"],
        "consolidated_phase1_5_tracks": contributing_tracks,
        "canonical_phase1_5_track": by_track.get(alias.get("canonical_track_id")),
        "phase1_stable_windows": candidate["diagnostics"].get("stable_windows", []),
        "sampled_full_resolution_comparison": sampled_full,
        "overlaps_another_phase1_candidate": bool(aliases),
        "final_geometry_status": (
            by_track[alias["canonical_track_id"]]["candidate_geometry_status"]
            if alias.get("canonical_track_id") in by_track
            else "INSUFFICIENT_EVIDENCE"
        ),
        "final_identity_status": (
            by_track[alias["canonical_track_id"]]["identity_status"]
            if alias.get("canonical_track_id") in by_track
            else "UNVERIFIED"
        ),
        "final_measurement_status": candidate["measurement_status"],
        "exact_reason_codes": candidate["reason_codes"],
        "full_resolution_available": full is not None,
    }


def stratified_review_queue(
    candidates: list[dict],
    measurements: list[dict],
    tracks: list[dict],
    alias_map: dict,
    pair_rows: list[dict],
    config: dict,
) -> dict:
    by_track, by_candidate = track_indexes(tracks, alias_map)
    candidate_by_id = {item["candidate_id"]: item for item in candidates}
    accepted_ids = {item["candidate_id"] for item in measurements}
    target_ids = {"C-0021", "C-0162", "C-0169"}
    probable_ids = {
        candidate_id
        for row in pair_rows
        if row["classification"] in {"DEFINITE_ALIAS", "PROBABLE_ALIAS_REQUIRES_REVIEW"}
        for candidate_id in (row["candidate_a"], row["candidate_b"])
    }
    large_ids = {
        item["candidate_id"]
        for item in candidates
        if (sampled_metrics(item).get("radius_m") or 0.0) >= config["review_queue"]["large_radius_m"]
    }
    adaptive_ids = {
        item["candidate_id"]
        for item in candidates
        if item.get("measurement_rule") == "ADAPTIVE_STABLE_STEM"
    }
    rng = np.random.default_rng(config["review_queue"]["random_seed"])

    def deterministic_sample(pool: set[str], count: int) -> set[str]:
        ordered = np.asarray(sorted(pool), dtype=object)
        if len(ordered) <= count:
            return set(ordered.tolist())
        return set(rng.choice(ordered, size=count, replace=False).tolist())

    already = accepted_ids | target_ids | probable_ids | large_ids | adaptive_ids
    needs_pool = {
        item["candidate_id"]
        for item in candidates
        if item["measurement_status"] == "NEEDS_REVIEW" and item["candidate_id"] not in already
    }
    needs_sample = deterministic_sample(needs_pool, config["review_queue"]["needs_review_sample_count"])
    already |= needs_sample
    insufficient_pool = {
        item["candidate_id"]
        for item in candidates
        if item["measurement_status"] == "INSUFFICIENT_COVERAGE" and item["candidate_id"] not in already
    }
    insufficient_sample = deterministic_sample(
        insufficient_pool, config["review_queue"]["insufficient_coverage_sample_count"]
    )
    selected_ids = already | insufficient_sample

    def categories(candidate_id: str) -> list[str]:
        result = []
        if candidate_id in accepted_ids:
            result.append("ACCEPTED_63_VALIDATION")
        if candidate_id in probable_ids:
            result.append("POTENTIAL_DUPLICATE")
        if candidate_id in large_ids:
            result.append("LARGE_RADIUS_FRAGMENTATION")
        if candidate_id in adaptive_ids:
            result.append("ADAPTIVE_OR_UPPER_HEIGHT")
        if candidate_id in target_ids:
            result.append("REQUIRED_TARGET_CASE")
        if candidate_id in needs_sample:
            result.append("STRATIFIED_NEEDS_REVIEW")
        if candidate_id in insufficient_sample:
            result.append("STRATIFIED_INSUFFICIENT_COVERAGE")
        return result

    priority_order = {
        "ACCEPTED_63_VALIDATION": 1,
        "POTENTIAL_DUPLICATE": 2,
        "LARGE_RADIUS_FRAGMENTATION": 3,
        "ADAPTIVE_OR_UPPER_HEIGHT": 4,
        "REQUIRED_TARGET_CASE": 5,
        "STRATIFIED_NEEDS_REVIEW": 6,
        "STRATIFIED_INSUFFICIENT_COVERAGE": 7,
    }
    entries = []
    for candidate_id in selected_ids:
        candidate = candidate_by_id[candidate_id]
        alias = by_candidate[candidate_id]
        track = by_track.get(alias.get("canonical_track_id"))
        cats = categories(candidate_id)
        sampled = sampled_metrics(candidate)
        entries.append(
            {
                "queue_id": f"RQ15-{candidate_id[2:]}",
                "candidate_id": candidate_id,
                "priority": min(priority_order[item] for item in cats),
                "categories": cats,
                "candidate_geometry_status": track.get("candidate_geometry_status") if track else "INSUFFICIENT_EVIDENCE",
                "identity_status": (
                    "DUPLICATE_ALIAS"
                    if alias.get("candidate_identity_status") == "DUPLICATE_ALIAS"
                    else track.get("identity_status") if track else "UNVERIFIED"
                ),
                "canonical_phase1_candidate_id": alias.get("canonical_phase1_candidate_id", candidate_id),
                "measurement_status": candidate["measurement_status"],
                "measurement_rule": candidate.get("measurement_rule"),
                "measurement_height_m": candidate.get("measurement_height_m"),
                "source_providers": candidate["seed_sources"],
                "source_height_count_phase1": len(candidate["source_heights_m"]),
                "track_id": alias.get("canonical_track_id"),
                "track_source_height_count": track.get("source_height_count", 0) if track else 0,
                "phase1_candidate_fragmented_across_tracks": alias["phase1_candidate_fragmented_across_tracks"],
                "potential_duplicate": candidate_id in probable_ids,
                "position": candidate["position"],
                "ground_z_m": candidate.get("ground_z_m"),
                "sampled_radius_m": sampled.get("radius_m"),
                "point_crop_url": f"data/points/{candidate_id}.json",
                "review_question": (
                    "Is this one of the accepted 63 a real, unique main stem?"
                    if candidate_id in accepted_ids
                    else "Does this geometry represent a main stem, root, branch, duplicate, or insufficient evidence?"
                ),
            }
        )
    entries.sort(key=lambda item: (item["priority"], item["candidate_id"]))
    return {
        "algorithm_version": phase15.ALGORITHM_VERSION,
        "interpretation": "STRATIFIED HUMAN REVIEW QUEUE; NOT A TREE INVENTORY",
        "queue_size": len(entries),
        "category_counts": dict(sorted(Counter(cat for item in entries for cat in item["categories"]).items())),
        "unique_candidate_count": len(entries),
        "entries": entries,
    }


def evenly_sample(points: np.ndarray, maximum: int) -> np.ndarray:
    if len(points) <= maximum:
        return np.round(points, 4)
    indexes = np.linspace(0, len(points) - 1, maximum, dtype=int)
    return np.round(points[indexes], 4)


def build_review_data_bundle(
    queue: dict,
    candidates: list[dict],
    tracks: list[dict],
    alias_map: dict,
    pair_rows: list[dict],
    config: dict,
    include_crops: bool,
) -> None:
    import analyze_samutsongkhram_trees as viewer_source
    from scipy.spatial import cKDTree

    review_root = ROOT / "site" / "public" / "viewer-v2-review"
    data_root = review_root / "data"
    point_root = data_root / "points"
    point_root.mkdir(parents=True, exist_ok=True)
    candidate_by_id = {item["candidate_id"]: item for item in candidates}
    by_track, by_candidate = track_indexes(tracks, alias_map)
    probable_by_candidate: dict[str, list[dict]] = defaultdict(list)
    for row in pair_rows:
        if row["classification"] in {"DEFINITE_ALIAS", "PROBABLE_ALIAS_REQUIRES_REVIEW"}:
            probable_by_candidate[row["candidate_a"]].append(row)
            probable_by_candidate[row["candidate_b"]].append(row)
    review_candidates = []
    for entry in queue["entries"]:
        candidate = candidate_by_id[entry["candidate_id"]]
        alias = by_candidate[entry["candidate_id"]]
        track = by_track.get(alias.get("canonical_track_id"))
        compact_track = None
        if track:
            compact_track = {
                key: track.get(key)
                for key in [
                    "track_id", "canonical_track_id", "candidate_geometry_status", "identity_status",
                    "measurement_status", "reason_codes", "source_candidate_ids", "source_seed_ids",
                    "source_providers", "source_height_count", "source_heights_m", "vertical_span_m", "gaps",
                    "reference_height_m", "reference_center", "centreline_coefficients", "radius_coefficients",
                    "centre_residual_p90_m", "radius_residual_mad_m", "median_radius_m",
                    "median_angular_coverage_deg", "median_fit_residual_m", "alias_track_ids",
                ]
            }
            compact_track["observations"] = [
                {
                    key: observation.get(key)
                    for key in [
                        "node_id", "source_seed_ids", "source_providers", "source_height_m", "source_xy",
                        "center", "radius_m", "circle_residual_m", "ellipse_residual_m", "ellipse_axis_ratio",
                        "angular_coverage_deg", "point_count", "inlier_count", "component_id",
                        "connected_component_count", "fit_validity", "local_evidence_quality",
                        "phase1_candidate_ids", "alias_observation_ids",
                    ]
                }
                for observation in track["observations"]
            ]
            compact_track["association_edges"] = [
                {
                    "from_node_id": edge.get("from_node_id"),
                    "to_node_id": edge.get("to_node_id"),
                    "cost": edge.get("cost"),
                    "criteria": edge.get("criteria"),
                    "alternative_associations": edge.get("alternative_associations", []),
                }
                for edge in track["association_edges"]
            ]
        compact_windows = [
            {
                key: window.get(key)
                for key in [
                    "start_height_m", "end_height_m", "supporting_slice_count", "centre_residual_p90_m",
                    "radius_residual_mad_m", "median_radius_m", "median_angular_coverage_deg",
                    "median_fit_residual_m", "detection_quality", "automatic_measurement_quality",
                    "reason_codes", "score",
                ]
            }
            for window in candidate["diagnostics"].get("stable_windows", [])
        ]
        review_candidates.append(
            {
                **entry,
                "reason_codes": candidate["reason_codes"],
                "sampled_metrics": sampled_metrics(candidate),
                "full_metrics": full_metrics(candidate),
                "stable_windows": compact_windows,
                "track": compact_track,
                "candidate_alias_mapping": alias,
                "potential_duplicate_pairs": probable_by_candidate.get(entry["candidate_id"], []),
            }
        )
    write_json(data_root / "review_queue.json", {**queue, "entries": review_candidates})

    if not include_crops:
        return
    sampled_points = viewer_source.load_positions()
    tree = cKDTree(sampled_points[:, :2])
    maximum = config["review_queue"]["maximum_crop_points_per_class"]
    for index, entry in enumerate(queue["entries"], start=1):
        candidate = candidate_by_id[entry["candidate_id"]]
        center = [candidate["position"]["x"], candidate["position"]["y"]]
        indexes = tree.query_ball_point(center, r=1.25)
        local = sampled_points[np.asarray(indexes, dtype=int)] if indexes else np.empty((0, 3))
        if candidate.get("ground_z_m") is not None and len(local):
            ground = candidate["ground_z_m"]
            local = local[(local[:, 2] >= ground + 0.30) & (local[:, 2] <= ground + 3.70)]
        sampled_crop = evenly_sample(local, maximum)
        accepted = rejected = np.empty((0, 3))
        point_file = candidate.get("full_resolution_point_file")
        if point_file and Path(point_file).exists():
            with np.load(point_file) as point_data:
                accepted = evenly_sample(np.asarray(point_data["accepted_points_xyz"]), maximum)
                rejected = evenly_sample(np.asarray(point_data["rejected_points_xyz"]), maximum)
        write_json(
            point_root / f"{entry['candidate_id']}.json",
            {
                "candidate_id": entry["candidate_id"],
                "sampled_points_xyz": sampled_crop,
                "full_accepted_points_xyz": accepted,
                "full_rejected_points_xyz": rejected,
                "counts_before_display_sampling": {
                    "sampled": len(local),
                    "full_accepted": (
                        candidate.get("diagnostics", {}).get("full_resolution", {}).get("accepted_point_count", 0)
                    ),
                    "full_rejected": (
                        candidate.get("diagnostics", {}).get("full_resolution", {}).get("rejected_point_count", 0)
                    ),
                },
            },
        )
        if index % 50 == 0 or index == len(queue["entries"]):
            print(f"Review crops: {index:,}/{len(queue['entries']):,}", flush=True)


def evaluate_manual_review_seeds(
    manual_seed_path: Path,
    source_las: Path,
    viewer_data: Path,
    phase1_config: dict,
) -> dict:
    """Run the unchanged Phase 1 profile/full-resolution logic for review clicks."""
    import analyze_samutsongkhram_trees as viewer_source
    import run_stem_inventory_v2 as phase1_runner
    from scipy.spatial import cKDTree

    manual_seeds = phase15.load_manual_review_seeds(manual_seed_path)
    if not manual_seeds:
        return {"algorithm_version": phase15.ALGORITHM_VERSION, "manual_seed_count": 0, "evaluations": []}
    if not source_las.exists() or source_las.stat().st_size < 1_000_000_000:
        raise FileNotFoundError(f"Full LAS is unavailable for manual-seed evaluation: {source_las}")
    points = viewer_source.load_positions()
    xmin, xmax, ymin, ymax = phase1_config["analysis"]["bounds"]
    points = points[
        (points[:, 0] >= xmin)
        & (points[:, 0] <= xmax)
        & (points[:, 1] >= ymin)
        & (points[:, 1] <= ymax)
    ]
    tree = cKDTree(points[:, :2])
    candidates = phase1.group_seeds_non_destructive(manual_seeds, phase1_config)
    evaluations = []
    for candidate in candidates:
        ground, profile = phase1.build_candidate_profile(candidate, points, tree, phase1_config)
        evaluations.append(phase1.evaluate_candidate_profile(candidate, ground, profile, phase1_config))
    neighborhoods = phase1.extract_full_resolution_neighborhoods(
        source_las, evaluations, phase1_config, viewer_data
    )
    point_dir = OUTPUTS / "debug" / "manual_seed_full_resolution_points_v2_phase1_5"
    for evaluation in evaluations:
        if evaluation.measurement_status.startswith("MEASURABLE_"):
            phase1.refine_candidate_full_resolution(
                evaluation,
                neighborhoods.get(evaluation.candidate_id, np.empty((0, 3))),
                phase1_config,
                point_dir,
            )
            phase1_runner.validate_full_resolution_measurement(evaluation, phase1_config)
    payload = {
        "algorithm_version": phase15.ALGORITHM_VERSION,
        "profile_algorithm_version": phase1_config["algorithm_version"],
        "manual_seed_source": str(manual_seed_path),
        "manual_seed_count": len(manual_seeds),
        "approximate_clean_height_is_hint_only": True,
        "evaluated_profile_range_m": [
            phase1_config["height_profile"]["min_height_m"],
            phase1_config["height_profile"]["max_height_m"],
        ],
        "evaluations": [item.to_dict() for item in evaluations],
    }
    write_json(OUTPUTS / "manual_seed_evaluations_v2_phase1_5.json", payload)
    return payload


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
    parser.add_argument("--config", type=Path, default=PHASE15_CONFIG)
    parser.add_argument("--manual-seeds", type=Path)
    parser.add_argument("--source", type=Path, default=default_source)
    parser.add_argument("--viewer-data", type=Path, default=ROOT / "site" / "public" / "data")
    parser.add_argument("--skip-review-crops", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    started = time.perf_counter()
    baseline_before = verify_locked_baseline()
    config = phase15.load_config(args.config)
    phase1_config = phase1.load_config(PHASE1_CONFIG)
    phase1_payload = read_json(PHASE1_CANDIDATES)
    candidates = phase1_payload["candidates"]
    measurements = read_json(PHASE1_MEASUREMENTS)["measurements"]
    candidate_by_id = {item["candidate_id"]: item for item in candidates}
    alias_rows = load_phase1_alias_rows()

    observations = phase15.extract_seed_observations(candidates)
    collapsed, seed_trace = phase15.collapse_duplicate_observations(observations, config)
    print(f"Phase 1.5 observations: raw={len(observations):,} collapsed={len(collapsed):,}", flush=True)
    initial_tracks, unassigned = phase15.initial_hungarian_tracks(collapsed, config)
    initial_track_count = len(initial_tracks)
    print(f"Phase 1.5 initial constrained tracks: {initial_track_count:,}", flush=True)
    refined_tracks, unassigned = phase15.refine_tracks(initial_tracks, unassigned, config)
    tracks = phase15.finalize_tracks(refined_tracks, candidate_by_id, config)
    canonical_tracks, track_alias_pairs = phase15.consolidate_track_aliases(tracks, alias_rows, config)
    print(f"Phase 1.5 refined/canonical tracks: {len(tracks):,}/{len(canonical_tracks):,}", flush=True)
    alias_map = phase15.build_candidate_alias_map(candidates, tracks, seed_trace)

    track_by_id = {track["track_id"]: track for track in tracks}
    for row in alias_map["candidate_aliases"]:
        raw_id = row.get("canonical_track_id")
        if raw_id:
            row["pre_consolidation_track_id"] = raw_id
            row["canonical_track_id"] = track_by_id[raw_id]["canonical_track_id"]
            row["identity_status"] = track_by_id[raw_id]["identity_status"]
    node_to_track = {
        node["node_id"]: track["track_id"] for track in tracks for node in track["observations"]
    }
    unassigned_by_node = {item["observation"]["node_id"]: item for item in unassigned}
    trace_rows = []
    for observation in observations:
        canonical_node = seed_trace[observation["source_seed_id"]]
        track_id = node_to_track.get(canonical_node)
        if track_id:
            canonical_track_id = track_by_id[track_id]["canonical_track_id"]
            disposition = "TRACK_ALIAS" if canonical_track_id != track_id else "TRACK"
            reason = "ASSIGNED_BY_CONSTRAINED_VERTICAL_TRACKING"
        else:
            canonical_track_id = None
            disposition = "UNASSIGNED_OBSERVATION"
            reason = unassigned_by_node.get(canonical_node, {}).get("reason", "NO_VALID_TRACK_ASSIGNMENT")
        trace_rows.append(
            {
                "source_seed_id": observation["source_seed_id"],
                "source_provider": observation["source_provider"],
                "source_height_m": observation.get("source_height_m"),
                "original_observation_id": observation["node_id"],
                "canonical_observation_id": canonical_node,
                "track_id": track_id,
                "canonical_track_id": canonical_track_id,
                "disposition": disposition,
                "reason": reason,
            }
        )
    expected_seed_ids = {seed for candidate in candidates for seed in candidate["source_seed_ids"]}
    traced_seed_ids = {row["source_seed_id"] for row in trace_rows}
    if expected_seed_ids != traced_seed_ids:
        raise AssertionError(f"Seed trace mismatch missing={expected_seed_ids-traced_seed_ids} extra={traced_seed_ids-expected_seed_ids}")

    geometry_counts = Counter(track["candidate_geometry_status"] for track in tracks)
    identity_counts = Counter(track["identity_status"] for track in tracks)
    tracks_payload = {
        "algorithm_version": phase15.ALGORITHM_VERSION,
        "interpretation": "GEOMETRY TRACKS; NOT GROUND-TRUTH TREE IDENTITIES",
        "configuration_file": str(args.config),
        "phase1_candidate_count": len(candidates),
        "original_seed_record_count": len(observations),
        "collapsed_observation_count": len(collapsed),
        "same_height_provider_alias_count": len(observations) - len(collapsed),
        "initial_track_count": initial_track_count,
        "refined_track_count": len(tracks),
        "canonical_track_count_after_consolidation": len(canonical_tracks),
        "duplicate_alias_track_count": sum(track["identity_status"] == "DUPLICATE_ALIAS" for track in tracks),
        "geometry_status_counts": dict(sorted(geometry_counts.items())),
        "identity_status_counts": dict(sorted(identity_counts.items())),
        "track_alias_pair_classification_counts": dict(sorted(Counter(row["classification"] for row in track_alias_pairs).items())),
        "tracks": tracks,
        "track_alias_pair_candidates": track_alias_pairs,
    }
    alias_map.update(
        {
            "canonical_track_count": len(canonical_tracks),
            "track_alias_pairs": track_alias_pairs,
        }
    )
    unassigned_payload = {
        "algorithm_version": phase15.ALGORITHM_VERSION,
        "original_seed_count": len(expected_seed_ids),
        "trace_record_count": len(trace_rows),
        "all_original_seeds_traceable": expected_seed_ids == traced_seed_ids,
        "disposition_counts": dict(sorted(Counter(row["disposition"] for row in trace_rows).items())),
        "seed_trace": trace_rows,
        "unassigned_observations": unassigned,
    }
    write_json(OUTPUTS / "tree_tracks_v2_phase1_5.json", tracks_payload)
    write_json(OUTPUTS / "candidate_alias_map_v2_phase1_5.json", alias_map)
    write_json(OUTPUTS / "unassigned_seed_observations_v2_phase1_5.json", unassigned_payload)

    failure_rows, comparison_rows, failure_summary = build_failure_audit(
        candidates, tracks, alias_map, phase1_config
    )
    print("Phase 1.5 failure audit reconciled: 27 non-attempted + 450 failed", flush=True)
    write_csv(OUTPUTS / "full_resolution_failure_reasons_v2_phase1_5.csv", failure_rows)
    write_csv(OUTPUTS / "sampled_vs_full_metrics_v2_phase1_5.csv", comparison_rows)
    write_json(OUTPUTS / "full_resolution_failure_summary_v2_phase1_5.json", failure_summary)

    pair_rows = build_final_measurement_pair_review(measurements, tracks, alias_map)
    print(f"Phase 1.5 final-measurement pair review: {len(pair_rows):,} pairs", flush=True)
    write_csv(OUTPUTS / "final_63_duplicate_review.csv", pair_rows)
    candidate_consolidations = build_candidate_consolidations(pair_rows, candidates)
    candidate_alias_lookup = {
        alias["alias_candidate_id"]: (group["canonical_candidate_id"], alias)
        for group in candidate_consolidations["consolidations"]
        for alias in group["aliases"]
    }
    for row in alias_map["candidate_aliases"]:
        if row["phase1_candidate_id"] in candidate_alias_lookup:
            canonical_id, evidence = candidate_alias_lookup[row["phase1_candidate_id"]]
            row["candidate_identity_status"] = "DUPLICATE_ALIAS"
            row["canonical_phase1_candidate_id"] = canonical_id
            row["candidate_alias_evidence"] = evidence
        else:
            row["candidate_identity_status"] = "UNVERIFIED"
            row["canonical_phase1_candidate_id"] = row["phase1_candidate_id"]
            row["candidate_alias_evidence"] = None
    alias_map["full_resolution_candidate_consolidation"] = candidate_consolidations
    write_json(OUTPUTS / "candidate_alias_map_v2_phase1_5.json", alias_map)

    for seed_number in (21, 162, 169):
        diagnostic = build_target_diagnostics(
            seed_number, candidates, tracks, alias_map, alias_rows, failure_rows
        )
        write_json(OUTPUTS / "debug" / f"phase1_5_seed_{seed_number}.json", diagnostic)

    review_queue = stratified_review_queue(
        candidates, measurements, tracks, alias_map, pair_rows, config
    )
    write_json(OUTPUTS / "review_queue_v2_phase1_5.json", review_queue)
    build_review_data_bundle(
        review_queue,
        candidates,
        tracks,
        alias_map,
        pair_rows,
        config,
        include_crops=not args.skip_review_crops,
    )
    print(f"Phase 1.5 review queue: {review_queue['queue_size']:,} candidates", flush=True)
    if args.manual_seeds:
        manual_payload = evaluate_manual_review_seeds(
            args.manual_seeds, args.source, args.viewer_data, phase1_config
        )
        print(f"Phase 1.5 manual seeds evaluated: {manual_payload['manual_seed_count']:,}", flush=True)

    # Later steps enrich target diagnostics, review queue, local crops and docs.
    run_summary = {
        "algorithm_version": phase15.ALGORITHM_VERSION,
        "runtime_seconds": time.perf_counter() - started,
        "baseline_before": baseline_before,
        "baseline_after": verify_locked_baseline(),
        "counts": {
            "phase1_candidates": len(candidates),
            "original_seeds": len(observations),
            "collapsed_observations": len(collapsed),
            "initial_tracks": initial_track_count,
            "refined_tracks": len(tracks),
            "canonical_tracks": len(canonical_tracks),
            "track_aliases": sum(track["identity_status"] == "DUPLICATE_ALIAS" for track in tracks),
            "unassigned_observations": len(unassigned),
            "failure_rows": len(failure_rows),
            "sampled_full_comparisons": len(comparison_rows),
            "final_measurement_pairs": len(pair_rows),
            "definite_final_measurement_alias_candidates": candidate_consolidations["alias_candidate_count"],
            "final_63_unique_after_definite_alias_consolidation": candidate_consolidations["final_63_unique_after_definite_alias_consolidation"],
            "review_queue": review_queue["queue_size"],
        },
    }
    write_json(OUTPUTS / "v2_phase1_5_run_summary.json", run_summary)
    print(json.dumps(run_summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
