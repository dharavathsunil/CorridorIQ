"""
Crowding-aware bus journey planner.

Search: direct and one-transfer bus journeys on the static GTFS timetable (same timetable every day).
Crowding: for each ride leg, the Crowding Index of the stretches between boarding and alighting stop,
          in the time band of the departure (see pipeline/08_crowding_levels.py).
Ranking:  score = (arrival - requested departure) in minutes + COMFORT_MAX_WEIGHT * comfort * discomfort_minutes
          discomfort_minutes = ride minutes x weight of the leg's crowding level
          comfort = 0 -> fastest arrival, 1 -> strongest preference for less crowded rides.
"""
import heapq
import json
import math

import duckdb

WALK_MS = 1.25            # walking speed, m/s
DETOUR = 1.3              # straight-line -> street distance
MAX_WALK_M = 800
FALLBACK_WALK_M = 2000
TRANSFER_BUFFER_S = 120
MAX_TRANSFER_WAIT_S = 2400
LEVEL_LABELS = {0: "No data", 1: "Seats likely", 2: "Standing room", 3: "Crowded", 4: "Very crowded"}
# minutes of discomfort per minute of riding at each level (unknown is treated like 'standing room')
PENALTY_W = {0: 0.15, 1: 0.0, 2: 0.15, 3: 0.4, 4: 0.8}
COMFORT_MAX_WEIGHT = 4.0  # at comfort = 1 a minute of discomfort counts as 4 minutes of arrival delay


def band_of(hour):
    if hour < 6:
        return "night"
    if hour < 8:
        return "early"
    if hour < 11:
        return "am_peak"
    if hour < 17:
        return "midday"
    if hour < 20:
        return "pm_peak"
    return "late"


def hhmm(s):
    s = int(s) % 86400
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}"


def parse_hhmm(t):
    parts = t.split(":")
    return int(parts[0]) * 3600 + int(parts[1]) * 60


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


def _q(s):
    return "'" + str(s).replace("'", "''") + "'"


class Planner:
    def __init__(self, db_path):
        self.con = duckdb.connect(db_path, read_only=True)
        cur = self.con.cursor()
        self.stops = cur.execute("SELECT stop_id, stop_name, lat, lon FROM stops").fetchall()
        self.routes = {
            r[0]: {"short": r[1], "description": r[2], "operator": r[3]}
            for r in cur.execute("SELECT route_id, short_name, description, operator FROM routes").fetchall()
        }
        meta = dict(cur.execute("SELECT k, v FROM meta").fetchall())
        self.t1, self.t2, self.t3 = float(meta["t1"]), float(meta["t2"]), float(meta["t3"])
        self._cache = {}
        self.has_hourly = bool(cur.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'levels_hourly'").fetchone()[0])
        self.has_geom = bool(cur.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'seg_geom'").fetchone()[0])

    # ------------------------------------------------------------------ helpers
    def level_of(self, ci):
        if ci < self.t1:
            return 1
        if ci < self.t2:
            return 2
        if ci < self.t3:
            return 3
        return 4

    def meta(self):
        return {
            "thresholds": {"seats_likely_below": self.t1, "standing_below": self.t2, "crowded_below": self.t3},
            "levels": LEVEL_LABELS,
            "bands": {"early": "06-08", "am_peak": "08-11", "midday": "11-17", "pm_peak": "17-20", "late": "20-24"},
            "note": ("Crowding is a relative index built from October 2026 e-ticket and ETM records (which cover only a "
                     "small share of riders). Levels compare a stretch with other Delhi bus stretches on weekdays: "
                     "'Seats likely' is below the median, 'Crowded' is in the busiest 20%, 'Very crowded' the busiest 5%. "
                     "The departure hour is blended with the time band, weighted by how many tickets the route has in that hour."),
        }

    def nearby(self, lat, lon):
        scored = heapq.nsmallest(
            20, ((haversine(lat, lon, s[2], s[3]), s) for s in self.stops), key=lambda x: x[0])
        within = [x for x in scored if x[0] <= MAX_WALK_M][:6]
        if not within:
            within = [x for x in scored[:3] if x[0] <= FALLBACK_WALK_M]
        return [
            {"stop_id": s[0], "name": s[1], "lat": s[2], "lon": s[3], "dist_m": round(d),
             "walk_s": int(d * DETOUR / WALK_MS)} for d, s in within
        ]

    def search_stops(self, q, limit=10):
        q = (q or "").strip().lower()
        if len(q) < 2:
            return []
        rows = self.con.cursor().execute(
            "SELECT stop_id, stop_name, lat, lon FROM stops WHERE lower(stop_name) LIKE ? "
            "ORDER BY length(stop_name) LIMIT ?", [f"%{q}%", limit]).fetchall()
        seen, out = set(), []
        for r in rows:
            if r[1] in seen:
                continue
            seen.add(r[1])
            out.append({"stop_id": r[0], "name": r[1], "lat": r[2], "lon": r[3]})
        return out

    # ------------------------------------------------------------------ crowding for one ride leg
    def leg_crowding(self, cur, route_id, board_stop, alight_stop, day, band, hour):
        key = (route_id, board_stop, alight_stop, day, hour)
        if key in self._cache:
            return self._cache[key]
        res = {"level": 0, "label": LEVEL_LABELS[0], "peak_level": 0, "crowded_share": None, "avg_ci": None,
               "has_data": False, "stretches": [], "busiest": None, "resolution": None, "hour_profile": []}
        b = cur.execute("SELECT min(idx) FROM canon WHERE route_id = ? AND stop_id = ?", [route_id, board_stop]).fetchone()[0]
        a = cur.execute("SELECT max(idx) FROM canon WHERE route_id = ? AND stop_id = ?", [route_id, alight_stop]).fetchone()[0]
        if b is None or a is None or b >= a:
            self._cache[key] = res
            return res
        geo = cur.execute(
            "SELECT c.idx, s.lat, s.lon, s.stop_name FROM canon c JOIN stops s ON s.stop_id = c.stop_id "
            "WHERE c.route_id = ? AND c.idx BETWEEN ? AND ? ORDER BY c.idx", [route_id, b, a]).fetchall()
        n_seg = max(1, a - b)
        lv, resolution = {}, "band"
        if self.has_hourly:
            rows = cur.execute(
                "SELECT seg, ci, level FROM levels_hourly WHERE route_id = ? AND daytype = ? AND hour = ? "
                "AND seg >= ? AND seg < ?", [route_id, day, hour, b, a]).fetchall()
            if len(rows) >= 0.5 * n_seg:
                lv, resolution = {r[0]: (r[1], r[2]) for r in rows}, "hour"
            prof = cur.execute(
                "SELECT hour, avg(ci) FROM levels_hourly WHERE route_id = ? AND daytype = ? AND seg >= ? AND seg < ? "
                "GROUP BY hour HAVING count(*) >= ? ORDER BY hour", [route_id, day, b, a, 0.5 * n_seg]).fetchall()
            res["hour_profile"] = [{"hour": h, "ci": round(c, 2), "level": self.level_of(c)} for h, c in prof]
        if not lv:
            lv = {r[0]: (r[1], r[2]) for r in cur.execute(
                "SELECT seg, ci, level FROM levels WHERE route_id = ? AND daytype = ? AND band = ? "
                "AND seg >= ? AND seg < ?", [route_id, day, band, b, a]).fetchall()}
        shapes = self._shapes(cur, route_id, b, a)
        stretches, with_data = [], []
        for g1, g2 in zip(geo, geo[1:]):
            ci, level = lv.get(g1[0], (None, 0))
            stretches.append({"p1": [g1[1], g1[2]], "p2": [g2[1], g2[2]], "pts": shapes.get(g1[0]), "level": level or 0,
                              "ci": None if ci is None else round(ci, 2)})
            if level and level > 0:
                with_data.append((ci, level, g1[3], g2[3]))
        n = max(1, len(stretches))
        res["stretches"], res["resolution"] = stretches, resolution
        if with_data and len(with_data) >= 0.5 * n:
            avg_ci = sum(x[0] for x in with_data) / len(with_data)
            worst = max(with_data, key=lambda x: x[0])
            res.update({
                "has_data": True, "avg_ci": round(avg_ci, 2), "level": self.level_of(avg_ci),
                "peak_level": max(x[1] for x in with_data),
                "crowded_share": round(sum(1 for x in with_data if x[1] >= 3) / len(with_data), 2),
                "busiest": {"from": worst[2], "to": worst[3], "ci": round(worst[0], 2)},
            })
            res["label"] = LEVEL_LABELS[res["level"]]
        self._cache[key] = res
        return res

    def _shapes(self, cur, route_id, seg_from, seg_to):
        """Road-following polylines for stretches seg_from .. seg_to-1 ({seg: [[lat, lon], ...]}); empty if unavailable."""
        if not self.has_geom:
            return {}
        rows = cur.execute("SELECT seg, pts FROM seg_geom WHERE route_id = ? AND seg >= ? AND seg < ?",
                           [route_id, seg_from, seg_to]).fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}

    # ------------------------------------------------------------------ journey search
    def plan(self, o_lat, o_lon, d_lat, d_lon, depart="08:30", day="weekday", comfort=0.5,
             window_min=90, max_options=6):
        t0 = parse_hhmm(depart)
        W = int(window_min) * 60
        O, D = self.nearby(o_lat, o_lon), self.nearby(d_lat, d_lon)
        if not O or not D:
            return {"error": "No bus stop within walking distance of the origin or destination.",
                    "options": [], "origin_stops": O, "dest_stops": D}
        cur = self.con.cursor()
        ov = ",".join(f"({_q(s['stop_id'])}, {s['walk_s']})" for s in O)
        dv = ",".join(f"({_q(s['stop_id'])}, {s['walk_s']})" for s in D)

        direct = cur.execute(f"""
        WITH o(stop_id, walk_s) AS (VALUES {ov}), d(stop_id, walk_s) AS (VALUES {dv}),
        a AS (SELECT s.trip_id, s.route_id, s.stop_id, s.seq, s.dep_s, o.walk_s AS ow
              FROM st s JOIN o ON o.stop_id = s.stop_id
              WHERE s.dep_s >= {t0} + o.walk_s AND s.dep_s <= {t0} + {W}),
        b AS (SELECT s.trip_id, s.stop_id, s.seq, s.arr_s, d.walk_s AS dw
              FROM st s JOIN d ON d.stop_id = s.stop_id)
        SELECT a.trip_id, a.route_id, a.stop_id, a.seq, a.dep_s, a.ow, b.stop_id, b.seq, b.arr_s, b.dw
        FROM a JOIN b ON a.trip_id = b.trip_id AND a.seq < b.seq
        ORDER BY a.dep_s LIMIT 600""").fetchall()

        transfer = cur.execute(f"""
        WITH o(stop_id, walk_s) AS (VALUES {ov}), d(stop_id, walk_s) AS (VALUES {dv}),
        l1 AS (SELECT a.trip_id AS t1, a.route_id AS r1, a.stop_id AS os, a.dep_s AS dep1, o.walk_s AS ow,
                      x.stop_id AS xs, x.arr_s AS arr1
               FROM st a JOIN o ON o.stop_id = a.stop_id
               JOIN st x ON x.trip_id = a.trip_id AND x.seq > a.seq
               WHERE a.dep_s >= {t0} + o.walk_s AND a.dep_s <= {t0} + {W}),
        l2 AS (SELECT y.trip_id AS t2, y.route_id AS r2, y.stop_id AS ys, y.dep_s AS dep2,
                      b.stop_id AS ds, b.arr_s AS arr2, d.walk_s AS dw
               FROM st b JOIN d ON d.stop_id = b.stop_id
               JOIN st y ON y.trip_id = b.trip_id AND y.seq < b.seq
               WHERE b.arr_s >= {t0} AND b.arr_s <= {t0} + {W} + 7200)
        SELECT l1.t1, l1.r1, l1.os, l1.dep1, l1.ow, l1.xs, l1.arr1, wp.dist_m,
               l2.t2, l2.r2, l2.ys, l2.dep2, l2.ds, l2.arr2, l2.dw
        FROM l1 JOIN walk_pairs wp ON wp.a = l1.xs
        JOIN l2 ON l2.ys = wp.b
        WHERE l1.r1 <> l2.r2
          AND l2.dep2 >= l1.arr1 + wp.dist_m * {DETOUR} / {WALK_MS} + {TRANSFER_BUFFER_S}
          AND l2.dep2 <= l1.arr1 + {MAX_TRANSFER_WAIT_S}
        ORDER BY l2.arr2 LIMIT 800""").fetchall()

        options = []
        # ---- direct: keep at most 3 departures (>= 8 min apart) per route / stop pair
        last, kept = {}, {}
        for (trip, rid, os_, oseq, dep, ow, ds_, dseq, arr, dw) in direct:
            k = (rid, os_, ds_)
            if k in last and dep < last[k] + 480:
                continue
            if kept.get(k, 0) >= 4:
                continue
            last[k] = dep
            kept[k] = kept.get(k, 0) + 1
            options.append(self._option(cur, day, t0, [("ride", rid, os_, ds_, dep, arr)], ow, dw))
        # ---- one transfer: keep best two per (route1, route2, transfer stop)
        seen = {}
        for (t1, r1, os_, dep1, ow, xs, arr1, dist, t2, r2, ys, dep2, ds_, arr2, dw) in transfer:
            k = (r1, r2, xs, ys)
            if seen.get(k, 0) >= 2:
                continue
            seen[k] = seen.get(k, 0) + 1
            twalk = int(dist * DETOUR / WALK_MS)
            options.append(self._option(
                cur, day, t0, [("ride", r1, os_, xs, dep1, arr1), ("transfer", xs, ys, twalk),
                               ("ride", r2, ys, ds_, dep2, arr2)], ow, dw))
        options = [o for o in options if o]
        if not options:
            return {"error": "No bus journey found in the next %d minutes." % window_min, "options": [],
                    "origin_stops": O, "dest_stops": D}

        def dominated(x):
            return any(
                y is not x and y["_arrive_s"] <= x["_arrive_s"] and y["penalty_min"] <= x["penalty_min"]
                and y["transfers"] <= x["transfers"]
                and (y["_arrive_s"] < x["_arrive_s"] or y["penalty_min"] < x["penalty_min"] or y["transfers"] < x["transfers"])
                for y in options)
        options = [o for o in options if not dominated(o)]
        for o in options:
            o["score"] = round((o["_arrive_s"] - t0) / 60 + COMFORT_MAX_WEIGHT * comfort * o["penalty_min"], 1)
        options.sort(key=lambda o: (o["score"], o["_arrive_s"]))
        fastest = min(options, key=lambda o: (o["_arrive_s"], o["transfers"]))
        comfy = min(options, key=lambda o: (o["penalty_min"], o["_arrive_s"]))
        chosen, per_combo = [], {}
        for o in options:
            combo = tuple(l.get("route_id") for l in o["legs"] if l["mode"] == "bus")
            if per_combo.get(combo, 0) >= 2:
                continue
            per_combo[combo] = per_combo.get(combo, 0) + 1
            chosen.append(o)
            if len(chosen) >= max_options:
                break
        for must in (fastest, comfy):
            if must not in chosen:
                chosen[-1] = must
        chosen.sort(key=lambda o: (o["score"], o["_arrive_s"]))
        for o in chosen:
            o["tags"] = []
            if o is fastest:
                o["tags"].append("Fastest arrival")
            if o is comfy:
                o["tags"].append("Most comfortable")
            if o is chosen[0]:
                o["tags"].append("Best for your comfort setting")
            del o["_arrive_s"]
        return {"day": day, "band": band_of((t0 // 3600) % 24), "comfort": comfort, "requested_departure": depart,
                "origin_stops": O, "dest_stops": D, "options": chosen}

    # ------------------------------------------------------------------ build one option
    def _option(self, cur, day, t0, parts, ow, dw):
        legs, penalty, levels, first_dep, last_arr = [], 0.0, [], None, None
        sn = {s[0]: s[1] for s in self.stops}
        legs.append({"mode": "walk", "label": "Walk to first stop", "minutes": max(1, round(ow / 60))})
        for p in parts:
            if p[0] == "ride":
                _, rid, bs, as_, dep, arr = p
                hour = (dep // 3600) % 24
                band = band_of(hour)
                crowd = self.leg_crowding(cur, rid, bs, as_, day, band, hour)
                ride_min = max(1, round((arr - dep) / 60))
                if first_dep is None:
                    first_dep = dep
                last_arr = arr
                penalty += ride_min * PENALTY_W[crowd["level"]]
                levels.append(crowd["level"])
                legs.append({
                    "mode": "bus", "route_id": rid, "route": self.routes.get(rid, {}).get("short", rid),
                    "description": self.routes.get(rid, {}).get("description"),
                    "operator": self.routes.get(rid, {}).get("operator"),
                    "from": sn.get(bs, bs), "to": sn.get(as_, as_), "depart": hhmm(dep), "arrive": hhmm(arr),
                    "ride_min": ride_min, "band": band, "level": crowd["level"], "label": crowd["label"],
                    "peak_level": crowd["peak_level"], "peak_label": LEVEL_LABELS[crowd["peak_level"]],
                    "crowded_share": crowd["crowded_share"], "avg_ci": crowd["avg_ci"],
                    "has_data": crowd["has_data"], "busiest": crowd["busiest"], "stretches": crowd["stretches"],
                    "resolution": crowd["resolution"], "hour_profile": crowd["hour_profile"],
                })
            else:
                _, xs, ys, twalk = p
                legs.append({"mode": "transfer", "label": f"Change at {sn.get(xs, xs)}",
                             "minutes": max(1, round(twalk / 60))})
        legs.append({"mode": "walk", "label": "Walk to destination", "minutes": max(1, round(dw / 60))})
        if first_dep is None:
            return None
        depart_origin = first_dep - ow
        arrive = last_arr + dw
        known = [l for l in levels if l > 0]
        overall = max(known) if known else 0
        return {
            "depart_origin": hhmm(depart_origin), "arrive": hhmm(arrive), "_arrive_s": arrive,
            "door_to_door_min": round((arrive - depart_origin) / 60), "wait_min": round((depart_origin - t0) / 60),
            "transfers": sum(1 for p in parts if p[0] == "transfer"),
            "overall_level": overall, "overall_label": LEVEL_LABELS[overall],
            "all_legs_have_data": all(l > 0 for l in levels), "penalty_min": round(penalty, 1), "legs": legs,
        }

    # ------------------------------------------------------------------ map and operator views
    def route_stretches(self, route_id, day, band):
        cur = self.con.cursor()
        geo = cur.execute(
            "SELECT c.idx, s.lat, s.lon, s.stop_name FROM canon c JOIN stops s ON s.stop_id = c.stop_id "
            "WHERE c.route_id = ? ORDER BY c.idx", [route_id]).fetchall()
        lv = {r[0]: (r[1], r[2]) for r in cur.execute(
            "SELECT seg, ci, level FROM levels WHERE route_id = ? AND daytype = ? AND band = ?",
            [route_id, day, band]).fetchall()}
        out = []
        shapes = self._shapes(cur, route_id, 0, len(geo))
        for g1, g2 in zip(geo, geo[1:]):
            ci, level = lv.get(g1[0], (None, 0))
            out.append({"from": g1[3], "to": g2[3], "p1": [g1[1], g1[2]], "p2": [g2[1], g2[2]], "pts": shapes.get(g1[0]),
                        "level": level or 0, "ci": None if ci is None else round(ci, 2)})
        info = self.routes.get(route_id, {})
        return {"route_id": route_id, "route": info.get("short"), "description": info.get("description"),
                "operator": info.get("operator"), "day": day, "band": band, "stretches": out}

    def operator_top(self, day, band, limit=25):
        rows = self.con.cursor().execute(f"""
        SELECT route_id, count(*) AS stretches, sum((level >= 3)::INT) AS crowded,
               sum((level >= 4)::INT) AS very_crowded, round(max(ci), 2) AS peak_ci,
               round(avg(ci), 2) AS avg_ci, max(trips) AS trips_in_band, max(n_tix) AS tickets,
               round(sum(CASE WHEN level >= 3 THEN ci ELSE 0 END), 1) AS overload_score
        FROM levels
        WHERE daytype = ? AND band = ? AND confidence IN ('high', 'medium') AND level > 0
        GROUP BY route_id HAVING count(*) >= 5
        ORDER BY overload_score DESC LIMIT ?""", [day, band, limit]).fetchall()
        out = []
        for r in rows:
            info = self.routes.get(r[0], {})
            out.append({
                "route_id": r[0], "route": info.get("short"), "description": info.get("description"),
                "operator": info.get("operator"), "stretches": r[1], "crowded_stretches": r[2],
                "very_crowded_stretches": r[3], "pct_crowded": round(100.0 * r[2] / r[1]),
                "peak_ci": r[4], "avg_ci": r[5], "scheduled_trips_in_band": r[6], "tickets": r[7],
                "overload_score": r[8],
            })
        return out
