"""
Step 8 (v2) - Crowding Index, 4 crowding levels, confidence, sensitivity and a week-to-week stability check.

Usage:
    python 08_crowding_levels.py "D:\\Transport\\gtfs" tickets_matched.parquet seg_load.parquet canon_stops.parquet [dtc_daily] [cluster_daily] [--no-adjust]

Why not absolute loads: step 7 showed ticket coverage differs by operator and no single scale factor
gives physically plausible loads. So we report a RELATIVE index built from the ticketed passengers
(a floor on the true load):

  CI = ticketed passengers per scheduled trip on a stretch, in a time band, smoothed over 5 consecutive
       stretches, times an operator coverage factor (S_operator / S_network; --no-adjust disables it).
       The factor uses published daily ridership (assumption; defaults DTC 2,430,000, cluster 1,530,000).

  Levels use weekday, trips-weighted percentiles of CI over well-supported cells:
       < P50 -> 1 seats likely | P50-P80 -> 2 standing room | P80-P95 -> 3 crowded | >= P95 -> 4 very crowded
  Confidence from the number of tickets behind the route/band: high >= 300, medium >= 60, else low
  (low-confidence cells get level 0 = "no data").

Time bands (equal-ish widths for the two peaks): night 0-5, early 6-7, am_peak 8-10, midday 11-16,
pm_peak 17-19, late 20-23.

Outputs: crowd_levels.parquet (route_id, daytype, band, seg, from_stop_id, to_stop_id, ci, ci_raw, level, level_raw, ...)
"""
import os
import sys

import duckdb

args = [a for a in sys.argv[1:] if not a.startswith("--")]
NO_ADJUST = "--no-adjust" in sys.argv
gdir = args[0] if len(args) > 0 else "gtfs"
tm = (args[1] if len(args) > 1 else "tickets_matched.parquet").replace("\\", "/")
sl = (args[2] if len(args) > 2 else "seg_load.parquet").replace("\\", "/")
cn = (args[3] if len(args) > 3 else "canon_stops.parquet").replace("\\", "/")
DTC = float(args[4]) if len(args) > 4 else 2_430_000.0
CLU = float(args[5]) if len(args) > 5 else 1_530_000.0
WIN = 2                           # smoothing window: +-2 stretches
HIGH_T, MED_T = 300, 60           # tickets per route/band for confidence
P1, P2, P3 = 0.50, 0.80, 0.95     # level cut-offs (percentiles)
DAY_BANDS = "('early', 'am_peak', 'midday', 'pm_peak', 'late')"

con = duckdb.connect()


def load(name):
    p = os.path.join(gdir, name + ".txt").replace("\\", "/")
    con.execute(
        f"CREATE TABLE {name} AS SELECT * FROM read_csv('{p}', header=true, all_varchar=true, "
        f"sample_size=-1, ignore_errors=true)"
    )


for n in ("routes", "stops", "trips", "stop_times"):
    load(n)


def band(h):
    return (f"CASE WHEN {h} < 6 THEN 'night' WHEN {h} < 8 THEN 'early' WHEN {h} < 11 THEN 'am_peak' "
            f"WHEN {h} < 17 THEN 'midday' WHEN {h} < 20 THEN 'pm_peak' ELSE 'late' END")


BAND_ORDER = ("CASE band WHEN 'night' THEN 0 WHEN 'early' THEN 1 WHEN 'am_peak' THEN 2 "
              "WHEN 'midday' THEN 3 WHEN 'pm_peak' THEN 4 ELSE 5 END")


def show(title, sql, rows=30):
    print(f"\n=== {title} ===")
    try:
        con.sql(sql).show(max_rows=rows, max_width=220)
    except Exception as e:
        print("  (could not run:", str(e).splitlines()[0], ")")


# ------------------------------------------------------------------ operators and coverage factor
cols = [r[0] for r in con.execute("DESCRIBE routes").fetchall()]
agency_expr = "upper(coalesce(agency_id, 'UNKNOWN'))" if "agency_id" in cols else "'UNKNOWN'"
con.execute(f"""
CREATE TABLE r AS
SELECT route_id, route_short_name,
       CASE WHEN {agency_expr} LIKE '%DTC%' THEN 'DTC' WHEN {agency_expr} LIKE '%DIMTS%' THEN 'cluster' ELSE 'other' END AS operator
FROM routes
""")
ndays = con.execute(f"SELECT count(DISTINCT date) FROM read_parquet('{tm}') WHERE NOT low_volume_day").fetchall()[0][0]
con.execute(f"""
CREATE TABLE opf AS
WITH t AS (
    SELECT r.operator, sum(m.pax) * 1.0 / {ndays} AS pax_day
    FROM read_parquet('{tm}') m JOIN r ON r.route_id = m.route_id
    WHERE NOT m.low_volume_day GROUP BY 1)
SELECT operator, pax_day,
       CASE WHEN {str(NO_ADJUST).upper()} THEN 1.0
            WHEN operator = 'DTC'     THEN ({DTC} / pax_day) / (({DTC} + {CLU}) / (SELECT sum(pax_day) FROM t))
            WHEN operator = 'cluster' THEN ({CLU} / pax_day) / (({DTC} + {CLU}) / (SELECT sum(pax_day) FROM t))
            ELSE 1.0 END AS f
FROM t
""")
show("Operator coverage factor applied to the index (f>1 = operator under-ticketed, so its tickets count for more)",
     "SELECT operator, round(pax_day, 0) AS ticketed_pax_per_day, round(f, 3) AS factor FROM opf")

# ------------------------------------------------------------------ banded + smoothed index
con.execute(f"""
CREATE TABLE sb AS
SELECT route_id, daytype, {band('hour')} AS band, seg,
       sum(ticketed_per_day) AS tick, sum(trips_per_hour) AS trips
FROM read_parquet('{sl}') GROUP BY ALL
""")
con.execute(f"""
CREATE TABLE sm AS
SELECT *, sum(tick)  OVER w AS tick_w, sum(trips) OVER w AS trips_w
FROM sb
WINDOW w AS (PARTITION BY route_id, daytype, band ORDER BY seg ROWS BETWEEN {WIN} PRECEDING AND {WIN} FOLLOWING)
""")
con.execute(f"""
CREATE TABLE nt AS
SELECT route_id, daytype, {band('hour(ts)')} AS band, count(*) AS n_tix
FROM read_parquet('{tm}')
WHERE NOT low_volume_day AND route_id IS NOT NULL AND start_stop_id IS NOT NULL AND end_stop_id IS NOT NULL
GROUP BY ALL
""")
con.execute(f"""
CREATE TABLE cells AS
SELECT s.route_id, s.daytype, s.band, s.seg, s.trips,
       s.tick_w / nullif(s.trips_w, 0)                          AS ci_raw,
       (s.tick_w / nullif(s.trips_w, 0)) * coalesce(o.f, 1.0)   AS ci,
       coalesce(n.n_tix, 0) AS n_tix,
       CASE WHEN coalesce(n.n_tix, 0) >= {HIGH_T} THEN 'high'
            WHEN coalesce(n.n_tix, 0) >= {MED_T}  THEN 'medium' ELSE 'low' END AS confidence
FROM sm s
JOIN r ON r.route_id = s.route_id
LEFT JOIN opf o ON o.operator = r.operator
LEFT JOIN nt n ON n.route_id = s.route_id AND n.daytype = s.daytype AND n.band = s.band
WHERE s.trips_w > 0
""")


# ------------------------------------------------------------------ level thresholds
def thresholds(col):
    def q(extra):
        return con.execute(f"""
        WITH w AS (
            SELECT {col} AS v, trips, sum(trips) OVER (ORDER BY {col}) AS cum, sum(trips) OVER () AS tot
            FROM cells WHERE daytype = 'weekday' AND band IN {DAY_BANDS} AND {col} IS NOT NULL {extra})
        SELECT min(v) FILTER (WHERE cum >= {P1} * tot), min(v) FILTER (WHERE cum >= {P2} * tot),
               min(v) FILTER (WHERE cum >= {P3} * tot) FROM w""").fetchall()[0]
    t = q("AND confidence IN ('high', 'medium')")
    if any(x is None for x in t):
        print("WARNING: no medium/high-confidence weekday cells; using all cells for thresholds.")
        t = q("")
    return [float(x) if x is not None else d for x, d in zip(t, (1.0, 2.0, 3.0))]


T = thresholds("ci")
T_RAW = thresholds("ci_raw")
print(f"\nLevel thresholds on CI (weekday, medium+high confidence, trips-weighted percentiles "
      f"{int(P1*100)}/{int(P2*100)}/{int(P3*100)}):")
print(f"  seats likely < {T[0]:.2f} <= standing room < {T[1]:.2f} <= crowded < {T[2]:.2f} <= very crowded")


def level_sql(col, t):
    return (f"CASE WHEN confidence = 'low' THEN 0 WHEN {col} < {t[0]} THEN 1 "
            f"WHEN {col} < {t[1]} THEN 2 WHEN {col} < {t[2]} THEN 3 ELSE 4 END")


con.execute(f"""
CREATE TABLE final AS
SELECT c.*, {level_sql('ci', T)} AS level, {level_sql('ci_raw', T_RAW)} AS level_raw,
       c1.stop_id AS from_stop_id, c2.stop_id AS to_stop_id
FROM cells c
LEFT JOIN read_parquet('{cn}') c1 ON c1.route_id = c.route_id AND c1.idx = c.seg
LEFT JOIN read_parquet('{cn}') c2 ON c2.route_id = c.route_id AND c2.idx = c.seg + 1
""")
con.execute("COPY final TO 'crowd_levels.parquet' (FORMAT PARQUET, COMPRESSION ZSTD)")

show("Weekday: share of scheduled trip-stretches by confidence (trips-weighted)", """
SELECT confidence, round(100.0 * sum(trips) / sum(sum(trips)) OVER (), 1) AS pct_of_trip_stretches
FROM final WHERE daytype = 'weekday' GROUP BY 1 ORDER BY 1""")


def mix(daytype):
    return f"""
SELECT band,
       round(100.0 * sum(trips) FILTER (WHERE level = 0) / sum(trips), 1) AS no_data,
       round(100.0 * sum(trips) FILTER (WHERE level = 1) / sum(trips), 1) AS seats,
       round(100.0 * sum(trips) FILTER (WHERE level = 2) / sum(trips), 1) AS standing,
       round(100.0 * sum(trips) FILTER (WHERE level = 3) / sum(trips), 1) AS crowded,
       round(100.0 * sum(trips) FILTER (WHERE level = 4) / sum(trips), 1) AS very_crowded
FROM final WHERE daytype = '{daytype}' GROUP BY 1 ORDER BY {BAND_ORDER}"""


show("Weekday level mix by time band (trips-weighted %)", mix("weekday"))
show("Saturday level mix (weekday thresholds)", mix("sat"))
show("Sunday level mix (weekday thresholds) - should look lighter", mix("sun"))

show("SENSITIVITY: weekday cells (medium/high confidence) whose level changes if the operator adjustment is removed", """
SELECT round(100.0 * sum(trips) FILTER (WHERE level <> level_raw) / sum(trips), 1) AS pct_trip_stretches_level_changes,
       round(100.0 * sum(trips) FILTER (WHERE abs(level - level_raw) >= 2) / sum(trips), 1) AS pct_change_by_2_or_more
FROM final WHERE daytype = 'weekday' AND confidence IN ('high', 'medium')""")

# ------------------------------------------------------------------ worked example: busiest route
ex = con.execute("""
SELECT route_id FROM final WHERE daytype = 'weekday' AND confidence = 'high'
GROUP BY 1 ORDER BY max(n_tix) DESC LIMIT 1""").fetchall()
if ex:
    rid = ex[0][0]
    show(f"Worked example (route_id {rid}): every 3rd stretch, weekday AM peak vs PM peak", f"""
    SELECT (SELECT route_short_name FROM r WHERE r.route_id = f.route_id) AS route, f.band, f.seg,
           s1.stop_name AS from_stop, s2.stop_name AS to_stop, round(f.ci, 2) AS ci, f.level, f.n_tix
    FROM final f
    LEFT JOIN stops s1 ON s1.stop_id = f.from_stop_id
    LEFT JOIN stops s2 ON s2.stop_id = f.to_stop_id
    WHERE f.route_id = '{rid}' AND f.daytype = 'weekday' AND f.band IN ('am_peak', 'pm_peak') AND f.seg % 3 = 0
    ORDER BY f.band, f.seg""", rows=60)

# ------------------------------------------------------------------ stability check (week blocks)
con.execute("""
CREATE TABLE st AS
SELECT trip_id, CAST(stop_sequence AS INTEGER) AS seq,
       TRY_CAST(split_part(departure_time, ':', 1) AS INTEGER) * 3600
     + TRY_CAST(split_part(departure_time, ':', 2) AS INTEGER) * 60
     + TRY_CAST(split_part(departure_time, ':', 3) AS INTEGER) AS dep_s
FROM stop_times
""")
con.execute(f"""
CREATE TABLE rb_trips AS
SELECT t.route_id,
       {band('CAST(floor(((f.dep_s % 86400) + 86400) % 86400 / 3600.0) AS INTEGER)')} AS band,
       count(*) AS trips
FROM (SELECT trip_id, arg_min(dep_s, seq) AS dep_s FROM st GROUP BY 1) f
JOIN trips t ON t.trip_id = f.trip_id WHERE f.dep_s IS NOT NULL GROUP BY ALL
""")
con.execute(f"""
CREATE TABLE blk AS
SELECT route_id, {band('hour(ts)')} AS band, date, pax,
       1 + CAST(floor((day(date) - 1) / 7) AS INTEGER) AS blk
FROM read_parquet('{tm}')
WHERE NOT low_volume_day AND route_id IS NOT NULL AND daytype = 'weekday'
  AND start_stop_id IS NOT NULL AND end_stop_id IS NOT NULL
""")
con.execute("CREATE TABLE blk_days AS SELECT blk, count(DISTINCT date) AS nd FROM blk GROUP BY 1")
con.execute("""
CREATE TABLE rbk AS
SELECT b.route_id, b.band, b.blk, count(*) AS tix,
       sum(b.pax) * 1.0 / d.nd / t.trips AS ci
FROM blk b JOIN blk_days d ON d.blk = b.blk
JOIN rb_trips t ON t.route_id = b.route_id AND t.band = b.band
WHERE b.blk <= 4 GROUP BY b.route_id, b.band, b.blk, d.nd, t.trips
""")
PAIRS = """
SELECT x.blk AS a, x.ci AS ca, y.ci AS cb, least(x.tix, y.tix) AS mt
FROM rbk x JOIN rbk y ON y.route_id = x.route_id AND y.band = x.band AND y.blk = x.blk + 1
WHERE x.band IN ('am_peak', 'midday', 'pm_peak') AND least(x.tix, y.tix) >= 30"""
show("STABILITY: Spearman rank correlation of the route x band index between consecutive weeks "
     "(weekdays; route-bands with >= 30 tickets in both weeks)", f"""
WITH pairs AS ({PAIRS}),
ranked AS (SELECT *, rank() OVER (PARTITION BY a ORDER BY ca) AS ra,
                      rank() OVER (PARTITION BY a ORDER BY cb) AS rb FROM pairs)
SELECT 'week ' || a || ' vs week ' || (a + 1) AS comparison, count(*) AS route_bands,
       round(corr(ra, rb), 3) AS spearman
FROM ranked GROUP BY a ORDER BY a""")
show("STABILITY by amount of data (consecutive weeks pooled)", f"""
WITH pairs AS (SELECT *, CASE WHEN mt >= 150 THEN '>=150 tickets/week' ELSE '30-149 tickets/week' END AS tier
               FROM ({PAIRS})),
ranked AS (SELECT *, rank() OVER (PARTITION BY tier ORDER BY ca) AS ra,
                      rank() OVER (PARTITION BY tier ORDER BY cb) AS rb FROM pairs)
SELECT tier, count(*) AS route_band_pairs, round(corr(ra, rb), 3) AS spearman
FROM ranked GROUP BY tier ORDER BY tier""")

print("\nWrote crowd_levels.parquet. Paste everything above.")
