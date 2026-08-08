#!/usr/bin/env python3
"""Detect visible mangrove stems and measure circumference at 1.30 m.

This analysis is deliberately conservative: it reports stems only when the
sampled point cloud contains a vertically persistent, circle-like surface near
breast height.  The output is consumed directly by the browser viewer.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy.ndimage import gaussian_filter
from scipy.spatial import cKDTree
from skimage.feature import peak_local_max


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "site" / "public" / "data"
SOURCE = ROOT / "samutsongkram" / "TD_008_2026_08_07_07_04_07.las"
OUTPUT = DATA / "tree-measurements.json"
DIAGNOSTIC = ROOT / "samutsongkram" / "tree-detection-diagnostic.png"
CROSS_SECTIONS = ROOT / "samutsongkram" / "tree-cross-sections.png"

BREAST_HEIGHT = 1.30
ANALYSIS_BOUNDS = (5.0, 45.0, -25.0, 5.0)  # dense mangrove survey footprint
GLOBAL_GROUND_HINT = -6.68


def load_positions() -> np.ndarray:
    chunks = [np.fromfile(path, dtype="<f4").reshape(-1, 3) for path in sorted(DATA.glob("positions-*.glbin"))]
    return np.concatenate(chunks).astype(np.float64, copy=False)


def circle_from_three(a: np.ndarray, b: np.ndarray, c: np.ndarray):
    divisor = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
    if abs(divisor) < 1e-10:
        return None
    ar2 = np.dot(a, a)
    br2 = np.dot(b, b)
    cr2 = np.dot(c, c)
    center = np.array(
        [
            (ar2 * (b[1] - c[1]) + br2 * (c[1] - a[1]) + cr2 * (a[1] - b[1])) / divisor,
            (ar2 * (c[0] - b[0]) + br2 * (a[0] - c[0]) + cr2 * (b[0] - a[0])) / divisor,
        ]
    )
    return center, float(np.linalg.norm(a - center))


def fit_circle_least_squares(xy: np.ndarray):
    if len(xy) < 3:
        return None
    origin = xy.mean(axis=0)
    local = xy - origin
    x = local[:, 0]
    y = local[:, 1]
    design = np.column_stack((x, y, np.ones(len(xy))))
    rhs = -(x * x + y * y)
    try:
        a, b, c = np.linalg.lstsq(design, rhs, rcond=None)[0]
    except np.linalg.LinAlgError:
        return None
    center_local = np.array([-a / 2, -b / 2])
    radius_squared = np.dot(center_local, center_local) - c
    if radius_squared <= 0:
        return None
    return center_local + origin, float(np.sqrt(radius_squared))


def angular_coverage(xy: np.ndarray, center: np.ndarray, bins: int = 24) -> float:
    angles = np.arctan2(xy[:, 1] - center[1], xy[:, 0] - center[0])
    occupied = np.unique(np.clip(((angles + np.pi) / (2 * np.pi) * bins).astype(int), 0, bins - 1))
    return float(len(occupied) / bins)


def fit_circle_ransac(xy: np.ndarray, seed: np.ndarray, rng: np.random.Generator):
    if len(xy) < 10:
        return None
    if len(xy) > 2500:
        xy_fit = xy[:: math.ceil(len(xy) / 2500)]
    else:
        xy_fit = xy
    best = None
    for _ in range(500):
        sample = xy_fit[rng.choice(len(xy_fit), 3, replace=False)]
        result = circle_from_three(*sample)
        if result is None:
            continue
        center, radius = result
        if not 0.025 <= radius <= 0.30 or np.linalg.norm(center - seed) > 0.38:
            continue
        tolerance = float(np.clip(radius * 0.16, 0.018, 0.045))
        residual = np.abs(np.linalg.norm(xy_fit - center, axis=1) - radius)
        inliers = residual <= tolerance
        if inliers.sum() < 8:
            continue
        coverage = angular_coverage(xy_fit[inliers], center)
        # A loose mangrove-root/branch cloud can support a large accidental
        # circle.  Normalising by radius makes the compact stem surface win.
        score = int(inliers.sum()) * (0.35 + coverage) / (0.04 + radius)
        if best is None or score > best[0]:
            best = (score, center, radius)
    if best is None:
        return None

    _, center, radius = best
    for _ in range(3):
        tolerance = float(np.clip(radius * 0.16, 0.018, 0.045))
        residual = np.abs(np.linalg.norm(xy - center, axis=1) - radius)
        inliers = residual <= tolerance
        refined = fit_circle_least_squares(xy[inliers])
        if refined is None:
            break
        next_center, next_radius = refined
        if not 0.025 <= next_radius <= 0.30 or np.linalg.norm(next_center - seed) > 0.40:
            break
        center, radius = next_center, next_radius

    tolerance = float(np.clip(radius * 0.16, 0.018, 0.045))
    radial_error = np.linalg.norm(xy - center, axis=1) - radius
    inliers = np.abs(radial_error) <= tolerance
    if inliers.sum() < 10:
        return None
    coverage = angular_coverage(xy[inliers], center)
    residual = float(np.sqrt(np.mean(radial_error[inliers] ** 2)))
    return {
        "center": center,
        "radius": float(radius),
        "inliers": int(inliers.sum()),
        "coverage": coverage,
        "residual": residual,
    }


def fit_circle_with_prior(
    xy: np.ndarray,
    seed: np.ndarray,
    prior_radius: float,
    rng: np.random.Generator,
):
    """Refine a compact preliminary stem fit against full-resolution LAS points."""
    if len(xy) < 30:
        return None
    stride = max(1, math.ceil(len(xy) / 6000))
    xy_fit = xy[::stride]
    radius_min = max(0.018, prior_radius * 0.58)
    radius_max = min(0.34, prior_radius * 1.50)
    center_tolerance = max(0.065, prior_radius * 0.75)
    best = None
    for _ in range(900):
        sample = xy_fit[rng.choice(len(xy_fit), 3, replace=False)]
        result = circle_from_three(*sample)
        if result is None:
            continue
        center, radius = result
        if not radius_min <= radius <= radius_max or np.linalg.norm(center - seed) > center_tolerance:
            continue
        tolerance = float(np.clip(radius * 0.13, 0.005, 0.020))
        residual = np.abs(np.linalg.norm(xy_fit - center, axis=1) - radius)
        inliers = residual <= tolerance
        if inliers.sum() < 20:
            continue
        coverage = angular_coverage(xy_fit[inliers], center)
        score = int(inliers.sum()) * (0.25 + coverage)
        if best is None or score > best[0]:
            best = (score, center, radius)
    if best is None:
        return None

    _, center, radius = best
    for _ in range(4):
        tolerance = float(np.clip(radius * 0.13, 0.005, 0.020))
        radial_error = np.linalg.norm(xy - center, axis=1) - radius
        inliers = np.abs(radial_error) <= tolerance
        refined = fit_circle_least_squares(xy[inliers])
        if refined is None:
            break
        next_center, next_radius = refined
        if not radius_min <= next_radius <= radius_max or np.linalg.norm(next_center - seed) > center_tolerance:
            break
        center, radius = next_center, next_radius

    tolerance = float(np.clip(radius * 0.13, 0.005, 0.020))
    radial_error = np.linalg.norm(xy - center, axis=1) - radius
    inliers = np.abs(radial_error) <= tolerance
    if inliers.sum() < 30:
        return None
    coverage = angular_coverage(xy[inliers], center)
    residual = float(np.sqrt(np.mean(radial_error[inliers] ** 2)))
    if coverage < 0.25:
        return None
    return {
        "center": center,
        "radius": float(radius),
        "inliers": int(inliers.sum()),
        "coverage": coverage,
        "residual": residual,
    }


def load_full_candidate_neighborhoods(candidates: list[dict]) -> list[np.ndarray]:
    """Read the 67M-point LAS once and retain only narrow candidate cylinders."""
    header = SOURCE.open("rb").read(227)
    point_offset = struct.unpack_from("<I", header, 96)[0]
    record_length = struct.unpack_from("<H", header, 105)[0]
    point_count = struct.unpack_from("<I", header, 107)[0]
    scale = np.asarray(struct.unpack_from("<3d", header, 131))
    offset = np.asarray(struct.unpack_from("<3d", header, 155))
    dtype = np.dtype(
        {"names": ["xyz"], "formats": [("<i4", (3,))], "offsets": [0], "itemsize": record_length}
    )
    source = np.memmap(SOURCE, dtype=dtype, mode="r", offset=point_offset, shape=(point_count,))

    viewer_first = np.fromfile(sorted(DATA.glob("positions-*.glbin"))[0], dtype="<f4", count=3)
    source_first = source[0]["xyz"].astype(np.float64) * scale + offset
    viewer_center = source_first - viewer_first

    centers = np.asarray([candidate["center"] for candidate in candidates])
    grounds = np.asarray([candidate["ground"] for candidate in candidates])
    center_tree = cKDTree(centers)
    xmin, xmax, ymin, ymax = ANALYSIS_BOUNDS
    cell_size = 0.25
    nx = math.ceil((xmax - xmin) / cell_size)
    ny = math.ceil((ymax - ymin) / cell_size)
    grid_x = xmin + (np.arange(nx) + 0.5) * cell_size
    grid_y = ymin + (np.arange(ny) + 0.5) * cell_size
    mesh_x, mesh_y = np.meshgrid(grid_x, grid_y)
    _, cell_owner = center_tree.query(np.column_stack((mesh_x.ravel(), mesh_y.ravel())))
    cell_owner = cell_owner.reshape(ny, nx)

    collected: list[list[np.ndarray]] = [[] for _ in candidates]
    chunk_size = 2_000_000
    for start in range(0, point_count, chunk_size):
        raw = source[start : min(start + chunk_size, point_count)]["xyz"]
        x = raw[:, 0].astype(np.float64) * scale[0] + offset[0] - viewer_center[0]
        y = raw[:, 1].astype(np.float64) * scale[1] + offset[1] - viewer_center[1]
        inside = (x >= xmin) & (x < xmax) & (y >= ymin) & (y < ymax)
        if not inside.any():
            continue
        source_indices = np.flatnonzero(inside)
        x = x[inside]
        y = y[inside]
        ix = np.clip(((x - xmin) / cell_size).astype(np.int32), 0, nx - 1)
        iy = np.clip(((y - ymin) / cell_size).astype(np.int32), 0, ny - 1)
        owner = cell_owner[iy, ix]
        delta = np.column_stack((x, y)) - centers[owner]
        near = np.einsum("ij,ij->i", delta, delta) <= 0.45**2
        if not near.any():
            continue
        source_indices = source_indices[near]
        x = x[near]
        y = y[near]
        owner = owner[near]
        z = raw[source_indices, 2].astype(np.float64) * scale[2] + offset[2] - viewer_center[2]
        vertical = (z >= grounds[owner] + 0.55) & (z <= grounds[owner] + 2.05)
        if not vertical.any():
            continue
        xyz = np.column_stack((x[vertical], y[vertical], z[vertical])).astype(np.float32)
        owner = owner[vertical]
        for candidate_index in np.unique(owner):
            collected[candidate_index].append(xyz[owner == candidate_index])
        if start and start % 10_000_000 == 0:
            print(f"read {start:,}/{point_count:,} full LAS points")
    return [np.concatenate(parts).astype(np.float64) if parts else np.empty((0, 3)) for parts in collected]


def refine_with_full_points(
    candidate: dict,
    local: np.ndarray,
    rng: np.random.Generator,
):
    slice_results = []
    for height in (0.75, 1.00, 1.30, 1.60, 1.90):
        section = local[np.abs(local[:, 2] - (candidate["ground"] + height)) <= 0.04, :2]
        fitted = fit_circle_with_prior(section, candidate["center"], candidate["radius"], rng)
        if fitted is not None:
            fitted["height"] = height
            slice_results.append(fitted)
    dbh = next((result for result in slice_results if result["height"] == BREAST_HEIGHT), None)
    if dbh is None or len(slice_results) < 4:
        return None

    heights = np.asarray([result["height"] for result in slice_results])
    centers = np.asarray([result["center"] for result in slice_results])
    design = np.column_stack((heights, np.ones(len(heights))))
    coefficients_x = np.linalg.lstsq(design, centers[:, 0], rcond=None)[0]
    coefficients_y = np.linalg.lstsq(design, centers[:, 1], rcond=None)[0]
    line_x = design @ coefficients_x
    line_y = design @ coefficients_y
    line_residual = float(np.max(np.linalg.norm(centers - np.column_stack((line_x, line_y)), axis=1)))
    radii = np.asarray([result["radius"] for result in slice_results])
    radius_cv = float(np.std(radii) / max(np.mean(radii), 1e-6))
    if line_residual > 0.065 or radius_cv > 0.28:
        return None

    axis = np.asarray([coefficients_x[0], coefficients_y[0], 1.0])
    axis /= np.linalg.norm(axis)
    reference = np.asarray([0.0, 1.0, 0.0]) if abs(axis[1]) < 0.9 else np.asarray([1.0, 0.0, 0.0])
    basis_u = np.cross(axis, reference)
    basis_u /= np.linalg.norm(basis_u)
    basis_v = np.cross(axis, basis_u)

    perpendicular_results = []
    for initial in slice_results:
        height = initial["height"]
        line_point = np.asarray(
            [
                coefficients_x[0] * height + coefficients_x[1],
                coefficients_y[0] * height + coefficients_y[1],
                candidate["ground"] + height,
            ]
        )
        relative = local - line_point
        axial = relative @ axis
        plane_coordinates = np.column_stack((relative @ basis_u, relative @ basis_v))
        radial = np.linalg.norm(plane_coordinates, axis=1)
        section = plane_coordinates[(np.abs(axial) <= 0.04) & (radial <= 0.38)]
        fitted = fit_circle_with_prior(section, np.zeros(2), initial["radius"], rng)
        if fitted is None:
            continue
        fitted_center_3d = line_point + fitted["center"][0] * basis_u + fitted["center"][1] * basis_v
        fitted["height"] = height
        fitted["center"] = fitted_center_3d[:2]
        fitted["center3d"] = fitted_center_3d
        fitted["plane_center"] = np.asarray(
            [relative_component for relative_component in (
                np.dot(fitted_center_3d - line_point, basis_u),
                np.dot(fitted_center_3d - line_point, basis_v),
            )]
        )
        fitted["section_points"] = section
        perpendicular_results.append(fitted)

    dbh = next((result for result in perpendicular_results if result["height"] == BREAST_HEIGHT), None)
    if dbh is None or len(perpendicular_results) < 4:
        return None
    perpendicular_radii = np.asarray([result["radius"] for result in perpendicular_results])
    perpendicular_radius_cv = float(np.std(perpendicular_radii) / max(np.mean(perpendicular_radii), 1e-6))
    if perpendicular_radius_cv > 0.24:
        return None

    quality = (
        dbh["inliers"]
        * (0.35 + dbh["coverage"])
        * (len(perpendicular_results) / 5)
        / ((0.04 + dbh["radius"]) * (1 + 12 * dbh["residual"]))
    )
    return {
        **candidate,
        **dbh,
        "slice_count": len(perpendicular_results),
        "center_spread": line_residual,
        "radius_cv": perpendicular_radius_cv,
        "axis": axis,
        "plane_center": dbh["plane_center"],
        "section_points": dbh["section_points"],
        "quality": float(quality),
        "full_points": local,
    }


def candidate_seeds(points: np.ndarray, ground: float):
    xmin, xmax, ymin, ymax = ANALYSIS_BOUNDS
    resolution = 0.08
    nx = int((xmax - xmin) / resolution)
    ny = int((ymax - ymin) / resolution)
    ix = ((points[:, 0] - xmin) / resolution).astype(np.int32)
    iy = ((points[:, 1] - ymin) / resolution).astype(np.int32)
    hag = points[:, 2] - ground
    bands = []
    for lower, upper in ((0.3, 0.7), (0.7, 1.1), (1.1, 1.5), (1.5, 1.9), (1.9, 2.3)):
        mask = (hag >= lower) & (hag < upper)
        density = np.bincount(iy[mask] * nx + ix[mask], minlength=nx * ny).reshape(ny, nx).astype(float)
        bands.append(gaussian_filter(density, 1.2))
    score = np.minimum.reduce(bands)
    positive = score[score > 0]
    threshold = np.percentile(positive, 97.5)
    peaks = peak_local_max(score, min_distance=4, threshold_abs=threshold, exclude_border=False)
    seeds = np.column_stack((xmin + (peaks[:, 1] + 0.5) * resolution, ymin + (peaks[:, 0] + 0.5) * resolution))
    return seeds, score, resolution


def local_ground(local_points: np.ndarray) -> float:
    estimate = float(np.percentile(local_points[:, 2], 2.5))
    return float(np.clip(estimate, GLOBAL_GROUND_HINT - 0.25, GLOBAL_GROUND_HINT + 0.45))


def evaluate_seed(seed: np.ndarray, points: np.ndarray, tree: cKDTree, rng: np.random.Generator):
    indices = tree.query_ball_point(seed, 0.95)
    if len(indices) < 80:
        return None
    local = points[indices]
    ground = local_ground(local)
    hag = local[:, 2] - ground

    column = local[(np.linalg.norm(local[:, :2] - seed, axis=1) <= 0.32) & (hag >= 0.3) & (hag <= 2.3)]
    if len(column) < 30 or np.ptp(column[:, 2]) < 1.2:
        return None
    covariance = np.cov(column.T)
    values, vectors = np.linalg.eigh(covariance)
    verticality = float(abs(vectors[2, np.argmax(values)]))
    if verticality < 0.72:
        return None

    slice_results = []
    for height in (0.75, 1.00, 1.30, 1.60, 1.90):
        mask = (np.abs(hag - height) <= 0.07) & (np.linalg.norm(local[:, :2] - seed, axis=1) <= 0.42)
        fitted = fit_circle_ransac(local[mask, :2], seed, rng)
        if fitted is not None:
            fitted["height"] = height
            slice_results.append(fitted)
    dbh = next((result for result in slice_results if result["height"] == BREAST_HEIGHT), None)
    if dbh is None or len(slice_results) < 3:
        return None

    centers = np.array([result["center"] for result in slice_results])
    center_spread = float(np.max(np.linalg.norm(centers - np.median(centers, axis=0), axis=1)))
    radii = np.array([result["radius"] for result in slice_results])
    radius_cv = float(np.std(radii) / max(np.mean(radii), 1e-6))
    if center_spread > 0.14 or radius_cv > 0.38:
        return None

    quality = (
        dbh["inliers"]
        * (0.4 + dbh["coverage"])
        * max(verticality, 0.1)
        * (len(slice_results) / 5)
        / ((0.04 + dbh["radius"]) * (1 + 8 * dbh["residual"]))
    )
    return {
        "seed": seed,
        "ground": ground,
        "verticality": verticality,
        "slice_count": len(slice_results),
        "center_spread": center_spread,
        "radius_cv": radius_cv,
        "quality": float(quality),
        **dbh,
    }


def suppress_duplicates(candidates: list[dict]) -> list[dict]:
    kept = []
    for candidate in sorted(candidates, key=lambda item: item["quality"], reverse=True):
        if any(
            np.linalg.norm(candidate["center"] - other["center"])
            < max(0.20, 0.72 * (candidate["radius"] + other["radius"]))
            for other in kept
        ):
            continue
        kept.append(candidate)
    return sorted(kept, key=lambda item: (item["center"][1], item["center"][0]), reverse=True)


def confidence(candidate: dict) -> str:
    if candidate["inliers"] >= 100 and candidate["coverage"] >= 0.55 and candidate["slice_count"] >= 5:
        return "high"
    if candidate["inliers"] >= 40 and candidate["coverage"] >= 0.35 and candidate["slice_count"] >= 4:
        return "medium"
    return "low"


def main() -> None:
    all_points = load_positions()
    xmin, xmax, ymin, ymax = ANALYSIS_BOUNDS
    mask = (
        (all_points[:, 0] >= xmin)
        & (all_points[:, 0] < xmax)
        & (all_points[:, 1] >= ymin)
        & (all_points[:, 1] < ymax)
    )
    points = all_points[mask]
    seeds, score, resolution = candidate_seeds(points, GLOBAL_GROUND_HINT)
    spatial_tree = cKDTree(points[:, :2])
    rng = np.random.default_rng(20260807)
    evaluated = []
    for index, seed in enumerate(seeds, start=1):
        result = evaluate_seed(seed, points, spatial_tree, rng)
        if result is not None:
            evaluated.append(result)
        if index % 25 == 0:
            print(f"evaluated {index}/{len(seeds)} seeds; {len(evaluated)} candidates")
    preliminary_stems = suppress_duplicates(evaluated)
    print(f"refining {len(preliminary_stems)} compact candidates from the full LAS")
    full_neighborhoods = load_full_candidate_neighborhoods(preliminary_stems)
    refined = []
    for candidate, local in zip(preliminary_stems, full_neighborhoods):
        result = refine_with_full_points(candidate, local, rng)
        if result is not None:
            refined.append(result)
    stems = suppress_duplicates(refined)
    print(f"retained {len(stems)} full-resolution multi-slice stems")

    output = []
    for tree_id, stem in enumerate(stems, start=1):
        radius = stem["radius"]
        axis = stem["axis"]
        ground_center = stem["center"] - axis[:2] / axis[2] * BREAST_HEIGHT
        output.append(
            {
                "id": tree_id,
                "center": [round(float(stem["center"][0]), 3), round(float(stem["center"][1]), 3)],
                "groundCenter": [round(float(ground_center[0]), 3), round(float(ground_center[1]), 3)],
                "axis": [round(float(value), 5) for value in axis],
                "groundZ": round(float(stem["ground"]), 3),
                "measurementZ": round(float(stem["ground"] + BREAST_HEIGHT), 3),
                "radiusM": round(float(radius), 4),
                "dbhCm": round(float(radius * 200), 1),
                "circumferenceM": round(float(2 * np.pi * radius), 3),
                "fitPoints": int(stem["inliers"]),
                "angularCoverage": round(float(stem["coverage"]), 3),
                "residualM": round(float(stem["residual"]), 4),
                "verticality": round(float(stem["verticality"]), 3),
                "validatedSlices": int(stem["slice_count"]),
                "confidence": confidence(stem),
            }
        )
    payload = {
        "source": "TD_008_2026_08_07_07_04_07.las",
        "method": "sampled-cloud candidate detection; full 67M-point LAS multi-slice circle validation",
        "measurementSourcePointCount": 67_177_038,
        "viewerPointCount": 2_920_741,
        "breastHeightM": BREAST_HEIGHT,
        "visibleMeasuredTrees": len(output),
        "trees": output,
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    extent = [xmin, xmax, ymin, ymax]
    figure, axis = plt.subplots(figsize=(13, 10), constrained_layout=True)
    axis.imshow(score, origin="lower", extent=extent, cmap="gray", vmax=np.percentile(score[score > 0], 99))
    if len(seeds):
        axis.scatter(seeds[:, 0], seeds[:, 1], s=9, facecolors="none", edgecolors="#ff6d5e", alpha=0.35, label=f"seeds {len(seeds)}")
    if stems:
        centers = np.array([stem["center"] for stem in stems])
        sizes = np.array([stem["radius"] for stem in stems]) * 850
        axis.scatter(centers[:, 0], centers[:, 1], s=sizes, facecolors="none", edgecolors="#58ff93", linewidths=1.8, label=f"measured {len(stems)}")
        for tree_id, center in enumerate(centers, start=1):
            axis.text(center[0], center[1], str(tree_id), color="#ffe182", fontsize=7)
    axis.set_aspect("equal")
    axis.set_title("Automatic visible-stem detection and DBH validation")
    axis.legend(loc="upper right")
    figure.savefig(DIAGNOSTIC, dpi=180)
    plt.close(figure)

    columns = 8
    rows = max(1, math.ceil(len(stems) / columns))
    figure, axes = plt.subplots(rows, columns, figsize=(16, rows * 2), constrained_layout=True)
    axes = np.atleast_1d(axes).ravel()
    for axis in axes:
        axis.set_axis_off()
    for tree_id, (stem, record) in enumerate(zip(stems, output), start=1):
        axis = axes[tree_id - 1]
        axis.set_axis_on()
        center = stem["plane_center"]
        section = stem["section_points"]
        axis.scatter(section[:, 0], section[:, 1], s=0.3, color="#9aa89e", alpha=0.45)
        axis.add_patch(plt.Circle(center, stem["radius"], fill=False, color="#ff9f1c", linewidth=1.2))
        axis.scatter([center[0]], [center[1]], s=4, color="red")
        axis.set_xlim(center[0] - 0.36, center[0] + 0.36)
        axis.set_ylim(center[1] - 0.36, center[1] + 0.36)
        axis.set_aspect("equal")
        axis.set_xticks([])
        axis.set_yticks([])
        axis.set_title(f"{tree_id} C={record['circumferenceM']:.2f}m n={record['fitPoints']}", fontsize=7)
    figure.savefig(CROSS_SECTIONS, dpi=180)
    plt.close(figure)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
