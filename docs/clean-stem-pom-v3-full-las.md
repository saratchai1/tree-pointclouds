# Clean-stem POM V3.1 — Samut Songkhram full-LAS lane

V3.1 is a new, additive measurement lane for the Samut Songkhram `TD_008`
point cloud. It does not replace or rewrite the frozen V2 review or the
sampled-evidence V3 lane.

## Source and reproducibility

- Source: `TD_008_2026_08_07_07_04_07.las`
- Public Drive file ID: `1y2u4apM3vEZHbea9u8HOXc4uSVgqmSbI`
- Size: `1,746,603,215` bytes
- LAS points: `67,177,038`
- SHA-256: `195725dbbc7f853994f926027dfea9c9e4d986ac06cd6d9187f30ffebe528276`
- LAS version / point format: `1.2 / 2 (RGB)`

The raw LAS and the per-tree working cache stay outside Git. The repository
contains the deterministic algorithm, configuration, compact review evidence,
source checksum, and generated measurements. The generator validates the LAS
size, byte layout, point count, and SHA-256 before fitting.

## Measurement method

For each of the preserved 118 physical Tree IDs, the workflow:

1. extracts a full-resolution tube around the frozen marking axis;
2. rechecks local ground against full-LAS points, with a bounded shift from the
   preserved ground estimate;
3. refits the stem centreline from horizontal full-resolution observations
   between 1.00 and 4.10 m AGL;
4. builds a local measurement plane perpendicular to that centreline at every
   0.10 m from 1.30 through 4.00 m AGL;
5. fits a robust circle and ellipse diagnostic to each full-resolution plane;
6. scores arc coverage, fit residual, circularity, radius stability, axis
   alignment, and vertical continuity;
7. reports `STANDARD_DBH` at exactly 1.30 m when it passes the strict lane;
8. otherwise reports the lowest near-best reliable `ALTERNATIVE_POM` above
   1.30 m; and
9. reports `MANUAL_REVIEW` without an automatic diameter or circumference when
   geometry or tree identity is not reliable enough.

Alternative POM selection is deliberately not tied to the highest prop root
plus 0.30 m. The actual selected height is always stored with the result.

Inclination alone is not a rejection condition. Leaning stems are measured on
a plane perpendicular to their local axis. Identity exclusions and uncertain
tree detections remain conservative gates.

## Root-crown safeguards

A dense prop-root crown can produce a visually excellent but biologically
wrong large circle. V3.1 therefore uses all of the following instead of trusting
circle residual alone:

- three-level radius MAD and total radius range;
- a search for a smaller, stable, cleaner section above a large lower section;
- stronger arc and circularity requirements for large sections; and
- an automatic cohort guardrail of 0.18 m radius. Larger candidates remain
  visible in review evidence but cannot be released automatically without
  field calibration.

This guardrail is an operational screening safeguard, not a biological claim
that larger mangrove stems cannot exist.

## Current result

The committed V3.1 run produced:

| Status | Tree IDs |
|---|---:|
| `STANDARD_DBH` | 21 |
| `ALTERNATIVE_POM` | 39 |
| `MANUAL_REVIEW` | 58 |
| Automatic total | 60 |

Coverage comparison only:

| Lane | Automatic geometry measurements |
|---|---:|
| V2 Phase 4 | 29 |
| V3 sampled evidence | 42 |
| V3.1 full LAS | 60 |

V3.1 adds 21 Tree IDs that were manual in V3 and moves 3 former V3 automatic
results back to manual (`TREE_0020`, `TREE_0063`, and `TREE_0085`). The net
change from V3 is +18. This is not an accuracy comparison because no matched
field circumference/DBH validation set was supplied.

## Run

Generate the untracked tube cache and results on the first run:

```bash
python scripts/clean_stem_pom_v3_full_las.py \
  --source-las /path/to/TD_008_2026_08_07_07_04_07.las \
  --tube-cache-dir /path/to/td008-v31-tubes \
  --extract-cache \
  --workers 4
```

Reuse the cache on subsequent deterministic runs by omitting
`--extract-cache`. The default output is
`site/public/viewer-v3-full-las/data/`.

## Review UI and interpretation

Open `/viewer-v3-full-las/` to inspect:

- an RGB sample from each full-resolution tree tube;
- the refitted local axis and selected/review plane;
- the complete 1.30–4.00 m diameter profile;
- accepted and rejected points in the perpendicular cross-section; and
- every pass/fail reason used by the automatic gate.

`HIGH`, `MEDIUM`, and `LOW` are deterministic geometry-QA labels. They are not
calibrated probabilities. All outputs remain unverified field aids and are not
protocol-final measurements.
