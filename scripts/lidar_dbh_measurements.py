#!/usr/bin/env python3
"""Full-resolution, protocol-aware LiDAR circumference/DBH measurements.

This is an operational layer after the frozen Phase 1--5A research products.
It reads the source LAS once, never rewrites a frozen output, and emits a new
set of measurement results plus browser marking evidence.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np

import stem_inventory_v2 as phase1
import stem_inventory_v2_phase5a as phase5a


ALGORITHM_VERSION = "lidar-dbh-full-resolution-field-aid-v2"
PROP_ROOT = "PROP_ROOT_PROTOCOL_APPLICABLE"
STANDARD = "STANDARD_NON_PROP_ROOT_PROTOCOL"
UNCERTAIN = "PROTOCOL_APPLICABILITY_UNCERTAIN"
NOT_REVIEWED = "NOT_REVIEWED"
FINAL_PROTOCOLS = {PROP_ROOT, STANDARD}
DETECTION_ELIGIBLE = {"CONFIRMED", "PROBABLE"}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.tmp-{os.getpid()}"
    try:
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def even_sample(points: np.ndarray, maximum: int) -> np.ndarray:
    if len(points) <= maximum:
        return points
    indexes = np.linspace(0, len(points) - 1, maximum, dtype=np.int64)
    return points[indexes]


def finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def effective_protocol(review: dict | None) -> str:
    review = review or {}
    return review.get("pilot_protocol_applicability") or review.get("protocol_applicability") or NOT_REVIEWED


def build_targets(
    inventory: dict,
    phase5a_records: dict,
    annotation: dict,
    identity_review: dict | None = None,
    phase1_measurements: dict | None = None,
    operational_exclusions: dict | None = None,
) -> list[dict]:
    reviews = {row["tree_id"]: row for row in annotation.get("annotations", [])}
    records = {row["tree_id"]: row for row in phase5a_records["records"]}
    phase1_by_candidate = {
        row["candidate_id"]: row
        for row in (phase1_measurements or {}).get("measurements", [])
    }
    excluded_by_tree = {
        row["tree_id"]: row
        for row in (operational_exclusions or {}).get("exclusions", [])
    }
    strict_identity = (identity_review or {}).get("strict") or {}
    identity_status_by_tree = {}
    for key, status in (
        ("true_positive_tree_ids", "TRUE_POSITIVE"),
        ("duplicate_tree_ids", "DUPLICATE"),
        ("false_positive_tree_ids", "FALSE_POSITIVE"),
        ("incorrect_merge_tree_ids", "INCORRECT_MERGE"),
        ("uncertain_tree_ids", "UNCERTAIN"),
    ):
        for tree_id in strict_identity.get(key, []):
            identity_status_by_tree[tree_id] = status
    targets = []
    for tree in sorted(inventory["trees"], key=lambda row: row["tree_id"]):
        tree_id = tree["tree_id"]
        record = records[tree_id]
        axis = record["main_stem"]
        review = reviews.get(tree_id)
        protocol = effective_protocol(review)
        legacy = tree.get("measurement") or {}
        legacy_accepted = (
            legacy.get("status") == "MEASURABLE"
            and finite(legacy.get("pom_m"))
            and finite(legacy.get("circumference_cm"))
        )
        if protocol == PROP_ROOT and finite((review or {}).get("reviewed_protocol_pom_height_agl_m")):
            height = float(review["reviewed_protocol_pom_height_agl_m"])
            measurement_kind = "PROP_ROOT_PLUS_030"
            protocol_resolved = True
            dbh_definition_applies = False
        elif protocol == STANDARD:
            height = 1.30
            measurement_kind = "STANDARD_DBH_1_30"
            protocol_resolved = True
            dbh_definition_applies = True
        elif legacy_accepted:
            height = float(legacy["pom_m"])
            if math.isclose(height, 1.30, abs_tol=1e-9):
                measurement_kind = "LEGACY_STANDARD_DBH_1_30"
                dbh_definition_applies = True
            else:
                measurement_kind = "LEGACY_ADAPTIVE_IRREGULAR_ZONE_PLUS_030"
                dbh_definition_applies = False
            protocol_resolved = False
        else:
            height = 1.30
            measurement_kind = "SCREENING_AT_1_30"
            protocol_resolved = False
            dbh_definition_applies = False
        measurement_axis = deepcopy(axis)
        legacy_evidence = None
        if legacy_accepted:
            legacy_evidence = phase1_by_candidate.get(legacy.get("source_candidate_id"))
            full_resolution = (legacy_evidence or {}).get("diagnostics", {}).get("full_resolution")
            if (
                full_resolution
                and finite(full_resolution.get("selected_height_m"))
                and math.isclose(float(full_resolution["selected_height_m"]), height, abs_tol=0.001)
                and full_resolution.get("centreline_coefficients")
            ):
                measurement_axis["centerline_coefficients"] = deepcopy(
                    full_resolution["centreline_coefficients"]
                )
                measurement_axis["ground_z_m"] = float(legacy_evidence["ground_z_m"])
                supported_heights = [
                    float(row["height_m"])
                    for row in full_resolution.get("perpendicular_slice_results", [])
                    if finite(row.get("height_m"))
                ]
                if supported_heights:
                    measurement_axis["vertical_range_agl_m"] = [
                        min(supported_heights), max(supported_heights)
                    ]
                measurement_axis["axis_uncertainty_m"] = float(
                    legacy_evidence.get("centreline_residual_p90_m") or 0.0
                )
                measurement_axis["axis_status"] = "CONFIRMED"
        initial = phase5a.axis_center_at_height(measurement_axis, height)
        targets.append({
            "tree_id": tree_id,
            "tree": tree,
            "record": record,
            "review": review,
            "protocol_applicability": protocol,
            "protocol_resolved": protocol_resolved,
            "measurement_kind": measurement_kind,
            "measurement_height_agl_m": height,
            "dbh_definition_applies": dbh_definition_applies,
            "legacy_full_resolution_accepted": legacy_accepted,
            "legacy_measurement": deepcopy(legacy) if legacy_accepted else None,
            "legacy_full_resolution_evidence": deepcopy(legacy_evidence) if legacy_evidence else None,
            "operational_exclusion": deepcopy(excluded_by_tree.get(tree_id)),
            "identity_review_status": identity_status_by_tree.get(tree_id, "NOT_REVIEWED"),
            "ground_z_m": float(axis["ground_z_m"]),
            "initial_center_xyz": np.asarray(initial, dtype=float),
            "frozen_axis": measurement_axis,
        })
    return targets


def las_neighbourhoods_once(
    source_path: Path,
    viewer_data_directory: Path,
    targets: list[dict],
    *,
    radius_m: float = 1.10,
    vertical_half_range_m: float = 0.48,
    cell_size_m: float = 0.35,
    chunk_size_points: int = 2_000_000,
) -> tuple[dict[str, np.ndarray], dict]:
    """Read the source LAS exactly once and collect all target neighbourhoods."""
    source_map, scale, offset, point_count = phase1._las_header(source_path)
    first_chunk = sorted(viewer_data_directory.glob("positions-*.glbin"))[0]
    viewer_first = np.fromfile(first_chunk, dtype="<f4", count=3).astype(np.float64)
    source_first = source_map[0]["xyz"].astype(np.float64) * scale + offset
    viewer_origin = source_first - viewer_first
    alignment_error_m = float(np.linalg.norm((source_first - viewer_origin) - viewer_first))
    if alignment_error_m > 0.001:
        raise RuntimeError(f"LAS/viewer alignment error {alignment_error_m:.6f} m")

    centers = np.asarray([row["initial_center_xyz"] for row in targets], dtype=float)
    x_min = float(centers[:, 0].min() - radius_m)
    x_max = float(centers[:, 0].max() + radius_m)
    y_min = float(centers[:, 1].min() - radius_m)
    y_max = float(centers[:, 1].max() + radius_m)
    nx = max(1, int(math.ceil((x_max - x_min) / cell_size_m)))
    ny = max(1, int(math.ceil((y_max - y_min) / cell_size_m)))
    cell_targets: dict[int, list[int]] = defaultdict(list)
    for target_index, center in enumerate(centers):
        ix0 = max(0, int(math.floor((center[0] - radius_m - x_min) / cell_size_m)))
        ix1 = min(nx - 1, int(math.floor((center[0] + radius_m - x_min) / cell_size_m)))
        iy0 = max(0, int(math.floor((center[1] - radius_m - y_min) / cell_size_m)))
        iy1 = min(ny - 1, int(math.floor((center[1] + radius_m - y_min) / cell_size_m)))
        for iy in range(iy0, iy1 + 1):
            for ix in range(ix0, ix1 + 1):
                cell_targets[iy * nx + ix].append(target_index)

    collected: list[list[np.ndarray]] = [[] for _ in targets]
    scanned_in_bounds = 0
    for start in range(0, point_count, chunk_size_points):
        stop = min(start + chunk_size_points, point_count)
        raw = source_map[start:stop]["xyz"]
        xyz = raw.astype(np.float64) * scale + offset - viewer_origin
        inside = (
            (xyz[:, 0] >= x_min) & (xyz[:, 0] < x_max)
            & (xyz[:, 1] >= y_min) & (xyz[:, 1] < y_max)
        )
        if inside.any():
            xyz = xyz[inside]
            scanned_in_bounds += int(len(xyz))
            ix = np.clip(((xyz[:, 0] - x_min) / cell_size_m).astype(np.int32), 0, nx - 1)
            iy = np.clip(((xyz[:, 1] - y_min) / cell_size_m).astype(np.int32), 0, ny - 1)
            keys = iy * nx + ix
            order = np.argsort(keys)
            sorted_keys = keys[order]
            boundaries = np.flatnonzero(np.r_[True, sorted_keys[1:] != sorted_keys[:-1], True])
            for left, right in zip(boundaries[:-1], boundaries[1:]):
                owners = cell_targets.get(int(sorted_keys[left]))
                if not owners:
                    continue
                cell_points = xyz[order[left:right]]
                for owner in owners:
                    center = centers[owner]
                    mask = (
                        np.linalg.norm(cell_points[:, :2] - center[:2], axis=1) <= radius_m
                    ) & (np.abs(cell_points[:, 2] - center[2]) <= vertical_half_range_m)
                    if mask.any():
                        collected[owner].append(cell_points[mask].astype(np.float32))
        if start and start % 10_000_000 == 0:
            print(f"Full LAS scan {start:,}/{point_count:,}", flush=True)

    neighbourhoods = {
        target["tree_id"]: (
            np.concatenate(parts).astype(np.float64, copy=False)
            if parts else np.empty((0, 3), dtype=np.float64)
        )
        for target, parts in zip(targets, collected)
    }
    return neighbourhoods, {
        "source_las_point_count": int(point_count),
        "source_las_scan_count": 1,
        "points_in_union_xy_bounds": int(scanned_in_bounds),
        "viewer_origin_offset_m": viewer_origin.tolist(),
        "alignment_error_m": alignment_error_m,
        "extraction_radius_m": radius_m,
        "vertical_half_range_m": vertical_half_range_m,
        "chunk_size_points": chunk_size_points,
    }


def _best_fit(fitted: dict, predicted: np.ndarray, radius_hint: float) -> dict | None:
    valid = [row for row in fitted.get("fits", []) if row.get("valid")]
    if not valid:
        return None
    return min(valid, key=lambda row: (
        float(np.linalg.norm(np.asarray(row["center"], dtype=float) - predicted))
        + 0.20 * abs(float(row["radius_m"]) - radius_hint)
        + 2.0 * float(row["circle_residual_m"])
        - 0.0002 * int(row.get("inlier_count") or 0)
    ))


def refine_axis(target: dict, local: np.ndarray, config: dict) -> dict:
    tree_id = target["tree_id"]
    height = target["measurement_height_agl_m"]
    ground = target["ground_z_m"]
    frozen = target["frozen_axis"]
    historical = target["tree"].get("measurement") or {}
    radius_hint = (
        float(historical["circumference_cm"]) / (2 * math.pi * 100)
        if finite(historical.get("circumference_cm")) else 0.08
    )
    rows = []
    for ordinal, delta in enumerate((-0.15, -0.10, -0.05, 0.0, 0.05, 0.10, 0.15)):
        slice_height = height + delta
        section = local[np.abs(local[:, 2] - (ground + slice_height)) <= 0.04]
        predicted = phase5a.axis_center_at_height(frozen, slice_height)[:2]
        rng = np.random.default_rng(
            config["random_seed"] + int(hashlib.sha256(tree_id.encode()).hexdigest()[:8], 16) + ordinal
        )
        fitted = phase1.fit_slice_profile(section[:, :2], predicted, config, rng, full_resolution=True)
        fit = _best_fit(fitted, predicted, radius_hint)
        if fit is not None and np.linalg.norm(np.asarray(fit["center"]) - predicted) <= 0.30:
            rows.append({"height_agl_m": slice_height, "point_count": len(section), "fit": fit})
    if len(rows) >= 3:
        heights = np.asarray([row["height_agl_m"] for row in rows], dtype=float)
        centers = np.asarray([row["fit"]["center"] for row in rows], dtype=float)
        coefficients, residuals = phase1.robust_centreline(heights, centers, config)
        axis = np.asarray([coefficients[0, 0], coefficients[1, 0], 1.0], dtype=float)
        axis /= np.linalg.norm(axis)
        center = np.asarray([
            coefficients[0, 0] * height + coefficients[0, 1],
            coefficients[1, 0] * height + coefficients[1, 1],
            ground + height,
        ])
        uncertainty = float(np.percentile(residuals, 90))
        source = "FULL_SOURCE_LAS_LOCAL_AXIS_REFIT"
        status = "CONFIRMED" if uncertainty <= 0.08 else "PROBABLE"
    else:
        center = phase5a.axis_center_at_height(frozen, height)
        axis = phase5a.axis_direction(frozen)
        coefficients = np.asarray(frozen["centerline_coefficients"], dtype=float)
        uncertainty = float(frozen.get("axis_uncertainty_m") or math.inf)
        source = "FROZEN_PHASE5A_AXIS_FALLBACK"
        status = frozen.get("axis_status") or "NEEDS_REVIEW"
    return {
        "status": status,
        "source": source,
        "center_xyz": np.asarray(center, dtype=float),
        "direction": np.asarray(axis, dtype=float),
        "coefficients": np.asarray(coefficients, dtype=float),
        "uncertainty_m": uncertainty,
        "supporting_slice_count": len(rows),
        "supporting_slices": [{
            "height_agl_m": round(row["height_agl_m"], 3),
            "point_count": int(row["point_count"]),
            "center_xy": np.asarray(row["fit"]["center"]).tolist(),
            "radius_m": float(row["fit"]["radius_m"]),
            "coverage_deg": float(row["fit"]["angular_coverage_deg"]),
        } for row in rows],
    }


def fit_plane(
    tree_id: str,
    local: np.ndarray,
    center: np.ndarray,
    direction: np.ndarray,
    config: dict,
    *,
    thickness_m: float,
    plane_offset_m: float = 0.0,
    seed_offset: int = 0,
) -> tuple[dict, dict | None]:
    section = phase5a.extract_perpendicular_cross_section(
        local, center, direction, thickness_m, 0.55, plane_offset_m
    )
    if len(section["plane_xy"]) < 20:
        return section, None
    seed = int(hashlib.sha256(tree_id.encode()).hexdigest()[:8], 16) + config["random_seed"] + seed_offset
    fitted = phase1.fit_slice_profile(
        section["plane_xy"], np.zeros(2), config, np.random.default_rng(seed), full_resolution=True
    )
    return section, _best_fit(fitted, np.zeros(2), 0.08)


def _compact_fit(fit: dict | None) -> dict | None:
    if not fit:
        return None
    result = {}
    for key in (
        "valid", "center", "radius_m", "circle_residual_m", "inlier_count",
        "angular_coverage_deg", "largest_missing_angular_sector_deg", "score",
        "inlier_tolerance_m", "component_index", "component_point_count",
        "local_point_density_per_m2", "ellipse",
    ):
        if key in fit:
            result[key] = phase1.json_ready(fit[key])
    return result


def _ellipse_circumference(ellipse: dict) -> float | None:
    if not ellipse.get("valid"):
        return None
    return phase1.ellipse_perimeter(
        float(ellipse["semi_major_axis_m"]), float(ellipse["semi_minor_axis_m"])
    ) * 100.0


def measure_target(target: dict, local: np.ndarray, config: dict) -> tuple[dict, dict]:
    tree_id = target["tree_id"]
    axis = refine_axis(target, local, config)
    center = axis["center_xyz"]
    direction = axis["direction"]
    exact_section, exact_fit = fit_plane(
        tree_id, local, center, direction, config, thickness_m=0.08
    )
    reasons = []
    candidate_circumference = None
    candidate_diameter = None
    selected_model = None
    circle_circumference = None
    ellipse_circumference = None
    qa_variants = []
    if exact_fit is None:
        reasons.append(
            "INSUFFICIENT_FULL_LAS_POINT_SUPPORT"
            if len(exact_section["points_xyz"]) < 20
            else "NO_PLAUSIBLE_FULL_LAS_CROSS_SECTION_FIT"
        )
    else:
        ellipse = exact_fit.get("ellipse") or {}
        circle_circumference = 2 * math.pi * float(exact_fit["radius_m"]) * 100
        ellipse_circumference = _ellipse_circumference(ellipse)
        use_ellipse = bool(
            ellipse.get("valid")
            and float(ellipse.get("ellipse_residual_m") or math.inf)
            < float(exact_fit["circle_residual_m"]) * config["full_resolution"]["ellipse_selection_residual_ratio"]
        )
        candidate_circumference = ellipse_circumference if use_ellipse else circle_circumference
        candidate_diameter = candidate_circumference / math.pi
        selected_model = "ELLIPSE" if use_ellipse else "CIRCLE"
        if int(exact_fit.get("inlier_count") or 0) < 20:
            reasons.append("INSUFFICIENT_INLIER_SUPPORT")
        if float(exact_fit.get("angular_coverage_deg") or 0) < 140.0:
            reasons.append("POOR_ANGULAR_COVERAGE")
        if float(np.linalg.norm(np.asarray(exact_fit["center"], dtype=float))) > 0.10:
            reasons.append("FIT_CENTER_INCONSISTENT_WITH_LOCAL_AXIS")
        if ellipse_circumference is not None:
            disagreement = abs(circle_circumference - ellipse_circumference) / max(
                circle_circumference, ellipse_circumference, 1e-9
            )
            if disagreement > 0.25:
                reasons.append("CIRCLE_ELLIPSE_DISAGREEMENT")
        radius = float(exact_fit["radius_m"])
        if phase5a.radius_at_configured_bound(radius, config, 1e-6):
            reasons.append("FITTED_RADIUS_AT_CONFIGURED_BOUND")
        if float(axis["uncertainty_m"]) > 0.15:
            reasons.append("LOCAL_AXIS_UNCERTAINTY_TOO_HIGH")
        if axis["source"] == "FROZEN_PHASE5A_AXIS_FALLBACK":
            z_min, z_max = target["frozen_axis"]["vertical_range_agl_m"]
            if not (float(z_min) <= target["measurement_height_agl_m"] <= float(z_max)):
                reasons.append("LOCAL_AXIS_NOT_SUPPORTED_AT_MEASUREMENT_HEIGHT")

        if target["protocol_resolved"] and target["tree"]["detection"]["status"] in DETECTION_ELIGIBLE:
            variant_values = []
            variant_index = 1
            for thickness in (0.06, 0.08, 0.10):
                for offset in (-0.03, 0.0, 0.03):
                    section, fit = fit_plane(
                        tree_id, local, center, direction, config,
                        thickness_m=thickness, plane_offset_m=offset, seed_offset=variant_index * 101,
                    )
                    value = None if fit is None else 2 * math.pi * float(fit["radius_m"]) * 100
                    qa_variants.append({
                        "slab_thickness_m": thickness,
                        "plane_offset_m": offset,
                        "point_count": int(len(section["points_xyz"])),
                        "circle_circumference_cm": None if value is None else round(value, 2),
                        "used_as_replacement_measurement": False,
                    })
                    if value is not None:
                        variant_values.append(value)
                    variant_index += 1
            if variant_values:
                variation = (max(variant_values) - min(variant_values)) / max(candidate_circumference, 1e-9)
                if variation > 0.20:
                    reasons.append("UNSTABLE_ACROSS_SLAB_OR_NEIGHBOURING_PLANES")
            else:
                reasons.append("NO_VALID_QA_VARIANT_FITS")

    geometric_status = "NOT_MEASURABLE" if exact_fit is None else "MEASURABLE" if not reasons else "NEEDS_REVIEW"
    detection_status = target["tree"]["detection"]["status"]
    if geometric_status != "MEASURABLE":
        acceptance_status = "NEEDS_LIDAR_REVIEW"
    elif not target["protocol_resolved"]:
        acceptance_status = "PROVISIONAL_PROTOCOL_REVIEW_REQUIRED"
    elif (
        detection_status not in DETECTION_ELIGIBLE
        or target["identity_review_status"] in {"DUPLICATE", "FALSE_POSITIVE", "INCORRECT_MERGE", "UNCERTAIN"}
    ):
        acceptance_status = "PROVISIONAL_TREE_IDENTITY_REVIEW_REQUIRED"
    else:
        acceptance_status = "FINAL_LIDAR_ESTIMATE"
    final = acceptance_status == "FINAL_LIDAR_ESTIMATE"
    circumference = round(candidate_circumference, 2) if final and candidate_circumference is not None else None
    diameter = round(candidate_diameter, 2) if final and candidate_diameter is not None else None
    dbh = diameter if final and target["dbh_definition_applies"] else None

    legacy_available = target["legacy_full_resolution_accepted"]
    legacy_height = (
        float(target["legacy_measurement"]["pom_m"])
        if legacy_available else None
    )
    legacy_circumference = (
        float(target["legacy_measurement"]["circumference_cm"])
        if legacy_available else None
    )
    legacy_diameter = (
        float(target["legacy_measurement"].get("equivalent_diameter_cm")
              or legacy_circumference / math.pi)
        if legacy_available else None
    )
    legacy_is_standard = bool(
        legacy_available and math.isclose(legacy_height, 1.30, abs_tol=1e-9)
    )

    # Operational field-aid values deliberately do not use protocol resolution as
    # a gate.  The hierarchy preserves the previously accepted 29 full-resolution
    # measurements, then adds clean new full-LAS fits.  Lower-confidence fits stay
    # visible for checking on site instead of being discarded.
    if target.get("operational_exclusion"):
        field_aid_status = "EXCLUDED_CONFIRMED_WRONG"
        field_aid_source = "HUMAN_OPERATOR_EXCLUSION"
        field_aid_circumference = None
        field_aid_diameter = None
        field_aid_height = target["measurement_height_agl_m"]
    elif final:
        field_aid_status = "READY_FOR_FIELD_USE"
        field_aid_source = "CURRENT_PROTOCOL_FINAL"
        field_aid_circumference = candidate_circumference
        field_aid_diameter = candidate_diameter
        field_aid_height = target["measurement_height_agl_m"]
    elif legacy_available:
        field_aid_status = "READY_FOR_FIELD_USE"
        field_aid_source = "LEGACY_FULL_RESOLUTION_ACCEPTED"
        field_aid_circumference = legacy_circumference
        field_aid_diameter = legacy_diameter
        field_aid_height = legacy_height
    elif geometric_status == "MEASURABLE" and candidate_circumference is not None:
        field_aid_status = "READY_FOR_FIELD_USE"
        field_aid_source = "FULL_LAS_GEOMETRY_ESTIMATE"
        field_aid_circumference = candidate_circumference
        field_aid_diameter = candidate_diameter
        field_aid_height = target["measurement_height_agl_m"]
    elif candidate_circumference is not None:
        field_aid_status = "CHECK_ON_SITE"
        field_aid_source = "FULL_LAS_REVIEW_CANDIDATE"
        field_aid_circumference = candidate_circumference
        field_aid_diameter = candidate_diameter
        field_aid_height = target["measurement_height_agl_m"]
    else:
        field_aid_status = "NO_ESTIMATE"
        field_aid_source = "NO_PLAUSIBLE_FIT"
        field_aid_circumference = None
        field_aid_diameter = None
        field_aid_height = target["measurement_height_agl_m"]

    field_aid_dbh = (
        field_aid_diameter
        if field_aid_diameter is not None
        and math.isclose(float(field_aid_height), 1.30, abs_tol=1e-9)
        else None
    )
    field_aid_fit = exact_fit
    field_aid_fit_model = selected_model
    field_aid_marking_source = "CURRENT_FULL_LAS_REFIT"
    if legacy_available and not final:
        archived = (
            (target.get("legacy_full_resolution_evidence") or {})
            .get("diagnostics", {})
            .get("full_resolution")
        )
        if archived and archived.get("circle_model"):
            field_aid_fit = deepcopy(archived["circle_model"])
            if archived.get("ellipse_model"):
                field_aid_fit["ellipse"] = deepcopy(archived["ellipse_model"])
            field_aid_fit_model = (
                (target.get("legacy_full_resolution_evidence") or {}).get("selected_model")
                or "CIRCLE"
            )
            field_aid_marking_source = "ARCHIVED_FULL_RESOLUTION_FIT"

    section_count = len(exact_section["points_xyz"])
    accepted_indexes = np.asarray([], dtype=np.int64)
    if exact_fit is not None:
        component_indexes = np.asarray(exact_fit.get("_component_point_indexes", []), dtype=np.int64)
        inlier_mask = np.asarray(exact_fit.get("_inlier_mask", []), dtype=bool)
        if len(component_indexes) == len(inlier_mask):
            accepted_indexes = component_indexes[inlier_mask]
    accepted_mask = np.zeros(section_count, dtype=bool)
    accepted_mask[accepted_indexes[accepted_indexes < section_count]] = True
    accepted_xyz = exact_section["points_xyz"][accepted_mask]
    rejected_xyz = exact_section["points_xyz"][~accepted_mask]
    accepted_xy = exact_section["plane_xy"][accepted_mask]
    rejected_xy = exact_section["plane_xy"][~accepted_mask]

    measurement = {
        "tree_id": tree_id,
        "detection_status": detection_status,
        "identity_review_status": target["identity_review_status"],
        "protocol_applicability": target["protocol_applicability"],
        "protocol_resolved": target["protocol_resolved"],
        "measurement_kind": target["measurement_kind"],
        "measurement_height_agl_m": round(target["measurement_height_agl_m"], 6),
        "measurement_plane_z_m": round(float(center[2]), 6),
        "geometric_status": geometric_status,
        "acceptance_status": acceptance_status,
        "circumference_cm": circumference,
        "dbh_cm": dbh,
        "diameter_at_measurement_height_cm": diameter,
        "candidate_circumference_cm": None if candidate_circumference is None else round(candidate_circumference, 2),
        "candidate_diameter_cm": None if candidate_diameter is None else round(candidate_diameter, 2),
        "candidate_dbh_cm": (
            round(candidate_diameter, 2)
            if candidate_diameter is not None and math.isclose(target["measurement_height_agl_m"], 1.30, abs_tol=1e-9)
            else None
        ),
        "field_aid_status": field_aid_status,
        "field_aid_source": field_aid_source,
        "field_aid_measurement_height_agl_m": round(float(field_aid_height), 6),
        "field_aid_circumference_cm": (
            None if field_aid_circumference is None else round(field_aid_circumference, 2)
        ),
        "field_aid_diameter_cm": (
            None if field_aid_diameter is None else round(field_aid_diameter, 2)
        ),
        "field_aid_dbh_cm": None if field_aid_dbh is None else round(field_aid_dbh, 2),
        "field_aid_is_current_protocol_final": final,
        "field_aid_fit_model": field_aid_fit_model,
        "field_aid_marking_source": field_aid_marking_source,
        "operationally_excluded": bool(target.get("operational_exclusion")),
        "operational_exclusion_decision": (
            (target.get("operational_exclusion") or {}).get("decision")
        ),
        "dbh_definition_applies": target["dbh_definition_applies"],
        "legacy_full_resolution_status": (
            "ACCEPTED" if target["legacy_full_resolution_accepted"] else "NOT_AVAILABLE"
        ),
        "legacy_measurement_rule": (
            "STANDARD_1_30"
            if legacy_is_standard
            else "ADAPTIVE_IRREGULAR_ZONE_PLUS_030"
            if legacy_available else None
        ),
        "legacy_measurement_height_agl_m": legacy_height,
        "legacy_circumference_cm": legacy_circumference,
        "legacy_diameter_cm": legacy_diameter,
        "legacy_dbh_cm": legacy_diameter if legacy_is_standard else None,
        "legacy_is_current_protocol_final": (
            final and target["legacy_full_resolution_accepted"]
            and math.isclose(
                float(target["legacy_measurement"]["pom_m"]),
                float(target["measurement_height_agl_m"]),
                abs_tol=0.001,
            )
        ),
        "fit_model": selected_model,
        "circle_circumference_cm": None if circle_circumference is None else round(circle_circumference, 2),
        "ellipse_circumference_cm": None if ellipse_circumference is None else round(ellipse_circumference, 2),
        "point_count": int(section_count),
        "inlier_count": int(len(accepted_xyz)),
        "angular_coverage_deg": None if exact_fit is None else round(float(exact_fit["angular_coverage_deg"]), 2),
        "axis_source": axis["source"],
        "axis_status": axis["status"],
        "axis_uncertainty_m": None if not math.isfinite(axis["uncertainty_m"]) else round(axis["uncertainty_m"], 6),
        "axis_supporting_slice_count": int(axis["supporting_slice_count"]),
        "protocol_plane_moved_to_cleaner_height": False,
        "field_verified": False,
        "qa_reason_codes": sorted(set(reasons)),
        "qa_variants": qa_variants,
        "marking_url": f"data/lidar-measurements/markings/{tree_id}.json",
    }
    marking = {
        "tree_id": tree_id,
        "source": "FULL_SOURCE_LAS_SINGLE_PASS",
        "field_verified": False,
        "measurement": measurement,
        "point_counts_before_display_sampling": {
            "neighbourhood": int(len(local)),
            "slice": int(section_count),
            "accepted_slice": int(len(accepted_xyz)),
            "rejected_slice": int(len(rejected_xyz)),
        },
        "display_points_xyz": even_sample(local, 5000).round(5).tolist(),
        "accepted_slice_points_xyz": even_sample(accepted_xyz, 2000).round(5).tolist(),
        "rejected_slice_points_xyz": even_sample(rejected_xyz, 2000).round(5).tolist(),
        "accepted_projected_points_xy": even_sample(accepted_xy, 2000).round(5).tolist(),
        "rejected_projected_points_xy": even_sample(rejected_xy, 2000).round(5).tolist(),
        "measurement_plane": {
            "center_xyz": center.round(8).tolist(),
            "axis_direction": direction.round(8).tolist(),
            "basis_u": exact_section["basis_u"].round(8).tolist(),
            "basis_v": exact_section["basis_v"].round(8).tolist(),
            "slab_thickness_m": 0.08,
            "height_agl_m": target["measurement_height_agl_m"],
            "orientation": "PERPENDICULAR_TO_LOCAL_STEM_AXIS",
        },
        "fit": _compact_fit(exact_fit),
        "field_aid_fit": _compact_fit(field_aid_fit),
        "field_aid_fit_model": field_aid_fit_model,
        "field_aid_marking_source": field_aid_marking_source,
        "axis_evidence": {
            "source": axis["source"],
            "status": axis["status"],
            "uncertainty_m": None if not math.isfinite(axis["uncertainty_m"]) else axis["uncertainty_m"],
            "supporting_slices": axis["supporting_slices"],
        },
    }
    return measurement, marking


def render_csv(measurements: list[dict]) -> bytes:
    fields = [
        "tree_id", "detection_status", "identity_review_status", "protocol_applicability", "protocol_resolved",
        "measurement_kind", "measurement_height_agl_m", "geometric_status", "acceptance_status",
        "circumference_cm", "dbh_cm", "diameter_at_measurement_height_cm",
        "candidate_circumference_cm", "candidate_dbh_cm", "dbh_definition_applies",
        "field_aid_status", "field_aid_source", "field_aid_measurement_height_agl_m",
        "field_aid_circumference_cm", "field_aid_diameter_cm", "field_aid_dbh_cm",
        "field_aid_is_current_protocol_final", "field_aid_fit_model", "field_aid_marking_source",
        "operationally_excluded", "operational_exclusion_decision",
        "legacy_full_resolution_status", "legacy_measurement_rule", "legacy_measurement_height_agl_m",
        "legacy_circumference_cm", "legacy_diameter_cm", "legacy_dbh_cm", "legacy_is_current_protocol_final",
        "fit_model", "point_count", "inlier_count", "angular_coverage_deg",
        "axis_source", "axis_status", "axis_uncertainty_m", "field_verified",
        "qa_reason_codes", "marking_url",
    ]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in measurements:
        encoded = {key: row.get(key) for key in fields}
        encoded["qa_reason_codes"] = "|".join(row["qa_reason_codes"])
        writer.writerow(encoded)
    return stream.getvalue().encode("utf-8-sig")


def build_summary(measurements: list[dict], source: dict) -> dict:
    finals = [row for row in measurements if row["acceptance_status"] == "FINAL_LIDAR_ESTIMATE"]
    dbh = [row for row in finals if row["dbh_cm"] is not None]
    prop = [row for row in finals if row["measurement_kind"] == "PROP_ROOT_PLUS_030"]
    candidates = [row for row in measurements if row["candidate_circumference_cm"] is not None]
    legacy = [row for row in measurements if row["legacy_full_resolution_status"] == "ACCEPTED"]
    legacy_standard = [row for row in legacy if row["legacy_measurement_rule"] == "STANDARD_1_30"]
    legacy_adaptive = [row for row in legacy if row["legacy_measurement_rule"] == "ADAPTIVE_IRREGULAR_ZONE_PLUS_030"]
    field_ready = [row for row in measurements if row["field_aid_status"] == "READY_FOR_FIELD_USE"]
    field_check = [row for row in measurements if row["field_aid_status"] == "CHECK_ON_SITE"]
    no_estimate = [row for row in measurements if row["field_aid_status"] == "NO_ESTIMATE"]
    excluded = [row for row in measurements if row["field_aid_status"] == "EXCLUDED_CONFIRMED_WRONG"]
    legacy_operational = [row for row in legacy if not row["operationally_excluded"]]
    prop_root = [row for row in measurements if row["measurement_kind"] == "PROP_ROOT_PLUS_030"]
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "source": source,
        "tree_count": len(measurements),
        "full_las_scan_count": 1,
        "final_lidar_measurement_count": len(finals),
        "final_standard_dbh_count": len(dbh),
        "final_prop_root_diameter_count": len(prop),
        "legacy_full_resolution_accepted_count": len(legacy),
        "legacy_standard_1_30_count": len(legacy_standard),
        "legacy_adaptive_plus_030_count": len(legacy_adaptive),
        "legacy_operational_count": len(legacy_operational),
        "field_aid_ready_count": len(field_ready),
        "field_aid_check_on_site_count": len(field_check),
        "field_aid_no_estimate_count": len(no_estimate),
        "operational_excluded_count": len(excluded),
        "prop_root_plus_030_count": len(prop_root),
        "prop_root_plus_030_ready_count": sum(
            row["field_aid_status"] == "READY_FOR_FIELD_USE" for row in prop_root
        ),
        "prop_root_plus_030_check_on_site_count": sum(
            row["field_aid_status"] == "CHECK_ON_SITE" for row in prop_root
        ),
        "provisional_candidate_count": len(candidates) - len(finals),
        "candidate_fit_count": len(candidates),
        "field_verified_count": 0,
        "geometric_status_counts": dict(sorted(Counter(row["geometric_status"] for row in measurements).items())),
        "acceptance_status_counts": dict(sorted(Counter(row["acceptance_status"] for row in measurements).items())),
        "protocol_counts": dict(sorted(Counter(row["protocol_applicability"] for row in measurements).items())),
        "final_tree_ids": [row["tree_id"] for row in finals],
        "final_standard_dbh_tree_ids": [row["tree_id"] for row in dbh],
        "final_prop_root_tree_ids": [row["tree_id"] for row in prop],
        "legacy_full_resolution_tree_ids": [row["tree_id"] for row in legacy],
        "legacy_standard_tree_ids": [row["tree_id"] for row in legacy_standard],
        "legacy_adaptive_tree_ids": [row["tree_id"] for row in legacy_adaptive],
        "field_aid_ready_tree_ids": [row["tree_id"] for row in field_ready],
        "field_aid_check_on_site_tree_ids": [row["tree_id"] for row in field_check],
        "field_aid_no_estimate_tree_ids": [row["tree_id"] for row in no_estimate],
        "operational_excluded_tree_ids": [row["tree_id"] for row in excluded],
        "legacy_operational_tree_ids": [row["tree_id"] for row in legacy_operational],
        "prop_root_plus_030_tree_ids": [row["tree_id"] for row in prop_root],
        "nonfinal_numeric_fields_are_null": all(
            row["circumference_cm"] is None and row["dbh_cm"] is None
            and row["diameter_at_measurement_height_cm"] is None
            for row in measurements if row["acceptance_status"] != "FINAL_LIDAR_ESTIMATE"
        ),
        "field_verified": False,
        "interpretation": (
            "Operational LiDAR field aid. Protocol resolution is a confidence label, not a gate that hides "
            "measurements. READY_FOR_FIELD_USE preserves all 29 earlier full-resolution accepted measurements "
            "and adds clean new full-LAS geometry fits. CHECK_ON_SITE remains visible as a lower-confidence "
            "candidate. The 11 legacy adaptive measurements use irregular-zone-top +0.30 m; they are useful "
            "field aids but are not relabelled as confirmed highest-prop-root-attachment measurements. "
            "Human-confirmed wrong Tree IDs are retained only as excluded audit records and contribute no "
            "operational circumference or diameter."
        ),
    }
