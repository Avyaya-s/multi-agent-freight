"""Simulation time.

Fixed epoch, timezone-naive throughout. Nothing in this package may call
datetime.now() -- that is the one accidental call that silently breaks
reproducibility, and it does not raise or fail a test, so it is a discipline
enforced by convention, not by code. Grep for "datetime.now" before merging
if a test starts producing different results between two runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta

EPOCH = datetime(2024, 1, 1, 0, 0, 0)  # "day 1 of the simulation", 00:00


def sim_day(t: datetime) -> int:
    """1-indexed simulation day number for a given sim time."""
    return (t - EPOCH).days + 1


def hours(n: float) -> timedelta:
    return timedelta(hours=n)


def minutes(n: float) -> timedelta:
    return timedelta(minutes=n)
