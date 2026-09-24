"""Build the test fixture: a small sample of REAL rows from the June 2026 HVFHV file.

The sample is a seeded random draw plus every kind of anomaly the rules look
for, taken from the real file (not invented), so the tests exercise the rules
on the data's actual quirks. Run once after downloading June:
    python tests/make_fixture.py
"""
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/raw/tlc/fhvhv/month=2026-06/fhvhv_tripdata_2026-06.parquet"
ZONES = ROOT / "data/raw/reference/taxi_zone_lookup.csv"
OUT = Path(__file__).parent / "fixtures"

con = duckdb.connect()
con.execute(f"CREATE VIEW t AS SELECT * FROM read_parquet('{SOURCE.as_posix()}', hive_partitioning = false)")
wait = "date_diff('second', request_datetime, pickup_datetime)"
dur = "date_diff('second', pickup_datetime, dropoff_datetime)"
targeted = {
    "pickup_before_request": f"{wait} < 0",
    "wait_over_cap": f"{wait} > 3600",
    "dropoff_not_after_pickup": f"{dur} <= 0",
    "implausible_trip": f"trip_miles <= 0 OR {dur} > 4 * 3600",
    "pickup_zone_unknown": "PULocationID IN (264, 265)",
    "on_scene_out_of_order": "on_scene_datetime < request_datetime OR on_scene_datetime > pickup_datetime",
    "trip_time_mismatch": f"abs(trip_time - {dur}) > 60",
    "wav_requested": "wav_request_flag = 'Y'",
}
parts = ["SELECT * FROM (SELECT * FROM t USING SAMPLE reservoir(3000 ROWS) REPEATABLE (42))"]
parts += [f"SELECT * FROM (SELECT * FROM t WHERE {cond} ORDER BY pickup_datetime LIMIT 25)" for cond in targeted.values()]
OUT.mkdir(parents=True, exist_ok=True)
con.execute(f"""
    COPY (SELECT DISTINCT * FROM ({' UNION ALL '.join(parts)}) ORDER BY pickup_datetime, request_datetime)
    TO '{(OUT / "fhvhv_sample_2026-06.parquet").as_posix()}' (FORMAT parquet)
""")
(OUT / "taxi_zone_lookup.csv").write_bytes(ZONES.read_bytes())
print(con.execute(f"SELECT count(*) FROM '{(OUT / 'fhvhv_sample_2026-06.parquet').as_posix()}'").fetchone())
