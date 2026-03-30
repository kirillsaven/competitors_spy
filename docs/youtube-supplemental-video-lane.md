# YouTube Supplemental Topic-Video Lane

## Status

- Scope: design / PRD only
- Runtime impact in this PR: none
- Target: future experimental YouTube-only supplemental lane

## A. Goal

The supplemental topic-video lane exists to add a second, narrower source of YouTube ideas without replacing the current competitor-based report path.

Its goals are:

- find additional YouTube videos relevant to the user's niche
- surface new content ideas the competitor lane may miss
- surface potential new competitors first noticed through their videos
- improve recall when the main YouTube competitor lane is thin or empty

## B. What We Are Not Doing

This lane is intentionally limited.

- We are not replacing the current competitor-based regular report.
- We are not doing a full migration to a video-first product model.
- We are not auto-adding discovered creators as competitors.
- We are not claiming perfect semantic matching.
- We are not making this the main ranking source in the first iterations.

## C. Minimal Product Shape

The first product shape should stay small and diagnostic.

- platform scope: YouTube only
- output scope: supplemental section and supplemental payload only
- ranking scope: separate from the main competitor-based ranking
- initial purpose: experiment / diagnostics / discovery

Recommended first shape:

- keep the existing main YouTube section unchanged
- append a separate supplemental section only when the lane is enabled
- mark every supplemental item as `source=supplemental_topic_video`
- store the full supplemental lane output in payload even if Telegram initially renders only a small capped subset

This separation matters because it lets us evaluate the lane honestly before it influences the main report.

## D. Candidate Source Design

### 1. Query construction from existing niche context

Build search queries only from signals already available in the product.

Available inputs:

- `niche_keywords`
- normalized keyword stems / phrases
- linked account metadata
- active competitor metadata
- repeated themes already observed in recent successful report items

Recommended query construction:

- start from the top niche phrases, not raw bag-of-words
- generate a small bounded query set:
  - exact phrase queries
  - phrase + format queries such as `shorts`, `lesson`, `tips`, `mistakes`, `examples`
  - phrase variants using normalized stems or known lexical alternates
- optionally add one competitor-informed query family when competitor metadata strongly reinforces a theme

Guardrails:

- cap the total query count per run
- avoid exploding combinations
- cache query results by normalized query + time bucket

### 2. Candidate collection

Initial candidate collection can use YouTube search and then hydrate the returned videos.

Proposed steps:

1. build a bounded query list from current niche context
2. run YouTube search for videos, preferring recent and short-form-friendly results
3. hydrate video metadata for scoring
4. optionally hydrate channel metadata only for authors that survive first-pass filtering

### 3. Deduplication

Dedup early and deterministically.

- dedup by `video_id`
- collapse duplicate hits from multiple queries into one candidate row
- retain provenance:
  - matched queries
  - matched phrases
  - first seen rank
  - hit count across queries

Optional later:

- light author-level dedup heuristics for “same creator, many near-duplicate uploads” when the same format floods the pool

### 4. Junk filtering

Before ranking, remove obvious low-value candidates.

- non-short-form or obviously long-form items if the experiment is Shorts-first
- missing metrics or missing publication time
- obvious off-topic terms
- blocked terms / stopwords if the user already configured them
- videos older than the supplemental lane freshness window
- duplicate items already present in the main report candidate set

### 5. Author hydration / channel lookup

Author hydration should be staged, not mandatory for the first collection pass.

Use it only when:

- a video survives first-pass filtering
- author metadata is needed for better ranking
- author repeat frequency matters for promotion suggestions

Useful author fields:

- channel id
- channel title / handle
- channel description
- basic recent activity signals if cheaply available

### 6. Promotion path into suggested competitors

Promotion should be suggestion-only.

Recommended rule:

- if one creator repeatedly appears in high-value supplemental candidates across multiple runs
- and those candidates are consistently relevant
- then emit a `suggested competitor` recommendation

Do not auto-add.

## E. Ranking Design

The ranking should be deterministic-first and explainable.

### Ranking components

Each candidate should combine:

- viral / traction signals
- topic relevance
- adaptation usefulness
- freshness
- off-topic penalties
- optional author-level context if available

### 1. Viral / traction signals

Use bounded, simple signals:

- views
- recent velocity if available
- likes/comments ratio when available
- search-position hints only as a weak secondary signal

These should help find interesting videos, but not dominate the ranking.

### 2. Topic relevance

Use deterministic lexical and phrase-based matching against:

- niche keywords
- normalized stems
- linked account metadata
- competitor metadata

Boost:

- exact phrase matches
- repeated topic clusters
- consistent overlap across title, description, and author metadata

### 3. Adaptation usefulness

This should prefer videos the user can plausibly adapt.

Boost examples:

- instructional markers
- list / mistakes / examples / template / walkthrough style
- clear audience-fit topics
- repeated winning formats already seen in the user's niche

Penalty examples:

- celebrity / prank / gossip / unrelated entertainment
- generic viral clips with weak topical fit
- broad trend bait with no clear adaptation value

### 4. Freshness

Freshness should be a bounded positive factor, not a hard guarantee of rank.

- newer videos should generally win over stale ones if other signals are similar
- freshness should not overpower a clear quality or relevance gap

### 5. Off-topic penalties

This lane needs strong off-topic penalties because topic-video search increases noise risk.

Penalties should fire on:

- obvious subject mismatch
- language mismatch when context is strong
- low-adaptation-value trendbait
- weak topical match combined with generic viral terms

### 6. Optional author context

When author metadata exists, use it conservatively.

Possible author boosts:

- channel metadata aligns with the niche
- the same creator produced multiple relevant supplemental candidates

Possible author penalties:

- creator appears broad/noisy across unrelated topics
- creator metadata strongly conflicts with user niche

## F. Quota, Cost, and Risk

### Why YouTube first

YouTube is the best first pilot because:

- we already have a working YouTube integration
- APIs and metadata are more structured than Instagram/TikTok
- channel and video identifiers are cleaner for dedup and promotion logic
- search + hydration can be reasoned about more explicitly

### Main quota risks

The main quota risks are:

- too many search queries per user per run
- over-hydrating low-quality search results
- repeated author/channel lookups for candidates that never matter

### Search budget guardrails

The first pilot should stay tightly bounded.

Recommended guardrails:

- max topic queries per run
- max search results per query
- max hydrated video candidates per run
- max hydrated authors per run
- shared cache by normalized query and recency bucket
- early stop once enough high-confidence supplemental candidates exist

### Additional risk guardrails

- lane is feature-flagged
- lane output is isolated from the main ranking
- diagnostics are always persisted
- per-run provenance is stored:
  - query used
  - why candidate survived
  - why candidate ranked high
- rollout starts with a small user subset or internal-only preview

## G. Suggested Rollout in Small PRs

Keep the rollout narrow and incremental.

### PR 1. Docs / PRD

- add this document
- no runtime changes

### PR 2. Topic-video candidate collection

- add query builder
- add bounded YouTube search collection
- add hydration and dedup
- add diagnostics for query count, hits, hydrated candidates, and dedup drops

### PR 3. Supplemental payload section

- persist a separate supplemental YouTube payload block
- optionally render a small diagnostic/supplemental section in Telegram
- keep it separate from the main YouTube ranking

### PR 4. Deterministic video relevance ranking

- add deterministic ranking across traction, topic relevance, adaptation usefulness, freshness, and penalties
- persist per-item ranking factors for investigation

### PR 5. Candidate promotion suggestions

- detect repeated useful creators
- emit `suggested competitor` recommendations only
- no auto-add behavior

### PR 6. Live evaluation / tuning

- evaluate real supplemental outputs
- tune query families, ranking weights, and promotion thresholds
- keep this data-driven

## H. Success Criteria

The lane is useful only if it improves recall without turning into noise.

Primary success criteria:

- more useful content ideas than the competitor lane alone
- fewer thin or empty YouTube outcomes
- acceptable off-topic rate
- repeated useful creators can be surfaced as competitor suggestions

Suggested evaluation questions:

- how often does the supplemental lane add at least one clearly useful idea?
- how often is the top supplemental item on-topic and adaptable?
- how often does the lane add only noise?
- how often do repeated creators from the lane look worth suggesting as competitors?

## Recommended First Experiment

The first experiment should stay deliberately conservative.

- enable the lane only for YouTube
- run it only as a separate supplemental payload / section
- cap it to a small query budget and a small number of returned items
- compare its outputs against the main competitor lane instead of mixing them
- use the first iteration to measure signal quality and repeated useful creators before any promotion logic is exposed to users
