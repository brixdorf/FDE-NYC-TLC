# Workflow and data model

Raw sources are organised by how TLC publishes them. The model reorganises them around the rider's workflow, so every metric is a question about a stage of that workflow.

## 1. The rider workflow (business process)

```mermaid
flowchart LR
    R["Rider requests a ride<br/><code>request_datetime</code>"] -->|"arrival<br/>(driver travels)"| S["Driver on scene<br/><code>on_scene_datetime</code>"]
    S -->|"boarding"| P["Pickup<br/><code>pickup_datetime</code>"]
    P -->|"in trip"| D["Dropoff<br/><code>dropoff_datetime</code>"]
    R -. "WAIT = pickup minus request<br/>(project KPI)" .-> P
    X["Request never fulfilled<br/>(not published)"]:::missing
    R -.-> X
    classDef missing stroke-dasharray: 5 5,color:#888;
```

| Stage | Measured as | Recorded by | Trust |
|---|---|---|---|
| Wait (KPI) | `pickup_datetime - request_datetime` | Both licensees, same meaning | Good; 1.2% negative (likely scheduled rides), quarantined |
| Arrival | `on_scene_datetime - request_datetime` | Both, but inconsistently | Descriptive only (see source map Q3) |
| Boarding | `pickup_datetime - on_scene_datetime` | Both, but inconsistently | Descriptive only |
| In trip | `dropoff_datetime - pickup_datetime` | Both | Good; `trip_time` disagrees for Lyft, so timestamps are used |
| Unfulfilled request | none | Not published | **Missing event**: the KPI can only describe riders who were picked up |

**Entities:**
- **Trip:** the unit of the KPI.
- **Taxi zone:** where the trip starts and ends, rolled up to borough.
- **Licensee:** the HV company.

**Events:** request, on scene, pickup, dropoff. Each is a timestamp on the trip row; the source has no separate event log.

**States:** a trip in the file is always in its final state (completed). Waiting, cancelled and unmatched are states the rider passes through, but the source never records them.

**Interventions:** none exist in this data. Compare FlashEats, where reassignments and restaurant contacts were recorded. The closest thing to a rider-side intervention is asking for a WAV or a shared ride (`wav_request_flag`, `shared_request_flag`).

## 2. Relational model

```mermaid
erDiagram
    DIM_ZONE ||--o{ FACT_TRIP : "pickup zone"
    DIM_ZONE ||--o{ FACT_TRIP : "dropoff zone"
    DIM_LICENSE ||--o{ FACT_TRIP : "dispatched by"
    FACT_TRIP }o--|| GOLD_BOROUGH : "aggregated to"
    FACT_TRIP }o--|| GOLD_ZONE : "aggregated to"
    FACT_TRIP }o--|| GOLD_WAV : "aggregated to"
    GOLD_ZONE ||--|| TLC_ZONE_REPORT : "reconciled with (same grain)"
    DIM_LICENSE ||--o{ TLC_BASE_REPORT : "reconciled with (brand name)"

    DIM_ZONE {
        int location_id PK
        string borough
        string zone
        string service_zone
    }
    DIM_LICENSE {
        string license PK "HV0003, HV0005"
        string brand "Uber, Lyft"
    }
    FACT_TRIP {
        string license FK
        int pu_zone_id FK
        int do_zone_id FK
        timestamp requested_at
        timestamp on_scene_at
        timestamp picked_up_at
        timestamp dropped_off_at
        int wait_s
        int arrival_s
        int boarding_s
        int duration_s
        bool wav_requested
        bool f_r01_to_f_r10 "one flag per rule"
        bool in_kpi
        bool in_borough_kpi
        bool in_stage_split
    }
    GOLD_BOROUGH {
        string month PK
        string licensee PK
        string pickup_borough PK
        int trips_total
        int trips_measured
        float long_wait_rate
        float median_wait_min
        float p90_wait_min
    }
    GOLD_ZONE {
        string month PK
        int zone_id PK
        float long_wait_rate
    }
    GOLD_WAV {
        string month PK
        string licensee PK
        string rider_request PK
        float long_wait_rate
        float measured_share
    }
    TLC_ZONE_REPORT {
        string metric_month PK
        int locationid PK
        int trip_count
    }
    TLC_BASE_REPORT {
        string base_license_number PK
        string month PK
        int total_dispatched_trips
    }
```

Grain per table:

| Table | Layer | Grain | Rows (June 2026) |
|---|---|---|---|
| raw trip file | bronze | one published trip record | 20,775,868 |
| `fact_trip` | silver | one trip, nothing dropped, every flag attached | 20,775,868 |
| `metrics_borough.csv` | gold | month x licensee (incl. All) x pickup borough (incl. Citywide) | 21 |
| `metrics_wav.csv` | gold | month x licensee x WAV requested | 6 |
| `metrics_zone.csv` | gold | month x pickup zone (known zones with trips) | 260 |
| `reconciliation_zone.csv` | gold | month x zone, file count vs TLC count (full outer join) | 261 |

The trip file has no trip ID. A duplicate is therefore defined as a row identical in every column (check D04: zero in all three months).

**Aggregate before joining (Class 7):** the file and TLC's report are each reduced to one row per zone before the join. Joining 20M trips to the report row by row would repeat each zone's published count on every trip.

## 3. Pipeline flow

```mermaid
flowchart LR
    subgraph Extract
      A1["Trip Parquet<br/>HTTPS"] --> M1["bytes = Content-Length<br/>SHA-256, footer rows"]
      A2["Zone lookup CSV<br/>HTTPS"] --> M1
    end
    subgraph Validate
      V1["Schema contract<br/>row count, zones"] -->|FAIL| STOP["exit 2<br/>outputs untouched"]
      V1 --> V2["Dataset checks D04-D06<br/>trip rules R01-R10 as flags"]
      V2 --> G["KPI gate G01"]
    end
    subgraph Model
      F["fact_trip (silver)<br/>row conservation D07"]
    end
    subgraph Reconcile
      API["NYC Open Data API<br/>paged, count(*) checked"] --> C["C01 zones, C02 licensees<br/>API down = UNKNOWN"]
    end
    subgraph Publish
      GOLD["gold CSVs + reports<br/>staged, swapped in atomically"] --> DEC["PUBLISH / WITH CAVEATS / HOLD<br/>+ KPI bounds"]
    end
    M1 --> V1
    G --> F --> C --> GOLD
    DEC --> EV["docs/evidence.md"]
```
