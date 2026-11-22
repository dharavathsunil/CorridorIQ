"""
Step 6 - expected on-board load per route x stretch x daytype x hour.

Usage:
    python 06_segment_load.py "D:\\Transport\\gtfs" tickets_matched.parquet [daily_ridership] [--from-date YYYY-MM-DD] [--to-date YYYY-MM-DD] [--suffix _train]

    daily_ridership  network-wide daily bus boardings used to scale the ticket sample up to the
                     whole fleet (default 4,080,000 = published DTC + cluster figure for FY2024-25;
                     this is an ASSUMPTION - change it, or replace with the organisers' figure).

Model:
  * canonical stop order of a route = its longest scheduled trip; a "stretch" (seg) is the pair of
    consecutive stops (idx, idx+1)
  * each matched ticket (board stop, alight stop, sale time) is spread over the stretches between
    its stops; the time at each later stretch = sale time + scheduled offset
  * ticketed_per_trip  = ticketed passengers crossing the stretch in that clock hour per day
                         / scheduled trips crossing it in that hour
  * est_load           = S * ticketed_per_trip   (S = daily_ridership / ticketed passengers per day)
  * each route is re-weighted by (all its ticketed pax) / (pax we could place on the route),
    capped at 4, to correct for tickets lost to unmatched stops
  * confidence from the number of tickets behind a cell: high >= 100 (about +-10%),
    medium >= 25 (about +-20%), else low (Poisson: relative s.e. ~ 1/sqrt(n))

Outputs: seg_load.parquet, canon_stops.parquet
"""
import sys

import duckdb

import argparse

ap = argparse.ArgumentParser()
ap.add_argument("gtfs", nargs="?", default="gtfs")
ap.add_argument("tickets", nargs="?", default="tickets_matched.parquet")
ap.add_argument("ridership", nargs="?", type=float, default=4_080_000.0)
ap.add_argument("--from-date", default=None, help="first ticket date to use, YYYY-MM-DD (for train/test splits)")
ap.add_argument("--to-date", default=None, help="last ticket date to use, YYYY-MM-DD")
ap.add_argument("--suffix", default="", help="suffix for output files, e.g. _train -> seg_load_train.parquet")
A = ap.parse_args()
gdir = A.gtfs
tm = A.tickets.replace("\\", "/")
RIDERSHIP = A.ridership
SUFFIX = A.suffix
W_CAP = 4.0
HIGH_N, MED_N = 100, 25

import os  # noqa: E402

con = duckdb.connect()
DATE_FILTER = (f" AND date >= DATE '{A.from_date}'" if A.from_date else "") + (f" AND date <= DATE '{A.to_date}'" if A.to_date else "")


def load(name):
    p = os.path.join(gdir, name + ".txt").replace("\\", "/")
    con.execute(
        f"CREATE TABLE {name} AS SELECT * FROM read_csv('{p}', header=true, all_varchar=true, "
        f"sample_size=-1, ignore_errors=true)"
    )


for n in ("routes", "stops", "trips", "stop_times"):
    load(n)


def show(title, sql, rows=25):
    print(f"\n=== {title} ===")
    try:
        con.sql(sql).show(max_rows=rows, max_width=220)
    except Exception as e:  # keep going so one failed check does not hide the others
        print("  (could not run:", str(e).splitlines()[0], ")")


# ---------------------------------------------------------------- schedule tables
con.execute("""
CREATE TABLE st AS
SELECT trip_id, stop_id, CAST(stop_sequence AS INTEGER) AS seq,
       TRY_CAST(split_part(arrival_time,   ':', 1) AS INTEGER) * 3600
     + TRY_CAST(split_part(arrival_time,   ':', 2) AS INTEGER) * 60
     + TRY_CAST(split_part(arrival_time,   ':', 3) AS INTEGER) AS arr_s,
       TRY_CAST(split_part(departure_time, ':', 1) AS INTEGER) * 3600
     + TRY_CAST(split_part(departure_time, ':', 2) AS INTEGER) * 60
     + TRY_CAST(split_part(departure_time, ':', 3) AS INTEGER) AS dep_s
FROM stop_times
""")
con.execute("""
CREATE TABLE canon_trip AS
SELECT route_id, trip_id FROM (
    SELECT t.route_id, s.trip_id,
           row_number() OVER (PARTITION BY t.route_id ORDER BY count(*) DESC, s.trip_id) AS rk
    FROM st s JOIN trips t ON t.trip_id = s.trip_id GROUP BY t.route_id, s.trip_id)
WHERE rk = 1
""")
con.execute("""
CREATE TABLE canon AS
SELECT c.route_id, s.stop_id,
       row_number() OVER (PARTITION BY c.route_id ORDER BY s.seq) - 1 AS idx,
       coalesce(s.arr_s, s.dep_s) - first_value(coalesce(s.arr_s, s.dep_s)) OVER (PARTITION BY c.route_id ORDER BY s.seq) AS off_s
FROM canon_trip c JOIN st s ON s.trip_id = c.trip_id
""")
con.execute("""
CREATE TABLE canon_ix AS
SELECT route_id, stop_id, min(idx) AS idx_min, max(idx) AS idx_max FROM canon GROUP BY 1, 2
""")

# ---------------------------------------------------------------- usable tickets
con.execute(f"""
CREATE TABLE typical AS
SELECT route_id, date, daytype, pax, ts, start_stop_id, end_stop_id
FROM read_parquet('{tm}') WHERE NOT low_volume_day{DATE_FILTER}
""")
con.execute("""
CREATE TABLE tk AS
SELECT m.route_id, m.date, m.daytype, m.pax,
       hour(m.ts) * 3600 + minute(m.ts) * 60 + CAST(second(m.ts) AS INTEGER) AS sod,
       b.idx_min AS b_idx, a.idx_max AS a_idx
FROM typical m
JOIN canon_ix b ON b.route_id = m.route_id AND b.stop_id = m.start_stop_id
JOIN canon_ix a ON a.route_id = m.route_id AND a.stop_id = m.end_stop_id
WHERE b.idx_min < a.idx_max
""")
con.execute(f"""
CREATE TABLE rw AS
SELECT tot.route_id, LEAST({W_CAP}, tot.pax * 1.0 / u.pax) AS w
FROM (SELECT route_id, sum(pax) AS pax FROM typical WHERE route_id IS NOT NULL GROUP BY 1) tot
JOIN (SELECT route_id, sum(pax) AS pax FROM tk GROUP BY 1) u USING (route_id)
""")
con.execute("CREATE TABLE ndays AS SELECT daytype, count(DISTINCT date) AS n_days FROM typical GROUP BY 1")

daily_pax = con.execute("SELECT sum(pax) * 1.0 / count(DISTINCT date) FROM typical").fetchall()[0][0]
S = RIDERSHIP / daily_pax

show("Parameters", f"""
SELECT {RIDERSHIP:.0f} AS assumed_daily_ridership,
       round({daily_pax}, 0) AS ticketed_pax_per_typical_day,
       round({S}, 2) AS scale_factor_S""")
show("Usable tickets (matched route + both stops on the route's canonical pattern, boarding before alighting)", """
SELECT (SELECT count(*) FROM typical WHERE route_id IS NOT NULL) AS route_matched_rows,
       (SELECT count(*) FROM tk) AS usable_rows,
       round(100.0 * (SELECT count(*) FROM tk) / (SELECT count(*) FROM typical WHERE route_id IS NOT NULL), 2) AS usable_pct""")
show("Route weights (correction for tickets lost to unmatched stops)", f"""
SELECT round(min(w), 2) AS min_w, round(median(w), 2) AS median_w, round(quantile_cont(w, 0.95), 2) AS p95_w,
       sum((w >= {W_CAP})::INT) AS routes_at_cap, count(*) AS routes FROM rw""")

# ---------------------------------------------------------------- spread tickets over stretches
print("\nSpreading tickets over stretches (this is the heavy step) ...")
con.execute("""
CREATE TABLE agg AS
SELECT e.route_id, e.daytype, e.seg,
       CAST(floor((((e.sod + cs.off_s - cb.off_s) % 86400) + 86400) % 86400 / 3600.0) AS INTEGER) AS hr,
       sum(e.pax * rw.w) AS wpax, count(*) AS n_obs
FROM (SELECT tk.*, unnest(generate_series(b_idx, a_idx - 1)) AS seg FROM tk) e
JOIN canon cs ON cs.route_id = e.route_id AND cs.idx = e.seg
JOIN canon cb ON cb.route_id = e.route_id AND cb.idx = e.b_idx
JOIN rw ON rw.route_id = e.route_id
GROUP BY ALL
""")

# ---------------------------------------------------------------- scheduled trips per stretch-hour
con.execute("""
CREATE TABLE seg_trips AS
WITH pairs AS (
    SELECT t.route_id, s.stop_id, s.dep_s,
           lead(s.stop_id) OVER (PARTITION BY s.trip_id ORDER BY s.seq) AS next_stop
    FROM st s JOIN trips t ON t.trip_id = s.trip_id
)
SELECT p.route_id, c.idx AS seg,
       CAST(floor(((p.dep_s % 86400) + 86400) % 86400 / 3600.0) AS INTEGER) AS hr,
       count(*) AS trips
FROM pairs p
JOIN canon c  ON c.route_id = p.route_id AND c.stop_id = p.stop_id
JOIN canon c2 ON c2.route_id = p.route_id AND c2.idx = c.idx + 1 AND c2.stop_id = p.next_stop
WHERE p.dep_s IS NOT NULL
GROUP BY ALL
""")

show("Ticket-hours with NO scheduled trip in that hour (schedule mismatch; dropped)", """
SELECT count(*) AS cells, round(100.0 * sum(a.n_obs) / (SELECT sum(n_obs) FROM agg), 2) AS pct_of_ticket_stretch_events
FROM agg a LEFT JOIN seg_trips t ON t.route_id = a.route_id AND t.seg = a.seg AND t.hr = a.hr
WHERE t.trips IS NULL""")

# ---------------------------------------------------------------- final table
con.execute(f"""
CREATE TABLE seg_load AS
SELECT g.route_id, g.seg, d.daytype, g.hr AS hour, g.trips AS trips_per_hour,
       coalesce(a.wpax, 0) / d.n_days                                        AS ticketed_per_day,
       coalesce(a.wpax, 0) / d.n_days / g.trips                              AS ticketed_per_trip,
       {S} * coalesce(a.wpax, 0) / d.n_days / g.trips                        AS est_load,
       coalesce(a.n_obs, 0)                                                  AS n_obs,
       CASE WHEN coalesce(a.n_obs, 0) > 0 THEN 1.0 / sqrt(a.n_obs) END       AS rel_se,
       CASE WHEN coalesce(a.n_obs, 0) >= {HIGH_N} THEN 'high'
            WHEN coalesce(a.n_obs, 0) >= {MED_N}  THEN 'medium' ELSE 'low' END AS confidence
FROM seg_trips g
CROSS JOIN ndays d
LEFT JOIN agg a ON a.route_id = g.route_id AND a.seg = g.seg AND a.hr = g.hr AND a.daytype = d.daytype
""")
con.execute(f"COPY seg_load TO 'seg_load{SUFFIX}.parquet' (FORMAT PARQUET, COMPRESSION ZSTD)")
con.execute(f"COPY canon TO 'canon_stops{SUFFIX}.parquet' (FORMAT PARQUET, COMPRESSION ZSTD)")

# ---------------------------------------------------------------- sanity checks
show("Cells by confidence and daytype", """
SELECT daytype, confidence, cells,
       round(100.0 * cells / sum(cells) OVER (PARTITION BY daytype), 1) AS pct
FROM (SELECT daytype, confidence, count(*) AS cells FROM seg_load GROUP BY daytype, confidence)
ORDER BY daytype, confidence""")

show("Mean estimated load per trip-stretch by hour (weekday vs sunday) - expect peaks ~8-9 and ~18-19", """
SELECT hour,
       round(sum(est_load * trips_per_hour) FILTER (WHERE daytype = 'weekday') /
             sum(trips_per_hour) FILTER (WHERE daytype = 'weekday'), 2) AS weekday_mean_load,
       round(sum(est_load * trips_per_hour) FILTER (WHERE daytype = 'sun') /
             sum(trips_per_hour) FILTER (WHERE daytype = 'sun'), 2) AS sunday_mean_load
FROM seg_load GROUP BY 1 ORDER BY 1""", rows=30)

show("Distribution of est_load in HIGH-confidence weekday cells (is it physically plausible?)", """
SELECT count(*) AS cells,
       round(quantile_cont(est_load, 0.5), 1)  AS p50,
       round(quantile_cont(est_load, 0.9), 1)  AS p90,
       round(quantile_cont(est_load, 0.99), 1) AS p99,
       round(max(est_load), 1)                 AS max_load
FROM seg_load WHERE confidence = 'high' AND daytype = 'weekday'""")

show("Busiest HIGH-confidence weekday stretches", """
SELECT r.route_short_name AS route, r.route_desc AS description, l.hour, l.seg,
       s1.stop_name AS from_stop, s2.stop_name AS to_stop,
       round(l.est_load, 1) AS est_load, l.n_obs, l.trips_per_hour AS trips
FROM seg_load l
JOIN routes r ON r.route_id = l.route_id
JOIN canon c1 ON c1.route_id = l.route_id AND c1.idx = l.seg
JOIN canon c2 ON c2.route_id = l.route_id AND c2.idx = l.seg + 1
JOIN stops s1 ON s1.stop_id = c1.stop_id
JOIN stops s2 ON s2.stop_id = c2.stop_id
WHERE l.confidence = 'high' AND l.daytype = 'weekday'
ORDER BY l.est_load DESC""", rows=15)

print(f"\nWrote seg_load{SUFFIX}.parquet and canon_stops{SUFFIX}.parquet. Paste everything above.")
