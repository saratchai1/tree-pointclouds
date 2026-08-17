# Rayong preview DBH measurements

> **SCREENING RESULT — not field verified and not a final DBH claim.**

The calculation reuses the repository stem skill: vertically persistent candidate detection, circle fitting at five heights, centre/radius consistency checks, and duplicate suppression.

- Browser points analysed: 998,284
- Source LAS points reported in metadata: 106,816,323
- Browser sampling stride: 1/107
- Retained candidates: 1
- Confidence: high 0, medium 0, low 1

| Tree | DBH screening (cm) | Circumference (cm) | Status | Confidence | Fit points | Coverage | Slices |
|---|---:|---:|---|---|---:|---:|---:|
| RY-001 | 59.7 | 187.4 | CHECK_ON_SITE | low | 18 | 0.54 | 3 |

QA flags for RY-001: `FITTED_RADIUS_NEAR_0_30_M_BOUND`, `LIMITED_POINT_SUPPORT`, `PARTIAL_ANGULAR_COVERAGE`, `LIMITED_VERTICAL_SLICE_SUPPORT`, `CENTERLINE_SPREAD_HIGH`, `RADIUS_VARIATION_HIGH`

The retained geometry is a candidate for locating the object in the viewer and planning a field/full-LAS check. Re-run the centre against the full LAS and review point-of-measurement applicability for prop-root trees before formal reporting.
