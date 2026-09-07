"""CLI guards that protect state."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from ba_radar.cli import app
from ba_radar.llm import LLMRequest

from .fake_llm import FakeProvider, fake_pool

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


def test_dry_run_without_a_provider_key_refuses_before_collect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage 2 scores during collect, so a run without the model key would leave
    every collected item permanently unscored — refuse before touching state."""

    async def _collect_must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("collect ran without a provider key")

    monkeypatch.setattr("ba_radar.tasks.collect", _collect_must_not_run)
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        "llm:\n  provider: anthropic\n  tasks:\n"
        "    relevance: {model: m, max_tokens: 10}\n    analysis: {model: m, max_tokens: 10}\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["run", "--dry-run", "--settings", str(settings_path)])

    assert result.exit_code == 2
    assert "ANTHROPIC_API_KEY" in result.output


def test_llm_check_reports_provider_model_and_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ba_radar.settings import Settings

    class Ping(BaseModel):
        ok: bool
        echo: str

    def ping(request: LLMRequest[object]) -> BaseModel:
        return Ping(ok=True, echo="radar")

    fake = FakeProvider(scripts={"relevance": ping}, input_tokens=17, output_tokens=5)

    class _Pool:
        @staticmethod
        def for_tasks(*args: object, **kwargs: object) -> object:
            return fake_pool(Settings.load(), fake)

    monkeypatch.setattr("ba_radar.cli.ProviderPool", _Pool)

    result = runner.invoke(app, ["llm-check"])

    assert result.exit_code == 0, result.output
    assert "provider=fake" in result.output and "model=claude-sonnet-5" in result.output
    assert "tokens_in=17 tokens_out=5" in result.output
    assert fake.calls["relevance"] == 1
