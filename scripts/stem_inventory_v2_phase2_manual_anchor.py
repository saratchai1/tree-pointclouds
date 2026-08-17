#!/usr/bin/env python3
"""Reviewer-click component association pilot for mangrove stems.

This module is deliberately additive. It does not alter the Phase 1 or Phase
1.5 algorithms. A clean-height hint selects an identity anchor near that
height, after which one sampled component is propagated up and down. Existing
Phase 1 stable-window and full-resolution acceptance functions remain the
measurement authority.
"""

from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
from typing import Any

import numpy as np
import yaml

import stem_inventory_v2 as phase1


ALGORITHM_VERSION = "stem-inventory-v2-phase2-manual-anchor-pilot"


def load_config(path: Path) -> dict:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if config.get("algorithm_version") != ALGORITHM_VERSION:
        raise ValueError("Unexpected Phase 2 manual-anchor pilot configuration version")
    return config


def _valid_fits(entry: dict) -> list[tuple[int, dict]]:
    return [
        (index, fit)
        for index, fit in enumerate(entry.get("fits", []))
        if fit.get("valid", True)
    ]


def select_identity_anchor(
    profile: list[dict],
    click_xy: np.ndarray,
    clean_height_hint_m: float,
    pilot_config: dict,
) -> dict:
    """Select a sampled component near the human height/XY hint.

    The hint selects an identity observation only. It is never returned as a
    measurement height and does not bypass geometric quality checks.
    """
    cfg = pilot_config["anchor_association"]
    ranked: list[tuple[float, int, int, dict, float, float]] = []
    for entry_index, entry in enumerate(profile):
        height_offset = abs(float(entry["height_m"]) - clean_height_hint_m)
        if height_offset > cfg["maximum_height_offset_m"] + 1e-9:
            continue
        for fit_index, fit in _valid_fits(entry):
            click_distance = float(np.linalg.norm(np.asarray(fit["center"], dtype=float) - click_xy))
            if click_distance > cfg["maximum_click_distance_m"]:
                continue
            coverage = float(fit.get("angular_coverage_deg", 0.0))
            residual = float(fit.get("circle_residual_m", math.inf))
            score = (
                cfg["click_distance_weight"] * click_distance
                + cfg["height_offset_weight"] * height_offset
                + cfg["incomplete_coverage_penalty"] * (1.0 - min(coverage, 360.0) / 360.0)
                + cfg["circle_residual_weight"] * residual
            )
            ranked.append((score, entry_index, fit_index, fit, click_distance, height_offset))
    if not ranked:
        raise ValueError(
            f"No valid sampled component within {cfg['maximum_click_distance_m']:.2f} m "
            f"and {cfg['maximum_height_offset_m']:.2f} m of the manual hints"
        )
    score, entry_index, fit_index, fit, click_distance, height_offset = min(
        ranked,
        key=lambda item: (item[0], item[5], item[4], item[1], item[2]),
    )
    return {
        "entry_index": entry_index,
        "fit_index": fit_index,
        "height_m": float(profile[entry_index]["height_m"]),
        "fit": fit,
        "association_score": score,
        "click_distance_m": click_distance,
        "height_offset_m": height_offset,
        "candidate_count_considered": len(ranked),
    }


def identity_anchor_hypotheses(
    profile: list[dict],
    click_xy: np.ndarray,
    clean_height_hint_m: float,
    pilot_config: dict,
) -> list[dict]:
    """Return every locally plausible identity anchor in deterministic order."""
    cfg = pilot_config["anchor_association"]
    hypotheses = []
    for entry_index, entry in enumerate(profile):
        height_offset = abs(float(entry["height_m"]) - clean_height_hint_m)
        if height_offset > cfg["maximum_height_offset_m"] + 1e-9:
            continue
        for fit_index, fit in _valid_fits(entry):
            click_distance = float(np.linalg.norm(np.asarray(fit["center"], dtype=float) - click_xy))
            if click_distance > cfg["maximum_click_distance_m"]:
                continue
            coverage = float(fit.get("angular_coverage_deg", 0.0))
            residual = float(fit.get("circle_residual_m", math.inf))
            score = (
                cfg["click_distance_weight"] * click_distance
                + cfg["height_offset_weight"] * height_offset
                + cfg["incomplete_coverage_penalty"] * (1.0 - min(coverage, 360.0) / 360.0)
                + cfg["circle_residual_weight"] * residual
            )
            hypotheses.append(
                {
                    "entry_index": entry_index,
                    "fit_index": fit_index,
                    "height_m": float(entry["height_m"]),
                    "fit": fit,
                    "association_score": score,
                    "click_distance_m": click_distance,
                    "height_offset_m": height_offset,
                }
            )
    hypotheses.sort(
        key=lambda item: (
            item["association_score"],
            item["height_offset_m"],
            item["click_distance_m"],
            item["entry_index"],
            item["fit_index"],
        )
    )
    for hypothesis in hypotheses:
        hypothesis["candidate_count_considered"] = len(hypotheses)
    return hypotheses


def _propagate_direction(
    profile: list[dict],
    selected: dict[int, tuple[int, dict]],
    anchor_index: int,
    direction: int,
    pilot_config: dict,
) -> list[dict]:
    cfg = pilot_config["vertical_propagation"]
    previous_index = anchor_index
    previous_fit = selected[anchor_index][1]
    missing = 0
    decisions = []
    indexes = range(anchor_index + direction, len(profile) if direction > 0 else -1, direction)
    for entry_index in indexes:
        entry = profile[entry_index]
        delta_height = abs(float(entry["height_m"]) - float(profile[previous_index]["height_m"]))
        center_gate = cfg["base_center_gate_m"] + cfg["maximum_centerline_slope_m_per_m"] * delta_height
        previous_center = np.asarray(previous_fit["center"], dtype=float)
        previous_radius = max(float(previous_fit["radius_m"]), 1e-6)
        compatible = []
        for fit_index, fit in _valid_fits(entry):
            center_distance = float(np.linalg.norm(np.asarray(fit["center"], dtype=float) - previous_center))
            if center_distance > center_gate:
                continue
            radius_change = abs(math.log(max(float(fit["radius_m"]), 1e-6) / previous_radius))
            cost = (
                center_distance
                + cfg["radius_log_change_weight"] * radius_change
                + cfg["circle_residual_weight"] * float(fit.get("circle_residual_m", math.inf))
                - cfg["angular_coverage_reward"] * float(fit.get("angular_coverage_deg", 0.0))
            )
            compatible.append((cost, fit_index, fit, center_distance, center_gate, radius_change))
        if not compatible:
            missing += 1
            decisions.append(
                {
                    "height_m": float(entry["height_m"]),
                    "selected": False,
                    "reason": "NO_COMPONENT_WITHIN_PROPAGATION_GATE",
                    "center_gate_m": center_gate,
                    "consecutive_missing": missing,
                }
            )
            if missing > cfg["maximum_consecutive_missing_slices"]:
                break
            continue
        cost, fit_index, fit, center_distance, center_gate, radius_change = min(
            compatible,
            key=lambda item: (item[0], item[3], item[1]),
        )
        selected[entry_index] = (fit_index, fit)
        decisions.append(
            {
                "height_m": float(entry["height_m"]),
                "selected": True,
                "fit_index": fit_index,
                "center": phase1.json_ready(fit["center"]),
                "radius_m": float(fit["radius_m"]),
                "center_distance_from_previous_m": center_distance,
                "center_gate_m": center_gate,
                "radius_log_change": radius_change,
                "association_cost": cost,
            }
        )
        previous_index = entry_index
        previous_fit = fit
        missing = 0
    return decisions


def build_anchored_profile(
    profile: list[dict],
    click_xy: list[float] | tuple[float, float] | np.ndarray,
    clean_height_hint_m: float,
    pilot_config: dict,
    phase1_config: dict | None = None,
) -> tuple[list[dict], dict]:
    """Return a one-component-per-height profile tied to the manual click.

    When the locked Phase 1 configuration is supplied, every local anchor is
    propagated and an anchor whose track is automatically stable around the
    clean-height hint is preferred. This prevents a nearby prop-root fragment
    from winning solely because it lies closest to the click.
    """
    click = np.asarray(click_xy, dtype=float)
    hypotheses = identity_anchor_hypotheses(profile, click, clean_height_hint_m, pilot_config)
    if not hypotheses:
        cfg = pilot_config["anchor_association"]
        raise ValueError(
            f"No valid sampled component within {cfg['maximum_click_distance_m']:.2f} m "
            f"and {cfg['maximum_height_offset_m']:.2f} m of the manual hints"
        )

    def materialize(anchor: dict) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
        selected: dict[int, tuple[int, dict]] = {
            anchor["entry_index"]: (anchor["fit_index"], anchor["fit"])
        }
        downward = _propagate_direction(profile, selected, anchor["entry_index"], -1, pilot_config)
        upward = _propagate_direction(profile, selected, anchor["entry_index"], 1, pilot_config)
        anchored = []
        track = []
        for entry_index, original in enumerate(profile):
            entry = deepcopy(original)
            chosen = selected.get(entry_index)
            if chosen is None:
                entry["fits"] = []
                entry["candidate_centres"] = []
                entry["fit_validity"] = False
                entry["rejection_reasons"] = list(dict.fromkeys(
                    list(entry.get("rejection_reasons", [])) + ["NOT_SELECTED_BY_MANUAL_ANCHOR_TRACK"]
                ))
            else:
                fit_index, fit = chosen
                entry["fits"] = [fit]
                entry["candidate_centres"] = [phase1.json_ready(fit["center"])]
                entry["fit_validity"] = True
                entry["rejection_reasons"] = []
                track.append(
                    {
                        "height_m": float(entry["height_m"]),
                        "original_fit_index": fit_index,
                        "center": phase1.json_ready(fit["center"]),
                        "radius_m": float(fit["radius_m"]),
                        "angular_coverage_deg": float(fit.get("angular_coverage_deg", 0.0)),
                        "circle_residual_m": float(fit.get("circle_residual_m", math.inf)),
                    }
                )
            anchored.append(entry)
        return anchored, track, downward, upward

    candidates = []
    for anchor in hypotheses:
        anchored, track, downward, upward = materialize(anchor)
        automatic_near_hint = []
        detection_near_hint = []
        if phase1_config is not None:
            windows = phase1.evaluate_stable_windows(anchored, phase1_config)
            automatic_near_hint = [
                window for window in windows
                if window.get("automatic_measurement_quality")
                and window["start_height_m"] <= clean_height_hint_m <= window["end_height_m"]
            ]
            detection_near_hint = [
                window for window in windows
                if window.get("detection_quality")
                and window["start_height_m"] <= clean_height_hint_m <= window["end_height_m"]
            ]
        candidates.append(
            {
                "anchor": anchor,
                "anchored_profile": anchored,
                "track": track,
                "downward": downward,
                "upward": upward,
                "automatic_near_hint": automatic_near_hint,
                "detection_near_hint": detection_near_hint,
            }
        )

    selected_hypothesis = min(
        candidates,
        key=lambda item: (
            not bool(item["automatic_near_hint"]),
            not bool(item["detection_near_hint"]),
            item["anchor"]["association_score"],
            -len(item["track"]),
            item["anchor"]["entry_index"],
            item["anchor"]["fit_index"],
        ),
    )
    anchor = selected_hypothesis["anchor"]
    anchored_profile = selected_hypothesis["anchored_profile"]
    track_slices = selected_hypothesis["track"]
    downward = selected_hypothesis["downward"]
    upward = selected_hypothesis["upward"]

    anchor_summary = {
        key: phase1.json_ready(value)
        for key, value in anchor.items()
        if key != "fit"
    }
    anchor_summary["center"] = phase1.json_ready(anchor["fit"]["center"])
    anchor_summary["radius_m"] = float(anchor["fit"]["radius_m"])
    diagnostics = {
        "click_xy": click.tolist(),
        "clean_height_hint_m": float(clean_height_hint_m),
        "hint_is_automatic_final_pom": False,
        "identity_anchor": anchor_summary,
        "identity_anchor_selection_rule": (
            "PREFER_PHASE1_AUTOMATIC_STABLE_TRACK_CONTAINING_CLEAN_HEIGHT_HINT_THEN_CLICK_ASSOCIATION_SCORE"
            if phase1_config is not None
            else "LOWEST_CLICK_ASSOCIATION_SCORE"
        ),
        "anchor_hypothesis_count": len(candidates),
        "selected_anchor_has_automatic_stable_window_at_hint": bool(
            selected_hypothesis["automatic_near_hint"]
        ),
        "selected_anchor_has_detection_window_at_hint": bool(
            selected_hypothesis["detection_near_hint"]
        ),
        "anchor_hypothesis_summary": [
            {
                "height_m": item["anchor"]["height_m"],
                "fit_index": item["anchor"]["fit_index"],
                "center": phase1.json_ready(item["anchor"]["fit"]["center"]),
                "radius_m": float(item["anchor"]["fit"]["radius_m"]),
                "click_distance_m": item["anchor"]["click_distance_m"],
                "association_score": item["anchor"]["association_score"],
                "track_slice_count": len(item["track"]),
                "automatic_stable_window_at_hint": bool(item["automatic_near_hint"]),
                "detection_window_at_hint": bool(item["detection_near_hint"]),
            }
            for item in candidates
        ],
        "track_slice_count": len(track_slices),
        "track_height_range_m": (
            [track_slices[0]["height_m"], track_slices[-1]["height_m"]]
            if track_slices
            else None
        ),
        "track_slices": track_slices,
        "propagation_decisions": {
            "downward": downward,
            "upward": upward,
        },
    }
    return anchored_profile, diagnostics


def evaluate_manual_anchor(
    *,
    pilot_candidate_id: str,
    manual_seed: dict,
    ground_z_m: float,
    original_profile: list[dict],
    phase1_config: dict,
    pilot_config: dict,
) -> phase1.CandidateEvaluation:
    """Associate identity, then delegate POM/acceptance to locked Phase 1."""
    source_seed = phase1.SeedRecord(
        seed_id=manual_seed["seed_id"],
        source="MANUAL_REVIEW_CLICK",
        source_height_m=float(manual_seed["clean_height_hint_m"]),
        x=float(manual_seed["x"]),
        y=float(manual_seed["y"]),
        source_index=None,
    )
    candidate = {
        "candidate_id": pilot_candidate_id,
        "position": {"x": source_seed.x, "y": source_seed.y},
        "source_seeds": [source_seed],
        "seed_relationships": [
            {
                **source_seed.to_dict(),
                "offset_from_group_m": 0.0,
                "human_label": manual_seed.get("human_label"),
            }
        ],
    }
    anchored_profile, anchor_diagnostics = build_anchored_profile(
        original_profile,
        [source_seed.x, source_seed.y],
        float(manual_seed["clean_height_hint_m"]),
        pilot_config,
        phase1_config,
    )
    evaluation = phase1.evaluate_candidate_profile(
        candidate,
        ground_z_m,
        anchored_profile,
        phase1_config,
    )
    evaluation.algorithm_version = ALGORITHM_VERSION
    evaluation.reason_codes = list(dict.fromkeys([
        "HUMAN_CONFIRMED_MAIN_STEM_IDENTITY",
        "MANUAL_CLICK_COMPONENT_ANCHOR",
        "CLEAN_HEIGHT_USED_AS_ASSOCIATION_HINT_ONLY",
        "ONE_COMPONENT_PER_HEIGHT_TRACK",
        *evaluation.reason_codes,
    ]))
    evaluation.diagnostics["manual_anchor_pilot"] = anchor_diagnostics
    evaluation.diagnostics["all_component_profile"] = phase1.json_ready(original_profile)
    evaluation.diagnostics["locked_measurement_acceptance_version"] = phase1_config["algorithm_version"]
    evaluation.diagnostics["selected_pom_equals_hint"] = bool(
        evaluation.measurement_height_m is not None
        and abs(evaluation.measurement_height_m - float(manual_seed["clean_height_hint_m"])) <= 1e-9
    )
    return evaluation


def select_full_resolution_identity_fit(
    fits: list[dict],
    predicted_center: np.ndarray,
    radius_hint_m: float,
    pilot_config: dict,
) -> dict | None:
    """Select the component nearest the anchored stem prediction.

    Unlike the Phase 1 generic selector, this reviewer-anchored pilot does not
    reward a component merely for having many points. A broad prop-root mass
    can contain thousands more points than the small stem the reviewer marked.
    """
    cfg = pilot_config["full_resolution_component_association"]
    candidates = []
    for fit in fits:
        if not fit.get("valid", True):
            continue
        center_distance = float(
            np.linalg.norm(np.asarray(fit["center"], dtype=float) - predicted_center)
        )
        if center_distance > cfg["maximum_predicted_center_distance_m"]:
            continue
        score = (
            center_distance
            + cfg["radius_absolute_difference_weight"]
            * abs(float(fit["radius_m"]) - radius_hint_m)
            + cfg["circle_residual_weight"] * float(fit.get("circle_residual_m", math.inf))
        )
        candidates.append((score, center_distance, fit))
    return min(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


def refine_candidate_full_resolution_anchored(
    evaluation: phase1.CandidateEvaluation,
    local: np.ndarray,
    phase1_config: dict,
    pilot_config: dict,
    point_output_dir: Path,
) -> phase1.CandidateEvaluation:
    """Run Phase 1 full fitting with identity-constrained component selection."""
    if evaluation.measurement_height_m is None:
        return evaluation
    if len(local) == 0:
        evaluation.measurement_status = "NEEDS_REVIEW"
        evaluation.reason_codes.append("FULL_RESOLUTION_NEIGHBORHOOD_EMPTY")
        return evaluation
    full_cfg = phase1_config["full_resolution"]
    window = evaluation.diagnostics["selected_window"]
    sampled_coefficients = np.asarray(window["centreline_coefficients"], dtype=float)
    radius_hint = float(window["median_radius_m"])
    selected_height = float(evaluation.measurement_height_m)
    neighbour_heights = phase1.heights_inclusive(
        selected_height - full_cfg["neighbouring_half_width_m"],
        selected_height + full_cfg["neighbouring_half_width_m"],
        full_cfg["neighbouring_step_m"],
    )
    rng = np.random.default_rng(
        phase1_config["random_seed"] + int(evaluation.candidate_id.split("-")[1]) * 17
    )
    horizontal = []
    for height in neighbour_heights:
        section_mask = np.abs(
            local[:, 2] - (evaluation.ground_z_m + height)
        ) <= full_cfg["slab_thickness_m"] / 2
        section = local[section_mask]
        predicted = np.asarray(
            [
                sampled_coefficients[0, 0] * height + sampled_coefficients[0, 1],
                sampled_coefficients[1, 0] * height + sampled_coefficients[1, 1],
            ]
        )
        slice_result = phase1.fit_slice_profile(
            section[:, :2], predicted, phase1_config, rng, full_resolution=True
        )
        best = select_full_resolution_identity_fit(
            slice_result["fits"], predicted, radius_hint, pilot_config
        )
        horizontal.append(
            {"height_m": height, "section": section, "slice": slice_result, "fit": best}
        )
    valid_horizontal = [entry for entry in horizontal if entry["fit"] is not None]
    if len(valid_horizontal) < full_cfg["minimum_neighbouring_valid_slices"]:
        evaluation.measurement_status = "NEEDS_REVIEW"
        evaluation.reason_codes.append("FULL_RESOLUTION_CENTRELINE_INSUFFICIENT")
        evaluation.diagnostics["full_resolution_horizontal_slices"] = [
            phase1.compact_full_resolution_slice(entry) for entry in horizontal
        ]
        return evaluation

    heights = np.asarray([entry["height_m"] for entry in valid_horizontal])
    centers = np.asarray([entry["fit"]["center"] for entry in valid_horizontal])
    coefficients, center_residuals = phase1.robust_centreline(
        heights, centers, phase1_config
    )
    axis = np.asarray([coefficients[0, 0], coefficients[1, 0], 1.0])
    axis /= np.linalg.norm(axis)
    reference = (
        np.asarray([0.0, 1.0, 0.0])
        if abs(axis[1]) < 0.9
        else np.asarray([1.0, 0.0, 0.0])
    )
    basis_u = np.cross(axis, reference)
    basis_u /= np.linalg.norm(basis_u)
    basis_v = np.cross(axis, basis_u)

    perpendicular = []
    for entry in valid_horizontal:
        height = entry["height_m"]
        line_point = np.asarray(
            [
                coefficients[0, 0] * height + coefficients[0, 1],
                coefficients[1, 0] * height + coefficients[1, 1],
                evaluation.ground_z_m + height,
            ]
        )
        relative = local - line_point
        axial = relative @ axis
        plane_xy = np.column_stack((relative @ basis_u, relative @ basis_v))
        radial = np.linalg.norm(plane_xy, axis=1)
        plane_mask = (
            np.abs(axial) <= full_cfg["slab_thickness_m"] / 2
        ) & (radial <= full_cfg["extraction_radius_maximum_m"])
        plane_points = local[plane_mask]
        plane_section = plane_xy[plane_mask]
        slice_result = phase1.fit_slice_profile(
            plane_section,
            np.zeros(2),
            phase1_config,
            rng,
            full_resolution=True,
        )
        best = select_full_resolution_identity_fit(
            slice_result["fits"], np.zeros(2), radius_hint, pilot_config
        )
        perpendicular.append(
            {
                "height_m": height,
                "line_point": line_point,
                "plane_points": plane_points,
                "plane_xy": plane_section,
                "slice": slice_result,
                "fit": best,
            }
        )
    selected = next(
        (
            entry for entry in perpendicular
            if abs(entry["height_m"] - selected_height) <= 1e-6
            and entry["fit"] is not None
        ),
        None,
    )
    valid_perpendicular = [entry for entry in perpendicular if entry["fit"] is not None]
    if selected is None or len(valid_perpendicular) < full_cfg["minimum_neighbouring_valid_slices"]:
        evaluation.measurement_status = "NEEDS_REVIEW"
        evaluation.reason_codes.append("FULL_RESOLUTION_SELECTED_SLICE_UNSTABLE")
        evaluation.diagnostics["full_resolution_perpendicular_slices"] = [
            phase1.compact_full_resolution_slice(entry) for entry in perpendicular
        ]
        return evaluation

    fit = selected["fit"]
    ellipse = fit["ellipse"]
    circle_residual = fit["circle_residual_m"]
    use_ellipse = bool(
        ellipse.get("valid")
        and ellipse["ellipse_residual_m"]
        < circle_residual * full_cfg["ellipse_selection_residual_ratio"]
    )
    if use_ellipse:
        equivalent_diameter_m = 2 * math.sqrt(
            ellipse["semi_major_axis_m"] * ellipse["semi_minor_axis_m"]
        )
        selected_model = "ELLIPSE"
    else:
        equivalent_diameter_m = 2 * fit["radius_m"]
        selected_model = "CIRCLE"
    neighbour_diameters = np.asarray(
        [2 * entry["fit"]["radius_m"] for entry in valid_perpendicular]
    )
    uncertainty_m = max(
        0.005,
        1.4826 * phase1.mad(neighbour_diameters),
        2 * fit["circle_residual_m"],
    )
    evaluation.selected_model = selected_model
    evaluation.equivalent_diameter_cm = phase1.rounded(equivalent_diameter_m * 100, 2)
    evaluation.diameter_uncertainty_cm = phase1.rounded(uncertainty_m * 100, 2)
    evaluation.circular_equivalent_girth_cm = phase1.rounded(
        math.pi * equivalent_diameter_m * 100, 2
    )
    if ellipse.get("valid"):
        evaluation.ellipse_major_axis_cm = phase1.rounded(
            2 * ellipse["semi_major_axis_m"] * 100, 2
        )
        evaluation.ellipse_minor_axis_cm = phase1.rounded(
            2 * ellipse["semi_minor_axis_m"] * 100, 2
        )
        perimeter_m = phase1.ellipse_perimeter(
            ellipse["semi_major_axis_m"], ellipse["semi_minor_axis_m"]
        )
        evaluation.ellipse_perimeter_cm = phase1.rounded(perimeter_m * 100, 2)
    evaluation.observed_contour_girth_cm = (
        phase1.rounded(
            evaluation.ellipse_perimeter_cm
            if use_ellipse and evaluation.ellipse_perimeter_cm is not None
            else 2 * math.pi * fit["radius_m"] * 100,
            2,
        )
        if fit["angular_coverage_deg"] >= full_cfg["observed_contour_minimum_coverage_deg"]
        else None
    )
    evaluation.angular_coverage_deg = phase1.rounded(fit["angular_coverage_deg"], 2)
    evaluation.centreline_residual_p90_m = phase1.rounded(
        float(np.percentile(center_residuals, 90))
    )
    evaluation.supporting_slice_count = len(valid_perpendicular)
    evaluation.measurement_confidence = phase1.rounded(
        max(
            0.0,
            min(
                0.99,
                0.30
                + 0.08 * len(valid_perpendicular)
                + 0.30 * fit["angular_coverage_deg"] / 360.0
                - 4.0 * fit["circle_residual_m"],
            ),
        )
    )
    evaluation.reason_codes.extend(
        [
            "FULL_RESOLUTION_MANUAL_IDENTITY_COMPONENT_ASSOCIATION",
            "FULL_RESOLUTION_MEASUREMENT_ACCEPTED",
        ]
    )

    radial_error = np.abs(
        np.linalg.norm(selected["plane_xy"] - np.asarray(fit["center"]), axis=1)
        - fit["radius_m"]
    )
    accepted_mask = radial_error <= fit["inlier_tolerance_m"]
    point_output_dir.mkdir(parents=True, exist_ok=True)
    point_path = point_output_dir / f"{evaluation.candidate_id}.npz"
    np.savez_compressed(
        point_path,
        accepted_points_xyz=selected["plane_points"][accepted_mask].astype(np.float32),
        rejected_points_xyz=selected["plane_points"][~accepted_mask].astype(np.float32),
    )
    evaluation.full_resolution_point_file = str(point_path)
    evaluation.diagnostics["full_resolution"] = {
        "component_selection_rule": "NEAREST_TO_MANUAL_ANCHORED_CENTRELINE_WITHOUT_POINT_COUNT_REWARD",
        "centreline_axis": phase1.json_ready(axis),
        "centreline_coefficients": phase1.json_ready(coefficients),
        "horizontal_slice_results": [
            phase1.compact_full_resolution_slice(entry) for entry in horizontal
        ],
        "perpendicular_slice_results": [
            phase1.compact_full_resolution_slice(entry) for entry in perpendicular
        ],
        "selected_height_m": selected_height,
        "accepted_point_count": int(accepted_mask.sum()),
        "rejected_point_count": int((~accepted_mask).sum()),
        "circle_model": phase1.json_ready(fit),
        "ellipse_model": phase1.json_ready(ellipse),
    }
    evaluation.reason_codes = list(dict.fromkeys(evaluation.reason_codes))
    return evaluation
