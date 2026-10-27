"""FastAPI layer over the interactive-mode engine (session.py) -- build order
step 2 in CLAUDE.md. Batch mode (simulator.py, scripts/run_*.py) is untouched
by this module and has no HTTP surface; this is the live, human-in-the-loop
path only.

No persistence (per CLAUDE.md: ephemeral in-memory session, no login, no
DB) -- `_AppState.sessions` is a plain dict that forgets everything on
restart. A session's negotiation runs in a background thread (the OpenAI
client is synchronous) and pushes events into any subscribed WebSocket via
`asyncio.run_coroutine_threadsafe`, so the live feed and the per-session LLM
calls never block each other.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from freight_sim.config import Config, load_config
from freight_sim.events import EventType, NegotiationParty
from freight_sim.llm_backend import GroqBackend, LLMBackend
from freight_sim.models import Deal, DealStatus
from freight_sim.session import (
    InteractiveSession,
    VisitorLoadInput,
    VisitorTruckInput,
    build_visitor_load,
    build_visitor_truck,
)
from freight_sim.straight_line_network import Network, StraightLineNetwork

ROOT = Path(__file__).resolve().parent.parent.parent
RECORDINGS_DIR = ROOT / "data" / "recordings"


# --------------------------------------------------------------------------- #
# app state -- set once in lifespan, not per-request
# --------------------------------------------------------------------------- #


@dataclass
class _AppState:
    config: Config
    network: Network
    backend: LLMBackend
    executor: ThreadPoolExecutor
    loop: asyncio.AbstractEventLoop
    sessions: dict[str, "_SessionState"] = field(default_factory=dict)


@dataclass
class _SessionState:
    session: InteractiveSession
    backlog: list[dict] = field(default_factory=list)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    done: bool = False


_state: _AppState | None = None

# Per-session rate limit on "run it live" -- shared across all visitors
# (there are no accounts to key a per-user limit on). Separate from
# RateLimiter, which tracks per-model RPM/RPD/TPM, not session creation.
_session_creation_times: deque[float] = deque()
_session_creation_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _state
    config, _hash = load_config(ROOT / "config" / "default.yaml")
    # Interactive mode uses the approximate straight-line network, not batch
    # mode's real RoadNetwork -- timing a live session on the real one
    # measured ~30s to load the cached graph plus ~40s of per-session
    # screening (fresh visitor-chosen coordinates can't be cache-warmed),
    # incompatible with a "live" demo. Decided with the user; see
    # straight_line_network.py's module docstring.
    network = StraightLineNetwork()
    backend = GroqBackend()
    # Captured here, not read via asyncio.get_running_loop() inside a route
    # handler: FastAPI runs a sync `def` handler in anyio's worker thread,
    # which has no running loop of its own -- lifespan is the one place that
    # actually runs on the real loop.
    loop = asyncio.get_running_loop()
    _state = _AppState(config=config, network=network, backend=backend, executor=ThreadPoolExecutor(max_workers=4), loop=loop)
    yield
    _state.executor.shutdown(wait=False)


app = FastAPI(title="Freight Marketplace -- Interactive API", lifespan=lifespan)

# Dev-permissive: the eventual deploy (Vercel frontend / Railway or Fly.io
# API, per CLAUDE.md's build order) is a different origin from this API, and
# there is no login/session cookie to protect, so a wide-open CORS policy
# matches the sandbox's actual trust boundary (nothing is private past the
# visitor's own agent config, which never touches another visitor's session).
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _require_state() -> _AppState:
    if _state is None:
        raise HTTPException(status_code=503, detail="Server is still starting up.")
    return _state


def _check_session_rate_limit(max_per_hour: int) -> None:
    now = time.monotonic()
    with _session_creation_lock:
        while _session_creation_times and now - _session_creation_times[0] > 3600:
            _session_creation_times.popleft()
        if len(_session_creation_times) >= max_per_hour:
            raise HTTPException(status_code=429, detail="Live session rate limit reached across all visitors; try again later, or watch a recorded replay instead.")
        _session_creation_times.append(now)


def _get_session(session_id: str) -> _SessionState:
    state = _require_state()
    session_state = state.sessions.get(session_id)
    if session_state is None:
        raise HTTPException(status_code=404, detail="Session not found (sessions are in-memory and do not survive a server restart).")
    return session_state


def _make_emit(session_state: _SessionState, loop: asyncio.AbstractEventLoop):
    seq_counter = itertools.count()

    def emit(event_type: EventType, entity_id: str, payload: dict) -> None:
        event = {
            "seq": next(seq_counter),
            "event_type": event_type.value,
            "entity_id": entity_id,
            "payload": payload,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with session_state.lock:
            session_state.backlog.append(event)
            subscribers = list(session_state.subscribers)
            if event_type == EventType.SESSION_ENDED:
                session_state.done = True
        for queue in subscribers:
            asyncio.run_coroutine_threadsafe(queue.put(event), loop)

    return emit


def _run_session_safely(session: InteractiveSession, emit, session_state: _SessionState) -> None:
    """Runs in a worker thread -- an uncaught exception here would otherwise
    vanish silently instead of reaching any caller, which is exactly the
    "never hangs, never silently fails" failure mode CLAUDE.md's LLM-call
    budget guard already exists to prevent. Emit a terminal event either way."""
    try:
        session.run(emit)
    except Exception as e:  # noqa: BLE001 -- top of a background thread; must not vanish silently
        emit(EventType.SESSION_ENDED, session.session_id, {"reason": "error", "error": str(e), "llm_calls_used": session.llm_calls_used, "deals_created": len(session.deals)})


def _deal_to_dict(deal: Deal) -> dict:
    return {
        "deal_id": deal.deal_id,
        "negotiation_id": deal.negotiation_id,
        "truck_id": deal.truck_id,
        "load_id": deal.load_id,
        "agreed_rate_per_km": deal.agreed_rate_per_km,
        "detour_km": deal.detour_km,
        "pickup_by": deal.pickup_by.isoformat(),
        "deliver_by": deal.deliver_by.isoformat(),
        "status": deal.status.value,
        "created_at": deal.created_at.isoformat(),
    }


# --------------------------------------------------------------------------- #
# request/response shapes -- mirror what the onboarding form (build order
# step 4, not yet built) will eventually submit; the API accepts this shape
# directly until that form exists.
# --------------------------------------------------------------------------- #


class VisitorTruckRequest(BaseModel):
    capacity_weight_kg: float
    capacity_volume_m3: float
    home_lat: float
    home_lon: float
    reservation_rate_per_km: float
    max_detour_km: float
    max_wait_hours: float
    fixed_cost_per_day: float
    variable_cost_per_km: float
    fuel_efficiency_kmpl: float
    variable_cost_per_hour_waiting: float
    return_leg_discount: float
    home_by: datetime | None = None


class VisitorLoadRequest(BaseModel):
    weight_kg: float
    volume_m3: float
    origin_lat: float
    origin_lon: float
    destination_lat: float
    destination_lon: float
    posted_rate_per_km: float
    reservation_rate_per_km: float
    penalty_per_hour_late: float
    loading_time_min: float
    unloading_time_min: float


class CreateSessionRequest(BaseModel):
    role: Literal["trucker", "shipper"]
    truck: VisitorTruckRequest | None = None
    load: VisitorLoadRequest | None = None


class CreateSessionResponse(BaseModel):
    session_id: str


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #


@app.post("/sessions", response_model=CreateSessionResponse, status_code=201)
def create_session(body: CreateSessionRequest) -> CreateSessionResponse:
    state = _require_state()
    _check_session_rate_limit(state.config.interactive.max_sessions_per_hour)

    role = NegotiationParty.TRUCKER if body.role == "trucker" else NegotiationParty.SHIPPER
    visitor_truck = visitor_load = None
    if role == NegotiationParty.TRUCKER:
        if body.truck is None:
            raise HTTPException(status_code=422, detail="role='trucker' requires a `truck` config.")
        visitor_truck = build_visitor_truck(state.network, VisitorTruckInput(**body.truck.model_dump()))
    else:
        if body.load is None:
            raise HTTPException(status_code=422, detail="role='shipper' requires a `load` config.")
        visitor_load = build_visitor_load(state.network, state.config, VisitorLoadInput(**body.load.model_dump()))

    session_id = str(uuid.uuid4())
    session = InteractiveSession(session_id, state.config, state.network, state.backend, role, visitor_truck, visitor_load)
    session_state = _SessionState(session=session)
    state.sessions[session_id] = session_state

    emit = _make_emit(session_state, state.loop)
    state.executor.submit(_run_session_safely, session, emit, session_state)

    return CreateSessionResponse(session_id=session_id)


@app.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    session_state = _get_session(session_id)
    return {
        "session_id": session_id,
        "role": session_state.session.role.value,
        "done": session_state.done,
        "llm_calls_used": session_state.session.llm_calls_used,
        "ended_reason": session_state.session.ended_reason if session_state.done else None,
        "deals": [_deal_to_dict(d) for d in session_state.session.deals.values()],
    }


@app.get("/sessions/{session_id}/deals")
def list_deals(session_id: str) -> list[dict]:
    session_state = _get_session(session_id)
    return [_deal_to_dict(d) for d in session_state.session.deals.values()]


@app.post("/sessions/{session_id}/deals/{deal_id}/accept")
def accept_deal(session_id: str, deal_id: str) -> dict:
    session_state = _get_session(session_id)
    deal = session_state.session.deals.get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Deal not found in this session.")
    if deal.status != DealStatus.VERIFIED:
        raise HTTPException(status_code=409, detail=f"Deal is '{deal.status.value}', not pending approval.")
    deal.status = DealStatus.ACTIVE
    _make_emit(session_state, _require_state().loop)(EventType.DEAL_ACCEPTED, deal_id, {"deal_id": deal_id})
    return _deal_to_dict(deal)


@app.post("/sessions/{session_id}/deals/{deal_id}/reject")
def reject_deal(session_id: str, deal_id: str) -> dict:
    session_state = _get_session(session_id)
    deal = session_state.session.deals.get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Deal not found in this session.")
    if deal.status != DealStatus.VERIFIED:
        raise HTTPException(status_code=409, detail=f"Deal is '{deal.status.value}', not pending approval.")
    deal.status = DealStatus.FAILED
    _make_emit(session_state, _require_state().loop)(EventType.DEAL_REJECTED, deal_id, {"deal_id": deal_id})
    return _deal_to_dict(deal)


@app.websocket("/sessions/{session_id}/ws")
async def session_events_ws(websocket: WebSocket, session_id: str) -> None:
    state = _require_state()
    session_state = state.sessions.get(session_id)
    if session_state is None:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue()
    with session_state.lock:
        backlog_snapshot = list(session_state.backlog)
        already_done = session_state.done
        session_state.subscribers.append(queue)

    try:
        for event in backlog_snapshot:
            await websocket.send_json(event)
        if already_done:
            return
        while True:
            event = await queue.get()
            await websocket.send_json(event)
            if event["event_type"] == EventType.SESSION_ENDED.value:
                break
    except WebSocketDisconnect:
        pass
    finally:
        with session_state.lock:
            if queue in session_state.subscribers:
                session_state.subscribers.remove(queue)


# --------------------------------------------------------------------------- #
# replay -- the PRIMARY demo experience per CLAUDE.md, not a fallback: most
# visitors should land here, costing nothing. No curated "good session"
# recordings exist yet (that is explicitly offline, future work -- see
# CLAUDE.md), so this only wires the plumbing against whatever .jsonl files
# are dropped in data/recordings/.
# --------------------------------------------------------------------------- #


@app.get("/recordings")
def list_recordings() -> list[str]:
    if not RECORDINGS_DIR.exists():
        return []
    return sorted(p.stem for p in RECORDINGS_DIR.glob("*.jsonl"))


@app.websocket("/recordings/{name}/ws")
async def replay_ws(websocket: WebSocket, name: str) -> None:
    path = RECORDINGS_DIR / f"{name}.jsonl"
    if not path.exists():
        await websocket.close(code=4404)
        return
    await websocket.accept()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                raw_event = json.loads(line)
                # Re-shaped to the same envelope the live WS sends, so the
                # frontend has one rendering code path for both -- the raw
                # on-disk Event schema (events.py) isn't what either
                # consumer should need to know about.
                await websocket.send_json(
                    {
                        "seq": raw_event["seq"],
                        "event_type": raw_event["event_type"],
                        "entity_id": raw_event["entity_id"],
                        "payload": raw_event["payload"],
                        "timestamp": raw_event["sim_time"],
                    }
                )
                # Light pacing so the frontend can animate turn-by-turn --
                # not a real-time replay of the original sim_time gaps.
                await asyncio.sleep(0.3)
    except WebSocketDisconnect:
        pass
