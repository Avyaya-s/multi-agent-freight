from datetime import datetime

from freight_sim.config import MatchingConfig
from freight_sim.events import VetoReason
from freight_sim.matching import propose_candidates, verify_match
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


def test_verify_match_vetoes_on_capacity(fake_network):
    truck = make_truck()
    truck.platform.spare_capacity_weight_kg = 1000
    load = make_load()
    result = verify_match(truck, load, fake_network, datetime(2024, 1, 1, 8))
    assert not result.ok
    assert result.reason == VetoReason.CAPACITY


def test_verify_match_vetoes_on_detour_limit(fake_network):
    truck = make_truck()
    truck.private.max_detour_km = 1.0  # the truck's own private threshold, tighter than reality
    load = make_load()
    result = verify_match(truck, load, fake_network, datetime(2024, 1, 1, 8))
    assert not result.ok
    assert result.reason == VetoReason.DETOUR_LIMIT


def test_verify_match_vetoes_on_time_window(fake_network):
    truck = make_truck()
    load = make_load()
    load.public.pickup_window = (datetime(2024, 1, 1, 9), datetime(2024, 1, 1, 9, 1))  # closes almost immediately
    result = verify_match(truck, load, fake_network, datetime(2024, 1, 1, 10))  # already past the window
    assert not result.ok
    assert result.reason == VetoReason.TIME_WINDOW


def test_verify_match_vetoes_on_home_deadline(fake_network):
    truck = make_truck()
    load = make_load()
    truck.private.home_by = load.public.pickup_window[0]  # before the job could possibly even start
    result = verify_match(truck, load, fake_network, datetime(2024, 1, 1, 8))
    assert not result.ok
    assert result.reason == VetoReason.HOME_DEADLINE


def test_verify_match_succeeds_for_a_feasible_load(fake_network):
    truck = make_truck()
    load = make_load()
    result = verify_match(truck, load, fake_network, datetime(2024, 1, 1, 8))
    assert result.ok
    assert result.reason is None
    assert result.detour_km > 0
