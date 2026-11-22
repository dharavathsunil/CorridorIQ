"""
One command to rebuild everything from raw data.

    python run_all.py --gtfs gtfs --tickets October-Delhi.csv            # build the app database
    python run_all.py --gtfs gtfs --tickets October-Delhi.csv --validate # also run the held-out validation

Steps (each is a script in pipeline/ and can also be run alone):
    02 clean tickets -> 05 match to GTFS -> 06 stretch loads -> 08 crowding levels -> 09 app database
    with --validate: 06 on training days, 06 on test days, 10 held-out validation
    with --select-params: 06 on days 1-14 and 15-21, then 11 sensitivity on that split (test days 22-31 stay untouched)

Outputs land in the current folder; the app reads app/data/app.duckdb.
Train/test split for validation: --train-to 2026-10-21 and --test-from 2026-10-22 (weekdays only are scored).
"""
import argparse
import os
import subprocess
import sys
import time

ap = argparse.ArgumentParser()
ap.add_argument("--gtfs", required=True, help="folder with the static GTFS .txt files")
ap.add_argument("--tickets", required=True, help="October ticket CSV")
ap.add_argument("--validate", action="store_true", help="also run the train/test validation")
ap.add_argument("--select-params", action="store_true",
                help="choose parameters on a VALIDATION split (train 1-14 Oct, validate 15-21 Oct), leaving the test days untouched")
ap.add_argument("--blend-k", type=int, default=10, help="hour/band blend constant K (see docs/PARAMETERS.md)")
ap.add_argument("--train-to", default="2026-10-21")
ap.add_argument("--test-from", default="2026-10-22")
A = ap.parse_args()
P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline")


def run(title, *args):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}", flush=True)
    t = time.time()
    r = subprocess.run([sys.executable, *args])
    if r.returncode != 0:
        sys.exit(f"\nStep failed: {title}")
    print(f"[{time.time() - t:.0f}s]", flush=True)


run("02 clean tickets", os.path.join(P, "02_clean_tickets.py"), A.tickets)
run("05 match tickets to GTFS", os.path.join(P, "05_match_tickets_to_gtfs.py"), A.gtfs, "tickets_clean.parquet")
run("06 stretch loads (all days)", os.path.join(P, "06_segment_load.py"), A.gtfs, "tickets_matched.parquet")
run("08 crowding levels", os.path.join(P, "08_crowding_levels.py"), A.gtfs, "tickets_matched.parquet",
    "seg_load.parquet", "canon_stops.parquet")
run("09 app database", os.path.join(P, "09_build_app_data.py"), A.gtfs, "crowd_levels.parquet", "canon_stops.parquet",
    "--seg-load", "seg_load.parquet", "--tickets", "tickets_matched.parquet", "--blend-k", str(A.blend_k))
if A.validate:
    run("06 stretch loads (training days)", os.path.join(P, "06_segment_load.py"), A.gtfs, "tickets_matched.parquet",
        "--to-date", A.train_to, "--suffix", "_train")
    run("06 stretch loads (test days)", os.path.join(P, "06_segment_load.py"), A.gtfs, "tickets_matched.parquet",
        "--from-date", A.test_from, "--suffix", "_test")
    run("10 held-out validation", os.path.join(P, "10_validate.py"), "tickets_matched.parquet", "seg_load_train.parquet",
        "seg_load_test.parquet", "--train-to", A.train_to, "--test-from", A.test_from, "--levels", "crowd_levels.parquet",
        "--blend-k", str(A.blend_k))
if A.select_params:
    run("06 stretch loads (selection: train 1-14 Oct)", os.path.join(P, "06_segment_load.py"), A.gtfs, "tickets_matched.parquet",
        "--to-date", "2026-10-14", "--suffix", "_sel_train")
    run("06 stretch loads (selection: validate 15-21 Oct)", os.path.join(P, "06_segment_load.py"), A.gtfs, "tickets_matched.parquet",
        "--from-date", "2026-10-15", "--to-date", "2026-10-21", "--suffix", "_sel_val")
    run("11 parameter sensitivity on the validation split", os.path.join(P, "11_sensitivity.py"), "tickets_matched.parquet",
        "seg_load_sel_train.parquet", "seg_load_sel_val.parquet", "--train-to", "2026-10-14", "--test-from", "2026-10-15",
        "--test-to", "2026-10-21", "--levels", "crowd_levels.parquet", "--out", "sensitivity_validation.csv")
print("\nDone. Start the app:  cd app  &&  uvicorn main:app")
