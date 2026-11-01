"""Static HTML report with matplotlib charts, published to GitHub Pages by CI."""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: CI has no display
import matplotlib.pyplot as plt
import pandas as pd
import sqlalchemy as sa
from jinja2 import Environment, PackageLoader, select_autoescape
from matplotlib.figure import Figure

from . import metrics

# One restrained palette across every chart: series colour carries meaning, grid recedes.
INK, MUTED, GRID = "#1f2937", "#6b7280", "#e5e7eb"
PRIMARY, SECONDARY, ACCENT, BAD = "#2563eb", "#93c5fd", "#0d9488", "#dc2626"

plt.rcParams.update(
    {
        "figure.dpi": 110,
        "savefig.dpi": 160,
        "font.size": 10,
        "axes.edgecolor": GRID,
        "axes.labelcolor": MUTED,
        "axes.titlecolor": INK,
        "axes.titlesize": 11,
        "axes.titleweight": "bold",
        "axes.titlelocation": "left",
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "axes.axisbelow": True,
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "legend.frameon": False,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
    }
)


def _save(fig: Figure, out: Path, name: str) -> str:
    fig.tight_layout()
    fig.savefig(out / name, bbox_inches="tight")
    plt.close(fig)
    return name


def chart_activity(df: pd.DataFrame, out: Path, slug: str) -> str | None:
    if df.empty or df["commits"].sum() == 0:
        return None
    df = df.sort_values("week")
    fig, ax = plt.subplots(figsize=(8, 3))
    ax.bar(df["week"], df["commits"], width=5, color=SECONDARY, label="commits / week")
    ax.plot(df["week"], df["commits_ma4"], color=PRIMARY, lw=2, label="4-week average")
    if df["merged_prs"].sum():
        ax.plot(
            df["week"], df["merged_prs"], color=ACCENT, lw=1.5, ls="--", label="merged PRs / week"
        )
    ax.set_title("Weekly activity")
    ax.legend(loc="upper left", ncols=3, fontsize=8)
    ax.set_ylim(bottom=0)
    fig.autofmt_xdate()
    return _save(fig, out, f"{slug}-activity.png")


def chart_cycle(df: pd.DataFrame, out: Path, slug: str) -> str | None:
    df = df.dropna(subset=["week"]).sort_values("week")
    if len(df) < 2:
        return None
    fig, ax = plt.subplots(figsize=(8, 3))
    ax.fill_between(
        df["week"],
        df["cycle_p50_h"].astype(float),
        df["cycle_p90_h"].astype(float),
        color=SECONDARY,
        alpha=0.5,
        label="p50–p90",
    )
    ax.plot(
        df["week"],
        df["cycle_p50_h"].astype(float),
        color=PRIMARY,
        lw=2,
        marker="o",
        ms=3,
        label="median",
    )
    ax.set_title("PR cycle time (open → merge), hours")
    ax.set_yscale("symlog", linthresh=1)
    ax.legend(loc="upper left", fontsize=8)
    fig.autofmt_xdate()
    return _save(fig, out, f"{slug}-cycle.png")


def chart_hotspots(df: pd.DataFrame, out: Path, slug: str) -> str | None:
    df = df.sort_values("rank").head(10)
    if df.empty:
        return None
    df = df.iloc[::-1]
    labels = [p if len(p) <= 42 else "…" + p[-41:] for p in df["path"]]
    fig, ax = plt.subplots(figsize=(8, 0.35 * len(df) + 0.9))
    bars = ax.barh(labels, df["score"], color=PRIMARY)
    for b, churn, a in zip(bars, df["churn"], df["authors"], strict=True):
        ax.text(
            b.get_width(),
            b.get_y() + b.get_height() / 2,
            f"  {int(churn):,} lines · {a} author{'s' if a != 1 else ''}",
            va="center",
            fontsize=8,
            color=MUTED,
        )
    ax.set_title("Hotspots: churn × distinct authors, last 90 days")
    ax.set_xlabel("score (lines changed × authors) · generated files excluded")
    ax.set_xlim(0, df["score"].max() * 1.35)
    ax.grid(axis="y", visible=False)
    ax.tick_params(axis="y", labelsize=8)
    return _save(fig, out, f"{slug}-hotspots.png")


def chart_bus_factor(df: pd.DataFrame, out: Path, slug: str) -> str | None:
    df = df.dropna(subset=["bus_factor"]).sort_values("churn", ascending=False).head(10)
    if df.empty:
        return None
    df = df.iloc[::-1]
    colors = [BAD if b <= 1 else PRIMARY for b in df["bus_factor"]]
    fig, ax = plt.subplots(figsize=(8, 0.35 * len(df) + 0.9))
    ax.barh(df["top_dir"], df["top_author_share"].astype(float) * 100, color=colors)
    for y, (bf, n) in enumerate(zip(df["bus_factor"], df["authors"], strict=True)):
        ax.text(
            1,
            y,
            f" bus factor {int(bf)} · {n} author{'s' if n != 1 else ''}",
            va="center",
            fontsize=8,
            color="white",
        )
    ax.set_xlim(0, 100)
    ax.set_title("Knowledge concentration by directory (top author's share of churn)")
    ax.set_xlabel("% of last year's churn by one person · red = bus factor 1")
    ax.grid(axis="y", visible=False)
    return _save(fig, out, f"{slug}-bus.png")


def chart_heatmap(df: pd.DataFrame, out: Path, slug: str) -> str | None:
    if df.empty:
        return None
    grid = df.pivot_table(
        index="iso_dow", columns="hour", values="commits", aggfunc="sum", fill_value=0
    )
    grid = grid.reindex(index=range(1, 8), columns=range(24), fill_value=0)
    fig, ax = plt.subplots(figsize=(8, 2.4))
    ax.imshow(grid.values, aspect="auto", cmap="Blues", vmin=0)
    ax.set_yticks(range(7), ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
    ax.set_xticks(range(0, 24, 3), [f"{h:02d}" for h in range(0, 24, 3)])
    ax.set_title("When commits happen (UTC)")
    ax.grid(False)
    for s in ax.spines.values():
        s.set_visible(False)
    return _save(fig, out, f"{slug}-heatmap.png")


def fmt_hours(h: float | None) -> str:
    if h is None or (isinstance(h, float) and math.isnan(h)):
        return "—"
    h = float(h)
    return f"{h * 60:.0f} min" if h < 1 else f"{h:.1f} h" if h < 48 else f"{h / 24:.1f} d"


def build(engine: sa.Engine, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary = metrics.summary(engine)
    repos = []
    # NaN -> None so templates render "—" instead of "nan".
    for row in summary.astype(object).where(summary.notna(), None).to_dict("records"):
        repo = str(row["repo"])
        slug = repo.replace("/", "--")
        charts = [
            c
            for c in (
                chart_activity(metrics.read(engine, "weekly_activity", repo), out, slug),
                chart_cycle(metrics.read(engine, "pr_cycle", repo), out, slug),
                chart_hotspots(metrics.read(engine, "hotspots", repo), out, slug),
                chart_bus_factor(metrics.read(engine, "bus_factor", repo), out, slug),
                chart_heatmap(metrics.read(engine, "activity_heatmap", repo), out, slug),
            )
            if c
        ]
        contributors = (
            metrics.read(engine, "contributors", repo)
            .sort_values(["commits", "prs"], ascending=False)
            .head(8)
        )
        repos.append(
            {**row, "slug": slug, "charts": charts, "people": contributors.to_dict("records")}
        )
    env = Environment(
        loader=PackageLoader("gitpulse", "templates"), autoescape=select_autoescape(["html"])
    )
    env.filters["hours"] = fmt_hours
    env.filters["pct"] = lambda v: (
        "—" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{float(v) * 100:.0f}%"
    )
    html = env.get_template("report.html").render(repos=repos, generated=dt.datetime.now(dt.UTC))
    (out / "index.html").write_text(html)
    return out / "index.html"
