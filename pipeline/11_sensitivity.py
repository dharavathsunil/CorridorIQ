"""
Step 11 - sensitivity of the validation results to the main parameters.

Usage (after the train/test files of step 10 exist):
    python pipeline/11_sensitivity.py tickets_matched.parquet seg_load_train.parquet seg_load_test.parquet \\
           --train-to 2026-10-21 --test-from 2026-10-22 --levels crowd_levels.parquet

Runs step 10 repeatedly, changing ONE parameter at a time around the defaults
(window = 2 stretches, cut-off percentiles = 50/80/95, hard-switch hourly minimum = 100 tickets, blend K = 10), and prints one table:
  dev_*      mean Poisson deviance on unseen days, well-supported routes (all_dev_*: all routes); lower is better
  level_exact / level_within1   share of cells whose level from training days equals / is within one level of the test level
  t1 t2 t3                      the level thresholds those percentiles produce
Writes sensitivity.csv (or --out). Use --test-to when the 'test' period must stop before a later untouched period.
"""
import argparse
import os
import subprocess
import sys

import duckdb

ap = argparse.ArgumentParser()
ap.add_argument("tickets")
ap.add_argument("train")
ap.add_argument("test")
ap.add_argument("--train-to", required=True)
ap.add_argument("--test-from", required=True)
ap.add_argument("--levels", default=None)
ap.add_argument("--test-to", default=None)
ap.add_argument("--out", default="sensitivity.csv")
A = ap.parse_args()
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = A.out
if os.path.exists(OUT):
    os.remove(OUT)

K0 = 10   # default blend constant (chosen on the validation split)
runs = [(w, "50,80,95", 100, K0) for w in (0, 1, 2, 3, 5)]
runs += [(2, p, 100, K0) for p in ("40,70,90", "50,75,90", "60,85,97")]
runs += [(2, "50,80,95", h, K0) for h in (50, 200)]
runs += [(2, "50,80,95", 100, k) for k in (5, 25, 50, 100, 200, 400)]
for w, pct, hm, bk in runs:
    cmd = [sys.executable, os.path.join(HERE, "10_validate.py"), A.tickets, A.train, A.test, "--train-to", A.train_to,
           "--test-from", A.test_from, "--window", str(w), "--percentiles", pct, "--hour-min", str(hm), "--blend-k", str(bk), "--quiet",
           "--summary-csv", OUT]
    if A.levels:
        cmd += ["--levels", A.levels]
    if A.test_to:
        cmd += ["--test-to", A.test_to]
    print(f"running window={w} percentiles={pct} hour_min={hm} blend_k={bk} ...", flush=True)
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit("a validation run failed")
print()
cols = ('"window", percentiles, hour_min, blend_k, dev_ours_band, dev_ours_app, dev_ours_blend, dev_ours_hour, '
        'all_dev_ours_band, all_dev_ours_app, all_dev_ours_blend, all_dev_ours_hour, level_exact, base_exact')
query = f"SELECT {cols} FROM read_csv('{OUT}') ORDER BY 1, 2, 3, 4"
duckdb.sql(query).show(max_rows=30, max_width=220)
print("Defaults are window=2, percentiles=50,80,95, hour_min=100, blend_k=10. Read each row against that default row.")
