"""`gitpulse` CLI: extract -> transform -> test -> report."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Annotated

import structlog
import typer

from . import report as report_mod
from . import warehouse
from .config import load_settings
from .extract import Extractor
from .github import GitHub

app = typer.Typer(help="Engineering analytics from git history and GitHub", no_args_is_help=True)
Cfg = Annotated[str, typer.Option("--config", "-c", envvar="GITPULSE_CONFIG")]


def _logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer()
            if os.getenv("GITPULSE_LOG_FORMAT") == "json"
            else structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=lambda *_: structlog.PrintLogger(sys.stderr),
    )


@app.command()
def extract(config: Cfg = "gitpulse.yml") -> None:
    """Pull git history + GitHub PRs, reviews and CI runs into raw tables (incremental)."""
    _logging()
    settings = load_settings(config)
    engine = warehouse.make_engine(settings.database_url)
    warehouse.init(engine)

    async def go() -> None:
        gh = GitHub(os.environ.get("GITHUB_TOKEN"), cache=warehouse.DbCache(engine))
        try:
            reports = await Extractor(settings, engine, gh).run()
        finally:
            await gh.aclose()
        for r in reports:
            note = f"  (API unavailable, git-only: {r.api_error[:80]})" if r.api_error else ""
            typer.echo(f"{r.repo}: {json.dumps(r.counts)} in {r.seconds}s{note}")
        s = gh.stats
        typer.echo(
            f"API: {s.requests} requests, {s.not_modified} not-modified (304), {s.retries} retries, {s.rate_limit_waits} rate-limit waits"
        )

    asyncio.run(go())


@app.command()
def transform(config: Cfg = "gitpulse.yml") -> None:
    """Rebuild the star schema and metric views, then run data tests."""
    _logging()
    settings = load_settings(config)
    engine = warehouse.make_engine(settings.database_url)
    warehouse.init(engine)
    timings = warehouse.transform(
        engine, settings.identities, {r.name: r.deploy_workflows for r in settings.repos}
    )
    typer.echo(f"built {len(timings)} model files in {sum(timings.values()):.0f} ms")
    failed = [t for t in warehouse.run_data_tests(engine) if t.failures]
    for t in failed:
        typer.echo(f"FAIL {t.name}: {t.failures} rows", err=True)
    if failed:
        raise typer.Exit(1)
    typer.echo(f"data tests: {len(warehouse.DATA_TESTS)} passed")


@app.command()
def report(config: Cfg = "gitpulse.yml", out: str = "site") -> None:
    """Render the static HTML report with charts."""
    settings = load_settings(config)
    path = report_mod.build(warehouse.make_engine(settings.database_url), out)
    typer.echo(f"report written to {path}")


@app.command()
def run(config: Cfg = "gitpulse.yml", out: str = "site") -> None:
    """extract + transform + report."""
    extract(config)
    transform(config)
    report(config, out)


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000) -> None:  # noqa: S104
    """Serve the metrics API."""
    import uvicorn

    uvicorn.run("gitpulse.api:app", host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    app()
