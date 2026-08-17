#!/usr/bin/env python3
"""Phase 3 physical-tree inventory assembled from existing V2 evidence.

This module is additive: it does not modify candidate discovery, vertical
tracking, or measurement acceptance in V1/Phase 1/Phase 1.5/Phase 2. It makes
the candidate -> physical tree -> measurement separation explicit.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import yaml


ALGORITHM_VERSION = "stem-inventory-v2-phase3-tree-inventory"
DETECTION_STATUSES = {"CONFIRMED", "PROBABLE", "UNCERTAIN", "REJECTED"}
MEASUREMENT_STATUSES = {"MEASURABLE", "NEEDS_REVIEW", "NOT_MEASURABLE", "NOT_ATTEMPTED"}
NON_TREE_LABELS = {"PROP_ROOT_OR_ROOT_ONLY", "BRANCH", "OTHER_VEGETATION"}


class UnionFind:
    def __init__(self, values: list[str]):
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> bool:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return False
        canonical, alias = sorted((left_root, right_root))
        self.parent[alias] = canonical
        return True


def load_config(path: Path) -> dict:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if config.get("algorithm_version") != ALGORITHM_VERSION:
        raise ValueError("Unexpected Phase 3 configuration version")
    return config


def relative_difference(left: float | None, right: float | None, floor: float = 1e-9) -> float | None:
    if left is None or right is None or not math.isfinite(left) or not math.isfinite(right):
        return None
    return abs(left - right) / max((abs(left) + abs(right)) / 2.0, floor)


def automatic_stem_association(
    geometry: dict,
    accepted_point_containment: float,
    config: dict,
    shared_candidate_count: int = 0,
) -> dict:
    """Evaluate a merge without allowing XY proximity to decide by itself."""
    cfg = config["stem_association"]
    checks = {
        "vertical_overlap": float(geometry.get("height_overlap_ratio") or 0.0)
        >= cfg["minimum_height_overlap_ratio"],
        "centreline_consistency": geometry.get("mean_centreline_distance_m") is not None
        and float(geometry["mean_centreline_distance_m"])
        <= cfg["maximum_mean_centreline_distance_m"],
        "radius_consistency": geometry.get("radius_relative_difference") is not None
        and float(geometry["radius_relative_difference"])
        <= cfg["maximum_radius_relative_difference"],
        "independent_support": accepted_point_containment
        >= cfg["minimum_accepted_point_containment"]
        or shared_candidate_count > 0,
    }
    accepted = all(checks.values())
    return {
        "accepted": accepted,
        "checks": checks,
        "reason": "VERTICAL_GEOMETRY_AND_SUPPORT_AGREE" if accepted else "INSUFFICIENT_MULTI_LEVEL_ASSOCIATION_EVIDENCE",
        "xy_distance_alone_can_merge": False,
    }


def measurement_from_candidate(candidate: dict, phase1_config: dict, config: dict) -> dict:
    """Translate an already accepted measurement with explicit validity checks."""
    reasons: list[str] = []
    radius = None
    if candidate.get("equivalent_diameter_cm") is not None:
        radius = float(candidate["equivalent_diameter_cm"]) / 200.0
    minimum = float(phase1_config["candidate_radius"]["minimum_m"])
    maximum = float(phase1_config["candidate_radius"]["maximum_m"])
    epsilon = float(config["measurement_guardrails"]["radius_bound_epsilon_m"])
    if radius is not None and (abs(radius - minimum) <= epsilon or abs(radius - maximum) <= epsilon):
        reasons.append("FITTED_RADIUS_AT_CONFIGURED_BOUND")

    circle = candidate.get("circular_equivalent_girth_cm")
    ellipse = candidate.get("ellipse_perimeter_cm")
    disagreement = relative_difference(circle, ellipse)
    if disagreement is not None and disagreement > config["measurement_guardrails"]["maximum_circle_ellipse_relative_difference"]:
        reasons.append("CIRCLE_ELLIPSE_DISAGREEMENT")

    validation = candidate.get("diagnostics", {}).get("full_resolution_measurement_validation")
    if validation is not None and not validation.get("valid"):
        reasons.append("FULL_RESOLUTION_VALIDATION_FAILED")
    if candidate.get("measurement_status", "").startswith("MEASURABLE_") is False:
        reasons.append("UPSTREAM_MEASUREMENT_NOT_ACCEPTED")

    circumference = candidate.get("observed_contour_girth_cm")
    if circumference is None:
        circumference = ellipse if candidate.get("selected_model") == "ELLIPSE" else circle
    status = "NEEDS_REVIEW" if reasons else "MEASURABLE"
    return {
        "status": status,
        "pom_m": candidate.get("measurement_height_m"),
        "circumference_cm": None if reasons else circumference,
        "reported_candidate_circumference_cm": circumference,
        "equivalent_diameter_cm": None if reasons else candidate.get("equivalent_diameter_cm"),
        "selected_model": candidate.get("selected_model"),
        "confidence": candidate.get("measurement_confidence"),
        "source_candidate_id": candidate.get("candidate_id"),
        "point_count": candidate.get("diagnostics", {}).get("full_resolution", {}).get("accepted_point_count"),
        "validity_reasons": reasons or ["EXISTING_FULL_RESOLUTION_ACCEPTANCE_PRESERVED"],
        "verified_field_measurement": False,
    }


def _candidate_to_track(alias_rows: list[dict]) -> dict[str, str | None]:
    return {row["phase1_candidate_id"]: row.get("canonical_track_id") for row in alias_rows}


def _track_groups(
    tracks: list[dict],
    aliases: dict,
    annotations: dict,
    config: dict,
) -> tuple[dict[str, list[str]], list[dict], dict[str, str | None]]:
    by_id = {track["track_id"]: track for track in tracks}
    candidate_track = _candidate_to_track(aliases["candidate_aliases"])
    union = UnionFind(list(by_id))
    decisions: list[dict] = []

    for pair in aliases.get("track_alias_pairs", []):
        if pair.get("classification") not in {"DEFINITE_ALIAS", "PROBABLE_ALIAS"}:
            continue
        left_track, right_track = pair["track_a"], pair["track_b"]
        shared_candidates = set(by_id[left_track].get("source_candidate_ids", [])) & set(by_id[right_track].get("source_candidate_ids", []))
        decision = automatic_stem_association(
            pair,
            float(pair.get("accepted_point_containment") or 0.0),
            config,
            shared_candidate_count=len(shared_candidates),
        )
        changed = union.union(left_track, right_track) if decision["accepted"] else False
        decisions.append({
            "track_a": left_track,
            "track_b": right_track,
            "decision": "SAME_PHYSICAL_TREE" if decision["accepted"] else "REVIEW_NOT_MERGED",
            "union_changed": changed,
            "evidence_type": "PHASE1_5_TRACK_PAIR_GEOMETRY",
            "evidence": {**pair, "phase3_checks": decision["checks"]},
            "xy_distance_only": False,
        })

    def merge_candidates(left_candidate: str, right_candidate: str, evidence: str, details: dict) -> None:
        left_track, right_track = candidate_track.get(left_candidate), candidate_track.get(right_candidate)
        accepted = bool(left_track and right_track)
        changed = False
        if accepted:
            changed = union.union(left_track, right_track)
        decisions.append({
            "candidate_a": left_candidate,
            "candidate_b": right_candidate,
            "track_a": left_track,
            "track_b": right_track,
            "decision": "SAME_PHYSICAL_TREE" if accepted else "UNRESOLVED_MISSING_TRACK",
            "union_changed": changed,
            "evidence_type": evidence,
            "evidence": details,
            "xy_distance_only": False,
        })

    consolidations = aliases.get("full_resolution_candidate_consolidation", {}).get("consolidations", [])
    for group in consolidations:
        canonical = group["canonical_candidate_id"]
        for alias in group.get("aliases", []):
            merge_candidates(canonical, alias["alias_candidate_id"], "FULL_RESOLUTION_DEFINITE_ALIAS", alias)

    for item in annotations.get("annotations", []):
        if item.get("human_label") == "DUPLICATE_OF" and item.get("duplicate_target"):
            merge_candidates(item["duplicate_target"], item["candidate_id"], "HUMAN_DUPLICATE_OF", {
                "reviewer_note": item.get("reviewer_note", ""),
                "timestamp": item.get("timestamp"),
            })

    groups: dict[str, list[str]] = defaultdict(list)
    for track_id in sorted(by_id):
        groups[union.find(track_id)].append(track_id)
    return dict(groups), decisions, candidate_track


def _group_center(group_tracks: list[dict]) -> list[float]:
    points = np.asarray([track["reference_center"] for track in group_tracks], dtype=float)
    weights = np.asarray([max(float(track.get("track_quality_score") or 0.0), 0.01) for track in group_tracks])
    return np.average(points, axis=0, weights=weights).tolist()


def _group_detection(
    group_tracks: list[dict],
    source_candidates: list[str],
    human_by_candidate: dict[str, dict],
    config: dict,
) -> tuple[str | None, list[str], list[str]]:
    labels = [human_by_candidate[c]["human_label"] for c in source_candidates if c in human_by_candidate]
    reasons: list[str] = []
    excluded_candidates = [
        candidate for candidate in source_candidates
        if human_by_candidate.get(candidate, {}).get("human_label") in NON_TREE_LABELS
    ]
    if "TRUE_MAIN_STEM" in labels:
        reasons.append("HUMAN_TRUE_MAIN_STEM_EVIDENCE")
        return "CONFIRMED", reasons, excluded_candidates
    if "NOT_ENOUGH_INFORMATION" in labels or "MANUAL_REVIEW_REQUIRED" in labels:
        reasons.append("HUMAN_REVIEW_INSUFFICIENT_INFORMATION")
        return "UNCERTAIN", reasons, excluded_candidates
    reviewed_nonduplicates = [label for label in labels if label != "DUPLICATE_OF"]
    all_sources_reviewed = bool(source_candidates) and all(candidate in human_by_candidate for candidate in source_candidates)
    if all_sources_reviewed and reviewed_nonduplicates and all(label in NON_TREE_LABELS for label in reviewed_nonduplicates):
        reasons.append("ALL_REVIEWED_SOURCES_CLASSIFIED_NON_TREE")
        return "REJECTED", reasons, excluded_candidates

    if any(track.get("candidate_geometry_status") == "STEM_LIKE" for track in group_tracks):
        reasons.append("PHASE1_5_STEM_LIKE_VERTICAL_TRACK")
        return "PROBABLE", reasons, excluded_candidates
    support = config["discovery_support"]
    if any(
        track.get("candidate_geometry_status") in {"AMBIGUOUS_MULTI_COMPONENT", "WEAK_GEOMETRY"}
        and int(track.get("source_height_count") or 0) >= support["minimum_height_levels"]
        and float(track.get("vertical_span_m") or 0.0) >= support["minimum_vertical_span_m"]
        for track in group_tracks
    ):
        reasons.append("MULTI_LEVEL_VERTICAL_STRUCTURE_REQUIRES_REVIEW")
        return "UNCERTAIN", reasons, excluded_candidates
    return None, ["INSUFFICIENT_VERTICAL_TREE_DISCOVERY_EVIDENCE"], excluded_candidates


def _stable_tree_key(track_ids: list[str], candidate_ids: list[str], manual_seed_ids: list[str]) -> str:
    identity = "|".join(sorted(track_ids) + sorted(candidate_ids) + sorted(manual_seed_ids))
    return "PT-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16].upper()


def build_phase3(
    phase1_candidates_payload: dict,
    phase1_measurements_payload: dict,
    tracks_payload: dict,
    aliases_payload: dict,
    annotations: dict,
    phase2_payload: dict,
    phase2_recheck: dict,
    phase1_config: dict,
    phase3_config: dict,
    prior_tree_id_registry: dict | None = None,
) -> tuple[dict, dict, dict, dict]:
    """Build inventory, associations, summary, and uncertainty report."""
    candidates = phase1_candidates_payload["candidates"]
    candidate_by_id = {item["candidate_id"]: item for item in candidates}
    measurements = {item["candidate_id"]: item for item in phase1_measurements_payload["measurements"]}
    tracks = tracks_payload["tracks"]
    track_by_id = {item["track_id"]: item for item in tracks}
    human_by_candidate = {item["candidate_id"]: item for item in annotations.get("annotations", [])}
    groups, merge_decisions, candidate_track = _track_groups(tracks, aliases_payload, annotations, phase3_config)

    candidate_ids_by_track: dict[str, set[str]] = defaultdict(set)
    for candidate_id, track_id in candidate_track.items():
        if track_id:
            candidate_ids_by_track[track_id].add(candidate_id)
    # A Phase 1 candidate can be fragmented across several Phase 1.5 tracks.
    # Human labels belong to its canonical track only; copying the label to
    # every fragment would manufacture several physical trees from one review.

    manual_by_reference: dict[str, list[dict]] = defaultdict(list)
    for seed in annotations.get("manual_seeds", []):
        references = seed.get("reference_candidate_ids") or [seed.get("reference_candidate_id")]
        for candidate_id in filter(None, references):
            manual_by_reference[candidate_id].append(seed)

    phase2_by_seed = {
        item.get("source_seed_ids", [None])[0]: item for item in phase2_payload.get("evaluations", [])
    }
    recheck_by_seed = {item["manual_seed_id"]: item for item in phase2_recheck.get("measurements", [])}

    provisional = []
    rejected_groups = []
    group_to_tree_key: dict[str, str] = {}
    group_records: dict[str, dict] = {}
    for group_root, track_ids in sorted(groups.items()):
        group_tracks = [track_by_id[track_id] for track_id in track_ids]
        source_candidates = sorted(set().union(*(candidate_ids_by_track[track_id] for track_id in track_ids)))
        manual_seeds = sorted(
            {seed["seed_id"]: seed for candidate in source_candidates for seed in manual_by_reference.get(candidate, [])}.values(),
            key=lambda item: item["seed_id"],
        )
        detection_status, detection_reasons, excluded_candidates = _group_detection(
            group_tracks, source_candidates, human_by_candidate, phase3_config
        )
        if manual_seeds and any(seed.get("human_label") == "TRUE_MAIN_STEM" for seed in manual_seeds):
            detection_status = "CONFIRMED"
            if "HUMAN_MANUAL_SEED_MAIN_STEM" not in detection_reasons:
                detection_reasons.insert(0, "HUMAN_MANUAL_SEED_MAIN_STEM")

        center = _group_center(group_tracks)
        manual_seed_ids = sorted({value for seed in manual_seeds for value in seed.get("merged_source_seed_ids", [seed["seed_id"]])})
        tree_key = _stable_tree_key(track_ids, source_candidates, manual_seed_ids)
        record = {
            "tree_key": tree_key,
            "tree_id": None,
            "center": {"x": center[0], "y": center[1]},
            "detection": {
                "status": detection_status or "UNRESOLVED_FRAGMENT",
                "confidence": None,
                "confidence_is_calibrated": False,
                "reason_codes": detection_reasons,
            },
            "stem": {
                "z_min_agl_m": min(min(track["source_heights_m"]) for track in group_tracks),
                "z_max_agl_m": max(max(track["source_heights_m"]) for track in group_tracks),
                "axis_confidence": None,
                "axis_confidence_is_calibrated": False,
                "axis_evidence_score": max(float(track.get("track_quality_score") or 0.0) for track in group_tracks),
                "source_track_ids": sorted(track_ids),
                "vertical_level_count": len(set().union(*(set(track["source_heights_m"]) for track in group_tracks))),
            },
            "source_candidates": source_candidates,
            "excluded_non_tree_source_candidates": excluded_candidates,
            "manual_seed_ids": manual_seed_ids,
            "association": {
                "candidate_count": len(source_candidates),
                "track_count": len(track_ids),
                "candidates_merged_within_tree": max(0, len(source_candidates) - 1),
                "uses_xy_distance_only": False,
            },
            "measurement": {"status": "NOT_ATTEMPTED", "pom_m": None, "circumference_cm": None, "validity_reasons": ["NO_ACCEPTED_MEASUREMENT_FOR_TREE"]},
            "provenance": {
                "algorithm_version": ALGORITHM_VERSION,
                "phase1_5_track_algorithm_version": tracks_payload.get("algorithm_version"),
                "source_las": phase1_candidates_payload.get("source_las"),
                "source_las_point_count": phase1_candidates_payload.get("source_las_point_count"),
            },
        }

        eligible_measurements = [
            measurements[candidate_id] for candidate_id in source_candidates
            if candidate_id in measurements and candidate_id not in excluded_candidates
            and human_by_candidate.get(candidate_id, {}).get("human_label") != "DUPLICATE_OF"
        ]
        if eligible_measurements:
            chosen = max(eligible_measurements, key=lambda item: (float(item.get("measurement_confidence") or 0.0), int(item.get("supporting_slice_count") or 0), item["candidate_id"]))
            record["measurement"] = measurement_from_candidate(chosen, phase1_config, phase3_config)

        for seed in manual_seeds:
            evaluation = phase2_by_seed.get(seed["seed_id"])
            recheck = recheck_by_seed.get(seed["seed_id"])
            if not evaluation or not recheck:
                continue
            manual_measurement = measurement_from_candidate(evaluation, phase1_config, phase3_config)
            manual_measurement.update({
                "source_manual_seed_id": seed["seed_id"],
                "independent_recheck_verdict": recheck["verdict"],
            })
            if recheck["verdict"] == "BOUNDARY_SENSITIVE_DO_NOT_TREAT_REPORTED_GIRTH_AS_FINAL":
                manual_measurement["status"] = "NEEDS_REVIEW"
                manual_measurement["circumference_cm"] = None
                manual_measurement["equivalent_diameter_cm"] = None
                manual_measurement["validity_reasons"] = [
                    "UNCONSTRAINED_RADIUS_BELOW_CONFIGURED_MINIMUM",
                    "BOUNDARY_SENSITIVE_RESULT_NOT_FINAL",
                    "TREE_PRESERVED_DESPITE_MEASUREMENT_FAILURE",
                ]
            record["measurement"] = manual_measurement
            phase2_range = evaluation.get("diagnostics", {}).get("manual_anchor_pilot", {}).get("track_height_range_m")
            if phase2_range and len(phase2_range) == 2:
                record["stem"]["z_min_agl_m"] = min(record["stem"]["z_min_agl_m"], float(phase2_range[0]))
                record["stem"]["z_max_agl_m"] = max(record["stem"]["z_max_agl_m"], float(phase2_range[1]))
                record["stem"]["phase2_manual_track_height_range_m"] = [float(phase2_range[0]), float(phase2_range[1])]
            record["stem"]["manual_clean_height_hint_m"] = seed.get("clean_height_hint_m")
            record["stem"]["manual_height_is_final_pom"] = False

        group_records[group_root] = record
        if detection_status == "REJECTED":
            rejected_groups.append(record)
        elif detection_status in {"CONFIRMED", "PROBABLE", "UNCERTAIN"}:
            provisional.append(record)

    provisional.sort(key=lambda item: (item["center"]["x"], item["center"]["y"], item["tree_key"]))
    registered = dict((prior_tree_id_registry or {}).get("tree_key_to_tree_id", {}))
    used_ids = set(registered.values())
    numeric_ids = [int(value.split("_")[-1]) for value in used_ids if value.startswith("TREE_") and value.split("_")[-1].isdigit()]
    next_id = max(numeric_ids, default=0) + 1
    for tree in provisional:
        tree_id = registered.get(tree["tree_key"])
        if tree_id is None:
            while f"TREE_{next_id:04d}" in used_ids:
                next_id += 1
            tree_id = f"TREE_{next_id:04d}"
            registered[tree["tree_key"]] = tree_id
            used_ids.add(tree_id)
            next_id += 1
        tree["tree_id"] = tree_id
        group_to_tree_key[next(root for root, record in group_records.items() if record is tree)] = tree["tree_id"]

    candidate_rows = []
    for candidate in candidates:
        candidate_id = candidate["candidate_id"]
        track_id = candidate_track.get(candidate_id)
        group_root = next((root for root, values in groups.items() if track_id in values), None)
        record = group_records.get(group_root) if group_root else None
        annotation = human_by_candidate.get(candidate_id)
        disposition = "UNRESOLVED_FRAGMENT"
        if record and record.get("tree_id"):
            disposition = "ASSOCIATED_NON_TREE_FRAGMENT" if candidate_id in record["excluded_non_tree_source_candidates"] else "SOURCE_CANDIDATE"
        elif record and record["detection"]["status"] == "REJECTED":
            disposition = "REJECTED_NON_TREE"
        candidate_rows.append({
            "candidate_id": candidate_id,
            "canonical_track_id": track_id,
            "tree_id": record.get("tree_id") if record else None,
            "tree_key": record.get("tree_key") if record else None,
            "disposition": disposition,
            "human_label": annotation.get("human_label") if annotation else None,
            "duplicate_target": annotation.get("duplicate_target") if annotation else None,
            "trace_preserved": True,
        })

    track_rows = []
    for root, track_ids in groups.items():
        record = group_records[root]
        for track_id in track_ids:
            track_rows.append({
                "track_id": track_id,
                "tree_id": record.get("tree_id"),
                "tree_key": record["tree_key"],
                "detection_status": record["detection"]["status"],
                "source_candidate_ids": track_by_id[track_id].get("source_candidate_ids", []),
            })
    track_rows.sort(key=lambda item: item["track_id"])

    explicit_duplicate_ids = sorted({
        decision["candidate_b"] for decision in merge_decisions
        if decision["decision"] == "SAME_PHYSICAL_TREE" and decision.get("candidate_b")
    })
    measurement_counts = Counter(tree["measurement"]["status"] for tree in provisional)
    detection_counts = Counter(tree["detection"]["status"] for tree in provisional)
    reviewed_label_counts = Counter(item["human_label"] for item in annotations.get("annotations", []))
    rejected_non_tree_candidates = sorted(
        candidate_id for candidate_id, item in human_by_candidate.items() if item["human_label"] in NON_TREE_LABELS
    )
    unresolved_tracks = [record for record in group_records.values() if record["detection"]["status"] == "UNRESOLVED_FRAGMENT"]
    uncertain_trees = [tree for tree in provisional if tree["detection"]["status"] == "UNCERTAIN"]
    needs_review = [tree for tree in provisional if tree["measurement"]["status"] == "NEEDS_REVIEW"]
    candidate_surplus = sum(max(0, len(tree["source_candidates"]) - 1) for tree in provisional)

    inventory = {
        "algorithm_version": ALGORITHM_VERSION,
        "interpretation": "PROVISIONAL PHYSICAL-TREE INVENTORY; CIRCUMFERENCES ARE NOT VERIFIED FIELD MEASUREMENTS",
        "source_las": phase1_candidates_payload.get("source_las"),
        "source_las_point_count": phase1_candidates_payload.get("source_las_point_count"),
        "tree_count": len(provisional),
        "tree_id_stability": "PERSISTED_FOR_UNCHANGED_INFERRED_TREE_KEYS",
        "trees": provisional,
    }
    associations = {
        "algorithm_version": ALGORITHM_VERSION,
        "association_policy": "VERTICAL_GEOMETRY_PLUS_PROVENANCE; XY_DISTANCE_ALONE_NEVER_MERGES",
        "candidate_association_count": len(candidate_rows),
        "track_association_count": len(track_rows),
        "explicit_merge_decisions": merge_decisions,
        "candidate_associations": candidate_rows,
        "track_associations": track_rows,
    }
    summary = {
        "algorithm_version": ALGORITHM_VERSION,
        "stage_counts": {
            "raw_phase1_candidates": len(candidates),
            "phase1_5_vertical_tracks": len(tracks),
            "track_groups_after_explicit_association": len(groups),
            "reviewed_true_main_stem_candidates": reviewed_label_counts["TRUE_MAIN_STEM"],
            "reviewed_duplicate_candidates": reviewed_label_counts["DUPLICATE_OF"],
            "explicit_unique_duplicate_candidate_ids_merged": len(explicit_duplicate_ids),
            "rejected_root_or_branch_candidates": len(rejected_non_tree_candidates),
            "rejected_non_tree_track_groups": len(rejected_groups),
            "unresolved_fragment_track_groups": len(unresolved_tracks),
            "provisional_physical_tree_count": len(provisional),
            "source_candidate_surplus_consolidated_into_trees": candidate_surplus,
        },
        "detection_status_counts": dict(sorted(detection_counts.items())),
        "measurement_status_counts": dict(sorted(measurement_counts.items())),
        "reviewed_candidate_label_counts": dict(sorted(reviewed_label_counts.items())),
        "evaluation": {
            "ground_truth_physical_trees": None,
            "detected_physical_trees": len(provisional),
            "true_positives": None,
            "false_positives": None,
            "false_negatives": None,
            "duplicate_detections": len(explicit_duplicate_ids),
            "uncertain_detections": detection_counts["UNCERTAIN"],
            "precision": None,
            "recall": None,
            "reason_metrics_unavailable": "The 39 reviews are candidate labels, not an exhaustive physical-tree ground-truth census.",
        },
        "regression_cases": {},
    }
    for tree in provisional:
        if {"MANUAL-P175-0001", "MANUAL-P175-0002"}.issubset(set(tree["manual_seed_ids"])):
            summary["regression_cases"]["manual_seeds_0001_0002"] = {
                "tree_id": tree["tree_id"],
                "same_tree": True,
                "physical_tree_count": 1,
                "source_manual_seed_ids": tree["manual_seed_ids"],
                "measurement": tree["measurement"],
            }
        if "MANUAL-P175-0003" in tree["manual_seed_ids"]:
            summary["regression_cases"]["manual_seed_0003"] = {
                "tree_id": tree["tree_id"],
                "tree_detected": True,
                "detection_status": tree["detection"]["status"],
                "measurement": tree["measurement"],
            }

    unresolved_fragment_rows = []
    for item in unresolved_tracks:
        group_tracks = [track_by_id[track_id] for track_id in item["stem"]["source_track_ids"]]
        unresolved_fragment_rows.append({
            "tree_key": item["tree_key"],
            "track_ids": item["stem"]["source_track_ids"],
            "canonical_source_candidates": item["source_candidates"],
            "all_track_source_candidates": sorted(set().union(*(set(track.get("source_candidate_ids", [])) for track in group_tracks))),
            "geometry_statuses": sorted({track["candidate_geometry_status"] for track in group_tracks}),
            "z_min_agl_m": item["stem"]["z_min_agl_m"],
            "z_max_agl_m": item["stem"]["z_max_agl_m"],
            "reason_codes": item["detection"]["reason_codes"],
        })
    accepted_measurement_without_tree = sorted(
        row["candidate_id"] for row in candidate_rows
        if row["candidate_id"] in measurements and row["tree_id"] is None
        and row["disposition"] == "UNRESOLVED_FRAGMENT"
    )
    mixed_label_trees = []
    for tree in provisional:
        labels = {candidate_id: human_by_candidate[candidate_id]["human_label"] for candidate_id in tree["source_candidates"] if candidate_id in human_by_candidate}
        if any(label == "TRUE_MAIN_STEM" for label in labels.values()) and any(label in NON_TREE_LABELS for label in labels.values()):
            mixed_label_trees.append({"tree_id": tree["tree_id"], "candidate_labels": labels, "resolution": "TREE_PRESERVED; NON_TREE_FRAGMENT_EXCLUDED"})

    uncertainty = {
        "algorithm_version": ALGORITHM_VERSION,
        "warning": "This is not an exhaustive false-negative report because complete physical-tree ground truth is unavailable.",
        "uncertain_tree_count": len(uncertain_trees),
        "uncertain_trees": [
            {"tree_id": tree["tree_id"], "source_candidates": tree["source_candidates"], "reason_codes": tree["detection"]["reason_codes"]}
            for tree in uncertain_trees
        ],
        "measurement_needs_review_count": len(needs_review),
        "measurement_needs_review": [
            {"tree_id": tree["tree_id"], "source_candidates": tree["source_candidates"], "reasons": tree["measurement"]["validity_reasons"]}
            for tree in needs_review
        ],
        "rejected_non_tree_candidates": rejected_non_tree_candidates,
        "rejected_track_groups": [
            {"tree_key": item["tree_key"], "source_candidates": item["source_candidates"], "reason_codes": item["detection"]["reason_codes"]}
            for item in rejected_groups
        ],
        "unresolved_fragment_track_group_count": len(unresolved_tracks),
        "unresolved_fragment_reason_counts": dict(Counter(reason for item in unresolved_tracks for reason in item["detection"]["reason_codes"])),
        "unresolved_fragment_geometry_status_counts": dict(Counter(status for item in unresolved_fragment_rows for status in item["geometry_statuses"])),
        "unresolved_fragments": unresolved_fragment_rows,
        "accepted_measurement_candidates_without_sufficient_phase3_discovery_evidence_count": len(accepted_measurement_without_tree),
        "accepted_measurement_candidates_without_sufficient_phase3_discovery_evidence": accepted_measurement_without_tree,
        "mixed_human_label_tree_groups": mixed_label_trees,
        "probable_alias_pairs_requiring_review": [
            item for item in aliases_payload.get("track_alias_pairs", []) if item.get("classification") == "PROBABLE_ALIAS"
        ],
    }
    return inventory, associations, summary, uncertainty
