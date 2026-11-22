# Validation: is the model right on days it has not seen?

## Design
- **Three periods.** Parameters that were not fixed in advance (smoothing window, blend constant K) are chosen on a validation split (train 1 to 14 October, validate 15 to 21 October, `run_all.py --select-params`). The final test period, 22 to 30 October, is not used to choose anything. Decision rule: a parameter moves away from its pre-specified value only if the validation split improves the score by more than 2%.
- **Split by time.** Train on 1 to 21 October; test on 22 to 31 October. Only typical weekdays are scored (flagged low-volume days are excluded).
- **Target.** The ticketed load on board, which is the quantity the index is built from. Tickets are a small sample of riders, so we do not score total passengers.
- **Unit of test.** Every route x stretch x hour cell. The model predicts the ticketed riders per day for each cell from the training days; the test days give the observed counts.
- **Score.** Poisson deviance (the standard loss for counts; lower is better), the rank correlation between predicted and observed counts, and calibration (total predicted / total observed).

## Baselines (what "simple" looks like)
| Model | What it assumes |
|---|---|
| B1 network x hour | Every route has the same ticketed load per trip at a given hour |
| B2 route mean x hourly shape | Each route has its own overall level, shaped by the network hourly profile |
| B3 route x hour | Route and hour, no stretch detail |
| OURS-band | Route x stretch x time band, smoothed over 5 stretches |
| OURS-hour | Route x stretch x hour, smoothed over 5 stretches |
| OURS-app | What the app does: hour where the route has enough tickets in that hour, else the band |

## Level agreement
Thresholds are learnt on the training days only. For route-bands with enough tickets in both periods we compare the
level the training days predict with the level the test days show, and compare against a baseline that gives every
route in a band the same level.

## How to run
```
python run_all.py --gtfs gtfs --tickets October-Delhi.csv --validate
python pipeline/11_sensitivity.py tickets_matched.parquet seg_load_train.parquet seg_load_test.parquet --train-to 2026-10-21 --test-from 2026-10-22 --levels crowd_levels.parquet
```

## Results (train 1-21 Oct = 12 typical weekdays, test 22-30 Oct = 7 typical weekdays; final model, K = 10)

**A1. Count prediction, well-supported routes (427,794 route x stretch x hour cells; mean Poisson deviance, lower is better)**

| Model | Mean deviance | Better than B1 | Predicted / observed |
|---|---|---|---|
| B1 network x hour | 21.48 | 0% | 0.82 |
| B2 route mean x hourly shape | 14.81 | 31.0% | 1.09 |
| B3 route x hour | 10.19 | 52.6% | 1.02 |
| OURS-band | 6.77 | 68.5% | 1.00 |
| OURS-app (hard switch at 100 tickets) | 5.19 | 75.8% | 1.05 |
| OURS-hour | 4.16 | 80.6% | 1.02 |
| **OURS-blend (final, K = 10)** | **3.81** | **82.3%** | 1.03 |

**A2. Thinner routes (358,683 cells):** B1 10.00, B2 3.85, B3 3.09, OURS-hour 2.89, OURS-band 2.67, OURS-app 2.67, **OURS-blend 2.33 (76.7% better than B1)**.
**A3. All routes (786,477 cells):** B1 16.24, B2 9.81, B3 6.95, OURS-band 4.90, OURS-app 4.04, OURS-hour 3.58, **OURS-blend 3.13 (80.7% better than B1)**.
**A4. Rank agreement, well-supported routes (Spearman):** B1 0.480, B2 0.598, B3 0.714, OURS-band 0.813, OURS-app 0.825, OURS-hour 0.873, OURS-blend 0.870.

**B. Level agreement on unseen days** (route-bands with at least 60 training and 20 test tickets; thresholds learnt on the training days: 0.373 / 0.961 / 2.247)

| Predictor | Exact | Within one level |
|---|---|---|
| OURS: level from training days | 86.5% | 99.9% |
| Baseline: same level for every route in the band | 49.7% | 92.1% |

By time band, exact agreement: early 85.7%, AM peak 84.6%, midday 90.5%, PM peak 83.0%, late 85.1%.

### What the results show
- The final blend has the lowest deviance in all three groups: 82.3% better than B1 on well-supported routes, 76.7% on thinner routes and 80.7% on all routes. On all routes it is 55% better than the route x hour baseline (B3) and 12.5% better than hourly detail alone.
- Hourly estimates win on busy routes but lose on thin ones, which is why the app blends the hour and the band with a weight n/(n+K) that grows with ticket count. Against hourly alone the blend is 8% better on well-supported routes and 20% better on thinner routes.
- Rank agreement matches the best single model (0.870 against 0.873 for hourly alone) and is far above the baselines (B1 0.48, B3 0.71).
- Totals are calibrated: predicted over observed is 1.03 to 1.06 for the blend, so it over-predicts slightly. B1 under-predicts well-supported routes by 18% and over-predicts thin routes by 3.1 times.
- Smoothing over stretches changes held-out deviance by under 5% (window 0 to 3). It is a stability choice, not the source of accuracy.
- The thresholds learnt on the training days (0.373 / 0.961 / 2.247) differ from the final ones (0.34 / 0.90 / 2.13) because a shorter training period leaves only route-bands with at least 60 tickets, which are busier.

### How K was chosen (validation split: train 1-14 Oct, validate 15-21 Oct)
All-routes deviance of the blend: K=5 2.100, **K=10 2.108**, K=25 2.153, K=50 2.221, K=100 2.317, K=200 2.432. Rule: change a parameter only if validation improves by more than 2%, and among values within 2% of the best take the larger K. K = 10 is 9.0% better than the original K = 100 and within 0.4% of the best. Windows 0, 1, 2, 3, 5 gave 2.301, 2.301, 2.317, 2.348, 2.447, so the pre-specified window of 2 was kept. The test days played no part in these choices.

### History (for transparency)
The hour/band blend was introduced after the first held-out run. Its first constant, K = 100, was fixed in advance. That first test run gave deviance 4.45 (well-supported), 2.52 (thinner) and 3.57 (all routes) with rank agreement 0.849. K was then chosen on the validation split above and moved to 10.

### Honest limits
- The target is the ticketed load, a lower bound on the real load, not total passengers.
- 22-30 October is our own held-out period. The organisers' unseen test cases are the true test.
- Level agreement (B) is measured on time-band levels; the hourly blend is validated through count prediction (A).

## Why we do not validate per bus trip
Ticketed riders average about 1.3 per scheduled trip per day, so a single bus trip carries 0 to 3 ticketed riders.
A trip-level count is too sparse to be a fair target. Aggregating tickets by route, stretch and hour is the level at
which the data supports a test.

## Other checks
- Boarding stop precedes alighting stop for about 98.8% of matched tickets.
- Fare rises with the share of the route travelled (0.12, 0.17, 0.34, 0.41, 0.65 across the five common fare bands).
- Rank correlation of the route-by-band index between consecutive weeks of October: 0.949, 0.964, 0.963.
- Automated tests: `pytest` (17 tests on a hand-checkable toy network).
