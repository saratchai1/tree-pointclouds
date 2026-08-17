#!/usr/bin/env python3
"""Constrained vertical stem-track association for V2 Phase 1.5."""

from __future__ import annotations

from collections import Counter, defaultdict
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from scipy.optimize import linear_sum_assignment
from scipy.spatial import cKDTree

import stem_inventory_v2 as phase1


ALGORITHM_VERSION = "stem-inventory-v2-phase1_5"
GEOMETRY_STATUSES = {
    "STEM_LIKE",
    "WEAK_GEOMETRY",
    "AMBIGUOUS_MULTI_COMPONENT",
    "INSUFFICIENT_EVIDENCE",
    "GEOMETRY_REJECTED",
}
IDENTITY_STATUSES = {
    "UNVERIFIED",
    "AUTO_HIGH_SUPPORT",
    "HUMAN_CONFIRMED",
    "HUMAN_REJECTED",
    "DUPLICATE_ALIAS",
}
MEASUREMENT_STATUSES = {
    "MEASURABLE_STANDARD_1_30",
    "MEASURABLE_ADAPTIVE_HEIGHT",
    "NEEDS_REVIEW",
    "INSUFFICIENT_COVERAGE",
    "MEASUREMENT_REJECTED",
}
HUMAN_LABELS = {
    "TRUE_MAIN_STEM",
    "PROP_ROOT_OR_ROOT_ONLY",
    "BRANCH",
    "OTHER_VEGETATION",
    "DUPLICATE_OF",
    "NOT_ENOUGH_INFORMATION",
    "MANUAL_REVIEW_REQUIRED",
}


def load_config(path: Path) -> dict:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if config.get("algorithm_version") != ALGORITHM_VERSION:
        raise ValueError("Unexpected Phase 1.5 configuration version")
    return config


def json_ready(value: Any):
    return phase1.json_ready(value)


def relative_difference(left: float | None, right: float | None, floor: float = 0.02) -> float:
    if left is None or right is None or not math.isfinite(left) or not math.isfinite(right):
        return 1.0
    return abs(left - right) / max((abs(left) + abs(right)) / 2, floor)


def ellipse_axis_ratio(fit: dict | None) -> float | None:
    if fit is None:
        return None
    ellipse = fit.get("ellipse", {})
    if not ellipse.get("valid"):
        return None
    major = ellipse.get("semi_major_axis_m")
    minor = ellipse.get("semi_minor_axis_m")
    if not major or not minor:
        return None
    return float(major / max(minor, 1e-9))


def local_evidence_quality(fit: dict | None, point_count: int) -> float:
    if fit is None or not fit.get("valid"):
        return 0.0
    coverage = min(1.0, fit.get("angular_coverage_deg", 0.0) / 240.0)
    residual = math.exp(-fit.get("circle_residual_m", 1.0) / 0.03)
    support = min(1.0, math.log1p(fit.get("inlier_count", 0)) / math.log(201))
    density_support = min(1.0, math.log1p(point_count) / math.log(2001))
    return float(0.35 * coverage + 0.30 * residual + 0.20 * support + 0.15 * density_support)


def extract_seed_observations(candidates: list[dict]) -> list[dict]:
    observations = []
    for candidate in candidates:
        profile = candidate["diagnostics"]["profile"]
        for relationship in candidate["seed_relationships"]:
            height = relationship.get("source_height_m")
            if height is None:
                observations.append(
                    {
                        "node_id": f"OBS-{relationship['seed_id']}",
                        "source_seed_id": relationship["seed_id"],
                        "source_seed_ids": [relationship["seed_id"]],
                        "source_provider": relationship["source"],
                        "source_providers": [relationship["source"]],
                        "source_height_m": None,
                        "source_xy": [relationship["x"], relationship["y"]],
                        "center": [relationship["x"], relationship["y"]],
                        "phase1_candidate_id": candidate["candidate_id"],
                        "phase1_candidate_ids": [candidate["candidate_id"]],
                        "fit_validity": False,
                        "rejection_reasons": ["SOURCE_HEIGHT_MISSING"],
                        "alias_observation_ids": [],
                        "alternative_fits": [],
                    }
                )
                continue
            entry = min(profile, key=lambda item: abs(item["height_m"] - float(height)))
            source_xy = np.asarray([relationship["x"], relationship["y"]])
            fits = entry.get("fits", [])
            ranked = sorted(
                fits,
                key=lambda fit: (
                    np.linalg.norm(np.asarray(fit["center"]) - source_xy)
                    + 2.0 * fit.get("circle_residual_m", 0.1)
                    - 0.001 * fit.get("angular_coverage_deg", 0.0)
                ),
            )
            selected = ranked[0] if ranked else None
            center = selected["center"] if selected is not None else source_xy.tolist()
            observation = {
                "node_id": f"OBS-{relationship['seed_id']}",
                "source_seed_id": relationship["seed_id"],
                "source_seed_ids": [relationship["seed_id"]],
                "source_provider": relationship["source"],
                "source_providers": [relationship["source"]],
                "source_height_m": float(height),
                "profile_height_m": entry["height_m"],
                "source_xy": source_xy.tolist(),
                "center": json_ready(center),
                "radius_m": selected.get("radius_m") if selected else None,
                "circle_residual_m": selected.get("circle_residual_m") if selected else None,
                "ellipse_residual_m": (
                    selected.get("ellipse", {}).get("ellipse_residual_m") if selected else None
                ),
                "ellipse_axis_ratio": ellipse_axis_ratio(selected),
                "angular_coverage_deg": selected.get("angular_coverage_deg") if selected else 0.0,
                "point_count": entry.get("point_count", 0),
                "inlier_count": selected.get("inlier_count", 0) if selected else 0,
                "component_id": selected.get("component_index") if selected else None,
                "connected_component_count": entry.get("connected_component_count", 0),
                "fit_validity": bool(selected and selected.get("valid")),
                "local_evidence_quality": local_evidence_quality(selected, entry.get("point_count", 0)),
                "phase1_candidate_id": candidate["candidate_id"],
                "phase1_candidate_ids": [candidate["candidate_id"]],
                "rejection_reasons": [] if selected else list(entry.get("rejection_reasons", [])),
                "alias_observation_ids": [],
                "alternative_fits": [
                    {
                        "component_id": fit.get("component_index"),
                        "center": fit.get("center"),
                        "radius_m": fit.get("radius_m"),
                        "circle_residual_m": fit.get("circle_residual_m"),
                        "angular_coverage_deg": fit.get("angular_coverage_deg"),
                    }
                    for fit in ranked[1:3]
                ],
            }
            observations.append(json_ready(observation))
    return observations


def collapse_duplicate_observations(observations: list[dict], config: dict) -> tuple[list[dict], dict[str, str]]:
    cfg = config["observation_aliasing"]
    valid = [item for item in observations if item.get("source_height_m") is not None]
    invalid = [item for item in observations if item.get("source_height_m") is None]
    by_height: dict[float, list[dict]] = defaultdict(list)
    for item in valid:
        by_height[round(float(item["source_height_m"]), 3)].append(item)
    collapsed = []
    trace = {}
    for height in sorted(by_height):
        level = sorted(
            by_height[height],
            key=lambda item: (-item.get("local_evidence_quality", 0.0), item["source_seed_id"]),
        )
        anchors: list[dict] = []
        for observation in level:
            center = np.asarray(observation["center"])
            compatible = []
            for anchor in anchors:
                distance = np.linalg.norm(center - np.asarray(anchor["center"]))
                radius_delta = relative_difference(observation.get("radius_m"), anchor.get("radius_m"))
                if (
                    distance <= cfg["maximum_xy_distance_m"]
                    and radius_delta <= cfg["maximum_radius_relative_difference"]
                ):
                    compatible.append((distance + 0.1 * radius_delta, anchor))
            if not compatible:
                canonical = dict(observation)
                canonical["alias_observation_ids"] = []
                anchors.append(canonical)
                collapsed.append(canonical)
                trace[observation["source_seed_id"]] = canonical["node_id"]
                continue
            _, canonical = min(compatible, key=lambda item: (item[0], item[1]["node_id"]))
            canonical["alias_observation_ids"].append(observation["node_id"])
            canonical["source_seed_ids"].extend(observation["source_seed_ids"])
            canonical["source_providers"] = sorted(
                set(canonical["source_providers"]) | set(observation["source_providers"])
            )
            canonical["phase1_candidate_ids"] = sorted(
                set(canonical["phase1_candidate_ids"]) | set(observation["phase1_candidate_ids"])
            )
            for seed_id in observation["source_seed_ids"]:
                trace[seed_id] = canonical["node_id"]
    for observation in invalid:
        collapsed.append(observation)
        trace[observation["source_seed_id"]] = observation["node_id"]
    collapsed.sort(
        key=lambda item: (
            item.get("source_height_m") if item.get("source_height_m") is not None else math.inf,
            item["node_id"],
        )
    )
    return collapsed, trace


def fit_track_model(nodes: list[dict], config: dict) -> dict:
    valid = [item for item in nodes if item.get("fit_validity") and item.get("source_height_m") is not None]
    if not valid:
        return {
            "valid_node_count": 0,
            "centreline_coefficients": None,
            "radius_coefficients": None,
            "centre_residuals_m": [],
            "radius_residuals_m": [],
            "centre_residual_p90_m": None,
            "radius_residual_mad_m": None,
            "median_radius_m": None,
            "median_angular_coverage_deg": 0.0,
            "median_fit_residual_m": None,
            "score": 0.0,
        }
    heights = np.asarray([item["source_height_m"] for item in valid])
    centers = np.asarray([item["center"] for item in valid])
    radii = np.asarray([item["radius_m"] for item in valid])
    if len(valid) >= 2:
        robust_config = {
            "tracking": {
                "huber_delta_m": config["track_refinement"]["huber_delta_m"],
                "robust_iterations": config["track_refinement"]["robust_iterations"],
            }
        }
        centreline, centre_residuals = phase1.robust_centreline(heights, centers, robust_config)
        radius_coefficients, radius_residuals = phase1.robust_scalar_line(
            heights,
            radii,
            config["track_refinement"]["robust_iterations"],
            config["track_refinement"]["huber_delta_m"],
        )
    else:
        centreline = np.asarray([[0.0, centers[0, 0]], [0.0, centers[0, 1]]])
        radius_coefficients = np.asarray([0.0, radii[0]])
        centre_residuals = np.zeros(1)
        radius_residuals = np.zeros(1)
    coverage = np.asarray([item.get("angular_coverage_deg", 0.0) for item in valid])
    fit_residuals = np.asarray(
        [item.get("circle_residual_m") if item.get("circle_residual_m") is not None else 1.0 for item in valid]
    )
    quality = np.asarray([item.get("local_evidence_quality", 0.0) for item in valid])
    vertical_span = float(np.max(heights) - np.min(heights)) if len(heights) > 1 else 0.0
    score = (
        2.0 * len(valid)
        + 1.2 * vertical_span
        + float(np.mean(quality))
        - 5.0 * float(np.percentile(centre_residuals, 90))
        - 5.0 * phase1.mad(radius_residuals)
    )
    return {
        "valid_node_count": len(valid),
        "centreline_coefficients": centreline,
        "radius_coefficients": radius_coefficients,
        "centre_residuals_m": centre_residuals,
        "radius_residuals_m": radius_residuals,
        "centre_residual_p90_m": float(np.percentile(centre_residuals, 90)),
        "radius_residual_mad_m": phase1.mad(radius_residuals),
        "median_radius_m": float(np.median(radii)),
        "median_angular_coverage_deg": float(np.median(coverage)),
        "median_fit_residual_m": float(np.median(fit_residuals)),
        "vertical_span_m": vertical_span,
        "mean_local_evidence_quality": float(np.mean(quality)),
        "score": float(score),
    }


def predict_track_center(model: dict, height: float) -> np.ndarray:
    coefficients = np.asarray(model["centreline_coefficients"])
    return np.asarray(
        [
            coefficients[0, 0] * height + coefficients[0, 1],
            coefficients[1, 0] * height + coefficients[1, 1],
        ]
    )


def predict_track_radius(model: dict, height: float) -> float | None:
    coefficients = model.get("radius_coefficients")
    if coefficients is None:
        return None
    return float(coefficients[0] * height + coefficients[1])


def association_cost(
    track: dict,
    observation: dict,
    config: dict,
    model: dict | None = None,
) -> tuple[float, dict]:
    cfg = config["track_association"]
    nodes = track["nodes"]
    model = model if model is not None else fit_track_model(nodes, config)
    height = float(observation["source_height_m"])
    last_height = max(float(item["source_height_m"]) for item in nodes)
    height_gap = height - last_height
    if height_gap <= 0 or height_gap > cfg["maximum_height_gap_m"] + 1e-9:
        return math.inf, {"reason": "HEIGHT_GAP_INCOMPATIBLE"}
    predicted = predict_track_center(model, height)
    center_distance = float(np.linalg.norm(np.asarray(observation["center"]) - predicted))
    predicted_radius = predict_track_radius(model, height)
    radius_reference = max(
        observation.get("radius_m") or 0.02,
        predicted_radius or 0.02,
        0.02,
    )
    center_gate = min(
        cfg["maximum_center_gate_m"],
        max(
            cfg["base_center_gate_m"],
            cfg["radius_center_gate_multiplier"] * radius_reference,
        )
        + cfg["maximum_lean_m_per_m"] * height_gap,
    )
    radius_change = relative_difference(predicted_radius, observation.get("radius_m"))
    if center_distance > center_gate:
        return math.inf, {"reason": "CENTER_GATE_EXCEEDED", "center_distance_m": center_distance, "center_gate_m": center_gate}
    if radius_change > cfg["maximum_radius_relative_change"]:
        return math.inf, {"reason": "RADIUS_CHANGE_EXCEEDED", "radius_relative_change": radius_change}
    previous_axis_ratio = np.median(
        [item["ellipse_axis_ratio"] for item in nodes if item.get("ellipse_axis_ratio") is not None]
    ) if any(item.get("ellipse_axis_ratio") is not None for item in nodes) else None
    component_difference = relative_difference(previous_axis_ratio, observation.get("ellipse_axis_ratio"), 1.0)
    weights = cfg["weights"]
    fit_residual = observation.get("circle_residual_m") or 0.10
    coverage_penalty = 1.0 - min(1.0, observation.get("angular_coverage_deg", 0.0) / 180.0)
    cost = (
        weights["height_gap"] * height_gap / cfg["maximum_height_gap_m"]
        + weights["center_displacement"] * center_distance / max(center_gate, 1e-9)
        + weights["radius_change"] * radius_change
        + weights["fit_residual"] * min(2.0, fit_residual / 0.03)
        + weights["angular_coverage"] * coverage_penalty
        + weights["component_compatibility"] * min(1.0, component_difference)
    )
    return float(cost), {
        "height_gap_m": height_gap,
        "predicted_center": predicted.tolist(),
        "center_distance_m": center_distance,
        "center_gate_m": center_gate,
        "predicted_radius_m": predicted_radius,
        "radius_relative_change": radius_change,
        "fit_residual_m": fit_residual,
        "angular_coverage_deg": observation.get("angular_coverage_deg", 0.0),
        "component_compatibility_penalty": component_difference,
    }


def cost_to_track_model(track: dict, observation: dict, config: dict) -> tuple[float, dict]:
    model = fit_track_model(track["nodes"], config)
    if model["valid_node_count"] == 0:
        return math.inf, {"reason": "TRACK_HAS_NO_VALID_MODEL"}
    height = float(observation["source_height_m"])
    existing_heights = {round(float(item["source_height_m"]), 6) for item in track["nodes"]}
    if round(height, 6) in existing_heights:
        return math.inf, {"reason": "TRACK_ALREADY_HAS_HEIGHT"}
    minimum_height = min(existing_heights)
    maximum_height = max(existing_heights)
    gap = max(minimum_height - height, height - maximum_height, 0.0)
    if gap > config["track_association"]["maximum_height_gap_m"]:
        return math.inf, {"reason": "TRACK_EXTENSION_GAP_EXCEEDED"}
    predicted = predict_track_center(model, height)
    predicted_radius = predict_track_radius(model, height)
    radius_reference = max(predicted_radius or 0.02, observation.get("radius_m") or 0.02, 0.02)
    cfg = config["track_association"]
    center_gate = min(
        cfg["maximum_center_gate_m"],
        max(cfg["base_center_gate_m"], cfg["radius_center_gate_multiplier"] * radius_reference)
        + cfg["maximum_lean_m_per_m"] * gap,
    )
    center_distance = float(np.linalg.norm(np.asarray(observation["center"]) - predicted))
    radius_change = relative_difference(predicted_radius, observation.get("radius_m"))
    if center_distance > center_gate or radius_change > cfg["maximum_radius_relative_change"]:
        return math.inf, {"reason": "MODEL_COMPATIBILITY_GATE_FAILED"}
    weights = cfg["weights"]
    fit_residual = observation.get("circle_residual_m") or 0.10
    coverage_penalty = 1.0 - min(1.0, observation.get("angular_coverage_deg", 0.0) / 180.0)
    cost = (
        weights["height_gap"] * gap / cfg["maximum_height_gap_m"]
        + weights["center_displacement"] * center_distance / max(center_gate, 1e-9)
        + weights["radius_change"] * radius_change
        + weights["fit_residual"] * min(2.0, fit_residual / 0.03)
        + weights["angular_coverage"] * coverage_penalty
    )
    return float(cost), {
        "predicted_center": predicted.tolist(),
        "center_distance_m": center_distance,
        "center_gate_m": center_gate,
        "predicted_radius_m": predicted_radius,
        "radius_relative_change": radius_change,
        "height_extension_gap_m": gap,
    }


def initial_hungarian_tracks(observations: list[dict], config: dict) -> tuple[list[dict], list[dict]]:
    valid = [item for item in observations if item.get("fit_validity") and item.get("source_height_m") is not None]
    unassigned = [
        {
            "observation": item,
            "reason": "FIT_INVALID_OR_SOURCE_HEIGHT_MISSING",
            "alternative_associations": [],
        }
        for item in observations
        if item not in valid
    ]
    by_height: dict[float, list[dict]] = defaultdict(list)
    for item in valid:
        by_height[round(float(item["source_height_m"]), 3)].append(item)
    tracks: list[dict] = []
    next_internal_id = 1
    for height in sorted(by_height):
        level = sorted(by_height[height], key=lambda item: item["node_id"])
        active = [
            track
            for track in tracks
            if 0 < height - max(float(node["source_height_m"]) for node in track["nodes"])
            <= config["track_association"]["maximum_height_gap_m"] + 1e-9
        ]
        assigned_observations = set()
        if active:
            active_models = [fit_track_model(track["nodes"], config) for track in active]
            costs = np.full((len(active), len(level)), 1e6, dtype=float)
            details: dict[tuple[int, int], dict] = {}
            for row, track in enumerate(active):
                for column, observation in enumerate(level):
                    cost, detail = association_cost(
                        track, observation, config, model=active_models[row]
                    )
                    details[(row, column)] = detail
                    if math.isfinite(cost):
                        costs[row, column] = cost
            rows, columns = linear_sum_assignment(costs)
            for row, column in zip(rows, columns):
                cost = float(costs[row, column])
                if cost > config["track_association"]["maximum_edge_cost"]:
                    continue
                observation = level[column]
                alternatives = sorted(
                    (
                        {
                            "track_internal_id": active[other_row]["internal_id"],
                            "cost": float(costs[other_row, column]),
                        }
                        for other_row in range(len(active))
                        if costs[other_row, column] <= config["track_association"]["maximum_edge_cost"]
                    ),
                    key=lambda item: (item["cost"], item["track_internal_id"]),
                )[:3]
                active[row]["nodes"].append(observation)
                active[row]["edges"].append(
                    {
                        "from_node_id": active[row]["nodes"][-2]["node_id"],
                        "to_node_id": observation["node_id"],
                        "cost": cost,
                        "criteria": details[(row, column)],
                        "alternative_associations": alternatives,
                    }
                )
                assigned_observations.add(column)
        for column, observation in enumerate(level):
            if column in assigned_observations:
                continue
            tracks.append(
                {
                    "internal_id": next_internal_id,
                    "nodes": [observation],
                    "edges": [],
                    "rejected_observations": [],
                    "refinement_iterations": 0,
                }
            )
            next_internal_id += 1
    return tracks, unassigned


def refine_tracks(tracks: list[dict], unassigned: list[dict], config: dict) -> tuple[list[dict], list[dict]]:
    refine_cfg = config["track_refinement"]

    def normalized_fit_cost(model: dict, median_radius: float) -> float:
        """Compare model fit quality without rewarding shorter tracks."""
        center_limit = max(
            refine_cfg["center_outlier_base_m"],
            refine_cfg["center_outlier_radius_fraction"] * median_radius,
        )
        radius_limit = max(
            refine_cfg["radius_outlier_base_m"],
            refine_cfg["radius_outlier_radius_fraction"] * median_radius,
        )
        return float(
            model["centre_residual_p90_m"] / max(center_limit, 1e-9)
            + model["radius_residual_mad_m"] / max(radius_limit, 1e-9)
        )

    for iteration in range(1, refine_cfg["maximum_iterations"] + 1):
        changed = False
        newly_unassigned = []
        for track in tracks:
            if len(track["nodes"]) < 4:
                continue
            model = fit_track_model(track["nodes"], config)
            radii = np.asarray([item["radius_m"] for item in track["nodes"]])
            median_radius = max(float(np.median(radii)), 0.02)
            center_limit = max(
                refine_cfg["center_outlier_base_m"],
                refine_cfg["center_outlier_radius_fraction"] * median_radius,
            )
            radius_limit = max(
                refine_cfg["radius_outlier_base_m"],
                refine_cfg["radius_outlier_radius_fraction"] * median_radius,
            )
            candidates = []
            for index, observation in enumerate(track["nodes"]):
                center_residual = float(model["centre_residuals_m"][index])
                radius_residual = abs(float(model["radius_residuals_m"][index]))
                if center_residual > center_limit or radius_residual > radius_limit:
                    severity = max(center_residual / center_limit, radius_residual / radius_limit)
                    candidates.append((severity, index, center_residual, radius_residual))
            if not candidates:
                continue
            _, index, center_residual, radius_residual = max(candidates)
            reduced = track["nodes"][:index] + track["nodes"][index + 1 :]
            if len(reduced) < 2:
                continue
            reduced_model = fit_track_model(reduced, config)
            old_cost = normalized_fit_cost(model, median_radius)
            reduced_radius = max(float(np.median([item["radius_m"] for item in reduced])), 0.02)
            new_cost = normalized_fit_cost(reduced_model, reduced_radius)
            if old_cost - new_cost < refine_cfg["minimum_cost_improvement"]:
                continue
            observation = track["nodes"].pop(index)
            record = {
                "observation": observation,
                "reason": "REJECTED_BY_ROBUST_TRACK_REFINEMENT",
                "centre_residual_m": center_residual,
                "radius_residual_m": radius_residual,
                "fit_cost_before": old_cost,
                "fit_cost_after": new_cost,
                "fit_cost_improvement": old_cost - new_cost,
                "from_track_internal_id": track["internal_id"],
                "alternative_associations": [],
            }
            track["rejected_observations"].append(record)
            newly_unassigned.append(record)
            changed = True
        unassigned.extend(newly_unassigned)

        still_unassigned = []
        occupied = {
            (track["internal_id"], round(float(node["source_height_m"]), 6))
            for track in tracks
            for node in track["nodes"]
        }
        for record in unassigned:
            observation = record["observation"]
            if not observation.get("fit_validity") or observation.get("source_height_m") is None:
                still_unassigned.append(record)
                continue
            options = []
            height_key = round(float(observation["source_height_m"]), 6)
            for track in tracks:
                if (track["internal_id"], height_key) in occupied:
                    continue
                cost, detail = cost_to_track_model(track, observation, config)
                if math.isfinite(cost) and cost <= config["track_association"]["maximum_edge_cost"]:
                    options.append((cost, track, detail))
            options.sort(key=lambda item: (item[0], item[1]["internal_id"]))
            record["alternative_associations"] = [
                {"track_internal_id": item[1]["internal_id"], "cost": item[0], "criteria": item[2]}
                for item in options[:3]
            ]
            if not options or config["track_association"]["unassigned_penalty"] - options[0][0] < refine_cfg["minimum_cost_improvement"]:
                still_unassigned.append(record)
                continue
            cost, track, detail = options[0]
            track["nodes"].append(observation)
            track["nodes"].sort(key=lambda item: (item["source_height_m"], item["node_id"]))
            track["edges"].append(
                {
                    "from_node_id": "UNASSIGNED",
                    "to_node_id": observation["node_id"],
                    "cost": cost,
                    "criteria": detail,
                    "association_type": "ITERATIVE_REASSIGNMENT",
                }
            )
            occupied.add((track["internal_id"], height_key))
            changed = True
        unassigned = still_unassigned
        for track in tracks:
            track["refinement_iterations"] = iteration
        if not changed:
            break
    tracks = [track for track in tracks if track["nodes"]]
    return tracks, unassigned


def geometry_status(track: dict, model: dict, config: dict) -> tuple[str, list[str]]:
    cfg = config["geometry_status"]
    nodes = track["nodes"]
    distinct_heights = len({round(float(item["source_height_m"]), 6) for item in nodes})
    if model["valid_node_count"] == 0:
        return "GEOMETRY_REJECTED", ["NO_VALID_FITTED_OBSERVATIONS"]
    multi_component_fraction = sum(item.get("connected_component_count", 0) > 1 for item in nodes) / len(nodes)
    radius = model["median_radius_m"] or 0.02
    center_limit = max(
        cfg["stem_like_center_residual_base_m"],
        cfg["stem_like_center_residual_radius_fraction"] * radius,
    )
    radius_limit = max(
        cfg["stem_like_radius_residual_base_m"],
        cfg["stem_like_radius_residual_radius_fraction"] * radius,
    )
    stem_like = (
        distinct_heights >= cfg["stem_like_minimum_height_levels"]
        and model["vertical_span_m"] >= cfg["stem_like_minimum_vertical_span_m"]
        and model["median_angular_coverage_deg"] >= cfg["stem_like_minimum_median_coverage_deg"]
        and model["centre_residual_p90_m"] <= center_limit
        and model["radius_residual_mad_m"] <= radius_limit
    )
    if stem_like:
        return "STEM_LIKE", ["CONSTRAINED_VERTICAL_TRACK_STABLE"]
    if multi_component_fraction >= cfg["ambiguous_multi_component_fraction"] and distinct_heights >= 2:
        return "AMBIGUOUS_MULTI_COMPONENT", ["MULTI_COMPONENT_EVIDENCE_DOMINANT"]
    if distinct_heights >= cfg["weak_minimum_height_levels"]:
        return "WEAK_GEOMETRY", ["MULTI_HEIGHT_TRACK_BELOW_STEM_LIKE_CRITERIA"]
    return "INSUFFICIENT_EVIDENCE", ["SINGLE_HEIGHT_OR_SHORT_TRACK"]


def phase1_measurement_for_track(track: dict, candidate_by_id: dict[str, dict]) -> tuple[str, dict | None, list[dict]]:
    source_candidates = sorted(
        {candidate_id for node in track["nodes"] for candidate_id in node["phase1_candidate_ids"]}
    )
    candidates = [candidate_by_id[candidate_id] for candidate_id in source_candidates]
    measurable = [item for item in candidates if item["measurement_status"].startswith("MEASURABLE_")]
    if measurable:
        selected = max(
            measurable,
            key=lambda item: (
                item.get("measurement_confidence") or 0.0,
                item.get("supporting_slice_count") or 0,
                -int(item["candidate_id"].split("-")[1]),
            ),
        )
        return selected["measurement_status"], selected, candidates
    if any(item["measurement_status"] == "NEEDS_REVIEW" for item in candidates):
        return "NEEDS_REVIEW", None, candidates
    if any(item["measurement_status"] == "INSUFFICIENT_COVERAGE" for item in candidates):
        return "INSUFFICIENT_COVERAGE", None, candidates
    return "MEASUREMENT_REJECTED", None, candidates


def finalize_tracks(tracks: list[dict], candidate_by_id: dict[str, dict], config: dict) -> list[dict]:
    enriched = []
    for track in tracks:
        track["nodes"].sort(key=lambda item: (item["source_height_m"], item["node_id"]))
        model = fit_track_model(track["nodes"], config)
        status, reasons = geometry_status(track, model, config)
        measurement_status, selected_measurement, source_candidates = phase1_measurement_for_track(
            track, candidate_by_id
        )
        heights = sorted({float(item["source_height_m"]) for item in track["nodes"]})
        gaps = [
            {
                "from_height_m": left,
                "to_height_m": right,
                "gap_m": right - left,
                "missing_nominal_0_25m_levels": max(0, int(round((right - left) / 0.25)) - 1),
            }
            for left, right in zip(heights[:-1], heights[1:])
            if right - left > 0.251
        ]
        source_candidate_ids = sorted(
            {candidate_id for node in track["nodes"] for candidate_id in node["phase1_candidate_ids"]}
        )
        has_high_support = (
            status == "STEM_LIKE"
            and len(heights) >= config["geometry_status"]["auto_high_support_minimum_height_levels"]
            and model["vertical_span_m"] >= config["geometry_status"]["auto_high_support_minimum_vertical_span_m"]
        )
        reference_height = 1.30 if heights[0] <= 1.30 <= heights[-1] else float(np.median(heights))
        reference_center = predict_track_center(model, reference_height) if model["centreline_coefficients"] is not None else np.asarray(track["nodes"][0]["center"])
        enriched.append(
            {
                "internal_id": track["internal_id"],
                "algorithm_version": ALGORITHM_VERSION,
                "track_id": None,
                "candidate_geometry_status": status,
                "identity_status": "AUTO_HIGH_SUPPORT" if has_high_support else "UNVERIFIED",
                "measurement_status": measurement_status,
                "reason_codes": reasons,
                "canonical_measurement_candidate_id": selected_measurement["candidate_id"] if selected_measurement else None,
                "measurement_rule": selected_measurement.get("measurement_rule") if selected_measurement else None,
                "measurement_height_m": selected_measurement.get("measurement_height_m") if selected_measurement else None,
                "equivalent_diameter_cm": selected_measurement.get("equivalent_diameter_cm") if selected_measurement else None,
                "phase1_measurement_candidate_ids": [item["candidate_id"] for item in source_candidates if item["measurement_status"].startswith("MEASURABLE_")],
                "source_candidate_ids": source_candidate_ids,
                "source_seed_ids": sorted({seed for node in track["nodes"] for seed in node["source_seed_ids"]}),
                "source_providers": sorted({provider for node in track["nodes"] for provider in node["source_providers"]}),
                "source_height_count": len(heights),
                "source_heights_m": heights,
                "vertical_span_m": model.get("vertical_span_m", 0.0),
                "gaps": gaps,
                "reference_height_m": reference_height,
                "reference_center": reference_center.tolist(),
                "centreline_coefficients": json_ready(model["centreline_coefficients"]),
                "radius_coefficients": json_ready(model["radius_coefficients"]),
                "centre_residual_p90_m": model["centre_residual_p90_m"],
                "radius_residual_mad_m": model["radius_residual_mad_m"],
                "median_radius_m": model["median_radius_m"],
                "median_angular_coverage_deg": model["median_angular_coverage_deg"],
                "median_fit_residual_m": model["median_fit_residual_m"],
                "track_quality_score": model["score"],
                "observations": track["nodes"],
                "association_edges": track["edges"],
                "rejected_observations": track["rejected_observations"],
                "refinement_iterations": track["refinement_iterations"],
                "alias_track_ids": [],
                "canonical_track_id": None,
                "alias_consolidation": None,
            }
        )
    enriched.sort(
        key=lambda item: (
            item["reference_center"][0],
            item["reference_center"][1],
            item["source_heights_m"][0],
            -item["track_quality_score"],
        )
    )
    for index, track in enumerate(enriched, start=1):
        track["track_id"] = f"T15-{index:04d}"
        track["canonical_track_id"] = track["track_id"]
    return enriched


def track_pair_geometry(left: dict, right: dict) -> dict:
    start = max(min(left["source_heights_m"]) - 0.125, min(right["source_heights_m"]) - 0.125)
    end = min(max(left["source_heights_m"]) + 0.125, max(right["source_heights_m"]) + 0.125)
    overlap = max(0.0, end - start)
    left_span = max(left["source_heights_m"]) - min(left["source_heights_m"]) + 0.25
    right_span = max(right["source_heights_m"]) - min(right["source_heights_m"]) + 0.25
    overlap_ratio = overlap / max(min(left_span, right_span), 1e-9)
    if overlap > 0:
        heights = np.linspace(start, end, 5)
        left_coefficients = np.asarray(left["centreline_coefficients"])
        right_coefficients = np.asarray(right["centreline_coefficients"])
        distances = []
        radius_differences = []
        for height in heights:
            left_center = np.asarray(
                [left_coefficients[0, 0] * height + left_coefficients[0, 1], left_coefficients[1, 0] * height + left_coefficients[1, 1]]
            )
            right_center = np.asarray(
                [right_coefficients[0, 0] * height + right_coefficients[0, 1], right_coefficients[1, 0] * height + right_coefficients[1, 1]]
            )
            distances.append(np.linalg.norm(left_center - right_center))
            left_radius = left["radius_coefficients"][0] * height + left["radius_coefficients"][1]
            right_radius = right["radius_coefficients"][0] * height + right["radius_coefficients"][1]
            radius_differences.append(relative_difference(left_radius, right_radius))
        mean_distance = float(np.mean(distances))
        maximum_distance = float(np.max(distances))
        radius_difference = float(np.median(radius_differences))
    else:
        mean_distance = maximum_distance = radius_difference = None
    return {
        "height_overlap_m": overlap,
        "height_overlap_ratio": overlap_ratio,
        "mean_centreline_distance_m": mean_distance,
        "maximum_centreline_distance_m": maximum_distance,
        "radius_relative_difference": radius_difference,
    }


def consolidate_track_aliases(
    tracks: list[dict],
    phase1_alias_rows: list[dict],
    config: dict,
) -> tuple[list[dict], list[dict]]:
    cfg = config["alias_consolidation"]
    phase1_overlap = {}
    for row in phase1_alias_rows:
        key = tuple(sorted((row["candidate_a"], row["candidate_b"])))
        phase1_overlap[key] = max(
            float(row.get("accepted_point_containment") or 0.0),
            phase1_overlap.get(key, 0.0),
        )
    positions = np.asarray([track["reference_center"] for track in tracks])
    pairs = cKDTree(positions).query_pairs(cfg["pair_search_m"]) if len(tracks) > 1 else set()
    pair_records = []
    definite_adjacency: dict[str, list[dict]] = defaultdict(list)
    for left_index, right_index in sorted(pairs):
        left = tracks[left_index]
        right = tracks[right_index]
        geometry = track_pair_geometry(left, right)
        point_overlap = max(
            (
                phase1_overlap.get(tuple(sorted((left_candidate, right_candidate))), 0.0)
                for left_candidate in left["source_candidate_ids"]
                for right_candidate in right["source_candidate_ids"]
            ),
            default=0.0,
        )
        definite = bool(
            geometry["height_overlap_ratio"] >= cfg["minimum_height_overlap_ratio"]
            and geometry["mean_centreline_distance_m"] is not None
            and geometry["mean_centreline_distance_m"] <= cfg["maximum_mean_centreline_distance_m"]
            and geometry["radius_relative_difference"] is not None
            and geometry["radius_relative_difference"] <= cfg["maximum_radius_relative_difference"]
            and point_overlap >= cfg["minimum_accepted_point_containment"]
        )
        probable = bool(
            not definite
            and geometry["height_overlap_ratio"] >= cfg["probable_minimum_height_overlap_ratio"]
            and geometry["mean_centreline_distance_m"] is not None
            and geometry["mean_centreline_distance_m"] <= cfg["probable_maximum_mean_centreline_distance_m"]
            and geometry["radius_relative_difference"] is not None
            and geometry["radius_relative_difference"] <= cfg["probable_maximum_radius_relative_difference"]
            and point_overlap >= cfg["probable_minimum_accepted_point_containment"]
        )
        classification = "DEFINITE_ALIAS" if definite else "PROBABLE_ALIAS" if probable else "NOT_ALIAS"
        record = {
            "track_a": left["track_id"],
            "track_b": right["track_id"],
            **geometry,
            "accepted_point_containment": point_overlap,
            "classification": classification,
            "merge_score": (
                geometry["height_overlap_ratio"]
                + max(0.0, 1.0 - (geometry["mean_centreline_distance_m"] or 1.0) / 0.20)
                + max(0.0, 1.0 - (geometry["radius_relative_difference"] or 1.0))
                + point_overlap
            )
            / 4.0,
            "criteria": {
                "height_overlap": geometry["height_overlap_ratio"] >= cfg["minimum_height_overlap_ratio"],
                "centreline_distance": geometry["mean_centreline_distance_m"] is not None and geometry["mean_centreline_distance_m"] <= cfg["maximum_mean_centreline_distance_m"],
                "radius_similarity": geometry["radius_relative_difference"] is not None and geometry["radius_relative_difference"] <= cfg["maximum_radius_relative_difference"],
                "supporting_point_overlap": point_overlap >= cfg["minimum_accepted_point_containment"],
            },
            "reason": "ALL_DETERMINISTIC_ALIAS_CRITERIA_MET" if definite else "PROBABLE_ALIAS_REQUIRES_REVIEW" if probable else "ALIAS_CRITERIA_NOT_MET",
        }
        pair_records.append(record)
        if definite:
            definite_adjacency[left["track_id"]].append(record)
            definite_adjacency[right["track_id"]].append(record)

    by_id = {track["track_id"]: track for track in tracks}
    canonical_tracks = []
    claimed = set()
    for track in sorted(tracks, key=lambda item: (-item["track_quality_score"], item["track_id"])):
        if track["track_id"] in claimed:
            continue
        canonical_tracks.append(track)
        for record in sorted(definite_adjacency.get(track["track_id"], []), key=lambda item: (-item["merge_score"], item["track_a"], item["track_b"])):
            alias_id = record["track_b"] if record["track_a"] == track["track_id"] else record["track_a"]
            if alias_id in claimed or alias_id == track["track_id"]:
                continue
            alias = by_id[alias_id]
            # Non-transitive NMS: only a direct deterministic edge to the
            # current canonical can create an alias.
            alias["identity_status"] = "DUPLICATE_ALIAS"
            alias["canonical_track_id"] = track["track_id"]
            alias["alias_consolidation"] = record
            track["alias_track_ids"].append(alias_id)
            claimed.add(alias_id)
        claimed.add(track["track_id"])
    canonical_tracks.sort(key=lambda item: item["track_id"])
    return canonical_tracks, pair_records


def build_candidate_alias_map(candidates: list[dict], tracks: list[dict], trace: dict[str, str]) -> dict:
    node_to_track = {
        node["node_id"]: track["track_id"]
        for track in tracks
        for node in track["observations"]
    }
    rows = []
    for candidate in candidates:
        track_counts = Counter()
        untraced = []
        for seed_id in candidate["source_seed_ids"]:
            node_id = trace.get(seed_id)
            track_id = node_to_track.get(node_id)
            if track_id is None:
                untraced.append(seed_id)
            else:
                track_counts[track_id] += 1
        ordered = sorted(track_counts.items(), key=lambda item: (-item[1], item[0]))
        rows.append(
            {
                "phase1_candidate_id": candidate["candidate_id"],
                "canonical_track_id": ordered[0][0] if ordered else None,
                "contributing_tracks": [
                    {"track_id": track_id, "source_seed_count": count}
                    for track_id, count in ordered
                ],
                "phase1_candidate_fragmented_across_tracks": len(ordered) > 1,
                "track_count": len(ordered),
                "source_seed_count": len(candidate["source_seed_ids"]),
                "untraced_seed_ids": untraced,
                "reason": (
                    "SOURCE_OBSERVATIONS_ASSOCIATED_TO_MULTIPLE_CONSTRAINED_TRACKS"
                    if len(ordered) > 1
                    else "SOURCE_OBSERVATIONS_ASSOCIATED_TO_ONE_TRACK"
                    if ordered
                    else "NO_TRACKED_VALID_OBSERVATION"
                ),
            }
        )
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "phase1_candidate_count": len(candidates),
        "candidate_aliases": rows,
    }


def load_manual_review_seeds(path: Path | None) -> list[phase1.SeedRecord]:
    if path is None:
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("manual_seeds", payload) if isinstance(payload, dict) else payload
    result = []
    for index, item in enumerate(records, start=1):
        source = item.get("source", "MANUAL_REVIEW_CLICK")
        if source != "MANUAL_REVIEW_CLICK":
            raise ValueError(f"Unexpected manual seed source: {source}")
        result.append(
            phase1.SeedRecord(
                seed_id=item.get("seed_id", f"MANUAL-REVIEW-{index:04d}"),
                source="MANUAL_REVIEW_CLICK",
                source_height_m=item.get("approximate_clean_height_m"),
                x=float(item["x"]),
                y=float(item["y"]),
                source_index=index,
            )
        )
    return result


def validate_annotation_bundle(payload: dict) -> dict:
    if payload.get("algorithm_version") != ALGORITHM_VERSION:
        raise ValueError("Annotation algorithm version mismatch")
    annotations = payload.get("annotations")
    if not isinstance(annotations, list):
        raise ValueError("annotations must be a list")
    normalized = []
    for item in annotations:
        if not item.get("candidate_id"):
            raise ValueError("candidate_id is required")
        label = item.get("human_label")
        if label not in HUMAN_LABELS:
            raise ValueError(f"Unknown human label: {label}")
        if label == "DUPLICATE_OF" and not item.get("duplicate_target"):
            raise ValueError("duplicate_target is required for DUPLICATE_OF")
        normalized.append(
            {
                "candidate_id": item["candidate_id"],
                "algorithm_version": ALGORITHM_VERSION,
                "automatic_status": item.get("automatic_status"),
                "human_label": label,
                "duplicate_target": item.get("duplicate_target"),
                "corrected_center": item.get("corrected_center"),
                "corrected_measurement_height_m": item.get("corrected_measurement_height_m"),
                "timestamp": item.get("timestamp"),
                "reviewer_note": item.get("reviewer_note", ""),
            }
        )
    return {"algorithm_version": ALGORITHM_VERSION, "annotations": normalized}
