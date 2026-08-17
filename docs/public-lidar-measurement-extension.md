# Public LiDAR measurement extension

This extension adds a static, read-only viewer at `/lidar-measurements/`.

It publishes derived artifacts only:

- `measurements.json` and `measurements.csv` with 118 stable Tree IDs;
- `summary.json` with the field-aid, exclusion, and prop-root counts;
- one marking JSON per Tree ID for the point-cloud slice, measurement plane,
  accepted points, rejected points, and fitted outline;
- the static HTML/CSS/JavaScript viewer.

The current public bundle records the operator's eight confirmed exclusions,
keeps the earlier 29 Full-LAS measurements traceable, and exposes all four
highest-prop-root `+0.30 m` cases. Values are LiDAR estimates and are not
field-verified measurements.

The source LAS and internal pipeline/debug products are deliberately excluded
from the public repository. To run the viewer locally from the repository
root, serve the static files over HTTP and open `/lidar-measurements/`.
