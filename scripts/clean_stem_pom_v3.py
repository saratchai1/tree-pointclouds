#!/usr/bin/env python3
"""Coverage-first clean-stem POM selection from preserved Samut V2 evidence.

This module is intentionally additive. It reads the published V2 review and
inventory products, never edits them, and produces a separate V3 screening
lane. The published profiles stop at 3.50 m AGL, so the configured 4.00 m
search ceiling is recorded but not extrapolated.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
from statistics import median
from typing import Any, Iterable


AUTOMATIC_STATUSES = {"STANDARD_DBH", "ALTERNATIVE_POM"}
SOURCE_CANDIDATE = "SOURCE_CANDIDATE"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def compact_json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}"
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def clipped(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def rounded(value: Any, digits: int = 6) -> Any:
    return round(float(value), digits) if finite(value) else None


def vector_norm(vector: Iterable[float]) -> float:
    return math.sqrt(sum(float(value) ** 2 for value in vector))


def normalized(vector: Iterable[float]) -> list[float] | None:
    values = [float(value) for value in vector]
    norm = vector_norm(values)
    return [value / norm for value in values] if norm > 1e-12 else None


def cross(a: list[float], b: list[float]) -> list[float]:
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def axis_geometry(entry: dict, pom_m: float | None, ground_z_m: float | None) -> dict:
    coefficients = (entry.get("track") or {}).get("centreline_coefficients")
    if not coefficients or len(coefficients) != 2:
        return {
            "source": "UNAVAILABLE",
            "direction_unit": None,
            "inclination_deg": None,
            "centreline_coefficients": None,
            "measurement_plane": None,
        }
    sx, ix = [float(value) for value in coefficients[0]]
    sy, iy = [float(value) for value in coefficients[1]]
    direction = normalized([sx, sy, 1.0])
    inclination = math.degrees(math.atan(math.hypot(sx, sy)))
    plane = None
    if direction and finite(pom_m) and finite(ground_z_m):
        center = [sx * float(pom_m) + ix, sy * float(pom_m) + iy, float(ground_z_m) + float(pom_m)]
        basis_u = normalized([-direction[1], direction[0], 0.0])
        if basis_u is None:
            basis_u = [1.0, 0.0, 0.0]
        basis_v = normalized(cross(direction, basis_u))
        plane = {
            "center_xyz": [rounded(value) for value in center],
            "axis_direction": [rounded(value) for value in direction],
            "basis_u": [rounded(value) for value in basis_u],
            "basis_v": [rounded(value) for value in basis_v],
            "orientation": "PERPENDICULAR_TO_LOCAL_STEM_AXIS",
            "height_agl_m": rounded(pom_m, 3),
        }
    return {
        "source": "PHASE1_5_ROBUST_TRACK_CENTRELINE",
        "direction_unit": [rounded(value) for value in direction] if direction else None,
        "inclination_deg": rounded(inclination, 3),
        "centreline_coefficients": [[rounded(sx), rounded(ix)], [rounded(sy), rounded(iy)]],
        "measurement_plane": plane,
    }


def observations_near_window(entry: dict, start_m: float, end_m: float) -> list[dict]:
    observations = [
        row for row in (entry.get("track") or {}).get("observations", [])
        if finite(row.get("source_height_m")) and row.get("fit_validity", True)
    ]
    nearby = [row for row in observations if start_m - 0.051 <= float(row["source_height_m"]) <= end_m + 0.051]
    if nearby:
        return nearby
    center = (start_m + end_m) / 2.0
    ranked = sorted(observations, key=lambda row: abs(float(row["source_height_m"]) - center))
    return ranked[:1] if ranked and abs(float(ranked[0]["source_height_m"]) - center) <= 0.30 else []


def median_or_none(values: Iterable[Any]) -> float | None:
    usable = [float(value) for value in values if finite(value)]
    return float(median(usable)) if usable else None


def identity_penalty(entry: dict, tree: dict, source_labels: list[str]) -> float:
    if "TRUE_MAIN_STEM" in source_labels:
        return 0.0
    detection = (tree.get("detection") or {}).get("status")
    penalty = {"CONFIRMED": 0.02, "PROBABLE": 0.08, "UNCERTAIN": 0.22}.get(detection, 0.18)
    if entry.get("identity_status") not in {"AUTO_HIGH_SUPPORT", "HUMAN_CONFIRMED"}:
        penalty += 0.04
    if entry.get("potential_duplicate"):
        penalty += 0.10
    return clipped(penalty)


def score_window(entry: dict, window: dict, tree: dict, labels: list[str], config: dict) -> dict | None:
    required = (
        "start_height_m", "end_height_m", "median_radius_m", "supporting_slice_count",
        "median_angular_coverage_deg", "median_fit_residual_m", "radius_residual_mad_m",
    )
    if any(not finite(window.get(key)) for key in required):
        return None
    start_m = float(window["start_height_m"])
    end_m = float(window["end_height_m"])
    center_m = round((start_m + end_m) / 2.0, 3)
    search = config["height_search"]
    if center_m < search["minimum_height_m"] - 1e-9:
        return None
    if end_m > search["published_evidence_maximum_height_m"] + 1e-9:
        return None

    radius_m = float(window["median_radius_m"])
    if not 0.02 <= radius_m <= 0.75:
        return None
    observations = observations_near_window(entry, start_m, end_m)
    axis_ratios = [max(1.0, float(row["ellipse_axis_ratio"])) for row in observations if finite(row.get("ellipse_axis_ratio"))]
    axis_ratio = median_or_none(axis_ratios)
    circularity = 1.0 / axis_ratio if axis_ratio else None
    component_count = median_or_none(row.get("connected_component_count") for row in observations)
    raw_point_count = sum(int(row.get("point_count") or 0) for row in observations) or None
    inlier_count = sum(int(row.get("inlier_count") or 0) for row in observations) or None
    supporting = int(window["supporting_slice_count"])
    expected = int(round(search["window_width_m"] / search["profile_step_m"])) + 1
    coverage = float(window["median_angular_coverage_deg"])
    radius_mad = float(window["radius_residual_mad_m"])
    fit_rmse = float(window["median_fit_residual_m"])
    relative_radius_mad = radius_mad / max(radius_m, 1e-9)
    relative_fit_rmse = fit_rmse / max(radius_m, 1e-9)
    axis = axis_geometry(entry, center_m, entry.get("ground_z_m"))
    inclination = axis["inclination_deg"]
    track_span = float((entry.get("track") or {}).get("vertical_span_m") or 0.0)

    scaling = config["quality_scaling"]
    components = {
        "axis_verticality": clipped(1.0 - float(inclination or 90.0) / scaling["maximum_inclination_deg"]),
        "vertical_continuity": clipped(supporting / expected) * clipped(track_span / 1.0),
        "circularity": clipped(circularity if circularity is not None else 0.0),
        "radius_stability": clipped(1.0 - relative_radius_mad / scaling["maximum_relative_radius_mad"]),
        "angular_coverage": clipped(
            (coverage - scaling["minimum_angular_coverage_deg"])
            / (scaling["target_angular_coverage_deg"] - scaling["minimum_angular_coverage_deg"])
        ),
        "fit_quality": clipped(1.0 - relative_fit_rmse / scaling["maximum_relative_fit_rmse"]),
    }
    if component_count is None:
        clutter_penalty = 0.0
    else:
        clutter_penalty = clipped(
            (component_count - scaling["clutter_free_component_count"])
            / (scaling["maximum_component_count"] - scaling["clutter_free_component_count"])
        )
    id_penalty = identity_penalty(entry, tree, labels)
    weighted = sum(config["quality_weights"][key] * value for key, value in components.items())
    weighted -= scaling["clutter_penalty_weight"] * clutter_penalty
    weighted -= scaling["identity_penalty_weight"] * id_penalty
    score = 100.0 * clipped(weighted)

    return {
        "source_candidate_id": entry["candidate_id"],
        "source_track_id": entry.get("track_id") or (entry.get("track") or {}).get("track_id"),
        "start_height_m": rounded(start_m, 3),
        "end_height_m": rounded(end_m, 3),
        "center_height_m": rounded(center_m, 3),
        "window_width_m": rounded(end_m - start_m, 3),
        "radius_m": rounded(radius_m),
        "supporting_slice_count": supporting,
        "expected_slice_count": expected,
        "observation_count": len(observations),
        "point_count": raw_point_count,
        "inlier_count": inlier_count,
        "fit_rmse_m": rounded(fit_rmse),
        "relative_fit_rmse": rounded(relative_fit_rmse),
        "radius_mad_m": rounded(radius_mad),
        "relative_radius_mad": rounded(relative_radius_mad),
        "angular_coverage_deg": rounded(coverage, 2),
        "ellipse_axis_ratio": rounded(axis_ratio),
        "circularity": rounded(circularity),
        "connected_component_count": rounded(component_count, 2),
        "track_vertical_span_m": rounded(track_span, 3),
        "inclination_deg": inclination,
        "phase1_5_detection_quality": bool(window.get("detection_quality")),
        "phase1_5_automatic_quality": bool(window.get("automatic_measurement_quality")),
        "quality_components": {key: rounded(value) for key, value in components.items()},
        "penalties": {
            "clutter": rounded(clutter_penalty),
            "identity_ambiguity": rounded(id_penalty),
        },
        "quality_score": rounded(score, 2),
    }


def reliability_failures(window: dict, lane: str, config: dict) -> list[str]:
    thresholds = config["reliability"][lane]
    failures = []
    if not window["phase1_5_detection_quality"]:
        failures.append("NO_DETECTION_QUALITY_STABLE_WINDOW")
    if thresholds["require_phase1_5_automatic_quality"] and not window["phase1_5_automatic_quality"]:
        failures.append("PHASE1_5_AUTOMATIC_QUALITY_FAILED")
    if window.get("source_human_label_blocked"):
        failures.append("SOURCE_CANDIDATE_HUMAN_LABEL_BLOCKED")
    checks = (
        (window["quality_score"] < thresholds["minimum_quality_score"], "QUALITY_SCORE_BELOW_THRESHOLD"),
        (window["supporting_slice_count"] < thresholds["minimum_supporting_slices"], "INSUFFICIENT_WINDOW_CONTINUITY"),
        (window["angular_coverage_deg"] < thresholds["minimum_angular_coverage_deg"], "INSUFFICIENT_ARC_COVERAGE"),
        (window["circularity"] is None, "CIRCULARITY_EVIDENCE_UNAVAILABLE"),
        (window["circularity"] is not None and window["circularity"] < thresholds["minimum_circularity"], "LOW_CIRCULARITY"),
        (window["relative_radius_mad"] > thresholds["maximum_relative_radius_mad"], "UNSTABLE_RADIUS"),
        (window["relative_fit_rmse"] > thresholds["maximum_relative_fit_rmse"], "HIGH_FIT_RMSE"),
        (window["inclination_deg"] is None, "LOCAL_AXIS_UNAVAILABLE"),
        (window["inclination_deg"] is not None and window["inclination_deg"] > thresholds["maximum_inclination_deg"], "AXIS_TOO_INCLINED"),
    )
    failures.extend(reason for failed, reason in checks if failed)
    return failures


def apply_cross_lane_qa(window: dict, current: dict, config: dict) -> None:
    """Flag large near-standard disagreement without treating either lane as truth."""
    qa = config["cross_lane_qa"]
    field_height = current.get("field_aid_measurement_height_agl_m")
    field_circumference = current.get("field_aid_circumference_cm")
    if (
        not finite(field_height)
        or not math.isclose(float(field_height), qa["reference_height_m"], abs_tol=0.001)
        or not finite(field_circumference)
        or window["center_height_m"] > qa["maximum_checked_window_center_m"]
    ):
        window["cross_lane_reference_diameter_cm"] = None
        window["cross_lane_relative_diameter_difference"] = None
        window["cross_lane_consistent"] = None
        return
    reference_diameter = float(field_circumference) / math.pi
    candidate_diameter = float(window["radius_m"]) * 200.0
    difference = abs(candidate_diameter - reference_diameter) / max(reference_diameter, 1e-9)
    consistent = difference <= qa["maximum_relative_diameter_difference"]
    window["cross_lane_reference_diameter_cm"] = rounded(reference_diameter, 2)
    window["cross_lane_relative_diameter_difference"] = rounded(difference)
    window["cross_lane_consistent"] = consistent
    if not consistent:
        for key in ("standard_failures", "alternative_failures"):
            window[key].append("CROSS_LANE_DIAMETER_DISAGREEMENT")


def evidence_index(queue: dict, associations: dict, config: dict) -> tuple[dict[str, list[dict]], dict[str, list[str]]]:
    entries = queue["entries"]
    by_candidate = {row["candidate_id"]: row for row in entries}
    by_track: dict[str, list[dict]] = defaultdict(list)
    by_source_candidate: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        track = entry.get("track") or {}
        track_id = entry.get("track_id") or track.get("track_id")
        if track_id:
            by_track[track_id].append(entry)
        for candidate_id in track.get("source_candidate_ids", []):
            by_source_candidate[candidate_id].append(entry)

    source_rows: dict[str, list[dict]] = defaultdict(list)
    labels_by_tree: dict[str, list[str]] = defaultdict(list)
    for row in associations["candidate_associations"]:
        tree_id = row.get("tree_id")
        if not tree_id or row.get("disposition") != SOURCE_CANDIDATE:
            continue
        source_rows[tree_id].append(row)
        if row.get("human_label"):
            labels_by_tree[tree_id].append(row["human_label"])

    result: dict[str, list[dict]] = defaultdict(list)
    for tree_id, rows in source_rows.items():
        found = {}
        source_ids = {row["candidate_id"] for row in rows}
        label_by_candidate = {row["candidate_id"]: row.get("human_label") for row in rows}
        for row in rows:
            candidate_id = row["candidate_id"]
            candidates = []
            if candidate_id in by_candidate:
                candidates.append(by_candidate[candidate_id])
            candidates.extend(by_track.get(row.get("canonical_track_id"), []))
            candidates.extend(by_source_candidate.get(candidate_id, []))
            for entry in candidates:
                found[entry["candidate_id"]] = entry
        enriched = []
        for key in sorted(found):
            entry = dict(found[key])
            related_ids = source_ids & {
                entry["candidate_id"],
                *(entry.get("track") or {}).get("source_candidate_ids", []),
            }
            related_labels = sorted({label_by_candidate[candidate_id] for candidate_id in related_ids if label_by_candidate.get(candidate_id)})
            entry["_tree_source_candidate_ids"] = sorted(related_ids)
            entry["_source_human_labels"] = related_labels
            entry["_direct_human_label"] = label_by_candidate.get(entry["candidate_id"])
            entry["_source_human_label_blocked"] = bool(
                related_labels
                and "TRUE_MAIN_STEM" not in related_labels
                and all(label in config["blocked_human_labels"] for label in related_labels)
            )
            enriched.append(entry)
        result[tree_id] = enriched
    return result, labels_by_tree


def blocked_tree_reasons(tree: dict, current: dict, config: dict) -> list[str]:
    reasons = []
    if current.get("operationally_excluded"):
        reasons.append("OPERATIONALLY_EXCLUDED_TREE_ID")
    if current.get("identity_review_status") in config["blocked_identity_review_statuses"]:
        reasons.append(f"IDENTITY_REVIEW_{current['identity_review_status']}")
    detection = (tree.get("detection") or {}).get("status")
    if detection not in config["eligible_detection_statuses"]:
        reasons.append(f"TREE_DETECTION_{detection or 'UNAVAILABLE'}")
    return reasons


def select_tree_measurement(
    tree: dict,
    current: dict,
    entries: list[dict],
    source_labels: list[str],
    config: dict,
) -> dict:
    search = config["height_search"]
    blocked = blocked_tree_reasons(tree, current, config)
    scored = []
    entry_by_candidate = {entry["candidate_id"]: entry for entry in entries}
    for entry in entries:
        for raw_window in entry.get("stable_windows", []):
            entry_labels = entry.get("_source_human_labels", [])
            window = score_window(entry, raw_window, tree, entry_labels, config)
            if window:
                window["source_human_labels"] = entry_labels
                window["source_human_label_blocked"] = entry.get("_source_human_label_blocked", False)
                window["standard_failures"] = reliability_failures(window, "standard", config)
                window["alternative_failures"] = reliability_failures(window, "alternative", config)
                apply_cross_lane_qa(window, current, config)
                scored.append(window)

    standard_candidates = [
        row for row in scored
        if row["start_height_m"] <= search["standard_height_m"] <= row["end_height_m"]
    ]
    standard = [row for row in standard_candidates if not row["standard_failures"]]
    alternative = [
        row for row in scored
        if row["center_height_m"] >= search["minimum_alternative_height_m"]
        and not row["alternative_failures"]
    ]
    selected = None
    status = "MANUAL_REVIEW"
    if not blocked and standard:
        selected = sorted(
            standard,
            key=lambda row: (-row["quality_score"], abs(row["center_height_m"] - search["standard_height_m"]), row["source_candidate_id"]),
        )[0]
        status = "STANDARD_DBH"
    elif not blocked and alternative:
        selected = sorted(
            alternative,
            key=lambda row: (-row["quality_score"], row["center_height_m"], row["source_candidate_id"]),
        )[0]
        status = "ALTERNATIVE_POM"

    best = selected or (sorted(scored, key=lambda row: (-row["quality_score"], row["center_height_m"], row["source_candidate_id"]))[0] if scored else None)
    best_standard = sorted(
        standard_candidates,
        key=lambda row: (-row["quality_score"], abs(row["center_height_m"] - search["standard_height_m"]), row["source_candidate_id"]),
    )[0] if standard_candidates else None
    selected_entry = entry_by_candidate.get(best["source_candidate_id"]) if best else None
    ground_z_m = selected_entry.get("ground_z_m") if selected_entry else None
    if not finite(ground_z_m) and finite(current.get("measurement_plane_z_m")) and finite(current.get("measurement_height_agl_m")):
        ground_z_m = float(current["measurement_plane_z_m"]) - float(current["measurement_height_agl_m"])
    pom_m = search["standard_height_m"] if status == "STANDARD_DBH" else best["center_height_m"] if status == "ALTERNATIVE_POM" else None
    axis_height_m = pom_m if pom_m is not None else best["center_height_m"] if best else None
    axis = axis_geometry(selected_entry or {}, axis_height_m, ground_z_m)
    radius_m = best["radius_m"] if selected else None
    diameter_cm = 200.0 * radius_m if radius_m is not None else None
    circumference_cm = math.pi * diameter_cm if diameter_cm is not None else None
    failures = []
    if status == "MANUAL_REVIEW":
        failures.extend(blocked)
        if not entries:
            failures.append("NO_PUBLISHED_MULTI_HEIGHT_EVIDENCE_FOR_TREE")
        elif not scored:
            failures.append("NO_SCORABLE_WINDOW_IN_PUBLISHED_RANGE")
        elif best:
            lane = "standard" if best["start_height_m"] <= search["standard_height_m"] <= best["end_height_m"] else "alternative"
            failures.extend(best[f"{lane}_failures"])
        if not failures:
            failures.append("NO_RELIABLE_CLEAN_STEM_WINDOW")
    else:
        failures.append("STANDARD_HEIGHT_RELIABLE" if status == "STANDARD_DBH" else "STANDARD_HEIGHT_UNRELIABLE_CLEANER_WINDOW_SELECTED_ABOVE")
        if status == "ALTERNATIVE_POM" and best_standard:
            failures.extend(f"STANDARD_REJECTED_{reason}" for reason in best_standard["standard_failures"])
        failures.append("ROBUST_MULTI_SLICE_WINDOW")
        failures.append("POM_NOT_TIED_TO_PROP_ROOT_PLUS_030")

    confidence = "MANUAL_REVIEW"
    if selected:
        labels = config["confidence_labels"]
        confidence = "HIGH" if best["quality_score"] >= labels["high_minimum_quality_score"] else "MEDIUM" if best["quality_score"] >= labels["medium_minimum_quality_score"] else "LOW"
    position = tree.get("center") or (selected_entry or {}).get("position") or {}
    point_crop = (selected_entry or {}).get("point_crop_url")
    if point_crop:
        point_crop = f"../viewer-v2-review/{point_crop}"
    v2_measurement = tree.get("measurement") or {}
    return {
        "tree_id": tree["tree_id"],
        "location": {"x": rounded(position.get("x")), "y": rounded(position.get("y"))},
        "local_ground_z_m": rounded(ground_z_m),
        "status": status,
        "measurement_kind": status,
        "automatic_measurement": status in AUTOMATIC_STATUSES,
        "measurement_height_agl_m": rounded(pom_m, 3),
        "candidate_height_agl_m": best["center_height_m"] if best else None,
        "radius_m": rounded(radius_m),
        "diameter_cm": rounded(diameter_cm, 2),
        "diameter_at_pom_cm": rounded(diameter_cm, 2),
        "dbh_cm": rounded(diameter_cm, 2) if status == "STANDARD_DBH" else None,
        "circumference_cm": rounded(circumference_cm, 2),
        "fit_model": "PUBLISHED_V2_ROBUST_MULTI_SLICE_CIRCLE_SCREENING" if selected else None,
        "source_slice_orientation": "HORIZONTAL_XY_PROFILE",
        "measurement_plane_orientation": "PERPENDICULAR_TO_LOCAL_STEM_AXIS" if axis["measurement_plane"] else None,
        "perpendicular_refit_performed": False,
        "fit_rmse_m": best["fit_rmse_m"] if best else None,
        "circularity": best["circularity"] if best else None,
        "ellipse_axis_ratio": best["ellipse_axis_ratio"] if best else None,
        "arc_coverage_deg": best["angular_coverage_deg"] if best else None,
        "radius_stability_mad_m": best["radius_mad_m"] if best else None,
        "radius_stability_relative_mad": best["relative_radius_mad"] if best else None,
        "vertical_continuity_score": best["quality_components"]["vertical_continuity"] if best else None,
        "supporting_slice_count": best["supporting_slice_count"] if best else None,
        "point_count": best["point_count"] if best else None,
        "inlier_count": best["inlier_count"] if best else None,
        "quality_score": best["quality_score"] if best else None,
        "quality_components": best["quality_components"] if best else None,
        "quality_penalties": best["penalties"] if best else None,
        "confidence_label": confidence,
        "confidence_is_calibrated": False,
        "local_axis": {key: value for key, value in axis.items() if key != "measurement_plane"},
        "measurement_plane": axis["measurement_plane"] if selected else None,
        "best_review_plane": axis["measurement_plane"] if not selected else None,
        "reason_codes": list(dict.fromkeys(failures)),
        "field_verified": False,
        "protocol_final": False,
        "source_candidate_id": best["source_candidate_id"] if best else None,
        "source_track_id": best["source_track_id"] if best else None,
        "source_human_labels": (selected_entry or {}).get("_source_human_labels", []),
        "tree_source_human_labels": sorted(set(source_labels)),
        "point_crop_url": point_crop,
        "selected_window": best if selected else None,
        "best_review_window": best if not selected else None,
        "standard_height_diagnostics": {
            "candidate_available": best_standard is not None,
            "candidate_window": best_standard,
            "accepted": status == "STANDARD_DBH",
            "failure_reasons": best_standard["standard_failures"] if best_standard else ["NO_STANDARD_HEIGHT_WINDOW"],
        },
        "scored_windows": sorted(scored, key=lambda row: (row["center_height_m"], row["source_candidate_id"])),
        "track": (selected_entry or {}).get("track"),
        "source_providers": (selected_entry or {}).get("source_providers", []),
        "v2_baseline": {
            "phase4_measurement_status": v2_measurement.get("status"),
            "phase4_pom_m": v2_measurement.get("pom_m"),
            "phase4_circumference_cm": v2_measurement.get("circumference_cm"),
            "current_field_aid_status": current.get("field_aid_status"),
            "current_field_aid_height_agl_m": current.get("field_aid_measurement_height_agl_m"),
            "current_field_aid_circumference_cm": current.get("field_aid_circumference_cm"),
        },
    }


def source_paths(root: Path) -> dict[str, Path]:
    return {
        "config": root / "config/clean_stem_pom_v3.json",
        "phase1_5_review_queue": root / "site/public/viewer-v2-review/data/review_queue.json",
        "candidate_tree_associations": root / "site/public/viewer-v2-review/data/phase3_candidate_tree_associations.json",
        "phase4_tree_inventory": root / "site/public/viewer-v2-review/data/phase4_tree_inventory.json",
        "current_lidar_measurements": root / "site/public/data/lidar-measurements/measurements.json",
    }


def build_artifacts(root: Path, config_path: Path | None = None) -> tuple[dict, dict, bytes, dict]:
    paths = source_paths(root)
    if config_path is not None:
        paths["config"] = config_path
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing V3 inputs: " + ", ".join(missing))
    config = read_json(paths["config"])
    queue = read_json(paths["phase1_5_review_queue"])
    associations = read_json(paths["candidate_tree_associations"])
    inventory = read_json(paths["phase4_tree_inventory"])
    current_payload = read_json(paths["current_lidar_measurements"])
    current_by_tree = {row["tree_id"]: row for row in current_payload["records"]}
    evidence_by_tree, labels_by_tree = evidence_index(queue, associations, config)

    records = [
        select_tree_measurement(
            tree,
            current_by_tree[tree["tree_id"]],
            evidence_by_tree.get(tree["tree_id"], []),
            labels_by_tree.get(tree["tree_id"], []),
            config,
        )
        for tree in sorted(inventory["trees"], key=lambda row: row["tree_id"])
    ]
    if len(records) != 118 or len({row["tree_id"] for row in records}) != 118:
        raise RuntimeError("The preserved 118 physical Tree IDs are required")

    source = {
        "site_id": config["site_id"],
        "field_verified": False,
        "raw_las_in_repository": False,
        "files": {
            key: {"path": str(path.relative_to(root)), "sha256": sha256_path(path)}
            for key, path in paths.items()
        },
        "requested_maximum_height_m": config["height_search"]["requested_maximum_height_m"],
        "published_evidence_maximum_height_m": config["height_search"]["published_evidence_maximum_height_m"],
        "maximum_robust_window_center_m": rounded(
            config["height_search"]["published_evidence_maximum_height_m"]
            - config["height_search"]["window_width_m"] / 2.0,
            3,
        ),
        "interpretation": "Derived from preserved Samut Songkhram sampled multi-height evidence; not field ground truth and not a full-LAS refit.",
        "geometry_limit": "Published stable-window radii originate from horizontal XY profiles. V3 supplies a perpendicular marking/debug plane, but a new perpendicular full-resolution refit was not possible without the excluded raw LAS.",
    }
    compact_records = [compact_record(record) for record in records]
    payload = {
        "algorithm_version": config["algorithm_version"],
        "workflow": "SEPARATE_CLEAN_STEM_POM_V3",
        "source": source,
        "tree_count": len(records),
        "field_verified": False,
        "records": compact_records,
    }
    summary = build_summary(records, inventory, current_payload, source, config)
    csv_bytes = render_csv(records)
    review_queue = build_review_queue(config["algorithm_version"], summary, records)
    return payload, summary, csv_bytes, review_queue


def build_summary(records: list[dict], inventory: dict, current: dict, source: dict, config: dict) -> dict:
    status_counts = Counter(row["status"] for row in records)
    confidence_counts = Counter(row["confidence_label"] for row in records)
    automatic_ids = {row["tree_id"] for row in records if row["automatic_measurement"]}
    v2_ids = {
        tree["tree_id"] for tree in inventory["trees"]
        if (tree.get("measurement") or {}).get("status") == "MEASURABLE"
    }
    field_aid_ids = {
        row["tree_id"] for row in current["records"]
        if finite(row.get("field_aid_circumference_cm"))
    }
    exclusion_ids = {
        row["tree_id"] for row in current["records"]
        if row.get("operationally_excluded")
    }
    manual_reasons = Counter(
        reason for row in records if row["status"] == "MANUAL_REVIEW" for reason in row["reason_codes"]
    )
    return {
        "algorithm_version": config["algorithm_version"],
        "workflow": "SEPARATE_CLEAN_STEM_POM_V3",
        "tree_count": len(records),
        "status_counts": dict(sorted(status_counts.items())),
        "confidence_label_counts": dict(sorted(confidence_counts.items())),
        "automatic_measurement_count": len(automatic_ids),
        "automatic_measurement_tree_ids": sorted(automatic_ids),
        "manual_review_count": status_counts["MANUAL_REVIEW"],
        "manual_review_reason_counts": dict(sorted(manual_reasons.items())),
        "operational_exclusion_count": len(exclusion_ids),
        "operational_exclusion_tree_ids": sorted(exclusion_ids),
        "v2_coverage_comparison": {
            "metric": "tree_count_with_automatic_geometry_measurement",
            "v2_phase4_measurable_count": len(v2_ids),
            "v3_automatic_count": len(automatic_ids),
            "net_change_count": len(automatic_ids) - len(v2_ids),
            "newly_automatic_in_v3_count": len(automatic_ids - v2_ids),
            "newly_automatic_in_v3_tree_ids": sorted(automatic_ids - v2_ids),
            "v2_measurable_not_automatic_in_v3_count": len(v2_ids - automatic_ids),
            "v2_measurable_not_automatic_in_v3_tree_ids": sorted(v2_ids - automatic_ids),
            "current_field_aid_numeric_count_for_context": len(field_aid_ids),
            "accuracy_comparison_performed": False,
            "interpretation": "Coverage comparison only; no field-verified accuracy claim.",
        },
        "height_search": {
            **config["height_search"],
            "maximum_robust_window_center_m": source["maximum_robust_window_center_m"],
            "four_metre_search_executed": False,
            "reason": "Published source evidence ends at 3.50 m AGL.",
        },
        "source": source,
        "field_verified": False,
        "confidence_is_calibrated": False,
        "perpendicular_full_resolution_refit_performed": False,
    }


CSV_COLUMNS = [
    "tree_id", "status", "measurement_kind", "measurement_height_agl_m", "local_ground_z_m",
    "diameter_cm", "dbh_cm", "circumference_cm", "fit_model", "fit_rmse_m", "circularity",
    "ellipse_axis_ratio", "arc_coverage_deg", "radius_stability_mad_m",
    "radius_stability_relative_mad", "vertical_continuity_score", "supporting_slice_count",
    "point_count", "inlier_count", "inclination_deg", "quality_score", "confidence_label",
    "confidence_is_calibrated", "field_verified", "protocol_final", "source_candidate_id",
    "source_track_id", "source_slice_orientation", "measurement_plane_orientation",
    "perpendicular_refit_performed", "reason_codes",
]


def render_csv(records: list[dict]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for record in records:
        row = {key: record.get(key) for key in CSV_COLUMNS}
        row["inclination_deg"] = (record.get("local_axis") or {}).get("inclination_deg")
        row["reason_codes"] = "|".join(record.get("reason_codes", []))
        writer.writerow(row)
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def compact_record(record: dict) -> dict:
    compact = {
        key: value
        for key, value in record.items()
        if key not in {
            "scored_windows", "track", "source_providers", "selected_window",
            "best_review_window", "standard_height_diagnostics",
        }
    }
    compact["selected_window"] = compact_window(record["selected_window"]) if record.get("selected_window") else None
    compact["best_review_window"] = compact_window(record["best_review_window"]) if record.get("best_review_window") else None
    diagnostics = record.get("standard_height_diagnostics") or {}
    compact["standard_height_diagnostics"] = {
        "candidate_available": diagnostics.get("candidate_available", False),
        "candidate_window": compact_window(diagnostics["candidate_window"]) if diagnostics.get("candidate_window") else None,
        "accepted": diagnostics.get("accepted", False),
        "failure_reasons": diagnostics.get("failure_reasons", []),
    }
    return compact


def compact_window(window: dict) -> dict:
    keep = {
        "source_candidate_id", "source_track_id", "start_height_m", "end_height_m",
        "center_height_m", "radius_m", "supporting_slice_count", "expected_slice_count",
        "fit_rmse_m", "radius_mad_m", "angular_coverage_deg", "circularity",
        "inclination_deg", "quality_score", "cross_lane_relative_diameter_difference",
        "cross_lane_consistent",
    }
    compact = {key: value for key, value in window.items() if key in keep}
    standard_failures = window.get("standard_failures", [])
    alternative_failures = window.get("alternative_failures", [])
    compact["standard_decision"] = standard_failures[0] if standard_failures else "PASS"
    compact["alternative_decision"] = alternative_failures[0] if alternative_failures else "PASS"
    return compact


def viewer_record(record: dict) -> dict:
    compact = compact_record(record)
    compact.pop("standard_height_diagnostics", None)
    compact.pop("tree_source_human_labels", None)
    return compact


def compact_track(track: dict | None) -> dict | None:
    if not track:
        return None
    observations = []
    observation_keys = {
        "source_height_m", "center", "radius_m", "circle_residual_m", "ellipse_axis_ratio",
        "angular_coverage_deg", "point_count", "inlier_count", "connected_component_count",
    }
    for observation in track.get("observations", []):
        observations.append({key: value for key, value in observation.items() if key in observation_keys})
    return {
        "track_id": track.get("track_id"),
        "centreline_coefficients": track.get("centreline_coefficients"),
        "source_heights_m": track.get("source_heights_m", []),
        "vertical_span_m": track.get("vertical_span_m"),
        "observations": observations,
    }


def review_window_subset(record: dict) -> list[dict]:
    source_candidate_id = record.get("source_candidate_id")
    windows = [
        window for window in record.get("scored_windows", [])
        if window.get("source_candidate_id") == source_candidate_id
    ]
    if not windows:
        return []
    selected = record.get("selected_window") or record.get("best_review_window")
    standard = (record.get("standard_height_diagnostics") or {}).get("candidate_window")
    chosen: dict[tuple[str, float], dict] = {}

    def include(window: dict | None) -> None:
        if window:
            chosen[(window["source_candidate_id"], window["center_height_m"])] = window

    include(selected)
    include(standard)
    for window in sorted(windows, key=lambda row: (-row["quality_score"], row["center_height_m"]))[:6]:
        include(window)
    for target_height in (1.30, 1.75, 2.25, 2.75, 3.25):
        include(min(windows, key=lambda row: (abs(row["center_height_m"] - target_height), -row["quality_score"])))
    return [compact_window(window) for window in sorted(chosen.values(), key=lambda row: row["center_height_m"])]


def build_review_queue(algorithm_version: str, summary: dict, records: list[dict]) -> dict:
    entries = []
    for record in records:
        review_windows = review_window_subset(record)
        entries.append({
            "review_item_id": record["tree_id"],
            "item_type": "V3_CLEAN_STEM_POM",
            "phase4_tree_id": record["tree_id"],
            "position": record["location"],
            "ground_z_m": record["local_ground_z_m"],
            "candidate_geometry_status": record["status"],
            "identity_status": record["confidence_label"],
            "measurement_status": record["status"],
            "measurement_rule": record["measurement_kind"],
            "measurement_height_m": record["measurement_height_agl_m"],
            "point_crop_url": record["point_crop_url"],
            "source_providers": record["source_providers"],
            "categories": [record["status"], f"CONFIDENCE_{record['confidence_label']}", "NOT_FIELD_VERIFIED"],
            "reason_codes": record["reason_codes"],
            "track": compact_track(record["track"]),
            "scored_windows": review_windows,
            "v3": viewer_record(record),
        })
    return {
        "algorithm_version": algorithm_version,
        "workflow": "SEPARATE_CLEAN_STEM_POM_V3",
        "queue_size": len(entries),
        "annotation_basis": "AI_SCREENING_NOT_FIELD_GROUND_TRUTH",
        "interpretation": "Standalone V3 clean-stem review; V2 remains unchanged.",
        "downloads": {"measurements_json": "data/measurements.json", "measurements_csv": "data/measurements.csv", "summary_json": "data/summary.json"},
        "summary": summary,
        "entries": entries,
    }


def write_artifacts(root: Path, output_directory: Path | None = None, config_path: Path | None = None) -> dict:
    payload, summary, csv_bytes, queue = build_artifacts(root, config_path)
    output = output_directory or root / "site/public/viewer-v3-clean-stem/data"
    atomic_write(output / "measurements.json", canonical_json_bytes(payload))
    atomic_write(output / "measurements.csv", csv_bytes)
    atomic_write(output / "summary.json", canonical_json_bytes(summary))
    atomic_write(output / "review_queue.json", compact_json_bytes(queue))
    return summary
