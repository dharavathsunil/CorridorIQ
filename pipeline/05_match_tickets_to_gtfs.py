"""
Step 5 - link every ticket to a GTFS route and to boarding / alighting stops.

Usage:
    pip install rapidfuzz
    python 05_match_tickets_to_gtfs.py "D:\\Transport\\gtfs" tickets_clean.parquet

Findings from step 4 that this script relies on:
  * GTFS route_short_name equals the ticket route string ("102ADOWN"), one GTFS route per direction
    (direction_id is empty everywhere). 98.4% of ticket rows already match exactly.
  * Only ~74% of ticket stop names match a GTFS stop name exactly. The rest are partial names
    ("mahipal pur" vs "Mahipal Pur Village"), composite GTFS names ("A / B") or "(T)" suffixes.

Method:
  1. Routes: exact match, then fallbacks (drop trailing variant number, drop STL/LNKSTL).
  2. Stops: match each ticket stop name ONLY against stops that lie on that route (about 30-80
     candidates), in this order: exact -> fuzzy (token-sort >= 85) -> subset (token-set >= 95).
     Each stop gets `pos` = fraction of the way along the route (0 = first stop, 1 = last stop).
  3. Validation that needs no ground truth: for correctly matched tickets the boarding stop must
     come BEFORE the alighting stop along the route (start_pos < end_pos). We report that share.

Output: tickets_matched.parquet  (all ticket columns + route_id, start_stop_id, start_pos,
        end_stop_id, end_pos, match methods and scores)
"""
import csv
import os
import re
import sys

import duckdb
from rapidfuzz import fuzz, process

gdir = sys.argv[1] if len(sys.argv) > 1 else "gtfs"
tick = (sys.argv[2] if len(sys.argv) > 2 else "tickets_clean.parquet").replace("\\", "/")
out = sys.argv[3] if len(sys.argv) > 3 else "tickets_matched.parquet"
FUZZY_MIN = 85
SUBSET_MIN = 95

con = duckdb.connect()


def load(name):
    p = os.path.join(gdir, name + ".txt").replace("\\", "/")
    con.execute(
        f"CREATE TABLE {name} AS SELECT * FROM read_csv('{p}', header=true, all_varchar=true, "
        f"sample_size=-1, ignore_errors=true)"
    )


for n in ("routes", "stops", "trips", "stop_times"):
    load(n)


def norm(s):  # same normalisation as step 2 (lowercase, non-alphanumerics -> single space)
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def key(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


# ------------------------------------------------------------------ 1. routes
gt = {}
for rid, sn in con.execute("SELECT route_id, route_short_name FROM routes").fetchall():
    gt.setdefault(key(sn), rid)

raw_routes = [r[0] for r in con.execute(f"SELECT DISTINCT route_raw FROM read_parquet('{tick}') ORDER BY 1").fetchall()]


def route_candidates(raw):
    k = key(raw)
    yield "exact", k
    k_no = re.sub(r"(UP|DOWN)[0-9]+$", r"\1", k)                       # 522BDOWN2 -> 522BDOWN
    if k_no != k:
        yield "strip_variant_no", k_no
    k_stl = re.sub(r"(LNKSTL|LINKSTL|STL)((UP|DOWN)[0-9]*)$", r"\2", k)  # 567BSTLUP -> 567BUP
    if k_stl != k:
        yield "strip_stl", k_stl
    k_both = re.sub(r"(LNKSTL|LINKSTL|STL)((UP|DOWN)[0-9]*)$", r"\2", k_no)
    if k_both != k and k_both not in (k_no, k_stl):
        yield "strip_both", k_both


with open("_route_map.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["route_raw", "route_id", "route_method"])
    for raw in raw_routes:
        for method, k in route_candidates(raw):
            if k in gt:
                w.writerow([raw, gt[k], method])
                break
con.execute("CREATE TABLE route_map AS SELECT * FROM read_csv('_route_map.csv', header=true, all_varchar=true)")
print(f"Routes: {len(raw_routes):,} ticket route strings, "
      f"{con.execute('SELECT count(*) FROM route_map').fetchone()[0]:,} mapped to GTFS")

# ------------------------------------------------------------------ 2. route -> stops with position
rs = con.execute("""
WITH st AS (
    SELECT t.route_id, s.trip_id, s.stop_id, CAST(s.stop_sequence AS INTEGER) AS seq
    FROM stop_times s JOIN trips t ON t.trip_id = s.trip_id
), n AS (SELECT trip_id, max(seq) AS mx FROM st GROUP BY 1)
SELECT st.route_id, st.stop_id,
       avg(CASE WHEN n.mx > 0 THEN st.seq * 1.0 / n.mx ELSE 0 END) AS pos
FROM st JOIN n ON n.trip_id = st.trip_id
GROUP BY 1, 2
ORDER BY 1, 2
""").fetchall()
sname = dict(con.execute("SELECT stop_id, stop_name FROM stops").fetchall())


def variants(name):
    """Normalised spellings of a GTFS stop name: full, each '/'-part, with '(T)'-style tags removed."""
    out = set()
    base = name or ""
    for s in [base] + re.split(r"\s*/\s*", base):
        out.add(norm(s))
        out.add(norm(re.sub(r"\([^)]*\)", "", s)))
    return {v for v in out if v}


route_stops = {}
for rid, sid, pos in rs:
    d = route_stops.setdefault(rid, {})
    for v in variants(sname.get(sid)):
        d.setdefault(v, (sid, float(pos)))
route_choices = {rid: list(d.keys()) for rid, d in route_stops.items()}

# ------------------------------------------------------------------ 3. stop matching per (route_id, ticket stop name)
pairs = con.execute(f"""
SELECT DISTINCT rm.route_id, x.nm
FROM (SELECT route_raw, start_norm AS nm FROM read_parquet('{tick}')
      UNION SELECT route_raw, end_norm FROM read_parquet('{tick}')) x
JOIN route_map rm ON rm.route_raw = x.route_raw
ORDER BY 1, 2
""").fetchall()
print(f"Stop matching: {len(pairs):,} distinct (route, ticket stop name) pairs ...")

with open("_stop_map.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["route_id", "name_norm", "stop_id", "pos", "score", "stop_method"])
    for i, (rid, nm) in enumerate(pairs):
        d = route_stops.get(rid)
        if not d:
            continue
        hit = d.get(nm)
        if hit:
            w.writerow([rid, nm, hit[0], hit[1], 100, "exact"])
            continue
        ch = route_choices[rid]
        best = process.extractOne(nm, ch, scorer=fuzz.token_sort_ratio, score_cutoff=FUZZY_MIN)
        if best:
            hit = d[best[0]]
            w.writerow([rid, nm, hit[0], hit[1], round(best[1], 1), "fuzzy"])
            continue
        subs = process.extract(nm, ch, scorer=fuzz.token_set_ratio, score_cutoff=SUBSET_MIN, limit=10)
        if subs:
            pick = min(subs, key=lambda s: abs(len(s[0]) - len(nm)))   # closest in length to the ticket name
            hit = d[pick[0]]
            w.writerow([rid, nm, hit[0], hit[1], round(pick[1], 1), "subset"])
        if i and i % 100000 == 0:
            print(f"  ... {i:,}")
con.execute("CREATE TABLE stop_map AS SELECT * FROM read_csv('_stop_map.csv', header=true, all_varchar=true)")
con.execute("""
CREATE TABLE stop_map2 AS
SELECT route_id, name_norm, stop_id, CAST(pos AS DOUBLE) AS pos, CAST(score AS DOUBLE) AS score, stop_method
FROM stop_map""")

# ------------------------------------------------------------------ 4. join back to tickets
con.execute(f"""
CREATE TABLE tm AS
SELECT t.*, rm.route_id, rm.route_method,
       a.stop_id AS start_stop_id, a.pos AS start_pos, a.score AS start_score, a.stop_method AS start_method,
       b.stop_id AS end_stop_id,   b.pos AS end_pos,   b.score AS end_score,   b.stop_method AS end_method
FROM read_parquet('{tick}') t
LEFT JOIN route_map rm ON rm.route_raw = t.route_raw
LEFT JOIN stop_map2 a ON a.route_id = rm.route_id AND a.name_norm = t.start_norm
LEFT JOIN stop_map2 b ON b.route_id = rm.route_id AND b.name_norm = t.end_norm
""")
con.execute(f"COPY tm TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)")


def show(title, sql, rows=25):
    print(f"\n=== {title} ===")
    con.sql(sql).show(max_rows=rows, max_width=200)


show("Route mapping (share of ticket rows)", """
SELECT coalesce(route_method, 'UNMATCHED') AS method, count(*) AS rows,
       round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct
FROM tm GROUP BY 1 ORDER BY rows DESC""")

show("Stop matching (share of ticket rows) - was 73.7% / 74.4% with exact whole-city name matching", """
SELECT round(100.0 * avg((start_stop_id IS NOT NULL)::INT), 2) AS pct_start_matched,
       round(100.0 * avg((end_stop_id   IS NOT NULL)::INT), 2) AS pct_end_matched,
       round(100.0 * avg((start_stop_id IS NOT NULL AND end_stop_id IS NOT NULL)::INT), 2) AS pct_both_matched
FROM tm""")

show("Start-stop match method", """
SELECT coalesce(start_method, 'UNMATCHED') AS method, count(*) AS rows,
       round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct
FROM tm GROUP BY 1 ORDER BY rows DESC""")

show("DIRECTION CHECK: is boarding before alighting along the route? (both stops matched)", """
SELECT round(100.0 * avg((start_pos <  end_pos)::INT), 2) AS pct_board_before_alight,
       round(100.0 * avg((start_pos =  end_pos)::INT), 2) AS pct_same_position,
       round(100.0 * avg((start_pos >  end_pos)::INT), 2) AS pct_board_after_alight,
       count(*) AS rows
FROM tm WHERE start_stop_id IS NOT NULL AND end_stop_id IS NOT NULL""")

show("DIRECTION CHECK by start-stop match method (is fuzzy matching hurting?)", """
SELECT start_method, round(100.0 * avg((start_pos < end_pos)::INT), 2) AS pct_board_before_alight, count(*) AS rows
FROM tm WHERE start_stop_id IS NOT NULL AND end_stop_id IS NOT NULL
GROUP BY 1 ORDER BY rows DESC""")

show("Fare band vs mean ride length along the route (should rise with fare)", """
SELECT fare_band, count(*) AS rows, round(avg(end_pos - start_pos), 3) AS mean_pos_travelled
FROM tm WHERE start_pos < end_pos GROUP BY 1 ORDER BY try_cast(fare_band AS DOUBLE)""")

show("Top ticket stop names still unmatched (by tickets)", """
SELECT route_raw, start_norm, count(*) AS tickets
FROM tm WHERE route_id IS NOT NULL AND start_stop_id IS NULL
GROUP BY 1, 2 ORDER BY tickets DESC""", rows=25)

show("Top ticket routes with no GTFS route (by tickets)", """
SELECT route_raw, count(*) AS tickets FROM tm WHERE route_id IS NULL
GROUP BY 1 ORDER BY tickets DESC""", rows=15)

os.remove("_route_map.csv")
os.remove("_stop_map.csv")
print(f"\nWrote {out}. Paste everything above.")
