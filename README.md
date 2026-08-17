# Tree Point Clouds

Interactive Three.js viewer and measurements for a PIX4Dcatch garden point
cloud.

## Local preview

```sh
python3 -m http.server 8094
```

Open <http://127.0.0.1:8094/moke/viewer/>.

The viewer includes a highlighted breast-height trunk section and a 3D crown
measurement overlay for the largest tree. See `moke/tree-measurement.json` for
the numerical results and limitations.

## Public LiDAR measurement extension

The derived full-resolution LiDAR measurement viewer is available at
`/lidar-measurements/` when this repository is served as a static site. It
includes circumference/diameter field-aid values, the recovered earlier
Full-LAS measurement lane, the four highest-prop-root `+0.30 m` cases, CSV and
JSON exports, and per-tree marking evidence for all 118 Tree IDs.

The public extension intentionally does not include the source LAS, private
debug caches, or the full internal analysis workspace. The owner explicitly
authorized this derived public release; the raw georeferenced source remains
local.

## Privacy

This repository contains georeferenced point-cloud data from a private garden.
Keep repository and deployment access private unless the owner explicitly
chooses to publish it.
