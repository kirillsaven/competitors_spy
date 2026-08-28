# Changelog

## [Unreleased]

### Added

- Demo script for reviewers and contributors.
- Public launch checklist for OSS readiness.
- Promotion kit with ethical sharing copy and suggested topics.
- Desired GitHub labels manifest.
- Docker image workflow for GHCR build/publish on public branches and tags.

### Changed

- Refreshed the pending Python dependency and GitHub Actions updates together, including Gunicorn 26 and pytest 9.
- CI now checks dependency compatibility, migration drift, Gunicorn configuration, and migrations against PostgreSQL 16 alongside the existing test suite.
- README now explains the problem, demo paths, Docker image distribution, and public discovery call to action.
- Roadmap now separates provider hardening, demo/report quality, AI-assisted reports, and maintainer automation.

## [0.1.0] - Public OSS baseline

### Added

- Public README
- MIT license
- Contribution guide
- Security policy
- Roadmap
- CI baseline
- Docker-first local setup documentation

### Changed

- Public-safe deployment documentation
- Public-safe environment examples

### Security

- Removed private operational details from public docs
