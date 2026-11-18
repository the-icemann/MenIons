from app.eta import describe, distance_km, estimate_minutes

KISEMENTI = (0.3136, 32.5811)
KAMPALA_ROAD = (0.3355, 32.5960)  # about 3 km away
ENTEBBE = (0.0512, 32.4637)  # about 32 km away


def test_distance():
    assert 2.5 < distance_km(*KISEMENTI, *KAMPALA_ROAD) < 3.2
    assert distance_km(*KISEMENTI, *KISEMENTI) == 0


def test_a_short_trip():
    assert estimate_minutes(KISEMENTI, KAMPALA_ROAD, picked_up=False) == (20, 40)
    assert estimate_minutes(KISEMENTI, KAMPALA_ROAD, picked_up=True) == (5, 15)
    assert describe((20, 40)) == "about 20–40 min"


def test_a_long_trip_reads_in_hours():
    minutes = estimate_minutes(KISEMENTI, ENTEBBE, picked_up=False)
    assert minutes[0] >= 90 and describe(minutes).startswith("about 2 h")


def test_ranges_are_never_empty_and_a_missing_pin_gives_none():
    low, high = estimate_minutes(KISEMENTI, KISEMENTI, picked_up=True)
    assert high - low >= 10
    assert estimate_minutes((None, None), KISEMENTI, picked_up=False) is None
    assert describe(None) == ""
