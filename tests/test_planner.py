import math

from planner import LEVEL_LABELS, band_of, haversine, hhmm, parse_hhmm

A = (28.600, 77.200)
D = (28.630, 77.230)


def test_time_bands_cover_the_day():
    assert [band_of(h) for h in (3, 6, 7, 8, 10, 11, 16, 17, 19, 20, 23)] == \
        ["night", "early", "early", "am_peak", "am_peak", "midday", "midday", "pm_peak", "pm_peak", "late", "late"]


def test_time_helpers_round_trip():
    assert hhmm(8 * 3600 + 30 * 60) == "08:30"
    assert hhmm(25 * 3600) == "01:00"          # times after midnight wrap
    assert parse_hhmm("8:05") == 8 * 3600 + 5 * 60


def test_haversine_one_degree_of_latitude():
    assert math.isclose(haversine(28.0, 77.0, 29.0, 77.0), 111_195, rel_tol=0.005)


def test_levels_follow_thresholds(planner, monkeypatch):
    monkeypatch.setattr(planner, "t1", 0.34)
    monkeypatch.setattr(planner, "t2", 0.90)
    monkeypatch.setattr(planner, "t3", 2.13)
    assert [planner.level_of(x) for x in (0.0, 0.33, 0.34, 0.89, 0.90, 2.12, 2.13, 9.0)] == [1, 1, 2, 2, 3, 3, 4, 4]


def test_leg_crowding_uses_the_right_band(planner):
    cur = planner.con.cursor()
    peak = planner.leg_crowding(cur, "1", "A", "D", "weekday", "am_peak", 8)
    mid = planner.leg_crowding(cur, "1", "A", "D", "weekday", "midday", 11)
    assert peak["level"] == 4 and peak["label"] == "Very crowded" and peak["crowded_share"] == 1.0
    assert mid["level"] == 1 and mid["label"] == "Seats likely" and mid["crowded_share"] == 0.0
    assert len(peak["stretches"]) == 3


def test_leg_without_data_is_labelled_no_data(planner):
    leg = planner.leg_crowding(planner.con.cursor(), "3", "A", "D", "weekday", "am_peak", 9)
    assert leg["level"] == 0 and leg["label"] == LEVEL_LABELS[0] and leg["has_data"] is False


def test_boarding_after_alighting_gives_no_crowding(planner):
    leg = planner.leg_crowding(planner.con.cursor(), "1", "D", "A", "weekday", "am_peak", 8)
    assert leg["level"] == 0 and leg["stretches"] == []


def test_fastest_setting_takes_the_first_bus(planner):
    r = planner.plan(*A, *D, depart="10:45", day="weekday", comfort=0.0, window_min=30)
    assert r["options"][0]["depart_origin"] == "10:50" and r["options"][0]["arrive"] == "11:05"
    assert "Fastest arrival" in r["options"][0]["tags"]


def test_comfort_setting_waits_for_the_quieter_bus(planner):
    r = planner.plan(*A, *D, depart="10:45", day="weekday", comfort=1.0, window_min=30)
    best = r["options"][0]
    assert best["depart_origin"] == "11:05" and best["overall_label"] == "Seats likely"
    assert any("Most comfortable" in o["tags"] for o in r["options"])


def test_slower_and_no_less_crowded_option_is_dropped(planner):
    r = planner.plan(*A, *D, depart="07:50", day="weekday", comfort=0.5, window_min=30)
    routes = {l["route"] for o in r["options"] for l in o["legs"] if l["mode"] == "bus"}
    assert "R2UP" not in routes and "R1UP" in routes


def test_unknown_crowding_is_reported_not_guessed(planner):
    r = planner.plan(*A, *D, depart="08:50", day="weekday", comfort=0.5, window_min=30)
    only = r["options"][0]
    assert only["overall_label"] == "No data" and only["all_legs_have_data"] is False


def test_origin_far_from_any_stop_returns_an_error(planner):
    r = planner.plan(28.9, 77.6, *D, depart="08:00")
    assert r["options"] == [] and "walking distance" in r["error"]


def test_route_view_returns_one_row_per_stretch(planner):
    r = planner.route_stretches("1", "weekday", "am_peak")
    assert len(r["stretches"]) == 3 and all(s["level"] == 4 for s in r["stretches"])


def test_route_line_follows_the_road_shape(planner):
    first = planner.route_stretches("1", "weekday", "am_peak")["stretches"][0]
    assert first["pts"] is not None and [28.6, 77.21] in first["pts"]       # the bend of the road, not the straight line
    assert first["pts"][0] == [28.6, 77.2] and first["pts"][-1] == [28.61, 77.21]


def test_leg_stretches_carry_the_shape(planner):
    leg = planner.leg_crowding(planner.con.cursor(), "1", "A", "D", "weekday", "am_peak", 8)
    assert all(s["pts"] and len(s["pts"]) >= 2 for s in leg["stretches"])
    assert max(len(s["pts"]) for s in leg["stretches"]) >= 3


def test_routes_without_a_shape_stay_straight(planner):
    leg = planner.leg_crowding(planner.con.cursor(), "3", "A", "D", "weekday", "am_peak", 9)
    assert all(s["pts"] is None for s in leg["stretches"])
