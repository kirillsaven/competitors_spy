# Public launch checklist

Use this checklist before sharing the repository publicly or sending an updated OSS-program application.

## Repository metadata

- [ ] Repository visibility is public.
- [ ] Description explains the project in one sentence.
- [ ] Homepage points to docs, demo, or README anchor.
- [ ] Topics cover the real stack and problem domain.
- [ ] License is visible on GitHub.

## First impression

- [ ] README explains why the project exists.
- [ ] README has a five-minute quick start.
- [ ] README links to demo, roadmap, security, contributing, and promotion docs.
- [ ] Screenshots or demo media are linked when available.
- [ ] The project states that it does not require social-network logins from users.

## Maintainer workflow

- [ ] Issue templates exist.
- [ ] PR template exists.
- [ ] Labels are documented.
- [ ] Good first issues are open.
- [ ] Roadmap has near-term and longer-term items.
- [ ] Changelog records public-facing changes.

## Release and distribution

- [ ] Tagged release exists.
- [ ] CI passes on `main`.
- [ ] Release workflow passes on tags.
- [ ] Docker image workflow exists for GHCR.
- [ ] Docker Compose remains the recommended full-stack local path.

## Security and privacy

- [ ] `.env` is ignored.
- [ ] `.env.example` uses placeholders only.
- [ ] `gitleaks` or equivalent scan passes.
- [ ] Old PR patch URLs do not expose private operational markers.
- [ ] Security policy tells users not to post secrets publicly.
- [ ] Docs avoid real Telegram IDs, server hosts, private paths, and deployment records.

## Promotion

- [ ] Promotion copy is ready in `docs/promotion-kit.md`.
- [ ] Demo script is ready in `docs/demo-script.md`.
- [ ] Maintainer can explain why the project matters without claiming adoption it does not have yet.
- [ ] Any request for stars is honest and secondary to a request for feedback.
