"""Builds a tiny, fully known world (4 stops on a line, 3 routes) and the app database from it,
so every test has a hand-checkable expected answer."""
import os
import subprocess
import sys

import duckdb
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

STOPS = [("A", "Alpha", 28.600, 77.200), ("B", "Bravo", 28.610, 77.210),
         ("C", "Charlie", 28.620, 77.220), ("D", "Delta", 28.630, 77.230)]


def _t(h, m, add=0):
    s = h * 3600 + m * 60 + add * 60
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:00"


def _write(path, header, rows):
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n")
        for r in rows:
            f.write(",".join(str(x) for x in r) + "\n")


@pytest.fixture(scope="session")
def db_path(tmp_path_factory):
    d = tmp_path_factory.mktemp("world")
    g = d / "gtfs"
    g.mkdir()
    _write(g / "stops.txt", "stop_id,stop_name,stop_lat,stop_lon", STOPS)
    _write(g / "routes.txt", "agency_id,route_id,route_long_name,route_short_name,route_desc,route_type",
           [("DTC", "1", "R1UP", "R1UP", "Alpha to Delta (all stops)", 3),
            ("DIMTS", "2", "R2UP", "R2UP", "Alpha to Delta (slow)", 3),
            ("DTC", "3", "R3UP", "R3UP", "Alpha to Delta (no crowding data)", 3)])
    trips, stop_times = [], []
    # route 1: A-B-C-D, 5 minutes between stops; trips 08:00, 08:20 (am peak), 10:50 (am peak), 11:05 (midday)
    for tid, (h, m) in {"r1_0800": (8, 0), "r1_0820": (8, 20), "r1_1050": (10, 50), "r1_1105": (11, 5)}.items():
        trips.append(("1", "s1", tid))
        for i, s in enumerate("ABCD"):
            stop_times.append((tid, _t(h, m, 5 * i), _t(h, m, 5 * i), s, i))
    # route 2: A-D only, 30 minutes, at 08:00 (slower, equally crowded -> must be dominated by route 1)
    trips.append(("2", "s1", "r2_0800"))
    stop_times += [("r2_0800", _t(8, 0), _t(8, 0), "A", 0), ("r2_0800", _t(8, 30), _t(8, 30), "D", 1)]
    # route 3: A-D at 09:00, no crowding rows at all
    trips.append(("3", "s1", "r3_0900"))
    stop_times += [("r3_0900", _t(9, 0), _t(9, 0), "A", 0), ("r3_0900", _t(9, 20), _t(9, 20), "D", 1)]
    _write(g / "trips.txt", "route_id,service_id,trip_id,shape_id", [t + (("shp1",) if t[0] == "1" else ("",)) for t in trips])
    # route 1's road bends away from the straight line between stops: A -> east -> B -> north -> C -> east -> D
    _write(g / "shapes.txt", "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence,shape_dist_traveled",
           [("shp1", la, lo, i + 1, 0) for i, (la, lo) in enumerate([
               (28.600, 77.200), (28.600, 77.210), (28.610, 77.210), (28.620, 77.210),
               (28.620, 77.220), (28.630, 77.220), (28.630, 77.230)])])
    _write(g / "stop_times.txt", "trip_id,arrival_time,departure_time,stop_id,stop_sequence", stop_times)

    con = duckdb.connect()
    canon_rows = [("1", s, i, 300 * i) for i, s in enumerate("ABCD")] + \
                 [("2", "A", 0, 0), ("2", "D", 1, 1800), ("3", "A", 0, 0), ("3", "D", 1, 1200)]
    con.execute("CREATE TABLE canon(route_id VARCHAR, stop_id VARCHAR, idx INTEGER, off_s INTEGER)")
    con.executemany("INSERT INTO canon VALUES (?,?,?,?)", canon_rows)
    canon_pq = (d / "canon_stops.parquet").as_posix()
    con.execute(f"COPY canon TO '{canon_pq}' (FORMAT PARQUET)")
    lv = []
    for seg in range(3):  # route 1: very crowded in the am peak, seats in the midday band
        lv.append(("1", "weekday", "am_peak", seg, 3.0, 3.0, 4, "high", 500, 3))
        lv.append(("1", "weekday", "midday", seg, 0.2, 0.2, 1, "high", 500, 3))
    lv.append(("2", "weekday", "am_peak", 0, 3.0, 3.0, 4, "high", 500, 1))
    con.execute("CREATE TABLE lv(route_id VARCHAR, daytype VARCHAR, band VARCHAR, seg INTEGER, ci DOUBLE, ci_raw DOUBLE, "
                "level INTEGER, confidence VARCHAR, n_tix INTEGER, trips INTEGER)")
    con.executemany("INSERT INTO lv VALUES (?,?,?,?,?,?,?,?,?,?)", lv)
    levels_pq = (d / "crowd_levels.parquet").as_posix()
    con.execute(f"COPY lv TO '{levels_pq}' (FORMAT PARQUET)")
    out = (d / "app.duckdb").as_posix()
    subprocess.run([sys.executable, os.path.join(ROOT, "pipeline", "09_build_app_data.py"), str(g), levels_pq,
                    canon_pq, "--out", out], check=True, cwd=ROOT, capture_output=True)
    return out


@pytest.fixture(scope="session")
def planner(db_path):
    from planner import Planner
    return Planner(db_path)


@pytest.fixture(scope="session")
def client(db_path):
    os.environ["APP_DB"] = db_path
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app)
