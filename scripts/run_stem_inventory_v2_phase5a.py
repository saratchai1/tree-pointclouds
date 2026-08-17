#!/usr/bin/env python3
"""Build additive Phase 5A protocol-POM shadow outputs and review assets."""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import numpy as np
from scipy.spatial import cKDTree
import yaml

import analyze_samutsongkhram_trees as viewer_source
import build_phase1_75_pilot_review as review_source
import stem_inventory_v2_phase5a as phase5a


ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
ANNOTATIONS = ROOT / "annotations"
VIEWER_DATA = ROOT / "site/public/viewer-v2-review/data"
PHASE5A_DATA = VIEWER_DATA / "phase5a"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ready = review_source.json_ready(payload)
    path.write_text(json.dumps(ready, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def payload_sha256(payload: Any) -> str:
    ready = review_source.json_ready(payload)
    encoded = json.dumps(ready, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def tree_ground_z(tree: dict, candidate_by_id: dict[str, dict], candidates: list[dict]) -> float:
    direct = [
        candidate_by_id[candidate_id].get("ground_z_m")
        for candidate_id in tree.get("source_candidates", [])
        if candidate_id in candidate_by_id and candidate_by_id[candidate_id].get("ground_z_m") is not None
    ]
    if direct:
        return float(np.median(np.asarray(direct, dtype=float)))
    center = tree["center"]
    nearby = [
        candidate["ground_z_m"]
        for candidate in candidates
        if candidate.get("ground_z_m") is not None
        and np.hypot(candidate["position"]["x"] - center["x"], candidate["position"]["y"] - center["y"]) <= 1.5
    ]
    if nearby:
        return float(np.median(np.asarray(nearby, dtype=float)))
    return float(np.median([candidate["ground_z_m"] for candidate in candidates if candidate.get("ground_z_m") is not None]))


def local_points_for_tree(
    tree: dict,
    sampled_points: np.ndarray,
    spatial_index: cKDTree,
    radius_m: float,
) -> np.ndarray:
    indexes = spatial_index.query_ball_point([tree["center"]["x"], tree["center"]["y"]], r=radius_m)
    return sampled_points[np.asarray(indexes, dtype=int)] if indexes else np.empty((0, 3), dtype=float)


def compact_track_line(track: dict, ground_z_m: float) -> list[list[float]]:
    coefficients = track.get("centreline_coefficients")
    heights = [float(value) for value in track.get("source_heights_m", [])]
    return review_source.line_points(coefficients, heights, ground_z_m) if coefficients and heights else []


def axis_line(axis: dict, count: int = 50) -> list[list[float]]:
    start, end = map(float, axis["vertical_range_agl_m"])
    if end <= start:
        heights = [start]
    else:
        heights = np.linspace(start, end, count)
    return [phase5a.axis_center_at_height(axis, float(height)).tolist() for height in heights]


def review_crop(tree: dict, ground_z_m: float, local_points: np.ndarray, config: dict) -> dict:
    cfg = config["viewer"]
    if len(local_points):
        local_points = local_points[
            (local_points[:, 2] >= ground_z_m + float(cfg["minimum_height_agl_m"]))
            & (local_points[:, 2] <= ground_z_m + float(cfg["maximum_height_agl_m"]))
        ]
    return {
        "review_item_id": tree["tree_id"],
        "sampled_points_xyz": review_source.evenly_sample(local_points, int(cfg["maximum_crop_points"])),
        "full_accepted_points_xyz": [],
        "full_rejected_points_xyz": [],
        "counts_before_display_sampling": {
            "sampled": len(local_points),
            "full_accepted": 0,
            "full_rejected": 0,
        },
        "source": "SAMPLED_BROWSER_POINT_CLOUD_LOADED_ONCE_AND_SPATIALLY_INDEXED",
        "full_las_rescan_per_tree": False,
    }


def phase5a_evidence(
    record: dict,
    candidates: list[dict],
    measurement_evidence: dict,
    track_by_id: dict[str, dict],
    config: dict,
) -> dict:
    ground = float(record["main_stem"]["ground_z_m"])
    root_track_ids = sorted({track_id for row in candidates for track_id in row.get("source_root_track_ids", [])})
    root_tracks = [track_by_id[track_id] for track_id in root_track_ids if track_id in track_by_id]
    maximum_slice_points = int(config["viewer"]["maximum_slice_points"])
    slice_points = np.asarray(measurement_evidence.get("extracted_points_xyz", []), dtype=float)
    if slice_points.size == 0:
        slice_points = np.empty((0, 3))
    return {
        "algorithm_version": phase5a.ALGORITHM_VERSION,
        "interpretation": "PHASE 5A SHADOW PROTOCOL EVIDENCE; LIDAR ESTIMATE — NOT FIELD VERIFIED",
        "tree_id": record["tree_id"],
        "ground_z_m": ground,
        "sampled": {
            "components_by_height": [],
            "radius_profile_selected": [],
            "centreline": {
                "points_xyz": axis_line(record["main_stem"]),
                "residual_p90_m": record["main_stem"]["axis_uncertainty_m"],
            },
        },
        "full_resolution": {
            "components_by_height": [],
            "radius_profile_selected": [],
            "centreline": {"points_xyz": []},
            "not_loaded_reason": "PHASE5A_REUSES_SHARED_SAMPLED_INDEX_AND_EXISTING_CACHES_WITHOUT_A_PER_TREE_LAS_SCAN",
        },
        "phase5a": {
            "measurement_protocol": record["measurement_protocol"],
            "main_stem": record["main_stem"],
            "main_stem_centerline_points_xyz": axis_line(record["main_stem"]),
            "candidate_root_tracks": [
                {
                    "track_id": track["track_id"],
                    "points_xyz": compact_track_line(track, ground),
                    "source_candidate_ids": track.get("source_candidate_ids", []),
                }
                for track in root_tracks
            ],
            "attachment_candidates": candidates,
            "highest_prop_root_attachment": record["highest_prop_root_attachment"],
            "protocol_pom": record["protocol_pom"],
            "measurement": record["measurement"],
            "historical_measurement": record["historical_measurement"],
            "measurement_plane": {
                "center_xyz": measurement_evidence.get("exact_protocol_pom_position_xyz"),
                "normal": measurement_evidence.get("local_stem_direction"),
                "basis_u": measurement_evidence.get("plane_basis_u"),
                "basis_v": measurement_evidence.get("plane_basis_v"),
            },
            "extracted_cross_section_points_xyz": review_source.evenly_sample(slice_points, maximum_slice_points),
            "fit": measurement_evidence.get("exact_fit"),
            "qa_variants": measurement_evidence.get("qa_variants", []),
            "field_verified": False,
            "display_label": "LiDAR estimate — not field verified",
        },
    }


def build_review_queue(records: list[dict], roi_annotation: dict) -> tuple[dict, set[str], dict[str, list[str]]]:
    certain_roi_refs = [row for row in roi_annotation.get("reference_trees", []) if row.get("confidence") == "CERTAIN"]
    roi_refs_by_tree: dict[str, list[str]] = defaultdict(list)
    for reference in certain_roi_refs:
        for tree_id in reference.get("matched_tree_ids", []):
            roi_refs_by_tree[tree_id].append(reference["reference_tree_id"])
    selected_ids = {
        row["tree_id"] for row in records
        if row["measurement_protocol"]["applicability"] in {
            "PROP_ROOT_PROTOCOL_APPLICABLE", "PROTOCOL_APPLICABILITY_UNCERTAIN"
        }
        or row.get("manual_seed_ids")
        or row["tree_id"] in roi_refs_by_tree
    }
    entries = []
    for record in records:
        if record["tree_id"] not in selected_ids:
            continue
        applicability = record["measurement_protocol"]["applicability"]
        attachment = record["highest_prop_root_attachment"]
        categories = ["PHASE5A_PROTOCOL_APPLICABILITY_REVIEW"]
        if applicability == "PROP_ROOT_PROTOCOL_APPLICABLE":
            categories.append("PROP_ROOT_PROTOCOL_APPLICABLE")
        if applicability == "PROTOCOL_APPLICABILITY_UNCERTAIN":
            categories.append("PROTOCOL_APPLICABILITY_UNCERTAIN")
        if record.get("manual_seed_ids"):
            categories.append("MANUAL_SEED_REGRESSION")
        if record["tree_id"] in roi_refs_by_tree:
            categories.append("ROI_A_REFERENCE_TREE")
        entries.append({
            "review_item_id": record["tree_id"],
            "item_type": "PHASE5A_PROP_ROOT_POM",
            "candidate_id": None,
            "priority": 1 if applicability in {"PROP_ROOT_PROTOCOL_APPLICABLE", "PROTOCOL_APPLICABILITY_UNCERTAIN"} else 2,
            "categories": sorted(categories),
            "candidate_geometry_status": attachment["status"],
            "identity_status": record["tree_detection_status"],
            "measurement_status": record["measurement"]["status"],
            "measurement_rule": phase5a.PROTOCOL_ID,
            "source_providers": ["PHASE1_5_VERTICAL_TRACKS", "PHASE4C_RELATIONAL_GEOMETRY_SHADOW", "PHASE5A_PROTOCOL_SHADOW"],
            "position": record["tree_center"],
            "ground_z_m": record["main_stem"]["ground_z_m"],
            "point_crop_url": f"data/phase5a/points/{record['tree_id']}.json",
            "evidence_url": f"data/phase5a/evidence/{record['tree_id']}.json",
            "phase4_tree_id": record["tree_id"],
            "tree_detection_status": record["tree_detection_status"],
            "protocol_applicability": applicability,
            "attachment_status": attachment["status"],
            "protocol_pom_status": record["protocol_pom"]["status"],
            "protocol_offset_m": record["measurement_protocol"]["offset_m"],
            "historical_measurement": record["historical_measurement"],
            "roi_a_reference_ids": sorted(roi_refs_by_tree.get(record["tree_id"], [])),
            "review_question": "Confirm protocol applicability, the highest prop-root attachment, +0.30 m POM, and exact-plane measurement QA.",
        })
    entries.sort(key=lambda row: (row["priority"], row["review_item_id"]))
    queue = {
        "algorithm_version": phase5a.ALGORITHM_VERSION,
        "mode": "SHADOW",
        "interpretation": "ROOT-ATTACHMENT AND PROTOCOL-POM REVIEW; LIDAR ESTIMATES ARE NOT FIELD VERIFIED",
        "annotation_export_path": "annotations/phase5a_prop_root_pom_review.json",
        "queue_size": len(entries),
        "unique_tree_id_count": len(entries),
        "roi_a_evaluable_reference_count": len(certain_roi_refs),
        "annotation_schema": {
            "protocol_applicability": sorted(phase5a.APPLICABILITY_STATES),
            "attachment_status": ["CONFIRMED", "PROBABLE", "NEEDS_REVIEW", "NOT_VISIBLE", "CONFLICTING_ROOT_OWNERSHIP"],
            "measurement_decision": ["ACCEPT", "REJECT", "UNCERTAIN"],
        },
        "entries": entries,
    }
    return queue, selected_ids, roi_refs_by_tree


def main() -> int:
    paths = {
        "config": ROOT / "config/stem_inventory_v2_phase5a.yaml",
        "phase1_config": ROOT / "config/stem_inventory_v2.yaml",
        "inventory": OUTPUTS / "phase4_tree_inventory.json",
        "registry": OUTPUTS / "phase3_tree_id_registry.json",
        "tracks": OUTPUTS / "tree_tracks_v2_phase1_5.json",
        "associations": OUTPUTS / "phase3_candidate_tree_associations.json",
        "candidates": OUTPUTS / "tree_candidates_v2_phase1.json",
        "phase1_annotations": ANNOTATIONS / "phase1_75_pilot_review.json",
        "phase4b_annotation": ANNOTATIONS / "phase4_ground_truth_roi.json",
        "phase4b_evaluation": OUTPUTS / "phase4b_roi_evaluation.json",
        "phase4c_parent_graph": OUTPUTS / "phase4c_parent_attachment_graph.json",
        "phase4c_classification": OUTPUTS / "phase4c_structure_classification_shadow.json",
        "phase2_manual": OUTPUTS / "manual_seed_evaluations_v2_phase2_anchor_pilot.json",
        "phase2_recheck": OUTPUTS / "phase2_manual_anchor_measurement_recheck.json",
        "phase5a_annotations": ANNOTATIONS / "phase5a_prop_root_pom_review.json",
    }
    protected_keys = [
        "inventory", "registry", "tracks", "associations", "phase1_annotations",
        "phase4b_annotation", "phase4b_evaluation", "phase4c_parent_graph",
        "phase4c_classification", "phase2_manual", "phase2_recheck",
    ]
    protected_before = {str(paths[key].relative_to(ROOT)): sha256(paths[key]) for key in protected_keys}
    input_hashes = {str(path.relative_to(ROOT)): sha256(path) for path in paths.values()}
    config = phase5a.load_config(paths["config"])
    phase1_config = yaml.safe_load(paths["phase1_config"].read_text(encoding="utf-8"))
    inventory = read_json(paths["inventory"])
    registry_before = read_json(paths["registry"])
    tracks_payload = read_json(paths["tracks"])
    associations = read_json(paths["associations"])
    candidates_payload = read_json(paths["candidates"])
    phase1_annotations = read_json(paths["phase1_annotations"])
    roi_annotation = read_json(paths["phase4b_annotation"])
    phase4b_evaluation = read_json(paths["phase4b_evaluation"])
    parent_graph = read_json(paths["phase4c_parent_graph"])
    classifications = read_json(paths["phase4c_classification"])
    manual_payload = read_json(paths["phase2_manual"])
    phase2_recheck = read_json(paths["phase2_recheck"])
    phase5a_annotations = read_json(paths["phase5a_annotations"])

    trees = sorted(inventory["trees"], key=lambda row: row["tree_id"])
    track_by_id = {row["track_id"]: row for row in tracks_payload["tracks"]}
    candidates = candidates_payload["candidates"]
    candidate_by_id = {row["candidate_id"]: row for row in candidates}
    human_labels = {row["candidate_id"]: row.get("human_label") for row in phase1_annotations["annotations"]}
    manual_reviews = {row["tree_id"]: row for row in phase5a_annotations.get("annotations", []) if row.get("tree_id")}
    manual_evaluations = manual_payload.get("evaluations", [])
    evaluation_by_seed = {
        seed_id: evaluation
        for evaluation in manual_evaluations
        for seed_id in evaluation.get("source_seed_ids", [])
    }
    root_edges_by_parent: dict[str, list[dict]] = defaultdict(list)
    for edge in parent_graph.get("edges", []):
        if edge.get("proposed_attachment_class") == "PROP_ROOT_ATTACHED":
            root_edges_by_parent[edge["parent_tree_id"]].append(edge)
    supported_root_parent_ids = set(root_edges_by_parent)

    # One shared point-cloud read and one shared XY spatial index serve every tree.
    sampled_points = viewer_source.load_positions()
    spatial_index = cKDTree(sampled_points[:, :2])
    ground_by_tree = {
        tree["tree_id"]: tree_ground_z(tree, candidate_by_id, candidates)
        for tree in trees
    }
    records = []
    all_candidates = []
    measurement_evidence_by_tree = {}
    for tree in trees:
        tree_id = tree["tree_id"]
        historical = next(
            (evaluation_by_seed[seed_id] for seed_id in tree.get("manual_seed_ids", []) if seed_id in evaluation_by_seed),
            None,
        )
        axis = phase5a.estimate_main_stem_axis(tree, track_by_id, ground_by_tree[tree_id], config)
        applicability = phase5a.resolve_protocol_applicability(
            tree, human_labels, supported_root_parent_ids,
            manual_reviews.get(tree_id), historical,
        )
        root_candidates = []
        for edge in root_edges_by_parent.get(tree_id, []):
            candidate = phase5a.candidate_from_phase4c_relationship(edge, axis, config)
            if candidate:
                root_candidates.append(candidate)
        root_candidates.extend(phase5a.candidates_from_human_root_transitions(
            tree, axis, track_by_id, human_labels, config
        ))
        profile_candidate = phase5a.candidate_from_historical_profile_transition(
            tree, axis, historical, config
        )
        if profile_candidate:
            root_candidates.append(profile_candidate)
        all_candidates.extend(root_candidates)
        attachment = phase5a.select_highest_supported_attachment(
            tree_id, applicability["applicability"], root_candidates, axis, config,
            manual_reviews.get(tree_id),
        )
        pom = phase5a.calculate_protocol_pom(attachment, axis, config)
        local = local_points_for_tree(
            tree, sampled_points, spatial_index,
            float(config["cross_section"]["spatial_query_radius_m"]),
        )
        measurement, measurement_evidence = phase5a.fit_cross_section_at_exact_pom(
            tree_id, local, pom, axis, phase1_config, config
        )
        measurement = phase5a.apply_measurement_review(measurement, manual_reviews.get(tree_id))
        measurement_evidence_by_tree[tree_id] = measurement_evidence
        historical_measurement = deepcopy(tree.get("measurement", {}))
        old_pom = historical_measurement.get("pom_m")
        proposed_pom = pom.get("height_agl_m") if pom.get("status") == "COMPUTED" else None
        pom_difference = abs(float(old_pom) - float(proposed_pom)) if old_pom is not None and proposed_pom is not None else None
        match_tolerance = float(config["cross_section"]["historical_pom_match_tolerance_m"])
        record = {
            "tree_id": tree_id,
            "tree_center": deepcopy(tree["center"]),
            "tree_detection_status": tree["detection"]["status"],
            "manual_seed_ids": deepcopy(tree.get("manual_seed_ids", [])),
            "measurement_protocol": phase5a.protocol_record(config, applicability["applicability"]),
            "protocol_applicability_evidence": applicability,
            "main_stem": axis,
            "highest_prop_root_attachment": attachment,
            "protocol_pom": pom,
            "measurement": measurement,
            "historical_measurement": historical_measurement,
            "historical_comparison": {
                "old_pom_m": old_pom,
                "protocol_pom_m": proposed_pom,
                "absolute_pom_difference_m": pom_difference,
                "match_tolerance_m": match_tolerance,
                "old_plane_matches_protocol_pom": pom_difference is not None and pom_difference <= match_tolerance,
                "historical_measurement_reused": False,
                "historical_measurement_is_current_protocol_truth": False,
                "reason": "PHASE5A_REQUIRES_A_RESOLVED_ATTACHMENT_AND_EXACT_PROTOCOL_PLANE_FIT",
            },
            "provenance": {
                "source_candidate_ids": deepcopy(tree.get("source_candidates", [])),
                "source_track_ids": deepcopy(tree["stem"].get("source_track_ids", [])),
                "algorithm_version": phase5a.ALGORITHM_VERSION,
                "config_version": config["config_version"],
                "phase4c_global_enforcement_enabled": False,
                "source_las_rescan_for_tree": False,
            },
        }
        records.append(record)

    evaluation = phase5a.evaluate_phase5a(
        records,
        phase5a_annotations.get("annotations", []),
        config["evaluation"]["attachment_height_tolerances_m"],
    )
    review_queue, review_ids, roi_refs_by_tree = build_review_queue(records, roi_annotation)
    record_by_id = {row["tree_id"]: row for row in records}
    candidates_by_tree: dict[str, list[dict]] = defaultdict(list)
    for candidate in all_candidates:
        candidates_by_tree[candidate["tree_id"]].append(candidate)
    for tree in trees:
        tree_id = tree["tree_id"]
        if tree_id not in review_ids:
            continue
        local = local_points_for_tree(
            tree, sampled_points, spatial_index, float(config["viewer"]["crop_radius_m"])
        )
        write_json(PHASE5A_DATA / "points" / f"{tree_id}.json", review_crop(
            tree, ground_by_tree[tree_id], local, config
        ))
        write_json(PHASE5A_DATA / "evidence" / f"{tree_id}.json", phase5a_evidence(
            record_by_id[tree_id], candidates_by_tree[tree_id], measurement_evidence_by_tree[tree_id], track_by_id, config
        ))

    detection_counts = Counter(row["tree_detection_status"] for row in records)
    applicability_counts = Counter(row["measurement_protocol"]["applicability"] for row in records)
    attachment_counts = Counter(row["highest_prop_root_attachment"]["status"] for row in records)
    measurement_counts = Counter(row["measurement"]["status"] for row in records)
    common = {
        "algorithm_version": phase5a.ALGORITHM_VERSION,
        "mode": "SHADOW",
        "input_provenance": input_hashes,
        "source_tree_count": len(records),
        "stable_tree_ids_changed": False,
        "source_las_scan_count": 0,
        "sampled_browser_cloud_read_count": 1,
        "per_tree_spatial_index_queries": len(records),
    }
    applicability_payload = {
        **common,
        "interpretation": "PROTOCOL APPLICABILITY IS DISTINCT FROM TREE DETECTION",
        "detection_status_counts": dict(sorted(detection_counts.items())),
        "applicability_counts": dict(sorted(applicability_counts.items())),
        "records": [
            {
                "tree_id": row["tree_id"],
                "tree_detection_status": row["tree_detection_status"],
                "measurement_protocol": row["measurement_protocol"],
                "evidence": row["protocol_applicability_evidence"],
            }
            for row in records
        ],
    }
    axes_payload = {**common, "main_stem_axes": [row["main_stem"] for row in records]}
    candidates_payload_out = {
        **common,
        "candidate_count": len(all_candidates),
        "selection_uses_highest_raw_root_point": False,
        "attachment_candidates": all_candidates,
    }
    attachments_payload = {
        **common,
        "status_counts": dict(sorted(attachment_counts.items())),
        "highest_prop_root_attachments": [
            {"tree_id": row["tree_id"], **row["highest_prop_root_attachment"]}
            for row in records
        ],
    }
    pom_payload = {
        **common,
        "measurement_protocol": phase5a.protocol_record(config, "PROP_ROOT_PROTOCOL_APPLICABLE"),
        "protocol_poms": [
            {"tree_id": row["tree_id"], **row["protocol_pom"]}
            for row in records
        ],
        "production_pom_values_overwritten": False,
    }
    measurements_payload = {
        **common,
        "interpretation": "PHASE5A SHADOW PROPOSALS; OLD AND PROPOSED MEASUREMENTS ARE RETAINED SIDE BY SIDE",
        "measurement_status_counts": dict(sorted(measurement_counts.items())),
        "records": records,
        "production_measurements_overwritten": False,
        "tree_count_changed": False,
        "phase4c_suppression_enabled": False,
    }
    review_queue["input_provenance"] = input_hashes
    review_queue["roi_a_evaluable_reference_tree_ids"] = sorted(roi_refs_by_tree)
    review_queue["roi_a_strict_reference_count_from_phase4b"] = phase4b_evaluation["strict"]["reference_tree_count"]
    evaluation.update({
        "input_provenance": input_hashes,
        "attachment_status_counts": dict(sorted(attachment_counts.items())),
        "measurement_status_counts": dict(sorted(measurement_counts.items())),
        "roi_a_evaluable_reference_count": phase4b_evaluation["strict"]["reference_tree_count"],
        "roi_a_evaluable_tree_ids_read_from_annotation": sorted(roi_refs_by_tree),
        "roi_a_prop_root_review_tree_ids": sorted(
            tree_id for tree_id in roi_refs_by_tree
            if record_by_id[tree_id]["measurement_protocol"]["applicability"] in {
                "PROP_ROOT_PROTOCOL_APPLICABLE", "PROTOCOL_APPLICABILITY_UNCERTAIN"
            }
        ),
        "root_top_accuracy_metrics_fabricated": False,
    })
    measurements_payload["determinism_sha256"] = payload_sha256(records)

    write_json(OUTPUTS / "phase5a_protocol_applicability.json", applicability_payload)
    write_json(OUTPUTS / "phase5a_main_stem_axes.json", axes_payload)
    write_json(OUTPUTS / "phase5a_prop_root_attachment_candidates.json", candidates_payload_out)
    write_json(OUTPUTS / "phase5a_highest_prop_root_attachments.json", attachments_payload)
    write_json(OUTPUTS / "phase5a_protocol_pom_shadow.json", pom_payload)
    write_json(OUTPUTS / "phase5a_protocol_measurements_shadow.json", measurements_payload)
    write_json(OUTPUTS / "phase5a_evaluation.json", evaluation)
    write_json(OUTPUTS / "phase5a_review_queue.json", review_queue)
    write_json(PHASE5A_DATA / "review_queue.json", review_queue)

    manifest_path = VIEWER_DATA / "phase4-queues.json"
    manifest = read_json(manifest_path)
    retained = [row for row in manifest["queues"] if row["queue_id"] != "phase5a_prop_root_pom_shadow"]
    manifest["queues"] = retained + [{
        "queue_id": "phase5a_prop_root_pom_shadow",
        "label": f"Phase 5A shadow · {review_queue['queue_size']} protocol-POM reviews",
        "url": "data/phase5a/review_queue.json",
    }]
    write_json(manifest_path, manifest)

    protected_after = {str(paths[key].relative_to(ROOT)): sha256(paths[key]) for key in protected_keys}
    if protected_before != protected_after:
        raise RuntimeError("A protected pre-Phase-5A artifact changed")
    registry_after = read_json(paths["registry"])
    if registry_before != registry_after:
        raise RuntimeError("Stable Tree ID registry changed")
    evaluation["protected_input_integrity"] = {
        "unchanged": True,
        "sha256_before": protected_before,
        "sha256_after": protected_after,
    }
    write_json(OUTPUTS / "phase5a_evaluation.json", evaluation)

    print(json.dumps({
        "algorithm_version": phase5a.ALGORITHM_VERSION,
        "mode": "SHADOW",
        "source_tree_count": len(records),
        "applicability_counts": dict(sorted(applicability_counts.items())),
        "attachment_status_counts": dict(sorted(attachment_counts.items())),
        "measurement_status_counts": dict(sorted(measurement_counts.items())),
        "review_queue_size": review_queue["queue_size"],
        "roi_a_evaluable_reference_count": phase4b_evaluation["strict"]["reference_tree_count"],
        "source_las_scan_count": 0,
        "sampled_browser_cloud_read_count": 1,
        "protected_inputs_unchanged": True,
        "stable_tree_ids_changed": False,
        "phase4c_global_enforcement_enabled": False,
        "deployed": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
