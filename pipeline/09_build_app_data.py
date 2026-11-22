"""
Step 9 - build the database used by the journey-planner app.

Usage (from the transport_app folder):
    python pipeline/09_build_app_data.py "D:\\Transport\\gtfs" crowd_levels.parquet canon_stops.parquet \\
           --seg-load seg_load.parquet --tickets tickets_matched.parquet [--out app/data/app.duckdb] [--blend-k 10] [--hour-floor 20]

--seg-load and --tickets are optional; with them the app also gets hour-by-hour crowding for routes with
enough tickets (>= --hour-floor per route, day type and hour). The hourly estimate is blended with the time-band
estimate: weight n/(n+K) on the hour (n = route-hour tickets, K = --blend-k), so thin hours lean on the band.

Inputs : static GTFS folder, crowd_levels.parquet (step 8), canon_stops.parquet (step 6)
Output : app/data/app.duckdb with tables
    stops(stop_id, stop_name, lat, lon)
    routes(route_id, short_name, description, operator)
    st(trip_id, route_id, stop_id, seq, arr_s, dep_s)        -- timetable, sorted by stop for fast lookups
    canon(route_id, stop_id, idx, off_s)                      -- canonical stop order per route
    levels(route_id, daytype, band, seg, ci, level, confidence, n_tix, trips)
    walk_pairs(a, b, dist_m)                                  -- stops within 250 m (transfers)
    seg_geom(route_id, seg, pts)                              -- road-following polyline of each stretch (from shapes.txt)
    meta(k, v)                                                -- level thresholds etc.
"""
import os
import sys

import duckdb

import argparse

ap = argparse.ArgumentParser()
ap.add_argument("gtfs", nargs="?", default="gtfs")
ap.add_argument("levels", nargs="?", default="crowd_levels.parquet")
ap.add_argument("canon", nargs="?", default="canon_stops.parquet")
ap.add_argument("--out", default=os.path.join("app", "data", "app.duckdb"))
ap.add_argument("--seg-load", default=None)
ap.add_argument("--tickets", default=None)
ap.add_argument("--hour-floor", type=int, default=20, help="min route-hour tickets before an hourly estimate is used at all")
ap.add_argument("--blend-k", type=int, default=10, help="hourly estimate gets weight n/(n+K), the time-band estimate the rest")
A = ap.parse_args()
gdir = A.gtfs
levels = A.levels.replace("\\", "/")
canon = A.canon.replace("\\", "/")
out = A.out
P1, P2, P3 = 0.50, 0.80, 0.95
DAY_BANDS = "('early', 'am_peak', 'midday', 'pm_peak', 'late')"

os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
if os.path.exists(out):
    os.remove(out)
con = duckdb.connect(out)


def load(name):
    p = os.path.join(gdir, name + ".txt").replace("\\", "/")
    con.execute(
        f"CREATE TEMP TABLE g_{name} AS SELECT * FROM read_csv('{p}', header=true, all_varchar=true, "
        f"sample_size=-1, ignore_errors=true)"
    )


for n in ("routes", "stops", "trips", "stop_times"):
    load(n)

con.execute("""
CREATE TABLE stops AS
SELECT stop_id, stop_name, TRY_CAST(stop_lat AS DOUBLE) AS lat, TRY_CAST(stop_lon AS DOUBLE) AS lon
FROM g_stops WHERE TRY_CAST(stop_lat AS DOUBLE) IS NOT NULL AND TRY_CAST(stop_lon AS DOUBLE) IS NOT NULL
""")

cols = [r[0] for r in con.execute("DESCRIBE g_routes").fetchall()]
agency_expr = "upper(coalesce(agency_id, 'UNKNOWN'))" if "agency_id" in cols else "'UNKNOWN'"
desc_expr = "coalesce(route_desc, route_long_name)" if "route_desc" in cols else "route_long_name"
con.execute(f"""
CREATE TABLE routes AS
SELECT route_id, route_short_name AS short_name, {desc_expr} AS description,
       CASE WHEN {agency_expr} LIKE '%DTC%' THEN 'DTC' WHEN {agency_expr} LIKE '%DIMTS%' THEN 'Cluster (DIMTS)' ELSE 'Other' END AS operator
FROM g_routes
""")

con.execute("""
CREATE TABLE st AS
SELECT s.trip_id, t.route_id, s.stop_id, CAST(s.stop_sequence AS INTEGER) AS seq,
       coalesce(
         TRY_CAST(split_part(s.arrival_time, ':', 1) AS INTEGER) * 3600
       + TRY_CAST(split_part(s.arrival_time, ':', 2) AS INTEGER) * 60
       + TRY_CAST(split_part(s.arrival_time, ':', 3) AS INTEGER),
         TRY_CAST(split_part(s.departure_time, ':', 1) AS INTEGER) * 3600
       + TRY_CAST(split_part(s.departure_time, ':', 2) AS INTEGER) * 60
       + TRY_CAST(split_part(s.departure_time, ':', 3) AS INTEGER)) AS arr_s,
       coalesce(
         TRY_CAST(split_part(s.departure_time, ':', 1) AS INTEGER) * 3600
       + TRY_CAST(split_part(s.departure_time, ':', 2) AS INTEGER) * 60
       + TRY_CAST(split_part(s.departure_time, ':', 3) AS INTEGER),
         TRY_CAST(split_part(s.arrival_time, ':', 1) AS INTEGER) * 3600
       + TRY_CAST(split_part(s.arrival_time, ':', 2) AS INTEGER) * 60
       + TRY_CAST(split_part(s.arrival_time, ':', 3) AS INTEGER)) AS dep_s
FROM g_stop_times s JOIN g_trips t ON t.trip_id = s.trip_id
ORDER BY s.stop_id, s.trip_id, seq
""")

con.execute(f"CREATE TABLE canon AS SELECT route_id, stop_id, CAST(idx AS INTEGER) AS idx, off_s FROM read_parquet('{canon}')")
con.execute(f"""
CREATE TABLE levels AS
SELECT route_id, daytype, band, CAST(seg AS INTEGER) AS seg, ci, CAST(level AS INTEGER) AS level,
       confidence, n_tix, trips
FROM read_parquet('{levels}')
""")

con.execute("""
CREATE TABLE walk_pairs AS
SELECT a, b, dist_m FROM (
  SELECT x.stop_id AS a, y.stop_id AS b,
         2 * 6371000 * asin(sqrt(pow(sin(radians(y.lat - x.lat) / 2), 2)
             + cos(radians(x.lat)) * cos(radians(y.lat)) * pow(sin(radians(y.lon - x.lon) / 2), 2))) AS dist_m
  FROM stops x JOIN stops y ON abs(x.lat - y.lat) < 0.0025 AND abs(x.lon - y.lon) < 0.0025)
WHERE dist_m <= 250
""")

# level thresholds: same rule as step 8 (weekday, medium+high confidence, trips-weighted percentiles)
t = con.execute(f"""
WITH w AS (
    SELECT ci AS v, trips, sum(trips) OVER (ORDER BY ci) AS cum, sum(trips) OVER () AS tot
    FROM levels WHERE daytype = 'weekday' AND band IN {DAY_BANDS} AND confidence IN ('high', 'medium') AND ci IS NOT NULL)
SELECT min(v) FILTER (WHERE cum >= {P1} * tot), min(v) FILTER (WHERE cum >= {P2} * tot),
       min(v) FILTER (WHERE cum >= {P3} * tot) FROM w
""").fetchall()[0]
t = [float(x) if x is not None else d for x, d in zip(t, (0.34, 0.90, 2.13))]
con.execute("CREATE TABLE meta(k VARCHAR, v VARCHAR)")
for k, v in (("t1", t[0]), ("t2", t[1]), ("t3", t[2]), ("p1", P1), ("p2", P2), ("p3", P3)):
    con.execute("INSERT INTO meta VALUES (?, ?)", [k, str(v)])


# ---- hour-by-hour crowding for routes with enough tickets in that hour
if A.seg_load and A.tickets:
    sl = A.seg_load.replace("\\", "/")
    tm = A.tickets.replace("\\", "/")
    con.execute(f"""
    CREATE TEMP TABLE rh AS
    SELECT route_id, daytype, hour(ts) AS hour, count(*) AS n_tix
    FROM read_parquet('{tm}')
    WHERE NOT low_volume_day AND route_id IS NOT NULL AND start_stop_id IS NOT NULL AND end_stop_id IS NOT NULL
    GROUP BY ALL""")
    con.execute(f"""
    CREATE TEMP TABLE fac AS
    SELECT route_id, median(ci / nullif(ci_raw, 0)) AS f FROM read_parquet('{levels}') WHERE ci_raw > 0 GROUP BY 1""")
    def band(h):
        return (f"CASE WHEN {h} < 6 THEN 'night' WHEN {h} < 8 THEN 'early' WHEN {h} < 11 THEN 'am_peak' "
                f"WHEN {h} < 17 THEN 'midday' WHEN {h} < 20 THEN 'pm_peak' ELSE 'late' END")

    con.execute(f"""
    CREATE TABLE levels_hourly AS
    SELECT route_id, daytype, hour, seg, ci, n_tix,
           CASE WHEN ci < {t[0]} THEN 1 WHEN ci < {t[1]} THEN 2 WHEN ci < {t[2]} THEN 3 ELSE 4 END AS level
    FROM (
      WITH h AS (SELECT route_id, daytype, hour, seg, ticketed_per_day AS tick, trips_per_hour AS trips
                 FROM read_parquet('{sl}')),
      sm AS (SELECT *, sum(tick) OVER w AS tick_w, sum(trips) OVER w AS trips_w FROM h
             WINDOW w AS (PARTITION BY route_id, daytype, hour ORDER BY seg ROWS BETWEEN 2 PRECEDING AND 2 FOLLOWING)),
      hb AS (
        SELECT sm.route_id, sm.daytype, sm.hour, sm.seg,
               (sm.tick_w / nullif(sm.trips_w, 0)) * coalesce(fac.f, 1.0) AS ci_h, rh.n_tix,
               {band('sm.hour')} AS band
        FROM sm JOIN rh ON rh.route_id = sm.route_id AND rh.daytype = sm.daytype AND rh.hour = sm.hour
        LEFT JOIN fac ON fac.route_id = sm.route_id
        WHERE sm.trips_w > 0 AND rh.n_tix >= {A.hour_floor})
      SELECT hb.route_id, hb.daytype, hb.hour, hb.seg,
             (hb.n_tix * hb.ci_h + {A.blend_k} * lv.ci) / (hb.n_tix + {A.blend_k}) AS ci, hb.n_tix
      FROM hb JOIN levels lv ON lv.route_id = hb.route_id AND lv.daytype = hb.daytype AND lv.band = hb.band AND lv.seg = hb.seg
      WHERE lv.ci IS NOT NULL AND hb.ci_h IS NOT NULL)
    WHERE ci IS NOT NULL""")
    print("hour-level crowding table built (levels_hourly)")


# ---- road-following geometry from GTFS shapes.txt (falls back to straight lines where a shape does not fit)
import csv
import json
import math

trip_cols = [r[0] for r in con.execute("DESCRIBE g_trips").fetchall()]
shapes_file = os.path.join(gdir, "shapes.txt")
con.execute("CREATE TABLE seg_geom(route_id VARCHAR, seg INTEGER, pts VARCHAR)")
SNAP_MAX_M = 150.0        # a stop further than this from its route's shape is treated as off the shape
SIMPLIFY_M = 6.0          # Douglas-Peucker tolerance, metres
if os.path.exists(shapes_file) and "shape_id" in trip_cols:
    load("shapes")
    canon_shape = con.execute("""
        SELECT route_id, shape_id FROM (
          SELECT t.route_id, t.shape_id,
                 row_number() OVER (PARTITION BY t.route_id ORDER BY count(*) DESC, s.trip_id) AS rk
          FROM st s JOIN g_trips t ON t.trip_id = s.trip_id
          GROUP BY t.route_id, t.shape_id, s.trip_id)
        WHERE rk = 1 AND shape_id IS NOT NULL""").fetchall()
    need = {sid for _, sid in canon_shape}
    shape_pts = {}
    for sid, la, lo in con.execute(
            "SELECT shape_id, TRY_CAST(shape_pt_lat AS DOUBLE), TRY_CAST(shape_pt_lon AS DOUBLE) FROM g_shapes "
            "ORDER BY shape_id, TRY_CAST(shape_pt_sequence AS INTEGER)").fetchall():
        if sid in need and la is not None and lo is not None:
            shape_pts.setdefault(sid, []).append((la, lo))
    route_stops = {}
    for rid, la, lo in con.execute(
            "SELECT c.route_id, s.lat, s.lon FROM canon c JOIN stops s ON s.stop_id = c.stop_id "
            "ORDER BY c.route_id, c.idx").fetchall():
        route_stops.setdefault(rid, []).append((la, lo))

    def project(P, stops):
        """Snap each stop (in route order) onto the shape polyline, moving only forward. Returns [(i, t, dist_m) or None]."""
        lat0, lon0 = P[0]
        kx, ky = 111320.0 * math.cos(math.radians(lat0)), 110540.0
        X = [(p[1] - lon0) * kx for p in P]
        Y = [(p[0] - lat0) * ky for p in P]
        snaps, last_i, last_t = [], 0, 0.0
        for la, lo in stops:
            x, y = (lo - lon0) * kx, (la - lat0) * ky
            best = None
            for lim in (150, None):
                end = len(P) - 1 if lim is None else min(len(P) - 1, last_i + lim)
                for i in range(last_i, end):
                    ax, ay, dx, dy = X[i], Y[i], X[i + 1] - X[i], Y[i + 1] - Y[i]
                    l2 = dx * dx + dy * dy
                    t = 0.0 if l2 == 0 else ((x - ax) * dx + (y - ay) * dy) / l2
                    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                    if i == last_i and t < last_t:
                        t = last_t
                    d = (x - ax - t * dx) ** 2 + (y - ay - t * dy) ** 2
                    if best is None or d < best[0]:
                        best = (d, i, t)
                if best is not None and best[0] <= SNAP_MAX_M ** 2:
                    break
            if best is None or best[0] > SNAP_MAX_M ** 2:
                snaps.append(None)
                continue
            snaps.append((best[1], best[2], math.sqrt(best[0])))
            last_i, last_t = best[1], best[2]
        return snaps

    def simplify(pts, tol):
        n = len(pts)
        if n <= 2:
            return pts
        lat0, lon0 = pts[0]
        kx, ky = 111320.0 * math.cos(math.radians(lat0)), 110540.0
        xy = [((p[1] - lon0) * kx, (p[0] - lat0) * ky) for p in pts]
        keep = [False] * n
        keep[0] = keep[-1] = True
        stack = [(0, n - 1)]
        while stack:
            i, j = stack.pop()
            ax, ay = xy[i]
            bx, by = xy[j]
            dx, dy = bx - ax, by - ay
            l2 = dx * dx + dy * dy
            dmax, kmax = 0.0, -1
            for k in range(i + 1, j):
                px, py = xy[k]
                if l2 == 0:
                    d = math.hypot(px - ax, py - ay)
                else:
                    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
                    d = math.hypot(px - ax - t * dx, py - ay - t * dy)
                if d > dmax:
                    dmax, kmax = d, k
            if kmax >= 0 and dmax > tol:
                keep[kmax] = True
                stack += [(i, kmax), (kmax, j)]
        return [p for p, k in zip(pts, keep) if k]

    stats = {"routes": len(route_stops), "with_shape": 0, "stops": 0, "within50": 0, "snapped": 0, "stretches": 0, "stored": 0}
    snap_d = []
    with open("_seg_geom.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["route_id", "seg", "pts"])
        for rid, sid in canon_shape:
            P, stops = shape_pts.get(sid), route_stops.get(rid)
            if not P or len(P) < 2 or not stops or len(stops) < 2:
                continue
            stats["with_shape"] += 1
            snaps = project(P, stops)
            for s_ in snaps:
                stats["stops"] += 1
                if s_ is not None:
                    stats["snapped"] += 1
                    snap_d.append(s_[2])
                    stats["within50"] += s_[2] <= 50.0
            for k in range(len(stops) - 1):
                stats["stretches"] += 1
                a, b = snaps[k], snaps[k + 1]
                if a is None or b is None:
                    continue
                (i1, t1, _), (i2, t2, _) = a, b

                def at(i, t):
                    j = min(i + 1, len(P) - 1)
                    return (P[i][0] + t * (P[j][0] - P[i][0]), P[i][1] + t * (P[j][1] - P[i][1]))

                line = [at(i1, t1)] + P[i1 + 1:i2 + 1] + [at(i2, t2)]
                dedup = [line[0]]
                for p in line[1:]:
                    if abs(p[0] - dedup[-1][0]) > 5e-6 or abs(p[1] - dedup[-1][1]) > 5e-6:
                        dedup.append(p)
                if len(dedup) < 2:
                    continue
                dedup = simplify(dedup, SIMPLIFY_M)
                w.writerow([rid, k, json.dumps([[round(p[0], 5), round(p[1], 5)] for p in dedup], separators=(",", ":"))])
                stats["stored"] += 1
    con.execute("COPY seg_geom FROM '_seg_geom.csv' (HEADER)")
    os.remove("_seg_geom.csv")
    snap_d.sort()
    med = snap_d[len(snap_d) // 2] if snap_d else float("nan")
    print(f"shapes: {stats['with_shape']:,} of {stats['routes']:,} routes have a usable shape; "
          f"{100 * stats['snapped'] / max(1, stats['stops']):.1f}% of stops snap within {SNAP_MAX_M:.0f} m "
          f"({100 * stats['within50'] / max(1, stats['stops']):.1f}% within 50 m, median {med:.0f} m); "
          f"{stats['stored']:,} of {stats['stretches']:,} stretches follow the road shape, the rest stay straight")
else:
    print("no shapes.txt or no shape_id in trips.txt: map lines stay straight between stops")

for tbl in ["stops", "routes", "st", "canon", "levels", "walk_pairs", "seg_geom"] + (["levels_hourly"] if A.seg_load and A.tickets else []):
    n = con.execute(f"SELECT count(*) FROM {tbl}").fetchall()[0][0]
    print(f"{tbl:<11} {n:>10,} rows")
print(f"level thresholds: {t[0]:.2f} / {t[1]:.2f} / {t[2]:.2f}  (v2 run printed 0.34 / 0.90 / 2.13)")
con.close()
print(f"\nWrote {out}")
