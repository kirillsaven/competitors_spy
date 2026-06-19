from __future__ import annotations

import pytest

from tools import maintainer_ai


def test_redacts_api_keys() -> None:
    text = "OPENAI_API_KEY=sk-" + ("a" * 32)

    redacted = maintainer_ai.redact_sensitive_text(text)

    assert "sk-" not in redacted
    assert "OPENAI_API_KEY=[REDACTED]" in redacted


def test_redacts_telegram_bot_tokens() -> None:
    text = "bot token 123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"

    redacted = maintainer_ai.redact_sensitive_text(text)

    assert "123456789:" not in redacted
    assert "[REDACTED_TELEGRAM_BOT_TOKEN]" in redacted


def test_redacts_database_urls() -> None:
    text = "DATABASE_URL=postgresql://user:secret@db:5432/app"

    redacted = maintainer_ai.redact_sensitive_text(text)

    assert "secret" not in redacted
    assert "DATABASE_URL=[REDACTED]" in redacted


def test_redacts_common_public_token_formats() -> None:
    text = "\n".join(
        [
            "ghp_" + ("a" * 36),
            "github_pat_" + ("A" * 80),
            "xoxb-" + ("1-" * 15) + "abcdef",
            "Authorization: Bearer " + ("b" * 40),
        ]
    )

    redacted = maintainer_ai.redact_sensitive_text(text)

    assert "ghp_" not in redacted
    assert "github_pat_" not in redacted
    assert "xoxb-" not in redacted
    assert "b" * 40 not in redacted
    assert "[REDACTED_GITHUB_TOKEN]" in redacted
    assert "[REDACTED_SLACK_TOKEN]" in redacted
    assert "[REDACTED_BEARER_TOKEN]" in redacted


@pytest.mark.parametrize(
    "path",
    [
        ".env.production",
        ".env.local",
        "deploy/.env",
        "secrets/.env.prod",
    ],
)
def test_private_env_diff_paths_are_refused_before_openai_import(tmp_path, monkeypatch, capsys, path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + ("a" * 32))
    diff_file = tmp_path / "change.diff"
    diff_file.write_text(
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "+SECRET=value\n",
        encoding="utf-8",
    )

    exit_code = maintainer_ai.main(["pr-summary", "--diff-file", str(diff_file)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "private .env file changes" in captured.err


def test_env_example_diff_is_allowed_in_fallback_mode(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    diff_file = tmp_path / "change.diff"
    diff_file.write_text(
        "diff --git a/.env.example b/.env.example\n"
        "--- a/.env.example\n"
        "+++ b/.env.example\n"
        "+EXAMPLE_TOKEN=\n",
        encoding="utf-8",
    )

    exit_code = maintainer_ai.main(["pr-summary", "--diff-file", str(diff_file)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Mode: deterministic fallback" in captured.out


def test_fallback_mode_with_no_openai_key(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    diff_file = tmp_path / "change.diff"
    diff_file.write_text("diff --git a/a.py b/a.py\n+print('x')\n", encoding="utf-8")

    exit_code = maintainer_ai.main(["pr-summary", "--diff-file", str(diff_file)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Mode: deterministic fallback" in captured.out
    assert "Files changed: 1" in captured.out


def test_missing_file_returns_error(monkeypatch, capsys) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    exit_code = maintainer_ai.main(["issue-triage", "--issue-file", "missing.md"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Input file does not exist" in captured.err


def test_empty_input_is_reported(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("", encoding="utf-8")

    exit_code = maintainer_ai.main(["release-notes", "--changelog", str(changelog)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Input is empty" in captured.out


def test_help_output(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        maintainer_ai.main(["--help"])

    captured = capsys.readouterr()
    assert exc.value.code == 0
    assert "pr-summary" in captured.out
    assert "issue-triage" in captured.out
    assert "release-notes" in captured.out
