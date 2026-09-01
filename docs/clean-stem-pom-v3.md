# Samut Songkhram clean-stem POM V3

## Purpose

V3 is a separate coverage-first screening workflow. It does not change the
existing `/viewer-v2-review/`, Phase 5A, or LiDAR field-aid outputs. V3 prefers
a reliable 1.30 m AGL measurement and, when that level is unreliable, searches
upward for the strongest multi-slice clean-stem window. Selection is not tied
to the highest prop-root attachment plus 0.30 m rule.

The standalone review route is `/viewer-v3-clean-stem/`. Its generated data is
kept under `site/public/viewer-v3-clean-stem/data/`.

## Inputs and limits

The generator reads these Samut Songkhram products without modifying them:

- the Phase 1.5 review queue with sampled stable windows and robust tracks;
- the Phase 3 candidate-to-Tree-ID associations;
- the Phase 4 inventory containing the preserved 118 Tree IDs; and
- the current measurement release only for exclusion, identity, and comparison context.

The raw LAS is intentionally absent from GitHub. Published height profiles end
at 3.50 m AGL. The requested ceiling remains configurable at 4.00 m, but the
current run does not extrapolate or fabricate evidence above 3.50 m. A robust
0.30 m window can therefore have a center no higher than 3.35 m in this release.

The preserved stable-window radii were fitted in horizontal XY profiles. V3
derives the robust local axis and emits a marking/debug plane perpendicular to
that axis, and the browser projects real sampled crop points onto that plane.
It does **not** claim that the published radius was newly refitted in the
perpendicular plane. A true full-resolution perpendicular refit at every new
POM requires the excluded raw LAS and must be run in the private data workspace.

## Deterministic selection

For each preserved physical Tree ID, V3 gathers only Phase 1.5 tracks linked by
the Phase 3 association graph. It evaluates every published 0.30 m stable
window from 1.30 m upward. This is geometry-level reasoning over a tracked stem,
not point-by-point root-versus-trunk classification.

The quality score is a fixed weighted combination of:

- local-axis verticality from the robust Phase 1.5 centreline;
- vertical continuity across the seven expected slices;
- circularity from nearby ellipse axis ratios;
- relative radius MAD within the window;
- median angular coverage; and
- relative circle-fit residual.

Clutter and identity ambiguity are explicit penalties. Thresholds and weights
are in `config/clean_stem_pom_v3.json`.

Near 1.30 m, V3 also uses the independent published full-LAS field-aid lane as
a disagreement check, not as ground truth. If candidate diameter differs by
more than 45%, that near-standard window is rejected and the search continues
upward. This catches internally stable root/branch components and wrong-track
associations that a single-lane score can otherwise rate highly.

Selection order is:

1. choose the best reliable window containing 1.30 m and report `STANDARD_DBH`;
2. otherwise choose the highest-quality reliable window above 1.30 m and report `ALTERNATIVE_POM`; or
3. report `MANUAL_REVIEW` with the best candidate window and failure reasons, but no V3 diameter or circumference.

Operationally excluded Tree IDs, identity-reviewed duplicates/false positives,
and uncertain tree detections cannot become automatic measurements. A duplicate
candidate trace may remain attached to a confirmed physical tree, but it cannot
by itself authorize a measurement.

## Output semantics

Each of the 118 records includes location, local ground, exact POM, local axis
and inclination, robust-window radius, diameter, circumference, fit residual,
circularity, coverage, radius stability, continuity, point support, quality
components, confidence label, status, reason codes, V2 context, and provenance.

Only `STANDARD_DBH` uses the `dbh_cm` field. `ALTERNATIVE_POM` reports diameter
and circumference at its explicit non-standard height and leaves `dbh_cm` null.
All confidence labels are deterministic QA labels, not calibrated probabilities.
Every record is `field_verified: false` and `protocol_final: false`.
Every record also exposes `source_slice_orientation` and
`perpendicular_refit_performed: false` so the approximation cannot be mistaken
for a new full-resolution perpendicular fit.

The current real-data run produces:

| Lane | Tree IDs |
| --- | ---: |
| `STANDARD_DBH` | 13 |
| `ALTERNATIVE_POM` | 29 |
| Automatic total | 42 |
| `MANUAL_REVIEW` | 76 |

The preserved Phase 4 geometry lane had 29 measurable Tree IDs. V3 therefore
adds 13 net automatic geometry measurements and makes 25 Tree IDs automatic
that were not measurable in that lane. This is a coverage comparison only; no
field-verified accuracy comparison has been performed. The existing field-aid
release contains 108 numeric screening aids under different acceptance rules,
so it is reported separately and is not presented as a V3 accuracy baseline.

## Run

```sh
python scripts/run_clean_stem_pom_v3.py
python -m unittest tests.test_clean_stem_pom_v3
```

The browser shows the selected oriented POM plane, local stem axis, sampled
point crop, radius profile, a sampled 2D perpendicular cross-section, all scored
windows for the selected evidence track, quality components, and explicit
failure reasons. CSV, compact JSON, and summary JSON are downloadable from the
standalone route.
