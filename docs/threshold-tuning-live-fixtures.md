# Threshold Tuning On Local Stored Fixtures

This note records the fixture basis for the tuning in this PR.

## Scope

- This is data-driven tuning of existing thresholds and weights.
- No new ranking layer, fallback type, stopword behavior, or supplemental video lane was added here.

## Thresholds changed

- `ADAPTATION_RELEVANCE_WEIGHT`: `1.0` -> `2.0`
- `REPORT_FALLBACK_MIN_ADAPTATION_SCORE`: `0.35` -> `0.45`
- `REPORT_FALLBACK_DELTA_RATIO`: `0.35` -> `0.5`
- Left unchanged on current local evidence:
  - `MIN_VIEWS_END=1000`
  - `MIN_DELTA_VIEWS=500`
  - `REPORT_FALLBACK_MIN_ITEMS_PER_PLATFORM=2`
  - `REPORT_FALLBACK_MAX_ITEMS_PER_PLATFORM=2`

## Local stored fixture basis

The local `db.sqlite3` contains stored reports and snapshots, but it does **not** contain the exact prod `report_id=69` lineage.

- Missing locally:
  - exact `report_id=69`
  - exact prod March 30 payload/log/snapshot set used in the earlier forensic
- Available locally:
  - zero/thin YouTube reports such as `report_id in {7, 10, 12, 14}`
  - stronger strict-path YouTube reports such as `report_id in {8, 11, 13, 15}`
  - stored titles/keywords for English-learning and engineering/science niches

## Cases used for tuning

### 1. English-learning strict-path fixture (`report_id=12` lineage)

- Local stored keywords are strongly English-learning oriented.
- Replay-style inspection showed a negative-adaptation item (`Pronunciation hack - temporary`) still sitting too high on base virality alone.
- Raising `ADAPTATION_RELEVANCE_WEIGHT` to `2.0` moved clearly on-niche, high-adaptation items above that weaker-fit item without needing a new algorithmic block.

### 2. English-learning thin/zero lineage (`report_id in {7, 8, 12}`)

- Local DB contains zero/thin reports for the same general niche cluster.
- This PR does not claim exact ex post recovery for those historical outputs.
- The tuning keeps fallback available but raises its quality bar so recall improvements come from clearly relevant low-delta items, not weak filler.

### 3. Engineering/science strict-path fixture (`report_id=15` lineage)

- Local stored report payload shows a strong strict-path case with enough YouTube items.
- This case was used as a non-regression guard:
  - strict path should remain dominant
  - fallback should not activate into noisy spam when the platform is already healthy

### 4. Controlled fallback quality fixtures

- Replay-style tests use stored/live-like English-learning titles and low-delta conditions.
- Tuning raised fallback quality requirements to:
  - require stronger adaptation relevance (`0.45`)
  - require a less permissive relaxed delta ratio (`0.5`)

## Trade-offs still left

- These defaults are improved for the current local fixtures, not “final” or “perfect”.
- The local DB cannot prove what would have happened on the exact prod `report_id=69` run.
- Further tuning may still be needed after more live reports accumulate, especially for neutral-but-not-obviously-off-topic viral items.

## What improved on current fixtures

- Clearly on-niche instructional items now beat more viral but weak-fit items in the English-learning fixture lineages.
- Fallback remains available for thin platforms, but only for items that clear a stronger adaptation bar and a less permissive relaxed delta threshold.
- Healthy strict-path cases keep their strict winners without forcing extra fallback fill.
