"""
FastAPI service for the crowding-aware Delhi bus journey planner.

Run (from the transport_app/app folder):
    uvicorn main:app --reload
then open http://127.0.0.1:8000  (interactive API docs at /docs)
"""
import os

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from planner import Planner

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("APP_DB", os.path.join(APP_DIR, "data", "app.duckdb"))

app = FastAPI(
    title="Crowding-aware Delhi bus journey planner",
    description="Journey options between two points with a relative crowding level for every bus ride, "
                "built from Oct 2026 ticket data and the static Delhi bus GTFS.",
    version="0.1",
)
planner = Planner(DB)

DAYS = "^(weekday|sat|sun)$"
BANDS = "^(night|early|am_peak|midday|pm_peak|late)$"


@app.get("/api/meta", summary="Level definitions, thresholds and data notes")
def meta():
    return planner.meta()


@app.get("/api/stops", summary="Stop-name search (autocomplete)")
def stops(q: str = Query(..., min_length=2), limit: int = Query(10, ge=1, le=25)):
    return planner.search_stops(q, limit)


@app.get("/api/journeys", summary="Bus journey options with crowding per ride")
def journeys(
    from_lat: float, from_lon: float, to_lat: float, to_lon: float,
    depart: str = Query("08:30", pattern=r"^\d{1,2}:\d{2}$", description="HH:MM"),
    day: str = Query("weekday", pattern=DAYS),
    comfort: float = Query(0.5, ge=0.0, le=1.0, description="0 = fastest, 1 = prefer less crowded"),
    window_min: int = Query(90, ge=10, le=180, description="how long after `depart` to look for buses"),
    max_options: int = Query(6, ge=1, le=10),
):
    try:
        return planner.plan(from_lat, from_lon, to_lat, to_lon, depart, day, comfort, window_min, max_options)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/route/{route_id}", summary="Crowding along one route, stretch by stretch")
def route(route_id: str, day: str = Query("weekday", pattern=DAYS), band: str = Query("am_peak", pattern=BANDS)):
    return planner.route_stretches(route_id, day, band)


@app.get("/api/operator/top", summary="Operator view: routes with the most overloaded stretches")
def operator_top(day: str = Query("weekday", pattern=DAYS), band: str = Query("am_peak", pattern=BANDS),
                 limit: int = Query(25, ge=1, le=100)):
    return planner.operator_top(day, band, limit)


app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(APP_DIR, "static", "index.html"))
