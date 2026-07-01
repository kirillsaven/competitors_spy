# ProAudit: OSS Growth Readiness

Date: 2026-07-01
Repository: `kirillsaven/competitors_spy`
Branch: `codex/oss-growth-readiness`
Base reviewed commit: `14e1ee0`
Post-publication evidence location: PR #80 metadata and comments
Audience: external Pro/model reviewer and project maintainer

## Verdict

Status: `PASS_WITH_CAVEATS`

The repository has been strengthened for public OSS review and Codex for OSS follow-up: the README now explains the product and value, GitHub metadata is populated, starter issues and labels are in place, Docker image publishing is wired, and a promotion kit/demo path exists.

Caveats:

- No external promotion post was sent from the user's accounts. The repo is prepared for ethical promotion, but stars cannot be guaranteed or manufactured.
- Current public social proof remains `0` stars, `0` forks, `0` watchers at audit time.
- GHCR publishing will only create packages after the Docker workflow is merged and a new `main` push or `v*` tag runs.
- `gitleaks` and `trufflehog` were not installed locally; safety evidence is from targeted `rg` marker scans plus existing repo policy/tests.
- A single monolithic `pytest -q` run timed out at 10 minutes because the suite is slow. The same suite was then run by directory segments and passed.
- Exact final PR head SHA and GitHub Actions conclusions are recorded externally in PR #80 metadata/comments, not inside this committed file.

## Requirement Closure Matrix

| Requirement | Status | Evidence |
| --- | --- | --- |
| Keep repository public and reviewable | PASS | GitHub repo visibility verified as `PUBLIC`. |
| Add strong GitHub description/homepage/topics | PASS | Description, README homepage, and 14 topics set through `gh repo edit`. |
| Improve README first impression | PASS | Added problem framing, demo links, Docker image note, and star CTA in `README.md`. |
| Add normal roadmap | PASS | `ROADMAP.md` now separates provider hardening, demo/report quality, AI-assisted reports, and maintainer automation. |
| Add demo script | PASS | Added `docs/demo-script.md`. |
| Add promotion material | PASS | Added `docs/promotion-kit.md` with copy and ethical promotion rules. |
| Add public launch checklist | PASS | Added `docs/public-launch-checklist.md`. |
| Add labels | PASS | Added `.github/labels.yml`; labels were also created/updated on GitHub. |
| Add good starter issues | PASS | Public issues #75, #76, #77, #78, #79 opened and labelled. |
| Add release notes | PASS | Added `[Unreleased]` section to `CHANGELOG.md`. |
| Add Docker image path | PASS | Added `.github/workflows/docker-image.yml`; local Docker build passed. |
| Keep public docs safe | PASS_WITH_CAVEAT | Targeted marker scan found no exact old private markers. External secret scanners unavailable locally. |
| Sync work to GitHub | PASS_EXTERNAL | Branch publication is tracked in PR #80. Exact final PR/head evidence is recorded in PR metadata/comments because a commit cannot contain its own final SHA. |

## GitHub State

Repository metadata verified with:

```text
gh repo view kirillsaven/competitors_spy --json description,homepageUrl,repositoryTopics,visibility,stargazerCount,forkCount,watchers
```

Observed state:

- visibility: `PUBLIC`
- description: `Self-hosted Telegram bot for competitor content trend reports across YouTube, TikTok, and Instagram.`
- homepage: `https://github.com/kirillsaven/competitors_spy#readme`
- topics: `apify`, `celery`, `competitor-analysis`, `content-intelligence`, `django`, `docker-compose`, `instagram`, `postgres`, `redis`, `self-hosted`, `social-media-analytics`, `telegram-bot`, `tiktok`, `youtube-api`
- stars/forks/watchers: `0/0/0`

Starter issues:

- #75 `Add a sanitized sample Telegram report payload`
- #76 `Document provider diagnostics for empty report sections`
- #77 `Add synthetic Instagram provider parsing fixtures`
- #78 `Add GHCR Docker image usage notes`
- #79 `Improve Russian report copy for empty platform sections`

Publication:

- PR: `https://github.com/kirillsaven/competitors_spy/pull/80`
- Final exact head SHA and GitHub Actions conclusions: PR #80 metadata/comments.

## Verification Evidence

Commands run and outcome:

```text
git diff --check
PASS
```

```text
PowerShell here-string piped to python:
import yaml
for path in ['.github/workflows/docker-image.yml', '.github/labels.yml']:
    yaml.safe_load(open(path, encoding='utf-8'))
PASS
```

```text
python manage.py check
PASS: System check identified no issues (0 silenced).
```

```text
pytest tests/test_maintainer_ai.py tracking/tests/test_provider_config.py tracking/tests/test_youtube_parsing.py tracking/tests/test_time.py tracking/tests/test_scoring.py -q
PASS: 45 passed
```

```text
pytest botapp -q --durations=10
PASS: 83 passed
```

```text
pytest tests -q --durations=10
PASS: 13 passed
```

```text
pytest tracking/tests -q --durations=15
PASS: 269 passed
```

```text
docker compose config --quiet
PASS
```

```text
docker build --pull=false -t competitors_spy:oss-growth-check .
PASS
```

Temporary image cleanup:

```text
docker image rm competitors_spy:oss-growth-check
PASS
```

## Test Notes

The first monolithic `pytest -q` run timed out after 10 minutes. A later `tracking/tests` run with `YOUTUBE_API_KEY='test-youtube-key'` failed 7 `test_report_pipeline` cases because the fake key enabled real YouTube supplemental calls and Google returned `API_KEY_INVALID`. Re-running the same failed cases with empty `YOUTUBE_API_KEY` and `YOUTUBE_API_KEYS` passed, and the full `tracking/tests` segment also passed with empty keys.

This means the local evidence is green under the offline CI-style env. It also exposes a useful maintenance note: full test runtime is long, and the report pipeline tests are sensitive to fake non-empty YouTube keys.

## Security Sweep

Targeted changed-file scan:

```text
rg -n "<known old private path marker>|<known ssh target marker>|<known container marker>|<numeric IP patterns>" <changed files>
```

Outcome: no matches.

Broader secret-like scan across the repo produced expected references to placeholder `.env.example` values, docs warnings, tests, and redaction code. It did not identify a new real secret in the files changed for this PR.

## Promotion Position

Done:

- GitHub description/homepage/topics populated.
- Beginner-friendly issues open and labelled.
- Promotion copy centralized in `docs/promotion-kit.md`.
- README now includes a restrained star CTA.
- Demo script and launch checklist are available for a short video, community post, or OSS-program reviewer.

Not done:

- No posts were submitted to X, Reddit, Hacker News, Telegram, Discord, email, or other external destinations.
- No stars were bought, traded, or requested through fake accounts.

Recommended next promotion actions:

1. Record a short demo using `docs/demo-script.md`.
2. Post once from the maintainer's real account using `docs/promotion-kit.md`.
3. Ask relevant developer peers for feedback first, stars second.
4. Keep issues #75-#79 responsive after sharing.

## Final Reviewer Instructions

Review the pushed PR branch, not only this file. Confirm:

- README and docs truthfully represent the current project.
- The Docker workflow is safe for PRs and only pushes on non-PR events.
- The public issues are useful and not spam.
- No changed file contains private operational markers or real credentials.
- CI/PR checks match the local evidence above.
