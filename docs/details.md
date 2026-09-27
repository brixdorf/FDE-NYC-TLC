# Project details

Everything the short [README](../README.md) leaves out: how each class skill maps to the repo, the stakeholders, the sources, every run option, the data layers, the schedule, the failure scenarios and the full Known / Unknown / Assumption / Limitation list.

## How the class method maps to this repo

| Skill (class) | Where it lives | Evidence |
|---|---|---|
| Understand sources (4) | [source_map.md](source_map.md) | Questions to fields to sources, owner, grain, freshness, trust and gaps; system-of-record calls; owner questions |
| Retrieve data (5) | [pipeline/extract.py](../pipeline/extract.py) | Two modes: files over HTTPS (Parquet, CSV) and the REST API (Socrata, paged). Completeness proven by bytes vs Content-Length, SHA-256, Parquet footer rows, API `count(*)`. Raw inputs preserved with manifests |
| Profile and validate (6) | [notebooks/01_profile_validate_model.ipynb](../notebooks/01_profile_validate_model.ipynb), [pipeline/validate.py](../pipeline/validate.py), [validation_rules.md](validation_rules.md) | 7 dataset checks, 10 trip rules as flags (quarantine, never fix), a KPI gate, results for three months |
| Model the workflow (7) | [pipeline/model.py](../pipeline/model.py), [pipeline/metrics.py](../pipeline/metrics.py), [data_model.md](data_model.md), [metrics.md](metrics.md) | request, on scene, pickup, dropoff stages; `fact_trip` at trip grain; aggregate-before-join gold tables; 5 metrics tied to the KPI |
| Dependable pipeline (8) | [run_pipeline.py](../run_pipeline.py), [pipeline/runner.py](../pipeline/runner.py), [pipeline/reconcile.py](../pipeline/reconcile.py), [tests/](../tests) | extract, validate, model, reconcile, metrics, publish; logging with run ids; idempotent reruns; retries; clear exit codes; chaos scenarios; 26 offline tests |
| Class challenges (5 to 7) | [Python Notebook Challenges/](../Python%20Notebook%20Challenges) | The FlashEats in-class notebooks, solved and run |

## Stakeholders

These are framed from TLC's public role, not from interviews. They are an assumption to confirm in discovery (Class 1 role types).

| Role type | Who | What they know or care about |
|---|---|---|
| Outcome owner | TLC Office of Policy | Reliable service across all five boroughs; owns the KPI definition and target |
| Users | TLC policy and research analysts | A monthly number they can defend in a licensee review |
| Operator | Uber and Lyft NYC operations teams | Driver supply by area; how they are measured |
| Data and system owner | TLC Data & Technology (publishes); licensees (submit the records) | Trip record accuracy, field meanings, republishing |
| Upstream | Licensee dispatch systems that stamp request, on-scene and pickup times | Whether timestamps mean the same thing across companies |
| Blocker or enabler | Accessibility advocates and TLC's WAV program; licensees disputing numbers | WAV service levels; a number that survives challenge |

## Sources

| Source | Mode | Grain | Role |
|---|---|---|---|
| `fhvhv_tripdata_YYYY-MM.parquet` (TLC CloudFront) | File over HTTPS | one completed trip | The facts |
| `taxi_zone_lookup.csv` | File over HTTPS | one zone (265) | Zone to borough |
| NYC Open Data `c5iv-bn4s` Pickups by Taxi Zone and Industry | REST API | month x zone x industry | Control total per zone |
| NYC Open Data `2v9c-2k7f` FHV Base Aggregate Report | REST API | licensee x month | Control total per licensee (lags the trip files) |
| DuckDB over the preserved raw files | SQL | as modelled | Validation, model and metrics |

Full map: [source_map.md](source_map.md). Workflow and data model diagrams: [data_model.md](data_model.md).

## Run options

Needs Python 3.11 or newer (tested on 3.14) and about 2 GB of disk for three months of raw data.

```bash
python run_pipeline.py --from 2026-05 --to 2026-07   # download, verify, validate, model, reconcile, publish
python run_pipeline.py                               # the latest month TLC should have published (today minus 2 months)
python run_pipeline.py --month 2026-06 --chaos api_down   # watch one failure path
python run_pipeline.py --report-only                 # rebuild docs/evidence.md from outputs/
pytest                                               # 26 tests, offline, on a fixture of real rows
```

A fresh month downloads about 500 MB, then runs in about 30 seconds. Reruns reuse a local file only if its size, ETag and SHA-256 still match the remote.

| Path | What is there | In git |
|---|---|---|
| `data/raw/` | Bronze: the files exactly as retrieved, plus `_manifest.json` and every raw API page | no (size) |
| `data/silver/month=YYYY-MM/fact_trip.parquet` | Silver: every trip with stage durations and validation flags | no (size) |
| `outputs/month=YYYY-MM/` | Gold: `metrics_*.csv`, `validation_report.json`, `reconciliation*.{json,csv}`, `run_manifest.json` | yes |
| `docs/evidence.md` | Generated evidence page | yes |
| `logs/`, [`docs/run_logs/`](run_logs) | One log per run id; copies of the real and chaos runs | copies only |

Monthly schedule (TLC publishes about two months after month end; a run before the file exists fails cleanly and is safe to repeat):

```bash
# cron: 06:00 on the 20th of every month
0 6 20 * * cd /path/to/repo && .venv/bin/python run_pipeline.py >> logs/cron.log 2>&1
# Windows Task Scheduler
schtasks /Create /SC MONTHLY /D 20 /ST 06:00 /TN TLCWaitPipeline /TR "cmd /c cd /d D:\path\to\repo && .venv\Scripts\python run_pipeline.py"
```

## Dependability

| Scenario | What the pipeline does | Exit | Evidence |
|---|---|---|---|
| Normal month | Six stages; outputs staged, then swapped in atomically; run manifest with input checksums, row counts per stage, config fingerprint, git commit | 0 | [01_monthly_run_2026-06.log](run_logs/01_monthly_run_2026-06.log) |
| Same month rerun | Reuses the verified download; every output file reproduces the same SHA-256 (only the run id changes) | 0 | [06_rerun_2026-06.log](run_logs/06_rerun_2026-06.log), `tests/test_pipeline.py` |
| Required column disappears | Schema contract FAIL before any processing; previous outputs untouched | 2 | [02_chaos_missing_column.log](run_logs/02_chaos_missing_column.log) |
| Download cut off mid-file | Detected by byte count, retried 4 times with backoff, no partial file kept | 3 | [03_chaos_truncated_download.log](run_logs/03_chaos_truncated_download.log) |
| Reconciliation API down | Retries, then UNKNOWN; month completes as degraded, "with caveats" | 0 | [04_chaos_api_down.log](run_logs/04_chaos_api_down.log) |
| Month not published yet | Clear message naming the month and TLC's publication lag | 3 | [05_chaos_unpublished_month.log](run_logs/05_chaos_unpublished_month.log) |
| File short of TLC's own count | HOLD, with the range the KPI could take | 0 | June 2026, [run_manifest.json](../outputs/month=2026-06/run_manifest.json) |

Exit codes: 0 success (including degraded), 1 unexpected error, 2 validation failed, 3 retrieval failed, 4 bad arguments. Retries are bounded (4 attempts, exponential backoff, `Retry-After` honoured) and only for 429, 5xx, timeouts and dropped connections. All thresholds live in [config/pipeline.toml](../config/pipeline.toml).

## Known / Unknown / Assumption / Limitation

**Known**
- About 1 in 10 app-based rides waits more than 10 minutes from request to pickup (9.7% in May, 10.0% in July). The median wait is about 4.5 minutes.
- Staten Island has the highest long-wait rate every month, followed by the Bronx and Queens; Manhattan has the lowest.
- WAV-requested rides wait past 10 minutes about 2.7 to 2.8 times as often as standard rides.
- The May and July files reconcile with TLC's published counts to within 0.01%; May matches per licensee exactly.
- The June file holds 455,490 fewer trips than TLC reports, concentrated in Lyft trips on June 8 to 14.

**Unknown**
- Why June is short, and whether TLC will republish the file.
- What `request_datetime` means for scheduled rides: 1.2% of trips are picked up before their request time.
- How many riders gave up waiting. Unfulfilled requests are not published.
- What Lyft's `trip_time` includes, and why the licensees stamp `on_scene_datetime` differently.
- *Why* waits are long (supply, traffic, events). The data shows where and when, not the cause.

**Assumptions** (all in config, all for the owner to confirm)
- A long wait means over 10 minutes. 5 and 15 minutes are reported beside it.
- A wait over 60 minutes is not an on-demand wait.
- A plausible trip has positive miles, lasts under 4 hours and averages under 70 mph.
- A licensee-day under 75% of its weekday median signals missing data.
- Reconciliation passes within 0.5% and fails beyond 2%. These thresholds were set before any month was reconciled.
- TLC's base report names licensees by brand (UBER, LYFT).
- Stakeholders are framed, not interviewed.

**Limitations**
- Only completed trips are published, so the KPI is a floor on rider pain and WAV fulfilment cannot be measured.
- Location is by taxi zone, not coordinates.
- Airport pickups include walking to a designated area.
- The arrival/boarding split cannot compare licensees.
- Three months give no seasonal baseline, so June's rise cannot yet be separated from the data gap or from season.
- Everything here is association, not causation.

## Repo layout

```
run_pipeline.py            CLI: one month, a range, or the latest published month
config/pipeline.toml       every URL, threshold and retry setting
pipeline/                  extract, validate, model, reconcile, metrics, report, runner
notebooks/                 executed profiling and modelling notebook (June 2026)
tests/                     offline tests on a fixture of real rows
outputs/month=YYYY-MM/     committed gold outputs and run manifests
docs/                      source map, data model, rules, metrics, decisions, evidence, run logs
Python Notebook Challenges/  solved FlashEats class notebooks (Classes 5 to 7)
```
