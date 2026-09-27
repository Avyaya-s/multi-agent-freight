"""check_feasibility's signature only accepts a TruckPlatform, not a Truck --
so unlike propose_candidates, there is no private field for it to even
structurally reach; that's a stronger guarantee than a runtime tripwire.
check_preference, which does take TruckPrivate, is exactly where the
private-data boundary is meant to live -- see feasibility.py's module
docstring.
"""

from dataclasses import replace
from datetime import datetime, timedelta

from freight_sim.config import SolverConfig
from freight_sim.events import FeasibilityLayer, VetoReason
from freight_sim.feasibility import check_feasibility, check_preference
from test_models import make_load, make_truck

SOLVER_CFG = SolverConfig(time_limit_ms=50, first_solution_strategy="PATH_CHEAPEST_ARC")
DEPART = datetime(2024, 1, 1, 8)


def physical_ok(fake_network, truck=None, load=None):
    truck = truck or make_truck()
    load = load or make_load()
    [result] = check_feasibility(truck.platform, [load], [], fake_network, DEPART, SOLVER_CFG)
    assert result.feasible, f"expected feasible, got veto reason {result.reason}"
    return truck, load, result


# ---------------------------------------------------------------------- #
# PHYSICAL layer
# ---------------------------------------------------------------------- #


def test_check_feasibility_succeeds_for_a_feasible_load(fake_network):
    _, _, result = physical_ok(fake_network)
    assert result.layer == FeasibilityLayer.PHYSICAL
    assert result.reason is None
    assert result.detour_km > 0
    assert result.pickup_eta is not None and result.delivery_eta is not None


def test_check_feasibility_vetoes_on_capacity(fake_network):
    truck = make_truck()
    truck.platform.spare_capacity_weight_kg = 1000  # too small for the default 5000kg load
    load = make_load()
    [result] = check_feasibility(truck.platform, [load], [], fake_network, DEPART, SOLVER_CFG)
    assert not result.feasible
    assert result.layer == FeasibilityLayer.PHYSICAL
    assert result.reason == VetoReason.CAPACITY
    assert result.solver_ms == 0.0  # fast pre-check, solver never invoked


def test_check_feasibility_vetoes_on_time_window(fake_network):
    truck = make_truck()
    load = make_load()
    load.public.pickup_window = (datetime(2024, 1, 1, 9), datetime(2024, 1, 1, 9, 1))  # closes almost immediately
    [result] = check_feasibility(truck.platform, [load], [], fake_network, datetime(2024, 1, 1, 10), SOLVER_CFG)
    assert not result.feasible
    assert result.layer == FeasibilityLayer.PHYSICAL
    assert result.reason == VetoReason.TIME_WINDOW


def test_check_feasibility_evaluates_a_batch_independently(fake_network):
    truck = make_truck()
    feasible_load = make_load(load_id="load-ok")
    infeasible_load = make_load(load_id="load-bad")
    infeasible_load.public.weight_kg = 999_999
    results = check_feasibility(truck.platform, [feasible_load, infeasible_load], [], fake_network, DEPART, SOLVER_CFG)
    assert results[0].feasible
    assert not results[1].feasible
    assert results[1].reason == VetoReason.CAPACITY


def test_check_feasibility_determinism(fake_network):
    truck = make_truck()
    load = make_load()
    [a] = check_feasibility(truck.platform, [load], [], fake_network, DEPART, SOLVER_CFG)
    [b] = check_feasibility(truck.platform, [load], [], fake_network, DEPART, SOLVER_CFG)
    # solver_ms is wall-clock timing of the call, not part of the solution --
    # excluded so this only asserts the actual result is identical.
    assert replace(a, solver_ms=0.0) == replace(b, solver_ms=0.0)


# ---------------------------------------------------------------------- #
# PREFERENCE layer
# ---------------------------------------------------------------------- #


def test_check_preference_succeeds_for_a_feasible_load(fake_network):
    truck, load, physical = physical_ok(fake_network)
    result = check_preference(truck.private, load, physical, is_backhaul=False, network=fake_network)
    assert result.feasible
    assert result.layer == FeasibilityLayer.PREFERENCE
    assert result.marginal_cost is not None and result.marginal_cost > 0


def test_check_preference_vetoes_on_detour_limit(fake_network):
    truck, load, physical = physical_ok(fake_network)
    truck.private.max_detour_km = 1.0  # tighter than the real detour
    result = check_preference(truck.private, load, physical, is_backhaul=False, network=fake_network)
    assert not result.feasible
    assert result.layer == FeasibilityLayer.PREFERENCE
    assert result.reason == VetoReason.DETOUR_LIMIT


def test_check_preference_vetoes_on_rate_too_low(fake_network):
    truck, load, physical = physical_ok(fake_network)
    truck.private.reservation_rate_per_km = 20.0  # above the load's posted 13/km
    result = check_preference(truck.private, load, physical, is_backhaul=False, network=fake_network)
    assert not result.feasible
    assert result.reason == VetoReason.RATE_TOO_LOW


def test_check_preference_backhaul_discount_can_rescue_a_rate_that_would_otherwise_fail(fake_network):
    truck, load, physical = physical_ok(fake_network)
    truck.private.reservation_rate_per_km = 15.0  # above the load's posted 13/km
    truck.private.return_leg_discount = 0.2  # discounted threshold: 15 * 0.8 = 12 <= 13

    at_home = check_preference(truck.private, load, physical, is_backhaul=False, network=fake_network)
    assert not at_home.feasible and at_home.reason == VetoReason.RATE_TOO_LOW

    backhaul = check_preference(truck.private, load, physical, is_backhaul=True, network=fake_network)
    assert backhaul.feasible


def test_check_preference_vetoes_on_home_deadline(fake_network):
    truck, load, physical = physical_ok(fake_network)
    truck.private.home_by = physical.pickup_eta - timedelta(hours=1)  # before the job could possibly finish
    result = check_preference(truck.private, load, physical, is_backhaul=False, network=fake_network)
    assert not result.feasible
    assert result.reason == VetoReason.HOME_DEADLINE
