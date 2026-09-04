#!/usr/bin/env python3
"""CFO-only indicative DBH screening for the two Rayong browser samples.

This intentionally uses sampled browser point clouds, not the raw LAS. It is
meant to answer whether the available geometry supports a rough DBH scale for
management discussion. It must not be represented as field-verified DBH or a
formal carbon/MRV measurement.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import math
from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter
from scipy.spatial import cKDTree
from skimage.feature import peak_local_max

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "analysis" / "rayong-cfo-screening"
OUT_DIR.mkdir(parents=True, exist_ok=True)

DATASETS = [
    ("site-001", ROOT / "rayong-preview" / "data"),
    ("site-002", ROOT / "rayong-preview" / "site-002" / "data"),
]
HEIGHTS = (0.80, 1.05, 1.30, 1.55, 1.80)
BREAST_HEIGHT = 1.30


def load_skill():
    path = ROOT / "scripts" / "analyze_samutsongkhram_trees.py"
    spec = importlib.util.spec_from_file_location("stem_skill", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_points(data_dir: Path) -> np.ndarray:
    chunks = [
        np.fromfile(path, dtype="<f4").reshape(-1, 3)
        for path in sorted(data_dir.glob("positions-*.glbin"))
    ]
    if not chunks:
        raise RuntimeError(f"no position chunks in {data_dir}")
    points = np.concatenate(chunks).astype(np.float64, copy=False)
    return points[np.isfinite(points).all(axis=1)]


def make_seeds(points: np.ndarray, bounds: tuple[float, float, float, float], ground: float):
    xmin, xmax, ymin, ymax = bounds
    resolution = 0.10
    nx = max(1, int(math.ceil((xmax - xmin) / resolution)))
    ny = max(1, int(math.ceil((ymax - ymin) / resolution)))
    ix = np.clip(((points[:, 0] - xmin) / resolution).astype(np.int32), 0, nx - 1)
    iy = np.clip(((points[:, 1] - ymin) / resolution).astype(np.int32), 0, ny - 1)
    hag = points[:, 2] - ground
    bands = []
    for lower, upper in ((0.35, 0.75), (0.75, 1.15), (1.15, 1.55), (1.55, 1.95), (1.95, 2.35)):
        mask = (hag >= lower) & (hag < upper)
        density = np.bincount(iy[mask] * nx + ix[mask], minlength=nx * ny).reshape(ny, nx).astype(float)
        bands.append(gaussian_filter(density, 1.0))
    score = np.minimum.reduce(bands)
    positive = score[score > 0]
    if not len(positive):
        return np.empty((0, 2)), score, resolution
    threshold = float(np.percentile(positive, 94.0))
    peaks = peak_local_max(score, min_distance=3, threshold_abs=threshold, exclude_border=False)
    seeds = np.column_stack(
        (xmin + (peaks[:, 1] + 0.5) * resolution, ymin + (peaks[:, 0] + 0.5) * resolution)
    )
    if len(seeds) > 1200:
        sx = np.clip(((seeds[:, 0] - xmin) / resolution).astype(int), 0, nx - 1)
        sy = np.clip(((seeds[:, 1] - ymin) / resolution).astype(int), 0, ny - 1)
        order = np.argsort(score[sy, sx])[::-1]
        seeds = seeds[order[:1200]]
    return seeds, score, resolution


def local_ground(local: np.ndarray, global_ground: float) -> float:
    estimate = float(np.percentile(local[:, 2], 2.5))
    return float(np.clip(estimate, global_ground - 0.35, global_ground + 0.60))


def fit_height(skill, local: np.ndarray, seed: np.ndarray, ground: float, height: float, rng):
    hag = local[:, 2] - ground
    radial = np.linalg.norm(local[:, :2] - seed, axis=1)
    # Wider than the formal sampled lane because this is CFO screening on a
    # 1/74–1/107 sample. Fits are subsequently filtered for persistence.
    mask = (np.abs(hag - height) <= 0.115) & (radial <= 0.46)
    fit = skill.fit_circle_ransac(local[mask, :2], seed, rng)
    if fit is not None:
        fit = dict(fit)
        fit["height"] = height
        fit["sectionPointCount"] = int(mask.sum())
    return fit


def evaluate_seed(skill, seed: np.ndarray, points: np.ndarray, tree: cKDTree, global_ground: float, rng):
    indices = tree.query_ball_point(seed, 1.00)
    if len(indices) < 55:
        return None
    local = points[indices]
    ground = local_ground(local, global_ground)
    hag = local[:, 2] - ground
    radial = np.linalg.norm(local[:, :2] - seed, axis=1)
    column = local[(radial <= 0.36) & (hag >= 0.30) & (hag <= 2.35)]
    if len(column) < 18 or np.ptp(column[:, 2]) < 0.95:
        return None
    covariance = np.cov(column.T)
    values, vectors = np.linalg.eigh(covariance)
    verticality = float(abs(vectors[2, np.argmax(values)]))
    if verticality < 0.58:
        return None

    fits = []
    for height in HEIGHTS:
        fit = fit_height(skill, local, seed, ground, height, rng)
        if fit is not None:
            fits.append(fit)
    dbh = next((fit for fit in fits if abs(fit["height"] - BREAST_HEIGHT) < 1e-9), None)
    if dbh is None or len(fits) < 2:
        return None

    centers = np.asarray([fit["center"] for fit in fits])
    median_center = np.median(centers, axis=0)
    center_spread = float(np.max(np.linalg.norm(centers - median_center, axis=1)))
    radii = np.asarray([fit["radius"] for fit in fits])
    radius_cv = float(np.std(radii) / max(np.mean(radii), 1e-6))
    if center_spread > 0.26 or radius_cv > 0.65:
        return None

    radius = float(dbh["radius"])
    flags: list[str] = []
    if radius >= 0.25:
        flags.append("LARGE_STRUCTURE_OR_PROP_ROOT_RISK")
    if dbh["inliers"] < 15:
        flags.append("LIMITED_POINT_SUPPORT")
    if dbh["coverage"] < 0.35:
        flags.append("PARTIAL_ANGULAR_COVERAGE")
    if len(fits) < 3:
        flags.append("LIMITED_VERTICAL_PERSISTENCE")
    if center_spread > 0.18:
        flags.append("CENTERLINE_SPREAD_HIGH")
    if radius_cv > 0.45:
        flags.append("RADIUS_VARIATION_HIGH")
    if verticality < 0.72:
        flags.append("LOW_VERTICALITY")
    if dbh["residual"] > 0.035:
        flags.append("FIT_RESIDUAL_HIGH")

    if (
        radius < 0.22
        and dbh["inliers"] >= 15
        and dbh["coverage"] >= 0.35
        and len(fits) >= 3
        and center_spread <= 0.18
        and radius_cv <= 0.45
        and verticality >= 0.72
    ):
        status = "A_INDICATIVE"
    elif (
        radius < 0.25
        and dbh["inliers"] >= 10
        and dbh["coverage"] >= 0.25
        and len(fits) >= 2
    ):
        status = "B_LOW_CONFIDENCE"
    else:
        status = "C_STRUCTURE_RISK"

    quality = (
        dbh["inliers"]
        * (0.25 + dbh["coverage"])
        * max(verticality, 0.1)
        * (len(fits) / 5.0)
        / ((0.04 + radius) * (1.0 + 8.0 * dbh["residual"]))
    )
    return {
        "center": [float(dbh["center"][0]), float(dbh["center"][1])],
        "seed": [float(seed[0]), float(seed[1])],
        "groundZ": ground,
        "measurementHeightM": BREAST_HEIGHT,
        "radiusM": radius,
        "dbhCm": radius * 200.0,
        "circumferenceCm": 2.0 * math.pi * radius * 100.0,
        "fitPoints": int(dbh["inliers"]),
        "sectionPointCount": int(dbh.get("sectionPointCount", 0)),
        "angularCoverageRatio": float(dbh["coverage"]),
        "residualM": float(dbh["residual"]),
        "verticality": verticality,
        "validatedSlices": len(fits),
        "centerSpreadM": center_spread,
        "radiusCv": radius_cv,
        "status": status,
        "qaFlags": flags,
        "quality": float(quality),
    }


def suppress_duplicates(candidates: list[dict]) -> list[dict]:
    kept: list[dict] = []
    for item in sorted(candidates, key=lambda x: x["quality"], reverse=True):
        center = np.asarray(item["center"])
        radius = item["radiusM"]
        duplicate = False
        for other in kept:
            distance = float(np.linalg.norm(center - np.asarray(other["center"])))
            threshold = max(0.22, 0.65 * (radius + other["radiusM"]))
            if distance < threshold:
                duplicate = True
                break
        if not duplicate:
            kept.append(item)
    return sorted(kept, key=lambda x: x["quality"], reverse=True)


def stats(values: list[float]) -> dict | None:
    if not values:
        return None
    array = np.asarray(values, dtype=float)
    return {
        "n": int(len(array)),
        "meanCm": round(float(np.mean(array)), 2),
        "medianCm": round(float(np.median(array)), 2),
        "p25Cm": round(float(np.percentile(array, 25)), 2),
        "p75Cm": round(float(np.percentile(array, 75)), 2),
        "minCm": round(float(np.min(array)), 2),
        "maxCm": round(float(np.max(array)), 2),
    }


def process_dataset(skill, site_id: str, data_dir: Path) -> dict:
    metadata = json.loads((data_dir / "metadata.json").read_text(encoding="utf-8"))
    points = load_points(data_dir)
    margin = 0.45
    x_min, y_min = np.min(points[:, :2], axis=0)
    x_max, y_max = np.max(points[:, :2], axis=0)
    bounds = (float(x_min + margin), float(x_max - margin), float(y_min + margin), float(y_max - margin))
    inside = (
        (points[:, 0] >= bounds[0]) & (points[:, 0] < bounds[1])
        & (points[:, 1] >= bounds[2]) & (points[:, 1] < bounds[3])
    )
    analysis_points = points[inside]
    global_ground = float(np.percentile(analysis_points[:, 2], 2.5))
    seeds, _, _ = make_seeds(analysis_points, bounds, global_ground)
    tree = cKDTree(analysis_points[:, :2])
    rng = np.random.default_rng(20260904 + (1 if site_id == "site-001" else 2))

    candidates = []
    for index, seed in enumerate(seeds, 1):
        result = evaluate_seed(skill, seed, analysis_points, tree, global_ground, rng)
        if result is not None:
            candidates.append(result)
        if index % 100 == 0 or index == len(seeds):
            print(site_id, f"evaluated {index}/{len(seeds)} seeds; raw candidates={len(candidates)}")
    candidates = suppress_duplicates(candidates)
    for index, candidate in enumerate(candidates, 1):
        candidate["treeId"] = f"{site_id.upper()}-CFO-{index:03d}"
        for key in (
            "groundZ", "radiusM", "dbhCm", "circumferenceCm", "angularCoverageRatio",
            "residualM", "verticality", "centerSpreadM", "radiusCv", "quality",
        ):
            candidate[key] = round(float(candidate[key]), 5 if key.endswith("M") or key.endswith("Ratio") else 3)
        candidate["center"] = [round(float(v), 3) for v in candidate["center"]]
        candidate["seed"] = [round(float(v), 3) for v in candidate["seed"]]

    statuses = {name: sum(item["status"] == name for item in candidates) for name in (
        "A_INDICATIVE", "B_LOW_CONFIDENCE", "C_STRUCTURE_RISK"
    )}
    a_values = [item["dbhCm"] for item in candidates if item["status"] == "A_INDICATIVE"]
    ab_values = [item["dbhCm"] for item in candidates if item["status"] in ("A_INDICATIVE", "B_LOW_CONFIDENCE")]
    preferred = a_values if len(a_values) >= 3 else ab_values
    preferred_basis = "A_INDICATIVE" if len(a_values) >= 3 else "A_PLUS_B_FALLBACK"

    return {
        "siteId": site_id,
        "measurementStatus": "CFO_SCREENING_ONLY",
        "fieldVerified": False,
        "sourceLas": metadata.get("source"),
        "sourcePointCount": int(metadata.get("sourcePointCount") or 0),
        "viewerPointCount": int(metadata.get("points") or len(points)),
        "samplingStride": int(metadata.get("samplingStride") or 0),
        "analysisPointCount": int(len(analysis_points)),
        "globalGroundHintZ": round(global_ground, 4),
        "candidateSeedCount": int(len(seeds)),
        "retainedCandidateCount": int(len(candidates)),
        "statusCounts": statuses,
        "aIndicativeDbh": stats(a_values),
        "aPlusBScreeningDbh": stats(ab_values),
        "preferredCfoScreeningDbh": stats(preferred),
        "preferredBasis": preferred_basis,
        "trees": candidates,
    }


def main():
    skill = load_skill()
    results = [process_dataset(skill, site_id, data_dir) for site_id, data_dir in DATASETS]
    payload = {
        "title": "Rayong two-site CFO DBH screening",
        "algorithmVersion": "cfo-sampled-screening-v1",
        "measurementStatus": "SCREENING_ONLY_NOT_FIELD_VERIFIED",
        "warning": "Do not represent these sampled-cloud estimates as formal DBH, MRV, verification, or issued carbon-credit evidence.",
        "sites": results,
    }
    json_path = OUT_DIR / "summary.json"
    csv_path = OUT_DIR / "candidates.csv"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    fields = [
        "siteId", "treeId", "status", "dbhCm", "circumferenceCm", "radiusM",
        "fitPoints", "sectionPointCount", "angularCoverageRatio", "residualM",
        "verticality", "validatedSlices", "centerSpreadM", "radiusCv", "qaFlags",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for site in results:
            for item in site["trees"]:
                row = {key: item.get(key) for key in fields}
                row["siteId"] = site["siteId"]
                row["qaFlags"] = ";".join(item.get("qaFlags") or [])
                writer.writerow(row)

    print(json.dumps({
        site["siteId"]: {
            "seedCount": site["candidateSeedCount"],
            "statusCounts": site["statusCounts"],
            "preferred": site["preferredCfoScreeningDbh"],
            "basis": site["preferredBasis"],
        }
        for site in results
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
