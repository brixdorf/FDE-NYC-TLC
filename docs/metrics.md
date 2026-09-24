# Metrics

**Project KPI:** reduce the **long-wait rate**, the share of HVFHV trips where the rider waited more than 10 minutes from request to pickup. It is reported citywide and by pickup borough, monthly. It is a lagging outcome.

The KPI is defined by the rider's experience, not by any system's internals. It survives a change of licensee, app or dispatch algorithm (the Class 7 point that a KPI must be business-measurable, not product-bound).

All metrics are computed in [`pipeline/metrics.py`](../pipeline/metrics.py) over the silver `fact_trip` table, using only trips whose validation flags allow it. Every rate travels with its denominator (`trips_measured`) and its coverage (`measured_share`).

| ID | Metric | Type | Formula | Grain | Why it matters | Link to the KPI | What it does NOT prove |
|---|---|---|---|---|---|---|---|
| M1 | **Long-wait rate** | Outcome (KPI) | trips with `wait_s > 600` / trips with `in_kpi` | month x licensee x pickup borough | The share of riders a service fails, in a form a policy team can set a target on | It is the KPI. Sensitivity at 5 and 15 min is reported alongside, because the threshold moves the number (June: 46.7% / 12.0% / 3.6%) | Why riders waited; riders who gave up never appear |
| M2 | **Median and P90 wait** | Workflow driver | median and 90th percentile of `wait_s` / 60 over `in_kpi` trips | same | The median shows the typical rider; P90 shows the tail the KPI counts | A rising P90 with a flat median means the long-wait rate will rise | Same as M1 |
| M3 | **Wait stage split** | Workflow driver (where the wait sits) | median `arrival_s` (request to on scene) and median `boarding_s` (on scene to pickup), over `in_stage_split` trips | same | Tells supply problems (driver far away) from pickup-point problems (rider or driver cannot find each other) | Arrival is about 80% of the median wait, so supply near the rider is the lever | Licensee comparisons: the two licensees record `on_scene` differently (source map, Q3) |
| M4 | **Accessible-ride wait gap** | Outcome for a priority rider group | long-wait rate for `wav_requested` trips vs standard trips, with the share of WAV trips whose wait is unmeasurable | month x licensee x WAV requested | WAV riders wait past 10 min about 2.7 times as often (June: 32.0% vs 12.0%) | A subgroup of the KPI with a regulatory interest | Fulfilment: only completed trips are published, so every WAV request in the file is matched. 20 to 25% of WAV trips (over 50% for Lyft) cannot be measured at all |
| M5 | **Measurable share and reconciliation gap** | Data trust | `in_kpi` trips / all trips; (file trips - TLC published trips) / TLC published trips | month (and licensee) | A number is only as good as its coverage; this decides PUBLISH / CAVEATS / HOLD | Tells the reader whether M1 to M4 can be quoted this month | That the matched trips are correct, only that the count agrees |

## Leading vs lagging

- **M1 is lagging.** It is the monthly scorecard.
- **M2 (P90) and M3 (arrival) are the closest this data has to leading indicators.** They move before the rate crosses a threshold and point at the stage to act on.
- **A true leading signal is out of reach with this data.** Live supply near the rider, or requests that time out, is not in any published source.

## Deliberately not a metric

| Candidate | Why not |
|---|---|
| WAV fulfilment rate (`wav_match_flag` among WAV requests) | 100% by construction, because unmatched requests never become trip records. Publishing it would say "every accessible request was served", which the data cannot know |
| Average wait | A few very long (possibly scheduled) waits pull it up; the median and P90 describe riders better |
| Licensee ranking on arrival time (M3) | The field means different things per licensee |
| Fare or driver pay | Not linked to the waiting KPI; minimum data beats all data |
