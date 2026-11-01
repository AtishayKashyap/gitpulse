"""Report, API and CLI on top of the known dataset."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from gitpulse import report
from gitpulse.api import create_app
from gitpulse.cli import app as cli

from .conftest import TEST_DB
from .test_warehouse import load


def test_report_renders_charts_and_html(engine: sa.Engine, tmp_path: Path) -> None:
    load(engine)
    index = report.build(engine, tmp_path / "site")
    html = index.read_text()
    pngs = sorted(p.name for p in (tmp_path / "site").glob("*.png"))
    assert pngs == [
        "o--r-activity.png",
        "o--r-bus.png",
        "o--r-heatmap.png",
        "o--r-hotspots.png",
    ]  # 1 merge week: no cycle chart
    assert "o/r" in html and "50%" in html and "20.0 h" in html  # coverage + median cycle time
    assert "nan" not in html


def test_format_helpers() -> None:
    assert report.fmt_hours(None) == "—"
    assert report.fmt_hours(float("nan")) == "—"
    assert report.fmt_hours(0.5) == "30 min"
    assert report.fmt_hours(5) == "5.0 h"
    assert report.fmt_hours(72) == "3.0 d"


def test_api(engine: sa.Engine) -> None:
    load(engine)
    client = TestClient(create_app(engine))
    assert client.get("/healthz").json() == {"status": "ok"}
    assert "bus_factor" in client.get("/metrics").json()
    rows = client.get("/metrics/bus_factor", params={"repo": "o/r"}).json()
    assert {r["top_dir"] for r in rows} >= {"src", "lib"}
    assert client.get("/metrics/nope").status_code == 404
    assert client.get("/metrics/hotspots", params={"limit": 0}).status_code == 422
    repos = client.get("/repos").json()
    assert repos[0]["repo"] == "o/r" and repos[0]["commits"] == 6


def test_cli_transform_and_report(engine: sa.Engine, tmp_path: Path) -> None:
    load(engine)
    cfg = tmp_path / "gitpulse.yml"
    cfg.write_text(
        f"database_url: {TEST_DB}\nrepos:\n  - name: o/r\n    deploy_workflows: [Deploy]\nidentities:\n  old@personal.com: alice\n"
    )
    runner = CliRunner()
    t = runner.invoke(cli, ["transform", "-c", str(cfg)])
    assert t.exit_code == 0, t.output
    assert "data tests: 7 passed" in t.output
    r = runner.invoke(cli, ["report", "-c", str(cfg), "--out", str(tmp_path / "out")])
    assert r.exit_code == 0 and (tmp_path / "out" / "index.html").exists()
