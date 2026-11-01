"""`gitpulse.yml`: which repos to analyse, identity overrides and the warehouse URL."""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class RepoSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str  # owner/repo
    #: Workflow names whose successful runs on the default branch count as deployments.
    deploy_workflows: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not REPO_RE.match(v):
            raise ValueError(f"repo must be 'owner/name', got {v!r}")
        return v


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/gitpulse"
    repos: list[RepoSpec] = Field(min_length=1)
    #: Look-back window for the first (full) extract; later runs are incremental.
    since_days: int = Field(default=180, ge=1, le=3650)
    #: alias email or login -> canonical identity (like git's .mailmap).
    identities: dict[str, str] = Field(default_factory=dict)
    cache_dir: str = ".gitpulse-cache"

    @field_validator("repos", mode="before")
    @classmethod
    def _repos(cls, v: object) -> object:
        # Allow the short form `- owner/repo` as well as mappings.
        return [{"name": r} if isinstance(r, str) else r for r in v] if isinstance(v, list) else v


def load_settings(path: str | Path = "gitpulse.yml") -> Settings:
    data = yaml.safe_load(Path(path).read_text()) or {}
    if url := os.environ.get("GITPULSE_DATABASE_URL"):
        data["database_url"] = url  # secrets come from the environment, never the YAML
    return Settings.model_validate(data)
