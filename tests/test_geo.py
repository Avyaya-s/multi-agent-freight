from freight_sim.geo import haversine_km
from freight_sim.models import Location


def test_haversine_zero_for_same_point():
    a = Location(lat=13.0, lon=77.5)
    assert haversine_km(a, a) == 0.0


def test_haversine_symmetric():
    a = Location(lat=13.0284, lon=77.5199)  # Peenya
    b = Location(lat=12.8151, lon=77.6910)  # Bommasandra
    assert haversine_km(a, b) == haversine_km(b, a)


def test_haversine_matches_known_scale():
    # Peenya to Bommasandra is roughly 25-30km straight-line.
    a = Location(lat=13.0284, lon=77.5199)
    b = Location(lat=12.8151, lon=77.6910)
    distance = haversine_km(a, b)
    assert 20 < distance < 35
