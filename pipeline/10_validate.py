"""
Step 10 - held-out validation: is the model right on days it has not seen?

Usage (from the project folder):
    python pipeline/06_segment_load.py "gtfs" tickets_matched.parquet --to-date 2026-10-21 --suffix _train
    python pipeline/06_segment_load.py "gtfs" tickets_matched.parquet --from-date 2026-10-22 --suffix _test
    python pipeline/10_validate.py tickets_matched.parquet seg_load_train.parquet seg_load_test.parquet \\
           --train-to 2026-10-21 --test-from 2026-10-22 [--levels crowd_levels.parquet]

What is tested (weekdays only, flagged low-volume days excluded):
  A. COUNT PREDICTION. From the training days we predict, for every route x stretch x hour, how many
     ticketed passengers will be on board per day on the unseen test days, and score it with the Poisson
     deviance (the right loss for counts; lower is better). Models compared:
        B1  network x hour           every route gets the same ticketed load per trip at that hour
        B2  route mean x hour shape  the route's overall level, shaped by the network hourly profile
        B3  route x hour             no stretch detail
        OURS-band   route x stretch x time band, smoothed over 5 stretches
        OURS-hour   route x stretch x hour, smoothed over 5 stretches
        OURS-app    hard switch: hourly where the route has >= HOUR_MIN tickets in that hour, else band
        OURS-blend  what the app does: weight n/(n+K) on the hourly estimate and the rest on the band estimate,
                    n = the route's training tickets in that hour (K = 10, chosen on the validation split, not on the test days)
  B. LEVEL AGREEMENT. Using thresholds learnt on the training days only, the level we would have shown
     from training data versus the level the test days actually show (same rule). Compared with a baseline
     that shows every route in a band the same level.

Because ticketed passengers are a small sample of riders, the target is the ticketed load on board (the
quantity the index is built from), not total passengers. The operator coverage factor is applied only in
part B (levels), because part A scores raw ticket counts.
"""
import argparse
import sys

import duckdb

ap = argparse.ArgumentParser()
ap.add_argument("tickets")
ap.add_argument("train")
ap.add_argument("test")
ap.add_argument("--train-to", required=True, help="last training date, YYYY-MM-DD")
ap.add_argument("--test-from", required=True, help="first test date, YYYY-MM-DD")
ap.add_argument("--levels", default=None, help="crowd_levels.parquet (only for the operator coverage factor)")
ap.add_argument("--hour-min", type=int, default=100, help="hard-switch rule for the OURS-app model: min route-hour tickets to use the hour")
ap.add_argument("--blend-k", type=int, default=10, help="blend weight: hour estimate gets n/(n+K), n = route-hour training tickets")
ap.add_argument("--test-to", default=None, help="last test date, YYYY-MM-DD (optional)")
ap.add_argument("--supported", type=int, default=300, help="min training weekday tickets for a 'well-supported' route")
ap.add_argument("--window", type=int, default=2, help="smoothing window: +- this many stretches")
ap.add_argument("--percentiles", default="50,80,95", help="level cut-off percentiles, e.g. 50,80,95")
ap.add_argument("--quiet", action="store_true", help="print nothing except errors (used by the sensitivity runner)")
ap.add_argument("--summary-csv", default=None, help="append one summary row to this CSV")
A = ap.parse_args()
tm = A.tickets.replace("\\", "/")
trf = A.train.replace("\\", "/")
tef = A.test.replace("\\", "/")
WIN = A.window
EPS = 1e-3
DAY_BANDS = "('early', 'am_peak', 'midday', 'pm_peak', 'late')"
P1, P2, P3 = (float(x) / 100 for x in A.percentiles.split(","))
SUMMARY = {}
MED_T = 60

con = duckdb.connect()


def band(h):
    return (f"CASE WHEN {h} < 6 THEN 'night' WHEN {h} < 8 THEN 'early' WHEN {h} < 11 THEN 'am_peak' "
            f"WHEN {h} < 17 THEN 'midday' WHEN {h} < 20 THEN 'pm_peak' ELSE 'late' END")


def show(title, sql, rows=30):
    if A.quiet:
        return
    print(f"\n=== {title} ===")
    try:
        con.sql(sql).show(max_rows=rows, max_width=200)
    except Exception as e:
        print("  (could not run:", str(e).splitlines()[0], ")")


TEST_TO = f" AND date <= DATE '{A.test_to}'" if A.test_to else ""
# ------------------------------------------------------------------ days and ticket counts
nd_tr = con.execute(f"""SELECT count(DISTINCT date) FROM read_parquet('{tm}')
    WHERE NOT low_volume_day AND daytype = 'weekday' AND date <= DATE '{A.train_to}'""").fetchall()[0][0]
nd_te = con.execute(f"""SELECT count(DISTINCT date) FROM read_parquet('{tm}')
    WHERE NOT low_volume_day AND daytype = 'weekday' AND date >= DATE '{A.test_from}'{TEST_TO}""").fetchall()[0][0]
if not A.quiet: print(f"Typical weekdays: {nd_tr} training days (to {A.train_to}), {nd_te} test days (from {A.test_from})")
if nd_tr == 0 or nd_te == 0:
    sys.exit("No training or test days found; check --train-to / --test-from.")

MATCHED = "NOT low_volume_day AND daytype = 'weekday' AND route_id IS NOT NULL AND start_stop_id IS NOT NULL AND end_stop_id IS NOT NULL"
con.execute(f"""
CREATE TABLE rh_tr AS SELECT route_id, hour(ts) AS hour, count(*) AS n_tix FROM read_parquet('{tm}')
WHERE {MATCHED} AND date <= DATE '{A.train_to}' GROUP BY ALL""")
con.execute(f"""
CREATE TABLE rb_tr AS SELECT route_id, {band('hour(ts)')} AS band, count(*) AS n_tix FROM read_parquet('{tm}')
WHERE {MATCHED} AND date <= DATE '{A.train_to}' GROUP BY ALL""")
con.execute(f"""
CREATE TABLE rb_te AS SELECT route_id, {band('hour(ts)')} AS band, count(*) AS n_tix FROM read_parquet('{tm}')
WHERE {MATCHED} AND date >= DATE '{A.test_from}'{TEST_TO} GROUP BY ALL""")
con.execute("CREATE TABLE rt_tr AS SELECT route_id, sum(n_tix) AS n_tix FROM rh_tr GROUP BY 1")

# ------------------------------------------------------------------ cells (weekday, same grid in both periods)
con.execute(f"""
CREATE TABLE g AS
SELECT a.route_id, a.hour, a.seg, a.trips_per_hour AS trips, a.ticketed_per_day AS tick_tr, b.ticketed_per_day AS tick_te,
       {band('a.hour')} AS band
FROM read_parquet('{trf}') a
JOIN read_parquet('{tef}') b ON b.route_id = a.route_id AND b.hour = a.hour AND b.seg = a.seg AND b.daytype = a.daytype
WHERE a.daytype = 'weekday' AND a.trips_per_hour > 0""")

con.execute(f"""
CREATE TABLE p_hour AS
SELECT route_id, hour, seg, sum(tick_tr) OVER w / nullif(sum(trips) OVER w, 0) AS ci_h
FROM g WINDOW w AS (PARTITION BY route_id, hour ORDER BY seg ROWS BETWEEN {WIN} PRECEDING AND {WIN} FOLLOWING)""")
con.execute("CREATE TABLE bandagg AS SELECT route_id, band, seg, sum(tick_tr) AS tick, sum(trips) AS trips FROM g GROUP BY ALL")
con.execute(f"""
CREATE TABLE p_band AS
SELECT route_id, band, seg, sum(tick) OVER w / nullif(sum(trips) OVER w, 0) AS ci_b
FROM bandagg WINDOW w AS (PARTITION BY route_id, band ORDER BY seg ROWS BETWEEN {WIN} PRECEDING AND {WIN} FOLLOWING)""")
con.execute("CREATE TABLE b1 AS SELECT hour, sum(tick_tr) / nullif(sum(trips), 0) AS ci FROM g GROUP BY hour")
con.execute("CREATE TABLE b2r AS SELECT route_id, sum(tick_tr) / nullif(sum(trips), 0) AS ci FROM g GROUP BY route_id")
con.execute("CREATE TABLE na AS SELECT sum(tick_tr) / nullif(sum(trips), 0) AS ci FROM g")
con.execute("CREATE TABLE b3 AS SELECT route_id, hour, sum(tick_tr) / nullif(sum(trips), 0) AS ci FROM g GROUP BY route_id, hour")

con.execute(f"""
CREATE TABLE cells AS
SELECT g.route_id, g.hour, g.seg, g.trips, g.band,
       g.tick_te * {nd_te} AS y,
       b1.ci * g.trips * {nd_te}                                      AS mu_b1,
       b2r.ci * (b1.ci / na.ci) * g.trips * {nd_te}                   AS mu_b2,
       b3.ci * g.trips * {nd_te}                                      AS mu_b3,
       pb.ci_b * g.trips * {nd_te}                                    AS mu_band,
       ph.ci_h * g.trips * {nd_te}                                    AS mu_hour,
       CASE WHEN coalesce(rh.n_tix, 0) >= {A.hour_min} THEN ph.ci_h ELSE pb.ci_b END * g.trips * {nd_te} AS mu_app,
       (coalesce(rh.n_tix, 0) * ph.ci_h + {A.blend_k} * pb.ci_b) / (coalesce(rh.n_tix, 0) + {A.blend_k}) * g.trips * {nd_te} AS mu_blend,
       coalesce(rt.n_tix, 0) AS route_tix
FROM g
JOIN b1 ON b1.hour = g.hour CROSS JOIN na
JOIN b2r ON b2r.route_id = g.route_id
JOIN b3 ON b3.route_id = g.route_id AND b3.hour = g.hour
JOIN p_hour ph ON ph.route_id = g.route_id AND ph.hour = g.hour AND ph.seg = g.seg
JOIN p_band pb ON pb.route_id = g.route_id AND pb.band = g.band AND pb.seg = g.seg
LEFT JOIN rh_tr rh ON rh.route_id = g.route_id AND rh.hour = g.hour
LEFT JOIN rt_tr rt ON rt.route_id = g.route_id
WHERE g.hour BETWEEN 6 AND 21""")

MODELS = [("B1 network x hour", "mu_b1"), ("B2 route mean x hourly shape", "mu_b2"), ("B3 route x hour", "mu_b3"),
          ("OURS-band (stretch smoothed)", "mu_band"), ("OURS-hour (stretch smoothed)", "mu_hour"),
          ("OURS-app (hard switch: hour if enough tickets, else band)", "mu_app"),
          ("OURS-blend (hour and band, weight n/(n+K))", "mu_blend")]


def score(where, title):
    parts = []
    for name, col in MODELS:
        parts.append(f"""
        SELECT '{name}' AS model, count(*) AS cells,
               round(avg(2 * (CASE WHEN y > 0 THEN y * ln(y / greatest({col}, {EPS})) ELSE 0 END - (y - greatest({col}, {EPS})))), 5) AS mean_deviance,
               round(sum({col}) / nullif(sum(y), 0), 3) AS pred_over_obs
        FROM cells WHERE {where}""")
    sql = " UNION ALL ".join(parts)
    con.execute(f"CREATE OR REPLACE TABLE sc AS {sql}")
    show(title, """
    SELECT model, cells, mean_deviance,
           round(100.0 * (1 - mean_deviance / (SELECT mean_deviance FROM sc WHERE model LIKE 'B1%')), 1) AS pct_better_than_B1,
           pred_over_obs
    FROM sc ORDER BY mean_deviance DESC""")


if not A.quiet: print("\nPoisson deviance per cell on UNSEEN test days (lower is better). pred_over_obs near 1 means the totals are calibrated.")
score(f"route_tix >= {A.supported}", f"A1. Well-supported routes (>= {A.supported} training weekday tickets)")
for m, d in con.execute("SELECT model, mean_deviance FROM sc").fetchall():
    SUMMARY["dev_" + m.split()[0].lower().replace("-", "_")] = d
score(f"route_tix < {A.supported}", f"A2. Thinner routes (< {A.supported} training weekday tickets)")
score("TRUE", "A3. All routes")
for m, d in con.execute("SELECT model, mean_deviance FROM sc").fetchall():
    SUMMARY["all_dev_" + m.split()[0].lower().replace("-", "_")] = d

show("A4. Rank agreement (Spearman) between predicted and observed counts, well-supported routes", f"""
WITH r AS (SELECT y, mu_b1, mu_b2, mu_b3, mu_band, mu_hour, mu_app, mu_blend,
                  rank() OVER (ORDER BY y) AS ry, rank() OVER (ORDER BY mu_b1) AS r1, rank() OVER (ORDER BY mu_b2) AS r2,
                  rank() OVER (ORDER BY mu_b3) AS r3, rank() OVER (ORDER BY mu_band) AS rb,
                  rank() OVER (ORDER BY mu_hour) AS rh, rank() OVER (ORDER BY mu_app) AS ra, rank() OVER (ORDER BY mu_blend) AS rbl
           FROM cells WHERE route_tix >= {A.supported})
SELECT round(corr(ry, r1), 3) AS B1, round(corr(ry, r2), 3) AS B2, round(corr(ry, r3), 3) AS B3,
       round(corr(ry, rb), 3) AS ours_band, round(corr(ry, rh), 3) AS ours_hour, round(corr(ry, ra), 3) AS ours_app,
       round(corr(ry, rbl), 3) AS ours_blend, count(*) AS cells FROM r""")

# ------------------------------------------------------------------ B. level agreement
if A.levels:
    con.execute(f"""CREATE TABLE fac AS SELECT route_id, median(ci / nullif(ci_raw, 0)) AS f
                    FROM read_parquet('{A.levels.replace(chr(92), '/')}') WHERE ci_raw > 0 GROUP BY 1""")
else:
    con.execute("CREATE TABLE fac AS SELECT NULL::VARCHAR AS route_id, 1.0 AS f WHERE FALSE")

con.execute(f"""
CREATE TABLE te_bandagg AS
SELECT route_id, {band('hour')} AS band, seg, sum(ticketed_per_day) AS tick, sum(trips_per_hour) AS trips
FROM read_parquet('{tef}') WHERE daytype = 'weekday' AND trips_per_hour > 0 GROUP BY ALL""")
con.execute(f"""
CREATE TABLE p_band_te AS
SELECT route_id, band, seg, sum(tick) OVER w / nullif(sum(trips) OVER w, 0) AS ci_b
FROM te_bandagg WINDOW w AS (PARTITION BY route_id, band ORDER BY seg ROWS BETWEEN {WIN} PRECEDING AND {WIN} FOLLOWING)""")
con.execute(f"""
CREATE TABLE lv AS
SELECT a.route_id, a.band, a.seg, ba.trips,
       a.ci_b * coalesce(f.f, 1.0) AS ci_tr, b.ci_b * coalesce(f.f, 1.0) AS ci_te,
       coalesce(rt.n_tix, 0) AS n_tr, coalesce(re.n_tix, 0) AS n_te
FROM p_band a
JOIN p_band_te b ON b.route_id = a.route_id AND b.band = a.band AND b.seg = a.seg
JOIN bandagg ba ON ba.route_id = a.route_id AND ba.band = a.band AND ba.seg = a.seg
LEFT JOIN fac f ON f.route_id = a.route_id
LEFT JOIN rb_tr rt ON rt.route_id = a.route_id AND rt.band = a.band
LEFT JOIN rb_te re ON re.route_id = a.route_id AND re.band = a.band
WHERE a.band IN {DAY_BANDS}""")
thr = con.execute(f"""
WITH w AS (SELECT ci_tr AS v, trips, sum(trips) OVER (ORDER BY ci_tr) AS cum, sum(trips) OVER () AS tot
           FROM lv WHERE n_tr >= {MED_T} AND ci_tr IS NOT NULL)
SELECT min(v) FILTER (WHERE cum >= {P1} * tot), min(v) FILTER (WHERE cum >= {P2} * tot),
       min(v) FILTER (WHERE cum >= {P3} * tot) FROM w""").fetchall()[0]
if any(x is None for x in thr):
    print("\nNot enough supported cells to learn level thresholds on the training days; skipping level agreement.")
    sys.exit(0)
t1, t2, t3 = (float(x) for x in thr)
if not A.quiet: print(f"\nLevel thresholds learnt on TRAINING days only: {t1:.3f} / {t2:.3f} / {t3:.3f}  (50th / 80th / 95th percentile)")


def lvl(col):
    return f"CASE WHEN {col} < {t1} THEN 1 WHEN {col} < {t2} THEN 2 WHEN {col} < {t3} THEN 3 ELSE 4 END"


con.execute(f"""
CREATE TABLE net_band AS
SELECT band, sum(tick) / nullif(sum(trips), 0) AS ci FROM bandagg GROUP BY band""")
con.execute(f"""
CREATE TABLE ev AS
SELECT lv.*, {lvl('ci_tr')} AS level_tr, {lvl('ci_te')} AS level_te, {lvl('nb.ci')} AS level_net
FROM lv JOIN net_band nb ON nb.band = lv.band
WHERE n_tr >= {MED_T} AND n_te >= 20 AND ci_tr IS NOT NULL AND ci_te IS NOT NULL""")

ag = con.execute("""
SELECT round(100.0 * sum(trips) FILTER (WHERE level_tr = level_te) / sum(trips), 1),
       round(100.0 * sum(trips) FILTER (WHERE abs(level_tr - level_te) <= 1) / sum(trips), 1),
       round(100.0 * sum(trips) FILTER (WHERE level_net = level_te) / sum(trips), 1),
       round(100.0 * sum(trips) FILTER (WHERE abs(level_net - level_te) <= 1) / sum(trips), 1), count(*)
FROM ev""").fetchall()[0]
SUMMARY.update({"t1": round(t1, 3), "t2": round(t2, 3), "t3": round(t3, 3), "level_exact": ag[0], "level_within1": ag[1],
                "base_exact": ag[2], "base_within1": ag[3], "level_cells": ag[4]})
show("B1. Level agreement on unseen days (route-bands with >= 60 training and >= 20 test tickets; trips-weighted %)", """
SELECT 'OURS: level from training days' AS predictor, count(*) AS cells,
       round(100.0 * sum(trips) FILTER (WHERE level_tr = level_te) / sum(trips), 1) AS pct_exact,
       round(100.0 * sum(trips) FILTER (WHERE abs(level_tr - level_te) <= 1) / sum(trips), 1) AS pct_within_one_level
FROM ev
UNION ALL
SELECT 'BASELINE: same level for every route in the band', count(*),
       round(100.0 * sum(trips) FILTER (WHERE level_net = level_te) / sum(trips), 1),
       round(100.0 * sum(trips) FILTER (WHERE abs(level_net - level_te) <= 1) / sum(trips), 1)
FROM ev""")
show("B2. Confusion matrix (rows = level from training days, columns = level on test days; trip-stretch counts)", """
SELECT level_tr AS train_level,
       sum(trips) FILTER (WHERE level_te = 1) AS test_1_seats, sum(trips) FILTER (WHERE level_te = 2) AS test_2_standing,
       sum(trips) FILTER (WHERE level_te = 3) AS test_3_crowded, sum(trips) FILTER (WHERE level_te = 4) AS test_4_very_crowded
FROM ev GROUP BY 1 ORDER BY 1""")
show("B3. Agreement by time band (OURS)", """
SELECT band, count(*) AS cells,
       round(100.0 * sum(trips) FILTER (WHERE level_tr = level_te) / sum(trips), 1) AS pct_exact,
       round(100.0 * sum(trips) FILTER (WHERE abs(level_tr - level_te) <= 1) / sum(trips), 1) AS pct_within_one
FROM ev GROUP BY band ORDER BY CASE band WHEN 'early' THEN 1 WHEN 'am_peak' THEN 2 WHEN 'midday' THEN 3 WHEN 'pm_peak' THEN 4 ELSE 5 END""")

if A.summary_csv:
    import csv
    import os
    row = {"window": A.window, "percentiles": A.percentiles, "hour_min": A.hour_min, "blend_k": A.blend_k, **SUMMARY}
    new = not os.path.exists(A.summary_csv)
    with open(A.summary_csv, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)
if not A.quiet: print("\nHeadline for the technical note: report A1 (mean deviance and % better than B1), A4 (rank agreement) and B1 (exact / within-one-level agreement against the baseline).")
