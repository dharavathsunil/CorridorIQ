# Parameters and thresholds: what each one is, why that value, how we checked it

Every number that changes an output is listed here with the file that holds it. Values marked **data-derived**
are computed from the October tickets and recomputed on every run. Values marked **judgement** are choices; for
those we say so and show how sensitive the results are (`pipeline/11_sensitivity.py` writes `sensitivity.csv`).

## 1. How the index and the levels are built (60-second version)

1. Every ticket is linked to a GTFS route and to its boarding and alighting stops.
2. The ticket is spread over the stretches (pairs of consecutive stops) between those two stops, and each
   stretch is dated using the scheduled travel offsets along the route.
3. For each route, stretch and hour we divide the ticketed riders by the number of scheduled trips crossing it.
   This is the **Crowding Index (CI): ticketed riders per scheduled trip.** It is a lower bound on the real load,
   because tickets cover only a small share of riders (about 94,000 a day against about 40 lakh bus riders).
4. CI is smoothed over 5 consecutive stretches and grouped into time bands. Where a route has enough tickets in the departure hour, the hourly estimate is blended with the band estimate.
5. **Levels are percentiles of CI.** Across all weekday, daytime, well-supported route-stretches (weighted by
   scheduled trips) the 50th, 80th and 95th percentile of CI are **0.34, 0.90 and 2.13**. So:
   - below 0.34: **Seats likely** (quieter than the typical half of Delhi bus stretches),
   - 0.34 to 0.90: **Standing room**,
   - 0.90 to 2.13: **Crowded** (in the busiest 20%),
   - above 2.13: **Very crowded** (in the busiest 5%).

**Why percentiles and not passenger counts.** Tickets are a small and uneven sample, so the true load cannot be
recovered: any single scale factor gave impossible loads (with one network-wide factor, 41% of well-supported
weekday cells exceeded 100 passengers). Percentiles need no scale factor, mean the same thing for every route,
and are easy to explain to a commuter ("busier than 8 in 10 Delhi bus stretches at this time of week").

## 2. Parameter table

| Parameter | File | Value | What it does | How the value was chosen | Evidence / sensitivity |
|---|---|---|---|---|---|
| Low-volume day flag | `02_clean_tickets.py` | below 80% of the median for the day type | Excludes outage or holiday-like days from typical-day profiles | Judgement | Flags 2, 18, 19 and 20 Oct; removing the flag changes none of the method |
| Route-name fallbacks | `05_match_tickets_to_gtfs.py` | exact, then drop variant number, drop STL | Match ticket routes that carry variant tags | Rule from the data: GTFS route names equal ticket route strings | 98.4% exact, 0.4% by fallback, 1.2% unmatched |
| Stop-name match | `05_match_tickets_to_gtfs.py` | exact, then fuzzy (token-sort >= 85), then subset (token-set >= 95), only among stops on that route | Link ticket stop names to GTFS stops | High cut-offs limit false matches; candidates restricted to the route's own stops | 82.6% of tickets get both stops; boarding before alighting for 98.9% (exact), 98.5% (fuzzy), 97.9% (subset) |
| Route weight cap | `06_segment_load.py` (`W_CAP`) | 4 | Re-weights a route by tickets lost to unmatched stops | Judgement: stops a route with few matched tickets being inflated more than 4x | Median weight 1.03, 95th percentile 2.24, 24 of 1,969 routes at the cap |
| Canonical route pattern | `06_segment_load.py` | route's longest scheduled trip (ties: smallest trip id) | Defines stretch order | Deterministic rule | Fixed tie-break gives identical reruns |
| Smoothing window | `08_crowding_levels.py` (`WIN`) | 2 (5 stretches) | Pools neighbouring stretches because one ticket covers several | Pre-specified, then tested | On the validation split the blend's deviance for windows 0, 1, 2, 3, 5 is 2.301, 2.301, 2.317, 2.348, 2.447 (all routes), so windows 0 to 2 are within 1%, below the 2% needed to change a parameter. Level agreement rises slightly with more smoothing (83.9% to 84.6%). Kept at 2 for steadier levels and cleaner maps |
| Time bands | `08_crowding_levels.py` | early 6-8, AM peak 8-11, midday 11-17, PM peak 17-20, late 20-24 | Groups hours so cells have enough tickets | Read off the network hourly profile (peaks at 8-9 and 18-19) | AM and PM peak bands are both three hours wide so they compare fairly |
| Operator coverage factor | `08_crowding_levels.py` | DTC 0.78, cluster 1.79 | Reweights each operator's tickets for unequal ticket coverage | Data-derived given published ridership (2.43M DTC, 1.53M cluster; assumptions you can override) | Independent evidence: for the same vehicle series cluster buses log 1.4 to 2.1 tickets per vehicle-day against 4.7 to 6.9 for DTC. Switching it off (`--no-adjust`) changes the level of 21.5% of weekday trip-stretches, always by one level |
| Level cut-offs | `08_crowding_levels.py` (`P1,P2,P3`) | percentiles 50 / 80 / 95 | Turn CI into four levels | Judgement for the percentiles (typical, busy, exceptional); the CI values are data-derived | Level agreement on unseen days is 83.8% to 88.5% exact across three alternative percentile sets against 41.4% to 56.3% for the same-level-for-every-route baseline (86.5% against 49.7% at the default); within one level 99.8% to 99.9% |
| Confidence of a route-band | `08_crowding_levels.py` (`HIGH_T`, `MED_T`) | high >= 300 tickets, medium >= 60, else 'no data' | Stops thin cells showing a made-up level | Poisson: relative error about 1/sqrt(n), so about 6% at 300 and 13% at 60 | Week-to-week rank correlation 0.965 for routes with 150+ tickets a week, 0.947 for 30-149 |
| Hour / time-band blend | `09_build_app_data.py` (`--blend-k`, `--hour-floor`) | K = 10 tickets; hourly estimate used from 20 route-hour tickets | Level for a departure hour = weight n/(n+K) on the hourly estimate plus the rest on the time-band estimate (n = route tickets in that hour). With K = 10 the hour gets 91% of the weight at 100 tickets and 50% at 10 | **Chosen on a validation split** (train 1-14 Oct, validate 15-21 Oct) by lowest deviance, taking the largest K within 2% of the best | Validation deviance, all routes: K=5 2.100, **K=10 2.108**, K=25 2.153, K=50 2.221, K=100 2.317, K=200 2.432. K=10 is 9.0% better than the original K=100 and within 0.4% of the best. Test days were not used |
| Level cut-offs are trip-weighted | `08_crowding_levels.py` | weights = scheduled trips | Percentiles reflect what a commuter meets on a bus, not what a timetable lists | Design choice | Stated in the note |
| Estimated load scale factor `S` | `06_segment_load.py` | about 43.5 | Diagnostic only; never shown | Published daily ridership / ticketed riders | Produced impossible loads, which is why the index is relative |
| Map line geometry | `09_build_app_data.py` (`SNAP_MAX_M`, `SIMPLIFY_M`) | stops snap to the route's GTFS shape within 150 m; lines simplified to 6 m | Draws each stretch along the scheduled road path instead of a straight stop-to-stop line | Judgement: 150 m allows for stops set back from the road without snapping to the wrong road; 6 m is far below what the eye sees on a city map | Display only, it does not change any crowding number. The build prints how many stops snap within 50 m and 150 m and how many stretches fall back to straight lines |
| Walking speed, detour | `app/planner.py` | 1.25 m/s, 1.3 | Walk times | Standard planning values | |
| Walk radius | `app/planner.py` | 800 m (2 km fallback) | Which stops are reachable | Standard planning value | |
| Transfers | `app/planner.py` | stops within 250 m, 2 min buffer, wait up to 40 min | Allowed changes | Judgement | Tests cover direct and one-change journeys |
| Discomfort weights | `app/planner.py` | 0 / 0.15 / 0.4 / 0.8 discomfort minutes per ride minute for seats / standing / crowded / very crowded; no data = 0.15 | Convert crowding into time so options can be ranked | Judgement: no commuter-choice data was available | The slider (0 to 1) scales it up to 4x, so users set their own trade-off |
| Search window | `app/planner.py` | 90 minutes, up to 4 departures per route and stop pair at least 8 minutes apart, 6 options | How far ahead to look | Judgement | |

## 3. How parameters were chosen (and what was not)
Parameters that were not fixed from first principles were chosen on a **validation split** (train 1 to 14 October,
validate 15 to 21 October; `python run_all.py --select-params`). The **test period (22 to 30 October) was not used to choose
anything.** Rule: a parameter moves away from its pre-specified value only if validation improves by more than 2%, and
among values within 2% of the best we take the more conservative one. Result: K moved from 100 to 10; the smoothing window,
percentiles and everything else stayed as specified.

## 4. What is not tuned, and why

We did not fit the discomfort weights, the percentile choice or the operator figures to maximise any score. They are
stated assumptions. The validation (`docs/VALIDATION.md`) tests whether the index predicts unseen days; it does not
prove the weights match what commuters prefer.

## 5. The one-minute explanation of the thresholds

> Tickets cover only a small share of riders, so we do not claim passenger counts. For each stretch and hour we
> measure how many ticketed riders a scheduled bus carries. We then rank every Delhi bus stretch on that measure.
> The median is 0.34 ticketed riders per trip: below it is "seats likely". The busiest fifth starts at 0.90 and the
> busiest 5% at 2.13. A level therefore says how the stretch compares with the rest of the network at that time. Where
> a route has too few tickets we say "no data" instead of guessing, and we test the whole thing on days it has not seen.
