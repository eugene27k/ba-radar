"""CLI guards that protect state."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ba_radar.cli import app

runner = CliRunner()


def test_run_without_credentials_refuses_before_touching_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run` must fail on missing Telegram credentials before collect/prepare, not at
    the send step — prepare marks items delivered, and a batch prepared by a run that
    can never send is orphaned once the resend window passes."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    async def _collect_must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("collect ran before the credentials check")

    monkeypatch.setattr("ba_radar.tasks.collect", _collect_must_not_run)

    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text("{}", encoding="utf-8")

    result = runner.invoke(app, ["run", "--settings", str(settings_path)])

    assert result.exit_code == 2
