# ProAudit: Competitor Spy Public OSS Readiness

Date: 2026-06-16

Repository: `kirillsaven/competitors_spy`

Branch: `chore/public-oss-readiness`

Head commit: `4003b77325645218036839abd383cab4045f360f`

Draft PR: https://github.com/kirillsaven/competitors_spy/pull/67

Base branch: `main`

Base commit at PR creation: `a7fc836c36f33b5df900ccd18733fc7b491a912f`

## Reviewer Mission

Review Codex's public open-source release preparation work as a strict maintainer/security/code-review audit.

Do not assume public adoption, users, stars, downloads, production scale, or affiliation with OpenAI, YouTube, TikTok, Instagram, Telegram, or Apify. Treat the project as a real private codebase being prepared for a first public OSS baseline.

## User Goal

Prepare the existing private repository `kirillsaven/competitors_spy` for a serious public open-source release and for a truthful OpenAI Codex for Open Source application.

Key constraints:

- Use the existing repo, not a new repo.
- Do not fabricate adoption or impact.
- Do not publish secrets, private server details, Telegram IDs, message IDs, API keys, or deployment records.
- Do not make the repo public until safety checks are complete.
- Do not submit the OpenAI form with placeholders.
- Core app must not require OpenAI.
- Any AI maintainer automation must redact secrets and have deterministic fallback behavior.

## Current PR Scope

The PR contains one Codex-authored commit:

```text
4003b77 chore: prepare public open-source release
```

Changed files relative to `origin/main`:

```text
.dockerignore
.env.example
.github/ISSUE_TEMPLATE/bug_report.yml
.github/ISSUE_TEMPLATE/feature_request.yml
.github/dependabot.yml
.github/pull_request_template.md
.github/workflows/maintainer-ai-example.yml.disabled
.github/workflows/release.yml
.gitignore
CHANGELOG.md
CODE_OF_CONDUCT.md
CONTRIBUTING.md
DEPLOY.md
LICENSE
README.md
ROADMAP.md
SECURITY.md
deploy/env.production.example
deploy/nginx/default.conf
docs/post-merge-deploy-verify.md
docs/report-selection-roadmap.md
scripts/post_merge_deploy_verify.ps1
tests/test_maintainer_ai.py
tools/README.md
tools/maintainer_ai.py
tracking/tests/test_platform_onboarding.py
```

## What Codex Changed

### Public OSS docs

- Rewrote `README.md` for public OSS positioning, architecture, Docker quick start, provider setup, security model, limitations, and public release safety.
- Rewrote `DEPLOY.md` as a generic self-hosting deployment template.
- Added `LICENSE` with MIT license for Kirill Saven.
- Added `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `CHANGELOG.md`, and `ROADMAP.md`.
- Added GitHub issue templates, PR template, Dependabot config, and release workflow.

### Security cleanup

- Generalized private deployment host details to placeholders such as `YOUR_SERVER_IP`, `YOUR_DOMAIN`, and `YOUR_APP_DIR`.
- Removed the private first-deploy record from public deployment docs.
- Replaced production env template host values with placeholders.
- Replaced Nginx `server_name` with `YOUR_DOMAIN`.
- Replaced default server/remote dir in `scripts/post_merge_deploy_verify.ps1` with placeholders.
- Tightened `.gitignore` and `.dockerignore` to exclude env files, logs, sqlite DBs, media/static output, dumps, SQL exports, archives, and local coverage artifacts.

### Optional maintainer AI tooling

Added:

- `tools/maintainer_ai.py`
- `tools/README.md`
- `tests/test_maintainer_ai.py`
- `.github/workflows/maintainer-ai-example.yml.disabled`

Behavior:

- Without `OPENAI_API_KEY`, it uses deterministic fallback output.
- With `OPENAI_API_KEY`, it lazy-imports `openai`.
- It refuses raw `.env` inputs.
- It refuses to send diffs containing `.env` changes to OpenAI.
- It redacts likely API keys, bot tokens, database URLs, JSON secret fields, env assignments, and private keys before model calls.
- The example workflow is disabled by filename and does not auto-post AI comments.

### Test fixture repair

Updated one stale date-sensitive fixture in `tracking/tests/test_platform_onboarding.py`.

Reason: hardcoded March 2026 social post timestamps were no longer recent on 2026-06-16, causing a candidate-collectibility test to fail for date drift rather than product behavior. The test now uses `datetime.now(UTC) - timedelta(...)` for recent/old fixture timestamps.

## Verification Evidence

Local commands run:

```text
python -m pip install -r requirements.txt
python manage.py check
python manage.py check --deploy --fail-level WARNING
pytest tests/test_maintainer_ai.py -q
pytest tracking/tests/test_platform_onboarding.py -q
pytest tests/test_maintainer_ai.py tracking/tests/test_youtube_parsing.py tracking/tests/test_time.py tracking/tests/test_scoring.py -q
pytest -q
git diff --check
```

Results:

- `python -m pip install -r requirements.txt`: passed; dependencies already installed; `timezonefinder` skipped on Windows due marker.
- `python manage.py check`: passed.
- `python manage.py check --deploy --fail-level WARNING`: failed under default local dev env, then passed with CI-style production security env vars.
- `pytest tests/test_maintainer_ai.py -q`: passed, 7 tests.
- `pytest tracking/tests/test_platform_onboarding.py -q`: passed after date-relative fixture repair.
- focused maintainer/parsing/time/scoring slice: passed, 33 tests.
- `pytest -q`: passed full suite.
- `git diff --check`: passed.
- GitHub Actions CI on PR #67: completed successfully.

Docker:

```text
docker compose version
docker compose build
```

Result:

- Docker Compose exists: `v5.0.0-desktop.1`.
- `docker compose build` could not run because Docker Desktop Linux engine was not reachable:
  `open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified`.

Secret/current-file scan:

- `gitleaks` was not installed.
- A targeted `rg` scan for previously found private values returned no hits:
  - real server IP that had appeared in docs
  - real Telegram user/chat ID that had appeared in smoke-test docs
  - private container name from old deployment record
  - private root SSH target
  - old Telegram message ID line
  - old private app directory path
- `git ls-files .env db.sqlite3 celerybeat-schedule` returned empty.

## GitHub Sync Evidence

Branch pushed:

```text
origin/chore/public-oss-readiness
```

Draft PR:

```text
https://github.com/kirillsaven/competitors_spy/pull/67
```

PR state at audit-file creation:

- open
- draft
- mergeable
- CI success
- repository visibility still `private`

## Known Blockers Before Public Release

- Repository is still private.
- PR #67 is not merged.
- No release tag exists.
- Docker build/checks were not run because Docker Desktop engine was unavailable locally.
- `gitleaks` scan was not run because `gitleaks` was unavailable locally.
- Current-file cleanup does not rewrite git history.
- Applicant placeholders are still missing for the OpenAI form:
  - first name
  - last name
  - ChatGPT-linked email
  - OpenAI organization ID

## OpenAI Codex OSS Application Status

Official form checked:

```text
https://openai.com/form/codex-for-oss/
```

Do not submit yet because:

- repo is private
- PR is not merged
- applicant fields are missing
- release tag is not created

Prepared answer constraints:

- Use GitHub username `kirillsaven`.
- Use repo URL `https://github.com/kirillsaven/competitors_spy` only after it is public.
- Role: `Primary maintainer` or closest truthful option.
- Do not claim adoption, users, stars, downloads, benchmarks, production scale, or external affiliation.

## Questions For Pro

1. Security review: Are there remaining public-release risks in the changed docs/templates/tooling, especially private operational metadata, credential handling, or old-history caveats?
2. OSS-readiness review: Is the new README/DEPLOY/SECURITY/CONTRIBUTING/ROADMAP set honest, complete, and not overclaiming?
3. Maintainer AI review: Is `tools/maintainer_ai.py` safe enough as optional tooling? Look for redaction gaps, `.env` bypasses, unsafe OpenAI call behavior, dependency concerns, and misleading fallback behavior.
4. CI/release workflow review: Are `.github/workflows/ci.yml`, the new release workflow, and the disabled maintainer-AI example appropriate for a first public OSS baseline?
5. Test review: Is the date-relative repair in `tracking/tests/test_platform_onboarding.py` correct and non-flaky?
6. Public-release gate: What must be done before changing repository visibility to public?
7. Application review: Are the proposed OpenAI Codex OSS application claims truthful and strong enough without exaggerating adoption?

## Desired Pro Response Format

Please respond as a code/security review:

```text
Verdict: BLOCK / APPROVE AFTER FIXES / APPROVE

Critical findings:
- file:line - issue - required fix

Non-critical findings:
- file:line - issue - recommended fix

Security/public-release checklist:
- pass/fail/unknown per item

Application wording review:
- what to keep
- what to weaken
- what to add

Minimum next Codex prompt:
<copyable prompt for Codex to apply fixes>
```

If there are no blocking issues, say so explicitly and list the residual risks.
