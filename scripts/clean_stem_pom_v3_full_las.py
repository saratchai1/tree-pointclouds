#!/usr/bin/env python3
"""Full-LAS clean-stem DBH/POM workflow for the Samut Songkhram site.

This module is additive: it reads the frozen V2/V3 products as identity and
QA context, reads the original LAS outside Git, and writes a separate V3.1
result lane.  Every fitted measurement plane is perpendicular to a local stem
axis refitted from full-resolution points.  Standard DBH at 1.30 m is preferred;
when it is unreliable, the lowest near-best clean section from 1.40--4.00 m is
reported as an alternative POM.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import struct
from typing import Any, Iterable

import numpy as np

import stem_inventory_v2 as phase1
import stem_inventory_v2_phase5a as phase5a


AUTOMATIC_STATUSES = {"STANDARD_DBH", "ALTERNATIVE_POM"}
WORKFLOW = "SEPARATE_CLEAN_STEM_POM_V3_1_FULL_LAS"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def compact_json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}"
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_directory(paths: Iterable[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_path(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def clipped(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def rounded(value: Any, digits: int = 6) -> Any:
    return round(float(value), digits) if finite(value) else None


def stable_seed(tree_id: str, suffix: str, base: int) -> int:
    token = hashlib.sha256(f"{tree_id}:{suffix}".encode("utf-8")).hexdigest()[:8]
    return int(token, 16) + int(base)


def mad(values: Iterable[float]) -> float:
    array = np.asarray(list(values), dtype=float)
    if not len(array):
        return math.inf
    center = np.median(array)
    return float(np.median(np.abs(array - center)))


def even_indexes(length: int, maximum: int) -> np.ndarray:
    if length <= maximum:
        return np.arange(length, dtype=np.int64)
    return np.linspace(0, length - 1, maximum, dtype=np.int64)


def heights(start: float, stop: float, step: float) -> list[float]:
    count = int(round((stop - start) / step))
    return [round(start + index * step, 6) for index in range(count + 1)]


def read_las_header(path: Path) -> dict:
    with path.open("rb") as handle:
        header = handle.read(227)
    if len(header) < 227 or header[:4] != b"LASF":
        raise ValueError(f"Not a supported LAS file: {path}")
    version = f"{header[24]}.{header[25]}"
    point_offset = struct.unpack_from("<I", header, 96)[0]
    point_format = header[104] & 0x3F
    point_record_length = struct.unpack_from("<H", header, 105)[0]
    point_count = struct.unpack_from("<I", header, 107)[0]
    scale = struct.unpack_from("<ddd", header, 131)
    offset = struct.unpack_from("<ddd", header, 155)
    max_x, min_x, max_y, min_y, max_z, min_z = struct.unpack_from("<dddddd", header, 179)
    return {
        "las_version": version,
        "point_data_offset": int(point_offset),
        "point_format": int(point_format),
        "point_record_length": int(point_record_length),
        "point_count": int(point_count),
        "scale": [float(value) for value in scale],
        "offset": [float(value) for value in offset],
        "bounds": {
            "xmin": float(min_x), "xmax": float(max_x),
            "ymin": float(min_y), "ymax": float(max_y),
            "zmin": float(min_z), "zmax": float(max_z),
        },
        "system_identifier": header[26:58].rstrip(b"\0 ").decode("ascii", errors="replace"),
        "generating_software": header[58:90].rstrip(b"\0 ").decode("ascii", errors="replace"),
    }


def validate_source_las(path: Path, config: dict) -> dict:
    expected = config["source_las"]
    header = read_las_header(path)
    size = path.stat().st_size
    if size != expected["expected_size_bytes"]:
        raise ValueError(f"LAS size mismatch: {size} != {expected['expected_size_bytes']}")
    if header["point_count"] != expected["expected_point_count"]:
        raise ValueError("LAS point-count mismatch")
    calculated_size = header["point_data_offset"] + header["point_count"] * header["point_record_length"]
    if calculated_size != size:
        raise ValueError(f"LAS byte layout mismatch: {calculated_size} != {size}")
    digest = sha256_path(path)
    if digest != expected["expected_sha256"]:
        raise ValueError(f"LAS SHA-256 mismatch: {digest}")
    return {**header, "file_name": path.name, "size_bytes": size, "sha256": digest}


def source_paths(root: Path) -> dict[str, Path]:
    return {
        "config": root / "config/clean_stem_pom_v3_full_las.json",
        "phase_config": root / "config/stem_inventory_v2.yaml",
        "inventory": root / "site/public/viewer-v2-review/data/phase4_tree_inventory.json",
        "current_measurements": root / "site/public/data/lidar-measurements/measurements.json",
        "v3_measurements": root / "site/public/viewer-v3-clean-stem/data/measurements.json",
    }


def load_context(root: Path, config_path: Path | None = None) -> dict:
    paths = source_paths(root)
    if config_path is not None:
        paths["config"] = config_path
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing V3.1 inputs: " + ", ".join(missing))
    marking_directory = root / "site/public/data/lidar-measurements/markings"
    marking_paths = sorted(marking_directory.glob("TREE_*.json"))
    if len(marking_paths) != 118:
        raise RuntimeError(f"Expected 118 frozen marking files, found {len(marking_paths)}")
    config = read_json(paths["config"])
    phase_config = phase1.load_config(paths["phase_config"])
    for key, value in config["fit_overrides"].items():
        phase_config["slice_fit"][key] = value
    phase_config["candidate_radius"] = deepcopy(config["candidate_radius"])
    inventory = read_json(paths["inventory"])
    current = read_json(paths["current_measurements"])
    v3 = read_json(paths["v3_measurements"])
    return {
        "config": config,
        "phase_config": phase_config,
        "inventory": inventory,
        "current": current,
        "v3": v3,
        "paths": paths,
        "marking_directory": marking_directory,
        "marking_paths": marking_paths,
    }


def seed_from_marking(marking: dict, minimum_axis_z: float) -> dict:
    plane = marking["measurement_plane"]
    direction = np.asarray(plane["axis_direction"], dtype=float)
    direction /= np.linalg.norm(direction)
    if abs(direction[2]) >= minimum_axis_z:
        slope = direction[:2] / direction[2]
        mode = marking.get("axis_evidence", {}).get("source") or "MARKING_AXIS"
    else:
        slope = np.zeros(2, dtype=float)
        mode = "VERTICAL_EXTRACTION_FALLBACK_FOR_EXTREME_SEED_AXIS"
    center = np.asarray(plane["center_xyz"], dtype=float)
    height = float(plane["height_agl_m"])
    ground = center[2] - height
    return {
        "direction": direction,
        "slope": slope,
        "center_xy_at_h0": center[:2],
        "height_h0": height,
        "reference_z": center[2],
        "ground_z": ground,
        "mode": mode,
    }


def _las_point_dtype(record_length: int) -> np.dtype:
    if record_length < 26:
        raise ValueError("V3.1 requires LAS point format with RGB fields")
    return np.dtype({
        "names": ["xyz", "rgb"],
        "formats": [("<i4", (3,)), ("<u2", (3,))],
        "offsets": [0, 20],
        "itemsize": record_length,
    })


def extract_tube_cache(
    root: Path,
    source_las: Path,
    cache_directory: Path,
    context: dict,
    source_metadata: dict,
) -> dict:
    """Scan the original LAS once and write an untracked per-tree tube cache."""
    config = context["config"]
    extraction = config["tube_extraction"]
    current_by_tree = {row["tree_id"]: row for row in context["current"]["records"]}
    inventory_trees = sorted(context["inventory"]["trees"], key=lambda row: row["tree_id"])
    seeds = []
    for tree in inventory_trees:
        tree_id = tree["tree_id"]
        marking = read_json(context["marking_directory"] / f"{tree_id}.json")
        seed = seed_from_marking(marking, extraction["minimum_axis_z_for_seed_slope"])
        seed["tree_id"] = tree_id
        seed["operationally_excluded"] = bool(current_by_tree[tree_id].get("operationally_excluded"))
        seeds.append(seed)

    radius = float(extraction["horizontal_radius_m"])
    h_min = float(extraction["minimum_height_agl_m"])
    h_max = float(extraction["maximum_height_agl_m"])
    cell = float(extraction["grid_cell_m"])
    broad_bounds = []
    for seed in seeds:
        centers = np.asarray([
            seed["center_xy_at_h0"] + seed["slope"] * (seed["ground_z"] + h - seed["reference_z"])
            for h in (h_min, h_max)
        ])
        broad_bounds.append((
            float(centers[:, 0].min() - radius), float(centers[:, 0].max() + radius),
            float(centers[:, 1].min() - radius), float(centers[:, 1].max() + radius),
        ))
    x_min = min(row[0] for row in broad_bounds)
    x_max = max(row[1] for row in broad_bounds)
    y_min = min(row[2] for row in broad_bounds)
    y_max = max(row[3] for row in broad_bounds)
    nx = max(1, int(math.ceil((x_max - x_min) / cell)))
    ny = max(1, int(math.ceil((y_max - y_min) / cell)))
    cell_owners: dict[int, list[int]] = defaultdict(list)
    for owner, bounds in enumerate(broad_bounds):
        ix0 = max(0, int(math.floor((bounds[0] - x_min) / cell)))
        ix1 = min(nx - 1, int(math.floor((bounds[1] - x_min) / cell)))
        iy0 = max(0, int(math.floor((bounds[2] - y_min) / cell)))
        iy1 = min(ny - 1, int(math.floor((bounds[3] - y_min) / cell)))
        for iy in range(iy0, iy1 + 1):
            for ix in range(ix0, ix1 + 1):
                cell_owners[iy * nx + ix].append(owner)

    point_map = np.memmap(
        source_las,
        mode="r",
        offset=source_metadata["point_data_offset"],
        dtype=_las_point_dtype(source_metadata["point_record_length"]),
        shape=(source_metadata["point_count"],),
    )
    scale = np.asarray(source_metadata["scale"], dtype=float)
    offset = np.asarray(source_metadata["offset"], dtype=float)
    xyz_parts: list[list[np.ndarray]] = [[] for _ in seeds]
    rgb_parts: list[list[np.ndarray]] = [[] for _ in seeds]
    scanned_union_points = 0
    chunk_size = int(extraction["chunk_size_points"])
    for start in range(0, source_metadata["point_count"], chunk_size):
        stop = min(start + chunk_size, source_metadata["point_count"])
        raw = point_map[start:stop]
        xyz = raw["xyz"].astype(np.float64) * scale + offset
        inside = (
            (xyz[:, 0] >= x_min) & (xyz[:, 0] < x_max)
            & (xyz[:, 1] >= y_min) & (xyz[:, 1] < y_max)
        )
        if inside.any():
            xyz = xyz[inside]
            rgb = raw["rgb"][inside]
            scanned_union_points += int(len(xyz))
            ix = np.clip(((xyz[:, 0] - x_min) / cell).astype(np.int32), 0, nx - 1)
            iy = np.clip(((xyz[:, 1] - y_min) / cell).astype(np.int32), 0, ny - 1)
            keys = iy * nx + ix
            order = np.argsort(keys)
            sorted_keys = keys[order]
            boundaries = np.flatnonzero(np.r_[True, sorted_keys[1:] != sorted_keys[:-1], True])
            for left, right in zip(boundaries[:-1], boundaries[1:]):
                owners = cell_owners.get(int(sorted_keys[left]))
                if not owners:
                    continue
                indexes = order[left:right]
                cell_xyz = xyz[indexes]
                cell_rgb = rgb[indexes]
                for owner in owners:
                    seed = seeds[owner]
                    hag = cell_xyz[:, 2] - seed["ground_z"]
                    predicted = (
                        seed["center_xy_at_h0"]
                        + (cell_xyz[:, 2] - seed["reference_z"])[:, None] * seed["slope"]
                    )
                    radial = np.linalg.norm(cell_xyz[:, :2] - predicted, axis=1)
                    selected = (hag >= h_min) & (hag <= h_max) & (radial <= radius)
                    if selected.any():
                        xyz_parts[owner].append(cell_xyz[selected].astype(np.float32))
                        rgb_parts[owner].append(cell_rgb[selected].astype(np.uint16))
        if start and start % 10_000_000 == 0:
            print(f"Full LAS tube scan {start:,}/{source_metadata['point_count']:,}", flush=True)

    cache_directory.mkdir(parents=True, exist_ok=True)
    counts = {}
    for seed, xyz_chunks, rgb_chunks in zip(seeds, xyz_parts, rgb_parts):
        xyz = np.concatenate(xyz_chunks) if xyz_chunks else np.empty((0, 3), dtype=np.float32)
        rgb = np.concatenate(rgb_chunks) if rgb_chunks else np.empty((0, 3), dtype=np.uint16)
        path = cache_directory / f"{seed['tree_id']}.npz"
        np.savez(
            path,
            xyz=xyz,
            rgb=rgb,
            axis_seed=seed["direction"],
            axis_slope=seed["slope"],
            axis_center_xy_at_h0=seed["center_xy_at_h0"],
            axis_h0=seed["height_h0"],
            ground_z=seed["ground_z"],
            axis_mode=seed["mode"],
        )
        counts[seed["tree_id"]] = int(len(xyz))
    manifest = {
        "workflow": WORKFLOW,
        "source_las_sha256": source_metadata["sha256"],
        "source_las_point_count": source_metadata["point_count"],
        "source_las_scan_count": 1,
        "points_in_union_xy_bounds": scanned_union_points,
        "tree_count": len(counts),
        "tube_point_counts": counts,
        "configuration": extraction,
    }
    atomic_write(cache_directory / "cache_manifest.json", canonical_json_bytes(manifest))
    return manifest


def validate_tube_cache(cache_directory: Path, tree_ids: list[str]) -> None:
    missing = [tree_id for tree_id in tree_ids if not (cache_directory / f"{tree_id}.npz").is_file()]
    if missing:
        raise FileNotFoundError(f"Missing tube caches for {len(missing)} trees: {', '.join(missing[:5])}")


def refine_ground(xyz: np.ndarray, seed: dict, config: dict) -> dict:
    settings = config["ground_refit"]
    base_xy = seed["center_xy_at_h0"] + seed["slope"] * (seed["ground_z"] - seed["reference_z"])
    radial = np.linalg.norm(xyz[:, :2] - base_xy, axis=1)
    candidate_mask = (
        (radial >= settings["annulus_minimum_radius_m"])
        & (radial <= settings["annulus_maximum_radius_m"])
        & (xyz[:, 2] >= seed["ground_z"] - settings["vertical_search_below_seed_m"])
        & (xyz[:, 2] <= seed["ground_z"] + settings["vertical_search_above_seed_m"])
    )
    values = xyz[candidate_mask, 2]
    if len(values) < settings["minimum_supporting_points"]:
        return {
            "ground_z_m": float(seed["ground_z"]),
            "seed_ground_z_m": float(seed["ground_z"]),
            "raw_candidate_ground_z_m": None,
            "shift_m": 0.0,
            "supporting_point_count": int(len(values)),
            "status": "SEED_GROUND_FALLBACK",
        }
    raw = float(np.quantile(values, settings["quantile"]))
    maximum_shift = float(settings["maximum_shift_from_seed_m"])
    selected = float(np.clip(raw, seed["ground_z"] - maximum_shift, seed["ground_z"] + maximum_shift))
    return {
        "ground_z_m": selected,
        "seed_ground_z_m": float(seed["ground_z"]),
        "raw_candidate_ground_z_m": raw,
        "shift_m": selected - float(seed["ground_z"]),
        "supporting_point_count": int(len(values)),
        "status": "FULL_LAS_LOCAL_GROUND_REFIT_CLIPPED" if not math.isclose(raw, selected) else "FULL_LAS_LOCAL_GROUND_REFIT",
    }


def seed_center_at_height(seed: dict, ground_z: float, height_agl: float) -> np.ndarray:
    absolute_z = float(ground_z) + float(height_agl)
    return seed["center_xy_at_h0"] + seed["slope"] * (absolute_z - seed["reference_z"])


def fit_cost(fit: dict, expected: np.ndarray, radius_hint: float | None) -> float:
    center_offset = float(np.linalg.norm(np.asarray(fit["center"], dtype=float) - expected))
    radius_cost = 0.0
    if finite(radius_hint) and float(radius_hint) > 0:
        radius_cost = abs(math.log(max(float(fit["radius_m"]), 1e-6) / float(radius_hint)))
    relative_residual = float(fit["circle_residual_m"]) / max(float(fit["radius_m"]), 0.02)
    coverage_reward = float(fit["angular_coverage_deg"]) / 360.0
    inlier_reward = min(int(fit.get("inlier_count") or 0), 600) / 600.0
    return center_offset / 0.18 + 0.22 * radius_cost + 0.55 * relative_residual - 0.28 * coverage_reward - 0.08 * inlier_reward


def choose_fit(
    fitted: dict,
    expected: np.ndarray,
    radius_hint: float | None,
    maximum_center_offset: float,
) -> dict | None:
    valid = [
        fit for fit in fitted.get("fits", [])
        if fit.get("valid")
        and np.linalg.norm(np.asarray(fit["center"], dtype=float) - expected) <= maximum_center_offset
    ]
    return min(valid, key=lambda fit: fit_cost(fit, expected, radius_hint)) if valid else None


def radius_hint_from_v3(v3: dict) -> float | None:
    for container in (v3.get("selected_window"), v3.get("best_review_window")):
        if container and finite(container.get("radius_m")):
            return float(container["radius_m"])
    if finite(v3.get("radius_m")):
        return float(v3["radius_m"])
    return None


def fit_axis_observations(
    tree_id: str,
    xyz: np.ndarray,
    seed: dict,
    ground_z: float,
    radius_hint: float | None,
    config: dict,
    phase_config: dict,
) -> tuple[dict, list[dict]]:
    settings = config["axis_refit"]
    candidates_by_height = []
    for ordinal, height in enumerate(heights(settings["minimum_height_m"], settings["maximum_height_m"], settings["step_m"])):
        predicted = seed_center_at_height(seed, ground_z, height)
        mask = (
            (np.abs(xyz[:, 2] - (ground_z + height)) <= settings["horizontal_slab_thickness_m"] / 2.0)
            & (np.linalg.norm(xyz[:, :2] - predicted, axis=1) <= 0.68)
        )
        section = xyz[mask]
        if len(section) < phase_config["slice_fit"]["minimum_component_points"]:
            candidates_by_height.append({"height_agl_m": height, "point_count": int(len(section)), "fits": []})
            continue
        rng = np.random.default_rng(stable_seed(tree_id, f"axis-{ordinal}", config["random_seed"]))
        fitted = phase1.fit_slice_profile(section[:, :2], predicted, phase_config, rng, full_resolution=True)
        usable = [
            fit for fit in fitted.get("fits", [])
            if fit.get("valid")
            and np.linalg.norm(np.asarray(fit["center"]) - predicted) <= settings["maximum_fit_center_offset_from_seed_m"]
        ]
        candidates_by_height.append({
            "height_agl_m": height,
            "point_count": int(len(section)),
            "component_count": int(fitted.get("connected_component_count") or 0),
            "predicted_center": predicted,
            "fits": usable,
        })

    first_rows = []
    for row in candidates_by_height:
        fit = choose_fit(
            {"fits": row["fits"]},
            row.get("predicted_center", np.zeros(2)),
            radius_hint,
            settings["maximum_fit_center_offset_from_seed_m"],
        )
        if fit is not None:
            first_rows.append((row, fit))

    seed_intercept = seed["center_xy_at_h0"] + seed["slope"] * (ground_z - seed["reference_z"])
    seed_coefficients = np.asarray([
        [seed["slope"][0], seed_intercept[0]],
        [seed["slope"][1], seed_intercept[1]],
    ], dtype=float)

    coefficients = seed_coefficients
    if len(first_rows) >= settings["minimum_supporting_slices"]:
        obs_heights = np.asarray([row[0]["height_agl_m"] for row in first_rows], dtype=float)
        centers = np.asarray([row[1]["center"] for row in first_rows], dtype=float)
        coefficients, residuals = phase1.robust_centreline(obs_heights, centers, phase_config)
        threshold = max(0.10, float(np.median(residuals) + 3.0 * max(mad(residuals), 0.01)))
        keep = residuals <= threshold
        if int(keep.sum()) >= settings["minimum_supporting_slices"]:
            coefficients, _ = phase1.robust_centreline(obs_heights[keep], centers[keep], phase_config)

    selected_rows = []
    for row in candidates_by_height:
        height = row["height_agl_m"]
        line_center = coefficients[:, 0] * height + coefficients[:, 1]
        seed_center = row.get("predicted_center", line_center)
        expected = 0.8 * line_center + 0.2 * seed_center
        fit = choose_fit(
            {"fits": row["fits"]}, expected, radius_hint,
            settings["maximum_fit_center_offset_from_seed_m"],
        )
        if fit is not None:
            selected_rows.append({
                "height_agl_m": height,
                "point_count": row["point_count"],
                "component_count": row.get("component_count", 0),
                "center_xy": np.asarray(fit["center"], dtype=float),
                "radius_m": float(fit["radius_m"]),
                "coverage_deg": float(fit["angular_coverage_deg"]),
                "fit_rmse_m": float(fit["circle_residual_m"]),
            })

    if len(selected_rows) >= settings["minimum_supporting_slices"]:
        obs_heights = np.asarray([row["height_agl_m"] for row in selected_rows], dtype=float)
        centers = np.asarray([row["center_xy"] for row in selected_rows], dtype=float)
        coefficients, residuals = phase1.robust_centreline(obs_heights, centers, phase_config)
        threshold = max(0.10, float(np.median(residuals) + 3.0 * max(mad(residuals), 0.01)))
        keep = residuals <= threshold
        if int(keep.sum()) >= settings["minimum_supporting_slices"]:
            selected_rows = [row for row, usable in zip(selected_rows, keep) if usable]
            obs_heights = np.asarray([row["height_agl_m"] for row in selected_rows], dtype=float)
            centers = np.asarray([row["center_xy"] for row in selected_rows], dtype=float)
            coefficients, residuals = phase1.robust_centreline(obs_heights, centers, phase_config)
        uncertainty = float(np.percentile(residuals, 90)) if len(residuals) else math.inf
        source = "FULL_SOURCE_LAS_MULTI_HEIGHT_AXIS_REFIT"
    else:
        coefficients = seed_coefficients
        uncertainty = math.inf
        source = "FROZEN_FULL_LAS_MARKING_AXIS_FALLBACK"

    direction = np.asarray([coefficients[0, 0], coefficients[1, 0], 1.0], dtype=float)
    direction /= np.linalg.norm(direction)
    inclination = math.degrees(math.atan(math.hypot(coefficients[0, 0], coefficients[1, 0])))
    if uncertainty <= settings["confirmed_uncertainty_p90_m"]:
        status = "CONFIRMED"
    elif uncertainty <= settings["maximum_automatic_uncertainty_p90_m"]:
        status = "PROBABLE"
    else:
        status = "NEEDS_REVIEW"
    compact_rows = [{
        "height_agl_m": rounded(row["height_agl_m"], 3),
        "point_count": int(row["point_count"]),
        "component_count": int(row["component_count"]),
        "center_xy": [rounded(value) for value in row["center_xy"]],
        "radius_m": rounded(row["radius_m"]),
        "coverage_deg": rounded(row["coverage_deg"], 1),
        "fit_rmse_m": rounded(row["fit_rmse_m"]),
    } for row in selected_rows]
    return ({
        "status": status,
        "source": source,
        "coefficients": coefficients,
        "direction": direction,
        "inclination_deg": inclination,
        "uncertainty_p90_m": uncertainty,
        "supporting_slice_count": len(selected_rows),
    }, compact_rows)


def local_axis_at_height(axis: dict, observations: list[dict], height: float, config: dict, phase_config: dict) -> dict:
    settings = config["axis_refit"]
    nearby = [row for row in observations if abs(float(row["height_agl_m"]) - height) <= settings["local_half_window_m"] + 1e-9]
    coefficients = np.asarray(axis["coefficients"], dtype=float)
    uncertainty = float(axis["uncertainty_p90_m"])
    source = axis["source"]
    if len(nearby) >= 3:
        obs_heights = np.asarray([row["height_agl_m"] for row in nearby], dtype=float)
        centers = np.asarray([row["center_xy"] for row in nearby], dtype=float)
        local_coefficients, residuals = phase1.robust_centreline(obs_heights, centers, phase_config)
        local_slope = float(np.linalg.norm(local_coefficients[:, 0]))
        local_uncertainty = float(np.percentile(residuals, 90))
        if local_slope <= settings["maximum_local_slope"] and local_uncertainty <= settings["maximum_automatic_uncertainty_p90_m"]:
            coefficients = local_coefficients
            uncertainty = local_uncertainty
            source = "FULL_SOURCE_LAS_LOCAL_AXIS_REFIT"
    direction = np.asarray([coefficients[0, 0], coefficients[1, 0], 1.0], dtype=float)
    direction /= np.linalg.norm(direction)
    return {
        "coefficients": coefficients,
        "direction": direction,
        "uncertainty_m": uncertainty,
        "source": source,
        "center_xy": coefficients[:, 0] * height + coefficients[:, 1],
        "inclination_deg": math.degrees(math.atan(float(np.linalg.norm(coefficients[:, 0])))),
    }


def perpendicular_section(
    xyz: np.ndarray,
    center_xyz: np.ndarray,
    direction: np.ndarray,
    thickness: float,
    radial_limit: float,
) -> dict:
    axis, basis_u, basis_v = phase5a.perpendicular_plane_basis(direction)
    relative = xyz - center_xyz
    axial = relative @ axis
    plane_xy = np.column_stack((relative @ basis_u, relative @ basis_v))
    radial = np.linalg.norm(plane_xy, axis=1)
    mask = (np.abs(axial) <= thickness / 2.0) & (radial <= radial_limit)
    return {
        "axis": axis,
        "basis_u": basis_u,
        "basis_v": basis_v,
        "center_xyz": center_xyz,
        "indexes": np.flatnonzero(mask),
        "plane_xy": plane_xy[mask],
        "point_count": int(mask.sum()),
    }


def fit_candidate(
    tree_id: str,
    ordinal: int,
    height: float,
    xyz: np.ndarray,
    ground_z: float,
    axis: dict,
    observations: list[dict],
    radius_hint: float | None,
    config: dict,
    phase_config: dict,
) -> dict:
    local_axis = local_axis_at_height(axis, observations, height, config, phase_config)
    center_xyz = np.asarray([*local_axis["center_xy"], ground_z + height], dtype=float)
    search = config["height_search"]
    section = perpendicular_section(
        xyz,
        center_xyz,
        local_axis["direction"],
        search["slab_thickness_m"],
        search["radial_limit_m"],
    )
    result = {
        "height_agl_m": height,
        "fit_valid": False,
        "point_count": section["point_count"],
        "axis_source": local_axis["source"],
        "axis_direction": local_axis["direction"],
        "axis_center_xyz": center_xyz,
        "axis_uncertainty_m": local_axis["uncertainty_m"],
        "inclination_deg": local_axis["inclination_deg"],
        "basis_u": section["basis_u"],
        "basis_v": section["basis_v"],
        "_section": section,
    }
    if section["point_count"] < phase_config["slice_fit"]["minimum_component_points"]:
        result["fit_rejection_reasons"] = ["INSUFFICIENT_FULL_LAS_POINT_SUPPORT"]
        return result
    rng = np.random.default_rng(stable_seed(tree_id, f"pom-{ordinal}", config["random_seed"]))
    fitted = phase1.fit_slice_profile(section["plane_xy"], np.zeros(2), phase_config, rng, full_resolution=True)
    fit = choose_fit(fitted, np.zeros(2), radius_hint, config["reliability"]["alternative"]["maximum_axis_center_offset_m"])
    if fit is None:
        result.update({
            "component_count": int(fitted.get("connected_component_count") or 0),
            "fit_rejection_reasons": list(fitted.get("rejection_reasons") or ["NO_PLAUSIBLE_PERPENDICULAR_FIT"]),
        })
        return result
    ellipse = fit.get("ellipse") or {}
    major = float(ellipse.get("semi_major_axis_m") or 0.0)
    minor = float(ellipse.get("semi_minor_axis_m") or 0.0)
    circularity = minor / major if ellipse.get("valid") and major > 0 else 0.0
    fit_center = np.asarray(fit["center"], dtype=float)
    fitted_center_xyz = center_xyz + section["basis_u"] * fit_center[0] + section["basis_v"] * fit_center[1]
    result.update({
        "fit_valid": True,
        "radius_m": float(fit["radius_m"]),
        "diameter_cm": float(fit["radius_m"]) * 200.0,
        "circumference_cm": float(fit["radius_m"]) * 200.0 * math.pi,
        "fit_rmse_m": float(fit["circle_residual_m"]),
        "relative_fit_rmse": float(fit["circle_residual_m"]) / max(float(fit["radius_m"]), 1e-9),
        "inlier_count": int(fit["inlier_count"]),
        "angular_coverage_deg": float(fit["angular_coverage_deg"]),
        "largest_missing_angular_sector_deg": float(fit["largest_missing_angular_sector_deg"]),
        "axis_center_offset_m": float(np.linalg.norm(fit_center)),
        "fit_center_xy": fit_center,
        "fitted_center_xyz": fitted_center_xyz,
        "ellipse_axis_ratio": major / minor if minor > 0 else None,
        "circularity": circularity,
        "ellipse": ellipse,
        "component_count": int(fitted.get("connected_component_count") or 0),
        "component_point_count": int(fit.get("component_point_count") or 0),
        "fit_rejection_reasons": [],
        "_fit": fit,
    })
    return result


def add_stability_and_quality(candidates: list[dict], config: dict) -> None:
    scaling = config["quality_scaling"]
    weights = config["quality_weights"]
    for index, candidate in enumerate(candidates):
        if index == 0:
            window = candidates[:3]
        elif index == len(candidates) - 1:
            window = candidates[-3:]
        else:
            window = candidates[index - 1:index + 2]
        valid = [row for row in window if row.get("fit_valid")]
        candidate["neighbouring_valid_slice_count"] = len(valid)
        candidate["expected_neighbouring_slice_count"] = len(window)
        if not candidate.get("fit_valid"):
            candidate.update({
                "radius_stability_mad_m": None,
                "relative_radius_mad": None,
                "quality_score": 0.0,
                "quality_components": {},
                "quality_penalties": {},
            })
            continue
        radius_mad = mad(row["radius_m"] for row in valid)
        relative_radius_mad = radius_mad / max(candidate["radius_m"], 1e-9)
        radius_values = [row["radius_m"] for row in valid]
        relative_radius_range = (
            (max(radius_values) - min(radius_values)) / max(candidate["radius_m"], 1e-9)
            if radius_values else math.inf
        )
        components = {
            "angular_coverage": clipped(
                (candidate["angular_coverage_deg"] - scaling["coverage_floor_deg"])
                / (scaling["coverage_target_deg"] - scaling["coverage_floor_deg"])
            ),
            "fit_quality": clipped(1.0 - candidate["relative_fit_rmse"] / scaling["maximum_relative_fit_rmse"]),
            "circularity": clipped(candidate["circularity"]),
            "radius_stability": clipped(1.0 - relative_radius_mad / scaling["maximum_relative_radius_mad"]),
            "axis_alignment": clipped(1.0 - candidate["axis_center_offset_m"] / scaling["maximum_axis_offset_m"]),
            "vertical_continuity": clipped(len(valid) / max(len(window), 1)),
        }
        clutter = min(
            scaling["maximum_clutter_penalty"],
            max(0, candidate["component_count"] - 1) * scaling["clutter_penalty_per_extra_component"],
        )
        score = 100.0 * sum(weights[key] * components[key] for key in weights) - clutter
        candidate.update({
            "radius_stability_mad_m": radius_mad,
            "relative_radius_mad": relative_radius_mad,
            "relative_radius_range": relative_radius_range,
            "quality_score": max(0.0, score),
            "quality_components": components,
            "quality_penalties": {"clutter": clutter},
        })

    guardrail = config["reliability"]["root_crown_guardrail"]
    for index, candidate in enumerate(candidates):
        candidate["cleaner_smaller_upper_section_available"] = False
        candidate["cleaner_upper_section_heights_m"] = []
        if not candidate.get("fit_valid") or candidate["radius_m"] < guardrail["minimum_candidate_radius_m"]:
            continue
        upper = [
            row for row in candidates[index + 1:]
            if row["height_agl_m"] - candidate["height_agl_m"] <= guardrail["maximum_upper_search_distance_m"] + 1e-9
        ]
        for left, right in zip(upper, upper[1:]):
            if not left.get("fit_valid") or not right.get("fit_valid"):
                continue
            if right["height_agl_m"] - left["height_agl_m"] > config["height_search"]["step_m"] + 1e-9:
                continue
            if any(row["radius_m"] > candidate["radius_m"] * guardrail["maximum_upper_to_lower_radius_ratio"] for row in (left, right)):
                continue
            pair_difference = abs(left["radius_m"] - right["radius_m"]) / max(np.mean([left["radius_m"], right["radius_m"]]), 1e-9)
            if pair_difference > guardrail["maximum_pair_relative_radius_difference"]:
                continue
            if any(row["quality_score"] < guardrail["minimum_upper_quality_score"] for row in (left, right)):
                continue
            if any(row["angular_coverage_deg"] < guardrail["minimum_upper_angular_coverage_deg"] for row in (left, right)):
                continue
            if any(row["circularity"] < guardrail["minimum_upper_circularity"] for row in (left, right)):
                continue
            candidate["cleaner_smaller_upper_section_available"] = True
            candidate["cleaner_upper_section_heights_m"] = [left["height_agl_m"], right["height_agl_m"]]
            break


def reliability_failures(candidate: dict, lane: str, config: dict, axis_supporting_slices: int) -> list[str]:
    if not candidate.get("fit_valid"):
        return list(candidate.get("fit_rejection_reasons") or ["NO_PLAUSIBLE_PERPENDICULAR_FIT"])
    gate = config["reliability"][lane]
    failures = []
    checks = (
        (candidate["quality_score"] >= gate["minimum_quality_score"], "QUALITY_SCORE_BELOW_THRESHOLD"),
        (candidate["inlier_count"] >= gate["minimum_inlier_count"], "INSUFFICIENT_INLIERS"),
        (candidate["angular_coverage_deg"] >= gate["minimum_angular_coverage_deg"], "INSUFFICIENT_ARC_COVERAGE"),
        (candidate["circularity"] >= gate["minimum_circularity"], "ELLIPSE_TOO_NONCIRCULAR"),
        (candidate["relative_fit_rmse"] <= gate["maximum_relative_fit_rmse"], "FIT_RMSE_TOO_HIGH"),
        (candidate["relative_radius_mad"] <= gate["maximum_relative_radius_mad"], "RADIUS_UNSTABLE_ACROSS_HEIGHTS"),
        (candidate["relative_radius_range"] <= gate["maximum_relative_radius_range"], "RADIUS_RANGE_UNSTABLE_ACROSS_HEIGHTS"),
        (candidate["axis_center_offset_m"] <= gate["maximum_axis_center_offset_m"], "FIT_CENTER_INCONSISTENT_WITH_LOCAL_AXIS"),
        (candidate["neighbouring_valid_slice_count"] >= gate["minimum_neighbouring_valid_slices"], "INSUFFICIENT_VERTICAL_CONTINUITY"),
        (axis_supporting_slices >= gate["minimum_axis_supporting_slices"], "LOCAL_AXIS_NOT_SUPPORTED_ACROSS_HEIGHTS"),
        (candidate["axis_uncertainty_m"] <= config["axis_refit"]["maximum_automatic_uncertainty_p90_m"], "LOCAL_AXIS_UNCERTAINTY_TOO_HIGH"),
        (config["candidate_radius"]["minimum_m"] <= candidate["radius_m"] <= config["candidate_radius"]["maximum_m"], "RADIUS_OUTSIDE_PLAUSIBLE_RANGE"),
        (candidate["radius_m"] <= config["reliability"]["maximum_automatic_radius_m"], "SECTION_RADIUS_EXCEEDS_AUTOMATIC_COHORT_GUARDRAIL"),
        (not candidate.get("cleaner_smaller_upper_section_available"), "CLEANER_SMALLER_UPPER_SECTION_AVAILABLE"),
    )
    failures.extend(reason for passed, reason in checks if not passed)
    if candidate["radius_m"] >= config["candidate_radius"]["large_stem_threshold_m"]:
        large = config["reliability"]["large_stem"]
        if candidate["angular_coverage_deg"] < large["minimum_angular_coverage_deg"]:
            failures.append("LARGE_SECTION_REQUIRES_STRONGER_ARC_COVERAGE")
        if candidate["circularity"] < large["minimum_circularity"]:
            failures.append("LARGE_SECTION_REQUIRES_STRONGER_CIRCULARITY")
    return list(dict.fromkeys(failures))


def blocked_tree_reasons(tree: dict, current: dict, v3: dict, config: dict) -> list[str]:
    reasons = []
    if current.get("operationally_excluded"):
        reasons.append("OPERATIONALLY_EXCLUDED_TREE_ID")
    identity = current.get("identity_review_status")
    if identity in config["blocked_identity_review_statuses"]:
        reasons.append(f"IDENTITY_REVIEW_{identity}")
    detection = (tree.get("detection") or {}).get("status")
    if detection not in config["eligible_detection_statuses"]:
        reasons.append(f"TREE_DETECTION_{detection or 'UNAVAILABLE'}")
    labels = v3.get("tree_source_human_labels") or []
    if labels and "TRUE_MAIN_STEM" not in labels and all(label in config["blocked_human_labels"] for label in labels):
        reasons.append("TREE_SOURCE_HUMAN_LABEL_BLOCKED")
    return reasons


def choose_measurement(candidates: list[dict], blocked: list[str], config: dict, axis_supporting_slices: int) -> dict:
    search = config["height_search"]
    for candidate in candidates:
        candidate["standard_failures"] = reliability_failures(candidate, "standard", config, axis_supporting_slices)
        candidate["alternative_failures"] = reliability_failures(candidate, "alternative", config, axis_supporting_slices)
    standard = next(
        (row for row in candidates if math.isclose(row["height_agl_m"], search["standard_height_m"], abs_tol=1e-9)),
        None,
    )
    selected = None
    status = "MANUAL_REVIEW"
    if not blocked and standard is not None and not standard["standard_failures"]:
        selected = standard
        status = "STANDARD_DBH"
    elif not blocked:
        alternatives = [
            row for row in candidates
            if row["height_agl_m"] >= search["minimum_alternative_height_m"] - 1e-9
            and not row["alternative_failures"]
        ]
        if alternatives:
            maximum_quality = max(row["quality_score"] for row in alternatives)
            near_best = [
                row for row in alternatives
                if row["quality_score"] >= maximum_quality - search["prefer_lowest_within_quality_points"]
            ]
            selected = min(near_best, key=lambda row: (row["height_agl_m"], -row["quality_score"]))
            status = "ALTERNATIVE_POM"
    valid = [row for row in candidates if row.get("fit_valid")]
    best_review = max(valid, key=lambda row: (row["quality_score"], -row["height_agl_m"])) if valid else None
    return {"status": status, "selected": selected, "best_review": selected or best_review, "standard": standard}


def compact_plane(candidate: dict | None) -> dict | None:
    if not candidate or not candidate.get("fit_valid"):
        return None
    return {
        "center_xyz": [rounded(value) for value in candidate["fitted_center_xyz"]],
        "axis_center_xyz": [rounded(value) for value in candidate["axis_center_xyz"]],
        "axis_direction": [rounded(value) for value in candidate["axis_direction"]],
        "basis_u": [rounded(value) for value in candidate["basis_u"]],
        "basis_v": [rounded(value) for value in candidate["basis_v"]],
        "orientation": "PERPENDICULAR_TO_LOCAL_STEM_AXIS",
        "height_agl_m": rounded(candidate["height_agl_m"], 3),
        "slab_thickness_m": 0.1,
    }


def compact_candidate(candidate: dict | None) -> dict | None:
    if candidate is None:
        return None
    keys = (
        "height_agl_m", "fit_valid", "point_count", "radius_m", "diameter_cm", "circumference_cm",
        "fit_rmse_m", "relative_fit_rmse", "inlier_count", "angular_coverage_deg",
        "largest_missing_angular_sector_deg", "axis_center_offset_m", "ellipse_axis_ratio", "circularity",
        "fit_center_xy",
        "component_count", "component_point_count", "axis_source", "axis_uncertainty_m", "inclination_deg",
        "neighbouring_valid_slice_count", "expected_neighbouring_slice_count", "radius_stability_mad_m",
        "relative_radius_mad", "quality_score", "quality_components", "quality_penalties",
        "relative_radius_range", "cleaner_smaller_upper_section_available", "cleaner_upper_section_heights_m",
        "fit_rejection_reasons", "standard_failures", "alternative_failures",
    )
    ready = {key: candidate.get(key) for key in keys if key in candidate}
    ready["measurement_plane"] = compact_plane(candidate)
    return phase1.json_ready({
        key: rounded(value, 6) if isinstance(value, (float, np.floating)) else value
        for key, value in ready.items()
    })


def sample_evidence(
    tree_id: str,
    xyz: np.ndarray,
    rgb: np.ndarray,
    ground: dict,
    axis: dict,
    observations: list[dict],
    candidates: list[dict],
    decision: dict,
    config: dict,
) -> dict:
    settings = config["evidence"]
    decimals = int(settings["coordinate_decimals"])
    display_mask = (xyz[:, 2] >= ground["ground_z_m"] - 0.1) & (xyz[:, 2] <= ground["ground_z_m"] + 4.15)
    display_indexes = np.flatnonzero(display_mask)
    display_indexes = display_indexes[even_indexes(len(display_indexes), settings["maximum_tube_points"])]
    focus = decision["selected"] or decision["best_review"]
    accepted_xyz = np.empty((0, 3), dtype=float)
    rejected_xyz = np.empty((0, 3), dtype=float)
    accepted_xy = np.empty((0, 2), dtype=float)
    rejected_xy = np.empty((0, 2), dtype=float)
    if focus and focus.get("fit_valid") and focus.get("_fit"):
        section = focus["_section"]
        fit = focus["_fit"]
        component_indexes = np.asarray(fit["_component_point_indexes"], dtype=np.int64)
        inlier_mask = np.asarray(fit["_inlier_mask"], dtype=bool)
        accepted_section_indexes = component_indexes[inlier_mask]
        all_section_indexes = np.arange(section["point_count"], dtype=np.int64)
        rejected_section_indexes = np.setdiff1d(all_section_indexes, accepted_section_indexes, assume_unique=False)
        accepted_section_indexes = accepted_section_indexes[
            even_indexes(len(accepted_section_indexes), settings["maximum_accepted_slice_points"])
        ]
        rejected_section_indexes = rejected_section_indexes[
            even_indexes(len(rejected_section_indexes), settings["maximum_rejected_slice_points"])
        ]
        accepted_xyz = xyz[section["indexes"][accepted_section_indexes]]
        rejected_xyz = xyz[section["indexes"][rejected_section_indexes]]
        accepted_xy = section["plane_xy"][accepted_section_indexes]
        rejected_xy = section["plane_xy"][rejected_section_indexes]
    return {
        "tree_id": tree_id,
        "full_resolution_tube_point_count": int(len(xyz)),
        "tube_sample_xyz": np.round(xyz[display_indexes], decimals).tolist(),
        "tube_sample_rgb": np.clip(rgb[display_indexes], 0, 255).astype(np.uint8).tolist(),
        "ground_refit": phase1.json_ready(ground),
        "axis_refit": {
            "status": axis["status"],
            "source": axis["source"],
            "direction": [rounded(value) for value in axis["direction"]],
            "inclination_deg": rounded(axis["inclination_deg"], 3),
            "uncertainty_p90_m": rounded(axis["uncertainty_p90_m"]),
            "supporting_slice_count": int(axis["supporting_slice_count"]),
            "supporting_slices": observations,
        },
        "candidate_profile": [compact_candidate(row) for row in candidates],
        "focus_height_agl_m": rounded(focus.get("height_agl_m"), 3) if focus else None,
        "accepted_slice_points_xyz": np.round(accepted_xyz, decimals).tolist(),
        "rejected_slice_points_xyz": np.round(rejected_xyz, decimals).tolist(),
        "accepted_projected_points_xy": np.round(accepted_xy, decimals).tolist(),
        "rejected_projected_points_xy": np.round(rejected_xy, decimals).tolist(),
    }


def process_tree(
    cache_path: Path,
    tree: dict,
    current: dict,
    v3: dict,
    config: dict,
    phase_config: dict,
) -> tuple[dict, dict]:
    tree_id = tree["tree_id"]
    cached = np.load(cache_path)
    xyz = cached["xyz"].astype(np.float64)
    rgb = cached["rgb"]
    seed = {
        "direction": cached["axis_seed"].astype(float),
        "slope": cached["axis_slope"].astype(float),
        "center_xy_at_h0": cached["axis_center_xy_at_h0"].astype(float),
        "height_h0": float(cached["axis_h0"]),
        "ground_z": float(cached["ground_z"]),
        "reference_z": float(cached["ground_z"] + cached["axis_h0"]),
        "mode": str(cached["axis_mode"]),
    }
    ground = refine_ground(xyz, seed, config)
    hint = radius_hint_from_v3(v3)
    axis, observations = fit_axis_observations(
        tree_id, xyz, seed, ground["ground_z_m"], hint, config, phase_config
    )
    candidate_rows = []
    search = config["height_search"]
    for ordinal, height in enumerate(heights(search["standard_height_m"], search["maximum_height_m"], search["step_m"])):
        candidate_rows.append(fit_candidate(
            tree_id, ordinal, height, xyz, ground["ground_z_m"], axis, observations,
            hint, config, phase_config,
        ))
    add_stability_and_quality(candidate_rows, config)
    blocked = blocked_tree_reasons(tree, current, v3, config)
    decision = choose_measurement(candidate_rows, blocked, config, axis["supporting_slice_count"])
    selected = decision["selected"]
    best = decision["best_review"]
    standard = decision["standard"]
    status = decision["status"]
    focus = selected or best
    if selected:
        reasons = [
            "FULL_SOURCE_LAS_PERPENDICULAR_REFIT",
            "ROBUST_MULTI_HEIGHT_STABILITY_QA",
            "STANDARD_HEIGHT_RELIABLE" if status == "STANDARD_DBH" else "STANDARD_HEIGHT_UNRELIABLE_CLEANER_SECTION_SELECTED_ABOVE",
            "POM_NOT_TIED_TO_PROP_ROOT_PLUS_030",
        ]
        if status == "ALTERNATIVE_POM" and standard:
            reasons.extend(f"STANDARD_REJECTED_{reason}" for reason in standard["standard_failures"])
    else:
        reasons = list(blocked)
        if not focus:
            reasons.append("NO_PLAUSIBLE_FULL_LAS_PERPENDICULAR_FIT")
        elif standard and math.isclose(focus["height_agl_m"], standard["height_agl_m"], abs_tol=1e-9):
            reasons.extend(standard["standard_failures"])
        else:
            reasons.extend(focus.get("alternative_failures") or [])
        if not reasons:
            reasons.append("NO_RELIABLE_CLEAN_STEM_SECTION")
    confidence = "MANUAL_REVIEW"
    if selected:
        thresholds = config["confidence_labels"]
        confidence = (
            "HIGH" if selected["quality_score"] >= thresholds["high_minimum_quality_score"]
            else "MEDIUM" if selected["quality_score"] >= thresholds["medium_minimum_quality_score"]
            else "LOW"
        )
    measurement_plane = compact_plane(selected)
    best_review_plane = compact_plane(best) if not selected else None
    location = tree.get("center") or {}
    record = {
        "tree_id": tree_id,
        "location": {"x": rounded(location.get("x")), "y": rounded(location.get("y"))},
        "status": status,
        "measurement_kind": status,
        "automatic_measurement": status in AUTOMATIC_STATUSES,
        "measurement_height_agl_m": rounded(selected["height_agl_m"], 3) if selected else None,
        "candidate_height_agl_m": rounded(focus["height_agl_m"], 3) if focus else None,
        "local_ground_z_m": rounded(ground["ground_z_m"]),
        "ground_refit_status": ground["status"],
        "radius_m": rounded(selected["radius_m"]) if selected else None,
        "diameter_cm": rounded(selected["diameter_cm"], 2) if selected else None,
        "diameter_at_pom_cm": rounded(selected["diameter_cm"], 2) if selected else None,
        "dbh_cm": rounded(selected["diameter_cm"], 2) if status == "STANDARD_DBH" else None,
        "circumference_cm": rounded(selected["circumference_cm"], 2) if selected else None,
        "fit_model": "FULL_LAS_ROBUST_CIRCLE_PERPENDICULAR_TO_LOCAL_AXIS" if selected else None,
        "source_slice_orientation": "PERPENDICULAR_TO_LOCAL_STEM_AXIS",
        "measurement_plane_orientation": "PERPENDICULAR_TO_LOCAL_STEM_AXIS" if focus else None,
        "perpendicular_refit_performed": bool(focus),
        "full_resolution_refit_performed": bool(focus),
        "fit_rmse_m": rounded(focus.get("fit_rmse_m")) if focus else None,
        "relative_fit_rmse": rounded(focus.get("relative_fit_rmse")) if focus else None,
        "circularity": rounded(focus.get("circularity")) if focus else None,
        "ellipse_axis_ratio": rounded(focus.get("ellipse_axis_ratio")) if focus else None,
        "arc_coverage_deg": rounded(focus.get("angular_coverage_deg"), 1) if focus else None,
        "radius_stability_mad_m": rounded(focus.get("radius_stability_mad_m")) if focus else None,
        "radius_stability_relative_mad": rounded(focus.get("relative_radius_mad")) if focus else None,
        "vertical_continuity_score": rounded((focus.get("quality_components") or {}).get("vertical_continuity")) if focus else None,
        "supporting_slice_count": int(focus.get("neighbouring_valid_slice_count") or 0) if focus else 0,
        "point_count": int(focus.get("point_count") or 0) if focus else 0,
        "inlier_count": int(focus.get("inlier_count") or 0) if focus else 0,
        "quality_score": rounded(focus.get("quality_score"), 2) if focus else None,
        "quality_components": phase1.json_ready(focus.get("quality_components")) if focus else None,
        "quality_penalties": phase1.json_ready(focus.get("quality_penalties")) if focus else None,
        "confidence_label": confidence,
        "confidence_is_calibrated": False,
        "field_verified": False,
        "protocol_final": False,
        "local_axis": {
            "status": axis["status"],
            "source": axis["source"],
            "direction_unit": [rounded(value) for value in (focus["axis_direction"] if focus else axis["direction"])],
            "inclination_deg": rounded(focus.get("inclination_deg") if focus else axis["inclination_deg"], 3),
            "uncertainty_p90_m": rounded(axis["uncertainty_p90_m"]),
            "supporting_slice_count": int(axis["supporting_slice_count"]),
            "seed_mode": seed["mode"],
        },
        "measurement_plane": measurement_plane,
        "best_review_plane": best_review_plane,
        "reason_codes": list(dict.fromkeys(reasons)),
        "blocked_reason_codes": blocked,
        "selected_candidate": compact_candidate(selected),
        "best_review_candidate": compact_candidate(best) if not selected else None,
        "standard_height_diagnostics": {
            "candidate_available": bool(standard and standard.get("fit_valid")),
            "candidate": compact_candidate(standard),
            "accepted": status == "STANDARD_DBH",
            "failure_reasons": list(standard.get("standard_failures") or []) if standard else ["NO_STANDARD_HEIGHT_CANDIDATE"],
        },
        "candidate_profile_count": len(candidate_rows),
        "full_resolution_tube_point_count": int(len(xyz)),
        "detection_status": (tree.get("detection") or {}).get("status"),
        "identity_review_status": current.get("identity_review_status"),
        "tree_source_human_labels": v3.get("tree_source_human_labels") or [],
        "v2_baseline": {
            "phase4_measurement_status": (tree.get("measurement") or {}).get("status"),
            "phase4_pom_m": (tree.get("measurement") or {}).get("pom_m"),
            "phase4_circumference_cm": (tree.get("measurement") or {}).get("circumference_cm"),
        },
        "v3_baseline": {
            "status": v3.get("status"),
            "measurement_height_agl_m": v3.get("measurement_height_agl_m"),
            "circumference_cm": v3.get("circumference_cm"),
        },
    }
    evidence = sample_evidence(
        tree_id, xyz, rgb, ground, axis, observations, candidate_rows, decision, config
    )
    return record, evidence


CSV_COLUMNS = [
    "tree_id", "status", "measurement_kind", "measurement_height_agl_m", "local_ground_z_m",
    "diameter_cm", "dbh_cm", "circumference_cm", "fit_model", "fit_rmse_m", "relative_fit_rmse",
    "circularity", "ellipse_axis_ratio", "arc_coverage_deg", "radius_stability_mad_m",
    "radius_stability_relative_mad", "supporting_slice_count", "point_count", "inlier_count",
    "quality_score", "confidence_label", "detection_status", "identity_review_status",
    "perpendicular_refit_performed", "field_verified", "reason_codes",
]


def render_csv(records: list[dict]) -> bytes:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for record in records:
        row = {key: record.get(key) for key in CSV_COLUMNS}
        row["reason_codes"] = "|".join(record["reason_codes"])
        writer.writerow(row)
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")


def build_source_metadata(root: Path, context: dict, las_metadata: dict) -> dict:
    config = context["config"]
    files = {
        key: {"path": str(path.relative_to(root)), "sha256": sha256_path(path)}
        for key, path in context["paths"].items()
    }
    files["marking_manifest"] = {
        "path": "site/public/data/lidar-measurements/markings/TREE_*.json",
        "file_count": len(context["marking_paths"]),
        "sha256_manifest": sha256_directory(context["marking_paths"], root),
    }
    return {
        "site_id": config["site_id"],
        "raw_las_in_repository": False,
        "source_las": {
            **las_metadata,
            "google_drive_file_id": config["source_las"]["google_drive_file_id"],
            "public_url": config["source_las"]["public_url"],
        },
        "files": files,
        "interpretation": "Full-resolution geometry screening from the public Samut Songkhram LAS; not field ground truth.",
    }


def build_summary(records: list[dict], context: dict, source: dict) -> dict:
    counts = Counter(record["status"] for record in records)
    automatic_ids = {record["tree_id"] for record in records if record["automatic_measurement"]}
    v2_ids = {
        tree["tree_id"] for tree in context["inventory"]["trees"]
        if (tree.get("measurement") or {}).get("status") == "MEASURABLE"
    }
    v3_ids = {
        record["tree_id"] for record in context["v3"]["records"]
        if record.get("automatic_measurement")
    }
    reasons = Counter(
        reason for record in records if record["status"] == "MANUAL_REVIEW" for reason in record["reason_codes"]
    )
    return {
        "algorithm_version": context["config"]["algorithm_version"],
        "workflow": WORKFLOW,
        "tree_count": len(records),
        "status_counts": dict(sorted(counts.items())),
        "automatic_measurement_count": len(automatic_ids),
        "automatic_measurement_tree_ids": sorted(automatic_ids),
        "manual_review_count": counts["MANUAL_REVIEW"],
        "manual_review_reason_counts": dict(sorted(reasons.items())),
        "coverage_comparison": {
            "metric": "tree_count_with_automatic_geometry_measurement",
            "v2_phase4_measurable_count": len(v2_ids),
            "v3_sampled_evidence_automatic_count": len(v3_ids),
            "v3_1_full_las_automatic_count": len(automatic_ids),
            "net_change_from_v2": len(automatic_ids) - len(v2_ids),
            "net_change_from_v3": len(automatic_ids) - len(v3_ids),
            "newly_automatic_vs_v3_count": len(automatic_ids - v3_ids),
            "newly_automatic_vs_v3_tree_ids": sorted(automatic_ids - v3_ids),
            "v3_automatic_now_manual_count": len(v3_ids - automatic_ids),
            "v3_automatic_now_manual_tree_ids": sorted(v3_ids - automatic_ids),
            "accuracy_comparison_performed": False,
            "interpretation": "Coverage comparison only; no field-verified accuracy claim.",
        },
        "height_search": {
            **context["config"]["height_search"],
            "four_metre_search_executed": True,
            "source_profile_limit_applies": False,
        },
        "source": source,
        "field_verified": False,
        "confidence_is_calibrated": False,
        "perpendicular_full_resolution_refit_performed": True,
    }


def build_review_queue(records: list[dict], summary: dict) -> dict:
    return {
        "algorithm_version": summary["algorithm_version"],
        "workflow": WORKFLOW,
        "queue_size": len(records),
        "entries": [{
            "review_item_id": record["tree_id"],
            "measurement_status": record["status"],
            "confidence_label": record["confidence_label"],
            "detection_status": record["detection_status"],
            "identity_review_status": record["identity_review_status"],
            "measurement_height_agl_m": record["measurement_height_agl_m"],
            "diameter_cm": record["diameter_cm"],
            "circumference_cm": record["circumference_cm"],
            "quality_score": record["quality_score"],
            "reason_codes": record["reason_codes"],
            "evidence_url": record["evidence_url"],
        } for record in records],
    }


def write_artifacts(
    root: Path,
    source_las: Path,
    tube_cache_directory: Path,
    output_directory: Path | None = None,
    config_path: Path | None = None,
    workers: int = 1,
) -> dict:
    context = load_context(root, config_path)
    metadata = validate_source_las(source_las, context["config"])
    inventory_trees = sorted(context["inventory"]["trees"], key=lambda row: row["tree_id"])
    tree_ids = [tree["tree_id"] for tree in inventory_trees]
    validate_tube_cache(tube_cache_directory, tree_ids)
    current_by_tree = {row["tree_id"]: row for row in context["current"]["records"]}
    v3_by_tree = {row["tree_id"]: row for row in context["v3"]["records"]}
    jobs = [(
        tube_cache_directory / f"{tree['tree_id']}.npz",
        tree,
        current_by_tree[tree["tree_id"]],
        v3_by_tree[tree["tree_id"]],
        context["config"],
        context["phase_config"],
    ) for tree in inventory_trees]
    results: dict[str, tuple[dict, dict]] = {}
    if workers <= 1:
        for index, job in enumerate(jobs, 1):
            record, evidence = process_tree(*job)
            results[record["tree_id"]] = (record, evidence)
            print(f"V3.1 full-LAS fit {index:03d}/{len(jobs)} {record['tree_id']} {record['status']}", flush=True)
    else:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(process_tree, *job): job[1]["tree_id"] for job in jobs}
            completed = 0
            for future in as_completed(futures):
                record, evidence = future.result()
                results[record["tree_id"]] = (record, evidence)
                completed += 1
                print(f"V3.1 full-LAS fit {completed:03d}/{len(jobs)} {record['tree_id']} {record['status']}", flush=True)
    records = [results[tree_id][0] for tree_id in tree_ids]
    evidence_by_tree = {tree_id: results[tree_id][1] for tree_id in tree_ids}
    if len(records) != 118 or len(set(tree_ids)) != 118:
        raise RuntimeError("The preserved 118 physical Tree IDs are required")
    output_directory = output_directory or root / "site/public/viewer-v3-full-las/data"
    output_directory.mkdir(parents=True, exist_ok=True)
    shard_size = int(context["config"]["evidence"]["trees_per_shard"])
    evidence_index = {"algorithm_version": context["config"]["algorithm_version"], "trees": {}}
    for shard_number, start in enumerate(range(0, len(tree_ids), shard_size)):
        shard_tree_ids = tree_ids[start:start + shard_size]
        name = f"evidence-{shard_number:03d}.json"
        shard = {
            "algorithm_version": context["config"]["algorithm_version"],
            "tree_ids": shard_tree_ids,
            "evidence": {tree_id: evidence_by_tree[tree_id] for tree_id in shard_tree_ids},
        }
        atomic_write(output_directory / name, compact_json_bytes(shard))
        for tree_id in shard_tree_ids:
            evidence_index["trees"][tree_id] = name
            results[tree_id][0]["evidence_url"] = f"data/{name}"
    source = build_source_metadata(root, context, metadata)
    summary = build_summary(records, context, source)
    payload = {
        "algorithm_version": context["config"]["algorithm_version"],
        "workflow": WORKFLOW,
        "tree_count": len(records),
        "field_verified": False,
        "source": source,
        "records": records,
    }
    atomic_write(output_directory / "measurements.json", compact_json_bytes(payload))
    atomic_write(output_directory / "measurements.csv", render_csv(records))
    atomic_write(output_directory / "summary.json", canonical_json_bytes(summary))
    atomic_write(output_directory / "review_queue.json", compact_json_bytes(build_review_queue(records, summary)))
    atomic_write(output_directory / "evidence-index.json", compact_json_bytes(evidence_index))
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--source-las", type=Path, required=True)
    parser.add_argument("--tube-cache-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--workers", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    parser.add_argument("--extract-cache", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = args.root.resolve()
    if args.extract_cache:
        context = load_context(root, args.config)
        metadata = validate_source_las(args.source_las.resolve(), context["config"])
        extract_tube_cache(root, args.source_las.resolve(), args.tube_cache_dir.resolve(), context, metadata)
    summary = write_artifacts(
        root,
        args.source_las.resolve(),
        args.tube_cache_dir.resolve(),
        args.output_dir.resolve() if args.output_dir else None,
        args.config.resolve() if args.config else None,
        args.workers,
    )
    print(json.dumps(summary["status_counts"], sort_keys=True))


if __name__ == "__main__":
    main()
