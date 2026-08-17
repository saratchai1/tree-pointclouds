#!/usr/bin/env python3
"""Preliminary Rayong DBH measurement using the repository LiDAR stem skill.

This reuses the sampled-cloud candidate detector and multi-slice circle fitting
from ``analyze_samutsongkhram_trees.py``. It deliberately stops before the
full-LAS refinement because ``rayong-preview`` contains a 1/107 browser sample,
not the 2.78 GB source LAS. Every output is therefore marked preliminary and
geometry close to the sampled detector's acceptance bounds is flagged for
on-site/full-LAS checking instead of being presented as a final measurement.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import math
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "rayong-preview" / "data"
METADATA = DATA / "metadata.json"
OUTPUT_JSON = DATA / "tree-measurements.json"
OUTPUT_CSV = DATA / "tree-measurements.csv"
OUTPUT_MD = ROOT / "rayong-preview" / "MEASUREMENTS.md"
BREAST_HEIGHT_M = 1.30


def load_skill():
    path = ROOT / "scripts" / "analyze_samutsongkhram_trees.py"
    spec = importlib.util.spec_from_file_location("stem_skill", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def qa_flags(row: dict, radius_m: float) -> list[str]:
    flags: list[str] = []
    # The inherited sampled detector accepts radii only up to 0.30 m. A fit
    # close to that bound may be truncated or may represent roots/branches.
    if radius_m >= 0.285:
        flags.append("FITTED_RADIUS_NEAR_0_30_M_BOUND")
    if int(row["inliers"]) < 25:
        flags.append("LIMITED_POINT_SUPPORT")
    if float(row["coverage"]) < 0.60:
        flags.append("PARTIAL_ANGULAR_COVERAGE")
    if int(row["slice_count"]) < 4:
        flags.append("LIMITED_VERTICAL_SLICE_SUPPORT")
    if float(row["center_spread"]) > 0.10:
        flags.append("CENTERLINE_SPREAD_HIGH")
    if float(row["radius_cv"]) > 0.25:
        flags.append("RADIUS_VARIATION_HIGH")
    return flags


def confidence(row: dict, flags: list[str]) -> str:
    if flags:
        return "low"
    if (
        row["inliers"] >= 40
        and row["coverage"] >= 0.65
        and row["slice_count"] >= 5
        and row["verticality"] >= 0.90
        and row["center_spread"] <= 0.06
        and row["radius_cv"] <= 0.18
    ):
        return "high"
    return "medium"


def write_csv(records: list[dict]) -> None:
    fields = [
        "treeId", "x", "y", "groundZ", "measurementZ", "dbhCm",
        "circumferenceCm", "radiusM", "confidence", "fitPoints",
        "angularCoverageRatio", "residualM", "verticality",
        "validatedSlices", "centerSpreadM", "radiusCv", "status", "qaFlags",
    ]
    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for record in records:
            row = {key: record.get(key) for key in fields}
            row["qaFlags"] = ";".join(record.get("qaFlags") or [])
            writer.writerow(row)


def write_markdown(payload: dict) -> None:
    counts = payload["summary"]
    lines = [
        "# Rayong preview DBH measurements",
        "",
        "> **SCREENING RESULT — not field verified and not a final DBH claim.**",
        "",
        "The calculation reuses the repository stem skill: vertically persistent candidate detection, circle fitting at five heights, centre/radius consistency checks, and duplicate suppression.",
        "",
        f"- Browser points analysed: {payload['source']['viewerPointCount']:,}",
        f"- Source LAS points reported in metadata: {payload['source']['sourcePointCount']:,}",
        f"- Browser sampling stride: 1/{payload['source']['samplingStride']}",
        f"- Retained candidates: {payload['visibleMeasuredTrees']}",
        f"- Confidence: high {counts['high']}, medium {counts['medium']}, low {counts['low']}",
        "",
        "| Tree | DBH screening (cm) | Circumference (cm) | Status | Confidence | Fit points | Coverage | Slices |",
        "|---|---:|---:|---|---|---:|---:|---:|",
    ]
    for row in payload["trees"]:
        lines.append(
            f"| {row['treeId']} | {row['dbhCm']:.1f} | {row['circumferenceCm']:.1f} | {row['status']} | {row['confidence']} | {row['fitPoints']} | {row['angularCoverageRatio']:.2f} | {row['validatedSlices']} |"
        )
        lines.append("")
        lines.append(f"QA flags for {row['treeId']}: `{'`, `'.join(row['qaFlags'])}`")
    lines += [
        "",
        "The retained geometry is a candidate for locating the object in the viewer and planning a field/full-LAS check. Re-run the centre against the full LAS and review point-of-measurement applicability for prop-root trees before formal reporting.",
        "",
    ]
    OUTPUT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    skill = load_skill()
    metadata = json.loads(METADATA.read_text(encoding="utf-8"))
    skill.DATA = DATA
    points = skill.load_positions()
    points = points[np.isfinite(points).all(axis=1)]
    if len(points) < 1000:
        raise RuntimeError("Too few finite points")

    x_min, y_min = np.min(points[:, :2], axis=0)
    x_max, y_max = np.max(points[:, :2], axis=0)
    # Keep a narrow border because partial objects at scan edges often produce
    # unstable circle fits.
    margin = 0.35
    skill.ANALYSIS_BOUNDS = (
        float(x_min + margin), float(x_max - margin),
        float(y_min + margin), float(y_max - margin),
    )
    global_ground = float(np.percentile(points[:, 2], 2.5))
    skill.GLOBAL_GROUND_HINT = global_ground
    skill.BREAST_HEIGHT = BREAST_HEIGHT_M

    x0, x1, y0, y1 = skill.ANALYSIS_BOUNDS
    inside = (
        (points[:, 0] >= x0) & (points[:, 0] < x1)
        & (points[:, 1] >= y0) & (points[:, 1] < y1)
    )
    analysis_points = points[inside]
    z_quantiles = np.percentile(analysis_points[:, 2], [0, 1, 2.5, 5, 50, 95, 100])
    print("analysis bounds:", skill.ANALYSIS_BOUNDS)
    print("z quantiles:", [round(float(value), 3) for value in z_quantiles])
    print("global ground hint:", round(global_ground, 3))

    seeds, score, resolution = skill.candidate_seeds(analysis_points, global_ground)
    if len(seeds):
        ix = np.clip(((seeds[:, 0] - x0) / resolution).astype(int), 0, score.shape[1] - 1)
        iy = np.clip(((seeds[:, 1] - y0) / resolution).astype(int), 0, score.shape[0] - 1)
        order = np.argsort(score[iy, ix])[::-1]
        seeds = seeds[order[:1800]]
    print(f"candidate seeds retained for evaluation: {len(seeds)}")

    tree = cKDTree(analysis_points[:, :2])
    rng = np.random.default_rng(20260817)
    evaluated = []
    for index, seed in enumerate(seeds, start=1):
        result = skill.evaluate_seed(seed, analysis_points, tree, rng)
        if result is not None:
            evaluated.append(result)
        if index % 100 == 0 or index == len(seeds):
            print(f"evaluated {index}/{len(seeds)}; accepted={len(evaluated)}")
    stems = skill.suppress_duplicates(evaluated)
    print(f"retained after duplicate suppression: {len(stems)}")

    records = []
    for index, stem in enumerate(stems, start=1):
        radius = float(stem["radius"])
        dbh_cm = radius * 200.0
        circumference_cm = 2.0 * math.pi * radius * 100.0
        flags = qa_flags(stem, radius)
        record_confidence = confidence(stem, flags)
        records.append({
            "treeId": f"RY-{index:03d}",
            "center": [round(float(v), 3) for v in stem["center"]],
            "x": round(float(stem["center"][0]), 3),
            "y": round(float(stem["center"][1]), 3),
            "groundZ": round(float(stem["ground"]), 3),
            "measurementZ": round(float(stem["ground"] + BREAST_HEIGHT_M), 3),
            "measurementHeightM": BREAST_HEIGHT_M,
            "radiusM": round(radius, 4),
            "dbhCm": round(dbh_cm, 2),
            "circumferenceCm": round(circumference_cm, 2),
            "circumferenceM": round(circumference_cm / 100.0, 4),
            "confidence": record_confidence,
            "fitPoints": int(stem["inliers"]),
            "angularCoverageRatio": round(float(stem["coverage"]), 4),
            "angularCoverageDeg": round(float(stem["coverage"] * 360.0), 1),
            "residualM": round(float(stem["residual"]), 5),
            "verticality": round(float(stem["verticality"]), 4),
            "validatedSlices": int(stem["slice_count"]),
            "centerSpreadM": round(float(stem["center_spread"]), 5),
            "radiusCv": round(float(stem["radius_cv"]), 4),
            "status": "CHECK_ON_SITE" if flags else "PRELIMINARY_SAMPLE_ESTIMATE",
            "qaFlags": flags,
            "fieldVerified": False,
        })

    counts = {level: sum(row["confidence"] == level for row in records) for level in ("high", "medium", "low")}
    payload = {
        "algorithmVersion": "rayong-preview-sampled-skill-v2",
        "method": "repository sampled-cloud stem candidate detection; five-height circle persistence; duplicate suppression; conservative QA flagging",
        "measurementStatus": "SCREENING_ONLY",
        "fieldVerified": False,
        "breastHeightM": BREAST_HEIGHT_M,
        "source": {
            "path": "rayong-preview/data/positions-*.glbin",
            "sourceLas": metadata.get("source"),
            "viewerPointCount": int(len(points)),
            "sourcePointCount": int(metadata.get("sourcePointCount") or 0),
            "samplingStride": int(metadata.get("samplingStride") or 0),
            "metadataSha256": hashlib.sha256(METADATA.read_bytes()).hexdigest(),
            "globalGroundHintZ": round(global_ground, 4),
            "zQuantiles": [round(float(v), 4) for v in z_quantiles],
        },
        "candidateSeedCount": int(len(seeds)),
        "acceptedBeforeDuplicateSuppression": int(len(evaluated)),
        "visibleMeasuredTrees": int(len(records)),
        "summary": counts,
        "limitations": [
            "The browser point cloud is a regular 1/107 sample, not the full LAS.",
            "The full-LAS perpendicular-plane refinement step was not available in this public preview run.",
            "Prop-root point-of-measurement applicability has not been reviewed.",
            "All measurements require full-LAS and/or field confirmation before formal reporting.",
        ],
        "trees": records,
    }
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(records)
    write_markdown(payload)
    print(json.dumps({"visibleMeasuredTrees": len(records), "summary": counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
