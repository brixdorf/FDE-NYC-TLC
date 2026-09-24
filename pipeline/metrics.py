"""Stage 4, METRICS: aggregate the silver fact table to the gold outputs.

Every metric is computed only over trips whose validation flags allow it
(in_kpi / in_borough_kpi / in_stage_split). Denominators travel with the
numbers, so a reader always sees how many trips a rate is based on.
Definitions are in docs/metrics.md.
"""
from __future__ import annotations

import duckdb
import pandas as pd

from .config import Config

METRICS = [
    {"id": "M1", "name": "Long-wait rate (project KPI)",
     "formula": "trips with request-to-pickup wait > {kpi} min / KPI-measurable trips",
     "grain": "month x licensee x pickup borough", "type": "Outcome (lagging)"},
    {"id": "M2", "name": "Median and P90 wait",
     "formula": "median and 90th percentile of pickup_datetime - request_datetime, minutes",
     "grain": "month x licensee x pickup borough", "type": "Workflow driver"},
    {"id": "M3", "name": "Wait stage split",
     "formula": "median(on_scene - request) and median(pickup - on_scene), minutes, where on_scene is in order",
     "grain": "month x licensee x pickup borough", "type": "Workflow driver (where the wait sits)"},
    {"id": "M4", "name": "Accessible-ride wait gap",
     "formula": "long-wait rate for WAV-requested trips minus rate for other trips, "
                "shown with the share of WAV trips whose wait cannot be measured",
     "grain": "month x licensee x WAV requested", "type": "Outcome for a priority rider group"},
    {"id": "M5", "name": "Measurable share and reconciliation gap",
     "formula": "KPI-measurable trips / all trips; (file trips - TLC published trips) / TLC published trips",
     "grain": "month (and licensee)", "type": "Data trust (is the month publishable)"},
]


def _minutes(expr: str) -> str:
    return f"round(({expr}) / 60.0, 2)"


def borough_metrics(con: duckdb.DuckDBPyConnection, cfg: Config, month: str) -> pd.DataFrame:
    kpi_min = cfg["kpi"]["long_wait_minutes"]
    sens = ",\n".join(
        f"round(avg((wait_s > {m * 60})::INT) FILTER (WHERE in_kpi), 4) AS long_wait_rate_{m}min"
        for m in cfg["kpi"]["sensitivity_minutes"] if m != kpi_min)
    return con.execute(f"""
        SELECT
            '{month}' AS month,
            CASE WHEN grouping(brand) = 1 THEN 'All' ELSE brand END AS licensee,
            CASE WHEN grouping(pu_borough) = 1 THEN 'Citywide'
                 ELSE coalesce(pu_borough, 'Unknown / outside NYC') END AS pickup_borough,
            count(*) AS trips_total,
            count(*) FILTER (WHERE in_kpi) AS trips_measured,
            round(count(*) FILTER (WHERE in_kpi) / count(*), 4) AS measured_share,
            round(avg((wait_s > {kpi_min * 60})::INT) FILTER (WHERE in_kpi), 4) AS long_wait_rate,
            {sens},
            {_minutes("median(wait_s) FILTER (WHERE in_kpi)")} AS median_wait_min,
            {_minutes("quantile_cont(wait_s, 0.9) FILTER (WHERE in_kpi)")} AS p90_wait_min,
            {_minutes("median(arrival_s) FILTER (WHERE in_stage_split)")} AS median_arrival_min,
            {_minutes("median(boarding_s) FILTER (WHERE in_stage_split)")} AS median_boarding_min,
            round(count(*) FILTER (WHERE in_stage_split) / nullif(count(*) FILTER (WHERE in_kpi), 0), 4)
                AS stage_split_coverage
        FROM fact_trip
        GROUP BY GROUPING SETS ((), (brand), (pu_borough), (brand, pu_borough))
        ORDER BY licensee, pickup_borough
    """).df()


def wav_metrics(con: duckdb.DuckDBPyConnection, cfg: Config, month: str) -> pd.DataFrame:
    kpi_min = cfg["kpi"]["long_wait_minutes"]
    return con.execute(f"""
        SELECT
            '{month}' AS month,
            CASE WHEN grouping(brand) = 1 THEN 'All' ELSE brand END AS licensee,
            CASE WHEN wav_requested THEN 'WAV requested' ELSE 'Standard' END AS rider_request,
            count(*) AS trips_total,
            count(*) FILTER (WHERE in_kpi) AS trips_measured,
            round(count(*) FILTER (WHERE in_kpi) / count(*), 4) AS measured_share,
            round(avg(f_r03_pickup_before_request::INT), 4) AS pickup_before_request_share,
            round(avg((wait_s > {kpi_min * 60})::INT) FILTER (WHERE in_kpi), 4) AS long_wait_rate,
            {_minutes("median(wait_s) FILTER (WHERE in_kpi)")} AS median_wait_min,
            round(avg(wav_vehicle::INT), 4) AS wav_vehicle_share
        FROM fact_trip
        GROUP BY GROUPING SETS ((wav_requested), (brand, wav_requested))
        ORDER BY licensee, rider_request
    """).df()


def zone_metrics(con: duckdb.DuckDBPyConnection, cfg: Config, month: str) -> pd.DataFrame:
    kpi_min = cfg["kpi"]["long_wait_minutes"]
    return con.execute(f"""
        SELECT
            '{month}' AS month, pu_zone_id AS zone_id, pu_borough AS borough, pu_zone AS zone,
            count(*) FILTER (WHERE in_kpi) AS trips_measured,
            round(avg((wait_s > {kpi_min * 60})::INT) FILTER (WHERE in_kpi), 4) AS long_wait_rate,
            {_minutes("median(wait_s) FILTER (WHERE in_kpi)")} AS median_wait_min,
            {_minutes("quantile_cont(wait_s, 0.9) FILTER (WHERE in_kpi)")} AS p90_wait_min
        FROM fact_trip
        WHERE in_borough_kpi
        GROUP BY ALL
        ORDER BY zone_id
    """).df()


def daily_licensee_counts(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.execute("""
        SELECT picked_up_at::DATE AS day, brand AS licensee, count(*) AS trips
        FROM fact_trip WHERE NOT f_r02_pickup_outside_month GROUP BY ALL ORDER BY day, licensee
    """).df()
