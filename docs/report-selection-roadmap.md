# Report Selection Roadmap

## Execution Standard

- Merge without deploy and live verification is not a completed rollout.
- For any merged production-affecting PR, done means `merge -> deploy -> verify live SHA -> smoke check -> truth report`.
- See [docs/post-merge-deploy-verify.md](./post-merge-deploy-verify.md).

## Public Forensic Summary

Historical production investigations showed that an empty YouTube section can be caused by valid selection filters rather than a crash.

Typical causes include:

- competitors skipped by recent-content gates
- refreshed content falling outside the configured age window
- items below `MIN_VIEWS_END`
- items below `MIN_DELTA_VIEWS`
- items removed by already-reported filters
- items removed by user stopwords

Public docs should not include private report IDs, Telegram IDs, server hosts, database snapshots, or deployment records. Keep incident-specific data in private operational notes.

## A. Global Roadmap

Keep the work split into narrow PRs. Do not mix selection tuning, diagnostics, and relevance logic into one rollout.

### Architectural position: competitor-based core, video-first only as a supplemental lane

- We are not planning a full transition from the current competitor-based model to direct topic-video search.
- The main model remains competitor-based tracking with baseline-aware scoring.
- A video-first flow is considered only as a supplemental lane.
- The supplemental lane should find new video ideas, identify potential competitors, and improve recall when the main competitor lane produces too few strong candidates.
- The supplemental lane should not become the sole source for regular reports in its first iterations.

### Why the roadmap stays hybrid

- A hybrid lets us test a video-first source safely and incrementally.
- If the supplemental lane is weak, the core competitor-based report still works.
- If the supplemental lane is strong, it adds expansion and discovery value.
- This reduces the risk of a full cutover onto an immature video-first ranking stack.

### PR 1. Diagnostics and truthful empty-section reasons

- Persist a section-level diagnostic summary into `Report.payload` for each platform:
  - gate-skipped competitors
  - refreshed item counts
  - drop counts by age, min views, delta, already reported, and stopwords
- Render truthful empty-section explanations in Telegram instead of only `Нет подходящих роликов`.
- Truncate long titles in report text so diagnostics stay readable.

### PR 2. Candidate age-window tuning

- Evaluate whether the default report candidate age window should stay broad.
- Keep the existing recency bonus in scoring so newer items still rank above older items.
- Verify with live-safe fixtures that wider windows do not flood reports with stale content.

### PR 3. Deeper YouTube Shorts fetch

- Fetch deeper than the first page of uploads when the initial page does not produce enough usable Shorts.
- Persist diagnostics for:
  - upload pages scanned
  - total uploads inspected
  - usable Shorts found
  - age distribution of found Shorts
- Keep this PR scoped to fetch depth only.

### PR 4. Deterministic adaptation-relevance layer

- Add a deterministic relevance layer before final ranking.
- Inputs:
  - normalized seed metadata
  - linked-account metadata
  - competitor/channel/profile metadata
  - content title and description
  - lightweight lexical features
- Candidate signals:
  - weighted keyword overlap
  - curated niche lexicons and blacklist terms
  - topic phrase normalization
  - handle, bio, and title affinity
  - language or subject mismatch penalties
  - platform-specific content-type penalties

High precision is achievable for stable niches with explicit vocabulary and clear negative signals. A deterministic layer is not a perfect semantic judge, so it should be paired with diagnostics and controlled fallback behavior.

### PR 5. Soft fallback layer for weaker-but-relevant items

- Add a clearly labeled fallback path when a platform would otherwise be empty.
- Candidate rules:
  - only after relevance filtering
  - only if the strict path yields zero items
  - weaker delta allowed
  - minimum view floor retained
  - fallback origin recorded in diagnostics

### PR 6. Threshold tuning using live-safe fixtures

- Build fixture sets from sanitized report payloads and stored snapshots.
- Evaluate and tune:
  - `MIN_VIEWS_END`
  - effective delta floor
  - fallback entry conditions
  - recency bonus weight
- Keep this PR data-driven and separate from fetch-depth and relevance work.

## B. Future Exploration: YouTube supplemental topic-video lane

- First step is YouTube only.
- This is not a replacement for the competitor lane.
- Treat it as an additional discovery/source layer, not as the main ranking source in early iterations.
- Intended use cases:
  - surface new video ideas
  - surface new potential competitors first noticed through videos
- Initial rollout should be experimental and diagnostic:
  - log how often the lane finds useful candidates
  - compare overlap against competitor-lane outputs
  - keep output clearly separated from the core report path until it proves value

### Candidate promotion flow

`video-discovered creators -> suggested competitors / add-to-tracking`

- If the supplemental lane repeatedly surfaces useful videos from the same creator, the system can suggest that creator as a new competitor.
- Start with suggestion/recommendation only.
- Do not auto-add video-discovered creators to tracking in the first iterations.
