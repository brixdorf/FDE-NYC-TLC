# Decisions log

The judgement calls behind the pipeline, each with the evidence, the options and the reason. D6 is the one covered in depth in the demo ([DEMO_SCRIPT.md](../DEMO_SCRIPT.md)).

## D1. Build on High Volume FHV trips, not Yellow taxi

- **Evidence:** Yellow taxi records two timestamps (meter on, meter off). HVFHV records four (request, on scene, pickup, dropoff).
- **Decision:** use HVFHV. It is the only TLC dataset with a workflow before the ride starts, and waiting is where riders feel the service.
- **Cost:** about 500 MB and 21M rows a month. DuckDB handles that in about 30 seconds per month; pandas alone would not.

## D2. The KPI is request to pickup, over 10 minutes

- **Options considered:**
  - Median wait: hides the tail.
  - Request to on scene: the licensees record it differently (D4).
  - Share over a threshold: chosen.
- **Why:** a rate is something a policy team can set a target on, and it maps to a rider's experience ("I waited more than 10 minutes").
- **The threshold is an assumption.** The pipeline reports 5 and 15 minutes beside it, because the definition moves the number: June is 46.7% / 12.0% / 3.6% (the Class 6 lesson).
- **No target is invented.** The baseline is measured; the target belongs to the owner.

## D3. Pickups before the request are quarantined, never clipped

- **Evidence:** about 1.2% of trips every month have pickup before request. The rate peaks at 4 to 5 AM (4.5% of trips) and covers 52 to 58% of Lyft wheelchair-accessible trips. The pattern fits pre-scheduled rides whose `request_datetime` holds the booked time. The data has no reservation flag.
- **Options:**
  - Clip to 0: turns an unknown into a perfect wait. For Lyft WAV in June the long-wait rate would read about 18% instead of 37.3%.
  - Drop silently: halves the Lyft WAV denominator with no trace.
  - Keep as is: impossible negative waits in the medians.
  - Quarantine and count: chosen.
- **Decision:** flag R03. Exclude these trips from wait metrics, report the count per segment (M4 shows the unmeasurable WAV share), and ask the owner what the field means for scheduled rides.

## D4. The driver-arrival split is descriptive only

- **Evidence:** the TLC dictionary says `on_scene_datetime` is recorded for accessible vehicles only. In 2026 it is filled for 100% of trips. Uber stamps it equal to the pickup time on 6.7% of trips, Lyft on 0.1%.
- **Decision:**
  - Use the arrival/boarding split (M3) to see *where* the wait sits (arrival is about 80% of it).
  - Never use it to compare licensees.
  - Never build the KPI on it.
  - Trips with arrival outside request to pickup (1.8%) leave only M3.

## D5. Duration comes from timestamps, not `trip_time`

- **Evidence:** Uber's `trip_time` equals dropoff minus pickup to the second. Lyft's is never shorter and is more than 60 seconds longer on about 10% of trips (P99 +5 min). The on-scene timestamp does not explain the extra.
- **Decision:** compute duration from timestamps, which mean the same for both licensees. Report the mismatch (R09) without excluding anyone, and ask the owner.

## D6. June 2026 is on HOLD: the file is 2.1% short of TLC's own count

- **What happened:**
  - The June file downloaded perfectly: bytes match Content-Length, SHA-256 recorded, footer rows match rows read.
  - TLC's own published zone report counts 21,231,358 June pickups. The file holds 20,775,868, which is **455,490 fewer (-2.15%)**.
  - May and July agree to within 0.01%. May also matches TLC's per-licensee report exactly, to the trip.
- **Locating it:**
  - The gap is spread evenly over zones (median -2.1%, spread 0.3 points), so it is not geographic.
  - The internal daily-volume check (D06) shows **Lyft's volume down by about a third on June 8 to 14** while Uber is normal. It estimates about 393K trips short, the same order as the external gap.
  - The most likely reading: a week of Lyft records is missing from the published file.
- **Options considered:**
  1. **Publish 12.0% as the June result.** The file is internally valid (98.7% measurable), but the number would silently rest on a sample that under-counts one licensee for a week.
  2. **Impute or backfill the missing trips.** We do not have them. Inventing 455K trips' waits is fabricating data.
  3. **Drop June.** Loses a month that is 97.9% present, and hides a data problem TLC should know about.
  4. **HOLD the month, keep the numbers for investigation, and size the uncertainty.** (Chosen.)
- **Decision:**
  - The pipeline marks June **HOLD** by a rule written before any month was reconciled (FAIL above 2%).
  - It still writes June's outputs and adds to the run manifest how far the missing trips could move the KPI:
    - reported 12.04%
    - between 11.78% (none of them waited long) and 13.95% (all did)
    - best estimate 11.95% if they look like Lyft's measured trips
  - So the June rise over May (9.7%) holds even at the lower bound, but it is not yet quotable. Uber-only June numbers show no dip days and look unaffected.
  - The question goes to TLC: will the file be republished? The pipeline's cache compares ETag and checksum, so a republished file is picked up on the next run.
- **Why it matters:** a byte-perfect download proves we got the file. It does not prove the file is complete. Without the second, independent source, June would have been published.

## D7. The KPI covers riders who were picked up; fulfilment is out of scope

- **Evidence:** every WAV-requested trip in the file has `wav_match_flag = Y`. Only completed trips are published, so a request that was never served is invisible.
- **Decision:**
  - Drop "WAV fulfilment rate" as a metric; it would read 100% by construction.
  - State in every summary that the long-wait rate is a floor on rider pain.
  - Ask whether TLC can publish unfulfilled requests. That is a workflow and instrumentation gap, like FlashEats' missing "driver arrived" event.

## D8. The silver table keeps every row

- **Decision:** flags, not deletes. `fact_trip` has exactly as many rows as the source (check D07). Each metric filters by the flags that matter to it.
- **Why:** a later reader can recount any exclusion, and a new metric can use trips the KPI rejects (for example, the arrival split uses trips outside the KPI's borough rule).

## D9. Airport zones are flagged for separate reading

- **Evidence:** JFK and LaGuardia sit in the top 10 long-wait zones every month (about 24 to 26%).
- **Decision:** keep them, but note in the evidence page that riders there walk to designated pickup areas, so part of the wait is not driver supply. Raising airports in a licensee review needs that context.

## D10. Every download is verified, because one silently failed

- **What happened:** during development, a plain `curl` download of the July file stopped at 76 MB of 511 MB and still exited 0.
- **What the pipeline did:** the first pipeline run saw the size mismatch against Content-Length, logged it, and downloaded the file again.
- **Decision:** that is why every file is verified on bytes, ETag and SHA-256, and why a truncated download is retried, then rejected with no partial file left.

## D11. Licensee totals are matched to the base report by brand name

- **Evidence:** the FHV Base Aggregate Report lists the HV licensees as `UBER` and `LYFT`, not by base number. In May both totals match the file to the trip.
- **Decision:** map `dim_license.brand` in upper case to `base_license_number`. This is recorded as an assumption; if TLC changes the naming, C02 returns 0 rows and reads UNKNOWN rather than a false PASS.

## D12. A secondary source being down degrades the run; it does not fail it

- **Decision:** if the NYC Open Data API is unreachable, C01/C02 are UNKNOWN, the month is "PUBLISH WITH CAVEATS", and the manifest says `degraded: true`. The trip data itself is still valid.
- **Tested with `--chaos api_down`.** Without the API, June's decision falls from HOLD to "with caveats". The internal D06 check still flags the Lyft shortfall, which is why it exists.
- **Contrast:** if the trip file itself cannot be retrieved, the run fails (exit 3). There is nothing to measure.
