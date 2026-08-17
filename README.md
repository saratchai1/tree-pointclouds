# Tree Point Clouds

Interactive Three.js viewers and LiDAR-derived tree measurements for the
Samut Songkhram mangrove point cloud and the earlier PIX4Dcatch garden sample.

## Local preview

```sh
python3 -m http.server 8094
```

Open the Samut Songkhram viewer at
`http://127.0.0.1:8094/site/public/viewer/`.

The earlier PIX4Dcatch sample remains available at
`http://127.0.0.1:8094/moke/viewer/`.

## Samut Songkhram LiDAR viewers

The main Three.js point-cloud viewer is served from `/viewer/`. It overlays
compact 3D measurement rings generated from the full-resolution per-tree
marking geometry. The viewer supports all 118 stable Tree IDs, status filters,
search, CSV export, click-to-focus, standard 1.30 m measurements, and the
highest-prop-root attachment `+0.30 m` protocol where applicable.

The detailed read-only QA viewer remains available at `/lidar-measurements/`.
It exposes the point-cloud slice, measurement plane, accepted/rejected points,
fit outline, QA reasons, and audit status for each Tree ID.

The compact 3D index is generated rather than edited manually:

```sh
node scripts/build_lidar_viewer_index.mjs
```

The GitHub Actions workflow `Build LiDAR viewer index` rebuilds and commits
`site/public/data/lidar-measurements/viewer-index.json` when the measurement
records, marking files, or generator change.

All values shown by these viewers are LiDAR estimates and are not field-verified
measurements. Records confirmed as wrong or lacking a plausible fit remain
visible for audit but are not drawn as measurement rings.

## Rayong site-001 archive processing

The raw LAS remains in the restricted Google Drive archive. Run the following
command on the Mac that has Google Drive for desktop mounted:

```sh
curl -fsSL https://raw.githubusercontent.com/saratchai1/tree-pointclouds/main/scripts/process_rayong_site001.sh | bash
```

The pipeline keeps the raw LAS unchanged and writes the following beside it:

- `metadata/`: SHA-256, PDAL summary, metadata, schema and processing logs
- `derived/*.copc.laz`: full-resolution COPC and a roughly two-million-point preview COPC

The script installs PDAL through Homebrew when PDAL is not already available.
Set `FORCE=1` to rebuild existing COPC files or `TARGET_PREVIEW_POINTS` to change
the preview point budget.

## Privacy

The public bundle contains derived measurement artifacts only. It intentionally
does not include the source LAS, private debug caches, or the full internal
analysis workspace. Keep raw georeferenced source data local unless the owner
explicitly authorizes another release.
