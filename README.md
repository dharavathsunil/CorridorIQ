# CorridorIQ: crowding-aware Delhi bus journey planner
Transport Stack Hackathon 2026, Problem 2.

Turns October 2026 bus e-ticket / ETM records into a **relative crowding index** per route, stretch and time
of day, and shows it next to travel time in a journey planner, with an operator view of overloaded routes.

## Quick start (prebuilt data, about 2 minutes)
1. `pip install -r requirements.txt`
2. Download `app.duckdb` from the Releases page: [release link] and put it in `app/data/`.
   It contains only the public GTFS timetable and aggregated crowding levels, no individual tickets.
3. `cd app` then `uvicorn main:app`, and open http://127.0.0.1:8000
4. Click 'Try an example' (Kapashera Border to Saket Metro Station, 08:30 weekday), then change the time to 11:30.

## Rebuild everything from the raw data (one command)
Raw tickets and GTFS are not in this repository. Get them from https://delhi.transportstack.in
(register and request access), put the GTFS files in `gtfs/` and the ticket CSV next to this file, then:
```
pip install -r requirements.txt
python run_all.py --gtfs gtfs --tickets October-Delhi.csv --validate
cd app && uvicorn main:app
```
`run_all.py` runs the numbered scripts in `pipeline/` in order: clean, match to GTFS, stretch loads, crowding levels,
app database, and (with `--validate`) the train/test split and held-out validation.

## Tests and validation
- `pip install -r requirements-dev.txt` then `pytest`: 17 tests on a tiny toy network with hand-checkable answers.
- `docs/VALIDATION.md`: train on 1-21 Oct, test on 22-31 Oct against simple baselines, plus level agreement.
- `pipeline/11_sensitivity.py`: how results change when each main parameter changes.

## Documentation
- `docs/PARAMETERS.md`: every parameter and threshold, why that value, and how it was checked.
- `docs/VALIDATION.md`: the validation design and how to read the results.
- Technical note and proposal: linked from the submission.

## Method in brief
1. Clean tickets, parse route strings (UP/DOWN, variants), normalise stop names.
2. Match ticket routes to GTFS routes (98.4% exact) and ticket stop names to stops on that route (exact, fuzzy, subset).
   Check: boarding before alighting for about 98.8% of matched tickets; fares rise with distance travelled.
3. Spread each ticket over the stretches between boarding and alighting; divide by scheduled trips per stretch and hour.
4. Tickets cover only a small share of riders and coverage differs by operator, so no absolute load is claimed.
   Crowding Index = ticketed riders per scheduled trip (a lower bound), smoothed over 5 stretches, time-banded,
   with an operator coverage factor. Levels are weekday percentiles (50/80/95), giving thresholds 0.34 / 0.90 / 2.13.
5. Confidence from ticket counts per route and band; thin cells show 'no data'.
6. Map lines follow each route's GTFS shape (`shapes.txt`): stops are snapped onto the shape in order, and a stretch whose stops do not fit the shape stays a straight line.

## Limitations
Relative levels only; weekends are mostly 'no data'; pass holders and bus size are not modelled; the timetable is
assumed to equal the service operated. See the technical note.
