#!/usr/bin/env python3
"""Phase 5A protocol-anchored POM and measurement helpers.

This module is an additive shadow layer.  It separates physical-tree
inventory, prop-root protocol applicability, the highest supported
root-to-main-stem attachment, the protocol POM, and the fit at that exact POM.
It never changes Tree eligibility or searches for a cleaner replacement POM.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import math
from pathlib import Path
from typing import Any

import numpy as np
import yaml

import stem_inventory_v2 as phase1


ALGORITHM_VERSION = "stem-inventory-v2-phase5a-protocol-anchored-pom-shadow"
PROTOCOL_ID = "MANGROVE_PROP_ROOT_PLUS_030"
REFERENCE_LANDMARK = "HIGHEST_PROP_ROOT_ATTACHMENT"
OFFSET_MODE = "VERTICAL_ABOVE_ATTACHMENT"
SLICE_ORIENTATION = "PERPENDICULAR_TO_LOCAL_STEM_AXIS"
PROP_ROOT_OFFSET_M = 0.30

APPLICABILITY_STATES = {
    "PROP_ROOT_PROTOCOL_APPLICABLE",
    "STANDARD_NON_PROP_ROOT_PROTOCOL",
    "PROTOCOL_APPLICABILITY_UNCERTAIN",
    "NOT_REVIEWED",
}
ATTACHMENT_STATUSES = {
    "CONFIRMED",
    "PROBABLE",
    "NEEDS_REVIEW",
    "NOT_VISIBLE",
    "NO_PROP_ROOT_FOUND",
    "CONFLICTING_ROOT_OWNERSHIP",
    "NOT_REVIEWED",
}


def load_config(path: Path) -> dict:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if config.get("algorithm_version") != ALGORITHM_VERSION:
        raise ValueError("Unexpected Phase 5A configuration version")
    if not config.get("mode", {}).get("shadow"):
        raise ValueError("Phase 5A must start in shadow mode")
    if config.get("mode", {}).get("phase4c_global_enforcement_enabled"):
        raise ValueError("Phase 4C global enforcement must remain disabled")
    protocol = config.get("measurement_protocol", {})
    expected = {
        "id": PROTOCOL_ID,
        "reference_landmark": REFERENCE_LANDMARK,
        "offset_mode": OFFSET_MODE,
        "slice_orientation": SLICE_ORIENTATION,
    }
    for key, value in expected.items():
        if protocol.get(key) != value:
            raise ValueError(f"Unexpected Phase 5A protocol {key}")
    offset = float(protocol.get("offset_m"))
    if not math.isclose(offset, PROP_ROOT_OFFSET_M, abs_tol=1e-12):
        raise ValueError("Confirmed prop-root protocol offset must be exactly 0.30 m")
    if math.isclose(offset, 1.30, abs_tol=1e-12):
        raise ValueError("The prop-root protocol must never use +1.30 m")
    return config


def protocol_record(config: dict, applicability: str) -> dict:
    if applicability not in APPLICABILITY_STATES:
        raise ValueError(f"Unknown applicability state: {applicability}")
    protocol = config["measurement_protocol"]
    return {
        "protocol_id": protocol["id"],
        "applicability": applicability,
        "reference_landmark": protocol["reference_landmark"],
        "offset_m": float(protocol["offset_m"]),
        "offset_mode": protocol["offset_mode"],
        "slice_orientation": protocol["slice_orientation"],
    }


def _normalise(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-12:
        raise ValueError("Cannot normalise a zero vector")
    return vector / norm


def perpendicular_plane_basis(direction: list[float] | np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    axis = _normalise(np.asarray(direction, dtype=float))
    reference = np.asarray([0.0, 1.0, 0.0]) if abs(axis[1]) < 0.9 else np.asarray([1.0, 0.0, 0.0])
    basis_u = _normalise(np.cross(axis, reference))
    basis_v = _normalise(np.cross(axis, basis_u))
    return axis, basis_u, basis_v


def axis_center_at_height(axis: dict, height_agl_m: float) -> np.ndarray:
    coefficients = np.asarray(axis["centerline_coefficients"], dtype=float)
    ground = float(axis["ground_z_m"])
    return np.asarray([
        coefficients[0, 0] * height_agl_m + coefficients[0, 1],
        coefficients[1, 0] * height_agl_m + coefficients[1, 1],
        ground + height_agl_m,
    ])


def axis_direction(axis: dict) -> np.ndarray:
    coefficients = np.asarray(axis["centerline_coefficients"], dtype=float)
    return _normalise(np.asarray([coefficients[0, 0], coefficients[1, 0], 1.0]))


def distance_along_axis_at_height(axis: dict, height_agl_m: float) -> float:
    coefficients = np.asarray(axis["centerline_coefficients"], dtype=float)
    scale = math.sqrt(1.0 + coefficients[0, 0] ** 2 + coefficients[1, 0] ** 2)
    return float(height_agl_m * scale)


def _fit_axis_from_observations(observations: list[dict]) -> tuple[np.ndarray, float]:
    heights = np.asarray([float(row["source_height_m"]) for row in observations], dtype=float)
    centers = np.asarray([row["center"] for row in observations], dtype=float)
    design = np.column_stack((heights, np.ones(len(heights))))
    coefficients = np.vstack([
        np.linalg.lstsq(design, centers[:, 0], rcond=None)[0],
        np.linalg.lstsq(design, centers[:, 1], rcond=None)[0],
    ])
    predicted = design @ coefficients.T
    residuals = np.linalg.norm(centers - predicted, axis=1)
    return coefficients, float(np.percentile(residuals, 90))


def estimate_main_stem_axis(
    tree: dict,
    track_by_id: dict[str, dict],
    ground_z_m: float,
    config: dict,
) -> dict:
    """Reuse Phase 1.5 tracks, preferring supported upper observations."""
    tracks = [track_by_id[track_id] for track_id in tree["stem"]["source_track_ids"]]
    primary = max(
        tracks,
        key=lambda row: (
            int(row.get("source_height_count") or 0),
            float(row.get("track_quality_score") or 0.0),
            row["track_id"],
        ),
    )
    observations = sorted(primary.get("observations", []), key=lambda row: float(row["source_height_m"]))
    heights = [float(row["source_height_m"]) for row in observations]
    cfg = config["main_stem_axis"]
    preferred = observations
    source_region = "FULL_SUPPORTED_TRACK"
    if observations:
        split = min(heights) + (max(heights) - min(heights)) * float(cfg["prefer_upper_fraction"])
        upper = [row for row in observations if float(row["source_height_m"]) >= split]
        if len(upper) >= int(cfg["minimum_upper_observations"]):
            preferred = upper
            source_region = "CLEANER_UPPER_MAIN_STEM_REGION"
    if len(preferred) >= 2 and max(float(row["source_height_m"]) for row in preferred) > min(float(row["source_height_m"]) for row in preferred):
        coefficients, uncertainty = _fit_axis_from_observations(preferred)
    else:
        coefficients = np.asarray(primary["centreline_coefficients"], dtype=float)
        uncertainty = float(primary.get("centre_residual_p90_m") or 0.0)
        source_region = "PHASE1_5_TRACK_MODEL_FALLBACK"
    z_min = min(heights) if heights else float(tree["stem"]["z_min_agl_m"])
    z_max = max(heights) if heights else float(tree["stem"]["z_max_agl_m"])
    span = z_max - z_min
    if (
        len(preferred) >= int(cfg["minimum_confirmed_observations"])
        and span >= float(cfg["minimum_confirmed_vertical_span_m"])
        and uncertainty <= float(cfg["maximum_confirmed_uncertainty_m"])
    ):
        status = "CONFIRMED"
    elif len(observations) >= 2:
        status = "PROBABLE"
    else:
        status = "NEEDS_REVIEW"
    tangent = _normalise(np.asarray([coefficients[0, 0], coefficients[1, 0], 1.0]))
    return {
        "tree_id": tree["tree_id"],
        "axis_status": status,
        "source_track_ids": sorted(tree["stem"]["source_track_ids"]),
        "primary_track_id": primary["track_id"],
        "source_region": source_region,
        "centerline_coefficients": coefficients.tolist(),
        "local_direction": tangent.tolist(),
        "vertical_range_agl_m": [z_min, z_max],
        "axis_uncertainty_m": uncertainty,
        "point_support_statistics": {
            "track_observation_count": len(observations),
            "axis_fit_observation_count": len(preferred),
            "source_height_count": len(set(heights)),
        },
        "gaps": deepcopy(primary.get("gaps", [])),
        "ground_z_m": float(ground_z_m),
        "source_candidate_ids": sorted(tree.get("source_candidates", [])),
        "algorithm_version": ALGORITHM_VERSION,
    }


def resolve_protocol_applicability(
    tree: dict,
    human_labels_by_candidate: dict[str, str],
    supported_root_parent_ids: set[str],
    manual_review: dict | None = None,
    historical_manual_evaluation: dict | None = None,
) -> dict:
    if tree.get("detection", {}).get("status") not in {"CONFIRMED", "PROBABLE"}:
        return {
            "tree_id": tree["tree_id"],
            "applicability": "NOT_REVIEWED",
            "decision_source": "TREE_INVENTORY_GATE",
            "reason_codes": ["PHYSICAL_TREE_NOT_CONFIRMED_OR_PROBABLE"],
        }
    manual = (manual_review or {}).get("protocol_applicability")
    if manual in APPLICABILITY_STATES:
        return {
            "tree_id": tree["tree_id"],
            "applicability": manual,
            "decision_source": "HUMAN_PHASE5A_REVIEW",
            "reason_codes": ["EXPLICIT_REVIEWER_PROTOCOL_DECISION"],
            "manual_review": deepcopy(manual_review),
        }
    root_candidates = sorted(
        candidate_id
        for candidate_id in tree.get("source_candidates", [])
        if human_labels_by_candidate.get(candidate_id) == "PROP_ROOT_OR_ROOT_ONLY"
    )
    if root_candidates:
        return {
            "tree_id": tree["tree_id"],
            "applicability": "PROP_ROOT_PROTOCOL_APPLICABLE",
            "decision_source": "EXISTING_HUMAN_ROOT_STRUCTURE_ANNOTATION",
            "reason_codes": ["HUMAN_PROP_ROOT_FRAGMENT_ASSOCIATED_WITH_PHYSICAL_TREE"],
            "source_root_candidate_ids": root_candidates,
        }
    if tree["tree_id"] in supported_root_parent_ids:
        return {
            "tree_id": tree["tree_id"],
            "applicability": "PROP_ROOT_PROTOCOL_APPLICABLE",
            "decision_source": "SUPPORTED_RELATIONAL_PROP_ROOT_GEOMETRY",
            "reason_codes": ["PHASE4C_SUPPORTED_PROP_ROOT_CHILD_RELATIONSHIP"],
            "phase4c_global_suppression_enabled": False,
        }
    reasons = set((historical_manual_evaluation or {}).get("reason_codes", []))
    if {"POSSIBLE_PROP_ROOT_ZONE", "LARGE_LOWER_COMPONENT"} & reasons:
        return {
            "tree_id": tree["tree_id"],
            "applicability": "PROTOCOL_APPLICABILITY_UNCERTAIN",
            "decision_source": "LOWER_STEM_GEOMETRY_REVIEW_TRIGGER",
            "reason_codes": sorted({"POSSIBLE_PROP_ROOT_GEOMETRY_NOT_A_BOTANICAL_SPECIES_INFERENCE"} | (reasons & {"POSSIBLE_PROP_ROOT_ZONE", "LARGE_LOWER_COMPONENT"})),
        }
    return {
        "tree_id": tree["tree_id"],
        "applicability": "NOT_REVIEWED",
        "decision_source": "NO_EXPLICIT_PROTOCOL_EVIDENCE",
        "reason_codes": ["REVIEW_REQUIRED_BEFORE_PROP_ROOT_OR_STANDARD_PROTOCOL_ASSIGNMENT"],
    }


def _candidate_id(tree_id: str, source: str, source_ids: list[str], height: float) -> str:
    value = "|".join([tree_id, source, *sorted(source_ids), f"{height:.6f}"])
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12].upper()
    return f"P5A-ATT-{digest}"


def make_attachment_candidate(
    tree_id: str,
    axis: dict,
    height_agl_m: float,
    *,
    source: str,
    source_root_track_ids: list[str],
    source_main_stem_track_ids: list[str],
    source_candidate_ids: list[str] | None = None,
    geometric_support_features: dict | None = None,
    contradictory_evidence: list[str] | None = None,
    evidence_score: float = 0.0,
    relationship_evidence_count: int = 0,
    ownership_status: str = "OWNERSHIP_UNCERTAIN",
    localization_interval_agl_m: list[float] | None = None,
    selection_eligible: bool = False,
    highest_raw_root_point_height_agl_m: float | None = None,
) -> dict:
    position = axis_center_at_height(axis, height_agl_m)
    source_ids = source_root_track_ids or source_candidate_ids or [source]
    return {
        "attachment_candidate_id": _candidate_id(tree_id, source, source_ids, height_agl_m),
        "tree_id": tree_id,
        "position_xyz": {"x": float(position[0]), "y": float(position[1]), "z": float(position[2])},
        "height_agl_m": float(height_agl_m),
        "distance_along_stem_axis_m": distance_along_axis_at_height(axis, height_agl_m),
        "source": source,
        "source_root_track_ids": sorted(source_root_track_ids),
        "source_main_stem_track_ids": sorted(source_main_stem_track_ids),
        "source_candidate_ids": sorted(source_candidate_ids or []),
        "geometric_support_features": deepcopy(geometric_support_features or {}),
        "contradictory_evidence": sorted(set(contradictory_evidence or [])),
        "relationship_evidence_count": int(relationship_evidence_count),
        "evidence_score": float(evidence_score),
        "evidence_score_is_calibrated_probability": False,
        "ownership_status": ownership_status,
        "localization_interval_agl_m": localization_interval_agl_m or [float(height_agl_m), float(height_agl_m)],
        "selection_eligible": bool(selection_eligible),
        "highest_raw_root_point_height_agl_m": highest_raw_root_point_height_agl_m,
        "highest_raw_root_point_used_as_attachment": False,
        "algorithm_version": ALGORITHM_VERSION,
    }


def candidate_from_phase4c_relationship(edge: dict, axis: dict, config: dict) -> dict | None:
    if edge.get("proposed_attachment_class") != "PROP_ROOT_ATTACHED":
        return None
    height = float(edge["attachment_height_agl_m"])
    root_indicators = [name for name, value in edge.get("root_indicators", {}).items() if value]
    common = [name for name, value in edge.get("common_parent_checks", {}).items() if value]
    evidence_count = len(root_indicators) + int(bool(edge.get("graph_edge_ids")))
    half = float(config["attachment"]["phase4c_height_half_interval_m"])
    return make_attachment_candidate(
        edge["parent_tree_id"],
        axis,
        height,
        source="PHASE4C_RELATIONAL_PROP_ROOT_EDGE",
        source_root_track_ids=[edge["child_track_id"]],
        source_main_stem_track_ids=[edge["parent_track_id"]],
        source_candidate_ids=edge.get("shared_candidate_ids", []),
        geometric_support_features={
            "root_indicators": root_indicators,
            "common_parent_checks_passed": common,
            "minimum_centerline_distance_m": edge.get("minimum_centerline_distance_m"),
            "convergence_upward_m": edge.get("convergence_upward_m"),
            "vertical_overlap_m": edge.get("vertical_overlap_m"),
            "graph_edge_ids": edge.get("graph_edge_ids", []),
        },
        contradictory_evidence=edge.get("failed_parent_checks", []),
        evidence_score=float(edge.get("attachment_rank_score") or 0.0),
        relationship_evidence_count=evidence_count,
        ownership_status="OWNERSHIP_SUPPORTED",
        localization_interval_agl_m=[height - half, height + half],
        selection_eligible=evidence_count >= int(config["attachment"]["minimum_relationship_evidence_count"]),
    )


def candidates_from_human_root_transitions(
    tree: dict,
    axis: dict,
    track_by_id: dict[str, dict],
    human_labels_by_candidate: dict[str, str],
    config: dict,
) -> list[dict]:
    root_candidate_ids = {
        candidate_id for candidate_id in tree.get("source_candidates", [])
        if human_labels_by_candidate.get(candidate_id) == "PROP_ROOT_OR_ROOT_ONLY"
    }
    main_candidate_ids = set(tree.get("source_candidates", [])) - root_candidate_ids
    result = []
    for track_id in tree["stem"]["source_track_ids"]:
        track = track_by_id[track_id]
        root_observations = []
        main_observations = []
        for observation in track.get("observations", []):
            candidate_ids = set(observation.get("phase1_candidate_ids", []))
            if candidate_ids & root_candidate_ids:
                root_observations.append(observation)
            if candidate_ids & main_candidate_ids:
                main_observations.append(observation)
        if not root_observations or not main_observations:
            continue
        lower_root = max(root_observations, key=lambda row: float(row["source_height_m"]))
        upper_main = min(
            (row for row in main_observations if float(row["source_height_m"]) >= float(lower_root["source_height_m"])),
            key=lambda row: float(row["source_height_m"]),
            default=None,
        )
        if not upper_main:
            continue
        low = float(lower_root["source_height_m"])
        high = float(upper_main["source_height_m"])
        height = (low + high) / 2.0
        center_distance = float(np.linalg.norm(np.asarray(lower_root["center"]) - np.asarray(upper_main["center"])))
        evidence = {
            "same_phase1_5_track": True,
            "ordered_root_to_main_transition": True,
            "root_observation_height_agl_m": low,
            "main_observation_height_agl_m": high,
            "transition_bracket_width_m": high - low,
            "transition_center_distance_m": center_distance,
            "human_root_structure_label": True,
        }
        score = max(0.0, min(1.0, 0.75 - (high - low) * 0.5 - center_distance * 0.2))
        result.append(make_attachment_candidate(
            tree["tree_id"],
            axis,
            height,
            source="HUMAN_ROOT_STRUCTURE_TRACK_TRANSITION",
            source_root_track_ids=[track_id],
            source_main_stem_track_ids=[track_id],
            source_candidate_ids=sorted(root_candidate_ids),
            geometric_support_features=evidence,
            contradictory_evidence=["ROOT_TO_STEM_TRANSITION_LOCALIZED_ONLY_TO_OBSERVATION_BRACKET"],
            evidence_score=score,
            relationship_evidence_count=3,
            ownership_status="OWNERSHIP_SUPPORTED",
            localization_interval_agl_m=[low, high],
            selection_eligible=True,
            highest_raw_root_point_height_agl_m=low,
        ))
    return result


def candidate_from_historical_profile_transition(
    tree: dict,
    axis: dict,
    historical_evaluation: dict | None,
    config: dict,
) -> dict | None:
    """Create a review suggestion, never an automatically valid landmark."""
    if not historical_evaluation:
        return None
    reasons = set(historical_evaluation.get("reason_codes", []))
    height = historical_evaluation.get("irregular_zone_top_height_m")
    if height is None or "POSSIBLE_PROP_ROOT_ZONE" not in reasons:
        return None
    height = float(height)
    half = float(config["attachment"]["profile_transition_half_interval_m"])
    return make_attachment_candidate(
        tree["tree_id"],
        axis,
        height,
        source="PHASE2_LOWER_STEM_TOPOLOGY_REVIEW_SUGGESTION",
        source_root_track_ids=[],
        source_main_stem_track_ids=tree["stem"]["source_track_ids"],
        source_candidate_ids=tree.get("source_candidates", []),
        geometric_support_features={
            "irregular_zone_top_height_m": height,
            "profile_reason_codes": sorted(reasons & {"POSSIBLE_PROP_ROOT_ZONE", "LARGE_LOWER_COMPONENT", "STANDARD_HEIGHT_UNSTABLE"}),
            "track_height_range_m": historical_evaluation.get("diagnostics", {}).get("manual_anchor_pilot", {}).get("track_height_range_m"),
        },
        contradictory_evidence=[
            "NO_INDEPENDENT_ROOT_TRACK_OWNERSHIP",
            "IRREGULAR_ZONE_TOP_IS_NOT_ITSELF_A_CONFIRMED_ATTACHMENT",
        ],
        evidence_score=0.45,
        relationship_evidence_count=1,
        ownership_status="OWNERSHIP_UNCERTAIN",
        localization_interval_agl_m=[height - half, height + half],
        selection_eligible=False,
    )


def _manual_attachment(manual_review: dict | None, axis: dict) -> dict | None:
    review = manual_review or {}
    status = review.get("attachment_status")
    if status == "NOT_VISIBLE":
        return {
            "status": "NOT_VISIBLE",
            "position_xyz": None,
            "height_agl_m": None,
            "distance_along_stem_axis_m": None,
            "source_root_track_ids": [],
            "confidence": None,
            "confidence_is_calibrated_probability": False,
            "reason_codes": ["HUMAN_MARKED_ATTACHMENT_NOT_VISIBLE"],
            "decision_source": "HUMAN_PHASE5A_REVIEW",
        }
    point = review.get("manual_attachment_point_xyz")
    if status in {"CONFIRMED", "PROBABLE"} and point:
        height = float(point["z"]) - float(axis["ground_z_m"])
        return {
            "status": status,
            "position_xyz": {key: float(point[key]) for key in ("x", "y", "z")},
            "height_agl_m": height,
            "distance_along_stem_axis_m": distance_along_axis_at_height(axis, height),
            "source_root_track_ids": sorted(review.get("accepted_root_track_ids", [])),
            "confidence": None,
            "confidence_is_calibrated_probability": False,
            "reason_codes": ["HUMAN_SELECTED_HIGHEST_PROP_ROOT_ATTACHMENT"],
            "decision_source": "HUMAN_PHASE5A_REVIEW",
            "timestamp": review.get("timestamp"),
            "reviewer_reason": review.get("reason"),
        }
    return None


def select_highest_supported_attachment(
    tree_id: str,
    applicability: str,
    candidates: list[dict],
    axis: dict,
    config: dict,
    manual_review: dict | None = None,
) -> dict:
    automatic_candidates = deepcopy(sorted(candidates, key=lambda row: (row["height_agl_m"], row["attachment_candidate_id"])))
    manual = _manual_attachment(manual_review, axis)
    if manual:
        manual["tree_id"] = tree_id
        manual["automatic_suggestion_preserved"] = automatic_candidates
        manual["manual_override_applied"] = True
        return manual
    if applicability == "STANDARD_NON_PROP_ROOT_PROTOCOL":
        status, reasons = "NO_PROP_ROOT_FOUND", ["EXPLICIT_STANDARD_NON_PROP_ROOT_PROTOCOL"]
    elif applicability == "NOT_REVIEWED":
        status, reasons = "NOT_REVIEWED", ["PROTOCOL_APPLICABILITY_NOT_REVIEWED"]
    else:
        supported = [
            row for row in candidates
            if row.get("selection_eligible")
            and row.get("ownership_status") == "OWNERSHIP_SUPPORTED"
            and int(row.get("relationship_evidence_count") or 0) >= int(config["attachment"]["minimum_relationship_evidence_count"])
        ]
        conflicting = [row for row in candidates if row.get("ownership_status") == "CONFLICTING_ROOT_OWNERSHIP"]
        if conflicting:
            status, reasons = "CONFLICTING_ROOT_OWNERSHIP", ["ROOT_HAS_COMPETING_PLAUSIBLE_PARENT_STEMS"]
        elif not supported:
            status, reasons = "NEEDS_REVIEW", ["NO_SUPPORTED_ROOT_TO_MAIN_STEM_ATTACHMENT"]
        else:
            supported.sort(key=lambda row: (row["height_agl_m"], row["evidence_score"], row["attachment_candidate_id"]), reverse=True)
            selected = supported[0]
            close = [
                row for row in supported[1:]
                if abs(float(row["height_agl_m"]) - float(selected["height_agl_m"])) <= float(config["attachment"]["competing_height_tolerance_m"])
                and set(row.get("source_root_track_ids", [])) != set(selected.get("source_root_track_ids", []))
            ]
            if close:
                status, reasons = "NEEDS_REVIEW", ["COMPETING_HIGHEST_SUPPORTED_ATTACHMENTS"]
            else:
                interval = selected["localization_interval_agl_m"]
                width = float(interval[1]) - float(interval[0])
                probable = (
                    float(selected["evidence_score"]) >= float(config["attachment"]["probable_evidence_score"])
                    and width <= float(config["attachment"]["maximum_localization_interval_m"])
                )
                status = "PROBABLE" if probable else "NEEDS_REVIEW"
                reasons = ["HIGHEST_SUPPORTED_ATTACHMENT_SELECTED"]
                if not probable:
                    reasons.append("ATTACHMENT_SUPPORT_OR_LOCALIZATION_REQUIRES_REVIEW")
                if status == "PROBABLE":
                    return {
                        "tree_id": tree_id,
                        "status": status,
                        "attachment_candidate_id": selected["attachment_candidate_id"],
                        "position_xyz": deepcopy(selected["position_xyz"]),
                        "height_agl_m": selected["height_agl_m"],
                        "distance_along_stem_axis_m": selected["distance_along_stem_axis_m"],
                        "source_root_track_ids": selected["source_root_track_ids"],
                        "source_main_stem_track_ids": selected["source_main_stem_track_ids"],
                        "confidence": selected["evidence_score"],
                        "confidence_is_calibrated_probability": False,
                        "reason_codes": reasons,
                        "localization_interval_agl_m": interval,
                        "automatic_suggestion_preserved": automatic_candidates,
                        "manual_override_applied": False,
                        "highest_raw_root_point_used_as_attachment": False,
                    }
    return {
        "tree_id": tree_id,
        "status": status,
        "position_xyz": None,
        "height_agl_m": None,
        "distance_along_stem_axis_m": None,
        "source_root_track_ids": [],
        "confidence": None,
        "confidence_is_calibrated_probability": False,
        "reason_codes": reasons,
        "automatic_suggestion_preserved": automatic_candidates,
        "manual_override_applied": False,
        "highest_raw_root_point_used_as_attachment": False,
    }


def calculate_protocol_pom(attachment: dict, axis: dict, config: dict) -> dict:
    protocol = config["measurement_protocol"]
    offset = float(protocol["offset_m"])
    if not math.isclose(offset, PROP_ROOT_OFFSET_M, abs_tol=1e-12) or math.isclose(offset, 1.30, abs_tol=1e-12):
        raise ValueError("Unsafe prop-root offset")
    if attachment.get("status") not in {"CONFIRMED", "PROBABLE"} or attachment.get("height_agl_m") is None:
        return {
            "status": "NOT_COMPUTED",
            "height_agl_m": None,
            "distance_along_stem_axis_m": None,
            "offset_m": offset,
            "position_xyz": None,
            "reason_codes": ["HIGHEST_PROP_ROOT_ATTACHMENT_UNRESOLVED"],
        }
    if protocol["offset_mode"] != OFFSET_MODE:
        raise ValueError("Only the configured vertical-above-attachment mode is implemented")
    height = float(attachment["height_agl_m"]) + offset
    center = axis_center_at_height(axis, height)
    direction = axis_direction(axis)
    attachment_position = np.asarray([
        attachment["position_xyz"]["x"], attachment["position_xyz"]["y"], attachment["position_xyz"]["z"]
    ], dtype=float)
    along_axis_position = attachment_position + direction * offset
    vertical_position = attachment_position + np.asarray([0.0, 0.0, offset])
    z_min, z_max = map(float, axis["vertical_range_agl_m"])
    reasons = []
    status = "COMPUTED"
    if height < z_min or height > z_max:
        status = "NEEDS_REVIEW"
        reasons.append("POM_OUTSIDE_SUPPORTED_MAIN_STEM_AXIS_RANGE")
    if axis.get("axis_status") == "NEEDS_REVIEW":
        status = "NEEDS_REVIEW"
        reasons.append("LOCAL_MAIN_STEM_AXIS_REQUIRES_REVIEW")
    return {
        "status": status,
        "height_agl_m": height,
        "distance_along_stem_axis_m": distance_along_axis_at_height(axis, height),
        "offset_m": offset,
        "offset_mode": protocol["offset_mode"],
        "position_xyz": {"x": float(center[0]), "y": float(center[1]), "z": float(center[2])},
        "local_stem_direction": direction.tolist(),
        "slice_orientation": protocol["slice_orientation"],
        "vertical_offset_position_xyz": vertical_position.tolist(),
        "along_axis_offset_position_xyz": along_axis_position.tolist(),
        "vertical_vs_along_axis_position_difference_m": float(np.linalg.norm(vertical_position - along_axis_position)),
        "vertical_vs_along_axis_height_difference_m": float(vertical_position[2] - along_axis_position[2]),
        "reason_codes": sorted(set(reasons)),
        "cleaner_height_substitution_allowed": False,
    }


def extract_perpendicular_cross_section(
    points_xyz: np.ndarray,
    center_xyz: list[float] | np.ndarray,
    direction: list[float] | np.ndarray,
    slab_thickness_m: float,
    radial_limit_m: float,
    plane_offset_m: float = 0.0,
) -> dict:
    axis, basis_u, basis_v = perpendicular_plane_basis(direction)
    center = np.asarray(center_xyz, dtype=float) + axis * float(plane_offset_m)
    relative = np.asarray(points_xyz, dtype=float) - center
    axial = relative @ axis
    plane_xy = np.column_stack((relative @ basis_u, relative @ basis_v))
    radial = np.linalg.norm(plane_xy, axis=1)
    mask = (np.abs(axial) <= float(slab_thickness_m) / 2.0) & (radial <= float(radial_limit_m))
    return {
        "center_xyz": center,
        "axis": axis,
        "basis_u": basis_u,
        "basis_v": basis_v,
        "points_xyz": np.asarray(points_xyz, dtype=float)[mask],
        "plane_xy": plane_xy[mask],
        "axial_offsets_m": axial[mask],
        "radial_offsets_m": radial[mask],
        "slab_thickness_m": float(slab_thickness_m),
        "plane_offset_m": float(plane_offset_m),
        "orientation_dot_product": float(abs(np.dot(axis, basis_u)) + abs(np.dot(axis, basis_v))),
    }


def _compact_fit(fit: dict | None) -> dict | None:
    if not fit:
        return None
    keys = [
        "valid", "center", "radius_m", "circle_residual_m", "inlier_count",
        "angular_coverage_deg", "largest_missing_angular_sector_deg", "score",
        "inlier_tolerance_m", "rejection_reasons", "component_index",
        "component_point_count", "local_point_density_per_m2", "ellipse",
    ]
    ready = {key: deepcopy(fit.get(key)) for key in keys if key in fit}
    if isinstance(ready.get("center"), np.ndarray):
        ready["center"] = ready["center"].tolist()
    return phase1.json_ready(ready)


def _relative_difference(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return abs(float(left) - float(right)) / max(abs(float(left)), abs(float(right)), 1e-9)


def radius_at_configured_bound(radius_m: float, phase1_config: dict, epsilon_m: float) -> bool:
    bounds = phase1_config["candidate_radius"]
    return bool(
        abs(float(radius_m) - float(bounds["minimum_m"])) <= float(epsilon_m)
        or abs(float(radius_m) - float(bounds["maximum_m"])) <= float(epsilon_m)
    )


def fit_cross_section_at_exact_pom(
    tree_id: str,
    local_points_xyz: np.ndarray,
    protocol_pom: dict,
    axis: dict,
    phase1_config: dict,
    config: dict,
    *,
    source: str = "SAMPLED_BROWSER_POINT_CLOUD_SHARED_INDEX",
) -> tuple[dict, dict]:
    if protocol_pom.get("status") != "COMPUTED":
        return ({
            "status": "NOT_ATTEMPTED",
            "circumference_cm": None,
            "diameter_cm": None,
            "fit_model": None,
            "field_verified": False,
            "reason_codes": ["PROTOCOL_POM_NOT_COMPUTED_OR_REQUIRES_REVIEW"],
        }, {})
    cross_cfg = config["cross_section"]
    center = np.asarray([protocol_pom["position_xyz"][key] for key in ("x", "y", "z")], dtype=float)
    direction = np.asarray(protocol_pom["local_stem_direction"], dtype=float)
    seed_value = int(hashlib.sha256(tree_id.encode("utf-8")).hexdigest()[:8], 16) + int(phase1_config["random_seed"])

    def one_fit(thickness: float, plane_offset: float) -> tuple[dict, dict | None]:
        section = extract_perpendicular_cross_section(
            local_points_xyz, center, direction, thickness,
            float(cross_cfg["radial_extraction_radius_m"]), plane_offset,
        )
        if len(section["plane_xy"]) < int(cross_cfg["minimum_point_support"]):
            return section, None
        rng = np.random.default_rng(seed_value + int(round(thickness * 10000)) + int(round((plane_offset + 1.0) * 10000)))
        fitted = phase1.fit_slice_profile(section["plane_xy"], np.zeros(2), phase1_config, rng)
        valid = [row for row in fitted["fits"] if row.get("valid")]
        if not valid:
            return section, None
        best = min(
            valid,
            key=lambda row: (
                float(np.linalg.norm(np.asarray(row["center"], dtype=float))),
                -float(row.get("score") or 0.0),
            ),
        )
        return section, best

    slab = float(cross_cfg["slab_thickness_m"])
    exact_section, exact_fit = one_fit(slab, 0.0)
    evidence = {
        "source": source,
        "source_las_rescan_for_tree": False,
        "protocol_plane_moved_to_cleaner_height": False,
        "exact_protocol_pom_position_xyz": center.tolist(),
        "local_stem_direction": direction.tolist(),
        "plane_basis_u": exact_section["basis_u"].tolist(),
        "plane_basis_v": exact_section["basis_v"].tolist(),
        "orientation_dot_product": exact_section["orientation_dot_product"],
        "slab_thickness_m": slab,
        "extracted_point_count": int(len(exact_section["points_xyz"])),
        "extracted_points_xyz": exact_section["points_xyz"].tolist(),
        "projected_points_xy": exact_section["plane_xy"].tolist(),
        "exact_fit": _compact_fit(exact_fit),
        "qa_variants": [],
    }
    if exact_fit is None:
        reasons = ["INSUFFICIENT_POINT_SUPPORT"] if len(exact_section["points_xyz"]) < int(cross_cfg["minimum_point_support"]) else ["NO_PLAUSIBLE_CROSS_SECTION_FIT_AT_PROTOCOL_POM"]
        return ({
            "status": "NOT_MEASURABLE",
            "circumference_cm": None,
            "diameter_cm": None,
            "fit_model": None,
            "field_verified": False,
            "reason_codes": reasons,
        }, evidence)

    ellipse = exact_fit.get("ellipse") or {}
    circle_circumference = 2.0 * math.pi * float(exact_fit["radius_m"]) * 100.0
    ellipse_circumference = None
    if ellipse.get("valid"):
        ellipse_circumference = phase1.ellipse_perimeter(
            float(ellipse["semi_major_axis_m"]), float(ellipse["semi_minor_axis_m"])
        ) * 100.0
    use_ellipse = bool(
        ellipse.get("valid")
        and float(ellipse.get("ellipse_residual_m") or math.inf)
        < float(exact_fit["circle_residual_m"]) * float(phase1_config["full_resolution"]["ellipse_selection_residual_ratio"])
    )
    proposed_circumference = ellipse_circumference if use_ellipse else circle_circumference
    proposed_diameter = proposed_circumference / math.pi
    reasons = []
    if int(exact_fit.get("inlier_count") or 0) < int(cross_cfg["minimum_point_support"]):
        reasons.append("INSUFFICIENT_INLIER_SUPPORT")
    if float(exact_fit.get("angular_coverage_deg") or 0.0) < float(cross_cfg["minimum_angular_coverage_deg"]):
        reasons.append("POOR_ANGULAR_COVERAGE")
    center_offset = float(np.linalg.norm(np.asarray(exact_fit["center"], dtype=float)))
    if center_offset > float(cross_cfg["maximum_axis_center_offset_m"]):
        reasons.append("FITTED_CENTER_INCONSISTENT_WITH_MAIN_STEM_AXIS")
    disagreement = _relative_difference(circle_circumference, ellipse_circumference)
    if disagreement is not None and disagreement > float(cross_cfg["maximum_circle_ellipse_relative_difference"]):
        reasons.append("CIRCLE_ELLIPSE_DISAGREEMENT")
    radius = float(exact_fit["radius_m"])
    epsilon = float(cross_cfg["radius_bound_epsilon_m"])
    radius_bound_hit = radius_at_configured_bound(radius, phase1_config, epsilon)
    if radius_bound_hit:
        reasons.append("FITTED_RADIUS_AT_CONFIGURED_BOUND")
    if float(axis.get("axis_uncertainty_m") or 0.0) > float(cross_cfg["maximum_axis_uncertainty_m"]):
        reasons.append("LOCAL_AXIS_UNCERTAINTY_TOO_HIGH")

    variant_values = []
    for thickness in cross_cfg["slab_thickness_variants_m"]:
        for offset in cross_cfg["neighbouring_plane_offsets_m"]:
            section, fit = one_fit(float(thickness), float(offset))
            value = None if fit is None else 2.0 * math.pi * float(fit["radius_m"]) * 100.0
            evidence["qa_variants"].append({
                "slab_thickness_m": float(thickness),
                "plane_offset_m": float(offset),
                "point_count": int(len(section["points_xyz"])),
                "circle_circumference_cm": value,
                "fit": _compact_fit(fit),
                "used_as_replacement_measurement": False,
            })
            if value is not None:
                variant_values.append(value)
    if variant_values:
        variation = (max(variant_values) - min(variant_values)) / max(abs(proposed_circumference), 1e-9)
        if variation > float(cross_cfg["maximum_variant_relative_difference"]):
            reasons.append("UNSTABLE_FIT_ACROSS_SLAB_OR_NEIGHBOURING_PLANES")
    else:
        reasons.append("NO_VALID_QA_VARIANT_FITS")
    if not bool(cross_cfg["sampled_browser_cloud_can_be_final"]) and source == "SAMPLED_BROWSER_POINT_CLOUD_SHARED_INDEX":
        reasons.append("SAMPLED_BROWSER_CLOUD_FIT_IS_NON_FINAL")
    status = "MEASURABLE" if not reasons else "NEEDS_REVIEW"
    return ({
        "status": status,
        "circumference_cm": round(proposed_circumference, 2) if status == "MEASURABLE" else None,
        "diameter_cm": round(proposed_diameter, 2) if status == "MEASURABLE" else None,
        "proposed_nonfinal_circumference_cm": round(proposed_circumference, 2),
        "proposed_nonfinal_diameter_cm": round(proposed_diameter, 2),
        "fit_model": "ELLIPSE" if use_ellipse else "CIRCLE",
        "circle_circumference_cm": round(circle_circumference, 2),
        "ellipse_circumference_cm": round(ellipse_circumference, 2) if ellipse_circumference is not None else None,
        "circle_ellipse_relative_difference": disagreement,
        "fitted_radius_m": radius,
        "radius_bound_hit": radius_bound_hit,
        "fit_center_offset_m": center_offset,
        "angular_coverage_deg": float(exact_fit.get("angular_coverage_deg") or 0.0),
        "point_count": int(len(exact_section["points_xyz"])),
        "inlier_count": int(exact_fit.get("inlier_count") or 0),
        "field_verified": False,
        "reason_codes": sorted(set(reasons)),
    }, evidence)


def apply_measurement_review(measurement: dict, manual_review: dict | None) -> dict:
    result = deepcopy(measurement)
    decision = (manual_review or {}).get("measurement_decision")
    result["automatic_measurement_preserved"] = deepcopy(measurement)
    result["manual_review_decision"] = decision
    if decision == "ACCEPT" and measurement.get("proposed_nonfinal_circumference_cm") is not None:
        blocking = {"FITTED_RADIUS_AT_CONFIGURED_BOUND", "NO_PLAUSIBLE_CROSS_SECTION_FIT_AT_PROTOCOL_POM"}
        if not (blocking & set(measurement.get("reason_codes", []))):
            result["status"] = "MEASURABLE"
            result["circumference_cm"] = measurement["proposed_nonfinal_circumference_cm"]
            result["diameter_cm"] = measurement["proposed_nonfinal_diameter_cm"]
            result["reason_codes"] = sorted(set(result.get("reason_codes", [])) | {"HUMAN_ACCEPTED_LIDAR_MEASUREMENT"})
    elif decision == "REJECT":
        result["status"] = "NOT_MEASURABLE"
        result["circumference_cm"] = None
        result["diameter_cm"] = None
        result["reason_codes"] = sorted(set(result.get("reason_codes", [])) | {"HUMAN_REJECTED_MEASUREMENT"})
    elif decision == "UNCERTAIN":
        result["status"] = "NEEDS_REVIEW"
        result["circumference_cm"] = None
        result["diameter_cm"] = None
        result["reason_codes"] = sorted(set(result.get("reason_codes", [])) | {"HUMAN_MARKED_MEASUREMENT_UNCERTAIN"})
    return result


def evaluate_phase5a(records: list[dict], annotations: list[dict], tolerances: list[float]) -> dict:
    annotation_by_tree = {row["tree_id"]: row for row in annotations if row.get("tree_id")}
    completed = [
        row for row in annotations
        if row.get("attachment_status") in {"CONFIRMED", "PROBABLE"}
        and row.get("manual_attachment_point_xyz")
    ]
    errors = []
    ownership_errors = 0
    missed = 0
    for manual in completed:
        record = next((row for row in records if row["tree_id"] == manual["tree_id"]), None)
        if not record:
            continue
        automatic = record["highest_prop_root_attachment"].get("automatic_suggestion_preserved", [])
        supported = [row for row in automatic if row.get("selection_eligible")]
        if not supported:
            missed += 1
            continue
        proposed = max(supported, key=lambda row: (row["height_agl_m"], row["attachment_candidate_id"]))
        manual_height = float(manual["manual_attachment_point_xyz"]["z"]) - float(record["main_stem"]["ground_z_m"])
        errors.append(abs(float(proposed["height_agl_m"]) - manual_height))
        if proposed.get("ownership_status") != "OWNERSHIP_SUPPORTED":
            ownership_errors += 1
    counts = {status: 0 for status in ["MEASURABLE", "NEEDS_REVIEW", "NOT_MEASURABLE", "NOT_ATTEMPTED"]}
    for record in records:
        counts[record["measurement"]["status"]] = counts.get(record["measurement"]["status"], 0) + 1
    accuracy_available = bool(completed)
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "evaluation_status": "COMPUTED_FROM_MANUAL_LANDMARKS" if accuracy_available else "NO_COMPLETED_MANUAL_ROOT_ATTACHMENT_LABELS",
        "accuracy_claim_allowed": False,
        "field_accuracy_claim_allowed": False,
        "reviewed_trees": len(annotation_by_tree),
        "protocol_applicable_trees": sum(row["measurement_protocol"]["applicability"] == "PROP_ROOT_PROTOCOL_APPLICABLE" for row in records),
        "automatic_root_attachment_found": len(errors) if accuracy_available else None,
        "automatic_root_attachment_missed": missed if accuracy_available else None,
        "incorrect_root_ownership": ownership_errors if accuracy_available else None,
        "attachment_height_absolute_error_m": {
            "count": len(errors),
            "mean": float(np.mean(errors)) if errors else None,
            "median": float(np.median(errors)) if errors else None,
            "maximum": max(errors) if errors else None,
        },
        "pom_height_absolute_error_m": {
            "count": len(errors),
            "mean": float(np.mean(errors)) if errors else None,
            "median": float(np.median(errors)) if errors else None,
            "maximum": max(errors) if errors else None,
        },
        "within_tolerance": {
            f"{float(tolerance):.2f}_m": (sum(error <= float(tolerance) for error in errors) / len(errors) if errors else None)
            for tolerance in tolerances
        },
        "measurement_status_counts": counts,
        "manual_landmarks_override_automatic_for_evaluation": True,
        "automatic_and_manual_landmarks_both_retained": True,
        "lidar_only_review_is_not_field_validation": True,
    }
