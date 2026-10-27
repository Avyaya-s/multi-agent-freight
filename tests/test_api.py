"""API tests use the real cached road network (loaded once per module, not
per test -- it's the same multi-second load batch mode already pays) but
swap in a fake LLM backend so no test spends real Groq budget or depends on
network access. Real-model quality is covered live by
scripts/check_negotiation_quality.py, not here."""

import time

import pytest
from fastapi.testclient import TestClient

import freight_sim.api as api_module
from freight_sim.llm_backend import LLMCallError


class AlwaysFailingBackend:
    """Forces RuleBasedStrategy fallback -- deterministic, no real API calls."""

    def complete(self, system_prompt, user_prompt, model, timeout_seconds):
        raise LLMCallError("forced failure for deterministic fallback in tests")


PEENYA = {"lat": 13.0284, "lon": 77.5199}

TRUCK_PAYLOAD = {
    "capacity_weight_kg": 20000.0,
    "capacity_volume_m3": 60.0,
    "home_lat": PEENYA["lat"],
    "home_lon": PEENYA["lon"],
    "reservation_rate_per_km": 5.0,  # deliberately low: guarantees acceptance
    "max_detour_km": 200.0,
    "max_wait_hours": 24.0,
    "fixed_cost_per_day": 1500.0,
    "variable_cost_per_km": 10.0,
    "fuel_efficiency_kmpl": 4.5,
    "variable_cost_per_hour_waiting": 60.0,
    "return_leg_discount": 0.15,
}


@pytest.fixture(scope="module")
def client():
    with TestClient(api_module.app) as c:
        api_module._state.backend = AlwaysFailingBackend()
        yield c


def _wait_for_session_done(client: TestClient, session_id: str, timeout_s: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = client.get(f"/sessions/{session_id}").json()
        if body["done"]:
            return body
        time.sleep(0.05)
    raise TimeoutError(f"session {session_id} did not finish within {timeout_s}s")


def test_unknown_session_returns_404(client: TestClient):
    resp = client.get("/sessions/does-not-exist")
    assert resp.status_code == 404


def test_create_session_requires_matching_role_payload(client: TestClient):
    resp = client.post("/sessions", json={"role": "trucker"})  # missing `truck`
    assert resp.status_code == 422


def test_trucker_session_runs_end_to_end_and_can_be_accepted(client: TestClient):
    resp = client.post("/sessions", json={"role": "trucker", "truck": TRUCK_PAYLOAD})
    assert resp.status_code == 201
    session_id = resp.json()["session_id"]

    final = _wait_for_session_done(client, session_id)
    assert final["role"] == "trucker"
    assert final["ended_reason"] in ("candidates_exhausted", "llm_call_cap_reached")
    assert len(final["deals"]) >= 1  # reservation_rate_per_km=5.0 guarantees a fallback accept

    deal = final["deals"][0]
    assert deal["status"] == "verified"

    accept_resp = client.post(f"/sessions/{session_id}/deals/{deal['deal_id']}/accept")
    assert accept_resp.status_code == 200
    assert accept_resp.json()["status"] == "active"

    # Can't accept twice.
    second_accept = client.post(f"/sessions/{session_id}/deals/{deal['deal_id']}/accept")
    assert second_accept.status_code == 409


def test_websocket_streams_backlog_then_session_ended(client: TestClient):
    resp = client.post("/sessions", json={"role": "trucker", "truck": TRUCK_PAYLOAD})
    session_id = resp.json()["session_id"]
    _wait_for_session_done(client, session_id)  # let it finish first -- tests backlog replay, not live streaming

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        event_types = []
        while True:
            event = ws.receive_json()
            event_types.append(event["event_type"])
            if event["event_type"] == "session_ended":
                break
    assert event_types[0] == "session_started"
    assert event_types[-1] == "session_ended"


def test_session_rate_limit_returns_429(client: TestClient):
    original = api_module._state.config.interactive.max_sessions_per_hour
    api_module._state.config.interactive.max_sessions_per_hour = 0
    try:
        resp = client.post("/sessions", json={"role": "trucker", "truck": TRUCK_PAYLOAD})
        assert resp.status_code == 429
    finally:
        api_module._state.config.interactive.max_sessions_per_hour = original


def test_recordings_endpoint_lists_available_files(client: TestClient):
    resp = client.get("/recordings")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
