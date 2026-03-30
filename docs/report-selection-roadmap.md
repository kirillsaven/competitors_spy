# Report Selection Roadmap

## A. Forensic Note: YouTube-zero report on 2026-03-30

### Target report

- Server: `YOUR_SERVER_IP`
- Report: `report_id=69`
- User: `user_id=33`
- Timezone: `Asia/Bangkok`
- Report period in payload:
  - local: `2026-03-29 22:05:59` -> `2026-03-30 12:00:22`
  - UTC: `2026-03-29T15:05:59.521875Z` -> `2026-03-30T05:00:22.877291Z`
- Payload fact:
  - `payload.sections[youtube].items == []`
  - there was no YouTube section note in the stored payload

### What was checked

- `tracking_report.id=69` payload in prod DB
- worker logs for `2026-03-30 05:00Z` around the scheduled run
- current `UserCompetitor` rows for `user_id=33`
- all YouTube `ContentItem` + `MetricSnapshot` rows captured at `period_end=2026-03-30T05:00:22.877291Z`
- prior sent reports for the same user to test `already_reported`
- prod settings used by this run:
  - `YT_RECENT_N_FOR_METRICS=50`
  - `REPORT_MAX_ITEM_AGE_DAYS=14`
  - `MIN_VIEWS_END=1000`
  - `MIN_DELTA_VIEWS=500`
  - `REPORT_SHORT_WINDOW_FALLBACK_HOURS` unset, so effective fallback window was `6h`

### Facts and limits

- The user had `9` active YouTube competitors at report time.
- We do not have a dedicated historical snapshot of the active competitor list inside `Report.payload` or `JobRun.payload`.
- For this specific report that gap did not change the conclusion:
  - all current YouTube `UserCompetitor` rows for `user_id=33` have `updated_at <= 2026-03-28T15:01:38.953010Z`
  - that is before `report_id=69.period_start`
  - no post-report YouTube link mutation exists in DB for this user
- Exact `playlistItems.list` return counts per competitor were not persisted or logged.
  - We only know the configured request cap was `50`.
- Exact 60-day recent-short counts were logged only for competitors rejected by the recent-shorts gate.
  - For competitors that passed the gate, the exact gate count was not logged.

### Per-competitor YouTube breakdown

The table below uses only stored DB rows for `report_id=69.period_end` plus worker logs for the gate rejections.

| competitor_id | handle | recent-shorts gate at runtime | persisted short items refreshed at `period_end` | dropped by age window (14d) | dropped by min views | dropped by delta threshold | dropped by already reported | dropped by stopwords | final YouTube items |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 118 | `miss.alex.english` | passed, exact count not logged | 50 | 50 | 0 | 0 | 0 | 0 | 0 |
| 119 | `ann_theteacher` | passed, exact count not logged | 50 | 50 | 0 | 0 | 0 | 0 | 0 |
| 132 | `romans_english` | passed, exact count not logged | 26 | 19 | 1 | 6 | 0 | 0 | 0 |
| 159 | `english.nochevkina` | failed, log says `recent_shorts=1` | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 138 | `englishgalaxy` | passed, exact count not logged | 15 | 15 | 0 | 0 | 0 | 0 | 0 |
| 167 | `darya_sinclair` | failed, log says `recent_shorts=1` | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 189 | `english_kris` | failed, log says `recent_shorts=0` | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 142 | `oxanadolinka` | passed, exact count not logged | 39 | 39 | 0 | 0 | 0 | 0 | 0 |
| 147 | `englishclub.` | passed, exact count not logged | 27 | 27 | 0 | 0 | 0 | 0 | 0 |

### Aggregate YouTube funnel for this report

- active YouTube competitors: `9`
- competitors rejected by the recent-shorts gate before refresh: `3`
- competitors refreshed for report preview: `6`
- persisted short-form YouTube items refreshed at `period_end`: `207`
- dropped by age window (`published_at < period_end - 14d`): `200`
- dropped by `MIN_VIEWS_END=1000`: `1`
- dropped by delta threshold: `6`
- reached post-scoring filters (`already_reported` / `stopwords`): `0`
- dropped by `already_reported`: `0`
- dropped by `stopwords`: `0`
- final YouTube items in payload: `0`

### What the logs show

Worker logs for the exact run contain these relevant facts:

- scheduled run started for `user_id=33` at `2026-03-30T05:00:22Z`
- three YouTube competitors were skipped by the recent-shorts gate:
  - `competitor_id=159`, `external_id=UCExkka-jLjpVQAyUyzVOQ-Q`, `recent_shorts=1`
  - `competitor_id=167`, `external_id=UCli5tNAT05BrqOsCgrnM81A`, `recent_shorts=1`
  - `competitor_id=189`, `external_id=UCXDnYcrvsZePyL1u10RTTPA`, `recent_shorts=0`
- logs also show `Excluded previously reported items for user_id=33 count=9`
  - DB replay shows those `9` exclusions were Instagram items, not YouTube items
  - for YouTube on this report, nothing reached the `already_reported` filter at all

### Exact root cause

This YouTube-zero section was caused by selection filters on valid data, not by a runtime crash and not by the `already_reported` filter.

The exact chain was:

1. `3/9` active YouTube competitors were rejected before refresh by the `>=2 recent Shorts in 60 days` gate.
2. The remaining `6` YouTube competitors did refresh short-form items successfully.
3. Of the `207` refreshed short items:
   - `200` were older than the current `14d` report age window
   - `1` was newer than `14d` but below `MIN_VIEWS_END=1000`
   - `6` were newer than `14d` and above the view floor, but were dropped by the delta threshold
4. No YouTube item reached `already_reported` or `stopwords`.
5. The stored payload therefore had `youtube.items=[]`, and because no section-level diagnostic note was attached, the user only saw the generic empty-section text.

### Would age window `14d -> 60d` fix this exact report?

No.

Replay on the same stored `ContentItem`/`MetricSnapshot` set with only `REPORT_MAX_ITEM_AGE_DAYS=60` changed the YouTube funnel to:

- dropped by age window: `158`
- dropped by min views: `8`
- dropped by delta threshold: `41`
- final YouTube items: `0`

So a pure age-window bump to `60d` does not populate YouTube for `report_id=69`.

### Would deeper YouTube Shorts fetch fix this exact report?

Not provable from stored artifacts.

What we know:

- the run used `YT_RECENT_N_FOR_METRICS=50`
- exact per-competitor upload counts returned by `playlistItems.list` were not logged
- non-short uploads were not persisted

Because of that, we cannot prove ex post whether fetching deeper than the first `50` uploads would have exposed additional Shorts that were both:

- still inside the report age window, and
- strong enough to survive the existing `min views` + `delta threshold` filters

So the correct answer for this report is: **unknown from current logs/DB/payload**.

### Which changes would fix this exact case?

Verified on the stored March 30 snapshot:

- A truthful diagnostic note for the empty YouTube section would explain this case correctly.
  - It would not add YouTube items, but it would remove the misleading generic empty section.
- A soft fallback that does not hard-drop all low-delta items would populate YouTube for this exact report.
  - Replay on the same stored rows with the current `14d` age window, `MIN_VIEWS_END=1000`, and without the hard delta drop yields `3` YouTube candidates.
- Threshold tuning can also fix this exact case.
  - The stored data proves there are `3` YouTube items inside the current `14d` window that fail only on the delta threshold.

### Which changes would not fix this exact case, or would only help partially?

- `REPORT_MAX_ITEM_AGE_DAYS=60` alone does **not** fix this report.
- The current `already_reported` logic did **not** cause this YouTube-zero report.
- Stopwords did **not** cause this YouTube-zero report.
- Deeper fetch may help in general, but for this exact report we do not have enough stored evidence to claim that it would.

## B. Global Roadmap

Keep the work split into narrow PRs. Do not mix selection tuning, diagnostics, and relevance logic into one rollout.

### PR 1. Forensic + roadmap

- Add this investigation note and roadmap.
- Do not change product behavior.

### PR 2. Diagnostics + truthful empty-section reasons + title truncation

- Persist a section-level diagnostic summary into `Report.payload` for each platform:
  - gate-skipped competitors
  - refreshed item counts
  - drop counts by age/min views/delta/already reported/stopwords
- Render truthful empty-section explanations in Telegram instead of only `Нет подходящих роликов`.
- Truncate long titles in report text more aggressively so diagnostics stay readable.

### PR 3. Expand candidate age window to 60d + keep recency bonus

- Change report candidate age window from `14d` to `60d`.
- Keep the existing recency bonus in scoring so newer items still rank above older ones.
- Verify with live fixtures that the wider window does not flood reports with stale content.

### PR 4. Deeper YouTube Shorts fetch

- Fetch deeper than the first `50` uploads when the first page does not produce enough usable Shorts.
- Persist enough diagnostics to know:
  - uploads pages scanned
  - total uploads inspected
  - usable Shorts found
  - age distribution of found Shorts
- Keep this PR scoped to fetch depth only.

### PR 5. Deterministic adaptation-relevance layer (no LLM)

- Add a deterministic relevance layer before final ranking.
- Inputs:
  - normalized seed metadata
  - linked-account metadata
  - competitor/channel/profile metadata
  - content title + description + lightweight lexical features
- Candidate signals:
  - weighted keyword overlap
  - curated niche lexicons and blacklist terms
  - topic phrase normalization
  - handle/bio/title affinity
  - language/subject mismatch penalties
  - platform-specific content-type penalties

#### Honest quality assessment for a non-LLM adaptation layer

- High precision is achievable for stable niches with explicit vocabulary and clear negative signals.
  - Examples: English learning, exam prep, fitness, recipes, marketing tools.
- It will work well for:
  - direct topical relevance
  - obvious off-topic rejection
  - language mismatch detection
  - explicit adaptation hooks like exam names, grammar topics, platform format markers
- It will work poorly for:
  - subtle “same audience but different wording” matches
  - metaphorical or highly creative content
  - implicit style adaptation
  - broad lifestyle/creator channels with sparse metadata
- Conclusion:
  - a deterministic layer can be strong enough as a precision-first filter and rank feature
  - it is not enough to guarantee high recall on ambiguous long-tail content
  - it should be paired with diagnostics and a controlled soft fallback, not treated as a perfect semantic judge

### PR 6. Soft fallback layer for weaker-but-relevant items

- Add a clearly labeled fallback path when a platform would otherwise be empty.
- Candidate rules:
  - only after relevance filtering
  - only if hard path yields zero items
  - weaker delta allowed
  - keep a minimum view floor
  - label items as fallback-origin in diagnostics
- This is the smallest product change that is already supported by the March 30 YouTube-zero forensic:
  - stored data for `report_id=69` shows that a softer delta policy would have yielded `3` YouTube items even with the current `14d` window

### PR 7. Threshold tuning using live fixtures

- Build a fixture set from real reports and stored snapshots.
- Evaluate and tune:
  - `MIN_VIEWS_END`
  - effective delta floor
  - fallback entry conditions
  - recency bonus weight
- Keep this PR data-driven and separate from fetch-depth/relevance PRs.
