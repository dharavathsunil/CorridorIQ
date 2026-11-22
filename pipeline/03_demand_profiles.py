"""
Step 3 - hourly demand profile per route x direction x daytype, with shrinkage for thin routes.

Usage:
    python 03_demand_profiles.py tickets_clean.parquet

Why: 69% of route-directions have < 1000 tickets in the month. Raw hourly shares for those are
noise. We shrink each route's hourly shape toward the network-wide shape:

    shape_shrunk[h] = (pax_h + K * network_share[h]) / (pax_total + K)

K is NOT guessed: it is chosen per confidence tier by split-half validation. Typical days of each
daytype are split in two halves (alternating dates); we predict half B's hourly shape from half A
and score it with multinomial log-loss (cross-entropy), evaluated on the SAME groups for every K.
(Mean absolute error is a poor score for sparse counts: it rewards spiky predictions.)
The chosen K per tier goes straight into the technical note.

Outputs: demand_profiles.parquet  (route_base, direction, daytype, hour, mean_pax_per_day,
         n_tickets, shape_raw, shape_shrunk, peak_index, confidence)

No GTFS needed yet. The per-bus crowding level (tickets per scheduled trip, per stretch) comes in
step 4 once GTFS is joined.
"""
import sys
import duckdb

src = sys.argv[1] if len(sys.argv) > 1 else "tickets_clean.parquet"
out = sys.argv[2] if len(sys.argv) > 2 else "demand_profiles.parquet"
K_GRID = [0.5, 5, 25, 100, 300, 1000, 3000, 1e6]   # 1e6 ~ network shape only

con = duckdb.connect()

# Typical days only (anomalous days excluded), parsed routes only (special services handled later)
con.execute(f"""
CREATE TABLE t AS
SELECT route_base, direction, daytype, hour, date, pax
FROM read_parquet('{src}')
WHERE NOT low_volume_day AND route_ok AND direction IS NOT NULL
""")

con.execute("""
CREATE TABLE days AS
SELECT date, daytype,
       dense_rank() OVER (PARTITION BY daytype ORDER BY date) % 2 AS half
FROM (SELECT DISTINCT date, daytype FROM t)
""")
con.execute("""
CREATE TABLE ndays AS SELECT daytype, count(*) AS n_days FROM days GROUP BY 1
""")

con.execute("""
CREATE TABLE rd AS
SELECT route_base, direction, daytype, count(*) AS n_tickets, sum(pax) AS pax_total
FROM t GROUP BY ALL
""")
con.execute("""
CREATE TABLE hp AS
SELECT route_base, direction, daytype, hour, sum(pax) AS pax FROM t GROUP BY ALL
""")
con.execute("""
CREATE TABLE netp AS
SELECT daytype, hour, sum(pax) * 1.0 / sum(sum(pax)) OVER (PARTITION BY daytype) AS p
FROM hp GROUP BY daytype, hour
""")
con.execute("""
CREATE TABLE halves AS
SELECT t.route_base, t.direction, t.daytype, t.hour, d.half, sum(t.pax) AS pax
FROM t JOIN days d ON d.date = t.date GROUP BY ALL
""")
con.execute("""
CREATE TABLE grid_h AS
SELECT rd.route_base, rd.direction, rd.daytype, rd.n_tickets, h.hour,
       coalesce(a.pax, 0) AS pa, coalesce(b.pax, 0) AS pb
FROM rd CROSS JOIN range(24) AS h(hour)
LEFT JOIN halves a ON a.route_base = rd.route_base AND a.direction = rd.direction
     AND a.daytype = rd.daytype AND a.hour = h.hour AND a.half = 0
LEFT JOIN halves b ON b.route_base = rd.route_base AND b.direction = rd.direction
     AND b.daytype = rd.daytype AND b.hour = h.hour AND b.half = 1
""")
con.execute("""
CREATE TABLE cmp AS
SELECT g.*, p.p AS pn,
       sum(pa) OVER w AS sa, sum(pb) OVER w AS sb
FROM grid_h g JOIN netp p USING (daytype, hour)
WINDOW w AS (PARTITION BY g.route_base, g.direction, g.daytype)
""")

TIER = "CASE WHEN n_tickets >= 2000 THEN 'high' WHEN n_tickets >= 300 THEN 'medium' ELSE 'low' END"

print("=== Split-half validation: log-loss predicting half B hourly shape from half A ===")
print("(mean cross-entropy per group, nats; LOWER is better; same groups for every K;")
print(" K=1e6 is the network shape alone, K=0.5 is almost no shrinkage)\n")
results = {}   # k -> {tier: (groups, ce)}
for k in K_GRID:
    rows = con.execute(f"""
    WITH per_group AS (
      SELECT route_base, direction, daytype, n_tickets,
             sum(CASE WHEN pb > 0 THEN (pb * 1.0 / sb) * ln((pa + {k} * pn) / (sa + {k})) ELSE 0 END) * -1 AS ce
      FROM cmp WHERE sb > 0
      GROUP BY ALL
    )
    SELECT {TIER} AS tier, count(*) AS groups, avg(ce) AS ce FROM per_group GROUP BY 1
    """).fetchall()
    results[k] = {tier: (g, c) for tier, g, c in rows}
    parts = "  ".join(f"{t}: {results[k][t][1]:.3f} (n={results[k][t][0]})"
                      for t in ("high", "medium", "low") if t in results[k])
    print(f"K={k:<9g} {parts}")

best_k = {}
for tier in ("high", "medium", "low"):
    cands = [k for k in K_GRID if tier in results[k]]
    best_k[tier] = min(cands, key=lambda k: results[k][tier][1]) if cands else 100
print("\nBest K per tier (lowest log-loss):", best_k)

K_CASE = ("CASE WHEN rd.n_tickets >= 2000 THEN %s WHEN rd.n_tickets >= 300 THEN %s ELSE %s END"
          % (best_k["high"], best_k["medium"], best_k["low"]))

con.execute(f"""
CREATE TABLE prof AS
SELECT rd.route_base, rd.direction, rd.daytype, h.hour,
       coalesce(hp.pax, 0) * 1.0 / nd.n_days                              AS mean_pax_per_day,
       rd.n_tickets,
       coalesce(hp.pax, 0) * 1.0 / rd.pax_total                          AS shape_raw,
       (coalesce(hp.pax, 0) + ({K_CASE}) * n.p) / (rd.pax_total + ({K_CASE})) AS shape_shrunk,
       CASE WHEN rd.n_tickets >= 2000 THEN 'high'
            WHEN rd.n_tickets >= 300  THEN 'medium' ELSE 'low' END        AS confidence
FROM rd CROSS JOIN range(24) AS h(hour)
JOIN ndays nd ON nd.daytype = rd.daytype
JOIN netp n ON n.daytype = rd.daytype AND n.hour = h.hour
LEFT JOIN hp ON hp.route_base = rd.route_base AND hp.direction = rd.direction
     AND hp.daytype = rd.daytype AND hp.hour = h.hour
""")
con.execute(f"""
COPY (
  SELECT *, shape_shrunk / max(shape_shrunk) OVER (PARTITION BY route_base, direction, daytype) AS peak_index
  FROM prof
) TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)
""")

print("\n=== Confidence tiers (route-direction-daytype groups) ===")
con.sql("""
SELECT confidence, daytype, count(DISTINCT route_base || direction) AS route_dirs
FROM prof GROUP BY ALL ORDER BY daytype, confidence
""").show()

print("=== Network-wide shape: weekday share of tickets by hour ===")
con.sql("SELECT hour, round(100*p, 2) AS pct FROM netp WHERE daytype='weekday' ORDER BY hour").show(max_rows=30)

print("=== Peak hour per busiest weekday route-directions (sanity check) ===")
con.sql("""
SELECT route_base, direction, n_tickets,
       arg_max(hour, shape_shrunk) AS peak_hour, round(max(shape_shrunk) * 100, 1) AS peak_share_pct
FROM prof WHERE daytype = 'weekday' GROUP BY route_base, direction, n_tickets
ORDER BY n_tickets DESC
""").show(max_rows=12)

print(f"Wrote {out}")
