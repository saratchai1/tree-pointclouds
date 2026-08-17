#!/usr/bin/env python3
"""Geometry-only adaptive-height stem inventory pipeline (V2 phase 1).

This module is intentionally separate from the production V1 analyzer.  It
keeps every candidate traceable, supports multi-height seeds, evaluates local
stable stem windows, and performs final measurements only against the original
full-resolution LAS.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
import struct
from typing import Any, Iterable

import numpy as np
import yaml
from scipy.ndimage import gaussian_filter
from scipy.optimize import linear_sum_assignment
from scipy.spatial import cKDTree
from skimage.feature import peak_local_max
from skimage.measure import EllipseModel

import analyze_samutsongkhram_trees as v1


STATUS_VOCABULARY = {
    "CANDIDATE",
    "CONFIRMED_STEM",
    "MEASURABLE_STANDARD_1_30",
    "MEASURABLE_ADAPTIVE_HEIGHT",
    "NEEDS_REVIEW",
    "INSUFFICIENT_COVERAGE",
    "REJECTED_GEOMETRY",
    "REJECTED_DUPLICATE",
    "REJECTED_FALSE_POSITIVE",
}


def load_config(path: Path) -> dict:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if config.get("algorithm_version") != "stem-inventory-v2-phase1":
        raise ValueError("Unexpected V2 configuration version")
    return config


def heights_inclusive(start: float, stop: float, step: float) -> list[float]:
    count = int(round((stop - start) / step))
    return [round(start + index * step, 6) for index in range(count + 1)]


def rounded(value: Any, digits: int = 6):
    if value is None:
        return None
    return round(float(value), digits)


def json_ready(value: Any):
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items() if not key.startswith("_")}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    return value


@dataclass(frozen=True)
class SeedRecord:
    seed_id: str
    source: str
    source_height_m: float | None
    x: float
    y: float
    source_index: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CandidateEvaluation:
    algorithm_version: str
    candidate_id: str
    source_seed_ids: list[str]
    seed_sources: list[str]
    source_heights_m: list[float]
    position: dict[str, float]
    seed_relationships: list[dict]
    candidate_status: str = "CANDIDATE"
    measurement_status: str = "INSUFFICIENT_COVERAGE"
    measurement_rule: str | None = None
    measurement_height_m: float | None = None
    irregular_zone_top_height_m: float | None = None
    equivalent_diameter_cm: float | None = None
    diameter_uncertainty_cm: float | None = None
    circular_equivalent_girth_cm: float | None = None
    ellipse_major_axis_cm: float | None = None
    ellipse_minor_axis_cm: float | None = None
    ellipse_perimeter_cm: float | None = None
    observed_contour_girth_cm: float | None = None
    tree_presence_confidence: float = 0.0
    stem_tracking_confidence: float = 0.0
    measurement_confidence: float = 0.0
    centreline_residual_p90_m: float | None = None
    radius_residual_mad_m: float | None = None
    raw_centre_spread_m: float | None = None
    raw_radius_cv: float | None = None
    angular_coverage_deg: float | None = None
    supporting_slice_count: int = 0
    reason_codes: list[str] = field(default_factory=list)
    ground_z_m: float | None = None
    selected_model: str | None = None
    duplicate_of_candidate_id: str | None = None
    full_resolution_point_file: str | None = None
    diagnostics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        if self.candidate_status not in STATUS_VOCABULARY:
            raise ValueError(f"Unknown candidate status {self.candidate_status}")
        if self.measurement_status not in STATUS_VOCABULARY:
            raise ValueError(f"Unknown measurement status {self.measurement_status}")
        return json_ready(asdict(self))


def angular_metrics(xy: np.ndarray, center: np.ndarray, bins: int = 36) -> tuple[float, float]:
    if len(xy) == 0:
        return 0.0, 360.0
    angles = np.arctan2(xy[:, 1] - center[1], xy[:, 0] - center[0])
    occupied = np.zeros(bins, dtype=bool)
    indexes = np.floor((angles + np.pi) / (2 * np.pi) * bins).astype(int) % bins
    occupied[np.unique(indexes)] = True
    coverage = float(occupied.sum() * 360.0 / bins)
    if occupied.all():
        return coverage, 0.0
    doubled = np.concatenate((~occupied, ~occupied))
    longest = current = 0
    for missing in doubled:
        current = current + 1 if missing else 0
        longest = max(longest, current)
    longest = min(longest, bins)
    return coverage, float(longest * 360.0 / bins)


def mad(values: np.ndarray) -> float:
    if len(values) == 0:
        return math.inf
    median = np.median(values)
    return float(np.median(np.abs(values - median)))


def robust_scalar_line(
    heights: np.ndarray,
    values: np.ndarray,
    iterations: int = 8,
    huber_delta: float = 0.05,
) -> tuple[np.ndarray, np.ndarray]:
    design = np.column_stack((heights, np.ones(len(heights))))
    weights = np.ones(len(heights))
    coefficients = np.linalg.lstsq(design, values, rcond=None)[0]
    for _ in range(iterations):
        residual = np.abs(values - design @ coefficients)
        scale = max(1.4826 * mad(residual), huber_delta / 3, 1e-6)
        cutoff = max(huber_delta, 1.5 * scale)
        weights = np.where(residual <= cutoff, 1.0, cutoff / np.maximum(residual, 1e-9))
        weighted_design = design * np.sqrt(weights)[:, None]
        weighted_values = values * np.sqrt(weights)
        coefficients = np.linalg.lstsq(weighted_design, weighted_values, rcond=None)[0]
    return coefficients, values - design @ coefficients


def robust_centreline(
    heights: np.ndarray,
    centers: np.ndarray,
    config: dict,
) -> tuple[np.ndarray, np.ndarray]:
    design = np.column_stack((heights, np.ones(len(heights))))
    weights = np.ones(len(heights))
    coefficients_x = np.linalg.lstsq(design, centers[:, 0], rcond=None)[0]
    coefficients_y = np.linalg.lstsq(design, centers[:, 1], rcond=None)[0]
    delta = config["tracking"]["huber_delta_m"]
    for _ in range(config["tracking"]["robust_iterations"]):
        predicted = np.column_stack((design @ coefficients_x, design @ coefficients_y))
        residual = np.linalg.norm(centers - predicted, axis=1)
        scale = max(1.4826 * mad(residual), delta / 3, 1e-6)
        cutoff = max(delta, 1.5 * scale)
        weights = np.where(residual <= cutoff, 1.0, cutoff / np.maximum(residual, 1e-9))
        weighted_design = design * np.sqrt(weights)[:, None]
        coefficients_x = np.linalg.lstsq(weighted_design, centers[:, 0] * np.sqrt(weights), rcond=None)[0]
        coefficients_y = np.linalg.lstsq(weighted_design, centers[:, 1] * np.sqrt(weights), rcond=None)[0]
    coefficients = np.vstack((coefficients_x, coefficients_y))
    predicted = np.column_stack((design @ coefficients_x, design @ coefficients_y))
    return coefficients, np.linalg.norm(centers - predicted, axis=1)


def connected_components_xy(xy: np.ndarray, cell_size: float, minimum_points: int) -> list[np.ndarray]:
    if len(xy) < minimum_points:
        return []
    cells = np.floor(xy / cell_size).astype(np.int32)
    unique_cells, inverse = np.unique(cells, axis=0, return_inverse=True)
    lookup = {tuple(cell): index for index, cell in enumerate(unique_cells)}
    labels = np.full(len(unique_cells), -1, dtype=np.int32)
    component = 0
    for start in range(len(unique_cells)):
        if labels[start] >= 0:
            continue
        labels[start] = component
        stack = [start]
        while stack:
            current = stack.pop()
            cx, cy = unique_cells[current]
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    neighbour = lookup.get((int(cx + dx), int(cy + dy)))
                    if neighbour is not None and labels[neighbour] < 0:
                        labels[neighbour] = component
                        stack.append(neighbour)
        component += 1
    point_labels = labels[inverse]
    components = [np.flatnonzero(point_labels == label) for label in range(component)]
    return sorted((indexes for indexes in components if len(indexes) >= minimum_points), key=len, reverse=True)


def fit_circle_v2(
    xy: np.ndarray,
    origin: np.ndarray,
    config: dict,
    rng: np.random.Generator,
    *,
    full_resolution: bool = False,
) -> dict:
    fit_cfg = config["slice_fit"]
    radius_cfg = config["candidate_radius"]
    minimum_points = max(3, fit_cfg["minimum_trial_inliers"])
    if len(xy) < minimum_points:
        return {"valid": False, "rejection_reasons": ["TOO_FEW_COMPONENT_POINTS"]}
    stride = max(1, math.ceil(len(xy) / fit_cfg["maximum_fit_points"]))
    xy_fit = xy[::stride]
    trials = fit_cfg["full_resolution_ransac_trials"] if full_resolution else fit_cfg["sampled_ransac_trials"]
    counters: dict[str, int] = defaultdict(int)
    trial_indexes = rng.integers(0, len(xy_fit), size=(trials, 3))
    collisions = (
        (trial_indexes[:, 0] == trial_indexes[:, 1])
        | (trial_indexes[:, 0] == trial_indexes[:, 2])
        | (trial_indexes[:, 1] == trial_indexes[:, 2])
    )
    while collisions.any():
        trial_indexes[collisions] = rng.integers(0, len(xy_fit), size=(int(collisions.sum()), 3))
        collisions = (
            (trial_indexes[:, 0] == trial_indexes[:, 1])
            | (trial_indexes[:, 0] == trial_indexes[:, 2])
            | (trial_indexes[:, 1] == trial_indexes[:, 2])
        )
    a, b, c = (xy_fit[trial_indexes[:, index]] for index in range(3))
    divisor = 2 * (
        a[:, 0] * (b[:, 1] - c[:, 1])
        + b[:, 0] * (c[:, 1] - a[:, 1])
        + c[:, 0] * (a[:, 1] - b[:, 1])
    )
    valid = np.abs(divisor) >= 1e-10
    counters["DEGENERATE_TRIPLE"] = int((~valid).sum())
    ar2 = np.einsum("ij,ij->i", a, a)
    br2 = np.einsum("ij,ij->i", b, b)
    cr2 = np.einsum("ij,ij->i", c, c)
    safe_divisor = np.where(valid, divisor, 1.0)
    centers = np.column_stack(
        (
            (
                ar2 * (b[:, 1] - c[:, 1])
                + br2 * (c[:, 1] - a[:, 1])
                + cr2 * (a[:, 1] - b[:, 1])
            )
            / safe_divisor,
            (
                ar2 * (c[:, 0] - b[:, 0])
                + br2 * (a[:, 0] - c[:, 0])
                + cr2 * (b[:, 0] - a[:, 0])
            )
            / safe_divisor,
        )
    )
    radii = np.linalg.norm(a - centers, axis=1)
    below = radii < radius_cfg["minimum_m"]
    above = radii > radius_cfg["maximum_m"]
    distant = np.linalg.norm(centers - origin, axis=1) > fit_cfg["maximum_center_distance_m"]
    counters["RADIUS_BELOW_CANDIDATE_MINIMUM"] = int((valid & below).sum())
    counters["RADIUS_ABOVE_BROAD_CANDIDATE_LIMIT"] = int((valid & above).sum())
    counters["CENTER_OUTSIDE_SEARCH_RADIUS"] = int((valid & ~below & ~above & distant).sum())
    plausible = valid & ~below & ~above & ~distant
    centers = centers[plausible]
    radii = radii[plausible]
    if len(centers) == 0:
        return {
            "valid": False,
            "rejection_reasons": ["NO_CIRCLE_MODEL"],
            "trial_rejections": dict(counters),
        }
    tolerances = np.clip(
        radii * fit_cfg["inlier_tolerance_radius_fraction"],
        fit_cfg["inlier_tolerance_minimum_m"],
        fit_cfg["inlier_tolerance_maximum_m"],
    )
    errors = np.abs(
        np.linalg.norm(xy_fit[None, :, :] - centers[:, None, :], axis=2) - radii[:, None]
    )
    inlier_matrix = errors <= tolerances[:, None]
    inlier_counts = inlier_matrix.sum(axis=1)
    enough = inlier_counts >= fit_cfg["minimum_trial_inliers"]
    counters["TOO_FEW_TRIAL_INLIERS"] = int((~enough).sum())
    if not enough.any():
        return {
            "valid": False,
            "rejection_reasons": ["NO_CIRCLE_MODEL"],
            "trial_rejections": dict(counters),
        }
    squared_error = np.where(inlier_matrix, errors * errors, 0.0).sum(axis=1)
    preliminary_residual = np.sqrt(squared_error / np.maximum(inlier_counts, 1))
    preliminary_score = inlier_counts / (1.0 + 20.0 * preliminary_residual)
    candidate_indexes = np.flatnonzero(enough)
    maximum_scored = fit_cfg["maximum_scored_trials"]
    if len(candidate_indexes) > maximum_scored:
        ranking = np.argsort(preliminary_score[candidate_indexes])[-maximum_scored:]
        candidate_indexes = candidate_indexes[ranking]
    scores = np.full(len(centers), -np.inf)
    for index in candidate_indexes:
        inliers = inlier_matrix[index]
        coverage, _ = angular_metrics(xy_fit[inliers], centers[index])
        residual = float(np.sqrt(np.mean(errors[index, inliers] ** 2)))
        scores[index] = inlier_counts[index] * (0.20 + coverage / 360.0) / (1.0 + 20.0 * residual)
    best_index = int(np.argmax(scores))
    score = float(scores[best_index])
    center = centers[best_index]
    radius = float(radii[best_index])
    for _ in range(4):
        tolerance = float(
            np.clip(
                radius * fit_cfg["inlier_tolerance_radius_fraction"],
                fit_cfg["inlier_tolerance_minimum_m"],
                fit_cfg["inlier_tolerance_maximum_m"],
            )
        )
        errors = np.abs(np.linalg.norm(xy - center, axis=1) - radius)
        inliers = errors <= tolerance
        refined = v1.fit_circle_least_squares(xy[inliers])
        if refined is None:
            break
        next_center, next_radius = refined
        if not radius_cfg["minimum_m"] <= next_radius <= radius_cfg["maximum_m"]:
            break
        if np.linalg.norm(next_center - origin) > fit_cfg["maximum_center_distance_m"]:
            break
        center, radius = next_center, next_radius

    tolerance = float(
        np.clip(
            radius * fit_cfg["inlier_tolerance_radius_fraction"],
            fit_cfg["inlier_tolerance_minimum_m"],
            fit_cfg["inlier_tolerance_maximum_m"],
        )
    )
    radial_errors = np.linalg.norm(xy - center, axis=1) - radius
    inliers = np.abs(radial_errors) <= tolerance
    if inliers.sum() < fit_cfg["minimum_final_inliers"]:
        return {
            "valid": False,
            "rejection_reasons": ["TOO_FEW_FINAL_INLIERS"],
            "trial_rejections": dict(counters),
        }
    coverage, missing_sector = angular_metrics(xy[inliers], center)
    residual = float(np.sqrt(np.mean(radial_errors[inliers] ** 2)))
    valid = coverage >= fit_cfg["minimum_fit_coverage_deg"]
    return {
        "valid": bool(valid),
        "center": center,
        "radius_m": float(radius),
        "circle_residual_m": residual,
        "inlier_count": int(inliers.sum()),
        "angular_coverage_deg": coverage,
        "largest_missing_angular_sector_deg": missing_sector,
        "score": float(score),
        "inlier_tolerance_m": tolerance,
        "rejection_reasons": [] if valid else ["ANGULAR_COVERAGE_BELOW_SLICE_MINIMUM"],
        "trial_rejections": dict(counters),
        "_inlier_mask": inliers,
    }


def fit_ellipse_robust(xy: np.ndarray, config: dict) -> dict:
    if len(xy) < 5:
        return {"valid": False, "rejection_reasons": ["TOO_FEW_ELLIPSE_POINTS"]}
    points = xy.copy()
    inliers = np.ones(len(points), dtype=bool)
    model = None

    def approximate_geometric_residuals(current_model) -> np.ndarray:
        center = np.asarray(current_model.center)
        axis_a, axis_b = current_model.axis_lengths
        theta = current_model.theta
        cosine, sine = math.cos(theta), math.sin(theta)
        relative = points - center
        u = cosine * relative[:, 0] + sine * relative[:, 1]
        v = -sine * relative[:, 0] + cosine * relative[:, 1]
        implicit = (u / axis_a) ** 2 + (v / axis_b) ** 2 - 1.0
        gradient = 2.0 * np.sqrt((u / axis_a**2) ** 2 + (v / axis_b**2) ** 2)
        return np.abs(implicit) / np.maximum(gradient, 1e-9)

    for _ in range(4):
        if inliers.sum() < 5:
            return {"valid": False, "rejection_reasons": ["ELLIPSE_FIT_FAILED"]}
        model = EllipseModel.from_estimate(points[inliers])
        if not model:
            return {"valid": False, "rejection_reasons": ["ELLIPSE_FIT_FAILED"]}
        residuals = approximate_geometric_residuals(model)
        median = float(np.median(residuals[inliers]))
        scale = max(
            1.4826 * mad(residuals[inliers]),
            config["slice_fit"]["ellipse_minimum_residual_threshold_m"],
        )
        threshold = median + config["slice_fit"]["ellipse_trim_mad_multiplier"] * scale
        next_inliers = residuals <= threshold
        if np.array_equal(next_inliers, inliers):
            break
        inliers = next_inliers
    xc, yc = model.center
    axis_a, axis_b = model.axis_lengths
    theta = model.theta
    major = max(float(axis_a), float(axis_b))
    minor = min(float(axis_a), float(axis_b))
    residuals = approximate_geometric_residuals(model)
    residual = float(np.sqrt(np.mean(residuals[inliers] ** 2)))
    return {
        "valid": True,
        "center": np.asarray([xc, yc]),
        "semi_major_axis_m": major,
        "semi_minor_axis_m": minor,
        "rotation_rad": float(theta),
        "ellipse_residual_m": residual,
        "inlier_count": int(inliers.sum()),
        "rejection_reasons": [],
    }


def fit_slice_profile(
    xy: np.ndarray,
    origin: np.ndarray,
    config: dict,
    rng: np.random.Generator,
    *,
    full_resolution: bool = False,
) -> dict:
    fit_cfg = config["slice_fit"]
    components = connected_components_xy(
        xy,
        fit_cfg["component_grid_cell_m"],
        fit_cfg["minimum_component_points"],
    )
    fits = []
    rejected = []
    for component_index, point_indexes in enumerate(components[: fit_cfg["maximum_components_to_fit"]]):
        component_xy = xy[point_indexes]
        circle = fit_circle_v2(component_xy, origin, config, rng, full_resolution=full_resolution)
        if not circle["valid"]:
            rejected.append(
                {
                    "component_index": component_index,
                    "point_count": int(len(component_xy)),
                    "rejection_reasons": circle["rejection_reasons"],
                    "trial_rejections": circle.get("trial_rejections", {}),
                }
            )
            continue
        span = np.ptp(component_xy, axis=0)
        density_area = max(float(span[0] * span[1]), fit_cfg["component_grid_cell_m"] ** 2)
        fit = {
            **circle,
            "component_index": component_index,
            "component_point_count": int(len(component_xy)),
            "local_point_density_per_m2": float(len(component_xy) / density_area),
            "_component_point_indexes": point_indexes,
        }
        fits.append(fit)
    fits.sort(
        key=lambda item: (
            item["inlier_count"] * (0.20 + item["angular_coverage_deg"] / 360.0)
            / (1.0 + 20.0 * item["circle_residual_m"])
        ),
        reverse=True,
    )
    fits = fits[: fit_cfg["maximum_fits_to_retain"]]
    for fit in fits:
        component_xy = xy[fit["_component_point_indexes"]]
        ellipse_points = component_xy[fit["_inlier_mask"]]
        fit["ellipse"] = fit_ellipse_robust(ellipse_points, config)
    return {
        "point_count": int(len(xy)),
        "connected_component_count": int(len(components)),
        "candidate_centres": [json_ready(fit["center"]) for fit in fits],
        "fits": fits,
        "fit_validity": bool(fits),
        "rejection_reasons": [] if fits else ["NO_PLAUSIBLE_SLICE_FIT"],
        "rejected_components": rejected,
    }


class V1DensitySeedProvider:
    name = "V1_DENSITY"

    def generate(self, points: np.ndarray, config: dict) -> list[SeedRecord]:
        seeds, _, _ = v1.candidate_seeds(points, config["analysis"]["global_ground_hint_m"])
        return [
            SeedRecord(
                seed_id=f"V1-{index:04d}",
                source=self.name,
                source_height_m=config["adaptive_measurement"]["standard_height_m"],
                x=float(seed[0]),
                y=float(seed[1]),
                source_index=index,
            )
            for index, seed in enumerate(seeds, start=1)
        ]


class MultiHeightDensitySeedProvider:
    name = "MULTI_HEIGHT_DENSITY"

    def generate(self, points: np.ndarray, config: dict) -> list[SeedRecord]:
        cfg = config["seed_generation"]
        xmin, xmax, ymin, ymax = config["analysis"]["bounds"]
        ground_resolution = cfg["local_ground_grid_resolution_m"]
        ground_nx = math.ceil((xmax - xmin) / ground_resolution)
        ground_ny = math.ceil((ymax - ymin) / ground_resolution)
        ground_ix = np.clip(((points[:, 0] - xmin) / ground_resolution).astype(np.int32), 0, ground_nx - 1)
        ground_iy = np.clip(((points[:, 1] - ymin) / ground_resolution).astype(np.int32), 0, ground_ny - 1)
        ground_keys = ground_iy * ground_nx + ground_ix
        order = np.argsort(ground_keys)
        sorted_keys = ground_keys[order]
        boundaries = np.flatnonzero(np.r_[True, sorted_keys[1:] != sorted_keys[:-1], True])
        ground_grid = np.full(ground_nx * ground_ny, np.nan)
        for left, right in zip(boundaries[:-1], boundaries[1:]):
            if right - left < cfg["local_ground_minimum_cell_points"]:
                continue
            key = int(sorted_keys[left])
            ground_grid[key] = np.percentile(
                points[order[left:right], 2], cfg["local_ground_percentile"]
            )
        known = np.flatnonzero(np.isfinite(ground_grid))
        if len(known):
            all_cells = np.arange(ground_nx * ground_ny)
            known_xy = np.column_stack((known % ground_nx, known // ground_nx))
            all_xy = np.column_stack((all_cells % ground_nx, all_cells // ground_nx))
            _, nearest = cKDTree(known_xy).query(all_xy)
            ground_grid = ground_grid[known[nearest]]
        else:
            ground_grid.fill(config["analysis"]["global_ground_hint_m"])
        ground_grid = np.clip(
            ground_grid,
            config["analysis"]["global_ground_hint_m"]
            - config["analysis"]["local_ground_clip_below_hint_m"],
            config["analysis"]["global_ground_hint_m"]
            + config["analysis"]["local_ground_clip_above_hint_m"],
        )
        point_ground = ground_grid[ground_keys]
        resolution = cfg["grid_resolution_m"]
        nx = int((xmax - xmin) / resolution)
        ny = int((ymax - ymin) / resolution)
        ix = np.clip(((points[:, 0] - xmin) / resolution).astype(np.int32), 0, nx - 1)
        iy = np.clip(((points[:, 1] - ymin) / resolution).astype(np.int32), 0, ny - 1)
        hag = points[:, 2] - point_ground
        records: list[SeedRecord] = []
        running_index = 1
        for height in heights_inclusive(cfg["min_height_m"], cfg["max_height_m"], cfg["step_m"]):
            mask = np.abs(hag - height) <= cfg["slab_thickness_m"] / 2
            if mask.sum() < cfg["minimum_points_in_slab"]:
                continue
            density = np.bincount(iy[mask] * nx + ix[mask], minlength=nx * ny).reshape(ny, nx).astype(float)
            density = gaussian_filter(density, cfg["gaussian_sigma_cells"])
            positive = density[density > 0]
            if len(positive) == 0:
                continue
            threshold = float(np.percentile(positive, cfg["peak_percentile"]))
            peaks = peak_local_max(
                density,
                min_distance=cfg["minimum_peak_distance_cells"],
                threshold_abs=threshold,
                exclude_border=False,
            )
            for peak in peaks:
                records.append(
                    SeedRecord(
                        seed_id=f"MH-{running_index:05d}",
                        source=self.name,
                        source_height_m=height,
                        x=float(xmin + (peak[1] + 0.5) * resolution),
                        y=float(ymin + (peak[0] + 0.5) * resolution),
                        source_index=running_index,
                    )
                )
                running_index += 1
        return records


def load_manual_seeds(path: Path | None) -> list[SeedRecord]:
    if path is None:
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        SeedRecord(
            seed_id=f"MANUAL-{index:04d}",
            source="MANUAL_DIAGNOSTIC",
            source_height_m=item.get("height_m"),
            x=float(item["x"]),
            y=float(item["y"]),
            source_index=index,
        )
        for index, item in enumerate(payload, start=1)
    ]


def group_seeds_non_destructive(seeds: list[SeedRecord], config: dict) -> list[dict]:
    if not seeds:
        return []
    alias_distance = config["candidate_grouping"]["alias_distance_m"]
    v1_indexes = sorted(
        (index for index, seed in enumerate(seeds) if seed.source == "V1_DENSITY"),
        key=lambda index: seeds[index].source_index or 0,
    )
    remaining_indexes = sorted(
        (index for index, seed in enumerate(seeds) if seed.source != "V1_DENSITY"),
        key=lambda index: (
            0 if seeds[index].source == "MANUAL_DIAGNOSTIC" else 1,
            seeds[index].source_height_m if seeds[index].source_height_m is not None else -1,
            seeds[index].source_index or 0,
        ),
    )
    # Every V1 seed stays an independent anchor.  Other providers can alias an
    # anchor, but an alias never moves the anchor and never links two groups by
    # transitive chaining.
    groups = [{"anchor": np.asarray([seeds[index].x, seeds[index].y]), "indexes": [index]} for index in v1_indexes]
    for index in remaining_indexes:
        position = np.asarray([seeds[index].x, seeds[index].y])
        if groups:
            distances = np.asarray([np.linalg.norm(position - group["anchor"]) for group in groups])
            nearest = int(np.argmin(distances))
        else:
            distances = np.asarray([])
            nearest = -1
        if nearest >= 0 and distances[nearest] <= alias_distance:
            groups[nearest]["indexes"].append(index)
        else:
            groups.append({"anchor": position, "indexes": [index]})

    grouped = []
    for candidate_number, group in enumerate(groups, start=1):
        indexes = group["indexes"]
        aliases = [seeds[index] for index in indexes]
        center = group["anchor"]
        grouped.append(
            {
                "candidate_id": f"C-{candidate_number:04d}",
                "position": {"x": float(center[0]), "y": float(center[1])},
                "source_seeds": aliases,
                "seed_relationships": [
                    {
                        **seed.to_dict(),
                        "offset_from_group_m": rounded(math.hypot(seed.x - center[0], seed.y - center[1])),
                    }
                    for seed in aliases
                ],
            }
        )
    return grouped


def estimate_local_ground(local_points: np.ndarray, config: dict) -> float:
    analysis = config["analysis"]
    estimate = float(np.percentile(local_points[:, 2], analysis["local_ground_percentile"]))
    return float(
        np.clip(
            estimate,
            analysis["global_ground_hint_m"] - analysis["local_ground_clip_below_hint_m"],
            analysis["global_ground_hint_m"] + analysis["local_ground_clip_above_hint_m"],
        )
    )


def build_candidate_profile(
    candidate: dict,
    points: np.ndarray,
    spatial_tree: cKDTree,
    config: dict,
) -> tuple[float | None, list[dict]]:
    center = np.asarray([candidate["position"]["x"], candidate["position"]["y"]])
    indexes = spatial_tree.query_ball_point(center, config["analysis"]["sampled_neighborhood_radius_m"])
    heights = heights_inclusive(
        config["height_profile"]["min_height_m"],
        config["height_profile"]["max_height_m"],
        config["height_profile"]["step_m"],
    )
    if not indexes:
        return None, [
            {
                "height_m": height,
                "point_count": 0,
                "connected_component_count": 0,
                "candidate_centres": [],
                "fits": [],
                "fit_validity": False,
                "rejection_reasons": ["NO_LOCAL_POINTS"],
            }
            for height in heights
        ]
    local = points[indexes]
    ground = estimate_local_ground(local, config)
    hag = local[:, 2] - ground
    slab_half = config["height_profile"]["slab_thickness_m"] / 2
    base_seed = int(candidate["candidate_id"].split("-")[1]) + config["random_seed"]
    rng = np.random.default_rng(base_seed)
    profile = []
    for height in heights:
        section = local[np.abs(hag - height) <= slab_half, :2]
        fitted = fit_slice_profile(section, center, config, rng)
        profile.append({"height_m": height, **fitted})
    return ground, profile


def maximum_consecutive_missing(slice_entries: list[dict], selected_heights: set[float]) -> int:
    longest = current = 0
    for entry in slice_entries:
        if entry["height_m"] in selected_heights:
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return longest


def _window_hypotheses(options: list[tuple[float, list[dict]]]) -> Iterable[tuple[np.ndarray, np.ndarray]]:
    flat = [(height, fit) for height, fits in options for fit in fits]
    for left_index, (left_height, left_fit) in enumerate(flat):
        for right_height, right_fit in flat[left_index + 1 :]:
            delta = right_height - left_height
            if abs(delta) < 0.10:
                continue
            left_center = np.asarray(left_fit["center"])
            right_center = np.asarray(right_fit["center"])
            slope = (right_center - left_center) / delta
            intercept = left_center - slope * left_height
            yield np.asarray([slope[0], intercept[0]]), np.asarray([slope[1], intercept[1]])


def select_window_track(slice_entries: list[dict], config: dict) -> dict | None:
    options = [(entry["height_m"], [fit for fit in entry["fits"] if fit["valid"]]) for entry in slice_entries]
    options = [(height, fits) for height, fits in options if fits]
    if len(options) < 2:
        return None
    maximum_distance = config["tracking"]["maximum_fit_to_line_distance_m"]
    hypotheses = list(_window_hypotheses(options))
    if not hypotheses:
        return None
    coefficient_x = np.asarray([item[0] for item in hypotheses])
    coefficient_y = np.asarray([item[1] for item in hypotheses])
    support = np.zeros(len(hypotheses), dtype=np.int16)
    distance_sum = np.zeros(len(hypotheses), dtype=float)
    for height, fits in options:
        predicted = np.column_stack(
            (
                coefficient_x[:, 0] * height + coefficient_x[:, 1],
                coefficient_y[:, 0] * height + coefficient_y[:, 1],
            )
        )
        fit_centers = np.asarray([fit["center"] for fit in fits])
        nearest = np.min(
            np.linalg.norm(predicted[:, None, :] - fit_centers[None, :, :], axis=2),
            axis=1,
        )
        compatible = nearest <= maximum_distance
        support += compatible
        distance_sum += np.where(compatible, nearest, 0.0)
    preliminary_scores = support * 100.0 - 250.0 * distance_sum
    ranked_hypotheses = [
        (preliminary_scores[index], hypotheses[index][0], hypotheses[index][1])
        for index in np.flatnonzero(support >= 2)
    ]
    ranked_hypotheses.sort(key=lambda item: item[0], reverse=True)
    ranked_hypotheses = ranked_hypotheses[: config["tracking"]["maximum_centreline_hypotheses"]]
    best = None
    for _, coefficients_x, coefficients_y in ranked_hypotheses:
        selected = []
        for height, fits in options:
            predicted = np.asarray(
                [coefficients_x[0] * height + coefficients_x[1], coefficients_y[0] * height + coefficients_y[1]]
            )
            distances = [np.linalg.norm(np.asarray(fit["center"]) - predicted) for fit in fits]
            index = int(np.argmin(distances))
            if distances[index] <= maximum_distance:
                selected.append((height, index, fits[index]))
        if len(selected) < 2:
            continue
        for _ in range(2):
            heights = np.asarray([item[0] for item in selected])
            centers = np.asarray([item[2]["center"] for item in selected])
            coefficients, _ = robust_centreline(heights, centers, config)
            selected = []
            for height, fits in options:
                predicted = np.asarray(
                    [coefficients[0, 0] * height + coefficients[0, 1], coefficients[1, 0] * height + coefficients[1, 1]]
                )
                distances = [np.linalg.norm(np.asarray(fit["center"]) - predicted) for fit in fits]
                index = int(np.argmin(distances))
                if distances[index] <= maximum_distance:
                    selected.append((height, index, fits[index]))
            if len(selected) < 2:
                break
        if len(selected) < 2:
            continue
        heights = np.asarray([item[0] for item in selected])
        centers = np.asarray([item[2]["center"] for item in selected])
        radii = np.asarray([item[2]["radius_m"] for item in selected])
        coefficients, center_residuals = robust_centreline(heights, centers, config)
        radius_coefficients, radius_residuals = robust_scalar_line(
            heights,
            radii,
            config["tracking"]["robust_iterations"],
            config["tracking"]["huber_delta_m"],
        )
        raw_spread = float(np.max(np.linalg.norm(centers - np.median(centers, axis=0), axis=1)))
        raw_cv = float(np.std(radii) / max(np.mean(radii), 1e-9))
        coverage = np.asarray([item[2]["angular_coverage_deg"] for item in selected])
        fit_residual = np.asarray([item[2]["circle_residual_m"] for item in selected])
        score = (
            len(selected) * 100
            + float(np.median(coverage))
            - float(np.percentile(center_residuals, 90)) * 900
            - mad(radius_residuals) * 900
            - float(np.median(fit_residual)) * 500
        )
        result = {
            "selected": selected,
            "centreline_coefficients": coefficients,
            "radius_coefficients": radius_coefficients,
            "centre_residual_p90_m": float(np.percentile(center_residuals, 90)),
            "radius_residual_mad_m": mad(radius_residuals),
            "raw_centre_spread_m": raw_spread,
            "raw_radius_cv": raw_cv,
            "median_radius_m": float(np.median(radii)),
            "median_angular_coverage_deg": float(np.median(coverage)),
            "median_fit_residual_m": float(np.median(fit_residual)),
            "score": float(score),
        }
        if best is None or score > best["score"]:
            best = result
    return best


def evaluate_window_flags(
    metrics: dict,
    slice_entries: list[dict],
    config: dict,
    *,
    threshold_scale: float = 1.0,
    coverage_scale: float = 1.0,
) -> tuple[bool, bool, dict]:
    radius = metrics["median_radius_m"]
    selected_heights = {item[0] for item in metrics["selected"]}
    missing = maximum_consecutive_missing(slice_entries, selected_heights)
    detection = config["stable_window"]
    automatic = config["automatic_measurement_window"]

    def passes(section: dict, minimum_slices: int) -> bool:
        center_limit = max(section["centre_residual_base_m"], section["centre_residual_radius_fraction"] * radius)
        radius_limit = max(section["radius_residual_base_m"], section["radius_residual_radius_fraction"] * radius)
        residual_limit = max(section["fit_residual_base_m"], section["fit_residual_radius_fraction"] * radius)
        return bool(
            len(selected_heights) >= minimum_slices
            and missing <= section["maximum_consecutive_missing_slices"]
            and config["candidate_radius"]["minimum_m"] <= radius <= config["candidate_radius"]["maximum_m"]
            and metrics["centre_residual_p90_m"] <= center_limit * threshold_scale
            and metrics["radius_residual_mad_m"] <= radius_limit * threshold_scale
            and metrics["median_angular_coverage_deg"] >= section["minimum_median_angular_coverage_deg"] * coverage_scale
            and metrics["median_fit_residual_m"] <= residual_limit * threshold_scale
        )

    detection_pass = passes(detection, detection["minimum_valid_slices"])
    automatic_pass = passes(automatic, automatic["minimum_valid_slices"])
    thresholds = {
        "maximum_consecutive_missing_slices": missing,
        "detection_centre_limit_m": max(
            detection["centre_residual_base_m"], detection["centre_residual_radius_fraction"] * radius
        )
        * threshold_scale,
        "detection_radius_limit_m": max(
            detection["radius_residual_base_m"], detection["radius_residual_radius_fraction"] * radius
        )
        * threshold_scale,
        "automatic_centre_limit_m": max(
            automatic["centre_residual_base_m"], automatic["centre_residual_radius_fraction"] * radius
        )
        * threshold_scale,
        "automatic_radius_limit_m": max(
            automatic["radius_residual_base_m"], automatic["radius_residual_radius_fraction"] * radius
        )
        * threshold_scale,
    }
    return detection_pass, automatic_pass, thresholds


def evaluate_stable_windows(profile: list[dict], config: dict) -> list[dict]:
    step = config["height_profile"]["step_m"]
    width = config["stable_window"]["width_m"]
    window_count = int(round(width / step)) + 1
    windows = []
    for start in range(0, len(profile) - window_count + 1):
        entries = profile[start : start + window_count]
        metrics = select_window_track(entries, config)
        if metrics is None:
            windows.append(
                {
                    "start_height_m": entries[0]["height_m"],
                    "end_height_m": entries[-1]["height_m"],
                    "detection_quality": False,
                    "automatic_measurement_quality": False,
                    "reason_codes": ["INSUFFICIENT_TRACKABLE_SLICES"],
                }
            )
            continue
        detection, automatic, thresholds = evaluate_window_flags(metrics, entries, config)
        windows.append(
            {
                "start_height_m": entries[0]["height_m"],
                "end_height_m": entries[-1]["height_m"],
                "supporting_slice_count": len(metrics["selected"]),
                "selected_slices": [
                    {
                        "height_m": height,
                        "fit_index": fit_index,
                        "center": json_ready(fit["center"]),
                        "radius_m": fit["radius_m"],
                        "angular_coverage_deg": fit["angular_coverage_deg"],
                        "circle_residual_m": fit["circle_residual_m"],
                    }
                    for height, fit_index, fit in metrics["selected"]
                ],
                "centreline_coefficients": json_ready(metrics["centreline_coefficients"]),
                "radius_trend_coefficients": json_ready(metrics["radius_coefficients"]),
                "centre_residual_p90_m": metrics["centre_residual_p90_m"],
                "radius_residual_mad_m": metrics["radius_residual_mad_m"],
                "raw_centre_spread_m": metrics["raw_centre_spread_m"],
                "raw_radius_cv": metrics["raw_radius_cv"],
                "median_radius_m": metrics["median_radius_m"],
                "median_angular_coverage_deg": metrics["median_angular_coverage_deg"],
                "median_fit_residual_m": metrics["median_fit_residual_m"],
                "detection_quality": detection,
                "automatic_measurement_quality": automatic,
                "thresholds": thresholds,
                "score": metrics["score"],
                "reason_codes": [] if detection else ["WINDOW_STABILITY_CRITERIA_NOT_MET"],
            }
        )
    return windows


def _window_selected_slice(window: dict, height: float, tolerance: float = 1e-6) -> dict | None:
    return next(
        (item for item in window.get("selected_slices", []) if abs(item["height_m"] - height) <= tolerance),
        None,
    )


def legacy_comparison_metrics(profile: list[dict], selected_window: dict) -> dict:
    legacy_heights = (0.75, 1.00, 1.30, 1.60, 1.90)
    coefficients = np.asarray(selected_window["centreline_coefficients"])
    selected = []
    for height in legacy_heights:
        entry = next((item for item in profile if abs(item["height_m"] - height) <= 1e-6), None)
        if entry is None or not entry["fits"]:
            continue
        predicted = np.asarray(
            [coefficients[0, 0] * height + coefficients[0, 1], coefficients[1, 0] * height + coefficients[1, 1]]
        )
        fit = min(entry["fits"], key=lambda item: np.linalg.norm(np.asarray(item["center"]) - predicted))
        selected.append((height, fit))
    if len(selected) < 3:
        return {"compatible_slice_count": len(selected), "raw_centre_spread_m": None, "raw_radius_cv": None}
    centers = np.asarray([fit["center"] for _, fit in selected])
    radii = np.asarray([fit["radius_m"] for _, fit in selected])
    return {
        "compatible_slice_count": len(selected),
        "selected_heights_m": [height for height, _ in selected],
        "raw_centre_spread_m": float(
            np.max(np.linalg.norm(centers - np.median(centers, axis=0), axis=1))
        ),
        "raw_radius_cv": float(np.std(radii) / max(np.mean(radii), 1e-9)),
        "maximum_radius_m": float(np.max(radii)),
    }


def _candidate_recovery_reasons(window: dict, legacy: dict) -> list[str]:
    reasons = []
    if max(window.get("median_radius_m", 0), legacy.get("maximum_radius_m") or 0) > 0.30:
        reasons.append("RECOVERED_OLD_RADIUS_CAP")
    if (legacy.get("raw_centre_spread_m") or 0) > 0.14:
        reasons.append("RECOVERED_CENTRELINE_RESIDUAL")
    if (legacy.get("raw_radius_cv") or 0) > 0.38:
        reasons.append("RECOVERED_LOCAL_RADIUS_STABILITY")
    return reasons


def evaluate_candidate_profile(candidate: dict, ground: float | None, profile: list[dict], config: dict) -> CandidateEvaluation:
    source_seeds: list[SeedRecord] = candidate["source_seeds"]
    evaluation = CandidateEvaluation(
        algorithm_version=config["algorithm_version"],
        candidate_id=candidate["candidate_id"],
        source_seed_ids=[seed.seed_id for seed in source_seeds],
        seed_sources=sorted({seed.source for seed in source_seeds}),
        source_heights_m=sorted({seed.source_height_m for seed in source_seeds if seed.source_height_m is not None}),
        position=dict(candidate["position"]),
        seed_relationships=candidate["seed_relationships"],
        ground_z_m=ground,
    )
    windows = evaluate_stable_windows(profile, config)
    detection_windows = [window for window in windows if window.get("detection_quality")]
    automatic_windows = [window for window in windows if window.get("automatic_measurement_quality")]
    valid_slice_count = sum(bool(entry["fits"]) for entry in profile)
    evaluation.diagnostics = {
        "profile": json_ready(profile),
        "stable_windows": windows,
        "valid_slice_count": valid_slice_count,
    }

    standard_height = config["adaptive_measurement"]["standard_height_m"]
    minimum_height = config["adaptive_measurement"]["minimum_height_m"]
    maximum_height = config["adaptive_measurement"]["maximum_height_m"]
    standard_windows = [
        window
        for window in automatic_windows
        if window["start_height_m"] <= standard_height <= window["end_height_m"]
        and _window_selected_slice(window, standard_height) is not None
    ]
    upward = [
        window
        for window in automatic_windows
        if window["end_height_m"] >= minimum_height and window["start_height_m"] <= maximum_height
        and any(
            minimum_height <= item["height_m"] <= maximum_height
            for item in window.get("selected_slices", [])
        )
    ]
    selected_window = None
    if standard_windows:
        selected_window = max(standard_windows, key=lambda window: window["score"])
        evaluation.candidate_status = "CONFIRMED_STEM"
        evaluation.measurement_status = "MEASURABLE_STANDARD_1_30"
        evaluation.measurement_rule = "STANDARD_1_30"
        evaluation.measurement_height_m = standard_height
        evaluation.reason_codes = ["STANDARD_HEIGHT_STABLE"]
    elif upward:
        lowest_window = min(upward, key=lambda window: (window["start_height_m"], -window["score"]))
        profile_step = config["height_profile"]["step_m"]
        lowest_supported_height = min(
            item["height_m"] for item in lowest_window["selected_slices"]
        )
        irregular_top = max(standard_height, lowest_supported_height - profile_step)
        preferred = max(
            standard_height,
            irregular_top + config["adaptive_measurement"]["preferred_clearance_above_irregular_zone_m"],
        )
        supported = [
            (window, item)
            for window in upward
            for item in window["selected_slices"]
            if minimum_height <= item["height_m"] <= maximum_height
        ]
        selected_window, chosen = min(
            supported,
            key=lambda pair: (
                abs(pair[1]["height_m"] - preferred),
                -pair[1]["angular_coverage_deg"],
                -pair[0]["score"],
            ),
        )
        evaluation.candidate_status = "CONFIRMED_STEM"
        evaluation.measurement_status = "MEASURABLE_ADAPTIVE_HEIGHT"
        evaluation.measurement_rule = "ADAPTIVE_STABLE_STEM"
        evaluation.measurement_height_m = chosen["height_m"]
        evaluation.irregular_zone_top_height_m = irregular_top
        evaluation.reason_codes = [
            "STANDARD_HEIGHT_UNSTABLE",
            "LOWER_STEM_IRREGULAR",
            "POSSIBLE_PROP_ROOT_ZONE",
            "ADAPTIVE_HEIGHT_SELECTED",
        ]
        lower_radii = [
            fit["radius_m"]
            for entry in profile
            if standard_height <= entry["height_m"] < lowest_supported_height
            for fit in entry["fits"][:1]
        ]
        if lower_radii and max(lower_radii) > (
            selected_window["median_radius_m"]
            * config["candidate_classification"]["possible_large_lower_radius_ratio"]
        ):
            evaluation.reason_codes.append("LARGE_LOWER_COMPONENT")
    elif detection_windows:
        selected_window = max(detection_windows, key=lambda window: window["score"])
        evaluation.candidate_status = "CONFIRMED_STEM"
        evaluation.measurement_status = "NEEDS_REVIEW"
        evaluation.reason_codes = ["DETECTION_WINDOW_ONLY", "AUTOMATIC_MEASUREMENT_CRITERIA_NOT_MET"]
    elif valid_slice_count >= config["candidate_classification"]["minimum_compatible_slices_for_insufficient_coverage"]:
        evaluation.candidate_status = "CANDIDATE"
        evaluation.measurement_status = "INSUFFICIENT_COVERAGE"
        evaluation.reason_codes = ["MULTI_SLICE_EVIDENCE", "NO_DETECTION_QUALITY_STABLE_WINDOW"]
    else:
        evaluation.candidate_status = "REJECTED_GEOMETRY"
        evaluation.measurement_status = "INSUFFICIENT_COVERAGE"
        evaluation.reason_codes = ["NO_STABLE_STEM_WINDOW", "INSUFFICIENT_COMPATIBLE_GEOMETRY"]

    if selected_window is not None:
        legacy = legacy_comparison_metrics(profile, selected_window)
        evaluation.centreline_residual_p90_m = rounded(selected_window["centre_residual_p90_m"])
        evaluation.radius_residual_mad_m = rounded(selected_window["radius_residual_mad_m"])
        evaluation.raw_centre_spread_m = rounded(selected_window["raw_centre_spread_m"])
        evaluation.raw_radius_cv = rounded(selected_window["raw_radius_cv"])
        evaluation.angular_coverage_deg = rounded(selected_window["median_angular_coverage_deg"], 2)
        evaluation.supporting_slice_count = selected_window["supporting_slice_count"]
        evaluation.reason_codes.extend(_candidate_recovery_reasons(selected_window, legacy))
        evaluation.tree_presence_confidence = rounded(
            min(0.99, 0.45 + 0.06 * evaluation.supporting_slice_count + selected_window["median_angular_coverage_deg"] / 900)
        )
        center_limit = max(0.05, 0.20 * selected_window["median_radius_m"])
        radius_limit = max(0.025, 0.20 * selected_window["median_radius_m"])
        evaluation.stem_tracking_confidence = rounded(
            max(
                0.0,
                min(
                    0.99,
                    1.0
                    - 0.35 * selected_window["centre_residual_p90_m"] / center_limit
                    - 0.35 * selected_window["radius_residual_mad_m"] / radius_limit,
                ),
            )
        )
        evaluation.measurement_confidence = (
            rounded(0.5 * evaluation.tree_presence_confidence + 0.5 * evaluation.stem_tracking_confidence)
            if evaluation.measurement_status.startswith("MEASURABLE_")
            else 0.0
        )
        evaluation.diagnostics["selected_window"] = selected_window
        evaluation.diagnostics["legacy_v1_height_comparison"] = legacy
    evaluation.reason_codes = list(dict.fromkeys(evaluation.reason_codes))
    return evaluation


def apply_duplicate_statuses(evaluations: list[CandidateEvaluation], config: dict) -> None:
    eligible = [evaluation for evaluation in evaluations if evaluation.candidate_status == "CONFIRMED_STEM"]
    eligible.sort(key=lambda item: (item.tree_presence_confidence, item.measurement_confidence), reverse=True)
    kept: list[CandidateEvaluation] = []
    threshold = config["candidate_grouping"]["duplicate_distance_m"]
    for evaluation in eligible:
        center = np.asarray([evaluation.position["x"], evaluation.position["y"]])
        duplicate = next(
            (
                other
                for other in kept
                if np.linalg.norm(
                    center - np.asarray([other.position["x"], other.position["y"]])
                )
                < threshold
            ),
            None,
        )
        if duplicate is None:
            kept.append(evaluation)
            continue
        evaluation.candidate_status = "REJECTED_DUPLICATE"
        evaluation.duplicate_of_candidate_id = duplicate.candidate_id
        evaluation.reason_codes.append("ALIASES_HIGHER_CONFIDENCE_CANDIDATE")


def sensitivity_counts(evaluations: list[CandidateEvaluation], config: dict) -> dict:
    counts = {}
    for name, variant in config["sensitivity"]["variants"].items():
        detection = automatic = 0
        for evaluation in evaluations:
            profile = evaluation.diagnostics.get("profile", [])
            windows = evaluation.diagnostics.get("stable_windows", [])
            variant_detection = variant_automatic = False
            width_count = int(round(config["stable_window"]["width_m"] / config["height_profile"]["step_m"])) + 1
            for index, window in enumerate(windows):
                if "selected_slices" not in window:
                    continue
                entries = profile[index : index + width_count]
                metrics = {
                    "selected": [
                        (
                            item["height_m"],
                            item["fit_index"],
                            {
                                "center": np.asarray(item["center"]),
                                "radius_m": item["radius_m"],
                                "angular_coverage_deg": item["angular_coverage_deg"],
                                "circle_residual_m": item["circle_residual_m"],
                            },
                        )
                        for item in window["selected_slices"]
                    ],
                    "centre_residual_p90_m": window["centre_residual_p90_m"],
                    "radius_residual_mad_m": window["radius_residual_mad_m"],
                    "median_radius_m": window["median_radius_m"],
                    "median_angular_coverage_deg": window["median_angular_coverage_deg"],
                    "median_fit_residual_m": window["median_fit_residual_m"],
                }
                det, auto, _ = evaluate_window_flags(
                    metrics,
                    entries,
                    config,
                    threshold_scale=variant["threshold_scale"],
                    coverage_scale=variant["coverage_scale"],
                )
                variant_detection |= det
                variant_automatic |= auto
            detection += int(variant_detection)
            automatic += int(variant_automatic)
        counts[name] = {"detection_quality_candidates": detection, "automatic_measurement_candidates": automatic}
    return counts


def _las_header(source: Path) -> tuple[np.memmap, np.ndarray, np.ndarray, int]:
    header = source.open("rb").read(227)
    point_offset = struct.unpack_from("<I", header, 96)[0]
    record_length = struct.unpack_from("<H", header, 105)[0]
    point_count = struct.unpack_from("<I", header, 107)[0]
    scale = np.asarray(struct.unpack_from("<3d", header, 131))
    offset = np.asarray(struct.unpack_from("<3d", header, 155))
    dtype = np.dtype(
        {"names": ["xyz"], "formats": [("<i4", (3,))], "offsets": [0], "itemsize": record_length}
    )
    source_map = np.memmap(source, dtype=dtype, mode="r", offset=point_offset, shape=(point_count,))
    return source_map, scale, offset, point_count


def extract_full_resolution_neighborhoods(
    source: Path,
    evaluations: list[CandidateEvaluation],
    config: dict,
    viewer_data_dir: Path,
) -> dict[str, np.ndarray]:
    measurable = [
        evaluation
        for evaluation in evaluations
        if evaluation.candidate_status == "CONFIRMED_STEM"
        and evaluation.measurement_status in {"MEASURABLE_STANDARD_1_30", "MEASURABLE_ADAPTIVE_HEIGHT"}
    ]
    if not measurable:
        return {}
    source_map, scale, offset, point_count = _las_header(source)
    viewer_first = np.fromfile(sorted(viewer_data_dir.glob("positions-*.glbin"))[0], dtype="<f4", count=3)
    source_first = source_map[0]["xyz"].astype(np.float64) * scale + offset
    viewer_center = source_first - viewer_first
    bounds = config["analysis"]["bounds"]
    xmin, xmax, ymin, ymax = bounds
    full_cfg = config["full_resolution"]
    cell_size = full_cfg["grid_cell_m"]
    nx = math.ceil((xmax - xmin) / cell_size)
    ny = math.ceil((ymax - ymin) / cell_size)
    candidate_specs = []
    cell_candidates: dict[int, list[int]] = defaultdict(list)
    for index, evaluation in enumerate(measurable):
        window = evaluation.diagnostics["selected_window"]
        coefficients = np.asarray(window["centreline_coefficients"])
        height = evaluation.measurement_height_m
        center = np.asarray(
            [coefficients[0, 0] * height + coefficients[0, 1], coefficients[1, 0] * height + coefficients[1, 1]]
        )
        radius = float(
            np.clip(
                max(
                    full_cfg["extraction_radius_minimum_m"],
                    window["median_radius_m"] * full_cfg["extraction_radius_radius_multiplier"]
                    + full_cfg["extraction_radius_padding_m"],
                ),
                full_cfg["extraction_radius_minimum_m"],
                full_cfg["extraction_radius_maximum_m"],
            )
        )
        spec = {
            "candidate_id": evaluation.candidate_id,
            "center": center,
            "radius": radius,
            "z_center": evaluation.ground_z_m + height,
        }
        candidate_specs.append(spec)
        ix_min = max(0, int((center[0] - radius - xmin) / cell_size))
        ix_max = min(nx - 1, int((center[0] + radius - xmin) / cell_size))
        iy_min = max(0, int((center[1] - radius - ymin) / cell_size))
        iy_max = min(ny - 1, int((center[1] + radius - ymin) / cell_size))
        for iy in range(iy_min, iy_max + 1):
            for ix in range(ix_min, ix_max + 1):
                cell_candidates[iy * nx + ix].append(index)

    collected: list[list[np.ndarray]] = [[] for _ in measurable]
    chunk_size = full_cfg["chunk_size_points"]
    for start in range(0, point_count, chunk_size):
        raw = source_map[start : min(start + chunk_size, point_count)]["xyz"]
        xyz = raw.astype(np.float64) * scale + offset - viewer_center
        inside = (
            (xyz[:, 0] >= xmin)
            & (xyz[:, 0] < xmax)
            & (xyz[:, 1] >= ymin)
            & (xyz[:, 1] < ymax)
        )
        if not inside.any():
            continue
        xyz = xyz[inside]
        ix = np.clip(((xyz[:, 0] - xmin) / cell_size).astype(np.int32), 0, nx - 1)
        iy = np.clip(((xyz[:, 1] - ymin) / cell_size).astype(np.int32), 0, ny - 1)
        keys = iy * nx + ix
        order = np.argsort(keys)
        sorted_keys = keys[order]
        boundaries = np.flatnonzero(np.r_[True, sorted_keys[1:] != sorted_keys[:-1], True])
        for left, right in zip(boundaries[:-1], boundaries[1:]):
            key = int(sorted_keys[left])
            owners = cell_candidates.get(key)
            if not owners:
                continue
            indexes = order[left:right]
            cell_points = xyz[indexes]
            for owner in owners:
                spec = candidate_specs[owner]
                radial = np.linalg.norm(cell_points[:, :2] - spec["center"], axis=1) <= spec["radius"]
                vertical = np.abs(cell_points[:, 2] - spec["z_center"]) <= full_cfg["vertical_half_range_m"]
                selected = cell_points[radial & vertical]
                if len(selected):
                    collected[owner].append(selected.astype(np.float32))
        if start and start % 10_000_000 == 0:
            print(f"V2 full LAS read {start:,}/{point_count:,}", flush=True)
    return {
        evaluation.candidate_id: (
            np.concatenate(parts).astype(np.float64) if parts else np.empty((0, 3), dtype=np.float64)
        )
        for evaluation, parts in zip(measurable, collected)
    }


def ellipse_perimeter(semi_major: float, semi_minor: float) -> float:
    h = ((semi_major - semi_minor) ** 2) / ((semi_major + semi_minor) ** 2)
    return float(
        math.pi
        * (semi_major + semi_minor)
        * (1 + 3 * h / (10 + math.sqrt(max(4 - 3 * h, 1e-12))))
    )


def _best_fit_near(fits: list[dict], predicted: np.ndarray, radius_hint: float) -> dict | None:
    valid = [fit for fit in fits if fit["valid"]]
    if not valid:
        return None
    return min(
        valid,
        key=lambda fit: (
            np.linalg.norm(np.asarray(fit["center"]) - predicted)
            + 0.20 * abs(fit["radius_m"] - radius_hint)
            + 2.0 * fit["circle_residual_m"]
            - 0.0002 * fit["inlier_count"]
        ),
    )


def compact_full_resolution_slice(entry: dict) -> dict:
    """Keep fit evidence in JSON while point arrays stay in the NPZ artifact."""
    slice_result = entry["slice"]
    compact_slice = {
        key: value
        for key, value in slice_result.items()
        if key not in {"fits", "rejected_components"}
    }
    compact_slice["fits"] = [json_ready(fit) for fit in slice_result.get("fits", [])]
    compact_slice["rejected_components"] = json_ready(slice_result.get("rejected_components", []))
    return {
        "height_m": entry["height_m"],
        "section_point_count": int(
            len(entry.get("plane_points", entry.get("section", ())))
        ),
        "line_point": json_ready(entry.get("line_point")),
        "slice": json_ready(compact_slice),
        "selected_fit": json_ready(entry.get("fit")),
    }


def refine_candidate_full_resolution(
    evaluation: CandidateEvaluation,
    local: np.ndarray,
    config: dict,
    point_output_dir: Path,
) -> CandidateEvaluation:
    if evaluation.measurement_height_m is None:
        return evaluation
    if len(local) == 0:
        evaluation.measurement_status = "NEEDS_REVIEW"
        evaluation.reason_codes.append("FULL_RESOLUTION_NEIGHBORHOOD_EMPTY")
        return evaluation
    full_cfg = config["full_resolution"]
    window = evaluation.diagnostics["selected_window"]
    sampled_coefficients = np.asarray(window["centreline_coefficients"])
    radius_hint = window["median_radius_m"]
    selected_height = evaluation.measurement_height_m
    neighbour_heights = heights_inclusive(
        selected_height - full_cfg["neighbouring_half_width_m"],
        selected_height + full_cfg["neighbouring_half_width_m"],
        full_cfg["neighbouring_step_m"],
    )
    rng = np.random.default_rng(config["random_seed"] + int(evaluation.candidate_id.split("-")[1]) * 17)
    horizontal = []
    for height in neighbour_heights:
        section_mask = np.abs(local[:, 2] - (evaluation.ground_z_m + height)) <= full_cfg["slab_thickness_m"] / 2
        section = local[section_mask]
        predicted = np.asarray(
            [
                sampled_coefficients[0, 0] * height + sampled_coefficients[0, 1],
                sampled_coefficients[1, 0] * height + sampled_coefficients[1, 1],
            ]
        )
        slice_result = fit_slice_profile(section[:, :2], predicted, config, rng, full_resolution=True)
        best = _best_fit_near(slice_result["fits"], predicted, radius_hint)
        horizontal.append({"height_m": height, "section": section, "slice": slice_result, "fit": best})
    valid_horizontal = [entry for entry in horizontal if entry["fit"] is not None]
    if len(valid_horizontal) < full_cfg["minimum_neighbouring_valid_slices"]:
        evaluation.measurement_status = "NEEDS_REVIEW"
        evaluation.reason_codes.append("FULL_RESOLUTION_CENTRELINE_INSUFFICIENT")
        evaluation.diagnostics["full_resolution_horizontal_slices"] = [
            compact_full_resolution_slice(entry) for entry in horizontal
        ]
        return evaluation

    heights = np.asarray([entry["height_m"] for entry in valid_horizontal])
    centers = np.asarray([entry["fit"]["center"] for entry in valid_horizontal])
    coefficients, center_residuals = robust_centreline(heights, centers, config)
    axis = np.asarray([coefficients[0, 0], coefficients[1, 0], 1.0])
    axis /= np.linalg.norm(axis)
    reference = np.asarray([0.0, 1.0, 0.0]) if abs(axis[1]) < 0.9 else np.asarray([1.0, 0.0, 0.0])
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
        plane_mask = (np.abs(axial) <= full_cfg["slab_thickness_m"] / 2) & (
            radial <= full_cfg["extraction_radius_maximum_m"]
        )
        plane_points = local[plane_mask]
        plane_section = plane_xy[plane_mask]
        slice_result = fit_slice_profile(
            plane_section,
            np.zeros(2),
            config,
            rng,
            full_resolution=True,
        )
        best = _best_fit_near(slice_result["fits"], np.zeros(2), radius_hint)
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
            entry
            for entry in perpendicular
            if abs(entry["height_m"] - selected_height) <= 1e-6 and entry["fit"] is not None
        ),
        None,
    )
    valid_perpendicular = [entry for entry in perpendicular if entry["fit"] is not None]
    if selected is None or len(valid_perpendicular) < full_cfg["minimum_neighbouring_valid_slices"]:
        evaluation.measurement_status = "NEEDS_REVIEW"
        evaluation.reason_codes.append("FULL_RESOLUTION_SELECTED_SLICE_UNSTABLE")
        evaluation.diagnostics["full_resolution_perpendicular_slices"] = [
            compact_full_resolution_slice(entry) for entry in perpendicular
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
        1.4826 * mad(neighbour_diameters),
        2 * fit["circle_residual_m"],
    )
    evaluation.selected_model = selected_model
    evaluation.equivalent_diameter_cm = rounded(equivalent_diameter_m * 100, 2)
    evaluation.diameter_uncertainty_cm = rounded(uncertainty_m * 100, 2)
    evaluation.circular_equivalent_girth_cm = rounded(math.pi * equivalent_diameter_m * 100, 2)
    if ellipse.get("valid"):
        evaluation.ellipse_major_axis_cm = rounded(2 * ellipse["semi_major_axis_m"] * 100, 2)
        evaluation.ellipse_minor_axis_cm = rounded(2 * ellipse["semi_minor_axis_m"] * 100, 2)
        perimeter_m = ellipse_perimeter(ellipse["semi_major_axis_m"], ellipse["semi_minor_axis_m"])
        evaluation.ellipse_perimeter_cm = rounded(perimeter_m * 100, 2)
    evaluation.observed_contour_girth_cm = (
        rounded(
            (
                evaluation.ellipse_perimeter_cm
                if use_ellipse and evaluation.ellipse_perimeter_cm is not None
                else 2 * math.pi * fit["radius_m"] * 100
            ),
            2,
        )
        if fit["angular_coverage_deg"] >= full_cfg["observed_contour_minimum_coverage_deg"]
        else None
    )
    evaluation.angular_coverage_deg = rounded(fit["angular_coverage_deg"], 2)
    evaluation.centreline_residual_p90_m = rounded(float(np.percentile(center_residuals, 90)))
    evaluation.supporting_slice_count = len(valid_perpendicular)
    evaluation.measurement_confidence = rounded(
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
    evaluation.reason_codes.append("FULL_RESOLUTION_MEASUREMENT_ACCEPTED")

    radial_error = np.abs(
        np.linalg.norm(selected["plane_xy"] - np.asarray(fit["center"]), axis=1) - fit["radius_m"]
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
        "centreline_axis": json_ready(axis),
        "centreline_coefficients": json_ready(coefficients),
        "horizontal_slice_results": [
            compact_full_resolution_slice(entry) for entry in horizontal
        ],
        "perpendicular_slice_results": [
            compact_full_resolution_slice(entry) for entry in perpendicular
        ],
        "selected_height_m": selected_height,
        "accepted_point_count": int(accepted_mask.sum()),
        "rejected_point_count": int((~accepted_mask).sum()),
        "circle_model": json_ready(fit),
        "ellipse_model": json_ready(ellipse),
    }
    evaluation.reason_codes = list(dict.fromkeys(evaluation.reason_codes))
    return evaluation


def build_v1_v2_crosswalk(v1_payload: dict, evaluations: list[CandidateEvaluation], maximum_distance_m: float = 1.0) -> dict:
    v1_trees = v1_payload["trees"]
    candidates = evaluations
    if not v1_trees or not candidates:
        return {"algorithm_version": "stem-inventory-v2-phase1", "matches": []}
    v1_xy = np.asarray([tree["center"] for tree in v1_trees])
    v2_xy = np.asarray([[item.position["x"], item.position["y"]] for item in candidates])
    distances = np.linalg.norm(v1_xy[:, None, :] - v2_xy[None, :, :], axis=2)
    rows, columns = linear_sum_assignment(distances)
    assigned = {int(row): int(column) for row, column in zip(rows, columns) if distances[row, column] <= maximum_distance_m}
    matches = []
    for index, tree in enumerate(v1_trees):
        candidate_index = assigned.get(index)
        if candidate_index is None:
            matches.append(
                {
                    "v1_tree_id": tree["id"],
                    "v1_center": tree["center"],
                    "v2_candidate_id": None,
                    "distance_m": None,
                    "status": "UNMATCHED_V1",
                    "reason": "NO_V2_CANDIDATE_WITHIN_1.0_M",
                }
            )
            continue
        evaluation = candidates[candidate_index]
        status = "MATCHED"
        reason = "V2_CANDIDATE_RETAINED"
        if evaluation.candidate_status.startswith("REJECTED"):
            status = "V2_REJECTED"
            reason = ",".join(evaluation.reason_codes)
        elif evaluation.measurement_status in {"NEEDS_REVIEW", "INSUFFICIENT_COVERAGE"}:
            status = "V2_UNMEASURED"
            reason = ",".join(evaluation.reason_codes)
        matches.append(
            {
                "v1_tree_id": tree["id"],
                "v1_center": tree["center"],
                "v1_dbh_cm": tree["dbhCm"],
                "v1_circumference_m": tree["circumferenceM"],
                "v2_candidate_id": evaluation.candidate_id,
                "distance_m": rounded(distances[index, candidate_index], 4),
                "v2_candidate_status": evaluation.candidate_status,
                "v2_measurement_status": evaluation.measurement_status,
                "v2_measurement_height_m": evaluation.measurement_height_m,
                "v2_equivalent_diameter_cm": evaluation.equivalent_diameter_cm,
                "status": status,
                "reason": reason,
            }
        )
    return {"algorithm_version": "stem-inventory-v2-phase1", "matches": matches}
