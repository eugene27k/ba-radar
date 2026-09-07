"""Command-line interface.

`prepare-digest` and `send-digest` are separate commands because the GitHub Actions
workflow commits state to git between them (answers doc Q14). `run` chains everything
for local use, where there is no git commit to interleave.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from ba_radar import tasks
from ba_radar.registry import load_registry
from ba_radar.schedule import evaluate_gate
from ba_radar.settings import Secrets, Settings

app = typer.Typer(
    add_completion=False,
    help="BA Radar — daily AI-native development digest for business analysts.",
)

SettingsOption = Annotated[
    Path | None,
    typer.Option("--settings", help="Path to settings.yaml (defaults to config/settings.yaml)."),
]


def _load(settings_path: Path | None) -> Settings:
    return Settings.load(settings_path)


def _echo_log(entries: list[object]) -> None:
    for entry in entries:
        level = getattr(entry, "level", "info")
        source = getattr(entry, "source_id", None)
        message = getattr(entry, "message", str(entry))
        prefix = f"[{level}]" + (f" {source}" if source else "")
        colour = typer.colors.RED if level == "error" else typer.colors.YELLOW
        typer.secho(f"  {prefix}: {message}", fg=colour)


@app.command()
def collect(settings: SettingsOption = None) -> None:
    """Fetch every active source, normalise, dedupe and store."""
    cfg = _load(settings)
    summary = asyncio.run(tasks.collect(cfg))

    typer.echo(
        f"run #{summary.run_id} {summary.status.value}: "
        f"{summary.sources_attempted - summary.sources_failed}/{summary.sources_attempted} "
        f"sources ok, {summary.items_created} new, {summary.items_merged} merged"
    )
    _echo_log(list(summary.log))


@app.command("prepare-digest")
def prepare_digest_cmd(settings: SettingsOption = None) -> None:
    """Select today's items and mark them delivered. Commit state after this."""
    cfg = _load(settings)
    summary = tasks.prepare_digest(cfg)

    match summary.state:
        case "already_delivered":
            typer.echo(f"already delivered today (run #{summary.run_id}); nothing to do")
        case "already_prepared":
            typer.echo(f"run #{summary.run_id} already prepared with {summary.selected} item(s)")
        case _:
            typer.echo(
                f"run #{summary.run_id} prepared: {summary.selected} of "
                f"{summary.pool} undelivered item(s)"
            )


@app.command("send-digest")
def send_digest_cmd(settings: SettingsOption = None) -> None:
    """Send the prepared digest to Telegram. Commit state after this too."""
    cfg = _load(settings)
    try:
        secrets = Secrets.from_env()
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc

    try:
        summary = asyncio.run(tasks.send_digest(cfg, secrets))
    except Exception as exc:
        # req. 4.2.5 / 4.2.6 tier 1 — a non-zero exit is the out-of-band alert, because
        # Telegram is exactly the channel that just failed.
        typer.secho(f"delivery failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    match summary.state:
        case "already_delivered":
            typer.echo(f"already delivered today (run #{summary.run_id})")
        case "nothing_prepared":
            typer.echo("nothing prepared for today; run prepare-digest first")
        case _:
            typer.echo(
                f"run #{summary.run_id} sent: {summary.delivered} item(s) in "
                f"{summary.messages} message(s)"
            )


@app.command()
def preview(settings: SettingsOption = None) -> None:
    """Render the digest to stdout without touching the database or Telegram."""
    cfg = _load(settings)
    for index, message in enumerate(tasks.preview_digest(cfg), start=1):
        typer.echo(f"--- message {index} ({len(message)} chars) ---")
        typer.echo(message)


@app.command()
def run(
    settings: SettingsOption = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Collect, then print the digest instead of sending.")
    ] = False,
) -> None:
    """Collect, prepare and send in one go. For local use and manual recovery."""
    cfg = _load(settings)

    if not dry_run:
        # Fail before touching any state: prepare marks items delivered, so finding
        # out about missing credentials only at the send step would leave a prepared
        # batch behind (resent automatically for pending_resend_days, orphaned after).
        try:
            Secrets.from_env()
        except RuntimeError as exc:
            typer.secho(f"{exc} — set them or use --dry-run", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from exc

    summary = asyncio.run(tasks.collect(cfg))
    typer.echo(
        f"collected: {summary.items_created} new, {summary.items_merged} merged "
        f"({summary.sources_failed} source failure(s))"
    )
    _echo_log(list(summary.log))

    if dry_run:
        for index, message in enumerate(tasks.preview_digest(cfg), start=1):
            typer.echo(f"--- message {index} ({len(message)} chars) ---")
            typer.echo(message)
        return

    prepare_digest_cmd(settings)
    send_digest_cmd(settings)


@app.command("gate")
def gate_cmd(
    hour: Annotated[int, typer.Option("--hour", help="Target local hour, 0-23.")],
    settings: SettingsOption = None,
    weekdays_only: Annotated[
        bool,
        typer.Option("--weekdays-only/--any-day", help="Skip Saturday and Sunday."),
    ] = True,
    force: Annotated[
        bool, typer.Option("--force", help="Bypass the gate (manual dispatch).")
    ] = False,
) -> None:
    """Decide whether this UTC-scheduled run is the right one for the local target hour.

    Writes `proceed=true|false` to $GITHUB_OUTPUT when running in Actions, and exits 0
    either way so a skipped run is not a failed run.
    """
    cfg = _load(settings)

    if force:
        decision_proceed, reason = True, "forced"
    else:
        decision = evaluate_gate(
            datetime.now(UTC),
            timezone=cfg.schedule.timezone,
            target_hour=hour,
            tolerance_minutes=cfg.schedule.gate_tolerance_minutes,
            weekdays_only=weekdays_only,
        )
        decision_proceed, reason = decision.proceed, decision.reason

    typer.echo(f"gate: {'proceed' if decision_proceed else 'skip'} — {reason}")

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"proceed={'true' if decision_proceed else 'false'}\n")


@app.command("validate-sources")
def validate_sources(settings: SettingsOption = None) -> None:
    """Check config/sources.yaml. Exits non-zero if any record is invalid."""
    cfg = _load(settings)
    result = load_registry(cfg.sources_path)

    typer.echo(f"{len(result.sources)} valid source(s), {len(result.active)} active")
    for source in result.sources:
        marker = "on " if source.active else "off"
        typer.echo(f"  [{marker}] {source.id:<24} {source.method.value:<20} w={source.weight}")

    if result.errors:
        typer.secho(f"{len(result.errors)} problem(s):", fg=typer.colors.RED, err=True)
        for error in result.errors:
            typer.secho(f"  {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
def status(settings: SettingsOption = None, limit: int = 10) -> None:
    """Show recent runs."""
    from ba_radar.store import RunRepo, connect

    cfg = _load(settings)
    conn = connect(cfg.db_path)
    runs = RunRepo(conn).recent(limit)
    conn.close()

    if not runs:
        typer.echo("no runs recorded yet")
        return

    for entry in runs:
        confirmed = "✓" if entry.telegram_confirmed_at else " "
        typer.echo(
            f"#{entry.id:<4} {entry.kind.value:<8} {entry.started_at:%Y-%m-%d %H:%M} "
            f"{entry.status.value:<9} {confirmed} "
            f"collected={entry.collected_count} delivered={entry.delivered_count} "
            f"errors={len(entry.errors)}"
        )


@app.command("prune")
def prune_cmd(settings: SettingsOption = None) -> None:
    """Apply the retention policy and vacuum the database."""
    cfg = _load(settings)
    summary = tasks.prune(cfg)
    typer.echo(f"pruned {summary.items_pruned} item(s), deleted {summary.runs_deleted} run(s)")


def main() -> None:  # pragma: no cover
    sys.exit(app())


if __name__ == "__main__":  # pragma: no cover
    main()
