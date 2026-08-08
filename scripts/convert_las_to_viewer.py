#!/usr/bin/env python3
"""Convert a colored LAS point cloud into the small binary format used by the viewer.

The source LAS can be very large.  This converter keeps a regular sample of at
most ``--max-points`` points, recenters the coordinates, and writes three
position chunks plus an RGBA color buffer.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
from pathlib import Path

import numpy as np


def las_header(path: Path) -> dict:
    with path.open("rb") as handle:
        header = handle.read(227)

    if header[:4] != b"LASF":
        raise ValueError(f"{path} is not a LAS file")

    return {
        "point_data_offset": struct.unpack_from("<I", header, 96)[0],
        "point_format": header[104],
        "record_length": struct.unpack_from("<H", header, 105)[0],
        "point_count": struct.unpack_from("<I", header, 107)[0],
        "scale": struct.unpack_from("<3d", header, 131),
        "offset": struct.unpack_from("<3d", header, 155),
        "version": f"{header[24]}.{header[25]}",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--max-points", type=int, default=1_000_000)
    args = parser.parse_args()

    if args.max_points < 1:
        raise ValueError("--max-points must be positive")

    header = las_header(args.input)
    if header["point_format"] not in (2, 3):
        raise ValueError(
            "This converter expects LAS point format 2 or 3 with RGB colors; "
            f"got format {header['point_format']}"
        )
    if header["record_length"] < 26:
        raise ValueError("LAS point records are missing RGB fields")

    count = header["point_count"]
    step = max(1, math.ceil(count / args.max_points))
    dtype = np.dtype(
        {
            "names": ["xyz", "rgb"],
            "formats": [("<i4", (3,)), ("<u2", (3,))],
            "offsets": [0, 20],
            "itemsize": header["record_length"],
        }
    )
    points = np.memmap(
        args.input,
        dtype=dtype,
        mode="r",
        offset=header["point_data_offset"],
        shape=(count,),
    )
    sampled = points[::step]
    xyz = sampled["xyz"].astype(np.float64) * np.asarray(header["scale"])
    xyz += np.asarray(header["offset"])

    # Keep the cloud near the origin so the browser camera and depth buffer
    # remain stable even when the LAS uses projected coordinates.
    center = (xyz.min(axis=0) + xyz.max(axis=0)) / 2
    positions = (xyz - center).astype("<f4")

    rgb = sampled["rgb"]
    if int(rgb.max()) > 255:
        rgb = rgb >> 8
    colors = np.empty((len(sampled), 4), dtype=np.uint8)
    colors[:, :3] = rgb.astype(np.uint8)
    colors[:, 3] = 255

    args.output_dir.mkdir(parents=True, exist_ok=True)
    chunk_size = math.ceil(len(positions) / 3)
    for index in range(3):
        start = index * chunk_size
        end = min(start + chunk_size, len(positions))
        positions[start:end].tofile(args.output_dir / f"positions-{index:02d}.glbin")
    colors.tofile(args.output_dir / "colors.glbin")

    bbox_min = positions.min(axis=0).tolist()
    bbox_max = positions.max(axis=0).tolist()
    metadata = {
        "name": "Samut Songkram Mangrove Point Cloud",
        "description": "Sampled colored LAS point cloud from the mangrove survey.",
        "source": args.input.name,
        "sourcePointCount": int(count),
        "samplingStride": int(step),
        "points": int(len(positions)),
        "projection": "LAS source coordinates, recentered for browser viewing",
        "offset": [0, 0, 0],
        "scale": [1, 1, 1],
        "boundingBox": {"min": bbox_min, "max": bbox_max},
        "attributes": [
            {
                "name": "rgba",
                "type": "uint8",
                "numElements": 4,
                "elementSize": 1,
                "size": 4,
                "bufferView": {"uri": "colors.glbin", "byteLength": int(colors.nbytes), "byteOffset": 0},
            },
            {
                "name": "position",
                "type": "float",
                "numElements": 3,
                "elementSize": 4,
                "size": 12,
                "bufferView": {
                    "uri": "positions-00.glbin",
                    "byteLength": int(positions.nbytes),
                    "byteOffset": 0,
                },
                "min": bbox_min,
                "max": bbox_max,
            },
        ],
    }
    (args.output_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Converted {count:,} source points -> {len(positions):,} points "
        f"(stride {step}) in {args.output_dir}"
    )


if __name__ == "__main__":
    main()
