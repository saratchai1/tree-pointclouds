# Public LiDAR measurement extension

This extension publishes derived, read-only LiDAR measurement artifacts for the
Samut Songkhram point cloud. The source LAS and internal pipeline/debug products
are deliberately excluded from the public repository.

## Published data

- `measurements.json` and `measurements.csv` contain 118 stable Tree IDs.
- `summary.json` contains field-aid, exclusion, protocol, and prop-root counts.
- `markings/TREE_*.json` contains the point-cloud slice, measurement plane,
  accepted/rejected points, and fitted circle or ellipse for each Tree ID.
- `/lidar-measurements/` is the detailed per-tree QA viewer.
- `/viewer/` is the main Three.js point-cloud viewer with integrated 3D rings.

The public bundle records the operator-confirmed exclusions, preserves the
traceable earlier Full-LAS measurements, and exposes all confirmed or candidate
highest-prop-root attachment `+0.30 m` cases. Values are LiDAR estimates and are
not field-verified measurements.

## Main viewer integration

The main viewer does not download every large marking file at startup. Instead,
`scripts/build_lidar_viewer_index.mjs` reads the measurement table and all
markings, then writes a compact
`site/public/data/lidar-measurements/viewer-index.json` containing only the
plane, basis vectors, selected circle/ellipse fit, display value, protocol, and
QA status required for the 3D overlay.

Only operational `READY_FOR_FIELD_USE` and `CHECK_ON_SITE` records with a valid
measurement plane, fit, and field-aid circumference are rendered as rings.
Confirmed exclusions and records with no plausible fit remain searchable audit
records and are shown only as status markers, not as measurements.

Regenerate the index after changing measurement or marking data:

```sh
node scripts/build_lidar_viewer_index.mjs
```

The GitHub Actions workflow `.github/workflows/build-lidar-viewer-index.yml`
performs the same generation on the integration branch and on `main`.
