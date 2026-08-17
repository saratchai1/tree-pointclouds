#!/usr/bin/env python3
"""Run the local-only Phase 2 manual-anchor pilot on reviewer seed clicks."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import time
from typing import Any

import numpy as np

import audit_v2_phase1_5_fragmentation as fragmentation
import run_stem_inventory_v2 as phase1_runner
import stem_inventory_v2 as phase1
import stem_inventory_v2_phase2_manual_anchor as phase2


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "samutsongkram" / "TD_008_2026_08_07_07_04_07.las"
DEFAULT_VIEWER_DATA = ROOT / "site" / "public" / "data"
DEFAULT_ANNOTATIONS = ROOT / "annotations" / "phase1_75_pilot_review.json"
DEFAULT_PHASE15_MANUAL = ROOT / "outputs" / "manual_seed_evaluations_v2_phase1_5.json"
PHASE1_CONFIG = ROOT / "config" / "stem_inventory_v2.yaml"
PILOT_CONFIG = ROOT / "config" / "stem_inventory_v2_phase2_manual_anchor_pilot.yaml"
OUTPUT = ROOT / "outputs" / "manual_seed_evaluations_v2_phase2_anchor_pilot.json"
POINT_OUTPUT = ROOT / "outputs" / "debug" / "manual_seed_full_resolution_points_v2_phase2_anchor_pilot"
DOC = ROOT / "docs" / "lidar-v2-phase2-manual-anchor-pilot.md"

LOCKED_HASHES = {
    "site/public/data/tree-measurements.json": "e9c741742ef00bf6bc0ca0e6551e37a835aacefdd0de7c50654ecf0e57755270",
    "scripts/stem_inventory_v2.py": "e28fd5d50612390bd26bc0b23d80d8e7a5ba3ce13d3ed9a6f12b15210bddf7ae",
    "scripts/run_stem_inventory_v2.py": "e9d3f91e03e6df5f14c7cc6fa88a14dcc14d10a88064a5c9022dbfffcd2c8f6c",
    "scripts/stem_inventory_v2_phase1_5.py": "6e99d0b7c2bdc854ac591fc140d713a13ac52a781ecbb8d2e5db266a208d44cf",
    "scripts/run_stem_inventory_v2_phase1_5.py": "1180c8a015cc86d296732de2f30be232465c5e399f5fb69f7e1b713ac76918a9",
    "config/stem_inventory_v2.yaml": "e49b9f97dd4c30e5d3f243dae7f1361aec0a49f4554cbffd24e601d30d2f73c7",
    "config/stem_inventory_v2_phase1_5.yaml": "58b2618b4c81d56a818b546ccd9e81101d0422c9303878660f703bdef0fffde6",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_locked_files() -> dict[str, str]:
    actual = {relative: sha256(ROOT / relative) for relative in LOCKED_HASHES}
    mismatches = {
        relative: {"expected": LOCKED_HASHES[relative], "actual": digest}
        for relative, digest in actual.items()
        if digest != LOCKED_HASHES[relative]
    }
    if mismatches:
        raise RuntimeError(f"Locked V1/Phase 1/Phase 1.5 inputs changed: {mismatches}")
    return actual


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(phase1.json_ready(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def selected_sampled_center(evaluation: dict | phase1.CandidateEvaluation) -> list[float] | None:
    if isinstance(evaluation, phase1.CandidateEvaluation):
        height = evaluation.measurement_height_m
        window = evaluation.diagnostics.get("selected_window")
    else:
        height = evaluation.get("measurement_height_m")
        window = evaluation.get("diagnostics", {}).get("selected_window")
    if height is None or not window:
        return None
    coefficients = np.asarray(window["centreline_coefficients"], dtype=float)
    return [
        float(coefficients[0, 0] * height + coefficients[0, 1]),
        float(coefficients[1, 0] * height + coefficients[1, 1]),
    ]


def full_validation_summary(evaluation: dict | phase1.CandidateEvaluation) -> dict | None:
    diagnostics = evaluation.diagnostics if isinstance(evaluation, phase1.CandidateEvaluation) else evaluation.get("diagnostics", {})
    validation = diagnostics.get("full_resolution_measurement_validation")
    if not validation:
        return None
    keys = (
        "valid",
        "checks",
        "valid_slice_count",
        "centreline_residual_p90_m",
        "centreline_residual_limit_m",
        "radius_residual_mad_m",
        "radius_residual_limit_m",
        "median_radius_m",
        "median_angular_coverage_deg",
        "selected_angular_coverage_deg",
        "median_fit_residual_m",
        "fit_residual_limit_m",
    )
    return {key: validation.get(key) for key in keys}


def existing_reference_point_file(reference_candidate_id: str | None) -> Path | None:
    if not reference_candidate_id:
        return None
    path = ROOT / "outputs" / "debug" / "full_resolution_points" / f"{reference_candidate_id}.npz"
    return path if path.exists() else None


def point_overlap(pilot_file: str | None, reference_file: Path | None) -> dict | None:
    if not pilot_file or reference_file is None:
        return None
    return fragmentation.overlap_metrics(
        fragmentation.point_hashes(pilot_file),
        fragmentation.point_hashes(str(reference_file)),
    )


def build_comparison(
    manual_seed: dict,
    phase15_evaluation: dict,
    pilot: phase1.CandidateEvaluation,
) -> dict:
    old_center = selected_sampled_center(phase15_evaluation)
    pilot_center = selected_sampled_center(pilot)
    click = np.asarray([manual_seed["x"], manual_seed["y"]], dtype=float)
    reference_id = manual_seed.get("reference_candidate_id")
    reference_file = existing_reference_point_file(reference_id)
    return {
        "manual_seed_id": manual_seed["seed_id"],
        "merged_source_seed_ids": manual_seed.get("merged_source_seed_ids", [manual_seed["seed_id"]]),
        "manual_click_xy": click.tolist(),
        "clean_height_hint_m": manual_seed.get("clean_height_hint_m"),
        "hint_is_automatic_final_pom": False,
        "reference_candidate_id": reference_id,
        "phase1_5_unchanged": {
            "candidate_id": phase15_evaluation["candidate_id"],
            "measurement_status": phase15_evaluation["measurement_status"],
            "measurement_rule": phase15_evaluation.get("measurement_rule"),
            "measurement_height_m": phase15_evaluation.get("measurement_height_m"),
            "sampled_selected_center": old_center,
            "sampled_selected_center_distance_from_click_m": (
                float(np.linalg.norm(np.asarray(old_center) - click)) if old_center else None
            ),
            "full_resolution_validation": full_validation_summary(phase15_evaluation),
            "reason_codes": phase15_evaluation.get("reason_codes", []),
        },
        "manual_anchor_pilot": {
            "candidate_id": pilot.candidate_id,
            "measurement_status": pilot.measurement_status,
            "measurement_rule": pilot.measurement_rule,
            "measurement_height_m": pilot.measurement_height_m,
            "selected_pom_equals_hint": pilot.diagnostics.get("selected_pom_equals_hint"),
            "sampled_selected_center": pilot_center,
            "sampled_selected_center_distance_from_click_m": (
                float(np.linalg.norm(np.asarray(pilot_center) - click)) if pilot_center else None
            ),
            "identity_anchor": pilot.diagnostics["manual_anchor_pilot"]["identity_anchor"],
            "track_slice_count": pilot.diagnostics["manual_anchor_pilot"]["track_slice_count"],
            "track_height_range_m": pilot.diagnostics["manual_anchor_pilot"]["track_height_range_m"],
            "equivalent_diameter_cm": pilot.equivalent_diameter_cm,
            "circular_equivalent_girth_cm": pilot.circular_equivalent_girth_cm,
            "full_resolution_validation": full_validation_summary(pilot),
            "reason_codes": pilot.reason_codes,
        },
        "full_resolution_accepted_point_overlap_with_reference": point_overlap(
            pilot.full_resolution_point_file,
            reference_file,
        ),
        "reference_point_file": str(reference_file) if reference_file else None,
    }


def write_doc(payload: dict) -> None:
    rows = []
    for comparison in payload["comparisons"]:
        old = comparison["phase1_5_unchanged"]
        pilot = comparison["manual_anchor_pilot"]
        validation = pilot.get("full_resolution_validation") or {}
        rows.append(
            "| {seed} | {merged} | {old_status} @ {old_h} | {track} | {pom} | {new_status} | {residual} / {limit} | {girth} |".format(
                seed=comparison["manual_seed_id"],
                merged=", ".join(comparison["merged_source_seed_ids"]),
                old_status=old["measurement_status"],
                old_h=old["measurement_height_m"],
                track=(
                    f"{pilot['track_height_range_m'][0]:.2f}–{pilot['track_height_range_m'][1]:.2f} m "
                    f"({pilot['track_slice_count']} slices)"
                    if pilot["track_height_range_m"]
                    else "none"
                ),
                pom=pilot["measurement_height_m"],
                new_status=pilot["measurement_status"],
                residual=(
                    f"{validation.get('centreline_residual_p90_m'):.4f}"
                    if validation.get("centreline_residual_p90_m") is not None else "—"
                ),
                limit=(
                    f"{validation.get('centreline_residual_limit_m'):.4f}"
                    if validation.get("centreline_residual_limit_m") is not None else "—"
                ),
                girth=(
                    f"{pilot['circular_equivalent_girth_cm']:.2f} cm"
                    if pilot["circular_equivalent_girth_cm"] is not None else "—"
                ),
            )
        )
    text = f"""# LiDAR V2 Phase 2 manual-anchor pilot

Local pilot only. This is not deployed and is not a whole-forest inventory.

## Scope and controls

- Human review explicitly merged `MANUAL-P175-0001` and `MANUAL-P175-0002` as one physical stem.
- The manual click and clean-height hint select component identity only.
- The hint is not copied into the final point of measurement (POM).
- Stable-window and full-resolution acceptance use the unchanged Phase 1 thresholds.
- V1, Phase 1, and Phase 1.5 locked files were hash-checked before and after the run.

## Actual LAS run

- LAS: `{payload['source_las']}`
- Point records: {payload['source_point_count']:,}
- Active manual stems: {payload['manual_seed_count']}
- Runtime: {payload['runtime_seconds']:.2f} s

| Manual stem | Review clicks represented | Phase 1.5 result | Anchored track | automatic POM | Pilot result | full centre P90 / limit | circular girth |
|---|---|---|---|---:|---|---:|---:|
{chr(10).join(rows)}

## Interpretation

This pilot tests whether identity-constrained component association can prevent a nearby root, branch, or neighbouring stem component from taking over a fit. A measurable result means the selected component passed the existing geometric thresholds; it is not an accuracy claim. A `NEEDS_REVIEW` result remains unmeasured.

The full machine-readable evidence, all sampled components, the selected track, full-resolution fits, and failure checks are in `{OUTPUT.relative_to(ROOT)}`.
"""
    DOC.parent.mkdir(parents=True, exist_ok=True)
    DOC.write_text(text, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--viewer-data", type=Path, default=DEFAULT_VIEWER_DATA)
    parser.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    parser.add_argument("--phase1-5-manual", type=Path, default=DEFAULT_PHASE15_MANUAL)
    parser.add_argument("--skip-full-las", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    started = time.perf_counter()
    hashes_before = verify_locked_files()
    phase1_config = phase1.load_config(PHASE1_CONFIG)
    pilot_config = phase2.load_config(PILOT_CONFIG)
    annotations = read_json(args.annotations)
    phase15_payload = read_json(args.phase1_5_manual)
    phase15_by_seed = {
        seed_id: evaluation
        for evaluation in phase15_payload["evaluations"]
        for seed_id in evaluation["source_seed_ids"]
    }
    manual_seeds = [
        item for item in annotations.get("manual_seeds", [])
        if item.get("human_label") == "TRUE_MAIN_STEM"
    ]
    if not manual_seeds:
        raise RuntimeError("No human-confirmed manual main-stem seeds found")

    evaluations = []
    source_pairs = []
    start_id = int(pilot_config["output"]["candidate_id_start"])
    for index, manual_seed in enumerate(manual_seeds):
        source = phase15_by_seed.get(manual_seed["seed_id"])
        if source is None:
            raise KeyError(f"Phase 1.5 sampled profile missing for {manual_seed['seed_id']}")
        evaluation = phase2.evaluate_manual_anchor(
            pilot_candidate_id=f"C-{start_id + index:04d}",
            manual_seed=manual_seed,
            ground_z_m=float(source["ground_z_m"]),
            original_profile=source["diagnostics"]["profile"],
            phase1_config=phase1_config,
            pilot_config=pilot_config,
        )
        evaluation.diagnostics["phase1_5_source_candidate_id"] = source["candidate_id"]
        evaluation.diagnostics["manual_seed_record"] = manual_seed
        evaluation.diagnostics["sampled_pilot_decision_before_full_resolution"] = {
            "measurement_status": evaluation.measurement_status,
            "measurement_rule": evaluation.measurement_rule,
            "measurement_height_m": evaluation.measurement_height_m,
            "selected_sampled_center": selected_sampled_center(evaluation),
        }
        evaluations.append(evaluation)
        source_pairs.append((manual_seed, source))

    source_point_count = 0
    if not args.skip_full_las:
        if not args.source.exists() or args.source.stat().st_size < 1_000_000_000:
            raise FileNotFoundError(f"Original full-resolution LAS unavailable: {args.source}")
        _, _, _, source_point_count = phase1._las_header(args.source)
        neighborhoods = phase1.extract_full_resolution_neighborhoods(
            args.source,
            evaluations,
            phase1_config,
            args.viewer_data,
        )
        for evaluation in evaluations:
            if evaluation.measurement_status.startswith("MEASURABLE_"):
                phase2.refine_candidate_full_resolution_anchored(
                    evaluation,
                    neighborhoods.get(evaluation.candidate_id, np.empty((0, 3))),
                    phase1_config,
                    pilot_config,
                    POINT_OUTPUT,
                )
                phase1_runner.validate_full_resolution_measurement(evaluation, phase1_config)

    comparisons = [
        build_comparison(manual_seed, phase15_source, evaluation)
        for (manual_seed, phase15_source), evaluation in zip(source_pairs, evaluations)
    ]
    hashes_after = verify_locked_files()
    payload = {
        "algorithm_version": phase2.ALGORITHM_VERSION,
        "scope": "LOCAL MANUAL-ANCHOR PILOT; NOT DEPLOYED; NOT A WHOLE-FOREST INVENTORY",
        "source_las": str(args.source),
        "source_point_count": source_point_count,
        "annotation_source": str(args.annotations),
        "phase1_5_profile_source": str(args.phase1_5_manual),
        "manual_seed_count": len(manual_seeds),
        "manual_click_count_represented": sum(
            int(item.get("merged_click_count", 1)) for item in manual_seeds
        ),
        "measurement_acceptance_algorithm_version": phase1_config["algorithm_version"],
        "measurement_thresholds_changed": False,
        "clean_height_hint_is_automatic_final_pom": False,
        "measurement_status_counts": dict(Counter(item.measurement_status for item in evaluations)),
        "full_resolution_accepted_count": sum(
            bool(item.diagnostics.get("full_resolution_measurement_validation", {}).get("valid"))
            for item in evaluations
        ),
        "locked_hashes_before": hashes_before,
        "locked_hashes_after": hashes_after,
        "locked_files_unchanged_during_run": hashes_before == hashes_after,
        "runtime_seconds": time.perf_counter() - started,
        "comparisons": comparisons,
        "evaluations": [item.to_dict() for item in evaluations],
    }
    write_json(OUTPUT, payload)
    write_doc(payload)
    print(f"Manual stems: {len(manual_seeds)}", flush=True)
    print(f"Full-resolution accepted: {payload['full_resolution_accepted_count']}", flush=True)
    for comparison in comparisons:
        pilot = comparison["manual_anchor_pilot"]
        print(
            f"{comparison['manual_seed_id']}: track={pilot['track_height_range_m']} "
            f"POM={pilot['measurement_height_m']} status={pilot['measurement_status']} "
            f"girth_cm={pilot['circular_equivalent_girth_cm']}",
            flush=True,
        )
    print(f"Output: {OUTPUT}", flush=True)
    print(f"Report: {DOC}", flush=True)


if __name__ == "__main__":
    main()
