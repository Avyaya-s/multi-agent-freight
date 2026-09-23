"""Core data model: Truck, Load, Deal.

Visibility tiers (enforced by convention: code that builds an agent's view
must only read the tier(s) that agent is allowed to see):

  Truck.public   -- static vehicle facts, visible to everyone (identity, max capacity)
  Truck.platform -- dynamic operational state, visible to the marketplace agent only
                    (location, status, spare capacity). Competing truckers must not
                    see this, or Stage 5's collusion/observation experiments are moot.
  Truck.private  -- visible only to the truck's own agent (costs, reservation price,
                    personal constraints)

  Load.public    -- visible to everyone (this is a load board; the load's existence
                    and terms are meant to be seen)
  Load.private   -- visible only to the shipper's own agent (true reservation price,
                    late penalty)

The event log is a separate, omniscient concern (see events.py): it may contain
private fields, because it is the simulator's own record used for metrics and
replay, not a channel between agents.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


@dataclass(frozen=True)
class Location:
    lat: float
    lon: float
    node_id: int | None = None  # OSMnx graph node, once the road network is loaded


# --------------------------------------------------------------------------- #
# Truck
# --------------------------------------------------------------------------- #


class TruckStatus(str, Enum):
    """See README "Idle vs waiting" for the metric-relevant boundary.

    WAITING: at a destination, holding out for a return load. Counts toward
             the waiting-time metric.
    IDLE:    at home base, nothing scheduled. Does not count toward waiting time.
    """

    IDLE = "idle"
    WAITING = "waiting"
    LOADING = "loading"
    UNLOADING = "unloading"
    EN_ROUTE_LADEN = "en_route_laden"
    EN_ROUTE_EMPTY = "en_route_empty"


@dataclass
class TruckPublic:
    """Static vehicle facts. Visible to everyone."""

    truck_id: str
    owner_id: str
    capacity_weight_kg: float
    capacity_volume_m3: float


@dataclass
class TruckPlatform:
    """Dynamic operational state. Visible to the marketplace agent only."""

    current_location: Location
    status: TruckStatus
    available_from: datetime
    spare_capacity_weight_kg: float
    spare_capacity_volume_m3: float


@dataclass
class TruckPrivate:
    """Visible only to the truck's own agent. Never reaches the marketplace."""

    fixed_cost_per_day: float  # driver wage + EMI/lease, amortized
    variable_cost_per_km: float  # wear + tolls (fuel is tracked separately, see fuel_efficiency_kmpl)
    fuel_efficiency_kmpl: float  # km per litre, for fuel-burned and CO2 metrics
    variable_cost_per_hour_waiting: float
    reservation_rate_per_km: float  # minimum acceptable rate, laden leg
    return_leg_discount: float  # fraction below reservation accepted on a backhaul
    home_location: Location
    home_by: datetime | None  # e.g. "must be home by Sunday"; None if no deadline
    max_detour_km: float
    max_wait_hours: float  # after which the trucker gives up and deadheads


@dataclass
class Truck:
    public: TruckPublic
    platform: TruckPlatform
    private: TruckPrivate

    @property
    def truck_id(self) -> str:
        return self.public.truck_id


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #


class LoadStatus(str, Enum):
    OPEN = "open"
    MATCHED = "matched"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    EXPIRED = "expired"


@dataclass
class LoadPublic:
    """A load board posting. Visible to everyone, by design."""

    load_id: str
    shipper_id: str
    origin: Location
    destination: Location
    weight_kg: float
    volume_m3: float
    pickup_window: tuple[datetime, datetime]
    delivery_window: tuple[datetime, datetime]
    posted_rate_per_km: float  # advertised/asking rate
    loading_time_min: float  # depends on cargo/site, not the vehicle
    unloading_time_min: float
    posted_at: datetime
    status: LoadStatus


@dataclass
class LoadPrivate:
    """Visible only to the shipper's own agent."""

    reservation_rate_per_km: float  # max the shipper is truly willing to pay
    penalty_per_hour_late: float


@dataclass
class Load:
    public: LoadPublic
    private: LoadPrivate

    @property
    def load_id(self) -> str:
        return self.public.load_id


# --------------------------------------------------------------------------- #
# Deal
# --------------------------------------------------------------------------- #


class MatchMechanism(str, Enum):
    NONE = "none"  # baseline: no marketplace
    RULE_BASED = "rule_based"
    BROKER_COMMISSION = "broker_commission"


class DealStatus(str, Enum):
    PROPOSED = "proposed"
    VERIFIED = "verified"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Deal:
    """Final agreed terms only. Negotiation history (Stage 2+) lives in the
    event log, keyed by negotiation_id, not in this record."""

    deal_id: str
    negotiation_id: str
    truck_id: str
    load_id: str
    mechanism: MatchMechanism
    agreed_rate_per_km: float
    commission_rate: float | None  # only set for BROKER_COMMISSION
    pickup_by: datetime
    deliver_by: datetime
    detour_km: float
    status: DealStatus
    created_at: datetime
