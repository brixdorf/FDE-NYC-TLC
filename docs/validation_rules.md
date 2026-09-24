# Validation rules

The deliverable of validation is a **decision**, not a cleaned table (Class 6). Every check reports PASS, WARN, FAIL or UNKNOWN. Nothing is silently fixed:
- **Dataset-level failures** stop the run.
- **Trip-level rules** become flag columns on the silver table. A flagged trip is held out only of the metrics its flag makes untrustworthy, and it is always counted.

The rules are defined once, in [`pipeline/validate.py`](../pipeline/validate.py) (`trip_rules`). Thresholds live in [`config/pipeline.toml`](../config/pipeline.toml). Each threshold is a business assumption, and the owner can change it without touching code.

Results below are from the committed runs (`outputs/month=*/validation_report.json`).

## 1. Dataset checks (can this file be used at all?)

| ID | Plain English | How it is checked | If it fails | 2026-05 | 2026-06 | 2026-07 |
|---|---|---|---|---|---|---|
| D01 | The 14 columns the KPI and model need exist with the right types | `DESCRIBE` against a contract; extra columns are listed as drift | **FAIL, stop, exit 2** | PASS | PASS | PASS |
| D02 | We read every row the file says it holds | rows scanned = Parquet footer `num_rows` | FAIL, stop | PASS 22,125,744 | PASS 20,775,868 | PASS 20,921,249 |
| D03 | The zone lookup is complete | 265 unique zone IDs | FAIL, stop | PASS | PASS | PASS |
| D04 | No trip is recorded twice | rows identical in every column (the file has no trip ID) | WARN | PASS 0 | PASS 0 | PASS 0 |
| D05 | Y/N flags only hold Y or N | domain check on 5 flag columns | WARN | PASS | PASS | PASS |
| D06 | No licensee has a day far below its usual volume (internal completeness) | licensee-day trips < 75% of that licensee's median for the same weekday | WARN, and a publish caveat | PASS | **WARN**: 6 Lyft days (June 8 to 14), about 393K trips short | PASS |
| D07 | Modelling drops nothing | source rows = silver rows | FAIL, stop | PASS | PASS | PASS |

## 2. Trip-level rules (which trips can each metric trust?)

Share = flagged trips / all trips in the month.

| ID | Plain English | Condition flagged (SQL) | Scope | Action | 2026-05 | 2026-06 | 2026-07 |
|---|---|---|---|---|---|---|---|
| R01 | Request, pickup and dropoff times are present | any of the three `IS NULL` | all wait metrics | quarantine | 0 | 0 | 0 |
| R02 | A trip belongs to the month its pickup falls in | pickup outside `[month start, next month start)` | all wait metrics | exclude, count | 0 | 0 | 0 |
| R03 | A rider cannot be picked up before requesting | `wait_s < 0` | all wait metrics | **quarantine, never clip to 0**; owner question (scheduled rides?) | 275,536 (1.25%) | 260,908 (1.26%) | 246,534 (1.18%) |
| R04 | A wait over 60 minutes is not an on-demand wait | `wait_s > 3600` | all wait metrics | hold out, count | 965 | 958 | 1,164 |
| R05 | Dropoff is after pickup | `duration_s <= 0` | all wait metrics | quarantine | 0 | 2 | 2 |
| R06 | A real trip has positive miles, lasts under 4 h and averages under 70 mph | `trip_miles <= 0 OR duration_s > 14400 OR mph > 70` | all wait metrics | quarantine | 3,375 | 5,855 | 3,053 |
| R07 | Pickup zone is a known NYC zone | zone 264/265 or not in lookup | borough and zone views only | keep citywide | 1,182 | 1,241 | 1,245 |
| R08 | Driver arrival falls between request and pickup | `on_scene` null, before request or after pickup | stage split (M3) only | exclude from M3 | 402,971 (1.82%) | 385,484 (1.86%) | 367,168 (1.76%) |
| R09 | `trip_time` equals dropoff minus pickup (within 60 s) | `abs(trip_time - duration_s) > 60` | none; duration comes from timestamps | report, owner question | 996,116 (4.50%) | 581,494 (2.80%) | 622,261 (2.97%) |
| R10 | License is a known HVFHS licensee | not in HV0002 to HV0005 | all wait metrics | quarantine | 0 | 0 | 0 |

`in_kpi` = not (R01 or R02 or R03 or R04 or R05 or R06 or R10). `in_borough_kpi` adds not R07. `in_stage_split` adds not R08.

What was deliberately **not** made a rule:
- **Trips under 1 mph** (about 9,800 in June): slow is not impossible. Profiling tells you where to look, not what to delete.
- **Dropoff zone 265, "Outside of NYC"** (about 1M trips): these are real trips to New Jersey and beyond, and the KPI uses the pickup zone.
- **`originating_base_num` null for 26% of trips:** not used.

## 3. The gate and the reconciliations (is the month publishable?)

| ID | Plain English | Rule | 2026-05 | 2026-06 | 2026-07 |
|---|---|---|---|---|---|
| G01 | Enough of the month is measurable for the KPI to mean something | `in_kpi` share: PASS >= 97%, WARN >= 90%, **FAIL < 90% stops the run** | PASS 98.74% | PASS 98.71% | PASS 98.80% |
| C01 | The file holds the trips TLC itself reports, zone by zone | abs(file - TLC) / TLC: PASS <= 0.5%, WARN <= 2%, FAIL above; API unreachable or month missing = UNKNOWN | PASS +0.005% | **FAIL -2.145%** | PASS +0.006% |
| C02 | Each licensee's total matches TLC's base report | same thresholds, per licensee | PASS, exact to the trip | UNKNOWN (not published yet) | UNKNOWN (not published yet) |

**Publish decision:** only G01, D06, C01 and C02 decide publishability. Row-level WARNs are already handled by quarantine.

| Decision | When |
|---|---|
| HOLD | any deciding check is FAIL |
| PUBLISH WITH CAVEATS | any deciding check is WARN or UNKNOWN |
| PUBLISH | all deciding checks pass |

A HOLD month still writes its outputs, marked HOLD, for investigation. The run manifest adds the range the KPI could take if the missing trips were counted (see [decisions.md](decisions.md), D6).

The reconciliation thresholds were set in `config/pipeline.toml` **before** any month was reconciled, so June's FAIL was not tuned after the fact.

## 4. Questions the rules cannot answer

Each is marked in the table above as "owner question". They are listed in [source_map.md](source_map.md#4-questions-for-the-data-owner-tlc-data--technology).
