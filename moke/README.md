# Moke garden point cloud

This folder contains a local browser preview reconstructed from the shared
PIX4Dcloud project. The viewer loads local copies of the point positions and
RGBA colours; no garden data is uploaded elsewhere.

Run from the repository root:

```sh
python3 -m http.server 8094
```

Then open <http://127.0.0.1:8094/moke/viewer/>.

The viewer starts at a one-million-point rendering budget for responsiveness.
Use the selector in the panel to display the full 3.9-million-point cloud.

The viewer also shows the estimated 1.3 m trunk section and crown footprint for
the largest tree. Numerical results and limitations are recorded in
`tree-measurement.json`.
