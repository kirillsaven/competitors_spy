from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path, PurePosixPath


MAX_MODEL_CHARS = 60000
DEFAULT_MODEL = "gpt-4.1-mini"


class MaintainerAiError(RuntimeError):
    pass


_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_TELEGRAM_BOT_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")
_GOOGLE_API_KEY_RE = re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b")
_GITHUB_CLASSIC_TOKEN_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")
_GITHUB_FINE_GRAINED_TOKEN_RE = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")
_SLACK_TOKEN_RE = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")
_DB_URL_WITH_CREDS_RE = re.compile(r"\b((?:postgresql?|mysql|redis)://)([^:/\s]+):([^@\s]+)@([^\s]+)")
_ENV_ASSIGNMENT_RE = re.compile(
    r"(?im)^([A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|PASSWD|ACCESS_TOKEN|DATABASE_URL|REDIS_URL|POSTGRES_PASSWORD)[A-Z0-9_]*\s*=\s*)(.+)$"
)
_JSON_SECRET_RE = re.compile(
    r'(?i)("?[a-z0-9_]*(?:api[_-]?key|token|secret|password|passwd|access[_-]?token)"?\s*:\s*")([^"]+)(")'
)
_BEARER_SECRET_CONTEXT_RE = re.compile(
    r"(?im)^((?:authorization|proxy-authorization)\s*:\s*bearer\s+)([A-Za-z0-9._~+/=-]{20,})$"
)
_DIFF_PATH_RE = re.compile(r"(?m)^(?:diff --git a/(.*?) b/(.*?)|--- (?:a/)?(.*?)|\+\+\+ (?:b/)?(.*?))$")


def _is_private_env_path(path: str) -> bool:
    cleaned = path.strip()
    if not cleaned or cleaned == "/dev/null":
        return False
    cleaned = cleaned.split("\t", 1)[0].strip()
    name = PurePosixPath(cleaned.replace("\\", "/")).name
    return (name == ".env" or name.startswith(".env.")) and name != ".env.example"


def diff_contains_private_env_path(diff_text: str) -> bool:
    for match in _DIFF_PATH_RE.finditer(diff_text):
        for group in match.groups():
            if group and _is_private_env_path(group):
                return True
    return False


def redact_sensitive_text(text: str) -> str:
    redacted = _PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", text)
    redacted = _TELEGRAM_BOT_TOKEN_RE.sub("[REDACTED_TELEGRAM_BOT_TOKEN]", redacted)
    redacted = _OPENAI_KEY_RE.sub("[REDACTED_API_KEY]", redacted)
    redacted = _GOOGLE_API_KEY_RE.sub("[REDACTED_API_KEY]", redacted)
    redacted = _GITHUB_CLASSIC_TOKEN_RE.sub("[REDACTED_GITHUB_TOKEN]", redacted)
    redacted = _GITHUB_FINE_GRAINED_TOKEN_RE.sub("[REDACTED_GITHUB_TOKEN]", redacted)
    redacted = _SLACK_TOKEN_RE.sub("[REDACTED_SLACK_TOKEN]", redacted)
    redacted = _DB_URL_WITH_CREDS_RE.sub(r"\1[REDACTED_CREDENTIALS]@\4", redacted)
    redacted = _ENV_ASSIGNMENT_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _JSON_SECRET_RE.sub(r"\1[REDACTED]\3", redacted)
    redacted = _BEARER_SECRET_CONTEXT_RE.sub(r"\1[REDACTED_BEARER_TOKEN]", redacted)
    return redacted


def _read_input_file(path: str) -> str:
    input_path = Path(path)
    if not input_path.exists():
        raise MaintainerAiError(f"Input file does not exist: {input_path}")
    if not input_path.is_file():
        raise MaintainerAiError(f"Input path is not a file: {input_path}")
    if input_path.name == ".env" or (input_path.name.startswith(".env.") and input_path.name != ".env.example"):
        raise MaintainerAiError("Refusing to read .env files. Provide a sanitized non-env input file instead.")
    return input_path.read_text(encoding="utf-8", errors="replace")


def _truncate_for_model(text: str) -> str:
    if len(text) <= MAX_MODEL_CHARS:
        return text
    omitted = len(text) - MAX_MODEL_CHARS
    return f"{text[:MAX_MODEL_CHARS]}\n\n[TRUNCATED {omitted} CHARACTERS]"


def _diff_stats(diff_text: str) -> tuple[list[str], int, int]:
    files: list[str] = []
    for match in re.finditer(r"^diff --git a/(.*?) b/(.*?)$", diff_text, flags=re.MULTILINE):
        files.append(match.group(2))
    added = 0
    removed = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return files, added, removed


def _fallback_pr_summary(content: str, source: str) -> str:
    if not content.strip():
        return "# PR Review Checklist\n\nMode: deterministic fallback\n\nInput is empty. Add a diff before requesting a PR summary.\n"
    files, added, removed = _diff_stats(content)
    file_lines = "\n".join(f"- `{item}`" for item in files[:20]) or "- No diff file headers detected"
    more = f"\n- ...and {len(files) - 20} more files" if len(files) > 20 else ""
    return (
        "# PR Review Checklist\n\n"
        "Mode: deterministic fallback\n\n"
        f"Source: `{source}`\n\n"
        "## Diff summary\n\n"
        f"- Files changed: {len(files)}\n"
        f"- Added lines: {added}\n"
        f"- Removed lines: {removed}\n\n"
        "## Files\n\n"
        f"{file_lines}{more}\n\n"
        "## Review checklist\n\n"
        "- Check that behavior changes have focused tests.\n"
        "- Check that configuration changes are documented in `.env.example` and README.\n"
        "- Check that provider/API failures are explicit and not silent empty fallbacks.\n"
        "- Check that no secrets, raw IDs, logs, dumps, or private deployment details are included.\n"
    )


def _fallback_issue_triage(content: str, source: str) -> str:
    if not content.strip():
        return "# Issue Triage\n\nMode: deterministic fallback\n\nInput is empty. Add issue text before requesting triage.\n"

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    title = next((line.lstrip("# ").strip() for line in lines if line), "Untitled issue")
    lowered = content.lower()
    labels: list[str] = []
    keyword_labels = {
        "bug": ("error", "traceback", "failed", "fails", "broken", "bug"),
        "documentation": ("readme", "docs", "documentation"),
        "provider": ("youtube", "tiktok", "instagram", "apify", "provider"),
        "telegram": ("telegram", "bot", "callback"),
        "reports": ("report", "scoring", "baseline", "viral"),
        "security": ("secret", "token", "password", "vulnerability", "security"),
    }
    for label, keywords in keyword_labels.items():
        if any(keyword in lowered for keyword in keywords):
            labels.append(label)
    if not labels:
        labels.append("needs-triage")

    return (
        "# Issue Triage\n\n"
        "Mode: deterministic fallback\n\n"
        f"Source: `{source}`\n\n"
        f"## Title\n\n{title}\n\n"
        "## Suggested labels\n\n"
        + "\n".join(f"- `{label}`" for label in labels)
        + "\n\n"
        "## Maintainer checklist\n\n"
        "- Ask for a minimal reproduction if one is missing.\n"
        "- Confirm whether real provider credentials or live network access are required.\n"
        "- Request sanitized logs only.\n"
        "- Decide whether the issue is a bug, docs task, provider task, or support question.\n"
    )


def _latest_changelog_section(content: str) -> str:
    match = re.search(r"(?ms)^##\s+(.+?)(?=^##\s+|\Z)", content)
    if not match:
        return content.strip()
    return match.group(0).strip()


def _fallback_release_notes(content: str, source: str) -> str:
    if not content.strip():
        return "# Release Notes Draft\n\nMode: deterministic fallback\n\nInput is empty. Add a changelog before requesting notes.\n"
    latest = _latest_changelog_section(content)
    return (
        "# Release Notes Draft\n\n"
        "Mode: deterministic fallback\n\n"
        f"Source: `{source}`\n\n"
        "## Draft\n\n"
        f"{latest}\n\n"
        "## Release checklist\n\n"
        "- Confirm CI passed on the release commit.\n"
        "- Confirm no secrets or private operational records are included.\n"
        "- Confirm migration and deployment notes are documented if needed.\n"
        "- Tag the release only after the public safety checklist is complete.\n"
    )


def _fallback_output(command: str, content: str, source: str) -> str:
    if command == "pr-summary":
        return _fallback_pr_summary(content, source)
    if command == "issue-triage":
        return _fallback_issue_triage(content, source)
    if command == "release-notes":
        return _fallback_release_notes(content, source)
    raise MaintainerAiError(f"Unsupported command: {command}")


def _model_prompt(command: str, content: str, source: str) -> str:
    task = {
        "pr-summary": "Generate a concise PR review checklist and risk summary from this sanitized diff.",
        "issue-triage": "Generate issue triage suggestions, likely labels, clarifying questions, and next maintainer action.",
        "release-notes": "Draft release notes from this sanitized changelog.",
    }[command]
    return (
        f"{task}\n\n"
        "Rules:\n"
        "- Do not claim adoption, usage, benchmarks, or affiliations not present in the input.\n"
        "- Do not ask for or reveal secrets.\n"
        "- Keep output practical and maintainer-focused.\n"
        "- If the input is empty or insufficient, say so directly.\n\n"
        f"Source: {source}\n\n"
        f"{_truncate_for_model(content)}"
    )


def _call_openai(command: str, content: str, source: str) -> str:
    if diff_contains_private_env_path(content):
        raise MaintainerAiError("Refusing to send a diff containing private .env file changes to OpenAI.")

    try:
        from openai import OpenAI  # type: ignore
    except ImportError as exc:
        raise MaintainerAiError(
            "OPENAI_API_KEY is set, but the optional `openai` package is not installed. "
            "Install it explicitly or unset OPENAI_API_KEY to use deterministic fallback mode."
        ) from exc

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = client.responses.create(
        model=os.environ.get("OPENAI_MAINTAINER_MODEL", DEFAULT_MODEL),
        input=[
            {
                "role": "system",
                "content": "You are a cautious open-source maintainer assistant. Never reveal or request secrets.",
            },
            {"role": "user", "content": _model_prompt(command, content, source)},
        ],
    )
    output_text = getattr(response, "output_text", None)
    if output_text:
        return str(output_text)
    return str(response)


def run_command(command: str, path: str) -> str:
    raw = _read_input_file(path)
    sanitized = redact_sensitive_text(raw)
    if os.environ.get("OPENAI_API_KEY"):
        return _call_openai(command, sanitized, path)
    return _fallback_output(command, sanitized, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Optional maintainer AI helper for Competitor Spy.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    pr_parser = subparsers.add_parser("pr-summary", help="Generate a PR review checklist from a diff file.")
    pr_parser.add_argument("--diff-file", required=True, help="Path to a sanitized diff file.")

    issue_parser = subparsers.add_parser("issue-triage", help="Generate issue triage suggestions from a Markdown file.")
    issue_parser.add_argument("--issue-file", required=True, help="Path to a sanitized issue Markdown file.")

    release_parser = subparsers.add_parser("release-notes", help="Draft release notes from a changelog.")
    release_parser.add_argument("--changelog", required=True, help="Path to CHANGELOG.md or another changelog file.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    path_by_command = {
        "pr-summary": args.diff_file if args.command == "pr-summary" else None,
        "issue-triage": args.issue_file if args.command == "issue-triage" else None,
        "release-notes": args.changelog if args.command == "release-notes" else None,
    }
    path = path_by_command[args.command]
    if path is None:
        parser.error("missing input file")

    try:
        print(run_command(args.command, path))
    except MaintainerAiError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
