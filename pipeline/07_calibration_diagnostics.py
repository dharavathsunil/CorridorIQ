"""
Step 7 - how uneven is ticket coverage, and which scaling option gives physically plausible loads?

Usage:
    python 07_calibration_diagnostics.py "D:\\Transport\\gtfs" tickets_matched.parquet seg_load.parquet [dtc_daily] [cluster_daily]

    dtc_daily, cluster_daily  published daily ridership used ONLY to compute an implied scale factor per
                              operator (defaults: DTC 2,430,000 and cluster 1,530,000 - recent published
                              figures; they are assumptions, override them if the organisers give others).

Prints (paste everything back):
  1. agency table and how agency_id values map to DTC / cluster
  2. per operator: scheduled trips, ticketed pax per day, ticketed pax per scheduled trip, implied scale factor
  3. spread across routes of "ticketed pax per scheduled trip-day" (by operator and route family)
  4. concentration of tickets in the top 1% / 5% / 10% of routes
  5. vehicle-number prefixes by operator (which prefix is which fleet)
  6. weekday high-confidence load quantiles under three scaling options:
        S=1 (ticketed only - a lower bound), one global S, and S per operator
"""
import os
import sys

import duckdb

gdir = sys.argv[1] if len(sys.argv) > 1 else "gtfs"
tm = (sys.argv[2] if len(sys.argv) > 2 else "tickets_matched.parquet").replace("\\", "/")
sl = (sys.argv[3] if len(sys.argv) > 3 else "seg_load.parquet").replace("\\", "/")
DTC = float(sys.argv[4]) if len(sys.argv) > 4 else 2_430_000.0
CLU = float(sys.argv[5]) if len(sys.argv) > 5 else 1_530_000.0
NET = DTC + CLU

con = duckdb.connect()


def load(name):
    p = os.path.join(gdir, name + ".txt").replace("\\", "/")
    if not os.path.exists(p):
        return False
    con.execute(
        f"CREATE TABLE {name} AS SELECT * FROM read_csv('{p}', header=true, all_varchar=true, "
        f"sample_size=-1, ignore_errors=true)"
    )
    return True


for n in ("agency", "routes", "trips"):
    load(n)


def show(title, sql, rows=25):
    print(f"\n=== {title} ===")
    try:
        con.sql(sql).show(max_rows=rows, max_width=220)
    except Exception as e:
        print("  (could not run:", str(e).splitlines()[0], ")")


show("agency.txt", "SELECT * FROM agency")

cols = [r[0] for r in con.execute("DESCRIBE routes").fetchall()]
agency_expr = "upper(coalesce(agency_id, 'UNKNOWN'))" if "agency_id" in cols else "'UNKNOWN'"
con.execute(f"""
CREATE TABLE r AS
SELECT route_id, route_short_name, {agency_expr} AS agency_id,
       CASE WHEN {agency_expr} LIKE '%DTC%'   THEN 'DTC'
            WHEN {agency_expr} LIKE '%DIMTS%' THEN 'cluster'
            ELSE 'other' END AS operator,
       CASE WHEN regexp_matches(route_short_name, '^D-') THEN 'D-series'
            WHEN regexp_matches(route_short_name, '^U-') THEN 'U-series'
            WHEN regexp_matches(route_short_name, '^[0-9]') THEN 'numbered'
            ELSE 'other' END AS family
FROM routes
""")
con.execute("CREATE TABLE rtrips AS SELECT route_id, count(*) AS trips FROM trips GROUP BY 1")
con.execute(f"""
CREATE TABLE typ AS
SELECT route_id, date, pax, vehicle_prefix, vehicle
FROM read_parquet('{tm}') WHERE NOT low_volume_day AND route_id IS NOT NULL
""")
ndays = con.execute("SELECT count(DISTINCT date) FROM typ").fetchall()[0][0]
con.execute(f"""
CREATE TABLE rp AS
SELECT r.route_id, r.route_short_name, r.agency_id, r.operator, r.family,
       coalesce(t.trips, 0) AS trips,
       coalesce(p.pax, 0) * 1.0 / {ndays} AS pax_day
FROM r
LEFT JOIN rtrips t ON t.route_id = r.route_id
LEFT JOIN (SELECT route_id, sum(pax) AS pax FROM typ GROUP BY 1) p ON p.route_id = r.route_id
""")

show("Per operator: coverage and implied scale factor", f"""
SELECT operator, count(*) AS routes, sum(trips) AS sched_trips_per_day,
       round(sum(pax_day), 0) AS ticketed_pax_per_day,
       round(sum(pax_day) / nullif(sum(trips), 0), 2) AS ticketed_per_sched_trip,
       CASE operator WHEN 'DTC' THEN {DTC} WHEN 'cluster' THEN {CLU} END AS published_daily,
       round(CASE operator WHEN 'DTC' THEN {DTC} WHEN 'cluster' THEN {CLU} END / nullif(sum(pax_day), 0), 1) AS implied_S
FROM rp GROUP BY 1 ORDER BY 1""")

show("Per route family", """
SELECT family, operator, count(*) AS routes, sum(trips) AS sched_trips_per_day,
       round(sum(pax_day), 0) AS ticketed_pax_per_day,
       round(sum(pax_day) / nullif(sum(trips), 0), 2) AS ticketed_per_sched_trip
FROM rp GROUP BY 1, 2 ORDER BY 1, 2""")

show("Spread across routes of ticketed pax per scheduled trip-day (routes with tickets)", """
SELECT operator, count(*) AS routes,
       round(quantile_cont(pax_day / trips, 0.10), 2) AS p10,
       round(quantile_cont(pax_day / trips, 0.50), 2) AS p50,
       round(quantile_cont(pax_day / trips, 0.90), 2) AS p90,
       round(quantile_cont(pax_day / trips, 0.99), 2) AS p99,
       round(max(pax_day / trips), 2) AS max_
FROM rp WHERE trips > 0 AND pax_day > 0 GROUP BY 1 ORDER BY 1""")

show("Concentration: share of all ticketed pax coming from the top routes", """
WITH o AS (
  SELECT pax_day, row_number() OVER (ORDER BY pax_day DESC) AS rn,
         count(*) OVER () AS n, sum(pax_day) OVER () AS tot
  FROM rp WHERE pax_day > 0)
SELECT max(n) AS routes_with_tickets,
       round(100 * sum(pax_day) FILTER (WHERE rn <= n * 0.01) / max(tot), 1) AS top_1pct_routes_share,
       round(100 * sum(pax_day) FILTER (WHERE rn <= n * 0.05) / max(tot), 1) AS top_5pct_routes_share,
       round(100 * sum(pax_day) FILTER (WHERE rn <= n * 0.10) / max(tot), 1) AS top_10pct_routes_share
FROM o""")

show("Routes with the most ticketed pax per scheduled trip (possible near-complete coverage)", """
SELECT route_short_name, operator, family, trips AS sched_trips_per_day,
       round(pax_day, 1) AS ticketed_pax_per_day, round(pax_day / trips, 2) AS ticketed_per_trip
FROM rp WHERE trips >= 5 AND pax_day > 0 ORDER BY ticketed_per_trip DESC""", rows=15)

show("Vehicle-number prefix by operator (tagged tickets only)", """
SELECT t.vehicle_prefix, r.operator, count(*) AS tickets, count(DISTINCT t.vehicle) AS vehicles,
       round(count(*) * 1.0 / count(DISTINCT t.vehicle) / (SELECT count(DISTINCT date) FROM typ), 2) AS tickets_per_vehicle_day
FROM typ t JOIN r ON r.route_id = t.route_id
WHERE t.vehicle IS NOT NULL GROUP BY 1, 2 ORDER BY tickets DESC""", rows=15)

# ---- scaling options on weekday high-confidence cells
con.execute(f"""
CREATE TABLE sc AS
SELECT operator, round(CASE operator WHEN 'DTC' THEN {DTC} WHEN 'cluster' THEN {CLU} END
       / nullif((SELECT sum(pax_day) FROM rp r2 WHERE r2.operator = rp.operator), 0), 4) AS s_op
FROM rp GROUP BY operator
""")
con.execute(f"""
CREATE TABLE cells AS
SELECT l.ticketed_per_trip, r.operator, sc.s_op,
       {NET} / (SELECT sum(pax_day) FROM rp) AS s_global
FROM read_parquet('{sl}') l
JOIN r ON r.route_id = l.route_id
LEFT JOIN sc ON sc.operator = r.operator
WHERE l.confidence = 'high' AND l.daytype = 'weekday'
""")
show("Weekday HIGH-confidence cells: load quantiles under three scaling options", """
SELECT scenario, count(*) AS cells,
       round(quantile_cont(load, 0.50), 1) AS p50, round(quantile_cont(load, 0.90), 1) AS p90,
       round(quantile_cont(load, 0.99), 1) AS p99, round(max(load), 1) AS max_load,
       round(100.0 * avg((load > 100)::INT), 1) AS pct_cells_over_100
FROM (
  SELECT 'S=1 (ticketed only, lower bound)' AS scenario, ticketed_per_trip AS load FROM cells
  UNION ALL SELECT 'global S', ticketed_per_trip * s_global FROM cells
  UNION ALL SELECT 'S per operator', ticketed_per_trip * s_op FROM cells WHERE s_op IS NOT NULL
) GROUP BY scenario ORDER BY scenario""")

print("\nDone. Paste everything above.")
