"""All metrics are computed from the event log alone -- never from live
simulator state -- so that Stage 3's replay viewer and any offline analysis
get identical numbers from the same file.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from freight_sim.events import EventType, read_events


@dataclass
class Metrics:
    num_trucks: int
    num_deals_completed: int
    num_backhaul_deals_completed: int
    num_deadhead_departures: int
    num_match_vetoed: int
    veto_by_reason: Counter = field(default_factory=Counter)
    empty_km: float = 0.0
    laden_km: float = 0.0
    fuel_liters: float = 0.0
    waiting_hours: float = 0.0
    idle_hours: float = 0.0
    trucker_earnings: float = 0.0
    broker_revenue: float = 0.0
    shipper_cost: float = 0.0

    @property
    def total_km(self) -> float:
        return self.empty_km + self.laden_km

    @property
    def loaded_return_share(self) -> float:
        """Of every time a truck was away from home and had to decide what's
        next, the fraction that ended in a loaded backhaul rather than an
        empty deadhead. Deliberately excludes at-home dispatch matches (those
        aren't "return trips" and would make this metric tautological -- see
        README)."""
        denom = self.num_backhaul_deals_completed + self.num_deadhead_departures
        return self.num_backhaul_deals_completed / denom if denom else 0.0

    def as_row(self) -> dict[str, float | int]:
        return {
            "trucks": self.num_trucks,
            "deals_completed": self.num_deals_completed,
            "backhaul_deals": self.num_backhaul_deals_completed,
            "deadhead_trips": self.num_deadhead_departures,
            "match_vetoed": self.num_match_vetoed,
            "empty_km": round(self.empty_km, 1),
            "laden_km": round(self.laden_km, 1),
            "loaded_return_share": round(self.loaded_return_share, 3),
            "fuel_liters": round(self.fuel_liters, 1),
            "waiting_hours": round(self.waiting_hours, 1),
            "idle_hours": round(self.idle_hours, 1),
            "trucker_earnings": round(self.trucker_earnings, 0),
            "broker_revenue": round(self.broker_revenue, 0),
            "shipper_cost": round(self.shipper_cost, 0),
        }

    def veto_rate_by_reason(self) -> dict[str, float]:
        """Fraction of all vetoes attributable to each reason. Even in Stage 0,
        vetoes are not rare -- the marketplace's coarse screen only checks
        capacity and a generous straight-line distance, so most of the real
        (road-network) detour and the truck's private home-deadline constraint
        only get checked at verification, producing a veto roughly twice as
        often as a successful match in the default config. Stage 1 replaces
        the coarse screen's straight-line distance with something closer to
        real feasibility, so this rate is expected to change, not appear from
        nothing."""
        total = sum(self.veto_by_reason.values())
        if not total:
            return {}
        return {reason: count / total for reason, count in self.veto_by_reason.items()}


def compute_metrics(log_path: str | Path) -> Metrics:
    fuel_efficiency_kmpl: dict[str, float] = {}
    num_trucks = 0
    num_deals_completed = 0
    num_backhaul_deals_completed = 0
    num_deadhead_departures = 0
    num_match_vetoed = 0
    veto_by_reason: Counter = Counter()
    empty_km = 0.0
    laden_km = 0.0
    km_by_truck: dict[str, float] = {}
    waiting_minutes = 0.0
    idle_minutes = 0.0
    trucker_earnings = 0.0
    broker_revenue = 0.0
    shipper_cost = 0.0

    for event in read_events(Path(log_path)):
        if event.event_type == EventType.TRUCK_SPAWNED:
            num_trucks += 1
            fuel_efficiency_kmpl[event.entity_id] = event.payload["fuel_efficiency_kmpl"]

        elif event.event_type == EventType.TRUCK_DEPARTED:
            truck_id = event.entity_id
            distance_km = event.payload["distance_km"]
            km_by_truck[truck_id] = km_by_truck.get(truck_id, 0.0) + distance_km
            if event.payload["laden"]:
                laden_km += distance_km
            else:
                empty_km += distance_km
                if event.payload["purpose"] == "deadhead_home":
                    num_deadhead_departures += 1

        elif event.event_type == EventType.TRUCK_WAIT_ENDED:
            waiting_minutes += event.payload["waited_min"]

        elif event.event_type == EventType.TRUCK_IDLE_ENDED:
            idle_minutes += event.payload["idle_min"]

        elif event.event_type == EventType.MATCH_VETOED:
            num_match_vetoed += 1
            veto_by_reason[event.payload["reason"]] += 1

        elif event.event_type == EventType.DEAL_COMPLETED:
            num_deals_completed += 1
            if event.payload.get("is_backhaul"):
                num_backhaul_deals_completed += 1
            revenue = event.payload["revenue"]
            commission_paid = event.payload["commission_paid"]
            trucker_earnings += revenue - commission_paid
            broker_revenue += commission_paid
            shipper_cost += revenue

    fuel_liters = sum(
        km / fuel_efficiency_kmpl[truck_id]
        for truck_id, km in km_by_truck.items()
        if truck_id in fuel_efficiency_kmpl
    )

    return Metrics(
        num_trucks=num_trucks,
        num_deals_completed=num_deals_completed,
        num_backhaul_deals_completed=num_backhaul_deals_completed,
        num_deadhead_departures=num_deadhead_departures,
        num_match_vetoed=num_match_vetoed,
        veto_by_reason=veto_by_reason,
        empty_km=empty_km,
        laden_km=laden_km,
        fuel_liters=fuel_liters,
        waiting_hours=waiting_minutes / 60.0,
        idle_hours=idle_minutes / 60.0,
        trucker_earnings=trucker_earnings,
        broker_revenue=broker_revenue,
        shipper_cost=shipper_cost,
    )
