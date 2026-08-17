#!/usr/bin/env python3
"""Build the read-only V2 Phase 1.75 pilot review queue and browser evidence.

This script selects and packages existing Phase 1 / Phase 1.5 evidence. It does
not rerun, tune, or alter candidate detection or measurement acceptance.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np


ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
REVIEW_ROOT = ROOT / "site" / "public" / "viewer-v2-review"
PILOT_DATA = REVIEW_ROOT / "data" / "phase1_75"
ALGORITHM_VERSION = "stem-inventory-v2-phase1_75-pilot-review"
SOURCE_ALGORITHM_VERSION = "stem-inventory-v2-phase1_5"
MANUAL_ITEM_ID = "MANUAL-LARGE-ROOT-001"

INPUTS = {
    "outputs/review_queue_v2_phase1_5.json": "054c7479139deff4ca5cc65f5c90ae8403bea53bb7611483fed207e9fc38954d",
    "outputs/tree_tracks_v2_phase1_5.json": "16b09909c7723fda61f610c25789222403e5e463beb508d5eb5895ace053a0a7",
    "outputs/final_63_duplicate_review.csv": "fa8f53366874d445ebfde44b73835a74639fec1ebd061b4752308e146c9be71b",
    "outputs/full_resolution_failure_reasons_v2_phase1_5.csv": "42af62dd866fc90bbe85d7613b785ebf8d9ec9ee5cd9009db9a4a29068c82f58",
    "outputs/sampled_vs_full_metrics_v2_phase1_5.csv": "ef2b0bd9296a03d93402be6f32d74ec703a9ec6afb90fb949877a7bf49d0bcc3",
    "outputs/debug/phase1_5_seed_21.json": "98a772f635c568a64a53212841c4b5e563506f44551427fa623f2a4c933f8476",
    "outputs/debug/phase1_5_seed_162.json": "53d79878b6ae4415743c86643d13c1680a2a0a831008bd778fe87ac5e54e5cf4",
    "outputs/debug/phase1_5_seed_169.json": "807a926ff875bc898722b57ec66fe164878a932225a09ec094a7c098f4b50616",
    "outputs/tree_candidates_v2_phase1.json": "1c084436d316ac562b0805c399ee32c49792091ffd2a6634ee9b819f2db6173a",
    "outputs/candidate_alias_map_v2_phase1_5.json": "39c2aaee724285431c4ef4924f30ac2481c3b31d785f5a0f213670dd00b5e8a0",
}

TARGET_CANDIDATES = {"C-0021", "C-0162", "C-0169", "C-0174"}
CENTRELINE_FAILURE_PILOT = {"C-0045", "C-0091", "C-0495", "C-0576"}
STANDARD_MEDIUM_PILOT = "C-0419"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_ready(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value


def verify_inputs() -> dict[str, str]:
    actual = {}
    for relative, expected in INPUTS.items():
        path = ROOT / relative
        digest = sha256(path)
        if digest != expected:
            raise RuntimeError(f"Locked Phase 1/1.5 input changed: {relative}: {digest} != {expected}")
        actual[relative] = digest
    return actual


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def number(value: Any) -> float | int | None:
    if value in (None, ""):
        return None
    result = float(value)
    return int(result) if result.is_integer() else result


def boolean(value: Any) -> bool:
    return str(value).lower() == "true"


def typed_metrics(row: dict[str, str]) -> dict[str, Any]:
    text_fields = {
        "primary_failure_stage", "primary_failure_reason", "all_failure_reason_codes",
        "phase1_measurement_status", "pom_class", "source_provider", "source_provider_class",
        "track_id", "alias_status", "fit_model", "radius_band", "angular_coverage_band",
        "ground_confidence",
    }
    bool_fields = {"full_resolution_attempted", "full_resolution_accepted", "height_normalization_changed"}
    result: dict[str, Any] = {}
    for key, value in row.items():
        if key == "candidate_id":
            continue
        if key in text_fields:
            result[key] = value or None
        elif key in bool_fields:
            result[key] = boolean(value)
        else:
            result[key] = number(value)
    return result


def residual_stratum(residual: float | None) -> str | None:
    if residual is None:
        return None
    if residual < 0.05:
        return "LOW_LT_0_05_M"
    if 0.10 <= residual < 0.25:
        return "MEDIUM_0_10_TO_LT_0_25_M"
    if residual >= 0.40:
        return "VERY_HIGH_GTE_0_40_M"
    return "INTERMEDIATE_NOT_TARGETED"


def compact_fit(fit: dict | None) -> dict | None:
    if fit is None:
        return None
    return {
        key: fit.get(key)
        for key in [
            "component_index", "center", "radius_m", "circle_residual_m", "inlier_count",
            "angular_coverage_deg", "component_point_count", "valid", "rejection_reasons",
        ]
    }


def line_points(coefficients: list | None, heights: list[float], ground_z: float | None) -> list[list[float]]:
    if coefficients is None or not heights or ground_z is None:
        return []
    start, end = min(heights), max(heights)
    if end <= start:
        values = [start]
    else:
        values = np.linspace(start, end, 40)
    return [
        [
            float(coefficients[0][0] * height + coefficients[0][1]),
            float(coefficients[1][0] * height + coefficients[1][1]),
            float(ground_z + height),
        ]
        for height in values
    ]


def sampled_evidence(candidate: dict, metrics: dict) -> dict:
    diagnostics = candidate.get("diagnostics", {})
    window = diagnostics.get("selected_window") or {}
    selected_by_height = {
        round(float(item["height_m"]), 4): item for item in window.get("selected_slices", [])
    }
    components = []
    all_radii = []
    selected_radii = []
    for profile in diagnostics.get("profile", []):
        height = float(profile["height_m"])
        selected = selected_by_height.get(round(height, 4))
        fit_entries = []
        for fit_index, fit in enumerate(profile.get("fits", [])):
            compact = compact_fit(fit)
            compact["fit_index"] = fit_index
            compact["selected"] = bool(selected and fit_index == selected.get("fit_index"))
            fit_entries.append(compact)
            all_radii.append({
                "height_m": height,
                "radius_m": fit.get("radius_m"),
                "fit_index": fit_index,
                "selected": compact["selected"],
            })
        selected_fit = None
        if selected:
            selected_fit = {
                **selected,
                "centerline_residual_m": float(np.linalg.norm(
                    np.asarray(selected["center"], dtype=float)
                    - np.asarray([
                        window["centreline_coefficients"][0][0] * height + window["centreline_coefficients"][0][1],
                        window["centreline_coefficients"][1][0] * height + window["centreline_coefficients"][1][1],
                    ])
                )),
            }
            selected_radii.append({
                "height_m": height,
                "radius_m": selected.get("radius_m"),
                "circle_residual_m": selected.get("circle_residual_m"),
                "centerline_residual_m": selected_fit["centerline_residual_m"],
            })
        components.append({
            "height_m": height,
            "point_count": profile.get("point_count"),
            "connected_component_count": profile.get("connected_component_count"),
            "candidate_centres": profile.get("candidate_centres", []),
            "fits": fit_entries,
            "selected_fit": selected_fit,
            "slice_rejection_reasons": profile.get("rejection_reasons", []),
            "rejected_components": profile.get("rejected_components", []),
        })
    coefficients = window.get("centreline_coefficients")
    heights = [item["height_m"] for item in selected_radii]
    return {
        "centreline": {
            "available": coefficients is not None,
            "coefficients": coefficients,
            "height_range_m": [min(heights), max(heights)] if heights else None,
            "points_xyz": line_points(coefficients, heights, candidate.get("ground_z_m")),
            "residual_p90_m": metrics.get("sampled_centreline_residual_p90_m"),
        },
        "components_by_height": components,
        "selected_components_by_height": [item for item in components if item["selected_fit"]],
        "radius_profile_all_fits": all_radii,
        "radius_profile_selected": selected_radii,
        "metrics": {key: value for key, value in metrics.items() if key.startswith("sampled_")},
    }


def full_evidence(candidate: dict, metrics: dict) -> dict:
    diagnostics = candidate.get("diagnostics", {})
    full = diagnostics.get("full_resolution") or {}
    validation = diagnostics.get("full_resolution_measurement_validation") or {}
    components = []
    for section in full.get("horizontal_slice_results", []):
        slice_data = section.get("slice") or {}
        selected = section.get("selected_fit")
        components.append({
            "height_m": section.get("height_m"),
            "section_point_count": section.get("section_point_count"),
            "connected_component_count": slice_data.get("connected_component_count"),
            "candidate_centres": slice_data.get("candidate_centres", []),
            "fits": [compact_fit(fit) for fit in slice_data.get("fits", [])],
            "selected_fit": compact_fit(selected),
            "slice_rejection_reasons": slice_data.get("rejection_reasons", []),
            "rejected_components": slice_data.get("rejected_components", []),
        })
    radius_profile = []
    for section in full.get("perpendicular_slice_results", []):
        fit = section.get("selected_fit")
        if fit:
            radius_profile.append({
                "height_m": section.get("height_m"),
                "radius_m": fit.get("radius_m"),
                "circle_residual_m": fit.get("circle_residual_m"),
                "component_index": fit.get("component_index"),
            })
    coefficients = full.get("centreline_coefficients")
    heights = [float(item["height_m"]) for item in components if item.get("height_m") is not None]
    return {
        "available": bool(full),
        "centreline": {
            "available": coefficients is not None,
            "coefficients": coefficients,
            "axis": full.get("centreline_axis"),
            "height_range_m": [min(heights), max(heights)] if heights else None,
            "points_xyz": line_points(coefficients, heights, candidate.get("ground_z_m")),
            "residual_p90_m": metrics.get("full_centreline_residual_p90_m"),
            "residual_limit_m": validation.get("centreline_residual_limit_m"),
        },
        "components_by_height": components,
        "selected_components_by_height": [item for item in components if item["selected_fit"]],
        "radius_profile_selected": radius_profile,
        "validation": validation,
        "metrics": {key: value for key, value in metrics.items() if key.startswith("full_")},
        "accepted_point_count": full.get("accepted_point_count", 0),
        "rejected_point_count": full.get("rejected_point_count", 0),
    }


def compact_track(track: dict | None) -> dict | None:
    if not track:
        return None
    return {
        key: track.get(key)
        for key in [
            "track_id", "canonical_track_id", "candidate_geometry_status", "identity_status",
            "measurement_status", "reason_codes", "source_candidate_ids", "source_seed_ids",
            "source_providers", "source_height_count", "source_heights_m", "vertical_span_m", "gaps",
            "reference_height_m", "reference_center", "centreline_coefficients", "radius_coefficients",
            "centre_residual_p90_m", "radius_residual_mad_m", "median_radius_m",
            "median_angular_coverage_deg", "median_fit_residual_m", "alias_track_ids", "observations",
        ]
    }


def evenly_sample(points: np.ndarray, maximum: int = 6000) -> np.ndarray:
    if len(points) <= maximum:
        return np.round(points, 4)
    indexes = np.linspace(0, len(points) - 1, maximum, dtype=int)
    return np.round(points[indexes], 4)


def build_crop(candidate: dict, sampled_points: np.ndarray, spatial_tree: Any) -> dict:
    center = [candidate["position"]["x"], candidate["position"]["y"]]
    indexes = spatial_tree.query_ball_point(center, r=1.25)
    local = sampled_points[np.asarray(indexes, dtype=int)] if indexes else np.empty((0, 3))
    ground = candidate.get("ground_z_m")
    if ground is not None and len(local):
        local = local[(local[:, 2] >= ground + 0.30) & (local[:, 2] <= ground + 3.70)]
    accepted = rejected = np.empty((0, 3))
    point_file = candidate.get("full_resolution_point_file")
    if point_file and Path(point_file).exists():
        with np.load(point_file) as point_data:
            accepted = np.asarray(point_data["accepted_points_xyz"])
            rejected = np.asarray(point_data["rejected_points_xyz"])
    return {
        "candidate_id": candidate["candidate_id"],
        "sampled_points_xyz": evenly_sample(local),
        "full_accepted_points_xyz": evenly_sample(accepted),
        "full_rejected_points_xyz": evenly_sample(rejected),
        "counts_before_display_sampling": {
            "sampled": len(local), "full_accepted": len(accepted), "full_rejected": len(rejected),
        },
    }


def main() -> None:
    locked_hashes = verify_inputs()
    phase15_queue = read_json(OUTPUTS / "review_queue_v2_phase1_5.json")
    queue_by_id = {item["candidate_id"]: item for item in phase15_queue["entries"]}
    tracks_payload = read_json(OUTPUTS / "tree_tracks_v2_phase1_5.json")
    tracks = tracks_payload.get("tracks", tracks_payload.get("entries", []))
    tracks_by_id = {item["track_id"]: item for item in tracks}
    alias_payload = read_json(OUTPUTS / "candidate_alias_map_v2_phase1_5.json")
    alias_by_candidate = {
        item["phase1_candidate_id"]: item for item in alias_payload["candidate_aliases"]
    }
    pair_rows = csv_rows(OUTPUTS / "final_63_duplicate_review.csv")
    metrics_rows = csv_rows(OUTPUTS / "sampled_vs_full_metrics_v2_phase1_5.csv")
    failure_rows = csv_rows(OUTPUTS / "full_resolution_failure_reasons_v2_phase1_5.csv")
    metrics_by_id = {row["candidate_id"]: typed_metrics(row) for row in metrics_rows}
    failures_by_id = {row["candidate_id"]: typed_metrics(row) for row in failure_rows}
    phase1_payload = read_json(OUTPUTS / "tree_candidates_v2_phase1.json")
    candidates_by_id = {item["candidate_id"]: item for item in phase1_payload["candidates"]}

    adaptive = {
        candidate_id for candidate_id, item in queue_by_id.items()
        if item.get("measurement_status") == "MEASURABLE_ADAPTIVE_HEIGHT"
    }
    definite_pairs = [row for row in pair_rows if row["classification"] == "DEFINITE_ALIAS"]
    probable_pairs = [row for row in pair_rows if row["classification"] == "PROBABLE_ALIAS_REQUIRES_REVIEW"]
    definite_members = {value for row in definite_pairs for value in [row["candidate_a"], row["candidate_b"]]}
    probable_members = {value for row in probable_pairs for value in [row["candidate_a"], row["candidate_b"]]}
    raw_mandatory = list(adaptive) + ["C-0174", "C-0021", "C-0162", "C-0169"]
    raw_mandatory += [value for row in definite_pairs for value in [row["candidate_a"], row["candidate_b"]]]
    raw_mandatory += [value for row in probable_pairs for value in [row["candidate_a"], row["candidate_b"]]]
    mandatory = set(raw_mandatory)
    selected_candidate_ids = mandatory | CENTRELINE_FAILURE_PILOT | {STANDARD_MEDIUM_PILOT}
    if len(selected_candidate_ids) != 39:
        raise AssertionError(f"Expected 39 unique candidates, got {len(selected_candidate_ids)}")

    aliases_by_id: dict[str, list[dict]] = defaultdict(list)
    for row in definite_pairs + probable_pairs:
        aliases_by_id[row["candidate_a"]].append(row)
        aliases_by_id[row["candidate_b"]].append(row)

    entries = []
    evidence_payloads: dict[str, dict] = {}
    for candidate_id in sorted(selected_candidate_ids):
        candidate = candidates_by_id[candidate_id]
        metrics = metrics_by_id.get(candidate_id, {})
        failure = failures_by_id.get(candidate_id, metrics)
        alias_mapping = alias_by_candidate[candidate_id]
        canonical_track_id = alias_mapping.get("canonical_track_id")
        track = tracks_by_id.get(canonical_track_id)
        source = queue_by_id.get(candidate_id) or {
            "queue_id": f"RQ15-{candidate_id[2:]}",
            "candidate_id": candidate_id,
            "candidate_geometry_status": (
                track.get("candidate_geometry_status") if track else "INSUFFICIENT_EVIDENCE"
            ),
            "identity_status": track.get("identity_status") if track else "UNVERIFIED",
            "canonical_phase1_candidate_id": alias_mapping.get(
                "canonical_phase1_candidate_id", candidate_id
            ),
            "measurement_status": candidate.get("measurement_status"),
            "measurement_rule": candidate.get("measurement_rule"),
            "measurement_height_m": candidate.get("measurement_height_m"),
            "source_providers": candidate.get("seed_sources", []),
            "source_height_count_phase1": len(candidate.get("source_heights_m", [])),
            "track_id": canonical_track_id,
            "track_source_height_count": track.get("source_height_count", 0) if track else 0,
            "phase1_candidate_fragmented_across_tracks": alias_mapping.get(
                "phase1_candidate_fragmented_across_tracks", False
            ),
            "potential_duplicate": candidate_id in definite_members | probable_members,
            "position": candidate.get("position"),
            "ground_z_m": candidate.get("ground_z_m"),
            "sampled_radius_m": metrics.get("sampled_radius_m"),
            "review_question": (
                "Does this existing geometry represent a main stem contaminated by roots or branches, "
                "or is the selected full-resolution component wrong?"
            ),
        }
        categories = []
        selection_reasons = []
        if candidate_id in adaptive:
            categories.append("ADAPTIVE_HEIGHT_ACCEPTED_GEOMETRY")
            selection_reasons.append("All 16 Phase 1.5 adaptive-height accepted geometry measurements")
        if candidate_id == "C-0174":
            categories.append("REQUIRED_CANDIDATE_C_0174")
            selection_reasons.append("Explicit required candidate C-0174")
        if candidate_id in {"C-0021", "C-0162", "C-0169"}:
            categories.append("REQUIRED_SEED_TRACE")
            selection_reasons.append(f"Explicit required Phase 1.5 debug seed {int(candidate_id[2:])}")
        if candidate_id in definite_members:
            categories.append("DEFINITE_ALIAS_MEMBER")
            selection_reasons.append("Member of a definite Phase 1.5 measurement alias pair")
        if candidate_id in probable_members:
            categories.append("PROBABLE_ALIAS_MEMBER")
            selection_reasons.append("Member of a highest-ranked probable Phase 1.5 alias pair")
        standard_accepted = (
            metrics.get("full_resolution_accepted") is True and metrics.get("pom_class") == "STANDARD"
        )
        if standard_accepted:
            band = metrics.get("radius_band")
            if band == "<0.10":
                categories.append("STANDARD_ACCEPTED_SMALL_RADIUS")
            elif band == "0.10-<0.20":
                categories.append("STANDARD_ACCEPTED_MEDIUM_RADIUS")
            selection_reasons.append(f"Standard-height accepted sample in existing {band} sampled-radius band")
        if candidate_id in CENTRELINE_FAILURE_PILOT:
            categories.append("HIGH_QUALITY_SAMPLED_FULL_CENTRELINE_FAILURE")
            selection_reasons.append("High-quality sampled fit whose primary full-resolution failure is centreline residual")
        stratum = residual_stratum(metrics.get("full_centreline_residual_p90_m"))
        if stratum in {"LOW_LT_0_05_M", "MEDIUM_0_10_TO_LT_0_25_M", "VERY_HIGH_GTE_0_40_M"}:
            categories.append(f"FULL_RESIDUAL_{stratum}")
        if (metrics.get("radius_delta_m") or 0) >= 0.30:
            categories.append("STRONG_SAMPLED_TO_FULL_RADIUS_EXPANSION")
        if metrics.get("pom_class") == "ADAPTIVE" and (metrics.get("selected_measurement_height_m") or 0) >= 1.95:
            categories.append("ADAPTIVE_POM_NEAR_OR_ABOVE_2_0_M")
        entry = {
            **source,
            "review_item_id": f"RQ175-{candidate_id[2:]}",
            "item_type": "CANDIDATE_EVIDENCE",
            "priority": 1 if candidate_id in TARGET_CANDIDATES else 2,
            "categories": categories,
            "selection_reasons": selection_reasons,
            "sampled_metrics": {key: value for key, value in metrics.items() if key.startswith("sampled_")},
            "full_metrics": {key: value for key, value in metrics.items() if key.startswith("full_")},
            "comparison_metrics": {
                "center_shift_m": metrics.get("center_shift_m"),
                "radius_delta_m": metrics.get("radius_delta_m"),
                "centreline_residual_delta_m": metrics.get("centreline_residual_delta_m"),
                "full_rejected_point_fraction": metrics.get("full_rejected_point_fraction"),
            },
            "residual_stratum": stratum,
            "alias_relationships": aliases_by_id.get(candidate_id, []),
            "failure": {
                "primary_stage": failure.get("primary_failure_stage"),
                "primary_reason": failure.get("primary_failure_reason"),
                "all_reason_codes": (failure.get("all_failure_reason_codes") or "").split("|") if failure else [],
            },
            "evidence_url": f"data/phase1_75/evidence/{candidate_id}.json",
            "point_crop_url": f"data/phase1_75/points/{candidate_id}.json",
        }
        entries.append(entry)
        evidence_payloads[candidate_id] = {
            "algorithm_version": ALGORITHM_VERSION,
            "source_algorithm_version": SOURCE_ALGORITHM_VERSION,
            "interpretation": "EXISTING GEOMETRY EVIDENCE FOR HUMAN REVIEW; NOT A VERIFIED TREE",
            "candidate_id": candidate_id,
            "position": candidate.get("position"),
            "ground_z_m": candidate.get("ground_z_m"),
            "selected_pom_m": candidate.get("measurement_height_m"),
            "reference_plane_height_m": 1.30,
            "sampled": sampled_evidence(candidate, metrics),
            "full_resolution": full_evidence(candidate, metrics),
            "track": compact_track(track),
            "alias_relationships": aliases_by_id.get(candidate_id, []),
            "failure": entry["failure"],
            "phase1_reason_codes": candidate.get("reason_codes", []),
        }

    entries.sort(key=lambda item: (item["priority"], item["candidate_id"]))
    entries.append({
        "review_item_id": MANUAL_ITEM_ID,
        "item_type": "MANUAL_SEED_PLACEHOLDER",
        "candidate_id": None,
        "priority": 3,
        "categories": ["MANUAL_VISUALLY_CONFIRMED_LARGE_ROOT_WORKFLOW"],
        "selection_reasons": [
            "Placeholder for a reviewer-clicked large mangrove with roots above 1.30 m and a clean upper stem near 2.50 m"
        ],
        "candidate_geometry_status": "HUMAN_PLACEHOLDER_NOT_EVALUATED",
        "identity_status": "UNVERIFIED",
        "measurement_status": "NOT_MEASURED",
        "measurement_rule": None,
        "measurement_height_m": None,
        "source_providers": ["MANUAL_REVIEW_CLICK"],
        "position": None,
        "ground_z_m": None,
        "point_crop_url": None,
        "evidence_url": None,
        "manual_seed_defaults": {
            "approximate_xy": None,
            "clean_height_hint_m": 2.50,
            "human_label": "TRUE_MAIN_STEM",
            "reviewer_note": "",
            "hint_is_automatic_final_pom": False,
        },
        "review_question": "Does the visually known large-root mangrove form a stable upper stem track?",
    })

    category_counts = Counter(category for item in entries for category in item["categories"])
    residual_counts = Counter(
        item.get("residual_stratum") for item in entries if item.get("residual_stratum")
    )
    standard_accepted_rows = [
        row for row in metrics_by_id.values()
        if row.get("full_resolution_accepted") is True and row.get("pom_class") == "STANDARD"
    ]
    standard_available = Counter(row.get("radius_band") for row in standard_accepted_rows)
    payload = {
        "algorithm_version": ALGORITHM_VERSION,
        "source_algorithm_version": SOURCE_ALGORITHM_VERSION,
        "interpretation": "FOCUSED HUMAN PILOT REVIEW QUEUE; ITEMS ARE NOT VERIFIED TREES",
        "locked_input_hashes": locked_hashes,
        "queue_size": len(entries),
        "unique_candidate_count": len(selected_candidate_ids),
        "manual_placeholder_count": 1,
        "duplicate_inclusions_removed": len(raw_mandatory) - len(mandatory),
        "mandatory_inclusion_count_before_deduplication": len(raw_mandatory),
        "mandatory_unique_candidate_count": len(mandatory),
        "category_counts": dict(sorted(category_counts.items())),
        "residual_stratum_counts": dict(sorted(residual_counts.items())),
        "selection_thresholds_for_review_strata_only": {
            "low_full_centreline_residual_m": "<0.05",
            "medium_full_centreline_residual_m": ">=0.10 and <0.25",
            "very_high_full_centreline_residual_m": ">=0.40",
            "strong_sampled_to_full_radius_expansion_m": ">=0.30",
            "adaptive_pom_near_or_above_m": ">=1.95",
            "note": "Review selection labels only; no measurement threshold or acceptance behavior changed.",
        },
        "standard_height_accepted_population_by_existing_radius_band": dict(sorted(standard_available.items())),
        "known_selection_gap": {
            "category": "STANDARD_ACCEPTED_LARGE_RADIUS",
            "available_count": standard_available.get(">=0.30", 0),
            "handling": "No substitute was relabelled as accepted; the only medium-band item C-0419 and small-band accepted items are included.",
        },
        "review_questions": [
            "Are accepted geometry measurements actually main stems?",
            "Are definite alias pairs really the same physical stem?",
            "Are centreline failures real stems contaminated by roots or branches?",
            "Is the selected full-resolution component wrong?",
            "Does the known large-root tree form a stable upper stem track?",
        ],
        "annotation_export_path": "annotations/phase1_75_pilot_review.json",
        "entries": entries,
    }
    write_json(OUTPUTS / "review_queue_v2_phase1_75_pilot.json", payload)
    write_json(PILOT_DATA / "review_queue.json", payload)
    for candidate_id, evidence in evidence_payloads.items():
        write_json(PILOT_DATA / "evidence" / f"{candidate_id}.json", evidence)

    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import analyze_samutsongkhram_trees as viewer_source
    from scipy.spatial import cKDTree

    sampled_points = viewer_source.load_positions()
    spatial_tree = cKDTree(sampled_points[:, :2])
    for candidate_id in sorted(selected_candidate_ids):
        write_json(
            PILOT_DATA / "points" / f"{candidate_id}.json",
            build_crop(candidates_by_id[candidate_id], sampled_points, spatial_tree),
        )

    write_json(REVIEW_ROOT / "data" / "queues.json", {
        "default_queue_id": "phase1_75_pilot",
        "queues": [
            {
                "queue_id": "phase1_75_pilot",
                "label": "Phase 1.75 pilot · 40 review items",
                "url": "data/phase1_75/review_queue.json",
            },
            {
                "queue_id": "phase1_5_full",
                "label": "Phase 1.5 full · 460 candidates",
                "url": "data/review_queue.json",
            },
        ],
    })
    print(json.dumps({
        "queue_size": len(entries),
        "unique_candidates": len(selected_candidate_ids),
        "duplicate_inclusions_removed": len(raw_mandatory) - len(mandatory),
        "category_counts": dict(sorted(category_counts.items())),
    }, indent=2))


if __name__ == "__main__":
    main()
