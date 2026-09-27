from freight_sim.config import MatchingConfig
from freight_sim.matching import propose_candidates
from freight_sim.models import Location
from test_models import make_load, make_truck

MATCHING_CFG = MatchingConfig(
    broker_commission_rate=0.10,
    max_candidate_detour_km=25,
    retry_interval_hours=1.0,
    home_deadline_buffer_hours=2.0,
)


def test_propose_candidates_filters_by_capacity(fake_network):
    truck = make_truck()
    truck.platform.spare_capacity_weight_kg = 1000  # too small for the default 5000kg load
    load = make_load()
    assert propose_candidates(truck, [load], MATCHING_CFG) == []


def test_propose_candidates_filters_by_screening_distance(fake_network):
    truck = make_truck()
    truck.platform.current_location = Location(lat=20.0, lon=80.0)  # far away
    load = make_load()
    assert propose_candidates(truck, [load], MATCHING_CFG) == []


def test_propose_candidates_accepts_a_feasible_load(fake_network):
    truck = make_truck()
    load = make_load()
    assert propose_candidates(truck, [load], MATCHING_CFG) == [load]
