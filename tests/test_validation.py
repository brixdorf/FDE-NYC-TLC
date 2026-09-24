"""Validation rules on real rows, and on real rows mutated into known violations."""
from __future__ import annotations

import logging

import duckdb
import pandas as pd
import pytest

from pipeline import model, validate
from pipeline.runner import month_bounds
from tests.conftest import MONTH, TRIP_FIXTURE, ZONE_FIXTURE

LOG = logging.getLogger("test")


def _flagged(cfg, trip_path=TRIP_FIXTURE, drop_column=None) -> duckdb.DuckDBPyConnection:
    con = model.connect()
    model.register_sources(con, trip_path, ZONE_FIXTURE, cfg, drop_column=drop_column)
    validate.build_flagged_view(con, cfg, *month_bounds(MONTH))
    return con


def test_rule_counts_match_an_independent_pandas_check(cfg):
    con = _flagged(cfg)
    df = pd.read_parquet(TRIP_FIXTURE)
    wait = (df.pickup_datetime - df.request_datetime).dt.total_seconds()
    duration = (df.dropoff_datetime - df.pickup_datetime).dt.total_seconds()
    expected = {
        "f_r03_pickup_before_request": int((wait < 0).sum()),
        "f_r04_wait_over_cap": int((wait > 3600).sum()),
        "f_r05_dropoff_not_after_pickup": int((duration <= 0).sum()),
        "f_r07_pickup_zone_unknown": int(df.PULocationID.isin([264, 265]).sum()),
    }
    for column, count in expected.items():
        assert con.execute(f"SELECT count(*) FILTER (WHERE {column}) FROM trips_flagged").fetchone()[0] == count
    assert all(v > 0 for v in expected.values()), "fixture should contain every targeted anomaly"


def test_flagged_trips_are_kept_not_deleted(cfg):
    con = _flagged(cfg)
    total = con.execute("SELECT count(*) FROM source_trips").fetchone()[0]
    assert con.execute("SELECT count(*) FROM trips_flagged").fetchone()[0] == total
    in_kpi = con.execute("SELECT count(*) FILTER (WHERE in_kpi) FROM trips_flagged").fetchone()[0]
    assert 0 < in_kpi < total


@pytest.mark.parametrize("mutation, flag", [
    ("pickup_datetime = request_datetime - INTERVAL 5 MINUTE", "f_r03_pickup_before_request"),
    ("pickup_datetime = request_datetime + INTERVAL 3 HOUR", "f_r04_wait_over_cap"),
    ("dropoff_datetime = pickup_datetime - INTERVAL 1 MINUTE", "f_r05_dropoff_not_after_pickup"),
    ("trip_miles = 0", "f_r06_implausible_trip"),
    ("PULocationID = 264", "f_r07_pickup_zone_unknown"),
    ("hvfhs_license_num = 'HV9999'", "f_r10_unknown_license"),
    ("pickup_datetime = TIMESTAMP '2026-05-31 23:00:00'", "f_r02_pickup_outside_month"),
])
def test_each_rule_catches_a_mutated_real_row(cfg, tmp_path, mutation, flag):
    column, expression = (part.strip() for part in mutation.split("=", 1))
    mutated = tmp_path / "mutated.parquet"
    duckdb.execute(f"""
        COPY (SELECT * REPLACE ({expression} AS {column})
              FROM (SELECT * FROM '{TRIP_FIXTURE.as_posix()}' WHERE trip_miles > 1 LIMIT 1))
        TO '{mutated.as_posix()}' (FORMAT parquet)""")
    con = _flagged(cfg, mutated)
    violated, in_kpi = con.execute(f"SELECT {flag}, in_kpi FROM trips_flagged").fetchone()
    assert violated
    if flag != "f_r07_pickup_zone_unknown":  # unknown zone stays in the citywide KPI by design
        assert not in_kpi


def test_schema_contract_stops_the_run_when_a_column_disappears(cfg):
    con = model.connect()
    model.register_sources(con, TRIP_FIXTURE, ZONE_FIXTURE, cfg, drop_column="request_datetime")
    with pytest.raises(validate.ValidationError, match="request_datetime"):
        validate.check_schema(con, LOG)


def test_hive_folder_name_is_not_read_as_a_column(cfg, tmp_path):
    partitioned = tmp_path / "month=2026-06" / "trips.parquet"
    partitioned.parent.mkdir()
    partitioned.write_bytes(TRIP_FIXTURE.read_bytes())
    con = model.connect()
    model.register_sources(con, partitioned, ZONE_FIXTURE, cfg)
    columns = {r[0] for r in con.execute("DESCRIBE source_trips").fetchall()}
    assert "month" not in columns
