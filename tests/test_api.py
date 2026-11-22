def test_meta_exposes_thresholds_and_note(client):
    j = client.get("/api/meta").json()
    assert set(j["thresholds"]) == {"seats_likely_below", "standing_below", "crowded_below"}
    assert "relative" in j["note"].lower()


def test_stop_search(client):
    r = client.get("/api/stops", params={"q": "alp"}).json()
    assert r and r[0]["name"] == "Alpha"


def test_journeys_contract(client):
    r = client.get("/api/journeys", params={"from_lat": 28.600, "from_lon": 77.200, "to_lat": 28.630, "to_lon": 77.230,
                                            "depart": "10:45", "day": "weekday", "comfort": 0.0, "window_min": 30})
    assert r.status_code == 200
    o = r.json()["options"][0]
    assert {"depart_origin", "arrive", "door_to_door_min", "legs", "overall_label", "tags"} <= set(o)
    bus = next(l for l in o["legs"] if l["mode"] == "bus")
    assert {"route", "from", "to", "depart", "arrive", "level", "label", "stretches"} <= set(bus)


def test_bad_input_is_rejected(client):
    assert client.get("/api/journeys", params={"from_lat": 1, "from_lon": 1, "to_lat": 1, "to_lon": 1,
                                               "depart": "noon"}).status_code == 422
    assert client.get("/api/journeys", params={"from_lat": 1, "from_lon": 1, "to_lat": 1, "to_lon": 1,
                                               "day": "monday"}).status_code == 422
