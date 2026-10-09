---
name: mangrove-feedback-learning
description: Reuse human LiDAR reviews, train/version advisory models, and apply them to new compatible survey candidates without relabelling old trees.
---

# Human feedback is reusable supervision

Use this skill when the owner asks to learn from correct/incorrect tree reviews,
import a review export, retrain, or reuse learned decisions on a new survey.

## Source and workflow

The implemented command is `python scripts/review_learning.py`.
Do not invent `AGENTS.md` rules, weights, metrics, labels, or field validation.
Do not overwrite frozen V2/V3/V3.1 measurements or publish raw point clouds.

1. Confirm the checked-out branch includes `scripts/review_learning.py` and the
   original `outputs/review_queue_v2_phase1_75_pilot.json` plus
   `annotations/phase1_75_pilot_review.json`.
2. Use an isolated Python environment and `requirements-feedback-learning.txt`.
3. Run `python scripts/review_learning.py train --output .feedback-learning`.
   This imports existing labels; never ask the owner to repeat those reviews.
4. Read `.feedback-learning/report.json`, including ignored/stale/conflicting
   reviews, class counts, skipped groups, and out-of-fold results. Missing model
   labels are not a failed run. Do not substitute automatic labels for human ones.
5. Retain the immutable model JSONs and review ledger. `.latest.json` is a local
   run pointer, not permission to deploy or certify a result.
6. For a new survey, produce the same PRE-REVIEW candidate feature schema, then
   use `score --queue ... --model ... --site-id ... --survey-id ... --output ...`.
   Do not pass raw LAS to `--queue`: raw-LAS ingestion/generalized tree discovery
   is a separate pipeline integration and is NOT implemented by this skill.
7. After another explicit review, use `review --candidate ... --task ...
   --label ... --ledger ...`. This appends the exact-snapshot decision and
   retrains from old plus new reviews. Keep all original exports unchanged.

## Label contract

- `TRUE_MAIN_STEM` is positive for `stem_identity` ONLY.
- `PROP_ROOT_OR_ROOT_ONLY`, `BRANCH`, `OTHER_VEGETATION` are negative identity.
- `DUPLICATE_OF` creates a group relation, NOT a negative stem example.
- `NOT_ENOUGH_INFORMATION` and conflicting answers remain unlabelled.
- Only `MEASUREMENT_CORRECT` / `MEASUREMENT_INCORRECT` supervise
  `measurement_validity`, bound to the exact evidence hash.
- Clean-height hints, manual seed clicks, full-LAS automatic acceptance, and
  field-aid flags are NOT verified circumference targets.
- Existing operational exclusions must remain enforced by the measurement
  pipeline. An identity suggestion cannot remove them or approve a circumference.

## Release policy

This first classifier is advisory. It exports learned coefficients and scores;
that does not establish cross-site accuracy, calibrated probabilities, or fewer
manual reviews. Report actual grouped results even when they are worse than the
baseline. Do not promote merely because training finished. Preserve review of
unknown geometry and geometric QA; never infer missing trunk surfaces.

No merge, deployment, source-data publication, or recurring training workflow is
authorized merely by running this skill. Browser buttons are not yet connected
to a persistent backend in this increment; the CLI and ledger are the implemented
feedback/retraining interface.
