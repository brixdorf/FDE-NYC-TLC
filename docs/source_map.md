# Source map

Method (Class 4): **problem → questions → information → fields → sources**. Start from what the decision needs, then ask where that truth lives, who owns it, how fresh it is and whether it can be trusted. Minimum data beats all data: the trip file has 25 columns and the KPI needs 14 of them (the schema contract in [validation_rules.md](validation_rules.md)).

## 1. Business questions to sources

| # | Business question | Information needed | Field or event | Source | Owner | Freshness | Can I trust it? | Gap |
|---|---|---|---|---|---|---|---|---|
| Q1 | How long do riders wait for an app-based ride? | When the ride was requested and when the rider was picked up | `request_datetime`, `pickup_datetime` | HVFHV trip file (Parquet) | HV licensees record it; TLC publishes it | Monthly, about 2 months after month end | Mostly. 98.7% of trips measurable. About 1.2% are picked up *before* the request time | No reservation flag, so scheduled rides cannot be separated from on-demand ones |
| Q2 | Where are waits longest? | Pickup location | `PULocationID` + zone lookup (`Borough`, `Zone`) | Trip file + Taxi Zone Lookup CSV | TLC | Lookup is static (265 zones) | Yes. Every ID maps; 264/265 mean Unknown / Outside NYC | Zones, not coordinates. No finer location |
| Q3 | Is the wait the driver getting there, or the rider boarding? | When the driver arrived at the pickup point | `on_scene_datetime` | Trip file | Licensees | Monthly | Partly. Filled for 100% of trips although the dictionary says "accessible vehicles only"; Uber stamps it equal to pickup on 6.7% of trips, Lyft on 0.1% | Meaning differs by licensee, so the split is descriptive only |
| Q4 | Do riders who need a wheelchair-accessible vehicle wait longer? | Whether the rider asked for a WAV | `wav_request_flag`, `wav_match_flag` | Trip file | Licensees | Monthly | Partly. 20 to 25% of WAV-requested trips have a pickup before the request, 52 to 58% for Lyft | Only completed trips are published: an unmet WAV request never appears, so fulfilment cannot be measured |
| Q5 | Does it differ by company? | Licensee | `hvfhs_license_num` | Trip file + TLC data dictionary | TLC | Dictionary dated March 18, 2025 | Yes | Base numbers change over time |
| Q6 | Is this month's file complete? | An independent count of the same trips | `trip_count` by zone; `total_dispatched_trips` by licensee | NYC Open Data API: `c5iv-bn4s`, `2v9c-2k7f` | TLC | Zone report: same lag as the files. Base report: lags further (2026 data only through May at build time) | It is TLC's own control total, but derived from the same submissions | When file and report disagree we cannot tell which is right from the data alone |
| Q7 | How many riders gave up waiting? | Cancelled or unmatched requests | none | **Not published** | Licensees hold it | n/a | n/a | **Scope change:** the KPI covers riders who were picked up. It is a floor on rider pain, not the full picture |

## 2. Source inventory (the five things to capture for every system)

| Source | Retrieval mode | Format | Grain | Owner | Freshness | Access | Reliability notes |
|---|---|---|---|---|---|---|---|
| `fhvhv_tripdata_YYYY-MM.parquet` | File over HTTPS (CloudFront) | Parquet, 25 columns, about 500 MB and 21M rows a month | One completed trip | Licensees submit; TLC publishes | Monthly, about 2 month lag; files are occasionally republished | Public, no key | TLC states it makes no representation about accuracy. June 2026 is 2.1% short of TLC's own count |
| `taxi_zone_lookup.csv` | File over HTTPS | CSV, 265 rows | One taxi zone | TLC | Static | Public | Clean; includes 264 Unknown and 265 Outside of NYC |
| `c5iv-bn4s` Pickups and Drop-offs by Taxi Zone and Industry | REST API (Socrata SODA, JSON, paged) | JSON | Month x industry x zone x pickup/drop-off | TLC | Covers through 2026-07 | Public | Used as a control total, never as trip data |
| `2v9c-2k7f` FHV Base Aggregate Report | REST API (Socrata SODA) | JSON | Base x month | TLC | Lags the trip files (2026 only through May) | Public | Lists HV licensees under brand names (UBER, LYFT), not base numbers |
| TLC HVFHV data dictionary | Reference PDF | PDF | One field | TLC | March 18, 2025 | Public | Out of date for `on_scene_datetime` |

Every raw input is preserved unchanged under `data/raw/` (gitignored because of size), partitioned by month, with a `_manifest.json` holding the completeness evidence: remote Content-Length, bytes on disk, SHA-256, ETag, Parquet footer row count, or for the API the reported `count(*)` against rows received and the raw JSON of every page.

## 3. System-of-record decisions

| Fact | System of record | Why | Not the SoR, and why |
|---|---|---|---|
| Trip timestamps, zones, flags | Trip file (licensee submissions as published by TLC) | The licensee dispatch system creates these events; the file is the only trip-level copy | Aggregates are derived counts, they cannot describe a trip |
| How many trips happened | **Unresolved** | The file and TLC's report agree to 0.01% in May and July and disagree by 2.1% in June | The aggregate is used as a control total; which copy is right is a question for TLC (see [decisions.md](decisions.md), D6) |
| Zone to borough | Taxi Zone Lookup | TLC owns the zone definitions | none |
| Licensee identity | TLC data dictionary | TLC issues the licenses | Base aggregate uses brand names, mapped by an assumption |
| What a field means | Nobody, today | The dictionary is out of date for `on_scene_datetime` and silent on scheduled rides and `trip_time` | Semantics questions go to the owner, not into code |

## 4. Questions for the data owner (TLC Data & Technology)

1. Does `request_datetime` hold the booked time for scheduled rides? Is there a reservation flag we can add?
2. What does `on_scene_datetime` mean now that it is filled for every trip, and why do the licensees stamp it differently?
3. What does Lyft's `trip_time` include beyond pickup to dropoff?
4. Why does the June 2026 file hold 455,490 fewer trips than the published zone report, concentrated in Lyft trips on June 8 to 14? Will the file be republished?
5. Can unfulfilled and cancelled requests be published, so long waits that end in no ride are visible?
