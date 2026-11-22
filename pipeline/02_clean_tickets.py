"""
Step 2 - clean the ticket data and write tickets_clean.parquet.

Usage:
    python 02_clean_tickets.py October-Delhi.csv

What it does (all driven by what the profile showed):
  * parses route strings -> route_base, variant (STL/LNKSTL/...), direction (UP/DOWN), variant_no
    so thin variant routes can later be pooled onto their base route
  * normalises start/end stop names for matching to GTFS (case, punctuation, spacing)
  * adds time fields: date, dow, daytype, hour, 15-minute bin, ISO week
  * fare_per_pax and a fare band (a rough trip-length proxy)
  * bus-type hints from vehicle prefix (vehicle is NULL on ~half the rows)
  * flags anomalous days (daily volume < 80% of the median for the same daytype)
  * drops obviously unusable rows (same start/end stop, missing stop names) into a reject count
"""
import sys
import duckdb

src = sys.argv[1] if len(sys.argv) > 1 else "October-Delhi.csv"
out = sys.argv[2] if len(sys.argv) > 2 else "tickets_clean.parquet"

con = duckdb.connect()

NORM = lambda col: (  # noqa: E731
    f"trim(regexp_replace(regexp_replace(lower({col}), '[^a-z0-9]+', ' ', 'g'), ' +', ' ', 'g'))"
)

con.execute(f"""
CREATE OR REPLACE TABLE raw AS
SELECT
    TRY_CAST(passenger_count AS INTEGER)               AS pax,
    TRY_CAST(amount AS DOUBLE)                         AS amount,
    vehicle_number                                     AS vehicle,
    start_stop_name                                    AS start_raw,
    end_stop_name                                      AS end_raw,
    route                                              AS route_raw,
    TRY_CAST(substr(created_at, 1, 19) AS TIMESTAMP)   AS ts,        -- already IST
    upper(is_ac) = 'TRUE'                              AS is_ac
FROM read_csv('{src}', header=true, nullstr='NULL', all_varchar=true)
""")

con.execute(f"""
CREATE OR REPLACE TABLE parsed AS
SELECT
    *,
    regexp_matches(route_raw, '^(.+?)(LNKSTL|LINKSTL|STL)?(UP|DOWN)([0-9]*)$') AS route_ok,
    CASE WHEN regexp_matches(route_raw, '^(.+?)(LNKSTL|LINKSTL|STL)?(UP|DOWN)([0-9]*)$')
         THEN regexp_extract(route_raw, '^(.+?)(LNKSTL|LINKSTL|STL)?(UP|DOWN)([0-9]*)$', 1)
         ELSE route_raw END                                                    AS route_base,
    nullif(regexp_extract(route_raw, '^(.+?)(LNKSTL|LINKSTL|STL)?(UP|DOWN)([0-9]*)$', 2), '') AS variant,
    nullif(regexp_extract(route_raw, '^(.+?)(LNKSTL|LINKSTL|STL)?(UP|DOWN)([0-9]*)$', 3), '') AS direction,
    nullif(regexp_extract(route_raw, '^(.+?)(LNKSTL|LINKSTL|STL)?(UP|DOWN)([0-9]*)$', 4), '') AS variant_no,
    {NORM('start_raw')}                                                        AS start_norm,
    {NORM('end_raw')}                                                          AS end_norm,
    CAST(ts AS DATE)                                                           AS date,
    isodow(ts)                                                                 AS isodow,
    CASE WHEN isodow(ts) <= 5 THEN 'weekday' WHEN isodow(ts) = 6 THEN 'sat' ELSE 'sun' END AS daytype,
    hour(ts)                                                                   AS hour,
    hour(ts) * 4 + CAST(minute(ts) / 15 AS INTEGER)                            AS bin15,
    weekofyear(ts)                                                             AS iso_week,
    amount / nullif(pax, 0)                                                    AS fare_per_pax,
    coalesce(regexp_extract(vehicle, '^[A-Z]{{2}}[0-9]+[A-Z]+', 0), NULL)      AS vehicle_prefix
FROM raw
""")

# Fare bands: the five common fares are distance bands; everything else is 'other'
con.execute("""
CREATE OR REPLACE TABLE clean0 AS
SELECT *,
    CASE WHEN fare_per_pax IN (4.5, 9.0, 13.5, 18.0, 22.5) THEN CAST(fare_per_pax AS VARCHAR)
         WHEN fare_per_pax IS NULL THEN NULL ELSE 'other' END AS fare_band
FROM parsed
WHERE ts IS NOT NULL AND pax IS NOT NULL AND pax > 0
  AND start_norm <> '' AND end_norm <> '' AND start_norm <> end_norm
""")

# Anomalous days: volume < 80% of the median of the same daytype
con.execute("""
CREATE OR REPLACE TABLE daily AS
SELECT date, daytype, count(*) AS tickets FROM clean0 GROUP BY 1, 2
""")
con.execute("""
CREATE OR REPLACE TABLE day_flags AS
SELECT d.date, d.daytype, d.tickets,
       m.med AS daytype_median,
       d.tickets < 0.8 * m.med AS low_volume_day
FROM daily d
JOIN (SELECT daytype, median(tickets) AS med FROM daily GROUP BY 1) m USING (daytype)
""")
con.execute("""
COPY (
  SELECT c.*, f.low_volume_day
  FROM clean0 c JOIN day_flags f USING (date)
) TO '""" + out + """' (FORMAT PARQUET, COMPRESSION ZSTD)
""")

print("\n=== Row accounting ===")
con.sql("""
SELECT (SELECT count(*) FROM raw)    AS raw_rows,
       (SELECT count(*) FROM clean0) AS kept_rows,
       (SELECT count(*) FROM raw) - (SELECT count(*) FROM clean0) AS dropped_rows
""").show()

print("=== Route parsing ===")
con.sql("""
SELECT route_ok, count(*) AS rows, count(DISTINCT route_raw) AS route_strings,
       count(DISTINCT route_base) AS base_routes
FROM clean0 GROUP BY 1
""").show()

print("=== Route strings that did not parse (special/circular/EV names) ===")
con.sql("""
SELECT route_raw, count(*) AS n FROM clean0 WHERE NOT route_ok
GROUP BY 1 ORDER BY n DESC
""").show(max_rows=25)

print("=== Variants (STL / LNKSTL ...) and numeric suffixes ===")
con.sql("""
SELECT coalesce(variant, '<none>') AS variant, coalesce(variant_no, '<none>') AS variant_no, count(*) AS n
FROM clean0 WHERE route_ok GROUP BY 1, 2 ORDER BY n DESC
""").show(max_rows=20)

print("=== Thin routes AFTER pooling variants onto base route + direction ===")
con.sql("""
SELECT count(*) FILTER (WHERE n < 100)  AS lt_100,
       count(*) FILTER (WHERE n < 1000) AS lt_1000,
       count(*)                          AS total
FROM (SELECT route_base, direction, count(*) AS n FROM clean0 GROUP BY 1, 2)
""").show()

print("=== Is a missing vehicle number random? (share of NULL vehicle by group) ===")
con.sql("""
SELECT is_ac, round(100.0 * avg((vehicle IS NULL)::INT), 1) AS vehicle_null_pct, count(*) AS n
FROM clean0 GROUP BY 1
""").show()
con.sql("""
SELECT hour, round(100.0 * avg((vehicle IS NULL)::INT), 1) AS vehicle_null_pct
FROM clean0 GROUP BY 1 ORDER BY 1
""").show(max_rows=30)

print("=== Day flags (low_volume_day = < 80% of daytype median) ===")
con.sql("SELECT * FROM day_flags ORDER BY date").show(max_rows=40)

print(f"\nWrote {out}")
