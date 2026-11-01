"""Read-only API over the metric views."""

from __future__ import annotations

import os
from typing import Annotated, Any

import pandas as pd
import sqlalchemy as sa
from fastapi import Depends, FastAPI, HTTPException, Query, Request

from . import metrics
from .warehouse import make_engine


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """JSON-safe rows: NaN/NaT -> null."""
    return [
        {str(k): v for k, v in row.items()}
        for row in df.astype(object).where(df.notna(), None).to_dict("records")
    ]


def get_engine(request: Request) -> sa.Engine:
    if request.app.state.engine is None:  # lazily, so importing the module opens nothing
        request.app.state.engine = make_engine(
            os.environ.get(
                "GITPULSE_DATABASE_URL",
                "postgresql+psycopg://postgres:postgres@localhost:5432/gitpulse",
            )
        )
    eng: sa.Engine = request.app.state.engine
    return eng


# Module scope on purpose: with `from __future__ import annotations` FastAPI resolves
# annotations from module globals, and a function-local alias would silently degrade
# into a required *query parameter*.
Eng = Annotated[sa.Engine, Depends(get_engine)]


def create_app(engine: sa.Engine | None = None) -> FastAPI:
    app = FastAPI(
        title="gitpulse",
        version="0.1.0",
        description="Engineering metrics computed from git history and GitHub.",
    )
    app.state.engine = engine

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/repos")
    def repos(engine: Eng) -> list[dict[str, Any]]:
        df = metrics.summary(engine)
        return _records(df)

    @app.get("/metrics")
    def list_metrics() -> list[str]:
        return sorted(metrics.VIEWS)

    @app.get("/metrics/{name}")
    def metric(
        engine: Eng, name: str, repo: str | None = None, limit: int = Query(500, ge=1, le=5000)
    ) -> list[dict[str, Any]]:
        if name not in metrics.VIEWS:
            raise HTTPException(404, f"unknown metric {name!r}; see /metrics")
        df = metrics.read(engine, name, repo, limit)
        return _records(df)

    return app


app = create_app()
