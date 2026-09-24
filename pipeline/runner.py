"""Run one month end to end: extract -> validate -> model -> reconcile -> metrics -> publish.

Failure policy (also in README):
  * retrieval cannot be completed        -> RetrievalError, exit 3, outputs untouched
  * validation gate fails                -> ValidationError, exit 2, outputs untouched
  * reconciliation source unavailable    -> check UNKNOWN, run completes as "degraded"
Outputs for a month are staged in a temp folder and swapped in only when
every file is written, so a crash never leaves a half-updated month.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from datetime import date, datetime, timezone
from pathlib import Path

from . import extract, metrics, model, reconcile, validate
from .config import PROJECT_ROOT, Config
from .io_utils import sha256_file, write_csv, write_json
from .validate import FAIL, PASS, UNKNOWN, WARN, ValidationError

CHAOS_SCENARIOS = ("missing_column", "truncated_download", "api_down", "unpublished_month")
UNREACHABLE_API = "http://127.0.0.1:9/resource"  # closed local port: connection refused


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = map(int, month.split("-"))
    nxt = date(year + mon // 12, mon % 12 + 1, 1)
    return f"{year:04d}-{mon:02d}-01 00:00:00", f"{nxt:%Y-%m-%d} 00:00:00"


def _git_commit() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT, capture_output=True,
                             text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "pipeline", "sql", "config", "run_pipeline.py"],
                               cwd=PROJECT_ROOT, capture_output=True, text=True).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def publish_decision(checks: list[dict]) -> tuple[str, list[str]]:
    """Row-level rule WARNs are handled by quarantine and do not block. What decides
    publishability is the KPI gate, internal completeness and the reconciliations."""
    deciding = [c for c in checks if c["check"].split(".")[0] in ("G01", "D06", "C01", "C02")]
    reasons = [f"{c['check']}={c['status']}: {c['detail']}" for c in deciding if c["status"] != PASS]
    if any(c["status"] == FAIL for c in deciding):
        return "HOLD", reasons
    if any(c["status"] in (WARN, UNKNOWN) for c in deciding):
        return "PUBLISH WITH CAVEATS", reasons
    return "PUBLISH", reasons


def kpi_impact_of_missing_trips(zone_check: dict, checks: list[dict], borough, licenses: dict) -> dict | None:
    """If the file holds fewer trips than TLC reports, bound how far that could move the KPI.

    Lower bound: none of the missing trips had a long wait. Upper bound: all did.
    Estimate: if the internal daily check pins the gap on one licensee, assume
    the missing trips look like that licensee's measured trips. Nothing is
    imputed into the metrics; this only sizes the uncertainty for the owner.
    """
    missing = -int(zone_check.get("delta", 0))
    if missing <= 0:
        return None
    city = borough.set_index(["licensee", "pickup_borough"])
    measured = int(city.loc[("All", "Citywide"), "trips_measured"])
    rate = float(city.loc[("All", "Citywide"), "long_wait_rate"])
    long_waits = rate * measured
    result = {"missing_trips": missing, "reported": round(rate, 4),
              "lower_bound": round(long_waits / (measured + missing), 4),
              "upper_bound": round((long_waits + missing) / (measured + missing), 4)}
    dips = next((c.get("dips", []) for c in checks if c["check"].startswith("D06")), [])
    licensees = {d["license"] for d in dips}
    if len(licensees) == 1:
        brand = licenses.get(licensees.pop())
        if brand and (brand, "Citywide") in city.index:
            brand_rate = float(city.loc[(brand, "Citywide"), "long_wait_rate"])
            result["estimate"] = round((long_waits + brand_rate * missing) / (measured + missing), 4)
            result["estimate_basis"] = f"missing trips assumed to match {brand}'s measured long-wait rate"
    return result


def _swap_in(staging: Path, final: Path) -> None:
    old = final.with_name(final.name + ".old")
    if old.exists():
        shutil.rmtree(old)
    if final.exists():
        final.rename(old)
    staging.rename(final)
    if old.exists():
        shutil.rmtree(old)


def run_month(month: str, cfg: Config, run_id: str, logger: logging.Logger, chaos: str | None = None,
              force_download: bool = False, session=None) -> dict:
    started, t0 = datetime.now(timezone.utc).isoformat(timespec="seconds"), time.monotonic()
    stage_seconds: dict[str, float] = {}
    client = extract.HttpClient(cfg["http"], logger, session=session)
    month_start, month_end = month_bounds(month)

    # 1. EXTRACT ---------------------------------------------------------------
    t = time.monotonic()
    logger.info("Stage extract | month=%s", month)
    zone_manifest = extract.fetch_zone_lookup(cfg, client, logger)
    file_month = f"{date.today():%Y-%m}" if chaos == "unpublished_month" else month
    trip_manifest = extract.fetch_trip_file(
        file_month, cfg, client, logger, force=force_download,
        truncate_after=cfg["http"]["download_chunk_bytes"] if chaos == "truncated_download" else None)
    stage_seconds["extract"] = round(time.monotonic() - t, 1)

    # 2. VALIDATE --------------------------------------------------------------
    t = time.monotonic()
    logger.info("Stage validate | month=%s", month)
    con = model.connect()
    model.register_sources(con, Path(trip_manifest["path"]), Path(zone_manifest["path"]), cfg,
                           drop_column="request_datetime" if chaos == "missing_column" else None)
    checks: list[dict] = []
    schema_check, schema_drift = validate.check_schema(con, logger)
    row_check, total_rows = validate.check_row_count(con, trip_manifest["parquet_footer_rows"])
    checks += [schema_check, row_check, validate.check_zone_lookup(con), validate.check_duplicates(con),
               validate.check_flag_domain(con),
               validate.check_daily_volume(con, month_start, month_end, cfg["validation"]["daily_dip_ratio"])]
    validate.build_flagged_view(con, cfg, month_start, month_end)
    checks += validate.summarise_trip_rules(con, cfg, total_rows)
    gate = validate.kpi_gate(con, cfg, total_rows)
    checks.append(gate)
    for c in checks:
        log = logger.warning if c["status"] in (WARN, UNKNOWN) else logger.info
        log("Validation | check=%s status=%s detail=%s", c["check"], c["status"], c["detail"])
    if gate["status"] == FAIL:
        raise ValidationError(f"KPI gate failed: {gate['detail']}")
    stage_seconds["validate"] = round(time.monotonic() - t, 1)

    # 3. MODEL -----------------------------------------------------------------
    t = time.monotonic()
    logger.info("Stage model | month=%s", month)
    silver_path = model.build_fact_trip(con, cfg, month, logger)
    silver_rows = con.execute("SELECT count(*) FROM fact_trip").fetchone()[0]
    conservation = {"check": "D07.silver_row_conservation", "status": PASS if silver_rows == total_rows else FAIL,
                    "detail": f"source rows {total_rows} -> silver rows {silver_rows} (nothing dropped, only flagged)"}
    checks.append(conservation)
    if conservation["status"] == FAIL:
        raise ValidationError(conservation["detail"])
    stage_seconds["model"] = round(time.monotonic() - t, 1)

    # 4. RECONCILE -------------------------------------------------------------
    t = time.monotonic()
    logger.info("Stage reconcile | month=%s", month)
    api_base = UNREACHABLE_API if chaos == "api_down" else None
    zone_check, zone_table = reconcile.zone_reconciliation(con, cfg, month, client, logger, api_base)
    base_check = reconcile.base_reconciliation(con, cfg, month, client, logger, api_base)
    recon_checks = [zone_check, base_check]
    degraded = any("unavailable" in c["detail"] for c in recon_checks)
    stage_seconds["reconcile"] = round(time.monotonic() - t, 1)

    # 5. METRICS ---------------------------------------------------------------
    t = time.monotonic()
    logger.info("Stage metrics | month=%s", month)
    gold = {
        "metrics_borough.csv": metrics.borough_metrics(con, cfg, month),
        "metrics_wav.csv": metrics.wav_metrics(con, cfg, month),
        "metrics_zone.csv": metrics.zone_metrics(con, cfg, month),
        "daily_trips.csv": metrics.daily_licensee_counts(con),
    }
    if zone_table is not None:
        gold["reconciliation_zone.csv"] = zone_table
    stage_seconds["metrics"] = round(time.monotonic() - t, 1)

    # 6. PUBLISH ---------------------------------------------------------------
    decision, reasons = publish_decision(checks + recon_checks)
    impact = kpi_impact_of_missing_trips(zone_check, checks, gold["metrics_borough.csv"], cfg["licenses"])
    if impact:
        logger.warning("Publish | month=%s missing trips %s could move the citywide long-wait rate from %.4f to "
                       "between %.4f and %.4f (estimate %s)", month, impact["missing_trips"], impact["reported"],
                       impact["lower_bound"], impact["upper_bound"], impact.get("estimate"))
    # Chaos runs are demonstrations: they must never overwrite a real month's outputs
    out_root = cfg.path("outputs") / "chaos" / chaos if chaos else cfg.path("outputs")
    final_dir = out_root / f"month={month}"
    staging = final_dir.with_name(final_dir.name + f".staging-{run_id}")
    staging.mkdir(parents=True, exist_ok=True)
    for name, df in gold.items():
        write_csv(df, staging / name)
    write_json({"month": month, "checks": checks, "schema": schema_drift,
                "trip_rules": [r.__dict__ for r in validate.trip_rules(cfg)]}, staging / "validation_report.json")
    write_json({"month": month, "checks": recon_checks}, staging / "reconciliation.json")
    citywide = gold["metrics_borough.csv"].query("licensee == 'All' and pickup_borough == 'Citywide'").iloc[0]
    manifest = {
        "run_id": run_id, "month": month, "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_seconds": round(time.monotonic() - t0, 1), "stage_seconds": stage_seconds,
        "git_commit": _git_commit(), "config_fingerprint": cfg.fingerprint, "chaos": chaos,
        "publish_decision": decision, "decision_reasons": reasons, "degraded": degraded,
        "kpi_impact_of_missing_trips": impact,
        "validation_status": validate.overall_status(checks),
        "inputs": {"trip_file": {k: v for k, v in trip_manifest.items() if k != "path"},
                   "zone_lookup": {k: v for k, v in zone_manifest.items() if k != "path"}},
        "row_counts": {"parquet_footer": trip_manifest["parquet_footer_rows"], "scanned": total_rows,
                       "silver": silver_rows, "kpi_measurable": gate["count"],
                       "held_out_of_kpi": total_rows - gate["count"]},
        "headline": {"long_wait_rate": float(citywide.long_wait_rate),
                     "median_wait_min": float(citywide.median_wait_min),
                     "p90_wait_min": float(citywide.p90_wait_min)},
        "silver_path": str(silver_path.relative_to(cfg.root)),
        # checksums of everything except this manifest: a rerun of the same inputs must reproduce them
        "outputs": {f.name: sha256_file(f) for f in sorted(staging.iterdir()) if f.name != "run_manifest.json"},
    }
    write_json(manifest, staging / "run_manifest.json")
    _swap_in(staging, final_dir)
    logger.info("Publish | month=%s decision=%s degraded=%s long_wait_rate=%.4f outputs=%s",
                month, decision, degraded, manifest["headline"]["long_wait_rate"], final_dir)
    for reason in reasons:
        logger.warning("Publish caveat | month=%s %s", month, reason)
    con.close()
    return manifest
