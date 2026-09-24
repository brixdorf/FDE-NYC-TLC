"""Stage 5, RECONCILE: does the trip file agree with what TLC itself reports?

A byte-perfect download proves we received the file TLC published. It does
not prove the file holds every trip. TLC publishes two independent monthly
aggregates through the NYC Open Data API; we compare our counts with both.
Each side is aggregated to the same grain before the join.

If the API is unreachable or the aggregate for this month is not published
yet, the check is UNKNOWN and the run continues in a degraded state: the
trip data itself is still valid, only the cross-check is missing.
"""
from __future__ import annotations

import logging

import duckdb
import pandas as pd

from .config import Config
from .extract import HttpClient, RetrievalError, fetch_socrata
from .validate import FAIL, PASS, UNKNOWN, WARN


def _status(delta_pct: float, cfg: Config) -> str:
    r = cfg["reconciliation"]
    d = abs(delta_pct)
    return PASS if d <= r["total_delta_pass_pct"] else WARN if d <= r["total_delta_warn_pct"] else FAIL


def zone_reconciliation(con: duckdb.DuckDBPyConnection, cfg: Config, month: str, client: HttpClient,
                        logger: logging.Logger, base_url: str | None = None) -> tuple[dict, pd.DataFrame | None]:
    dataset = cfg["sources"]["zone_counts_dataset"]
    where = (f"metric_month = '{month}-01' AND industry = '{cfg['project']['industry_label']}' "
             "AND pickup_dropoff = 'Pick-up'")
    out_dir = cfg.path("raw") / "api" / dataset / f"month={month}"
    check = "C01.pickups_vs_tlc_zone_report"
    try:
        rows, meta = fetch_socrata(dataset, where, "locationid", out_dir, cfg, client, logger, base_url)
    except RetrievalError as exc:
        logger.warning("Reconcile | check=%s status=UNKNOWN reason=%s", check, exc)
        return {"check": check, "status": UNKNOWN, "detail": f"TLC aggregate unavailable: {exc}"}, None
    if not rows:
        return {"check": check, "status": UNKNOWN,
                "detail": f"TLC has not published the {month} zone aggregate yet"}, None

    published = pd.DataFrame(rows)[["locationid", "trip_count"]].astype({"locationid": int, "trip_count": int})
    con.register("published_zone_counts", published)
    by_zone = con.execute("""
        WITH ours AS (SELECT pu_zone_id AS zone_id, count(*) AS file_trips FROM fact_trip GROUP BY 1),
             theirs AS (SELECT locationid AS zone_id, sum(trip_count) AS tlc_trips
                        FROM published_zone_counts GROUP BY 1)
        SELECT coalesce(o.zone_id, t.zone_id) AS zone_id, z.borough, z.zone,
               coalesce(o.file_trips, 0) AS file_trips, coalesce(t.tlc_trips, 0) AS tlc_trips,
               coalesce(o.file_trips, 0) - coalesce(t.tlc_trips, 0) AS delta
        FROM ours o FULL OUTER JOIN theirs t ON o.zone_id = t.zone_id
        LEFT JOIN dim_zone z ON z.location_id = coalesce(o.zone_id, t.zone_id)
        ORDER BY zone_id
    """).df()
    con.unregister("published_zone_counts")

    file_total, tlc_total = int(by_zone.file_trips.sum()), int(by_zone.tlc_trips.sum())
    delta_pct = 100.0 * (file_total - tlc_total) / tlc_total
    r = cfg["reconciliation"]
    big = by_zone[(by_zone.tlc_trips >= r["min_zone_trips"])
                  & ((by_zone.delta.abs() / by_zone.tlc_trips * 100) > r["zone_delta_warn_pct"])]
    status = _status(delta_pct, cfg)
    result = {
        "check": check, "status": status,
        "detail": (f"file {file_total} vs TLC published {tlc_total} pickups: delta {file_total - tlc_total:+d} "
                   f"({delta_pct:+.3f}%); {len(big)} zones differ by more than {r['zone_delta_warn_pct']}%"),
        "file_trips": file_total, "tlc_published_trips": tlc_total, "delta": file_total - tlc_total,
        "delta_pct": round(delta_pct, 4), "zones_compared": int(len(by_zone)),
        "zones_over_threshold": int(len(big)), "api_rows": meta["rows_received"],
    }
    logger.info("Reconcile | check=%s status=%s detail=%s", check, status, result["detail"])
    return result, by_zone


def base_reconciliation(con: duckdb.DuckDBPyConnection, cfg: Config, month: str, client: HttpClient,
                        logger: logging.Logger, base_url: str | None = None) -> dict:
    """Per-licensee check against the FHV Base Aggregate Report.

    That report lists the HV licensees under their brand names (UBER, LYFT);
    matching brand to row is an observed convention, recorded as an assumption.
    """
    dataset = cfg["sources"]["base_aggregate_dataset"]
    year, mon = month.split("-")
    brands = [b.upper() for b in cfg["licenses"].values()]
    where = (f"year = '{year}' AND month = '{int(mon)}' AND base_license_number IN "
             f"({', '.join(repr(b) for b in brands)})")
    out_dir = cfg.path("raw") / "api" / dataset / f"month={month}"
    check = "C02.trips_vs_tlc_base_report"
    try:
        rows, _ = fetch_socrata(dataset, where, "base_license_number", out_dir, cfg, client, logger, base_url)
    except RetrievalError as exc:
        logger.warning("Reconcile | check=%s status=UNKNOWN reason=%s", check, exc)
        return {"check": check, "status": UNKNOWN, "detail": f"TLC aggregate unavailable: {exc}"}
    if not rows:
        result = {"check": check, "status": UNKNOWN,
                  "detail": f"TLC has not published the {month} base report yet (it lags the trip files)"}
        logger.info("Reconcile | check=%s status=UNKNOWN detail=%s", check, result["detail"])
        return result

    ours = dict(con.execute("SELECT upper(brand), count(*) FROM fact_trip GROUP BY 1").fetchall())
    per_licensee, worst = [], PASS
    for row in rows:
        brand, tlc = row["base_license_number"], int(row["total_dispatched_trips"])
        file_trips = int(ours.get(brand, 0))
        pct = 100.0 * (file_trips - tlc) / tlc
        status = _status(pct, cfg)
        worst = max(worst, status, key=[PASS, UNKNOWN, WARN, FAIL].index)
        per_licensee.append({"licensee": brand, "file_trips": file_trips, "tlc_published_trips": tlc,
                             "delta": file_trips - tlc, "delta_pct": round(pct, 4), "status": status})
    detail = "; ".join(f"{p['licensee']} {p['delta']:+d} ({p['delta_pct']:+.3f}%)" for p in per_licensee)
    logger.info("Reconcile | check=%s status=%s detail=%s", check, worst, detail)
    return {"check": check, "status": worst, "detail": detail, "per_licensee": per_licensee}
