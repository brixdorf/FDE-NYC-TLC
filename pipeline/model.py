"""Stage 3, MODEL: reorganise the source rows around the rider's workflow.

    request_datetime -> on_scene_datetime -> pickup_datetime -> dropoff_datetime
         |<----- arrival ----->|<- boarding ->|<------ in trip ------>|
         |<------------- wait --------------->|

Tables (DuckDB, SQL):
  dim_zone     one row per TLC taxi zone (265), from the zone lookup CSV
  dim_license  one row per HVFHS licensee, from the TLC data dictionary
  fact_trip    one row per trip record: workflow timestamps, stage durations,
               zone/borough, rider requests (WAV, shared) and every
               validation flag. Written to data/silver/month=YYYY-MM/.
"""
from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from .config import Config
from .validate import trip_rules


def connect(threads: int | None = None) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    if threads:
        con.execute(f"SET threads = {int(threads)}")
    return con


def register_sources(con: duckdb.DuckDBPyConnection, trip_path: Path, zone_path: Path, cfg: Config,
                     drop_column: str | None = None) -> None:
    """Expose the raw files as SQL views. hive_partitioning is off on purpose:
    the month=YYYY-MM folder name would otherwise be injected as a fake column."""
    exclude = f" EXCLUDE ({drop_column})" if drop_column else ""
    con.execute(f"""
        CREATE OR REPLACE VIEW source_trips AS
        SELECT *{exclude} FROM read_parquet('{trip_path.as_posix()}', hive_partitioning = false)
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE dim_zone AS
        SELECT "LocationID"::INTEGER AS location_id, "Borough" AS borough, "Zone" AS zone,
               service_zone
        FROM read_csv('{zone_path.as_posix()}', header = true)
    """)
    rows = ", ".join(f"('{k}', '{v}')" for k, v in cfg["licenses"].items())
    con.execute(f"CREATE OR REPLACE TABLE dim_license AS SELECT * FROM (VALUES {rows}) t(license, brand)")


def build_fact_trip(con: duckdb.DuckDBPyConnection, cfg: Config, month: str, logger: logging.Logger) -> Path:
    """Materialise the silver fact table (one row per trip, nothing dropped)."""
    flags = ", ".join(f"f_{r.rule_id.lower()}_{r.name}" for r in trip_rules(cfg))
    silver = cfg.path("silver") / f"month={month}" / "fact_trip.parquet"
    silver.parent.mkdir(parents=True, exist_ok=True)
    tmp = silver.with_name(silver.name + ".tmp")
    con.execute(f"""
        COPY (
            SELECT
                t.hvfhs_license_num                     AS license,
                l.brand                                 AS brand,
                t.dispatching_base_num                  AS dispatching_base,
                t.request_datetime                      AS requested_at,
                t.on_scene_datetime                     AS on_scene_at,
                t.pickup_datetime                       AS picked_up_at,
                t.dropoff_datetime                      AS dropped_off_at,
                t.wait_s, t.arrival_s, t.boarding_s, t.duration_s,
                t.trip_miles, t.trip_time               AS reported_trip_time_s,
                t.PULocationID                          AS pu_zone_id,
                t.DOLocationID                          AS do_zone_id,
                CASE WHEN f_r07_pickup_zone_unknown THEN NULL ELSE pz.borough END AS pu_borough,
                pz.zone                                 AS pu_zone,
                dz.borough                              AS do_borough,
                t.wav_request_flag = 'Y'                AS wav_requested,
                t.wav_match_flag = 'Y'                  AS wav_vehicle,
                t.shared_request_flag = 'Y'             AS shared_requested,
                t.access_a_ride_flag = 'Y'              AS access_a_ride,
                {flags},
                t.in_kpi,
                t.in_kpi AND NOT f_r07_pickup_zone_unknown                 AS in_borough_kpi,
                t.in_kpi AND NOT f_r08_on_scene_out_of_order               AS in_stage_split
            FROM trips_flagged t
            LEFT JOIN dim_license l ON l.license = t.hvfhs_license_num
            LEFT JOIN dim_zone pz ON pz.location_id = t.PULocationID
            LEFT JOIN dim_zone dz ON dz.location_id = t.DOLocationID
        ) TO '{tmp.as_posix()}' (FORMAT parquet, COMPRESSION zstd)
    """)
    tmp.replace(silver)
    con.execute(f"CREATE OR REPLACE VIEW fact_trip AS SELECT * FROM read_parquet('{silver.as_posix()}')")
    rows = con.execute("SELECT count(*) FROM fact_trip").fetchone()[0]
    logger.info("Model | built fact_trip rows=%s path=%s", rows, silver)
    return silver
