from datetime import datetime

from freight_sim.models import (
    Deal,
    DealStatus,
    Load,
    LoadPrivate,
    LoadPublic,
    LoadStatus,
    Location,
    MatchMechanism,
    Truck,
    TruckPlatform,
    TruckPrivate,
    TruckPublic,
    TruckStatus,
)


def make_truck(truck_id: str = "truck-001") -> Truck:
    # Deliberately offset from make_load()'s origin (13.0, 77.5) so tests exercise
    # a real, non-zero detour/reposition leg rather than a coincidental 0km one.
    home = Location(lat=13.05, lon=77.55)
    return Truck(
        public=TruckPublic(truck_id=truck_id, owner_id="owner-001", capacity_weight_kg=10000, capacity_volume_m3=30),
        platform=TruckPlatform(
            current_location=home,
            status=TruckStatus.IDLE,
            available_from=datetime(2024, 1, 1),
            spare_capacity_weight_kg=10000,
            spare_capacity_volume_m3=30,
        ),
        private=TruckPrivate(
            fixed_cost_per_day=1500,
            variable_cost_per_km=10,
            fuel_efficiency_kmpl=4.5,
            variable_cost_per_hour_waiting=60,
            reservation_rate_per_km=12,
            return_leg_discount=0.15,
            home_location=home,
            home_by=None,
            max_detour_km=20,
            max_wait_hours=12,
        ),
    )


def make_load(load_id: str = "load-001") -> Load:
    origin = Location(lat=13.0, lon=77.5)
    destination = Location(lat=12.9, lon=77.7)
    return Load(
        public=LoadPublic(
            load_id=load_id,
            shipper_id="shipper-001",
            origin=origin,
            destination=destination,
            weight_kg=5000,
            volume_m3=15,
            pickup_window=(datetime(2024, 1, 1, 9), datetime(2024, 1, 1, 12)),
            delivery_window=(datetime(2024, 1, 1, 12), datetime(2024, 1, 1, 18)),
            posted_rate_per_km=13,
            loading_time_min=30,
            unloading_time_min=30,
            posted_at=datetime(2024, 1, 1, 6),
            status=LoadStatus.OPEN,
        ),
        private=LoadPrivate(reservation_rate_per_km=15, penalty_per_hour_late=200),
    )


def test_truck_public_private_tiers_are_separate_objects():
    truck = make_truck()
    assert truck.truck_id == "truck-001"
    assert truck.public.capacity_weight_kg == 10000
    assert truck.private.reservation_rate_per_km == 12
    # private fields must not appear on the public/platform tiers
    assert not hasattr(truck.public, "reservation_rate_per_km")
    assert not hasattr(truck.platform, "reservation_rate_per_km")


def test_load_public_private_tiers_are_separate_objects():
    load = make_load()
    assert load.load_id == "load-001"
    assert load.public.posted_rate_per_km == 13
    assert load.private.reservation_rate_per_km == 15
    assert not hasattr(load.public, "reservation_rate_per_km")


def test_location_is_frozen_and_hashable():
    a = Location(lat=1.0, lon=2.0)
    b = Location(lat=1.0, lon=2.0)
    assert a == b
    assert hash(a) == hash(b)
    assert {a, b} == {a}


def test_deal_holds_final_terms_only():
    deal = Deal(
        deal_id="deal-1",
        negotiation_id="neg-1",
        truck_id="truck-001",
        load_id="load-001",
        mechanism=MatchMechanism.RULE_BASED,
        agreed_rate_per_km=13,
        commission_rate=None,
        pickup_by=datetime(2024, 1, 1, 12),
        deliver_by=datetime(2024, 1, 1, 18),
        detour_km=5.0,
        status=DealStatus.COMPLETED,
        created_at=datetime(2024, 1, 1, 9),
    )
    assert deal.status == DealStatus.COMPLETED
    assert not hasattr(deal, "offers")  # no negotiation transcript on the Deal itself
