"""
Step 4 - inspect the static GTFS and measure how well the ticket data joins to it.

Usage:
    python 04_inspect_gtfs.py <folder_with_gtfs_txt_files> [tickets_clean.parquet]

Example:
    python 04_inspect_gtfs.py D:\\Transport\\gtfs tickets_clean.parquet

Prints: file sizes and columns, direction handling, and the two numbers that decide how hard the
rest of the project is:
  * what share of ticket rows have a route name that exists in GTFS
  * what share of ticket rows have a start stop name that exists in GTFS (exact, after normalising)
Paste the whole output back.
"""
import os
import sys

import duckdb

gdir = sys.argv[1] if len(sys.argv) > 1 else "gtfs"
tick = (sys.argv[2] if len(sys.argv) > 2 else "tickets_clean.parquet").replace("\\", "/")
con = duckdb.connect()
have = {}


def load(name):
    p = os.path.join(gdir, name + ".txt").replace("\\", "/")
    if not os.path.exists(p):
        print(f"[missing] {name}.txt")
        have[name] = False
        return
    con.execute(
        f"CREATE TABLE {name} AS SELECT * FROM read_csv('{p}', header=true, all_varchar=true, "
        f"sample_size=-1, ignore_errors=true)"
    )
    n = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
    cols = [r[0] for r in con.execute(f"DESCRIBE {name}").fetchall()]
    have[name] = True
    print(f"[ok] {name}.txt  rows={n:,}  columns={cols}")


def show(title, sql, rows=20):
    print(f"\n=== {title} ===")
    try:
        con.sql(sql).show(max_rows=rows, max_width=200)
    except Exception as e:  # keep going: a missing column is itself useful information
        print("  (could not run:", str(e).splitlines()[0], ")")


print("=== GTFS files ===")
for f in ["agency", "calendar", "calendar_dates", "routes", "stops", "trips", "stop_times", "shapes", "frequencies"]:
    load(f)

if have.get("routes"):
    show("routes sample", "SELECT * FROM routes LIMIT 8")
if have.get("trips"):
    show("trips sample", "SELECT * FROM trips LIMIT 5")
    show("direction_id values (are UP/DOWN separate routes or separate directions?)",
         "SELECT direction_id, count(*) AS trips FROM trips GROUP BY 1 ORDER BY 1")
    show("trips per route (distribution)",
         "SELECT min(n) AS min_trips, median(n) AS median_trips, max(n) AS max_trips, count(*) AS routes "
         "FROM (SELECT route_id, count(*) AS n FROM trips GROUP BY 1)")
if have.get("stop_times"):
    show("stop_times sample", "SELECT * FROM stop_times LIMIT 6")
    show("stop_times size",
         "SELECT count(*) AS rows, count(DISTINCT trip_id) AS trips, count(DISTINCT stop_id) AS stops FROM stop_times")
if have.get("calendar"):
    show("calendar (service patterns)", "SELECT * FROM calendar LIMIT 10")

# ---------- route name join ----------
if have.get("routes"):
    print("\n=== Ticket route -> GTFS route match (share of TICKET ROWS) ===")
    cand = [c for c in ("route_short_name", "route_long_name", "route_id")
            if c in [r[0] for r in con.execute("DESCRIBE routes").fetchall()]]
    for col in cand:
        sql = f"""
        WITH g AS (SELECT DISTINCT regexp_replace(upper({col}), '[^A-Z0-9]', '', 'g') AS k FROM routes),
        tk AS (SELECT regexp_replace(upper(route_raw),  '[^A-Z0-9]', '', 'g') AS full_k,
                      regexp_replace(upper(route_base), '[^A-Z0-9]', '', 'g') AS base_k,
                      count(*) AS n
               FROM read_parquet('{tick}') GROUP BY 1, 2)
        SELECT '{col}' AS gtfs_column,
               sum(n) AS ticket_rows,
               round(100.0 * sum(CASE WHEN full_k IN (SELECT k FROM g) THEN n ELSE 0 END) / sum(n), 2) AS pct_full_route_match,
               round(100.0 * sum(CASE WHEN base_k IN (SELECT k FROM g) THEN n ELSE 0 END) / sum(n), 2) AS pct_base_route_match
        FROM tk"""
        show(f"match using routes.{col}", sql)

    if "route_short_name" in cand:
        show("Top ticket routes NOT found in GTFS (by tickets; tried full and base name)", """
        WITH g AS (SELECT DISTINCT regexp_replace(upper(route_short_name), '[^A-Z0-9]', '', 'g') AS k FROM routes)
        SELECT route_raw, route_base, count(*) AS tickets
        FROM read_parquet('""" + tick + """')
        WHERE regexp_replace(upper(route_raw),  '[^A-Z0-9]', '', 'g') NOT IN (SELECT k FROM g)
          AND regexp_replace(upper(route_base), '[^A-Z0-9]', '', 'g') NOT IN (SELECT k FROM g)
        GROUP BY 1, 2 ORDER BY tickets DESC""", rows=20)

# ---------- stop name join ----------
if have.get("stops"):
    print("\n=== Ticket stop name -> GTFS stop name (exact, after normalising) ===")
    NORM = "trim(regexp_replace(regexp_replace(lower(stop_name), '[^a-z0-9]+', ' ', 'g'), ' +', ' ', 'g'))"
    show("share of ticket rows with exact start / end stop match", f"""
    WITH g AS (SELECT DISTINCT {NORM} AS k FROM stops)
    SELECT count(*) AS ticket_rows,
           round(100.0 * avg((start_norm IN (SELECT k FROM g))::INT), 2) AS pct_start_exact,
           round(100.0 * avg((end_norm   IN (SELECT k FROM g))::INT), 2) AS pct_end_exact
    FROM read_parquet('{tick}')""")
    show("Most common ticket start stops with NO exact GTFS name match", f"""
    WITH g AS (SELECT DISTINCT {NORM} AS k FROM stops)
    SELECT start_norm, count(*) AS tickets
    FROM read_parquet('{tick}')
    WHERE start_norm NOT IN (SELECT k FROM g)
    GROUP BY 1 ORDER BY tickets DESC""", rows=25)

print("\nDone. Paste everything above.")
