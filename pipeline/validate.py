"""Stage 2, VALIDATE: decide whether this month's data can support the KPI.

Nothing is deleted or silently fixed here. Dataset-level checks either stop
the run (FAIL) or are reported (PASS/WARN/UNKNOWN). Trip-level rules become
flag columns; a flagged trip stays in the silver table and is only held out
of the metrics its flag makes untrustworthy. Every rule, threshold and
action is listed in docs/validation_rules.md.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import duckdb

from .config import Config

PASS, WARN, FAIL, UNKNOWN = "PASS", "WARN", "FAIL", "UNKNOWN"
SEVERITY_ORDER = {PASS: 0, UNKNOWN: 1, WARN: 2, FAIL: 3}

# Schema contract: columns the KPI and model cannot do without, and the type
# family each must have. Anything missing or retyped stops the run.
REQUIRED_COLUMNS = {
    "hvfhs_license_num": "VARCHAR",
    "dispatching_base_num": "VARCHAR",
    "request_datetime": "TIMESTAMP",
    "on_scene_datetime": "TIMESTAMP",
    "pickup_datetime": "TIMESTAMP",
    "dropoff_datetime": "TIMESTAMP",
    "PULocationID": "INTEGER",
    "DOLocationID": "INTEGER",
    "trip_miles": "DOUBLE",
    "trip_time": "INTEGER",
    "wav_request_flag": "VARCHAR",
    "wav_match_flag": "VARCHAR",
    "shared_request_flag": "VARCHAR",
    "access_a_ride_flag": "VARCHAR",
}
TYPE_FAMILIES = {
    "VARCHAR": {"VARCHAR"},
    "TIMESTAMP": {"TIMESTAMP", "TIMESTAMP_NS", "TIMESTAMP_US", "TIMESTAMP_MS", "TIMESTAMP WITH TIME ZONE"},
    "INTEGER": {"INTEGER", "BIGINT", "SMALLINT", "HUGEINT", "UBIGINT", "UINTEGER"},
    "DOUBLE": {"DOUBLE", "FLOAT", "DECIMAL", "REAL"},
}
FLAG_COLUMNS = ["wav_request_flag", "wav_match_flag", "shared_request_flag", "shared_match_flag", "access_a_ride_flag"]
UNKNOWN_ZONES = (264, 265)  # TLC lookup: 264 "Unknown", 265 "Outside of NYC"


class ValidationError(Exception):
    """A check that makes the month unusable. Maps to exit code 2."""


@dataclass(frozen=True)
class TripRule:
    rule_id: str
    name: str
    plain_english: str
    sql: str          # condition that marks a trip as violating the rule
    scope: str        # which metrics the flag removes the trip from
    action: str


def trip_rules(cfg: Config) -> list[TripRule]:
    v = cfg["validation"]
    kpi = "wait KPI and all wait metrics"
    return [
        TripRule("R01", "missing_timestamp",
                 "Request, pickup and dropoff times must all be present.",
                 "request_datetime IS NULL OR pickup_datetime IS NULL OR dropoff_datetime IS NULL",
                 kpi, "Quarantine; wait cannot be measured."),
        TripRule("R02", "pickup_outside_month",
                 "A trip belongs to the month file its pickup falls in.",
                 "pickup_datetime < TIMESTAMP '{month_start}' OR pickup_datetime >= TIMESTAMP '{month_end}'",
                 kpi, "Exclude from this month; report the count."),
        TripRule("R03", "pickup_before_request",
                 "A rider cannot be picked up before requesting the ride.",
                 "wait_s < 0",
                 kpi, "Quarantine, do not clip to zero. Likely pre-scheduled rides whose request time "
                      "is the booked time; owner to confirm."),
        TripRule("R04", "wait_over_cap",
                 f"A request-to-pickup gap above {v['max_wait_minutes']} minutes is not an on-demand wait.",
                 f"wait_s > {int(v['max_wait_minutes']) * 60}",
                 kpi, "Hold out of the KPI; likely reservation or data error; report the count."),
        TripRule("R05", "dropoff_not_after_pickup",
                 "Dropoff must be after pickup.",
                 "duration_s <= 0",
                 kpi, "Quarantine; chronology is broken so the record is untrustworthy."),
        TripRule("R06", "implausible_trip",
                 f"A real trip has positive miles, lasts under {v['max_trip_hours']} hours and averages "
                 f"under {v['max_speed_mph']} mph.",
                 f"trip_miles <= 0 OR duration_s > {int(v['max_trip_hours']) * 3600} "
                 f"OR trip_miles / nullif(duration_s / 3600.0, 0) > {v['max_speed_mph']}",
                 kpi, "Quarantine; the record may not describe a real passenger trip."),
        TripRule("R07", "pickup_zone_unknown",
                 "Pickup zone must be a known NYC taxi zone (not 264 Unknown / 265 Outside NYC).",
                 f"PULocationID IS NULL OR PULocationID IN {UNKNOWN_ZONES} "
                 "OR PULocationID NOT IN (SELECT location_id FROM dim_zone)",
                 "borough and zone breakdowns (still counted citywide)", "Keep citywide; no borough."),
        TripRule("R08", "on_scene_out_of_order",
                 "Driver arrival (on scene) must fall between request and pickup.",
                 "on_scene_datetime IS NULL OR on_scene_datetime < request_datetime "
                 "OR on_scene_datetime > pickup_datetime",
                 "stage-split metric only", "Exclude from arrival/boarding split."),
        TripRule("R09", "trip_time_mismatch",
                 f"trip_time should equal dropoff minus pickup within {v['trip_time_tolerance_seconds']} seconds.",
                 f"abs(trip_time - duration_s) > {int(v['trip_time_tolerance_seconds'])}",
                 "none (trip_time is not used; duration comes from timestamps)",
                 "Report only; ask owner what trip_time measures."),
        TripRule("R10", "unknown_license",
                 "License number must be one of the HVFHS licensees in the TLC dictionary.",
                 "hvfhs_license_num IS NULL OR hvfhs_license_num NOT IN ({licenses})",
                 kpi, "Quarantine; the trip cannot be attributed."),
    ]


KPI_RULES = ["R01", "R02", "R03", "R04", "R05", "R06", "R10"]


def _check(check: str, status: str, detail: str, **extra) -> dict:
    return {"check": check, "status": status, "detail": detail, **extra}


def check_schema(con: duckdb.DuckDBPyConnection, logger: logging.Logger) -> tuple[dict, dict]:
    """Schema contract. Raises ValidationError when a required column is missing or retyped."""
    actual = {row[0]: row[1] for row in con.execute("DESCRIBE source_trips").fetchall()}
    missing = sorted(set(REQUIRED_COLUMNS) - set(actual))
    retyped = sorted(c for c, fam in REQUIRED_COLUMNS.items()
                     if c in actual and actual[c] not in TYPE_FAMILIES[fam])
    extra = sorted(set(actual) - set(REQUIRED_COLUMNS))
    drift = {"columns": actual, "missing_required": missing, "retyped_required": retyped,
             "optional_columns": extra}
    if missing or retyped:
        detail = f"missing required columns: {missing}; wrong type: {retyped}"
        logger.error("Validation | check=schema_contract status=FAIL detail=%s", detail)
        raise ValidationError(f"schema contract broken: {detail}")
    result = _check("D01.schema_contract", PASS,
                    f"{len(REQUIRED_COLUMNS)} required columns present with expected types; "
                    f"{len(extra)} optional columns: {', '.join(extra)}")
    return result, drift


def check_row_count(con: duckdb.DuckDBPyConnection, footer_rows: int) -> tuple[dict, int]:
    scanned = con.execute("SELECT count(*) FROM source_trips").fetchone()[0]
    if scanned != footer_rows:
        raise ValidationError(f"rows scanned ({scanned}) differ from Parquet footer ({footer_rows})")
    return _check("D02.footer_row_count", PASS, f"scanned {scanned} rows = Parquet footer {footer_rows}"), scanned


def check_zone_lookup(con: duckdb.DuckDBPyConnection) -> dict:
    n, distinct_ids, boroughs = con.execute(
        "SELECT count(*), count(DISTINCT location_id), count(DISTINCT borough) FROM dim_zone").fetchone()
    if n != 265 or distinct_ids != 265:
        raise ValidationError(f"zone lookup should have 265 unique zones, found {n} rows / {distinct_ids} ids")
    return _check("D03.zone_lookup", PASS, f"265 unique zones across {boroughs} borough labels")


def check_duplicates(con: duckdb.DuckDBPyConnection) -> dict:
    """Trips have no ID in the source, so a duplicate is a row identical in every column."""
    columns = ", ".join(f'"{r[0]}"' for r in con.execute("DESCRIBE source_trips").fetchall())
    dupes = con.execute(f"""
        SELECT coalesce(sum(n - 1), 0) FROM (
            SELECT count(*) AS n FROM source_trips GROUP BY {columns} HAVING count(*) > 1)
    """).fetchone()[0]
    status = PASS if dupes == 0 else WARN
    return _check("D04.exact_duplicate_rows", status, f"{int(dupes)} extra copies of identical rows", count=int(dupes))


def check_flag_domain(con: duckdb.DuckDBPyConnection) -> dict:
    present = [c for c in FLAG_COLUMNS
               if c in {r[0] for r in con.execute("DESCRIBE source_trips").fetchall()}]
    parts = " + ".join(f"count(*) FILTER (WHERE {c} IS NULL OR {c} NOT IN ('Y', 'N'))" for c in present)
    bad = con.execute(f"SELECT {parts} FROM source_trips").fetchone()[0]
    status = PASS if bad == 0 else WARN
    return _check("D05.flag_domain", status, f"{bad} flag values outside Y/N across {len(present)} flag columns",
                  count=int(bad))


def check_daily_volume(con: duckdb.DuckDBPyConnection, month_start: str, month_end: str, dip_ratio: float) -> dict:
    """Internal completeness signal: a licensee's day far below its usual volume for that weekday."""
    rows = con.execute(f"""
        WITH daily AS (
            SELECT hvfhs_license_num AS license, pickup_datetime::DATE AS day, count(*) AS trips
            FROM source_trips
            WHERE pickup_datetime >= TIMESTAMP '{month_start}' AND pickup_datetime < TIMESTAMP '{month_end}'
            GROUP BY ALL),
        typical AS (
            SELECT *, median(trips) OVER (PARTITION BY license, dayofweek(day)) AS weekday_median FROM daily)
        SELECT license, day::VARCHAR, trips, weekday_median::BIGINT, round(trips / weekday_median, 3) AS ratio
        FROM typical WHERE trips < {dip_ratio} * weekday_median ORDER BY license, day
    """).fetchall()
    dips = [{"license": r[0], "day": r[1], "trips": r[2], "weekday_median": r[3], "ratio": r[4]} for r in rows]
    shortfall = int(sum(d["weekday_median"] - d["trips"] for d in dips))
    status = PASS if not dips else WARN
    detail = (f"{len(dips)} licensee-days below {dip_ratio:.0%} of their weekday median; "
              f"estimated shortfall {shortfall} trips")
    return _check("D06.daily_volume_dips", status, detail, dips=dips, estimated_shortfall=shortfall)


def build_flagged_view(con: duckdb.DuckDBPyConnection, cfg: Config, month_start: str, month_end: str) -> None:
    """Create trips_flagged: every source trip plus derived durations and one boolean per rule."""
    licenses = ", ".join(f"'{k}'" for k in cfg["licenses"])
    flag_sql = ",\n    ".join(
        # NULL comparisons count as "not violated"; rules that care about NULLs test them explicitly
        f"coalesce(({r.sql.format(month_start=month_start, month_end=month_end, licenses=licenses)}), false) "
        f"AS f_{r.rule_id.lower()}_{r.name}"
        for r in trip_rules(cfg))
    kpi_cols = [f"f_{r.rule_id.lower()}_{r.name}" for r in trip_rules(cfg) if r.rule_id in KPI_RULES]
    con.execute(f"""
        CREATE OR REPLACE VIEW trips_derived AS
        SELECT *,
            date_diff('second', request_datetime, pickup_datetime)  AS wait_s,
            date_diff('second', request_datetime, on_scene_datetime) AS arrival_s,
            date_diff('second', on_scene_datetime, pickup_datetime)  AS boarding_s,
            date_diff('second', pickup_datetime, dropoff_datetime)   AS duration_s
        FROM source_trips
    """)
    con.execute(f"""
        CREATE OR REPLACE VIEW trips_flagged AS
        SELECT *, NOT ({' OR '.join(kpi_cols)}) AS in_kpi FROM (
            SELECT *,
            {flag_sql}
            FROM trips_derived)
    """)


def summarise_trip_rules(con: duckdb.DuckDBPyConnection, cfg: Config, total_rows: int) -> list[dict]:
    rules = trip_rules(cfg)
    cols = [f"f_{r.rule_id.lower()}_{r.name}" for r in rules]
    counts = con.execute(
        "SELECT " + ", ".join(f"count(*) FILTER (WHERE {c})" for c in cols) + " FROM trips_flagged").fetchone()
    results = []
    for rule, n in zip(rules, counts):
        results.append(_check(f"{rule.rule_id}.{rule.name}", PASS if n == 0 else WARN,
                              f"{n} trips ({n / total_rows:.3%}) flagged; scope: {rule.scope}",
                              count=int(n), share=round(n / total_rows, 6), action=rule.action))
    return results


def kpi_gate(con: duckdb.DuckDBPyConnection, cfg: Config, total_rows: int) -> dict:
    """The gate: is enough of the month measurable for the wait KPI to mean something?"""
    usable = con.execute("SELECT count(*) FILTER (WHERE in_kpi) FROM trips_flagged").fetchone()[0]
    share = usable / total_rows if total_rows else 0.0
    v = cfg["validation"]
    status = PASS if share >= v["min_valid_share_pass"] else WARN if share >= v["min_valid_share_warn"] else FAIL
    return _check("G01.kpi_population_share", status,
                  f"{usable} of {total_rows} trips ({share:.2%}) usable for the wait KPI "
                  f"(PASS >= {v['min_valid_share_pass']:.0%}, FAIL < {v['min_valid_share_warn']:.0%})",
                  count=int(usable), share=round(share, 6))


def overall_status(checks: list[dict]) -> str:
    return max((c["status"] for c in checks), key=SEVERITY_ORDER.__getitem__, default=PASS)
