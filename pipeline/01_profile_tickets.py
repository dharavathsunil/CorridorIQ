"""
Step 1 - profile the Transport Stack ticket data (Problem 2).

Usage:
    pip install duckdb
    python 01_profile_tickets.py October-Delhi.csv

Reads the CSV with DuckDB (no need to load it into Excel/pandas) and prints the
facts we need before designing the crowding model. Paste the printed output
back so the next step can be tuned to the real data.
"""
import sys
import duckdb

path = sys.argv[1] if len(sys.argv) > 1 else "October-Delhi.csv"
con = duckdb.connect()

# Read everything as text first, then cast - avoids timestamp/NULL auto-detect surprises.
con.execute(f"""
CREATE OR REPLACE VIEW t AS
SELECT
    TRY_CAST(passenger_count AS INTEGER)            AS pax,
    TRY_CAST(amount AS DOUBLE)                      AS amount,
    vehicle_number                                  AS vehicle,
    ticket_type_id                                  AS ttype,
    start_stop_name                                 AS s,
    end_stop_name                                   AS e,
    route                                           AS route,
    TRY_CAST(substr(created_at, 1, 19) AS TIMESTAMP) AS ts,   -- already IST (+05:30)
    is_ac                                           AS is_ac
FROM read_csv('{path}', header=true, nullstr='NULL', all_varchar=true)
""")


def q(title, sql, rows=25):
    print(f"\n=== {title} ===")
    con.sql(sql).show(max_rows=rows, max_width=200)


q("Overview", """
SELECT count(*) AS rows, sum(pax) AS total_pax,
       min(ts) AS first_ticket, max(ts) AS last_ticket,
       count(DISTINCT route) AS routes, count(DISTINCT s) AS start_stops,
       count(DISTINCT e) AS end_stops, count(DISTINCT vehicle) AS vehicles
FROM t""")

q("Null / bad-value rates", """
SELECT round(100.0*avg((pax IS NULL)::INT),2)     AS pax_null_pct,
       round(100.0*avg((amount IS NULL)::INT),2)  AS amount_null_pct,
       round(100.0*avg((vehicle IS NULL)::INT),2) AS vehicle_null_pct,
       round(100.0*avg((s IS NULL)::INT),2)       AS start_null_pct,
       round(100.0*avg((e IS NULL)::INT),2)       AS end_null_pct,
       round(100.0*avg((ts IS NULL)::INT),2)      AS ts_null_pct,
       round(100.0*avg((amount = 0)::INT),2)      AS zero_fare_pct
FROM t""")

q("ticket_type_id counts (look for pass / free-ride types)",
  "SELECT ttype, count(*) AS n, round(avg(amount),2) AS avg_amount, "
  "sum((amount=0)::INT) AS zero_amount FROM t GROUP BY 1 ORDER BY n DESC")

q("is_ac split", "SELECT is_ac, count(*) AS n, sum(pax) AS pax FROM t GROUP BY 1")

q("passenger_count distribution",
  "SELECT pax, count(*) AS n FROM t GROUP BY 1 ORDER BY 1")

q("Fare per passenger (top values) - proxy for distance travelled",
  "SELECT round(amount/NULLIF(pax,0),2) AS fare_per_pax, count(*) AS n "
  "FROM t GROUP BY 1 ORDER BY n DESC", rows=20)

q("Route direction suffix (UP / DOWN / other)", """
SELECT coalesce(regexp_extract(route, '(UP|DOWN)$', 1), '<none>') AS suffix, count(*) AS n
FROM t GROUP BY 1 ORDER BY n DESC""")

q("Sample route strings that do NOT end in UP/DOWN",
  "SELECT DISTINCT route FROM t WHERE NOT regexp_matches(route, '(UP|DOWN)$') LIMIT 20")

q("Vehicle-number prefixes (bus type hint, e.g. electric vs other)", """
SELECT coalesce(regexp_extract(vehicle, '^[A-Z]{2}[0-9]+[A-Z]+', 0), '<null>') AS prefix,
       count(*) AS n
FROM t GROUP BY 1 ORDER BY n DESC""", rows=15)

q("Tickets per day", """
SELECT CAST(ts AS DATE) AS day, dayname(ts) AS dow, count(*) AS tickets, sum(pax) AS pax
FROM t GROUP BY 1,2 ORDER BY 1""", rows=40)

q("Tickets per hour of day (all days)", """
SELECT hour(ts) AS hr, count(*) AS tickets FROM t GROUP BY 1 ORDER BY 1""", rows=30)

q("Busiest routes", """
SELECT route, count(*) AS tickets, sum(pax) AS pax,
       count(DISTINCT CAST(ts AS DATE)) AS days_seen
FROM t GROUP BY 1 ORDER BY tickets DESC""", rows=15)

q("Thin routes (few tickets -> low confidence)", """
SELECT count(*) FILTER (WHERE n < 100)  AS routes_lt_100,
       count(*) FILTER (WHERE n < 1000) AS routes_lt_1000,
       count(*)                          AS routes_total
FROM (SELECT route, count(*) AS n FROM t GROUP BY 1)""")

q("Same start and end stop (data quality)",
  "SELECT count(*) AS same_stop_rows FROM t WHERE s = e")

q("Most common start stop names (check spelling mess for matching to GTFS)",
  "SELECT s, count(*) AS n FROM t GROUP BY 1 ORDER BY n DESC", rows=15)
