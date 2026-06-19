# Security Policy

## Supported versions

Security fixes are accepted for the current `main` branch and the latest tagged public release.

## Reporting a vulnerability

Please report vulnerabilities privately to the maintainer instead of opening a public issue. If you do not have a private contact path, open a GitHub security advisory for this repository.

Do not post secrets in public issues, pull requests, logs, screenshots, or comments. This includes API keys, Telegram bot tokens, database URLs, Redis URLs, cookies, private keys, Telegram user IDs, chat IDs, and private server details.

## Responsible disclosure

- Include a clear description of the issue and affected component.
- Include minimal reproduction steps that do not expose real credentials.
- Allow reasonable time for triage and remediation before public disclosure.
- Avoid accessing, modifying, or exfiltrating data that is not yours.

## Secrets and token rotation

Users and contributors must not commit API keys, bot tokens, database credentials, private keys, or `.env` files.

If a token or credential is accidentally exposed:

1. Revoke or rotate it immediately.
2. Remove it from current files.
3. Check git history for exposure.
4. Treat public exposure as compromised even if the value was deleted later.

## Self-hosting responsibility

Self-hosters are responsible for platform API credentials, deployment hardening, HTTPS, backups, firewall rules, Django Admin access, and compliance with provider terms and rate limits.
