"""Stage 6, REPORT: rebuild docs/evidence.md and its chart from every month in outputs/.

The evidence page is generated, never hand-edited, so it always matches the
committed outputs. Each month carries its publish decision next to its
numbers, so a HOLD month cannot be read as if it were clean.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from .config import Config  # noqa: E402
from .io_utils import write_text  # noqa: E402

BOROUGHS = ["Bronx", "Brooklyn", "Manhattan", "Queens", "Staten Island"]
# Validated categorical slots 1-3 (blue, orange, aqua); aqua is under 3:1 on white,
# so every bar also carries a direct value label and the same numbers are in a table.
MONTH_COLORS = ["#2a78d6", "#eb6834", "#1baf7a"]
INK, INK_MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"


def _pct(x) -> str:
    return "n/a" if pd.isna(x) else f"{100 * x:.1f}%"


def _num(x) -> str:
    return f"{int(x):,}"


def _table(df: pd.DataFrame) -> str:
    head = "| " + " | ".join(df.columns) + " |"
    sep = "|" + "|".join("---" for _ in df.columns) + "|"
    rows = ["| " + " | ".join(str(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join([head, sep, *rows])


def load_months(outputs: Path) -> list[dict]:
    months = []
    for folder in sorted(outputs.glob("month=*")):
        manifest = json.loads((folder / "run_manifest.json").read_text(encoding="utf-8"))
        recon = json.loads((folder / "reconciliation.json").read_text(encoding="utf-8"))
        months.append({
            "month": manifest["month"], "folder": folder, "manifest": manifest,
            "recon": {c["check"].split(".")[0]: c for c in recon["checks"]},
            "borough": pd.read_csv(folder / "metrics_borough.csv"),
            "wav": pd.read_csv(folder / "metrics_wav.csv"),
            "zone": pd.read_csv(folder / "metrics_zone.csv"),
        })
    return months


def _chart(months: list[dict], destination: Path) -> None:
    fig, ax = plt.subplots(figsize=(8.2, 4.6), dpi=150)
    fig.patch.set_facecolor("#ffffff")
    rows = ["Citywide", *BOROUGHS]
    n = len(months)
    height = 0.8 / n
    top_value = 0.0
    for i, m in enumerate(months):
        b = m["borough"].query("licensee == 'All'").set_index("pickup_borough")["long_wait_rate"]
        values = [b.get(r) for r in rows]
        top_value = max([top_value, *values])
        ys = [r_idx + (i - (n - 1) / 2) * height for r_idx in range(len(rows))]
        hold = m["manifest"]["publish_decision"] == "HOLD"
        label = f"{m['month']}" + ("  (HOLD: trips missing)" if hold else "")
        ax.barh(ys, values, height=height - 0.04, color=MONTH_COLORS[i % 3], label=label,
                hatch="////" if hold else None, edgecolor="#ffffff", linewidth=0.8)
        for y, v in zip(ys, values):
            ax.text(v + 0.002, y, f"{100 * v:.1f}%", va="center", ha="left", fontsize=7.5, color=INK_MUTED)
    ax.set_yticks(range(len(rows)), rows, fontsize=9, color=INK)
    ax.invert_yaxis()
    ax.xaxis.set_major_locator(matplotlib.ticker.MultipleLocator(0.02))
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
    ax.tick_params(axis="x", labelsize=8, colors=INK_MUTED, length=0)
    ax.tick_params(axis="y", length=0)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.set_xlim(0, top_value * 1.12)
    ax.set_title("Share of HVFHV trips with a request-to-pickup wait over 10 minutes",
                 fontsize=10.5, color=INK, loc="left", pad=28)
    ax.legend(frameon=False, fontsize=8, loc="lower left", bbox_to_anchor=(0, 1.0), ncol=n,
              handlelength=1.4, columnspacing=1.6, borderaxespad=0.2)
    fig.tight_layout()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(destination, facecolor="#ffffff", metadata={"Software": None})
    plt.close(fig)


def build_evidence(cfg: Config, logger: logging.Logger) -> Path | None:
    months = load_months(cfg.path("outputs"))
    if not months:
        logger.warning("Report | no months in outputs/, nothing to report")
        return None
    docs = cfg.path("docs")
    kpi_min = cfg["kpi"]["long_wait_minutes"]
    _chart(months, docs / "img" / "long_wait_by_borough.png")

    status_rows = []
    for m in months:
        man, c01, c02 = m["manifest"], m["recon"].get("C01", {}), m["recon"].get("C02", {})
        impact = man.get("kpi_impact_of_missing_trips")
        status_rows.append({
            "Month": m["month"], "Decision": f"**{man['publish_decision']}**",
            "Trips in file": _num(man["row_counts"]["scanned"]),
            "vs TLC zone report": f"{c01.get('status', 'n/a')} ({c01['delta_pct']:+.2f}%)" if "delta_pct" in c01
            else c01.get("status", "n/a"),
            "vs TLC base report": c02.get("status", "n/a"),
            "Usable for KPI": _pct(man["row_counts"]["kpi_measurable"] / man["row_counts"]["scanned"]),
            "KPI range if missing trips counted": (f"{_pct(impact['lower_bound'])} to {_pct(impact['upper_bound'])}"
                                                   if impact else "not needed"),
        })

    def city(m, licensee="All"):
        return m["borough"].set_index(["licensee", "pickup_borough"]).loc[(licensee, "Citywide")]

    def wav(m, req):
        return m["wav"].set_index(["licensee", "rider_request"]).loc[("All", req)]

    metric_rows = []
    for label, fn in [
        (f"M1 Long-wait rate (> {kpi_min} min), citywide", lambda m: _pct(city(m).long_wait_rate)),
        ("M1 sensitivity: > 5 min / > 15 min",
         lambda m: f"{_pct(city(m).long_wait_rate_5min)} / {_pct(city(m).long_wait_rate_15min)}"),
        ("M1 by licensee: Uber / Lyft",
         lambda m: f"{_pct(city(m, 'Uber').long_wait_rate)} / {_pct(city(m, 'Lyft').long_wait_rate)}"),
        ("M2 Median / P90 wait (min)", lambda m: f"{city(m).median_wait_min:.2f} / {city(m).p90_wait_min:.2f}"),
        ("M3 Median driver arrival / boarding (min)",
         lambda m: f"{city(m).median_arrival_min:.2f} / {city(m).median_boarding_min:.2f}"),
        ("M4 Long-wait rate: WAV requested / standard",
         lambda m: f"{_pct(wav(m, 'WAV requested').long_wait_rate)} / {_pct(wav(m, 'Standard').long_wait_rate)}"),
        ("M4 WAV trips whose wait is unmeasurable",
         lambda m: _pct(1 - wav(m, "WAV requested").measured_share)),
        ("M5 Trips usable for the KPI", lambda m: _pct(city(m).measured_share)),
        ("M5 File vs TLC published pickups",
         lambda m: f"{m['recon']['C01']['delta_pct']:+.2f}%" if "delta_pct" in m["recon"].get("C01", {}) else "UNKNOWN"),
    ]:
        metric_rows.append({"Metric": label, **{m["month"]: fn(m) for m in months}})

    borough_rows = []
    for b in BOROUGHS:
        row = {"Pickup borough": b}
        for m in months:
            r = m["borough"].set_index(["licensee", "pickup_borough"]).loc[("All", b)]
            row[m["month"]] = f"{_pct(r.long_wait_rate)} (P90 {r.p90_wait_min:.1f} min)"
        borough_rows.append(row)

    min_trips = 5000
    zones = pd.concat(m["zone"] for m in months)
    zp = zones[zones.trips_measured >= min_trips].pivot_table(
        index=["zone_id", "borough", "zone"], columns="month", values="long_wait_rate").dropna()
    zp["mean"] = zp.mean(axis=1)
    top = zp.sort_values("mean", ascending=False).head(10).reset_index()
    zone_rows = pd.DataFrame({
        "Zone": top.zone, "Borough": top.borough,
        **{m["month"]: top[m["month"]].map(_pct) for m in months}})

    latest = months[-1]
    lines = [
        "# Evidence: NYC HVFHV rider wait times",
        "",
        "Generated by `pipeline/report.py` from the committed outputs in `outputs/month=*/`. "
        "Do not edit by hand; rerun the pipeline instead. Metric definitions: [metrics.md](metrics.md). "
        "Validation rules: [validation_rules.md](validation_rules.md).",
        "",
        "## 1. Can each month be published?",
        "",
        _table(pd.DataFrame(status_rows)),
        "",
        "PUBLISH: every deciding check passed. PUBLISH WITH CAVEATS: a check is WARN or UNKNOWN "
        "(usually a TLC report not yet released). HOLD: the file disagrees with TLC's own published "
        "counts beyond the agreed tolerance; the numbers exist for investigation but should not be "
        "quoted as the month's result.",
        "",
        "## 2. Final evidence table (citywide)",
        "",
        _table(pd.DataFrame(metric_rows)),
        "",
        "## 3. Project KPI by pickup borough",
        "",
        f"![Long-wait rate by borough and month](img/long_wait_by_borough.png)",
        "",
        _table(pd.DataFrame(borough_rows)),
        "",
        f"## 4. Zones with the highest long-wait rate in every month (at least {min_trips:,} measured trips)",
        "",
        "These are the candidates to raise in licensee reviews. Airport zones need their own "
        "reading: riders walk to a designated pickup area, so part of the wait is not driver supply.",
        "",
        _table(zone_rows),
        "",
        "## 5. Run provenance",
        "",
        _table(pd.DataFrame([{
            "Month": m["month"], "Run id": f"`{m['manifest']['run_id']}`",
            "Git commit": f"`{m['manifest']['git_commit']}`", "Config": f"`{m['manifest']['config_fingerprint']}`",
            "Trip file SHA-256": f"`{m['manifest']['inputs']['trip_file']['sha256'][:16]}...`",
        } for m in months])),
        "",
        f"Latest month in this report: {latest['month']}.",
        "",
    ]
    destination = docs / "evidence.md"
    write_text("\n".join(lines), destination)
    logger.info("Report | wrote %s and chart for months=%s", destination, [m["month"] for m in months])
    return destination
