# Samut Songkhram V2 LiDAR review viewer

This directory preserves the local Samut Songkhram LiDAR geometry-review application. It is a browser review tool, not a field-verified tree inventory.

## Use

Serve `site/public` with a static HTTP server and open:

`http://localhost:3001/viewer-v2-review/`

The intended public route is:

`https://tree-pointclouds-rayong.vercel.app/viewer-v2-review/`

The viewer defaults to the **Phase 1.75 pilot** queue: 40 review items consisting of 39 automatic candidates and one manual large-root placeholder. It also exposes the 460-item Phase 1.5 queue and later Phase 4, Phase 4B, Phase 4C, Phase 4D, and Phase 5A review queues. Candidate `C-0174` is associated with physical `TREE_0017`; Phase 5A includes the `MANGROVE_PROP_ROOT_PLUS_030` protocol.

## Preserved files

- `site/public/viewer-v2-review/` — complete static runtime snapshot, including queue manifests, candidate point crops, evidence, and phase inventories.
- `annotations/phase1_75_pilot_review.json` — human review export; clean-height hints are not automatic final POM values.
- `outputs/review_queue_v2_phase1_75_pilot.json` — canonical 40-item Phase 1.75 queue.
- `scripts/build_phase1_75_pilot_review.py` — tracked generator for the Phase 1.75 review snapshot.

The viewer data are derived LiDAR/review results and are not automatically field verified. No raw LAS/LAZ/COPC point-cloud file is included. Source-LAS metadata in the published snapshot has been made non-local so the public repository does not expose private workstation or Google Drive paths.

Regeneration requires the original local processing inputs and raw LAS data, which remain outside this public preservation commit. The committed static snapshot is the reproducible serving artifact.
