---
name: mangrove-local-workspace
description: Run persistent web review, reuse learned models, and screen a new local LAS without losing earlier operator feedback.
---

# Local web feedback and new survey screening

Continue PR #8 on feat/review-feedback-learning-20261009. Read
`docs/feedback-workspace.md`. This extends, not replaces, the v1 learning skill.

1. Use Python >=3.11 and the isolated `requirements-feedback-workspace.txt`.
2. Start `python scripts/start_feedback.py` or the OS launcher. Open
   `http://127.0.0.1:8095/`. Do not expose this single-operator backend remotely.
3. Import existing pilot reviews automatically at startup; do not ask the user
   to repeat reviews already stored in annotations/the workspace database.
4. For V3.1 reviews, open the existing viewer through the local server. The
   bridge registers the exact displayed record AND point-evidence snapshot.
   Reject stale/mismatched snapshots; never label a different selected tree.
5. Record only explicit operator decisions. Identity and ring validity are
   different tasks. Unknowns are unlabelled, not negative examples. A screen
   review never becomes field-verified circumference.
6. Save the event transaction before retraining. Keep history, revision checks,
   and idempotency. Training failure must not remove a review or a prior model.
7. For a new survey, use the upload UI and require explicit metric units. The
   new adapter screens LAS locally and scores candidates using saved advisory
   models. Do not call this the unchanged frozen V3.1 pipeline.
8. Keep measurement full-resolution; sample only for seed discovery/display.
   Missing geometry must remain missing. Respect resource limits and surface
   interrupted/failed jobs. LAZ requires the optional reader and was not tested
   in the initial increment.
9. Review `.feedback-workspace/training/<id>/report.json`. Sites and shared
   source hashes stay together in folds. One site cannot establish cross-site
   accuracy. No automatic release/promotion based on training completing.
10. Run the existing 23 learning tests and the workspace tests manually. The
    Chromium smoke is offline DOM + real ASGI, not a deployed-site assertion.

Never place state under site/public, delete prior answers, overwrite the frozen
V2/V3/V3.1 data, publish raw files, create recurring CI/CD, or deploy/merge just
because this skill was invoked. SQLite here is durable local desktop storage;
it is not a shared PostgreSQL service and not persistent Vercel storage.
