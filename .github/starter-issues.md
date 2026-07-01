# Starter issues

Use these as the first public issues after the OSS-readiness PR lands. They are intentionally small, testable, and safe for new contributors.

Status note: the OSS approval polish PR addresses #75, #76, and the documentation part of #78. Keep #77 and #79 open unless code changes close them, and replenish beginner-friendly issues when the documentation tasks are merged.

## 1. Add a sanitized sample Telegram report payload

Public issue: https://github.com/kirillsaven/competitors_spy/issues/75

Labels: `good first issue`, `documentation`, `area:scoring`

Create `docs/examples/sample-report-payload.md` with a redacted, deterministic example of the report payload shape. It should show YouTube, TikTok, and Instagram sections with fake handles, fake links, fake view counts, and no real Telegram IDs.

Acceptance criteria:

- The example includes all three platform sections.
- All identifiers are placeholders.
- README links to the example from the demo section.

## 2. Add provider diagnostics examples to the README

Public issue: https://github.com/kirillsaven/competitors_spy/issues/76

Labels: `good first issue`, `documentation`

Document the safest commands for checking provider configuration without sending real Telegram messages. Focus on `python manage.py check`, provider env vars, and the existing live verification command with placeholder handles.

Acceptance criteria:

- README gains a short "Provider diagnostics" subsection.
- The subsection tells users not to paste real tokens into issues.
- No code changes are required.

## 3. Improve empty-report diagnostics in docs

Public issue: https://github.com/kirillsaven/competitors_spy/issues/79

Labels: `help wanted`, `documentation`, `area:scoring`

Create a troubleshooting section that explains why a report section might be empty: missing provider credentials, provider rate limits, no public view counts, thresholds too high, no baseline snapshots, or all items below score filters.

Acceptance criteria:

- Add the troubleshooting section to README or a dedicated doc.
- Include at least one safe command for collecting local diagnostics.
- Do not recommend silent fallback behavior.

## 4. Add a provider fixture for Instagram parser behavior

Public issue: https://github.com/kirillsaven/competitors_spy/issues/77

Labels: `help wanted`, `provider:instagram`

Add a small sanitized fixture and test for Instagram provider response parsing. The fixture must not be copied from private provider output unless it is fully synthetic and reviewed for secrets.

Acceptance criteria:

- Fixture is synthetic.
- Test covers a normal recent Reel with public view count.
- Test covers missing view count behavior.

## 5. Add a short Docker image usage doc

Public issue: https://github.com/kirillsaven/competitors_spy/issues/78

Labels: `good first issue`, `documentation`, `area:deployment`

Once GHCR publishing is enabled, document how to pull a tagged image and how it differs from `docker compose up -d --build`.

Acceptance criteria:

- Add a short GHCR section to README or `docs/deployment.md`.
- Explain that Compose remains preferred for full local development.
- Do not require users to publish their own package.

## Replacement issue ideas after merge

These replacement issues are already open so the repo keeps newcomer-friendly tasks after #75, #76, or #78 are closed:

- #81 Add YouTube sample fixture for missing engagement metrics.
- #82 Add provider diagnostics test for empty Instagram response.
- #83 Add docs asset accessibility pass.
- #84 Add maintainer demo recording checklist.

Additional future ideas:

- Improve Russian copy for first-run no-baseline reports.
- Add a short fixture for TikTok provider rate-limit diagnostics.
